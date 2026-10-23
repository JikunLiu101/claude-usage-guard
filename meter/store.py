"""本地用量记录（SQLite）：每次调用的 token 数，以及 Claude Code 报告的账号额度占用。"""

from __future__ import annotations

import pathlib
import sqlite3
import time
import typing

KEEP_DAYS = 10

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    bot TEXT NOT NULL DEFAULT 'default',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    five_hour_util REAL,
    five_hour_resets_at REAL,
    seven_day_util REAL,
    seven_day_resets_at REAL
);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(ts);
"""


class Call(typing.NamedTuple):
    ts: float
    bot: str
    model: str
    input_tokens: int
    output_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    cost_usd: float
    outcome: str

    def tokens(self, *, count_cache_read: bool = True) -> int:
        total = self.input_tokens + self.output_tokens + self.cache_creation_tokens
        return total + (self.cache_read_tokens if count_cache_read else 0)


class Snapshot(typing.NamedTuple):
    ts: float
    status: str
    five_hour_util: float | None
    five_hour_resets_at: float | None
    seven_day_util: float | None
    seven_day_resets_at: float | None


class UsageStore:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 多个包装脚本进程会同时写：WAL + 等锁
        self.db = sqlite3.connect(str(self.path), timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_call(self, call: Call) -> None:
        self.db.execute(
            'INSERT INTO calls (ts, bot, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,'
            ' cost_usd, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            tuple(call),
        )
        self.db.commit()

    def calls_since(self, since: float, bot: str | None = None) -> list[Call]:
        sql = ('SELECT ts, bot, model, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,'
               ' cost_usd, outcome FROM calls WHERE ts >= ?')
        args: list[typing.Any] = [since]
        if bot is not None:
            sql += ' AND bot = ?'
            args.append(bot)
        return [Call(*row) for row in self.db.execute(sql + ' ORDER BY ts', args)]

    def add_snapshot(self, snap: Snapshot) -> None:
        self.db.execute(
            'INSERT INTO snapshots (ts, status, five_hour_util, five_hour_resets_at, seven_day_util, seven_day_resets_at)'
            ' VALUES (?, ?, ?, ?, ?, ?)',
            tuple(snap),
        )
        self.db.commit()

    def latest_snapshot(self) -> Snapshot | None:
        row = self.db.execute(
            'SELECT ts, status, five_hour_util, five_hour_resets_at, seven_day_util, seven_day_resets_at'
            ' FROM snapshots ORDER BY ts DESC LIMIT 1'
        ).fetchone()
        return Snapshot(*row) if row else None

    def purge(self, now: float | None = None) -> None:
        cutoff = (now if now is not None else time.time()) - KEEP_DAYS * 86400
        self.db.execute('DELETE FROM calls WHERE ts < ?', (cutoff,))
        self.db.execute('DELETE FROM snapshots WHERE ts < ?', (cutoff,))
        self.db.commit()
