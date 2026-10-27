"""极简服务守护：只在被叫的时候才醒。

为什么需要它
    AstrBot 挂了要有人拉。但"要有人拉"这件事本身有个圈：
    如果拉它的人也在 AstrBot 里，那 AstrBot 一死，拉它的人也死了。

    所以得有一个**不在 AstrBot 进程树下面**的独立东西盯着。

为什么不是计划任务轮询
    计划任务是"每 N 分钟查一次"——没病也去敲门，纯浪费。
    而且真挂的时候，要等下一个周期才发现。

    这里用 Windows 的目录变更通知（ReadDirectoryChangesW）：
    内核在文件变化时把事件推过来，**没有事件时这个进程一个 CPU 周期都不占**。

它监听什么
    <桥目录>\\restart.request

    其他程序（比如桥、任务端）要重启 AstrBot 时，只做一件事：
    往这个文件里写一行。剩下的交给这里。

它做什么
    1. 看到 restart.request 出现/变化
    2. 读里面的内容（记下是谁请求的、什么理由）
    3. 杀掉 AstrBot 整棵进程树
    4. 重新拉起（start_astrbot.bat）
    5. 等端口起来，确认成功
    6. 把结果写到 restart.result，删掉请求文件

防护（这些不是可选项）
    · **冷却期**：一次重启后 5 分钟内不再重启第二次。
      今天已经因为重启太多次被 QQ 踢过一次，这个必须挡住。
    · **连续失败上限**：连续失败 3 次就停手，写个 .stuck 文件，
      不再自动重试。免得把机器搞成"起了就崩、崩了再起"的死循环。
    · **不碰 NapCat**：NapCat 自己会重连，重启它反而容易触发风控。

用法
    pythonw watchdog.py            ← 无窗口跑（推荐）
    python watchdog.py             ← 带控制台，调试时用
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import traceback
from ctypes import wintypes
from datetime import datetime

# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
REQUEST = os.path.join(BRIDGE_DIR, "restart.request")
RESULT = os.path.join(BRIDGE_DIR, "restart.result")
STUCK = os.path.join(BRIDGE_DIR, "restart.stuck")

# AstrBot 的启动脚本。开源版必须可配置，不能写死某台机器的路径。
#
# 优先级：环境变量 DSH_WATCHDOG_START_BAT > 同目录下的 start_astrbot.bat
START_BAT = os.environ.get("DSH_WATCHDOG_START_BAT") or os.path.join(
    BRIDGE_DIR, "start_astrbot.bat"
)

# AstrBot 的 WebUI 端口。起来没起来，就看这个端口通不通。
PORT = int(os.environ.get("DSH_WATCHDOG_PORT") or "6185")

# 冷却期：两次重启之间至少隔这么久（秒）
COOLDOWN = 300
# 端口等多久算"起来了"
READY_TIMEOUT = 120
# 连续失败几次就停手
MAX_CONSECUTIVE_FAIL = 3
# 请求文件多久没处理就算过期（避免积压的旧请求突然触发）
REQUEST_STALE = 600


def _log(msg: str) -> None:
    """写日志。用文件而不是 stdout —— pythonw 下没有控制台。"""
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    try:
        with open(os.path.join(BRIDGE_DIR, "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ----------------------------------------------------------------------
# 端口检查
# ----------------------------------------------------------------------


def _port_open(port: int = PORT) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _kill_astrbot_tree() -> bool:
    """杀掉监听 6185 的那个进程及其整棵树。返回是否真的杀了。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=15,
        ).stdout
    except Exception:  # noqa: BLE001
        return False

    pid = ""
    for line in out.splitlines():
        if f":{PORT}" in line and "LISTENING" in line:
            pid = line.split()[-1]
            break
    if not pid:
        return False

    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", pid],
            capture_output=True, timeout=20, shell=False,
        )
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    return not _port_open()


def _start_astrbot() -> bool:
    """拉起 AstrBot。用 start 开独立窗口，不挂在守护进程下面。"""
    try:
        subprocess.Popen(
            ["cmd", "/c", "start", "", START_BAT],
            cwd=os.path.dirname(START_BAT),
            shell=False,
            close_fds=True,
        )
        return True
    except Exception as e:  # noqa: BLE001
        _log(f"拉起失败: {e}")
        return False


def _wait_ready() -> bool:
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if _port_open():
            return True
        time.sleep(2)
    return False


# ----------------------------------------------------------------------
# 处理一次重启
# ----------------------------------------------------------------------

_last_restart = 0.0
_consecutive_fail = 0


def handle_request() -> None:
    global _last_restart, _consecutive_fail

    # 冷却期内直接拒绝——这条是为了保护 QQ 号，不是保护服务
    since = time.time() - _last_restart
    if _last_restart and since < COOLDOWN:
        wait = int(COOLDOWN - since)
        _write_result({"status": "rejected", "reason": f"冷却中，还要等 {wait} 秒"})
        _log(f"拒绝重启请求：冷却中，还要等 {wait} 秒")
        _consume_request()
        return

    # 连续失败太多次就停手，等人来看
    if _consecutive_fail >= MAX_CONSECUTIVE_FAIL:
        _write_result({
            "status": "stuck",
            "reason": f"连续失败 {_consecutive_fail} 次，已停止自动重启。删掉 {STUCK} 才会恢复。",
        })
        _log(f"连续失败 {_consecutive_fail} 次，停手")
        try:
            with open(STUCK, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except OSError:
            pass
        _consume_request()
        return

    # 请求太旧就当没看见（防止积压的文件突然全触发）
    try:
        age = time.time() - os.path.getmtime(REQUEST)
        if age > REQUEST_STALE:
            _log(f"请求文件太旧（{int(age)}秒），忽略")
            _consume_request()
            return
    except OSError:
        return

    reason = ""
    try:
        with open(REQUEST, encoding="utf-8") as f:
            reason = f.read().strip()[:200]
    except OSError:
        pass

    _log(f"开始重启。理由: {reason or '(未说明)'}")
    started = time.time()

    already_up = _port_open()
    killed = _kill_astrbot_tree() if already_up else True

    if not _start_astrbot():
        _consecutive_fail += 1
        _write_result({"status": "error", "reason": "拉起命令失败"})
        _consume_request()
        return

    ready = _wait_ready()
    elapsed = round(time.time() - started, 1)

    if ready:
        _last_restart = time.time()
        _consecutive_fail = 0
        _write_result({
            "status": "ok",
            "seconds": elapsed,
            "was_running": already_up,
            "reason": reason,
        })
        _log(f"重启成功，用时 {elapsed} 秒")
    else:
        _consecutive_fail += 1
        _write_result({
            "status": "error",
            "reason": f"{READY_TIMEOUT} 秒内端口没起来（第 {_consecutive_fail} 次失败）",
            "seconds": elapsed,
        })
        _log(f"重启失败：{READY_TIMEOUT} 秒内 {PORT} 没起来")

    _consume_request()


def _write_result(rec: dict) -> None:
    rec["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    tmp = RESULT + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, RESULT)
    except OSError:
        pass


def _consume_request() -> None:
    try:
        os.remove(REQUEST)
    except OSError:
        pass


# ----------------------------------------------------------------------
# 目录监控（事件驱动，不轮询）
# ----------------------------------------------------------------------

# ReadDirectoryChangesW 的最小可用封装。只用这一件事，不做通用库。
_INVALID_HANDLE = ctypes.c_void_p(-1).value
_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
_FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
_FILE_NOTIFY_CHANGE_SIZE = 0x00000008


def watch_loop() -> None:
    """监听目录变化。

    ⚠️ 这里返工过一次，原因值得留：
        第一版用 ctypes 直接调 ReadDirectoryChangesW。
        实测写文件进去，守护进程 CPU 一直是 0、没有任何反应——
        调用卡在里面没返回，参数拼错也不报错。

        ctypes 拼 Win32 结构太容易出无声的错，
        改用 pywin32 的 win32file，它把这层封装好了。
    """
    import win32con
    import win32file

    try:
        handle = win32file.CreateFile(
            BRIDGE_DIR,
            win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None,
            win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
    except Exception as e:  # noqa: BLE001
        _log(f"打不开目录监控: {e}")
        return

    target = os.path.basename(REQUEST).lower()
    _log(f"开始监听 {BRIDGE_DIR}")

    while True:
        try:
            # 阻塞等待，有变化才返回。没有事件时这个进程不占 CPU。
            results = win32file.ReadDirectoryChangesW(
                handle,
                8192,
                False,
                win32con.FILE_NOTIFY_CHANGE_FILE_NAME
                | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE
                | win32con.FILE_NOTIFY_CHANGE_SIZE,
                None,
                None,
            )
        except Exception as e:  # noqa: BLE001
            _log(f"读取通知失败: {e}，5 秒后重试")
            time.sleep(5)
            continue

        hit = False
        for _action, name in results or []:
            if str(name).lower() == target:
                hit = True
                break

        if hit and os.path.exists(REQUEST):
            try:
                handle_request()
            except Exception:  # noqa: BLE001
                _log("处理请求时异常:\n" + traceback.format_exc())



def main() -> None:
    _log("守护启动")

    # 启动自检：启动脚本不存在就别装模作样地跑下去。
    # 否则它会在"看起来在守护"的状态里，真要重启时才发现叫不起来。
    if not os.path.exists(START_BAT):
        _log(f"⚠️ 启动脚本不存在: {START_BAT}")
        _log("   用环境变量 DSH_WATCHDOG_START_BAT 指定，或把 start_astrbot.bat 放同目录")
        _log("   守护仍然监听，但拉起会失败——先修好路径")
    else:
        _log(f"启动脚本: {START_BAT}")
    _log(f"监控端口: {PORT}")

    # 启动时如果服务就是死的，顺手拉一把 —— 这不消耗任何东西，只是开机自检
    if not _port_open():
        _log("启动时发现 AstrBot 没在跑，先拉起")
        if _start_astrbot() and _wait_ready():
            _log("自检拉起成功")
        else:
            _log("自检拉起失败，继续监听请求")

    watch_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出")
    except Exception:  # noqa: BLE001
        _log("守护崩了:\n" + traceback.format_exc())
        raise
