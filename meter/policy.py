"""三道闸门：账号 5 小时占用、账号 7 天占用、机器人自身在窗口内的 token 用量。任一超限即锁定。

机器人窗口和 Claude 的 5 小时窗口一样：从窗口结束后的第一次调用开始计时，满 N 小时清零。
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import time
import typing

from meter.store import Call, Snapshot, UsageStore

FIVE_HOUR = 'five_hour'
SEVEN_DAY = 'seven_day'
BOT_WINDOW = 'bot_window'

REASON_LABELS = {
    FIVE_HOUR: '账号 5 小时额度',
    SEVEN_DAY: '账号 7 天额度',
    BOT_WINDOW: '机器人窗口额度',
}

DEFAULT_LOCK_OUTPUT = '（额度暂时用完了，{resets_in}后恢复）'


@dataclasses.dataclass(frozen=True)
class Config:
    enabled: bool = True
    five_hour_limit_pct: float = 80.0   # 0 = 不限
    seven_day_limit_pct: float = 90.0   # 0 = 不限
    bot_window_hours: float = 5.0
    bot_window_tokens: int = 1_000_000  # 0 = 不限
    count_cache_read: bool = True
    lock_output: str = DEFAULT_LOCK_OUTPUT
    error_output: str = ''              # 空 = 出错时原样交给 LangBot

    @classmethod
    def from_dict(cls, data: dict[str, typing.Any]) -> Config:
        def num(key: str, default: float) -> float:
            try:
                value = float(data.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value >= 0 else default

        def flag(key: str, default: bool) -> bool:
            value = data.get(key, default)
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

        return cls(
            enabled=flag('enabled', True),
            five_hour_limit_pct=num('five-hour-limit-percent', cls.five_hour_limit_pct),
            seven_day_limit_pct=num('seven-day-limit-percent', cls.seven_day_limit_pct),
            bot_window_hours=max(0.1, num('bot-window-hours', cls.bot_window_hours)),
            bot_window_tokens=int(num('bot-window-tokens', cls.bot_window_tokens)),
            count_cache_read=flag('count-cache-read', True),
            lock_output=str(data.get('lock-output') or DEFAULT_LOCK_OUTPUT),
            error_output=str(data.get('error-output') or ''),
        )

    @classmethod
    def load(cls, path: pathlib.Path) -> Config:
        try:
            return cls.from_dict(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            return cls()


class Window(typing.NamedTuple):
    start: float
    end: float
    used: int
    calls: int


class Lock(typing.NamedTuple):
    reason: str
    until: float | None
    detail: str


def current_window(calls: typing.Iterable[Call], now: float, hours: float, *, count_cache_read: bool = True) -> Window | None:
    """当前机器人窗口；已经过期（或从没调用过）返回 None，表示下一次调用会开一个新窗口。"""
    length = hours * 3600
    start: float | None = None
    used = count = 0
    for call in sorted(calls, key=lambda c: c.ts):
        if call.outcome == 'locked':
            continue  # 被锁的调用没用 token，也不开新窗口
        if start is None or call.ts >= start + length:
            start, used, count = call.ts, 0, 0
        used += call.tokens(count_cache_read=count_cache_read)
        count += 1
    if start is None or now >= start + length:
        return None
    return Window(start, start + length, used, count)


def _account_util(snap: Snapshot | None, now: float, which: str) -> tuple[float, float | None]:
    """返回 (占用百分比, 重置时间)；快照里的窗口已经重置过的按 0 算。"""
    if snap is None:
        return 0.0, None
    util = snap.five_hour_util if which == FIVE_HOUR else snap.seven_day_util
    resets = snap.five_hour_resets_at if which == FIVE_HOUR else snap.seven_day_resets_at
    if util is None or (resets is not None and resets <= now):
        return 0.0, None
    return util * 100, resets


def account_limits_enabled(cfg: Config) -> bool:
    return cfg.enabled and (cfg.five_hour_limit_pct > 0 or cfg.seven_day_limit_pct > 0)


def account_lock(snap: Snapshot | None, cfg: Config, now: float) -> Lock | None:
    """只看账号 5 小时 / 7 天占用。"""
    if not cfg.enabled:
        return None
    for which, limit in ((FIVE_HOUR, cfg.five_hour_limit_pct), (SEVEN_DAY, cfg.seven_day_limit_pct)):
        pct, resets = _account_util(snap, now, which)
        if limit > 0 and pct >= limit:
            return Lock(which, resets, f'{REASON_LABELS[which]}已用 {pct:.0f}%（上限 {limit:.0f}%）')
    return None


def check(store: UsageStore, cfg: Config, now: float | None = None) -> Lock | None:
    now = now if now is not None else time.time()
    if not cfg.enabled:
        return None
    lock = account_lock(store.latest_snapshot(), cfg, now)
    if lock is not None:
        return lock
    if cfg.bot_window_tokens > 0:
        since = now - cfg.bot_window_hours * 3600 * 2  # 往前看两个窗口长度就够判断当前窗口从哪开始
        window = current_window(store.calls_since(since), now, cfg.bot_window_hours,
                                count_cache_read=cfg.count_cache_read)
        if window and window.used >= cfg.bot_window_tokens:
            return Lock(BOT_WINDOW, window.end,
                        f'{REASON_LABELS[BOT_WINDOW]}已用 {window.used:,} token（上限 {cfg.bot_window_tokens:,}）')
    return None


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return '一会儿'
    minutes = max(1, int(seconds // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f'{days}天{hours}小时'
    if hours:
        return f'{hours}小时{minutes}分钟'
    return f'{minutes}分钟'


def render(template: str, *, kind: str, lock: Lock | None = None, now: float | None = None) -> str:
    """输出模板：{kind} 错误类型 / {reason} 锁定原因 / {resets_in} 多久后恢复。"""
    now = now if now is not None else time.time()
    resets_in = format_duration(lock.until - now) if lock and lock.until else '一会儿'
    reason = REASON_LABELS.get(lock.reason, '') if lock else ''
    return (template.replace('{kind}', kind).replace('{reason}', reason).replace('{resets_in}', resets_in))
