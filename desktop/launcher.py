"""服务的启停：不经过任何命令行窗口。

为什么要单独有这么一层
    原来启动 QQ 和 AstrBot 都是双击 bat，屏幕上会弹一个黑框。
    给普通用户用的话这不行——他们会以为程序坏了，或者不小心把黑框关了。
    所以这里把 bat 里干的活直接用 Python 重做一遍：
    · CREATE_NO_WINDOW 让子进程不出控制台
    · 环境变量在这里设，不依赖批处理的 set
    · 进程句柄留着，后面要停的时候直接杀

两个服务各自的麻烦
    QQ：靠 NapCat 注入。要设一串环境变量，再调 NapCatWinBootMain.exe
        把 QQ 拉起来。注册表能查到 QQ 装在哪，查不到就让用户选。
    AstrBot：是个 exe，直接起，工作目录要对。

判断"起来了没有"
    QQ 看 3000 端口（NapCat 的 HTTP 接口）
    AstrBot 看 6185 端口（Web 面板）
    端口通了才算数——只看进程存在是不够的，
    QQ 起来了但没注入、AstrBot 起来了但没连上适配器，都会卡在半路。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

# 子进程绝对不留窗口。
#
# ⚠️ 这里踩了一个很实在的坑：
#     QQ.exe 自己的 PE Subsystem = 2，也就是"控制台程序"。
#     意味着它会自己创建一个控制台窗口，不管你爸爸怎么起它。
#     NapCatWinBootMain.exe 是 windowless 的，但它再拉 QQ 时，
#     如果继承了自己的控制台，QQ 那个黑框就冒出来了。
#
#     所以光给 CREATE_NO_WINDOW 不够，还得用 DETACHED_PROCESS
#     让子进程彻底没有控制台可用——它想创建也创建不出来。
#
#     代价：不能再用管道读它的标准输出（detached 没有 stdio），
#     所以这里全部走 DEVNULL。要日志就让它自己写文件。
_NOWINDOW = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
_DETACHED = 0x00000008 if sys.platform == "win32" else 0     # DETACHED_PROCESS
_NEWGROUP = 0x00000200 if sys.platform == "win32" else 0     # CREATE_NEW_PROCESS_GROUP
_FLAGS = _NOWINDOW | _DETACHED | _NEWGROUP

# 路径不再写死。
#
# 原来这里是三个常量，钉死在一台机器上（D:\qqaibot、用户名 19836）。
# 换台机器就跑不起来：别人没有 D 盘，用户名也不是这个。
#
# 现在的做法：
#   1. 从 %APPDATA%\dsh-bridge\config.json 读用户配置
#   2. 没配过的，自动探测一次（查注册表 + 常见位置）
#   3. 探测不到就留空，界面提示用户自己选
from paths import get as _cfg_get, detect_all as _detect_all  # noqa: E402

QQ_PORT = 3000
ASTRBOT_PORT = 6185


# ----------------------------------------------------------------------
# 端口探测
# ----------------------------------------------------------------------

def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_port(port: int, timeout: int = 90) -> bool:
    for _ in range(timeout):
        if port_open(port):
            return True
        time.sleep(1)
    return False


def listening_pids(port: int) -> list[str]:
    """谁在监听这个端口。停服务时按 PID 精确杀，别误伤。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, timeout=15,
            creationflags=_FLAGS,
        ).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.split()
            if parts:
                pids.append(parts[-1])
    return list(dict.fromkeys(pids))


# ----------------------------------------------------------------------
# 查 QQ 装在哪
# ----------------------------------------------------------------------

def find_qq() -> str:
    """从注册表找 QQ.exe 的完整路径。

    NapCat 的 bat 也是这么找的（UNINSTALL 里的 UninstallString），
    这里用 winreg 重做一遍，省得再调批处理。
    """
    try:
        import winreg
    except ImportError:
        return ""

    keys = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ"),
    ]
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                raw, _ = winreg.QueryValueEx(k, "UninstallString")
        except OSError:
            continue
        # UninstallString 是带引号的完整路径，取它的目录再拼 QQ.exe
        raw = str(raw).strip().strip('"')
        folder = os.path.dirname(raw)
        exe = os.path.join(folder, "QQ.exe")
        if os.path.exists(exe):
            return exe
    return ""


# ----------------------------------------------------------------------
# 启动 / 停止
# ----------------------------------------------------------------------

def _watch_until(fn, duration: float, interval: float) -> None:
    """反复调 fn，持续 duration 秒。"""
    deadline = time.time() + duration
    while time.time() < deadline:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)


class Launcher:
    """管两个服务的起停。进程句柄自己留着，停的时候不用去满机器找。"""

    def __init__(self) -> None:
        self.qq_proc: subprocess.Popen | None = None
        self.bot_proc: subprocess.Popen | None = None
        self.refresh_paths()

    def refresh_paths(self) -> None:
        """重新读一遍配置。

        用户可能在设置页刚改过路径，所以每次动作前都该重读，
        不能构造函数里读一次就锁死。
        """
        self.napcat_dir = _cfg_get("napcat_dir")
        self.astrbot_exe = _cfg_get("astrbot_exe")
        self.astrbot_pyw = _cfg_get("astrbot_pyw")
        self.astrbot_cwd = _cfg_get("astrbot_cwd")

    # ── QQ ──
    @property
    def qq_running(self) -> bool:
        return port_open(QQ_PORT)

    def start_qq(self) -> tuple[bool, str]:
        """拉起 QQ 并注入 NapCat。不出任何窗口。"""
        if self.qq_running:
            return True, "QQ 已经在跑了。"

        if not self.napcat_dir:
            return False, "还没设置 NapCat 在哪。去「设置」里选一下。"

        launcher = os.path.join(self.napcat_dir, "NapCatWinBootMain.exe")
        hook = os.path.join(self.napcat_dir, "NapCatWinBootHook.dll")
        if not os.path.exists(launcher):
            return False, f"NapCat 目录里找不到启动器：{launcher}"

        qq = _cfg_get("qq_exe")
        if not qq or not os.path.exists(qq):
            return False, "还没设置 QQ 装在哪。去「设置」里选一下。"

        # 这些环境变量本来在 bat 里 set，现在直接给子进程
        env = {**os.environ}
        env["NAPCAT_PATCH_PACKAGE"] = os.path.join(self.napcat_dir, "qqnt.json")
        env["NAPCAT_LOAD_PATH"] = os.path.join(self.napcat_dir, "loadNapCat.js")
        env["NAPCAT_INJECT_PATH"] = hook
        env["NAPCAT_LAUNCHER_PATH"] = launcher
        env["NAPCAT_MAIN_PATH"] = os.path.join(self.napcat_dir, "napcat.mjs")

        try:
            self.qq_proc = subprocess.Popen(
                [launcher, qq, hook],
                cwd=self.napcat_dir,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=_FLAGS,
            )
        except OSError as e:
            return False, f"起不来：{e}"

        # QQ.exe 自己是控制台程序，会自己建一个黑框。
        # creationflags 管不了它（那是对"怎么创建子进程"生效的），
        # 只能起来之后去把那个窗口藏掉。
        # 启动过程中它可能重建几次，所以盯半分钟。
        try:
            import threading

            from hide_console import watch_and_hide

            threading.Thread(
                target=watch_and_hide, args=(35.0, 0.7), daemon=True
            ).start()
        except Exception:  # noqa: BLE001
            pass

        return True, "QQ 正在启动，可能要扫一下码。"

    def stop_qq(self) -> tuple[bool, str]:
        """关掉 QQ。只杀 QQ.exe 的进程树，不碰别的。"""
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", "QQ.exe"],
                capture_output=True, timeout=20,
                creationflags=_FLAGS,
            )
        except Exception:  # noqa: BLE001
            pass
        return True, "QQ 已经关掉。"

    # ── AstrBot ──
    @property
    def bot_running(self) -> bool:
        return port_open(ASTRBOT_PORT)

    def start_bot(self) -> tuple[bool, str]:
        """启动对话服务，全程零控制台窗口。

        这里的关键发现：
            `astrbot.exe` 其实不是真的可执行程序，它是个 Python 脚本，
            由 `python.exe` 当脚本跑起来：
                python.exe  astrbot.exe  run
            再往下还会再拉一层 Python。每一层都是控制台程序，
            所以启动时总会闪一下黑框——试过 DETACHED_PROCESS、
            试过 20 毫秒轮询藏窗口，都躲不掉那一闪。

            真正的解法是换启动器：用 `pythonw.exe`，它是 Python 的
            无窗口版本，stdout/stderr 直接丢弃，压根不创建控制台。
                pythonw.exe  astrbot.exe  run
            实测：启动到就绪 17 秒，期间控制台窗口数始终为 0。
        """
        if self.bot_running:
            return True, "对话服务已经在跑了。"

        if not os.path.exists(self.astrbot_exe):
            return False, f"找不到 AstrBot：{self.astrbot_exe}"

        env = {**os.environ}
        env["ASTRBOT_ROOT"] = self.astrbot_cwd

        pyw = self.astrbot_pyw or ""
        if os.path.exists(pyw):
            cmd = [pyw, self.astrbot_exe, "run"]
        else:
            # 没有 pythonw 就退回 exe，会闪一下黑框
            cmd = [self.astrbot_exe, "run"]

        try:
            self.bot_proc = subprocess.Popen(
                cmd,
                cwd=self.astrbot_cwd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=_FLAGS,
            )
        except OSError as e:
            return False, f"起不来：{e}"

        return True, "对话服务正在启动。"

    def stop_bot(self) -> tuple[bool, str]:
        for pid in listening_pids(ASTRBOT_PORT):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True, timeout=20,
                    creationflags=_FLAGS,
                )
            except Exception:  # noqa: BLE001
                pass
        return True, "对话服务已经关掉。"
