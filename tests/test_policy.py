from meter import policy
from meter.store import Call, Snapshot, UsageStore

H = 3600


def call(ts, tokens, outcome='ok'):
    return Call(ts, 'qq', 'm', tokens, 0, 0, 0, 0.0, outcome)


def test_window_starts_at_first_call_and_resets():
    calls = [call(0, 100), call(1 * H, 200), call(4.9 * H, 50)]
    w = policy.current_window(calls, now=4.95 * H, hours=5)
    assert w.start == 0 and w.used == 350 and w.calls == 3
    assert policy.current_window(calls, now=5.1 * H, hours=5) is None  # 满 5 小时清零
    calls.append(call(6 * H, 10))  # 新窗口从这次调用开始
    w = policy.current_window(calls, now=6.5 * H, hours=5)
    assert w.start == 6 * H and w.used == 10


def test_locked_calls_do_not_open_a_window():
    calls = [call(0, 100), call(5.5 * H, 0, 'locked')]
    assert policy.current_window(calls, now=5.6 * H, hours=5) is None


def test_cache_read_toggle():
    c = Call(0, 'qq', 'm', 1, 2, 3, 1000, 0.0, 'ok')
    assert c.tokens() == 1006 and c.tokens(count_cache_read=False) == 6


def store_with(tmp_path, snap=None, calls=()):
    store = UsageStore(tmp_path / 'usage.db')
    if snap:
        store.add_snapshot(snap)
    for c in calls:
        store.add_call(c)
    return store


def test_account_limits(tmp_path):
    now = 1000 * H
    store = store_with(tmp_path, Snapshot(now - 60, 'allowed', 0.81, now + 2 * H, 0.5, now + 50 * H))
    lock = policy.check(store, policy.Config(), now)
    assert lock.reason == policy.FIVE_HOUR and lock.until == now + 2 * H
    assert policy.check(store, policy.Config(five_hour_limit_pct=90), now) is None
    assert policy.check(store, policy.Config(five_hour_limit_pct=0), now) is None  # 0 = 不限
    assert policy.check(store, policy.Config(seven_day_limit_pct=40, five_hour_limit_pct=0), now).reason == policy.SEVEN_DAY
    assert policy.check(store, policy.Config(enabled=False), now) is None


def test_account_snapshot_after_reset_counts_as_zero(tmp_path):
    now = 1000 * H
    store = store_with(tmp_path, Snapshot(now - 3 * H, 'allowed', 0.95, now - H, 0.1, now + 50 * H))
    assert policy.check(store, policy.Config(), now) is None


def test_bot_window_limit(tmp_path):
    now = 1000 * H
    store = store_with(tmp_path, calls=[call(now - 2 * H, 600_000), call(now - H, 500_000)])
    lock = policy.check(store, policy.Config(), now)
    assert lock.reason == policy.BOT_WINDOW and lock.until == now + 3 * H
    assert policy.check(store, policy.Config(bot_window_tokens=2_000_000), now) is None


def test_render_and_config():
    lock = policy.Lock(policy.BOT_WINDOW, 3 * H + 120, 'x')
    text = policy.render('{reason}，{resets_in}后恢复 {kind}', kind='limit', lock=lock, now=0)
    assert text == '机器人窗口额度，3小时2分钟后恢复 limit'
    cfg = policy.Config.from_dict({'five-hour-limit-percent': '70', 'bot-window-tokens': 5, 'count-cache-read': False,
                                   'enabled': 'false', 'bot-window-hours': -1})
    assert (cfg.five_hour_limit_pct, cfg.bot_window_tokens, cfg.count_cache_read, cfg.enabled) == (70, 5, False, False)
    assert cfg.bot_window_hours == 5  # 负数用默认
