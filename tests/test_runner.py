"""用一个假的 claude（打印 stream-json）把包装脚本完整跑一遍。"""

import io
import json
import sys

import pytest

from meter import runner
from meter.store import UsageStore

RATE = {'type': 'rate_limit_event', 'rate_limit_info': {
    'status': 'allowed_warning', 'unifiedWindows': {
        'five_hour': {'utilization': 0.37, 'resetsAt': 4_000_000_000},
        'seven_day': {'utilization': 0.74, 'resetsAt': 4_100_000_000}}}}
RESULT_OK = {'type': 'result', 'is_error': False, 'result': '<say>好</say>', 'total_cost_usd': 0.01,
             'usage': {'input_tokens': 2, 'output_tokens': 34, 'cache_creation_input_tokens': 984,
                       'cache_read_input_tokens': 6355}, 'modelUsage': {'claude-opus-5-5': {}}}
RESULT_429 = {'type': 'result', 'is_error': True, 'api_error_status': 429, 'result': "You've hit your limit",
              'usage': {}}


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    def make(lines, rc=0):
        script = tmp_path / 'fake.py'
        body = '\n'.join(f'print({json.dumps(json.dumps(line))})' for line in lines)
        script.write_text(f'import sys\nsys.stdin.read()\n{body}\nsys.exit({rc})\n', encoding='utf-8')
        cmd = tmp_path / 'fake.cmd'
        cmd.write_text(f'@echo off\r\n"{sys.executable}" "{script}"\r\n', encoding='ascii')
        monkeypatch.setenv('CUG_REAL_CLAUDE', str(cmd))
    return make


def run(data_dir, config=None, env=None, monkeypatch=None):
    if config is not None:
        (data_dir / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    out = io.BytesIO()
    rc = runner.run(['-p'], data_dir, stdin=io.BytesIO(b'hi'), stdout=out)
    lines = [json.loads(line) for line in out.getvalue().decode('utf-8').splitlines() if line.strip()]
    return rc, lines


def test_records_usage_and_snapshot(tmp_path, fake_claude, monkeypatch):
    monkeypatch.setenv('CUG_BOT', 'qq')
    fake_claude([RATE, RESULT_OK])
    rc, lines = run(tmp_path)
    assert rc == 0 and [l['type'] for l in lines] == ['rate_limit_event', 'result']
    store = UsageStore(tmp_path / 'usage.db')
    snap = store.latest_snapshot()
    assert (snap.five_hour_util, snap.seven_day_util, snap.status) == (0.37, 0.74, 'allowed_warning')
    (c,) = store.calls_since(0)
    assert (c.bot, c.tokens(), c.outcome, c.model) == ('qq', 7375, 'ok', 'claude-opus-5-5')
    store.close()
    status = runner.status(tmp_path)
    assert status['account']['five_hour_percent'] == 37 and status['bot_window']['used'] == 7375
    assert status['today']['qq']['calls'] == 1 and not status['locked']


def test_locks_without_calling_claude(tmp_path, fake_claude, monkeypatch):
    fake_claude([RATE, RESULT_OK])
    run(tmp_path)  # 记下 37% / 74%
    monkeypatch.setenv('CUG_REAL_CLAUDE', 'Z:/does-not-exist.cmd')  # 锁定时根本不该启动 claude
    rc, lines = run(tmp_path, {'seven-day-limit-percent': 70, 'lock-output': '锁了：{reason}'})
    assert rc == 0 and lines == [{'type': 'result', 'subtype': 'success', 'is_error': False,
                                   'result': '锁了：账号 7 天额度'}]
    monkeypatch.setenv('CUG_LOCK_OUTPUT', '<qgc-error kind="{kind}"/>')
    assert run(tmp_path)[1][0]['result'] == '<qgc-error kind="limit"/>'
    events = (tmp_path / 'events.log').read_text(encoding='utf-8')
    assert '"locked"' in events and 'seven_day' in events


def test_bot_window_lock(tmp_path, fake_claude):
    fake_claude([RESULT_OK])
    run(tmp_path, {'bot-window-tokens': 7000})
    rc, lines = run(tmp_path)  # 第一次已经用了 7375 > 7000
    assert lines[0]['result'].startswith('（额度暂时用完了，') and lines[0]['result'].endswith('后恢复）')


def test_error_passthrough_by_default(tmp_path, fake_claude):
    fake_claude([RATE, RESULT_429], rc=1)
    rc, lines = run(tmp_path)
    assert rc == 1 and lines[-1]['is_error'] is True  # 原样交给 LangBot


def test_error_template(tmp_path, fake_claude, monkeypatch):
    fake_claude([RATE, RESULT_429], rc=1)
    monkeypatch.setenv('CUG_ERROR_OUTPUT', '<qgc-error kind="{kind}"/>')
    rc, lines = run(tmp_path)
    assert rc == 0 and lines[-1]['result'] == '<qgc-error kind="limit"/>'
    assert all(not l.get('is_error') for l in lines)
    assert UsageStore(tmp_path / 'usage.db').latest_snapshot().five_hour_util == 0.37  # 出错也记下额度


def test_missing_claude(tmp_path, monkeypatch):
    monkeypatch.setenv('CUG_REAL_CLAUDE', str(tmp_path / 'nope.cmd'))
    monkeypatch.setenv('CUG_ERROR_OUTPUT', 'E:{kind}')
    assert run(tmp_path)[1][0]['result'] == 'E:other'


def test_classify():
    assert runner.classify(status=429, codes=set(), text='', safety_stops=0) == 'limit'
    assert runner.classify(status=None, codes=set(), text='appears to violate our Usage Policy', safety_stops=0) == 'refused'
    assert runner.classify(status=500, codes=set(), text='boom', safety_stops=0) == 'other'


ASSISTANT = {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': '<say>没token谁在跟你说话</say>'}]}}
SYSTEM = {'type': 'system', 'subtype': 'init'}


def test_locks_during_call_when_no_prior_snapshot(tmp_path, fake_claude):
    """第一次调用、手里没有占用数据：调用中拿到的最新占用已经超限，就丢掉回复、改回锁定回复。"""
    fake_claude([SYSTEM, ASSISTANT, RATE, RESULT_OK])
    rc, lines = run(tmp_path, {'five-hour-limit-percent': 30, 'seven-day-limit-percent': 70})
    assert rc == 0 and len(lines) == 1 and lines[0]['result'].startswith('（额度暂时用完了，')
    store = UsageStore(tmp_path / 'usage.db')
    (c,) = store.calls_since(0)
    assert c.outcome == 'suppressed' and c.tokens() == 7375  # token 照样花了，计入机器人窗口
    store.close()
    assert '"during_call": true' in (tmp_path / 'events.log').read_text(encoding='utf-8')
    # 下一次在调用前就锁住，不再启动 claude
    assert run(tmp_path)[1][0]['result'].startswith('（额度暂时用完了，')


def test_under_limit_output_is_forwarded_in_order(tmp_path, fake_claude):
    fake_claude([SYSTEM, ASSISTANT, RATE, RESULT_OK])
    rc, lines = run(tmp_path, {'five-hour-limit-percent': 80, 'seven-day-limit-percent': 90})
    assert rc == 0 and [l['type'] for l in lines] == ['system', 'assistant', 'rate_limit_event', 'result']


def test_no_rate_event_still_forwards(tmp_path, fake_claude):
    fake_claude([SYSTEM, ASSISTANT, RESULT_OK])  # 例如 API Key 账号：没有占用数据
    rc, lines = run(tmp_path, {'five-hour-limit-percent': 30})
    assert rc == 0 and [l['type'] for l in lines] == ['system', 'assistant', 'result']


def test_account_limits_off_does_not_buffer(tmp_path, fake_claude):
    fake_claude([SYSTEM, ASSISTANT, RATE, RESULT_OK])
    rc, lines = run(tmp_path, {'five-hour-limit-percent': 0, 'seven-day-limit-percent': 0})
    assert [l['type'] for l in lines] == ['system', 'assistant', 'rate_limit_event', 'result']
