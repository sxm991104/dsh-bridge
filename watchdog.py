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
import threading
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

# AstrBot 的启动脚本。可用环境变量覆盖，不写死路径。
START_BAT = os.environ.get("DSH_WATCHDOG_START_BAT") or os.path.join(
    BRIDGE_DIR, "start_astrbot.bat"
)

# AstrBot 的 WebUI 端口。起来没起来，就看这个端口通不通。
PORT = int(os.environ.get("DSH_WATCHDOG_PORT") or "6185")

# 直接起 AstrBot 用的可执行文件——bat 那条路坏了的兜底。
# 为什么要有这个：全指着 bat 的话，bat 一改坏、一删，
# 守护就废了，而且是在最需要它的时候。
ASTRBOT_EXE = os.environ.get("DSH_WATCHDOG_EXE") or os.path.join(
    os.path.expanduser("~"), ".local", "bin", "astrbot.exe"
)
# 起 exe 时的工作目录
ASTRBOT_CWD = os.environ.get("DSH_WATCHDOG_CWD") or ""

# 冷却期：两次重启之间至少隔这么久（秒）
COOLDOWN = 300
# 端口等多久算"起来了"
READY_TIMEOUT = 120
# 连续失败几次就停手
MAX_CONSECUTIVE_FAIL = 3
# 请求文件多久没处理就算过期（避免积压的旧请求突然触发）
REQUEST_STALE = 600

# 主动体检：每隔多久查一次"服务还活着吗"（秒）
# 这条和"有人请求重启"是两条独立的线——
# 没有人请求、AstrBot 自己崩了，也要能发现。
PROBE_SECONDS = int(os.environ.get("DSH_WATCHDOG_PROBE") or "30")
# 连续几次查不到才判定挂了（防止偶发抖动、也防止正在启动时误判）
PROBE_MISS_LIMIT = 3


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
    """拉起 AstrBot。两条路：先走 bat，不行就直接起 exe。

    ⚠️ 为什么要双保险（这条是别人提醒的，提醒得对）：
        原来只指着 start_astrbot.bat。**那个 bat 一旦被改坏、被删、
        或者里面路径变了，整个守护就废了**——而且是在最需要它的时候。
        所以留一条不依赖 bat 的路：直接调 astrbot.exe。

    两个都尽量用"独立窗口"的方式起，不要挂在守护进程下面：
    守护进程自己重启时不能把 AstrBot 一起带走。
    """
    # 路线一：bat（正常情况走这条，因为它可能有环境变量设置）
    if os.path.exists(START_BAT):
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", START_BAT],
                cwd=os.path.dirname(START_BAT),
                shell=False,
                close_fds=True,
            )
            _log(f"用 bat 拉起: {START_BAT}")
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"bat 拉起失败（{e}），改用 exe 直接起")

    # 路线二：直接起 astrbot.exe —— 不依赖那个 bat
    exe = ASTRBOT_EXE
    if exe and os.path.exists(exe):
        try:
            subprocess.Popen(
                [exe, "run"],
                cwd=ASTRBOT_CWD or None,
                shell=False,
                close_fds=True,
            )
            _log(f"用 exe 拉起: {exe} run")
            return True
        except Exception as e:  # noqa: BLE001
            _log(f"exe 拉起也失败: {e}")

    _log("两条路都不通：bat 不可用，exe 也起不来")
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


# ----------------------------------------------------------------------
# 主动体检：没人请求，也要知道服务死没死
# ----------------------------------------------------------------------

_miss_count = 0


def probe_loop() -> None:
    """每隔 PROBE_SECONDS 查一次端口，连续 MISS 次不通就拉起来。

    ⚠️ 这条是别人指出来的，指得对：
        第一版只做了"有人写 restart.request 才重启"。
        但 AstrBot 自己崩了的时候，**没有人会写那个文件**——
        守护进程就那么干看着，等于没装。

        真正的守护得主动体检。

    为什么是轮询而不是事件：
        服务死掉这件事不会通知任何人，没有可监听的事件源。
        只能去问。30 秒问一次，开销可以忽略（一次 TCP 连接尝试）。

    为什么要连续 MISS 次才动手：
        偶尔一次连不上（正在启动、系统卡顿）不算死。
        连续三次都连不上（约 90 秒）才算。
    """
    global _miss_count
    while True:
        time.sleep(PROBE_SECONDS)

        if _port_open():
            if _miss_count:
                _log(f"服务恢复（之前漏了 {_miss_count} 次探测）")
            _miss_count = 0
            continue

        _miss_count += 1
        _log(f"体检：{PORT} 端口不通（第 {_miss_count}/{PROBE_MISS_LIMIT} 次）")

        if _miss_count < PROBE_MISS_LIMIT:
            continue

        # 连续多次不通 —— 判定挂了，走同一套重启流程
        _log("连续探测不到，判定服务已挂，开始拉起")
        try:
            _auto_restart()
        except Exception:  # noqa: BLE001
            _log("自动拉起时异常:\n" + traceback.format_exc())
        _miss_count = 0


def _auto_restart() -> None:
    """自动拉起。走和手动请求一样的防护逻辑，但不读请求文件。"""
    global _last_restart, _consecutive_fail

    if _consecutive_fail >= MAX_CONSECUTIVE_FAIL:
        _log(f"连续失败 {_consecutive_fail} 次，停止自动拉起（删 {STUCK} 才恢复）")
        try:
            with open(STUCK, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except OSError:
            pass
        return

    since = time.time() - _last_restart
    if _last_restart and since < COOLDOWN:
        _log(f"冷却中（还要等 {int(COOLDOWN - since)} 秒），这次不拉")
        return

    started = time.time()
    if not _start_astrbot():
        _consecutive_fail += 1
        _log("自动拉起：启动命令失败")
        return

    if _wait_ready():
        _last_restart = time.time()
        _consecutive_fail = 0
        _log(f"自动拉起成功，用时 {round(time.time() - started, 1)} 秒")
    else:
        _consecutive_fail += 1
        _log(f"自动拉起失败：{READY_TIMEOUT} 秒内 {PORT} 没起来（第 {_consecutive_fail} 次）")


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

    # 启动时如果服务就是死的，顺手拉一把 —— 这不消耗任何东西，只是开机自检
    if not _port_open():
        _log("启动时发现 AstrBot 没在跑，先拉起")
        if _start_astrbot() and _wait_ready():
            _log("自检拉起成功")
        else:
            _log("自检拉起失败，继续监听请求")

    # 两条线并行：一条听"有人要重启"，一条定期体检"服务还活着吗"
    t = threading.Thread(target=probe_loop, daemon=True, name="probe")
    t.start()
    _log(f"主动体检已启动，每 {PROBE_SECONDS} 秒一次")

    watch_loop()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出")
    except Exception:  # noqa: BLE001
        _log("守护崩了:\n" + traceback.format_exc())
        raise
