"""claude 的包装命令：把 LangBot Claude Code Runner 的 command 设成

    <python> -I <数据目录>\\bin\\claude_meter.py

插件启动时会把本文件和 meter/ 复制到 <数据目录>\\bin\\，并把完整命令写进 <数据目录>\\COMMAND.txt。

    claude_meter.py --status    打印当前用量和锁定状态（JSON）
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
# 安装后的布局：<数据目录>/bin/claude_meter.py + <数据目录>/bin/meter/；仓库里的布局：tools/ + meter/
sys.path.insert(0, str(HERE if (HERE / 'meter').is_dir() else HERE.parent))

from meter import runner  # noqa: E402


def data_dir() -> pathlib.Path:
    if os.environ.get('CUG_DATA_DIR'):
        return pathlib.Path(os.environ['CUG_DATA_DIR'])
    return HERE.parent  # 安装后 bin/ 的上一级就是数据目录


if __name__ == '__main__':
    if sys.argv[1:] == ['--status']:
        sys.stdout.reconfigure(encoding='utf-8')
        print(json.dumps(runner.status(data_dir()), ensure_ascii=False, indent=2))
        sys.exit(0)
    sys.exit(runner.run(sys.argv[1:], data_dir(), stdin=sys.stdin.buffer, stdout=sys.stdout.buffer))
