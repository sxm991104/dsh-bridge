"""平台差异，集中在这一个文件里。

为什么要有这个文件
    这个项目最早只在 Windows 上跑。后来加了 CI（ubuntu-latest），
    等于公开承诺支持 Linux，那就得真的能跑。

    问题在于：Windows 相关的代码散落在十几个文件里，
    每个地方各写一句 `if sys.platform == "win32"`。
    那样改起来容易漏，而且以后加 macOS 还得再找一遍。

    所以把平台判断收拢在这里。别的地方要用，从这里拿。

两类差异
    一、能力差异（文件权限、进程查询、无窗口启动）
        这些是"同一个动作，两个平台上做法不同"。

    二、路径语义差异（盘符、反斜杠、设备名）
        这些是"同一个字符串，两个平台上意思不同"。
        最麻烦的一类是：Windows 路径在 Linux 上看就是个普通文件名。

    ⚠️ 路径那一类已经单独处理过了（action_guard.is_outside 里
       按"这个路径本身是不是绝对形态"判断，而不是按当前平台）。
       这个文件的职责是第一条。
"""

from __future__ import annotations

import os
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
IS_MACOS = sys.platform == "darwin"


# ══════════════════════════════════════════════════════
# 一、启动子进程
# ══════════════════════════════════════════════════════

def no_window_flags() -> int:
    """启动子进程时不弹黑窗口。

    Windows 上需要 CREATE_NO_WINDOW，别的平台没有这个概念。

    ⚠️ 这个常量在 Windows 上是 0x08000000。但别直接把数字抄到
       subprocess.run 里——在某几个调用点上它是显式传的，
       Linux 上传一个 0 没关系，传一个非 0 会被误当成别的标志位。
    """
    return 0x08000000 if IS_WINDOWS else 0


# ══════════════════════════════════════════════════════
# 二、文件权限
# ══════════════════════════════════════════════════════

def tighten_file(path: str) -> None:
    """把文件权限收到只有当前用户能读写。

    Windows    icacls 去掉继承，授予当前用户完全控制
    Linux/mac  chmod 600

    失败就算了——这个函数是加固，不是功能。加不上不影响到主流程，
    但不能让它抛异常把主流程打断。
    """
    if not path:
        return

    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return

    try:
        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True,
            timeout=10,
            creationflags=no_window_flags(),
        )
    except Exception:  # noqa: BLE001
        pass


# ══════════════════════════════════════════════════════
# 三、查进程
# ══════════════════════════════════════════════════════

def process_running(name: str) -> bool:
    """这个进程在不在。

    Windows    tasklist /FI "IMAGENAME eq xxx.exe"
    Linux      pgrep -x xxx
    macOS      pgrep -x xxx

    name 传不带 .exe 的名字，比如 "QQ"。
    """
    if not name:
        return False

    try:
        if IS_WINDOWS:
            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"],
                capture_output=True, text=True, timeout=10,
                encoding="utf-8", errors="replace",
                creationflags=no_window_flags(),
            )
            out = (r.stdout or "").lower()
            # 没找到时 tasklist 会输出 "信息: 没有运行的任务..."
            return f"{name.lower()}.exe" in out
        else:
            r = subprocess.run(
                ["pgrep", "-x", name],
                capture_output=True, text=True, timeout=10,
            )
            return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ══════════════════════════════════════════════════════
# 四、路径
# ══════════════════════════════════════════════════════

# Windows 上不能做文件名的东西（设备名、保留字符）。
# Linux 上这些都能用，所以判断要按平台来。
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def is_reserved_name(name: str) -> bool:
    """这个名字在**当前平台**上是不是不能用。

    ⚠️ 注意"当前平台"这三个字。

        之前 safe_path.py 里把 CON/NUL 这类判成一律非法。
        在 Windows 上对，在 Linux 上过度严格——Linux 上 "con" 是个
        完全正常的文件名。

        但反过来想：我们要防的是"用户派来的活在 Windows 上出事"。
        这个项目的任务端主要跑 Windows，所以宁可严一点。

        所以这个函数保留 Windows 的严格判断，**两个平台都拒**。
        理由是安全优先级高于"在 Linux 上也能用奇怪的名字"。
    """
    base = (name or "").split(".")[0].strip().lower()
    return base in _WIN_RESERVED


def path_sep() -> str:
    return os.sep


def looks_like_windows_path(p: str) -> bool:
    """看着是不是个 Windows 风格路径。

    用来判断"这条路径在哪个平台上会生效"。
    D:/x、C:\\x、\\\\server\\share 都算。
    """
    if not p:
        return False
    s = p.replace("\\", "/")
    # 盘符
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        return True
    # UNC
    return s.startswith("//")


# ══════════════════════════════════════════════════════
# 五、给测试用的说明
# ══════════════════════════════════════════════════════

def describe() -> str:
    """当前平台的一句话描述，写进日志用。"""
    return f"{sys.platform} / Python {sys.version_info.major}.{sys.version_info.minor}"
