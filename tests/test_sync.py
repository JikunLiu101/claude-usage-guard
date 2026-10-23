import json
import subprocess
import sys

from components.event_listener.guard import PLUGIN_ROOT, sync


def test_sync_writes_config_and_installs_wrapper(tmp_path):
    data = tmp_path / 'guard'
    sync({'data-dir': str(data), 'five-hour-limit-percent': 70, 'lock-output': 'x'}, python='PY')
    assert json.loads((data / 'config.json').read_text(encoding='utf-8')) == {
        'five-hour-limit-percent': 70, 'lock-output': 'x'}
    assert (data / 'bin' / 'claude_meter.py').exists() and (data / 'bin' / 'meter' / 'runner.py').exists()
    assert f'PY -I {data / "bin" / "claude_meter.py"}' in (data / 'COMMAND.txt').read_text(encoding='utf-8')

    # 装好的包装脚本能独立运行（不依赖插件目录）
    out = subprocess.run([sys.executable, '-I', str(data / 'bin' / 'claude_meter.py'), '--status'],
                         capture_output=True, text=True, encoding='utf-8')
    assert out.returncode == 0 and json.loads(out.stdout)['limits']['five_hour_percent'] == 70


def test_plugin_root_layout():
    assert (PLUGIN_ROOT / 'tools' / 'claude_meter.py').exists() and (PLUGIN_ROOT / 'meter' / 'policy.py').exists()
