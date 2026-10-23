import json
import logging
import time

from meter.monitor import Monitor
from meter.store import Call, Snapshot, UsageStore
from components.event_listener.guard import Guard


def msgs(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.name.startswith('claude_usage_guard')]


def write_event(data_dir, **record):
    with (data_dir / 'events.log').open('a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


def test_events_are_logged_by_importance(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger='claude_usage_guard')
    write_event(tmp_path, event='error', kind='limit', bot='qq', detail='old')  # 启动前的旧记录不重放
    mon = Monitor(tmp_path)
    mon.poll()
    assert not caplog.records
    write_event(tmp_path, event='locked', bot='qq', reason='five_hour', detail='5 小时 81%')
    write_event(tmp_path, event='locked', bot='qq', reason='five_hour', detail='5 小时 81%', during_call=True)
    write_event(tmp_path, event='error', kind='refused', bot='qq', detail='policy')
    write_event(tmp_path, event='error', kind='other', bot='wechat', reason='claude-not-found')
    mon.poll()
    assert any('锁定中' in m for m in msgs(caplog, logging.INFO))
    warnings = msgs(caplog, logging.WARNING)
    assert any('调用中发现' in w for w in warnings) and any('安全机制拒答' in w for w in warnings)
    assert any('无法启动 claude' in e for e in msgs(caplog, logging.ERROR))  # 只有这个需要人处理


def test_lock_and_unlock_transitions(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger='claude_usage_guard')
    now = time.time()
    (tmp_path / 'config.json').write_text(json.dumps({'five-hour-limit-percent': 50}), encoding='utf-8')
    store = UsageStore(tmp_path / 'usage.db')
    store.add_snapshot(Snapshot(now, 'allowed', 0.46, now + 3600, 0.2, now + 86400))
    store.close()
    mon = Monitor(tmp_path)
    mon.poll(now)
    assert any('接近上限' in m for m in msgs(caplog, logging.INFO))  # 46% ≥ 50% × 0.9
    store = UsageStore(tmp_path / 'usage.db')
    store.add_snapshot(Snapshot(now + 1, 'allowed', 0.55, now + 3600, 0.2, now + 86400))
    store.add_call(Call(now + 2, 'qq', 'm', 1, 2, 3, 4, 0.01, 'ok'))
    store.close()
    mon.poll(now + 3)
    assert any('已锁定' in w for w in msgs(caplog, logging.WARNING))
    assert any('调用（qq' in d for d in msgs(caplog, logging.DEBUG))
    mon.poll(now + 3600 + 5)  # 窗口重置
    assert any('已解锁' in m for m in msgs(caplog, logging.INFO))


def test_guard_sync_logs_command_and_errors(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger='claude_usage_guard')
    guard = Guard()
    guard.sync({'data-dir': str(tmp_path / 'd'), 'log-level': 'INFO'})
    infos = msgs(caplog, logging.INFO)
    assert any('command 设成' in i for i in infos) and any('配置已同步' in i for i in infos)
    blocker = tmp_path / 'file'
    blocker.write_text('x')
    assert guard.sync({'data-dir': str(blocker / 'sub')}) is None  # 数据目录建不起来
    assert any('无法写入数据目录' in e for e in msgs(caplog, logging.ERROR))


def test_guard_without_data_dir_is_error_not_crash(caplog):
    caplog.set_level(logging.INFO, logger='claude_usage_guard')
    guard = Guard()
    assert guard.sync({}) is None
    assert guard.sync({}) is None  # 只报一次
    errors = msgs(caplog, logging.ERROR)
    assert len(errors) == 1 and '数据目录' in errors[0]
