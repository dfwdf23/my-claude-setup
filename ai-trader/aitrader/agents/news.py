"""정보수집 에이전트 — 종목 소식을 거의 실시간으로 모아 매매 판단에 반영한다(느린 경로).

  1. 수집   종목별 RSS/Atom 피드 + 증권사 뉴스 API를 돌아가며 조회. 같은 제목은 한 번만.
  2. 즉시 판정(규칙, 0토큰)  제목의 키워드로 호재/악재 점수 → 악재면 그 종목 신규 매수를 일정 시간 차단.
  3. 정밀 판정(Claude, 가끔)  새 제목을 모아 한 번에 분류(최소 간격·하루 횟수·토큰 예산 제한).
     규칙이 잘못 막은 것은 풀고, '심각한 악재'로 확인되면 보유분 청산을 지시한다.
핫패스(엔진)는 이 에이전트가 갱신하는 engine.news / engine.exit_now 를 읽기만 한다.
"""
from __future__ import annotations

import asyncio
import heapq
import hashlib
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..config import Config
from ..llm import LLM
from ..models import NewsState
from .base import Agent

ATOM = "{http://www.w3.org/2005/Atom}"
MAX_FEED_BYTES = 2_000_000

# (정규식, 점수, 분류) — 점수는 −1(강한 악재) ~ +1(강한 호재). 제목 한 줄에 여러 개가 걸리면 더한다.
_RULES = [
    (r"public offering|secondary offering|stock offering|share sale|dilut|유상증자", -0.8, "offering"),
    (r"miss(es|ed)? (on )?(estimates|expectations|earnings|revenue|forecasts?)|disappoint|weak (guidance|outlook|results)"
     r"|(cuts?|lowers?|slash(es)?|withdraws?) (its )?(full-year |annual )?(guidance|forecast|outlook)|profit warning"
     r"|실적 부진|어닝 ?쇼크|가이던스 하향", -0.7, "earnings"),
    (r"downgrad|(cuts?|lowers?|slash(es)?|trims?) (its )?(price )?target|목표가 하향|투자의견 하향", -0.5, "analyst"),
    (r"investigat|subpoena|lawsuit|sued\b|\bsues\b|\bprobe\b|fraud|indict|sec charges|antitrust|accounting (issue|irregular)"
     r"|소송|조사 착수|분식|횡령", -0.6, "legal"),
    (r"recall|trading halt|halts? trading|suspend(s|ed)? (trading|sales|production)|delist|bankrupt|chapter 11|defaults? on"
     r"|리콜|거래정지|상장폐지|파산", -0.8, "event"),
    (r"short[- ]seller|short report|hindenburg|muddy waters|공매도 보고서", -0.7, "short"),
    (r"\b(ceo|cfo|chief executive|founder)\b.*\b(resigns?|steps down|exit|ousted|fired|departs?)\b"
     r"|\b(resigns?|steps down|ousted)\b|사임|해임", -0.5, "management"),
    (r"plung|tumbl|slump|\bsinks?\b|crash|sell-?off|\bslides?\b|nosedive|급락|폭락", -0.4, "price"),
    (r"tariff|export (ban|curb|restriction|control)|sanction|관세|수출 규제|제재", -0.4, "macro"),
    (r"(beats?|tops?|crush(es)?|exceeds?) (on )?(estimates|expectations|forecasts?|earnings|revenue)|record (revenue|profit|sales|earnings|quarter)"
     r"|strong (results|earnings|guidance|demand)|(raises?|lifts?|boosts?|hikes?) (its )?(full-year |annual )?(guidance|forecast|outlook)"
     r"|어닝 ?서프라이즈|실적 호조|가이던스 상향", 0.7, "earnings"),
    (r"upgrad|(raises?|lifts?|boosts?|hikes?) (its )?(price )?target|목표가 상향|투자의견 상향", 0.5, "analyst"),
    (r"fda approv|wins? approval|(wins?|lands?|secures?|awarded) .{0,40}(contract|order|deal)|partnership|buyback|repurchase"
     r"|stock split|dividend (hike|increase)|to acquire|승인|수주|자사주|액면분할", 0.5, "event"),
    (r"surg|soar|\bjumps?\b|rall(y|ies)|spikes?|record high|all-time high|급등|신고가", 0.3, "price"),
]
RULES = [(re.compile(p, re.I), w, k) for p, w, k in _RULES]

NEWS_SYSTEM = """당신은 주식 자동매매 시스템의 '정보수집' 담당이다. 뉴스 제목 목록을 받아, 각 제목이
'표시된 종목'의 오늘 주가에 미칠 영향을 분류한다.
입력: {"items": [[번호, 종목코드, 제목], ...]}
각 항목마다 impact 를 정한다:
 -2 심각한 악재(대규모 증자, 실적 쇼크·가이던스 급하향, 회계부정·수사, 거래정지, 핵심 경영진 돌연 이탈 등 당일 급락이 날 만한 것)
 -1 악재   0 중립이거나 주가와 무관한 잡음(시황 일반론, 과거 기사 재탕, 광고성 글 포함)   +1 호재   +2 강한 호재
kind: earnings | guidance | analyst | offering | legal | product | management | macro | other
제목만으로 판단이 어려우면 0. 그 종목이 아니라 경쟁사·시장 전체 이야기면 영향이 간접적이므로 0 또는 ±1.
JSON만 출력한다."""

NEWS_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "impact": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["earnings", "guidance", "analyst", "offering", "legal",
                                                        "product", "management", "macro", "other"]},
                },
                "required": ["i", "impact", "kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["items"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class NewsItem:
    id: str
    ts: float               # 발행 시각
    seen: float             # 처음 본 시각
    source: str
    title: str
    url: str
    symbols: tuple
    rule: float = 0.0       # 규칙 점수
    tags: tuple = ()
    impact: int | None = None   # Claude 분류(−2~+2). None = 아직
    kind: str = ""
    fresh: bool = True      # 매매 판단에 쓸 만큼 새 소식인가

    def score(self) -> float:
        """매매에 쓰는 점수(−1~+1): Claude 분류가 있으면 그것을, 없으면 규칙 점수를 쓴다."""
        return self.impact / 2.0 if self.impact is not None else self.rule


def rule_score(title: str) -> tuple[float, tuple]:
    s, tags = 0.0, []
    for rx, w, kind in RULES:
        if rx.search(title):
            s += w
            tags.append(kind)
    return max(-1.0, min(1.0, s)), tuple(tags)


def _norm(title: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", title)         # "... - 매체명" 꼬리 제거(같은 기사 중복 제거용)
    return re.sub(r"[^a-z0-9가-힣]+", " ", t.lower()).strip()


def item_id(title: str) -> str:
    return hashlib.sha1(_norm(title).encode()).hexdigest()[:16]


def _parse_time(s: str | None) -> float:
    if not s:
        return 0.0
    s = s.strip()
    try:
        return parsedate_to_datetime(s).timestamp()        # RSS: RFC 822
    except (TypeError, ValueError):
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))   # Atom: ISO 8601
        return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()
    except ValueError:
        return 0.0


_XML_DECL = re.compile(rb"^\s*<\?xml[^>]*?encoding=[\"']([A-Za-z0-9._-]+)[\"'][^>]*\?>", re.I)
_NATIVE_ENC = {"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "latin1", "utf-16"}


def parse_feed(body: bytes, source: str) -> list[tuple[float, str, str]]:
    """RSS 2.0 / Atom 본문 → [(발행시각, 제목, 링크)]. 형식이 이상하면 빈 목록."""
    if len(body) > MAX_FEED_BYTES:
        return []
    head = body[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:      # 외부 엔티티·엔티티 폭탄 차단
        return []
    try:
        m = _XML_DECL.match(body[:300])
        enc = m.group(1).decode("ascii").lower() if m else "utf-8"
        if enc in _NATIVE_ENC:
            root = ET.fromstring(body)
        else:                                             # EUC-KR 같은 다중 바이트 인코딩은 파서가 못 읽는다 → 직접 풀어서 넘긴다
            root = ET.fromstring(body[m.end():].decode(enc, errors="replace"))
    except Exception:                                     # 깨진 문서·모르는 인코딩 — 소식 한 건 때문에 다른 일이 멈추면 안 된다
        return []
    out = []
    for it in root.iter("item"):                          # RSS
        title = (it.findtext("title") or "").strip()
        if title:
            out.append((_parse_time(it.findtext("pubDate")), title, (it.findtext("link") or "").strip()))
    for it in root.iter(ATOM + "entry"):                  # Atom
        title = (it.findtext(ATOM + "title") or "").strip()
        if title:
            link = it.find(ATOM + "link")
            out.append((_parse_time(it.findtext(ATOM + "updated") or it.findtext(ATOM + "published")),
                        title, link.get("href", "") if link is not None else ""))
    return out


def safe_url(u: str) -> str:
    return u if u.startswith(("https://", "http://")) else ""


class NewsAgent(Agent):
    name = "news"
    role = "정보수집"
    kind = "llm"

    def __init__(self, cfg: Config, llm: LLM, engine, broker, journal=None) -> None:
        super().__init__()
        self.cfg = cfg
        self.nc = cfg.news
        self.llm = llm
        self.engine = engine
        self.broker = broker
        self.journal = journal
        self.symbols: list[str] = list(cfg.universe[:cfg.max_symbols])
        self.items: OrderedDict[str, NewsItem] = OrderedDict()     # 최근 소식(중복 제거 겸용)
        self.session = None
        self.last_llm = 0.0
        self.llm_calls_day = 0
        self.fetch_errors = 0
        self.fetched = 0
        self._stop = False
        self._alias: list[tuple[re.Pattern, str]] = []
        self.set_symbols(self.symbols)

    def new_day(self) -> None:
        self.llm_calls_day = 0

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        pats = []
        for s in self.symbols:
            names = [re.escape(s)] + [re.escape(a) for a in self.nc.aliases.get(s, [])]
            pats.append((re.compile(r"(?<![A-Za-z0-9])(" + "|".join(names) + r")(?![A-Za-z0-9])"), s))
        self._alias = pats

    def find_symbols(self, title: str) -> tuple:
        return tuple(s for rx, s in self._alias if rx.search(title))

    # ---------------------------------------------------------------- 수집
    async def run(self) -> None:
        import aiohttp
        feeds = list(self.nc.feeds)
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12),
                                             headers={"User-Agent": self.nc.user_agent})
        try:
            k = 0
            last_broker = 0.0
            while not self._stop:
                jobs = [(f, s) for s in self.symbols for f in feeds if "{symbol}" in f] + \
                       [(f, "") for f in feeds if "{symbol}" not in f]
                gap = max(1.0, self.nc.poll_sec / max(len(jobs), 1))
                now = time.time()
                try:
                    if jobs:
                        f, sym = jobs[k % len(jobs)]
                        k += 1
                        await self._poll_feed(f, sym, now)
                    if now - last_broker >= 60.0:
                        last_broker = now
                        await self._poll_broker(now)
                    await self.classify(now)
                except asyncio.CancelledError:
                    raise
                except Exception as ex:                   # 소식 수집은 보조 기능 — 어떤 오류도 매매를 멈추게 하지 않는다
                    self.fetch_errors += 1
                    self.note(f"수집 오류: {ex!r}"[:80], now)
                self.status = "warn" if self.fetch_errors > 20 and self.fetched == 0 else "ok"
                await asyncio.sleep(gap)
        finally:
            await self.session.close()

    def stop(self) -> None:
        self._stop = True

    async def _poll_feed(self, feed: str, symbol: str, now: float) -> None:
        url = feed.replace("{symbol}", symbol)
        src = re.sub(r"^https?://(www\.)?([^/]+).*$", r"\2", feed)
        try:
            async with self.session.get(url) as r:
                if r.status != 200:
                    self.fetch_errors += 1
                    return
                body = await r.content.read(MAX_FEED_BYTES + 1)
        except Exception:
            self.fetch_errors += 1
            return
        self.fetched += 1
        self.ingest([(ts, title, link, src, (symbol,) if symbol else ()) for ts, title, link in parse_feed(body, src)], now)

    async def _poll_broker(self, now: float) -> None:
        try:
            rows = await self.broker.get_news(40)
        except Exception:
            return
        old = now - self.nc.max_age_min * 60.0 - 1.0     # '묵은 목록'으로 표시된 것은 화면에만 보이고 판단에는 안 쓴다
        self.ingest([(old if r.get("stale") else float(r.get("ts") or now), str(r.get("title", "")), "",
                      str(r.get("source") or self.broker.name), tuple(r.get("symbols") or ()))
                     for r in rows if r.get("title")], now)

    def ingest(self, rows: list[tuple], now: float) -> int:
        """rows: (발행시각, 제목, 링크, 출처, 종목들). 새 소식 수를 돌려준다."""
        self.calls += 1
        fresh_sec = self.nc.max_age_min * 60.0
        known = set(self.symbols)
        new = 0
        for ts, title, link, src, syms in rows:
            title = title.strip()[:300]
            iid = item_id(title)
            if not title or iid in self.items:
                continue
            ts = ts if 0 < ts <= now + 300 else now
            syms = tuple(dict.fromkeys([s for s in syms if s in known] + list(self.find_symbols(title))))
            if not syms:
                continue                                    # 감시 종목과 무관한 소식은 버린다
            rule, tags = rule_score(title)
            it = NewsItem(iid, ts, now, src, title, safe_url(link), syms, rule, tags, fresh=now - ts <= fresh_sec)
            self.items[iid] = it
            new += 1
            if it.fresh:
                self._apply(it, now, it.rule)
            if self.journal is not None:
                self.journal.news(it)
        while len(self.items) > 3000:
            self.items.popitem(last=False)
        if new:
            self.note(f"새 소식 {new}건 · 최근: {next(reversed(self.items.values())).title[:40]}", now)
        return new

    def _apply(self, it: NewsItem, now: float, delta: float) -> None:
        """소식 점수를 종목 상태에 반영한다(반감기로 감쇠). 악재면 신규 매수를 잠시 막는다."""
        hl = self.nc.half_life_min * 60.0
        for sym in it.symbols:
            ns = self.engine.news.get(sym)
            if ns is None:
                ns = self.engine.news[sym] = NewsState()
            if ns.ts:
                ns.score *= 0.5 ** ((now - ns.ts) / hl)
            ns.score = max(-2.0, min(2.0, ns.score + delta))
            ns.ts = now
            ns.headline = it.title
            if it.score() <= -0.5:
                ns.block_until = max(ns.block_until, now + self.nc.block_min * 60.0)
                self.engine.emit("news", sym, f"악재 감지({it.source}): {it.title[:80]} — {self.nc.block_min:.0f}분간 매수 보류")

    # ---------------------------------------------------------------- Claude 정밀 분류
    async def classify(self, now: float) -> int:
        c = self.cfg.llm
        if (not self.llm.enabled or now - self.last_llm < c.news_min_interval_min * 60.0
                or self.llm_calls_day >= c.news_max_calls):
            return 0
        todo = [it for it in self.items.values() if it.impact is None and it.fresh
                and now - it.ts <= self.nc.max_age_min * 60.0][-20:]
        if not todo:
            return 0
        self.last_llm = now
        self.llm_calls_day += 1
        payload = {"items": [[i, it.symbols[0], it.title[:140]] for i, it in enumerate(todo)]}
        d = await self.llm.ask_json("news", c.model_news, c.effort_news, NEWS_SYSTEM, payload, NEWS_SCHEMA,
                                    max_tokens=4096)
        if d is None:
            for it in todo:                                 # 실패한 묶음을 무한 재시도하지 않는다
                it.impact = round(it.rule * 2) if abs(it.rule) >= 0.5 else 0
            return 0
        n = 0
        for row in d.get("items", []):
            try:
                it = todo[int(row["i"])]
                impact = max(-2, min(2, int(row["impact"])))
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            before = it.score()
            it.impact, it.kind = impact, str(row.get("kind", ""))[:16]
            n += 1
            self._apply(it, now, it.score() - before)        # 규칙 점수와의 차이만큼 보정
            for sym in it.symbols:
                ns = self.engine.news.get(sym)
                if ns is None:
                    continue
                if impact >= 0 and before <= -0.5 and not self._has_negative(sym, now):
                    ns.block_until = 0.0                     # 규칙이 잘못 막은 것을 푼다
                if impact <= -2 and self.nc.exit_on_severe:
                    self.engine.exit_now.add(sym)
                    self.engine.emit("news", sym, f"심각한 악재 확인(Claude): {it.title[:80]} — 보유분 청산")
            if self.journal is not None:
                self.journal.news(it)
        for it in todo:
            if it.impact is None:
                it.impact = 0
        return n

    def _has_negative(self, sym: str, now: float) -> bool:
        lim = self.nc.block_min * 60.0
        return any(sym in it.symbols and it.score() <= -0.5 and now - it.seen <= lim for it in self.items.values())

    # ---------------------------------------------------------------- 화면용
    def recent(self, n: int = 40) -> list[dict]:
        out = []
        for it in heapq.nlargest(n, self.items.values(), key=lambda x: x.ts):       # 최신 소식이 위로
            out.append({"ts": it.ts, "source": it.source, "title": it.title, "url": it.url,
                        "symbols": list(it.symbols), "score": round(it.score(), 2),
                        "by": "claude" if it.impact is not None else "rule", "kind": it.kind or ",".join(it.tags),
                        "fresh": it.fresh})
        return out

    def info(self) -> dict:
        d = super().info()
        d["fetched"] = self.fetched
        d["errors"] = self.fetch_errors
        return d
