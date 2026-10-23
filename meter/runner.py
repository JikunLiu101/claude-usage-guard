"""替 LangBot 的 Claude Code Runner 运行 claude：调用前过闸门，调用中记录用量。

- 锁定时不调用 claude，而是输出一条正常的 result（内容是 lock-output 模板），LangBot 当成普通回复处理。
- 正常时原样转发 claude 的输出；从 stream-json 里记下 rate_limit_event（账号 5 小时 / 7 天占用）和 result 的 token 用量。
- 出错时：error-output 为空就原样转发、保留退回码（交给 LangBot 自己处理）；不为空就输出模板（{kind} = limit / refused / other）。

每个流水线可以用环境变量覆盖（Runner 的 env-json）：
  CUG_BOT           记账用的机器人名，默认 default
  CUG_LOCK_OUTPUT   覆盖 lock-output
  CUG_ERROR_OUTPUT  覆盖 error-output
  CUG_REAL_CLAUDE   真正的 claude 命令，默认在 PATH 里找
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import typing

from meter import policy
from meter.store import Call, Snapshot, UsageStore

TIMEOUT = float(os.environ.get('CUG_TIMEOUT', '270'))  # 比 Runner 默认的 300 秒早一点，保证来得及输出

LIMIT_CODES = {'rate_limit', 'billing_error', 'quota_exceeded', 'usage_limit'}
LIMIT_TEXT = re.compile(
    r'usage limit|hit your limit|limit reached|limit will reset|resets? (at|in)|rate[ _-]?limit|quota|credit balance|out of (extra )?usage',
    re.IGNORECASE,
)
REFUSED_TEXT = re.compile(r'usage policy|unable to respond to this request|violat|refus', re.IGNORECASE)


def classify(*, status: object, codes: set[str], text: str, safety_stops: int) -> str:
    if str(status) == '429' or codes & LIMIT_CODES or LIMIT_TEXT.search(text):
        return 'limit'
    if safety_stops > 0 or 'refusal' in codes or REFUSED_TEXT.search(text):
        return 'refused'
    return 'other'


def snapshot_from_event(event: dict[str, typing.Any], now: float) -> Snapshot | None:
    info = event.get('rate_limit_info')
    if not isinstance(info, dict):
        return None
    windows = info.get('unifiedWindows') if isinstance(info.get('unifiedWindows'), dict) else {}

    def window(name: str) -> tuple[float | None, float | None]:
        w = windows.get(name)
        if isinstance(w, dict) and w.get('utilization') is not None:
            return float(w['utilization']), _float(w.get('resetsAt'))
        if info.get('rateLimitType') == name and info.get('utilization') is not None:
            return float(info['utilization']), _float(info.get('resetsAt'))
        return None, None

    five, five_reset = window('five_hour')
    seven, seven_reset = window('seven_day')
    if five is None and seven is None:
        return None
    return Snapshot(now, str(info.get('status') or ''), five, five_reset, seven, seven_reset)


def _float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _usage_call(event: dict[str, typing.Any], bot: str, now: float, outcome: str) -> Call:
    usage = event.get('usage') if isinstance(event.get('usage'), dict) else {}
    models = event.get('modelUsage') if isinstance(event.get('modelUsage'), dict) else {}
    return Call(
        ts=now,
        bot=bot,
        model=','.join(models.keys()),
        input_tokens=int(usage.get('input_tokens') or 0),
        output_tokens=int(usage.get('output_tokens') or 0),
        cache_creation_tokens=int(usage.get('cache_creation_input_tokens') or 0),
        cache_read_tokens=int(usage.get('cache_read_input_tokens') or 0),
        cost_usd=float(event.get('total_cost_usd') or 0.0),
        outcome=outcome,
    )


def emit_result(out: typing.BinaryIO, text: str) -> None:
    line = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text}
    out.write((json.dumps(line, ensure_ascii=False) + '\n').encode('utf-8'))
    out.flush()


def log_event(data_dir: pathlib.Path, record: dict[str, typing.Any]) -> None:
    """events.log：锁定和出错各记一行（不含聊天内容）。"""
    try:
        with (data_dir / 'events.log').open('a', encoding='utf-8') as f:
            f.write(json.dumps({'time': time.strftime('%Y-%m-%d %H:%M:%S'), **record}, ensure_ascii=False) + '\n')
    except OSError:
        pass


def run(argv: list[str], data_dir: pathlib.Path, *, stdin: typing.BinaryIO, stdout: typing.BinaryIO) -> int:
    cfg = policy.Config.load(data_dir / 'config.json')
    bot = os.environ.get('CUG_BOT') or 'default'
    lock_output = os.environ.get('CUG_LOCK_OUTPUT') or cfg.lock_output
    error_output = os.environ.get('CUG_ERROR_OUTPUT', cfg.error_output)
    store = UsageStore(data_dir / 'usage.db')
    try:
        now = time.time()
        lock = policy.check(store, cfg, now)
        if lock is not None:
            store.add_call(Call(now, bot, '', 0, 0, 0, 0, 0.0, 'locked'))
            log_event(data_dir, {'event': 'locked', 'bot': bot, 'reason': lock.reason, 'detail': lock.detail,
                                 'until': lock.until})
            emit_result(stdout, policy.render(lock_output, kind='limit', lock=lock, now=now))
            return 0
        return _run_claude(argv, data_dir, store, bot, error_output, stdin=stdin, stdout=stdout,
                           cfg=cfg, lock_output=lock_output)
    finally:
        try:
            store.purge()
        finally:
            store.close()


def _run_claude(argv, data_dir, store, bot, error_output, *, stdin, stdout,
                cfg: policy.Config | None = None, lock_output: str = policy.DEFAULT_LOCK_OUTPUT) -> int:
    cfg = cfg or policy.Config()
    real = os.environ.get('CUG_REAL_CLAUDE') or shutil.which('claude')
    stdin_data = stdin.read()
    if not real:
        return _fail(data_dir, bot, error_output, stdout, 'other', 1, {'reason': 'claude-not-found'}, held=[])
    try:
        proc = subprocess.Popen([real, *argv], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return _fail(data_dir, bot, error_output, stdout, 'other', 1, {'reason': 'start-failed', 'detail': str(exc)[:300]},
                     held=[])

    stderr_chunks: list[bytes] = []
    reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    reader.start()
    timed_out = threading.Event()
    timer = threading.Timer(TIMEOUT, lambda: (timed_out.set(), proc.kill()))
    timer.start()
    try:
        proc.stdin.write(stdin_data)
        proc.stdin.close()
    except OSError:
        pass

    hold_errors = bool(error_output)
    held: list[bytes] = []
    codes: set[str] = set()
    status: object = None
    error_text = ''
    safety_stops = 0
    failed = False
    result_event: dict[str, typing.Any] | None = None
    # 调用中再查一次：调用前用的是上一次的占用数据（可能过时或者还没有）。
    # Claude Code 会在回复过程中报告最新占用（rate_limit_event），在那之前先把输出扣住；
    # 最新占用已经超限，就丢掉这次回复、改为输出锁定回复；没超限再原样放出。
    decided = not policy.account_limits_enabled(cfg)
    pending: list[bytes] = []
    late_lock: policy.Lock | None = None
    for raw in proc.stdout:
        hold = False
        try:
            event = json.loads(raw.decode('utf-8', errors='replace'))
        except json.JSONDecodeError:
            event = None
        if isinstance(event, dict):
            kind = event.get('type')
            if kind == 'rate_limit_event':
                now = time.time()
                snap = snapshot_from_event(event, now)
                if snap:
                    store.add_snapshot(snap)
                    if not decided:
                        decided = True
                        late_lock = policy.account_lock(snap, cfg, now)
                        if late_lock is None:
                            for line in pending:
                                stdout.write(line)
                            stdout.flush()
                        pending.clear()
            elif kind == 'assistant' and event.get('error'):
                codes.add(str(event['error']))
                hold = hold_errors
            elif kind == 'error':
                failed = True
                error_text += ' ' + str(event.get('message') or '')
                hold = hold_errors
            elif kind == 'result':
                result_event = event
                safety_stops = int(event.get('safety_stops') or 0)
                if event.get('is_error'):
                    failed = True
                    status = event.get('api_error_status')
                    error_text += ' ' + str(event.get('result') or '')
                    hold = hold_errors
        if late_lock is not None:
            continue  # 已判定超限：后面的输出一律丢掉
        if hold:
            held.append(raw)
        elif not decided:
            pending.append(raw)
        else:
            stdout.write(raw)
            stdout.flush()

    rc = proc.wait()
    timer.cancel()
    reader.join(timeout=2)
    stderr = b''.join(stderr_chunks).decode('utf-8', errors='replace').strip()
    ok = rc == 0 and not failed and not codes
    if result_event is not None:
        outcome = 'suppressed' if late_lock is not None else ('ok' if ok else 'error')
        store.add_call(_usage_call(result_event, bot, time.time(), outcome))
    if late_lock is not None:
        now = time.time()
        log_event(data_dir, {'event': 'locked', 'bot': bot, 'reason': late_lock.reason, 'detail': late_lock.detail,
                             'until': late_lock.until, 'during_call': True})
        emit_result(stdout, policy.render(lock_output, kind='limit', lock=late_lock, now=now))
        return 0
    if pending:
        # 没有收到占用数据（例如 API Key 账号）：照常放出
        for line in pending:
            stdout.write(line)
        stdout.flush()
    if ok:
        return 0

    text = f'{error_text} {stderr}'.strip()
    kind = 'other' if timed_out.is_set() else classify(status=status, codes=codes, text=text, safety_stops=safety_stops)
    detail = {'returncode': rc, 'api_error_status': status, 'codes': sorted(codes), 'safety_stops': safety_stops,
              'timed_out': timed_out.is_set(), 'detail': text[:300]}
    if stderr and not hold_errors:
        sys.stderr.write(stderr + '\n')
    return _fail(data_dir, bot, error_output, stdout, kind, rc or 1, detail, held=held)


def _fail(data_dir, bot, error_output, stdout, kind, rc, detail, *, held) -> int:
    log_event(data_dir, {'event': 'error', 'bot': bot, 'kind': kind, **detail})
    if not error_output:
        return rc  # 原样交给 LangBot（held 为空，因为没有扣留任何输出）
    emit_result(stdout, policy.render(error_output, kind=kind))
    return 0


def status(data_dir: pathlib.Path, now: float | None = None) -> dict[str, typing.Any]:
    """当前状态：三道闸门各用了多少、是否锁定。插件和命令行共用。"""
    now = now if now is not None else time.time()
    cfg = policy.Config.load(data_dir / 'config.json')
    store = UsageStore(data_dir / 'usage.db')
    try:
        snap = store.latest_snapshot()
        window = policy.current_window(store.calls_since(now - cfg.bot_window_hours * 7200), now, cfg.bot_window_hours,
                                       count_cache_read=cfg.count_cache_read)
        today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        by_bot: dict[str, dict[str, float]] = {}
        for call in store.calls_since(today):
            item = by_bot.setdefault(call.bot, {'calls': 0, 'tokens': 0, 'cost_usd': 0.0, 'locked': 0})
            if call.outcome == 'locked':
                item['locked'] += 1
                continue
            item['calls'] += 1
            item['tokens'] += call.tokens(count_cache_read=cfg.count_cache_read)
            item['cost_usd'] += call.cost_usd
        lock = policy.check(store, cfg, now)
        return {
            'locked': lock is not None,
            'lock_reason': lock.detail if lock else '',
            'lock_resets_in': policy.format_duration(lock.until - now) if lock and lock.until else '',
            'account': None if snap is None else {
                'checked_at': time.strftime('%m-%d %H:%M', time.localtime(snap.ts)),
                'status': snap.status,
                'five_hour_percent': None if snap.five_hour_util is None else round(snap.five_hour_util * 100),
                'five_hour_resets_in': policy.format_duration(snap.five_hour_resets_at - now) if snap.five_hour_resets_at else '',
                'seven_day_percent': None if snap.seven_day_util is None else round(snap.seven_day_util * 100),
                'seven_day_resets_in': policy.format_duration(snap.seven_day_resets_at - now) if snap.seven_day_resets_at else '',
            },
            'bot_window': None if window is None else {
                'used': window.used, 'limit': cfg.bot_window_tokens, 'calls': window.calls,
                'resets_in': policy.format_duration(window.end - now),
            },
            'today': by_bot,
            'limits': {'five_hour_percent': cfg.five_hour_limit_pct, 'seven_day_percent': cfg.seven_day_limit_pct,
                       'bot_window_hours': cfg.bot_window_hours, 'bot_window_tokens': cfg.bot_window_tokens},
        }
    finally:
        store.close()
