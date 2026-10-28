"""桌面端的一键启动器。

为什么要多这一层
    桌面端其实是两件事：接口服务 + 界面。
    直接给 desktop_qt.py 建快捷方式的话，接口服务没人起，
    界面上全是"连不上后台"。

    这个脚本负责：
        1. 看看接口服务起了没，没起就先起它
        2. 等它真的能响应了
        3. 再开界面

    用户双击快捷方式，只看到一个窗口，背后两件事都办好了。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
API_PORT = 8790


def port_ok(port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def main() -> int:
    # 用一个无窗口的 Python 拉起接口服务，别开黑框
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable

    if not port_ok(API_PORT):
        try:
            subprocess.Popen(
                [pyw, os.path.join(HERE, "api_server.py")],
                cwd=HERE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=0x08000000 | 0x00000008,  # NO_WINDOW | DETACHED
            )
        except OSError:
            pass

        # 等它能响应，最多 15 秒
        for _ in range(30):
            time.sleep(0.5)
            if port_ok(API_PORT):
                break

    # 起界面。用同一个解释器，保持依赖一致。
    os.chdir(HERE)
    return subprocess.call([sys.executable, os.path.join(HERE, "desktop_qt.py")])


if __name__ == "__main__":
    sys.exit(main())
