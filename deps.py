"""运行环境检查与依赖安装。

为什么要有这个
    别人 clone 下来，跑不起来的原因往往不是代码有问题，
    而是缺东西：没装 PySide6、没装 python-docx、Python 版本太老。

    让人去翻文档、敲 pip install 是很劝退的。
    所以把这个做进桌面端：
        看一眼缺什么 → 点一下 → 自动装 → 进度条走给你看

设计上的两个决定
    一、只检查不强制
        缺 python-docx 只是"读不了 Word"，桥本身照样能跑。
        所以标成"可选"，不能因为缺它就拦着用户用。

    二、安装走 pip，不自己下载 whl
        pip 处理依赖、轮子、平台匹配都在行。
        自己拼 URL 下 whl 等于重造一遍，还容易出错。
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import sysconfig
import threading
import urllib.error
import urllib.request

# ── 依赖清单 ────────────────────────────────────────────
#
# 每一条：
#   key       内部名字
#   label     显示给人看的名字
#   module    导入时用的模块名（判断装没装）
#   pip       pip 装的时候用的包名
#   why       为什么要它
#   needed    True = 没有就跑不起来；False = 没有只是少个功能

DEPS = [
    {
        "key": "python",
        "label": "Python",
        "module": "",
        "pip": "",
        "why": "整个项目的基础",
        "needed": True,
        "is_python": True,
    },
    {
        "key": "pyside6",
        "label": "PySide6",
        "module": "PySide6",
        "pip": "PySide6",
        "why": "桌面端的界面",
        "needed": True,
    },
    {
        "key": "docx",
        "label": "python-docx",
        "module": "docx",
        "pip": "python-docx",
        "why": "读 Word 文档",
        "needed": False,
    },
    {
        "key": "openpyxl",
        "label": "openpyxl",
        "module": "openpyxl",
        "pip": "openpyxl",
        "why": "读 Excel 表格",
        "needed": False,
    },
    {
        "key": "pptx",
        "label": "python-pptx",
        "module": "pptx",
        "pip": "python-pptx",
        "why": "读 PPT",
        "needed": False,
    },
    {
        "key": "pypdf",
        "label": "pypdf",
        "module": "pypdf",
        "pip": "pypdf",
        "why": "读 PDF",
        "needed": False,
    },
]

MIN_PYTHON = (3, 10)


# ── 检查 ────────────────────────────────────────────────

def _module_installed(name: str) -> tuple[bool, str]:
    """这个模块能导入吗。返回 (装没装, 版本号)。"""
    if not name:
        return False, ""
    try:
        m = importlib.import_module(name)
        ver = getattr(m, "__version__", "") or getattr(m, "VERSION", "")
        if isinstance(ver, tuple):
            ver = ".".join(str(x) for x in ver)
        return True, str(ver)
    except ImportError:
        return False, ""
    except Exception:  # noqa: BLE001
        # 装是装了，但导入时炸了（通常是依赖缺或者 ABI 不匹配）
        return False, "装错了"


def check_python() -> dict:
    """Python 版本够不够。"""
    v = sys.version_info
    ok = (v.major, v.minor) >= MIN_PYTHON
    return {
        "key": "python",
        "label": "Python",
        "installed": ok,
        "version": f"{v.major}.{v.minor}.{v.micro}",
        "why": f"需要 {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 以上",
        "needed": True,
        "can_install": False,        # Python 没法从桌面端装
        "note": "" if ok else "版本太老，请去 python.org 装新的",
    }


def check_all() -> list[dict]:
    """把清单里每一项都查一遍。"""
    out = [check_python()]

    for d in DEPS:
        if d.get("is_python"):
            continue
        ok, ver = _module_installed(d["module"])
        out.append({
            "key": d["key"],
            "label": d["label"],
            "installed": ok,
            "version": ver,
            "why": d["why"],
            "needed": d["needed"],
            "pip": d["pip"],
            "can_install": True,
            "note": "",
        })

    # AstrBot 是单独的软件，检查一下在不在常见位置
    out.append(_check_astrbot())
    return out


def _check_astrbot() -> dict:
    """找 AstrBot 装在哪。

    它不一定跟我们同一个 Python 环境，所以不能 import。
    查几个常见位置就够了。
    """
    candidates = [
        r"D:\qqaibot\astrbot",
        os.path.expanduser(r"~\astrbot"),
        r"C:\astrbot",
    ]

    for p in candidates:
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "main.py")):
            return {
                "key": "astrbot",
                "label": "AstrBot",
                "installed": True,
                "version": "",
                "why": "前台（接 QQ / 飞书）",
                "needed": False,
                "can_install": False,
                "note": p,
            }

    return {
        "key": "astrbot",
        "label": "AstrBot",
        "installed": False,
        "version": "",
        "why": "前台（接 QQ / 飞书）",
        "needed": False,
        "can_install": False,
        "note": "要接聊天平台才需要，单独安装",
    }


def missing(items: list[dict]) -> list[dict]:
    """缺哪些、而且能从桌面端装的。"""
    return [i for i in items if not i["installed"] and i.get("can_install")]


# ── 安装 ────────────────────────────────────────────────

class Installer:
    """装 pip 包，带进度回调。

    用 pip 的子进程，边读输出边解析进度。
    为什么不用 pip 的 API：
        pip 没有稳定的公开 API，直接 import pip 干活
        在官方眼里是"不支持的用法"，换版本就可能坏。
        当子进程调是它支持的用法。
    """

    def __init__(self, on_progress=None, on_done=None):
        self.on_progress = on_progress or (lambda msg, pct: None)
        self.on_done = on_done or (lambda ok, msg: None)
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def install(self, packages: list[str]) -> tuple[bool, str]:
        """装一批包。阻塞调用，进度通过回调出去。"""
        if not packages:
            return True, "没有要装的"

        cmd = [
            sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check",
            "--no-input",
            *packages,
        ]

        self.on_progress(f"开始安装：{', '.join(packages)}", 0)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=(
                    0x08000000 if sys.platform == "win32" else 0
                ),
            )
        except OSError as e:
            return False, f"起不来：{e}"

        lines: list[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            if self._cancelled:
                try:
                    proc.terminate()
                except OSError:
                    pass
                return False, "用户取消了"

            line = raw.rstrip("\r\n")
            if not line:
                continue
            lines.append(line)

            # 从 pip 的输出里挖进度
            pct = _parse_progress(line)
            if pct is not None:
                self.on_progress(line.strip(), pct)
            elif "Downloading" in line or "Collecting" in line:
                self.on_progress(line.strip(), -1)   # -1 表示"在动但不知道到哪"

            if len(lines) > 2000:
                lines = lines[-1000:]

        rc = proc.wait()

        if rc == 0:
            self.on_progress("装好了", 100)
            return True, "装好了"

        detail = "\n".join(lines[-6:])
        return False, f"没装上（退出码 {rc}）\n{detail}"


def _parse_progress(line: str) -> int | None:
    """从 pip 的下载行里挖出百分比。

    pip 的输出长这样：
        Downloading openpyxl-3.1.5-py3-none-any.whl (250 kB)
        ━━━━━━━━━━━━━━━━━━━━━━━━━━ 250.1/250.1 kB 1.2 MB/s eta 0:00:00
    """
    import re

    # 进度条那一行。
    #
    # pip 实际输出长这样（注意单位只写在末尾一次）：
    #     ━━━━━━ 250.1/250.1 kB 1.2 MB/s eta 0:00:00
    #
    # 我第一版的正则要求两边都带单位，于是这种写法一个都匹配不上。
    # 现在两种都认。
    unit = {"kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}

    # 形式一：250.1/250.1 kB
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*(kB|MB|GB)", line
    )
    if m:
        a, b, u = m.groups()
        try:
            total = float(b) * unit[u]
            if total > 0:
                return max(0, min(99, int(float(a) * unit[u] / total * 100)))
        except (ValueError, KeyError):
            pass

    # 形式二：250.1 kB/250.1 kB（两边都带单位）
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(kB|MB|GB)\s*/\s*(\d+(?:\.\d+)?)\s*(kB|MB|GB)",
        line,
    )
    if m:
        a, ua, b, ub = m.groups()
        try:
            total = float(b) * unit[ub]
            if total > 0:
                return max(0, min(99, int(float(a) * unit[ua] / total * 100)))
        except (ValueError, KeyError):
            pass

    # 有的版本直接带百分比
    m = re.search(r"(\d{1,3})%", line)
    if m:
        try:
            return max(0, min(99, int(m.group(1))))
        except ValueError:
            pass

    return None


def install_async(packages: list[str], on_progress, on_done) -> Installer:
    """在后台线程里装，不卡界面。"""
    inst = Installer(on_progress=on_progress, on_done=on_done)

    def _run():
        ok, msg = inst.install(packages)
        on_done(ok, msg)

    threading.Thread(target=_run, daemon=True, name="dep-install").start()
    return inst


# ── 环境信息（给界面显示）────────────────────────────────

def env_info() -> dict:
    """当前 Python 环境的关键信息。"""
    return {
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": sysconfig.get_platform(),
        "prefix": sys.prefix,
    }
