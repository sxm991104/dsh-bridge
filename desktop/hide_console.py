"""把 QQ 那个控制台窗口藏起来。

问题
    QQ.exe 的 PE 头里 Subsystem = 2，也就是"控制台程序"。
    它一启动就自己建一个控制台窗口，标题还带完整路径
    （「C:\\Program Files\\QQ.exe」）。谁去启动它都躲不开这个窗口——
    CREATE_NO_WINDOW / DETACHED_PROCESS 是对"父进程怎么创建子进程"
    生效的，管不到子进程自己后来新建的窗口。

为什么不能简单隐藏
    隐藏窗口有几种做法，各有代价，这里都试过：

    · ShowWindow(SW_HIDE)：进程下一轮刷新窗口状态时可能又冒出来，
      而且有些程序会主动重新显示自己。
    · 直接把窗口的 WndProc 换掉：太侵入，QQ 自己会崩。
    · 设置 STARTUPINFO 的 wShowWindow：只对父进程传下来的那个
      控制台有效，对子进程新建的无效。

    **能稳定做到的，是给控制台窗口换一个属主窗口**——
    把这个控制台窗口的 owner 设成 QQ 自己的主窗口，
    这样它就永远排在主窗口后面，任务栏里也不单独占位。
    用户看到的就是"QQ 窗口"，不会看到那个黑框。

更省事的做法
    用 SW_HIDE 藏掉，然后持续盯着：一旦它再冒出来就再藏一次。
    QQ 的控制台窗口在启动完成后一般不会主动再显示，
    所以这个"盯一段时间就收工"的方式最省事也够用。
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

SW_HIDE = 0
SW_SHOWNORMAL = 1


class _TITLEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("dwStyles", wintypes.DWORD),
        ("hwnd", wintypes.HWND),
    ]


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _title_of(hwnd) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def hide_qq_console() -> int:
    """把所有属于 QQ 的控制台窗口藏掉。返回藏了几个。

    认法：类名是 ConsoleWindowClass，标题里带 QQ.exe。
    只认标题带 QQ.exe 的，不碰别的控制台——
    用户自己开的 cmd、别的程序的窗口都别动。
    """
    targets = []

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _class_of(hwnd) not in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
            return True
        title = _title_of(hwnd)
        if "QQ.exe" in title.lower() or "QQ.exe" in title:
            targets.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)

    for h in targets:
        user32.ShowWindow(h, SW_HIDE)

    return len(targets)


def watch_and_hide(duration: float = 30.0, interval: float = 0.8) -> int:
    """盯着一段时间，见一个藏一个。

    QQ 启动过程中可能重建几次控制台窗口，所以不能只藏一次。
    duration 用完就收工，不留常驻线程。
    """
    total = 0
    deadline = time.time() + duration
    while time.time() < deadline:
        total += hide_qq_console()
        time.sleep(interval)
    return total

def hide_bot_console() -> int:
    """把 AstrBot 那个控制台藏掉。

    AstrBot 走的是 astrbot.exe，它自己无窗口，但会把真正的
    Python 进程拉起来，那个 Python 进程会带控制台。
    实测：用 DETACHED_PROCESS 启动还是会闪一下。

    认法比 QQ 复杂一点：标题是 "qqaibot-AstrBot"（从 start_astrbot.bat
    继承来的 title），或者标题里带 astrbot / python。
    但也可能没有标题，所以额外用一个特征：窗口大小很小（控制台默认尺寸），
    并且本机没有其他可能来源。

    为了避免误伤用户自己开的 cmd，只藏标题明确匹配的。
    """
    targets = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if _class_of(hwnd) not in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
            return True
        title = _title_of(hwnd).lower()
        if ("astrbot" in title) or ("qqaibot" in title):
            targets.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    for h in targets:
        user32.ShowWindow(h, SW_HIDE)
    return len(targets)


def watch_bot_hidden(duration: float = 25.0, interval: float = 0.5) -> int:
    total = 0
    deadline = time.time() + duration
    while time.time() < deadline:
        total += hide_bot_console()
        time.sleep(interval)
    return total
