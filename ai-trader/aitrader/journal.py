"""매매 일지(SQLite). 쓰기는 전용 스레드가 모아서 처리 — 핫패스는 큐에 넣기만 한다(~1µs)."""
from __future__ import annotations

import json
import queue
import sqlite3
import threading
from pathlib import Path

from .models import Bar, Fill, Trade

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars(symbol TEXT, ts REAL, o REAL, h REAL, l REAL, c REAL, v INTEGER,
  PRIMARY KEY(symbol, ts)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS fills(ts REAL, symbol TEXT, side TEXT, qty INTEGER, price REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS trades(exit_ts REAL, entry_ts REAL, symbol TEXT, qty INTEGER, entry REAL,
  exit REAL, pnl REAL, pnl_pct REAL, r REAL, reason TEXT, bars INTEGER, mae_r REAL, mfe_r REAL,
  pv INTEGER, ctx TEXT, causes TEXT);
CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, equity REAL, pnl REAL, n INTEGER, wins INTEGER,
  pv INTEGER);
CREATE TABLE IF NOT EXISTS lessons(ts REAL, day TEXT, source TEXT, text TEXT, changes TEXT);
CREATE TABLE IF NOT EXISTS llm_usage(ts REAL, role TEXT, model TEXT, input INTEGER, output INTEGER,
  cache_read INTEGER, cache_write INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS events(ts REAL, kind TEXT, text TEXT);
CREATE INDEX IF NOT EXISTS bars_ts ON bars(ts);
"""

_SQL = {
    "bars": "INSERT OR REPLACE INTO bars VALUES(?,?,?,?,?,?,?)",
    "fills": "INSERT INTO fills VALUES(?,?,?,?,?,?)",
    "trades": "INSERT INTO trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    "daily": "INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?)",
    "lessons": "INSERT INTO lessons VALUES(?,?,?,?,?)",
    "llm_usage": "INSERT INTO llm_usage VALUES(?,?,?,?,?,?,?,?)",
    "events": "INSERT INTO events VALUES(?,?,?)",
}


class Journal:
    def __init__(self, path: str | Path | None) -> None:
        self.path = str(path) if path else None
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        if self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(self.path)
            con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;" + SCHEMA)
            con.close()
            self.thread = threading.Thread(target=self._run, name="journal", daemon=True)
            self.thread.start()

    # ------------------------------------------------------------ 쓰기(논블로킹)
    def _put(self, table: str, row: tuple) -> None:
        if self.path:
            self.q.put((table, row))

    def bar(self, b: Bar) -> None:
        self._put("bars", (b.symbol, b.ts, b.open, b.high, b.low, b.close, b.volume))

    def fill(self, f: Fill) -> None:
        self._put("fills", (f.ts, f.symbol, f.side, f.qty, f.price, f.reason))

    def trade(self, t: Trade) -> None:
        self._put("trades", (t.exit_ts, t.entry_ts, t.symbol, t.qty, t.entry_price, t.exit_price,
                             t.pnl, t.pnl_pct, t.r_multiple, t.exit_reason, t.bars_held, t.mae_r,
                             t.mfe_r, t.params_version, json.dumps(t.ctx, ensure_ascii=False),
                             json.dumps(t.causes, ensure_ascii=False)))

    def daily(self, day: str, equity: float, pnl: float, n: int, wins: int, pv: int) -> None:
        self._put("daily", (day, equity, pnl, n, wins, pv))

    def lesson(self, ts: float, day: str, source: str, text: str, changes: dict) -> None:
        self._put("lessons", (ts, day, source, text, json.dumps(changes, ensure_ascii=False)))

    def llm_usage(self, ts: float, role: str, model: str, inp: int, out: int, cr: int, cw: int,
                  cost: float) -> None:
        self._put("llm_usage", (ts, role, model, inp, out, cr, cw, cost))

    def event(self, ts: float, kind: str, text: str) -> None:
        self._put("events", (ts, kind, text))

    def _run(self) -> None:
        con = sqlite3.connect(self.path)
        while True:
            item = self.q.get()
            batch = [item]
            try:
                while len(batch) < 500:
                    batch.append(self.q.get_nowait())
            except queue.Empty:
                pass
            stop = False
            for it in batch:
                if it is None:
                    stop = True
                elif isinstance(it, threading.Event):
                    con.commit()
                    it.set()
                else:
                    try:
                        con.execute(_SQL[it[0]], it[1])
                    except sqlite3.Error:
                        pass                    # 일지 오류가 매매를 멈추게 하지 않는다
            con.commit()
            if stop:
                con.close()
                return

    def flush(self, timeout: float = 5.0) -> None:
        if self.thread:
            ev = threading.Event()
            self.q.put(ev)
            ev.wait(timeout)

    def close(self) -> None:
        if self.thread:
            self.q.put(None)
            self.thread.join(timeout=5.0)
            self.thread = None

    # ------------------------------------------------------------ 읽기(느린 경로에서만)
    def _read(self, sql: str, args: tuple = ()) -> list[tuple]:
        if not self.path:
            return []
        con = sqlite3.connect(self.path)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def load_bars(self, since_ts: float) -> list[Bar]:
        return [Bar(*r) for r in self._read(
            "SELECT symbol, ts, o, h, l, c, v FROM bars WHERE ts >= ? ORDER BY ts, symbol", (since_ts,))]

    def recent_bars(self, symbol: str, n: int) -> list[Bar]:
        rows = self._read("SELECT symbol, ts, o, h, l, c, v FROM bars WHERE symbol = ? "
                          "ORDER BY ts DESC LIMIT ?", (symbol, n))
        return [Bar(*r) for r in reversed(rows)]

    def load_trades(self, since_ts: float = 0.0) -> list[Trade]:
        out = []
        for r in self._read("SELECT symbol, entry_ts, exit_ts, qty, entry, exit, pnl, pnl_pct, r, "
                            "reason, bars, mae_r, mfe_r, pv, ctx, causes FROM trades "
                            "WHERE exit_ts >= ? ORDER BY exit_ts", (since_ts,)):
            out.append(Trade(*r[:14], json.loads(r[14] or "{}"), json.loads(r[15] or "[]")))
        return out

    def load_daily(self, n: int = 60) -> list[dict]:
        rows = self._read("SELECT day, equity, pnl, n, wins, pv FROM daily ORDER BY day DESC LIMIT ?", (n,))
        return [dict(zip(("day", "equity", "pnl", "n", "wins", "pv"), r)) for r in reversed(rows)]

    def load_lessons(self, n: int = 20) -> list[dict]:
        rows = self._read("SELECT ts, day, source, text, changes FROM lessons ORDER BY ts DESC LIMIT ?", (n,))
        return [{"ts": r[0], "day": r[1], "source": r[2], "text": r[3], "changes": json.loads(r[4] or "{}")}
                for r in rows]
