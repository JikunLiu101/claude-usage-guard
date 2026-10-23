"""把包装脚本留下的记录转写成插件日志。

包装脚本在 Runner 启动的子进程里运行，它的日志进不了 LangBot；它把锁定和出错写进 events.log、把用量写进 usage.db。
插件在每条消息前后调用 poll()，读出新的记录写进日志，并汇报"开始锁定 / 已解锁 / 接近上限"这些状态变化。
"""

from __future__ import annotations

import json
import pathlib
import time

from meter import policy
from meter.log import get_logger
from meter.store import UsageStore

logger = get_logger(__name__)

NEAR_RATIO = 0.9  # 用到上限的 90% 时提醒一次
ERROR_KINDS = {'limit': '额度用完', 'refused': '安全机制拒答', 'other': '其他错误'}
NEEDS_HUMAN = {'claude-not-found', 'start-failed'}


class Monitor:
    def __init__(self, data_dir: pathlib.Path):
        self.data_dir = pathlib.Path(data_dir)
        self._offset: int | None = None  # events.log 读到哪里；第一次从文件末尾开始，不重放旧记录
        self._last_call_ts: float | None = None
        self._locked: bool | None = None
        self._near_reported: set[tuple[str, object]] = set()

    def poll(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._read_events()
        if not (self.data_dir / 'usage.db').exists():
            return
        store = UsageStore(self.data_dir / 'usage.db')
        try:
            self._read_calls(store, now)
            self._check_state(store, now)
        finally:
            store.close()

    # ---------- events.log ----------

    def _read_events(self) -> None:
        path = self.data_dir / 'events.log'
        try:
            size = path.stat().st_size
        except OSError:
            self._offset = 0
            return
        if self._offset is None or size < self._offset:
            self._offset = size if self._offset is None else 0  # 首次：跳过旧记录；文件被截断：从头读
            return
        if size == self._offset:
            return
        with path.open('rb') as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
        self._offset = size
        for line in chunk.decode('utf-8', errors='replace').splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._log_event(record)

    def _log_event(self, record: dict) -> None:
        bot = record.get('bot', 'default')
        if record.get('event') == 'locked':
            if record.get('during_call'):
                logger.warning('调用中发现账号已超限（%s）：%s。这次回复已改成锁定回复，这次的 token 已经花掉。',
                               bot, record.get('detail', ''))
            else:
                logger.info('锁定中，拦下一次调用（%s）：%s', bot, record.get('detail', ''))
            return
        if record.get('event') == 'error':
            reason = record.get('reason', '')
            if reason in NEEDS_HUMAN:
                logger.error('包装脚本无法启动 claude（%s，%s）：%s。请确认 Runner 所在环境里能找到 claude 命令，'
                             '或用环境变量 CUG_REAL_CLAUDE 指定完整路径。', bot, reason, record.get('detail', ''))
            elif record.get('timed_out'):
                logger.warning('Claude 调用超时，已结束（%s）', bot)
            else:
                logger.warning('Claude 调用出错（%s，%s）：%s', bot, ERROR_KINDS.get(record.get('kind'), record.get('kind')),
                               str(record.get('detail', ''))[:200])

    # ---------- usage.db ----------

    def _read_calls(self, store: UsageStore, now: float) -> None:
        if self._last_call_ts is None:
            self._last_call_ts = now  # 首次：不重放旧记录
            return
        calls = [c for c in store.calls_since(self._last_call_ts) if c.ts > self._last_call_ts]
        for call in calls:
            if call.outcome == 'locked':
                continue
            logger.debug('调用（%s，%s）：%s token（输入 %s，输出 %s，缓存写入 %s，缓存读取 %s），按标价约 $%.4f',
                         call.bot, call.outcome, f'{call.tokens():,}', call.input_tokens, call.output_tokens,
                         call.cache_creation_tokens, call.cache_read_tokens, call.cost_usd)
        if calls:
            self._last_call_ts = calls[-1].ts

    def _check_state(self, store: UsageStore, now: float) -> None:
        cfg = policy.Config.load(self.data_dir / 'config.json')
        lock = policy.check(store, cfg, now)
        if lock is not None and not self._locked:
            until = policy.format_duration(lock.until - now) if lock.until else '一会儿'
            logger.warning('用量闸门已锁定：%s，约 %s后恢复。锁定期间不会调用 Claude。', lock.detail, until)
        elif lock is None and self._locked:
            logger.info('用量闸门已解锁，恢复调用 Claude')
        self._locked = lock is not None
        if lock is not None or not cfg.enabled:
            return
        snap = store.latest_snapshot()
        for which, limit, util, resets in (
            (policy.FIVE_HOUR, cfg.five_hour_limit_pct, getattr(snap, 'five_hour_util', None), getattr(snap, 'five_hour_resets_at', None)),
            (policy.SEVEN_DAY, cfg.seven_day_limit_pct, getattr(snap, 'seven_day_util', None), getattr(snap, 'seven_day_resets_at', None)),
        ):
            if limit <= 0 or util is None or (resets is not None and resets <= now):
                continue
            pct = util * 100
            key = (which, resets)
            if pct >= limit * NEAR_RATIO and key not in self._near_reported:
                self._near_reported.add(key)
                logger.info('%s已用 %.0f%%，接近上限 %.0f%%', policy.REASON_LABELS[which], pct, limit)
        if cfg.bot_window_tokens > 0:
            window = policy.current_window(store.calls_since(now - cfg.bot_window_hours * 7200), now,
                                           cfg.bot_window_hours, count_cache_read=cfg.count_cache_read)
            if window and window.used >= cfg.bot_window_tokens * NEAR_RATIO:
                key = (policy.BOT_WINDOW, window.start)
                if key not in self._near_reported:
                    self._near_reported.add(key)
                    logger.info('机器人窗口已用 %s token，接近上限 %s', f'{window.used:,}', f'{cfg.bot_window_tokens:,}')
