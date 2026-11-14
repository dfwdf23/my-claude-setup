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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from ..config import Config
from ..llm import LLM
from ..models import BUY, NewsState
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
    impact: int | None = None   # Claude 판정 요약(가장 강한 것, −2~+2). None = 아직. 일지 기록·화면용
    kind: str = ""
    fresh: bool = True      # 처음 봤을 때 매매 판단에 쓸 만큼 새 소식이었는가(규칙 점수를 반영했는가)
    stale: bool = False     # 발행 시각을 믿을 수 없는 묵은 목록(증권사가 시작 직후 준 것) — 화면에만 보이고 판정하지 않는다
    verdicts: dict = field(default_factory=dict)   # 종목 → Claude 판정(−2~+2). 같은 제목이라도 종목마다 뜻이 다르다
    tries: dict = field(default_factory=dict)      # 종목 → Claude 가 응답했는데 이 쌍의 답이 빠진 횟수
    fails: dict = field(default_factory=dict)      # 종목 → 응답 자체가 없었던(장애·예산 소진) 횟수 — 날이 바뀌면 다시 기회를 준다

    def score(self, sym: str | None = None) -> float:
        """매매에 쓰는 점수(−1~+1). 그 종목에 대한 Claude 판정이 있으면 그것을, 없으면 규칙 점수를 쓴다.
        sym 을 안 주면 화면용 요약(가장 강한 판정)."""
        if sym is not None:
            v = self.verdicts.get(sym)
            return v / 2.0 if v is not None else self.rule
        return self.impact / 2.0 if self.impact is not None else self.rule

    def judge(self, sym: str, impact: int, kind: str) -> None:
        self.verdicts[sym] = impact
        worst = min(self.verdicts.values(), key=lambda v: (-abs(v), v))      # 가장 강한 판정(같으면 악재 쪽)
        self.impact = worst
        if impact == worst:
            self.kind = kind


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


class _NoDTD(ET.TreeBuilder):
    """DTD(DOCTYPE)가 있는 문서를 파서 단계에서 거부한다 — 엔티티 폭탄·외부 엔티티 차단.
    앞부분 글자를 훑는 방식은 UTF-16 이나 긴 머리말로 피할 수 있어서 쓰지 않는다."""

    def doctype(self, name, pubid, system):
        raise ValueError("DTD not allowed")


def _xml(data):
    return ET.fromstring(data, parser=ET.XMLParser(target=_NoDTD()))


def parse_feed(body: bytes, source: str) -> list[tuple[float, str, str]]:
    """RSS 2.0 / Atom 본문 → [(발행시각, 제목, 링크)]. 형식이 이상하면 빈 목록."""
    return _parse(body) or []


def _parse(body: bytes) -> list[tuple[float, str, str]] | None:
    """parse_feed 와 같지만, 읽지 못한 문서(None)와 소식이 없는 정상 문서([])를 구분한다."""
    if len(body) > MAX_FEED_BYTES:
        return None
    try:
        m = _XML_DECL.match(body[:300])
        enc = m.group(1).decode("ascii").lower() if m else "utf-8"
        if enc in _NATIVE_ENC:
            root = _xml(body)
        else:                                             # EUC-KR 같은 다중 바이트 인코딩은 파서가 못 읽는다 → 직접 풀어서 넘긴다
            root = _xml(body[m.end():].decode(enc, errors="replace"))
    except Exception:                                     # 깨진 문서·모르는 인코딩·DTD — 소식 한 건 때문에 다른 일이 멈추면 안 된다
        return None
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
        self.started = time.time()
        self.last_llm = 0.0
        self.llm_calls_day = 0
        self.fetch_errors = 0
        self.parse_errors = 0
        self.fetched = 0
        self.last_ok = 0.0            # 마지막으로 피드를 정상 해석한 시각
        self.llm_off = ""             # Claude 분류를 못 쓰는 이유(화면 표시용). 빈 문자열 = 정상
        self._prior: dict | None = None   # 이전 실행에서 이미 받은 Claude 판정(일지에서 한 번 읽는다): id → ({종목: 판정}, 분류)
        self.exits_day = 0            # 오늘 소식으로 건 청산 지시 수
        self._stop = False
        self._alias: list[tuple[re.Pattern, str]] = []
        self.set_symbols(self.symbols)

    def new_day(self) -> None:
        self.llm_calls_day = 0
        self.exits_day = 0
        if self.llm_off.startswith("오늘"):
            self.llm_off = ""
        for it in self.items.values():                # 어제 응답을 못 받은 소식에 다시 기회를 준다(예산·횟수가 새로 시작된다)
            it.fails.clear()

    def set_symbols(self, symbols: list[str]) -> None:
        self.symbols = list(symbols)
        pats = []
        for s in self.symbols:
            # 종목 코드는 대소문자를 구분하고(META·SPY 같은 코드는 소문자면 보통 낱말이다), 회사 이름은 구분하지 않는다
            # (같은 회사를 'Nvidia'·'NVIDIA' 로 섞어 쓴다).
            alts = [re.escape(s)] + ["(?i:" + re.escape(a) + ")" for a in self.nc.aliases.get(s, [])]
            pats.append((re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(alts) + r")(?![A-Za-z0-9])"), s))
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
                # 피드가 있는데 15분 넘게 한 건도 제대로 읽지 못했거나, Claude 분류가 꺼져 있으면 '주의'
                dead = bool(jobs) and self.fetch_errors > 5 and now - max(self.last_ok, self.started) > 900.0
                self.status = "warn" if dead or self.llm_off else "ok"
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
                buf = bytearray()                         # read(n) 한 번은 '지금 도착한 만큼'만 준다 → 끝까지 읽는다
                async for chunk in r.content.iter_chunked(65536):
                    buf += chunk
                    if len(buf) > MAX_FEED_BYTES:
                        break
        except Exception:
            self.fetch_errors += 1
            return
        rows = _parse(bytes(buf))
        if rows is None:                                  # 읽었지만 해석하지 못했다 — '소식 없음'과 구분해 센다
            self.fetch_errors += 1
            self.parse_errors += 1
            return
        self.fetched += 1
        self.last_ok = now
        self.ingest([(ts, title, link, src, (symbol,) if symbol else ()) for ts, title, link in rows], now)

    async def _poll_broker(self, now: float) -> None:
        try:
            rows = await self.broker.get_news(40)
        except Exception:
            return
        # '묵은 목록'으로 표시된 것(시작 직후 받은 것 — 언제 나온 소식인지 믿을 수 없다)은 화면에만 보이고 판단에는 안 쓴다
        self.ingest([(float(r.get("ts") or now), str(r.get("title", "")), "",
                      str(r.get("source") or self.broker.name), tuple(r.get("symbols") or ()), bool(r.get("stale")))
                     for r in rows if r.get("title")], now)

    def _prior_verdicts(self, now: float) -> dict:
        if self._prior is None:
            self._prior = {}
            if self.journal is not None:
                try:
                    self._prior = self.journal.news_verdicts(now - self.HELD_MAX_AGE)
                except Exception:                           # 일지를 못 읽어도 수집은 계속(다시 물으면 된다)
                    self._prior = {}
        return self._prior

    def ingest(self, rows: list[tuple], now: float) -> int:
        """rows: (발행시각, 제목, 링크, 출처, 종목들[, 묵은 목록 여부]). 새 소식 수를 돌려준다."""
        self.calls += 1
        fresh_sec = self.nc.max_age_min * 60.0
        known = set(self.symbols)
        prior = self._prior_verdicts(now)
        new = 0
        for row in rows:
            ts, title, link, src, syms = row[:5]
            stale = len(row) > 5 and bool(row[5])
            title = title.strip()[:300]
            iid = item_id(title)
            if not title:
                continue
            old = self.items.get(iid)
            if old is not None:
                # 같은 제목이 다른 종목의 피드에서 또 왔다 → 그 종목도 이 소식의 대상에 넣는다(안 그러면 영영 판정받지 못한다)
                add = tuple(s for s in syms if s in known and s not in old.symbols)
                if add:
                    old.symbols += add
                    if old.fresh:
                        self._apply(old, now, old.rule, add)
                continue
            ts = ts if 0 < ts <= now + 300 else now
            syms = tuple(dict.fromkeys([s for s in syms if s in known] + list(self.find_symbols(title))))
            if not syms:
                continue                                    # 감시 종목과 무관한 소식은 버린다
            rule, tags = rule_score(title)
            it = NewsItem(iid, ts, now, src, title, safe_url(link), syms, rule, tags,
                          fresh=not stale and now - ts <= fresh_sec, stale=stale)
            pv = prior.get(iid)
            if pv is not None and not stale:                # 이전 실행에서 받은 판정을 이어 쓴다(같은 소식을 다시 묻지 않는다)
                for sym, impact in pv[0].items():
                    if sym in syms:
                        it.judge(sym, impact, pv[1][:16])
            self.items[iid] = it
            new += 1
            if it.fresh:
                for sym in it.symbols:
                    self._apply(it, now, it.score(sym), (sym,))
            if self.journal is not None:
                self.journal.news(it)
        while len(self.items) > 3000:
            self.items.popitem(last=False)
        if new:
            self.note(f"새 소식 {new}건 · 최근: {next(reversed(self.items.values())).title[:40]}", now)
        return new

    def _apply(self, it: NewsItem, now: float, delta: float, syms: tuple | None = None) -> None:
        """소식 점수를 종목 상태에 반영한다(반감기로 감쇠). 악재면 신규 매수를 잠시 막는다. syms: 반영할 종목(기본: 전부)."""
        hl = self.nc.half_life_min * 60.0
        for sym in (it.symbols if syms is None else syms):
            ns = self.engine.news.get(sym)
            if ns is None:
                ns = self.engine.news[sym] = NewsState()
            if ns.ts:
                ns.score *= 0.5 ** ((now - ns.ts) / hl)
            ns.score = max(-2.0, min(2.0, ns.score + delta))
            ns.ts = now
            ns.headline = it.title
            if it.score(sym) <= -0.5:
                ns.block_until = max(ns.block_until, now + self.nc.block_min * 60.0)
                self.engine.emit("news", sym, f"악재 감지({it.source}): {it.title[:80]} — {self.nc.block_min:.0f}분간 매수 보류")

    # ---------------------------------------------------------------- Claude 정밀 분류
    HELD_MAX_AGE = 4 * 86400.0     # 보유 종목의 소식은 이만큼 지나도 판정한다(금요일 장 마감 뒤 소식을 연휴 뒤 아침에라도 본다)
    LATE_PER_SYM = 8               # 처음 봤을 때 이미 묵어 있던 소식(꺼져 있던 사이에 나온 것)은 보유 종목마다 최신 몇 건만 묻는다
    BATCH = 24
    MAX_TRIES = 2                  # 모델이 답을 빼먹은 쌍을 다시 묻는 횟수
    MAX_FAILS = 6                  # 응답 자체가 없었던(장애·예산 소진) 시도는 따로 센다 — 판정 기회를 태우지 않게

    def _pairs(self, now: float) -> list[tuple]:
        """아직 Claude 판정을 받지 않은 (소식, 종목) 쌍. 보유 종목 것을 앞에, 그다음은 최신순.

        보통은 처음 봤을 때 새 소식이던 것만 묻는다. 보유 종목은 예외다 — 프로그램이 꺼져 있던 사이(밤사이·주말)에
        나와 처음 봤을 때 이미 묵어 있던 소식도 HELD_MAX_AGE 안이면 묻는다. 그러지 않으면 켤 때마다 밤사이 악재를
        통째로 버리게 된다. 다만 토큰을 아끼려고 종목마다 최신 LATE_PER_SYM 건으로 제한한다."""
        held = self.engine.positions
        fresh_sec = self.nc.max_age_min * 60.0
        cand = []
        late: dict[str, list] = {}
        for it in self.items.values():
            if it.stale:
                continue
            age = now - it.ts
            for sym in it.symbols:
                if it.fresh:
                    if age <= fresh_sec or (sym in held and age <= self.HELD_MAX_AGE):
                        cand.append((it, sym))
                elif sym in held and age <= self.HELD_MAX_AGE:
                    late.setdefault(sym, []).append(it)
        for sym, lst in late.items():
            lst.sort(key=lambda x: -x.ts)
            cand.extend((it, sym) for it in lst[:self.LATE_PER_SYM])
        out = [(it, sym) for it, sym in cand
               if not (sym in it.verdicts or it.tries.get(sym, 0) >= self.MAX_TRIES
                       or it.fails.get(sym, 0) >= self.MAX_FAILS)]
        out.sort(key=lambda p: (p[1] not in held, -p[0].ts))
        return out[:self.BATCH]

    def _llm_unavailable(self, why: str, now: float) -> None:
        if self.llm_off != why:
            self.llm_off = why
            self.engine.emit("news", "", f"Claude 소식 분류 중단: {why} — 규칙 판정(매수 보류)만 동작하고 악재 청산 지시는 나가지 않습니다")
            self.note(f"Claude 분류 중단: {why}"[:80], now)

    async def classify(self, now: float) -> int:
        """새 소식을 '제목×종목' 단위로 묶어 Claude 에게 한 번에 묻는다. 판정은 물어본 그 종목에만 적용한다.

        한 제목에 여러 종목이 걸릴 수 있다("A사 회계부정 … B·C사 반사이익"). 종목 하나에 대한 판정을
        제목에 언급된 모든 종목에 적용하면 무관한 보유 종목까지 강제 매도된다."""
        c = self.cfg.llm
        if not self.llm.enabled or now - self.last_llm < c.news_min_interval_min * 60.0:
            return 0
        pairs = self._pairs(now)
        if not pairs:
            return 0
        held = self.engine.positions
        has_held = any(sym in held for _, sym in pairs)
        reserve = max(5, c.news_max_calls // 3)          # 하루 횟수의 일부는 보유 종목 소식을 위해 남겨 둔다
        if self.llm_calls_day >= c.news_max_calls:
            self._llm_unavailable("오늘의 분류 횟수를 다 썼습니다", now)
            return 0
        if self.llm_calls_day >= c.news_max_calls - reserve and not has_held:
            return 0                                     # 잡다한 소식으로 남은 횟수를 태우지 않는다
        self.last_llm = now
        self.llm_calls_day += 1
        payload = {"items": [[i, sym, it.title[:140]] for i, (it, sym) in enumerate(pairs)]}
        d = await self.llm.ask_json("news", c.model_news, c.effort_news, NEWS_SYSTEM, payload, NEWS_SCHEMA,
                                    max_tokens=4096)
        if d is None:
            # 응답 없음(예산 소진·장애): 판정을 지어내지 않는다(규칙 판정 그대로). 이 시도는 '물어본 횟수'에 넣지 않는다 —
            # 저녁에 잠깐 안 됐다고 다음 날 아침의 판정 기회까지 사라지면 안 된다. 다만 끝없이 반복하지는 않는다.
            self.llm_calls_day -= 1
            for it, sym in pairs:
                it.fails[sym] = it.fails.get(sym, 0) + 1
            self._llm_unavailable("Claude 응답 없음(예산 소진·장애)", now)
            return 0
        for it, sym in pairs:
            it.tries[sym] = it.tries.get(sym, 0) + 1
        if self.llm_off and not self.llm_off.startswith("오늘"):
            self.llm_off = ""
        n = 0
        seen: set[int] = set()
        for row in d.get("items", []):
            try:
                i = int(row["i"])
                impact = max(-2, min(2, int(row["impact"])))
            except (KeyError, TypeError, ValueError):
                continue
            if not 0 <= i < len(pairs) or i in seen:     # 범위를 벗어난 번호·중복 번호는 버린다(음수 번호가 뒤에서부터 세어지지 않게)
                continue
            seen.add(i)
            it, sym = pairs[i]
            before = it.score(sym)
            it.judge(sym, impact, str(row.get("kind", ""))[:16])
            n += 1
            # 규칙 점수와의 차이만큼, 그 종목에만. 처음 봤을 때 이미 묵어 있던 소식은 규칙 점수를 반영한 적이 없으므로 판정 전체를 반영한다.
            self._apply(it, now, it.score(sym) - (before if it.fresh else 0.0), (sym,))
            ns = self.engine.news.get(sym)
            if ns is not None and impact >= 0 and before <= -0.5 and not self._has_negative(sym, now):
                ns.block_until = 0.0                     # 규칙이 잘못 막은 것을 푼다
            pend = self.engine.pending.get(sym)
            if (impact <= -2 and self.nc.exit_on_severe and sym not in self.engine.exit_now
                    and (sym in held or (pend is not None and pend.side == BUY))):
                # 보유 중이거나 매수 주문이 나가 있는 종목만 청산 지시(체결되면 곧바로 정리). 그 밖의 종목은 매수 보류로 충분하다.
                # 제목은 바깥에서 온 글이므로 (1) 하루 지시 수에 상한을 두고 (2) 실제 청산은 가격이 내려 있을 때만 한다(엔진).
                if self.exits_day >= self.nc.max_exits_per_day:
                    self.engine.emit("news", sym, f"심각한 악재 확인(Claude): {it.title[:80]} — 오늘의 자동 청산 지시 한도"
                                     f"({self.nc.max_exits_per_day}건)를 넘어 청산하지 않습니다. 직접 확인하세요", alert=1)
                else:
                    self.exits_day += 1
                    self.engine.exit_now.add(sym)
                    self.engine.exit_day[sym] = self.engine.m.local_day(now)
                    drop = self.nc.exit_min_drop_pct
                    cond = f"가격이 전일 종가보다 {drop:g}% 이상 내려 있으면 " if drop > 0 else ""
                    self.engine.emit("news", sym, f"심각한 악재 확인(Claude): {it.title[:80]} — {cond}보유분 청산", alert=1)
            if self.journal is not None:
                self.journal.news(it)
        return n

    def _has_negative(self, sym: str, now: float) -> bool:
        lim = self.nc.block_min * 60.0
        return any(sym in it.symbols and it.score(sym) <= -0.5 and now - it.seen <= lim for it in self.items.values())

    # ---------------------------------------------------------------- 화면용
    def recent(self, n: int = 40) -> list[dict]:
        out = []
        for it in heapq.nlargest(n, self.items.values(), key=lambda x: x.ts):       # 최신 소식이 위로
            out.append({"ts": it.ts, "source": it.source, "title": it.title, "url": it.url,
                        "symbols": list(it.symbols), "score": round(it.score(), 2),
                        "by": "claude" if it.verdicts else "rule", "kind": it.kind or ",".join(it.tags),
                        "fresh": it.fresh})
        return out

    def info(self) -> dict:
        d = super().info()
        d["fetched"] = self.fetched
        d["errors"] = self.fetch_errors
        d["llm_off"] = self.llm_off
        return d
