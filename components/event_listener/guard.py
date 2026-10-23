from __future__ import annotations

import json
import pathlib
import shutil
import sys
import typing

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events

from meter import __version__
from meter.log import apply_level, get_logger
from meter.monitor import Monitor

logger = get_logger(__name__)

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parents[2]
CONFIG_KEYS = (
    'enabled', 'five-hour-limit-percent', 'seven-day-limit-percent', 'bot-window-hours', 'bot-window-tokens',
    'count-cache-read', 'lock-output', 'error-output',
)


class SyncResult(typing.NamedTuple):
    data_dir: pathlib.Path
    command: str
    config_changed: bool
    installed: bool


def default_data_dir() -> pathlib.Path:
    return pathlib.Path.home() / '.claude-usage-guard'


def resolve_data_dir(config: dict[str, typing.Any]) -> pathlib.Path:
    raw = str(config.get('data-dir') or '').strip()
    return pathlib.Path(raw) if raw else default_data_dir()


def sync(config: dict[str, typing.Any], *, plugin_root: pathlib.Path = PLUGIN_ROOT, python: str = sys.executable) -> SyncResult:
    """把插件配置写成 <数据目录>/config.json，把包装脚本装到 <数据目录>/bin/（版本变了才重装）。"""
    data_dir = resolve_data_dir(config)
    data_dir.mkdir(parents=True, exist_ok=True)

    wanted = {key: config.get(key) for key in CONFIG_KEYS if config.get(key) is not None}
    path = data_dir / 'config.json'
    text = json.dumps(wanted, ensure_ascii=False, indent=2)
    try:
        current = path.read_text(encoding='utf-8')
    except OSError:
        current = None
    changed = current != text
    if changed:
        tmp = path.with_suffix('.tmp')
        tmp.write_text(text, encoding='utf-8')
        tmp.replace(path)  # 原子替换：包装脚本随时可能在读

    bin_dir = data_dir / 'bin'
    marker = bin_dir / 'VERSION'
    installed = not (marker.exists() and marker.read_text(encoding='utf-8').strip() == __version__)
    if installed:
        bin_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plugin_root / 'tools' / 'claude_meter.py', bin_dir / 'claude_meter.py')
        target = bin_dir / 'meter'
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(plugin_root / 'meter', target, ignore=shutil.ignore_patterns('__pycache__'))
        marker.write_text(__version__, encoding='utf-8')

    command = f'{python} -I {bin_dir / "claude_meter.py"}'
    cmd_file = data_dir / 'COMMAND.txt'
    note = ('把 LangBot 流水线里 Claude Code Runner 的「command」设成下面这一行：\n'
            f'{command}\n\n查看当前用量：{command} --status\n')
    try:
        if cmd_file.read_text(encoding='utf-8') != note:
            cmd_file.write_text(note, encoding='utf-8')
    except OSError:
        cmd_file.write_text(note, encoding='utf-8')
    return SyncResult(data_dir, command, changed, installed)


def describe(config: dict[str, typing.Any]) -> str:
    def val(key, default):
        value = config.get(key)
        return default if value in (None, '') else value

    return ('闸门{}；账号 5 小时上限 {}%，7 天上限 {}%；机器人窗口 {} 小时 / {} token（0 = 不限）').format(
        '开启' if val('enabled', True) not in (False, 'false', 'False', 0) else '关闭（只记录）',
        val('five-hour-limit-percent', 80), val('seven-day-limit-percent', 90),
        val('bot-window-hours', 5), val('bot-window-tokens', 1_000_000),
    )


class Guard:
    """插件进程里的状态：同步配置、汇报变化、转写包装脚本的记录。插件入口和事件监听共用一个实例。"""

    _instance: Guard | None = None

    def __init__(self) -> None:
        self._monitor: Monitor | None = None
        self._announced: pathlib.Path | None = None

    @classmethod
    def shared(cls) -> Guard:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def sync(self, config: dict[str, typing.Any]) -> pathlib.Path | None:
        level = apply_level(config.get('log-level'))
        if not str(config.get('data-dir') or '').strip():
            if self._announced is not False:
                self._announced = False  # type: ignore[assignment]
                logger.error('没有填写数据目录（data-dir）：配置不会同步给包装脚本，日志里也不会有用量记录。'
                             '请在插件配置里填一个本机绝对路径。')
            return None
        try:
            result = sync(config)
        except Exception:
            logger.exception('无法写入数据目录 %s：配置没有同步给包装脚本，闸门可能还在用旧配置。'
                             '请检查数据目录（data-dir）是否存在、是否有写入权限。', config.get('data-dir'))
            return None
        if result.installed:
            logger.info('包装脚本已安装 / 更新到 %s（%s）', result.data_dir / 'bin', __version__)
        if result.config_changed:
            logger.info('配置已同步：%s；日志级别 %s', describe(config), level)
        else:
            logger.debug('配置没有变化')
        if self._announced != result.data_dir:
            self._announced = result.data_dir
            logger.info('数据目录：%s。请把每条用 Claude Code Runner 的流水线的 command 设成：%s',
                        result.data_dir, result.command)
            self._monitor = Monitor(result.data_dir)
            self.poll()
        return result.data_dir

    def poll(self) -> None:
        if self._monitor is None:
            return
        try:
            self._monitor.poll()
        except Exception:
            logger.warning('读取用量记录失败（不影响闸门本身）', exc_info=True)


class GuardEventListener(EventListener):
    """不拦任何消息：闸门在包装脚本里。这里在调用 Claude 之前同步配置，在调用之后把包装脚本的记录写进日志。"""

    async def initialize(self):
        await super().initialize()
        guard = Guard.shared()

        def safe_sync() -> None:
            try:
                guard.sync(self.get_plugin_config() or {})
            except Exception:
                logger.exception('同步配置时出错（不影响消息处理）')

        safe_sync()

        async def on_before_claude(ctx: context.EventContext):
            safe_sync()
            guard.poll()

        async def on_after_claude(ctx: context.EventContext):
            guard.poll()

        self.handler(events.PersonNormalMessageReceived)(on_before_claude)
        self.handler(events.GroupNormalMessageReceived)(on_before_claude)
        self.handler(events.NormalMessageResponded)(on_after_claude)
