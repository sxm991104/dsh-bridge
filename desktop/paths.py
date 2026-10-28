"""程序装在哪、配置存在哪、怎么自己找出来。

为什么需要这一层
    原来路径是写死的：
        D:\\qqaibot\\napcat\\v436
        C:\\Users\\19836\\.local\\bin\\astrbot.exe
    那台机器上能用，别人拿去就跑不起来——他没有 D 盘，
    也不是叫 19836 这个用户名。

    所以路径要满足三件事：
        1. 能自己探测出来（大部分情况用户不用管）
        2. 探测不到让用户自己选（不能因为找不到就罢工）
        3. 选过一次就记住（下次不用再选）

配置存在哪
    %APPDATA%\\dsh-bridge\\config.json

    不放在程序目录：打包成 exe 之后程序目录可能是只读的
    （Program Files 下面），写不进去。
    %APPDATA% 是 Windows 给每个用户放配置的地方，
    无论程序装在哪都写得进去。
"""

from __future__ import annotations

import json
import os
import sys

APP_NAME = "dsh-bridge"


def app_dir() -> str:
    """程序所在目录（源码运行时是脚本目录，打包后是 exe 旁边）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


# ----------------------------------------------------------------------
# 读 / 写
# ----------------------------------------------------------------------

DEFAULTS = {
    "qq_exe": "",
    "napcat_dir": "",
    "astrbot_exe": "",
    "astrbot_pyw": "",
    "astrbot_cwd": "",
}


def load() -> dict:
    """读配置。缺的字段用默认值补上。"""
    data = dict(DEFAULTS)
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in DEFAULTS:
                    if isinstance(disk.get(k), str):
                        data[k] = disk[k]
        except (OSError, json.JSONDecodeError):
            pass
    return data


def save(data: dict) -> None:
    """写配置。先写临时文件再换，避免写一半断电留下坏文件。"""
    p = config_path()
    tmp = p + ".tmp"
    clean = {k: (data.get(k) or "") if isinstance(data.get(k), str) else ""
             for k in DEFAULTS}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def get(key: str) -> str:
    return load().get(key, "") or ""


def set_value(key: str, value: str) -> None:
    data = load()
    data[key] = value or ""
    save(data)


# ----------------------------------------------------------------------
# 自动探测
# ----------------------------------------------------------------------

def detect_qq() -> str:
    """找 QQ.exe。先查注册表，再查几个常见位置。"""
    # 1) 注册表（NapCat 的 bat 也是这么找的）
    try:
        import winreg
    except ImportError:
        winreg = None

    if winreg is not None:
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
            folder = os.path.dirname(str(raw).strip().strip('"'))
            exe = os.path.join(folder, "QQ.exe")
            if os.path.exists(exe):
                return exe

    # 2) 常见安装位置
    guesses = [
        r"C:\Program Files\QQ.exe",
        r"C:\Program Files (x86)\QQ.exe",
        r"C:\Program Files\Tencent\QQNT\QQ.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\QQ\QQ.exe"),
        os.path.expandvars(r"%ProgramFiles%\Tencent\QQNT\QQ.exe"),
    ]
    for g in guesses:
        if os.path.exists(g):
            return g
    return ""


def detect_napcat() -> str:
    """找 NapCat 目录。认它里面必须有 NapCatWinBootMain.exe。"""
    roots = [
        r"D:\qqaibot\napcat",
        r"C:\qqaibot\napcat",
        os.path.expandvars(r"%USERPROFILE%\qqaibot\napcat"),
        os.path.expandvars(r"%LOCALAPPDATA%\qqaibot\napcat"),
        os.path.expandvars(r"%APPDATA%\qqaibot\napcat"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        # 直接就是，或者下面有一层版本目录（v436 这种）
        if os.path.exists(os.path.join(root, "NapCatWinBootMain.exe")):
            return root
        try:
            for name in os.listdir(root):
                sub = os.path.join(root, name)
                if os.path.isdir(sub) and os.path.exists(
                    os.path.join(sub, "NapCatWinBootMain.exe")
                ):
                    return sub
        except OSError:
            continue
    return ""


def detect_astrbot() -> tuple[str, str, str]:
    """找 AstrBot。返回 (exe, pythonw, 工作目录)。"""

    def _pair(exe: str) -> tuple[str, str, str]:
        """给定 astrbot.exe，找同环境的 pythonw 和它的数据目录。"""
        # astrbot.exe 通常在 <venv>\Scripts\ 或 ~/.local/bin
        folder = os.path.dirname(exe)
        pyw = ""
        # pythonw 不一定在 astrbot.exe 旁边：
        # astrbot.exe 常在 %USERPROFILE%\.local\bin\，
        # 而它的 Python 环境可能在 uv 的工具目录里。
        cands = [
            os.path.join(os.path.dirname(folder), "Scripts", "pythonw.exe"),
            os.path.join(folder, "pythonw.exe"),
            os.path.expandvars(
                r"%APPDATA%\uv\tools\astrbot\Scripts\pythonw.exe"
            ),
        ]
        for cand in cands:
            if cand and os.path.exists(cand):
                pyw = cand
                break
        cwd = ""
        for cand in (r"D:\qqaibot\astrbot", r"C:\qqaibot\astrbot"):
            if os.path.isdir(cand):
                cwd = cand
                break
        return exe, pyw, cwd

    guesses = [
        os.path.expandvars(r"%USERPROFILE%\.local\bin\astrbot.exe"),
        # 用户名不能写死——换台机器就不对了，所以只用环境变量拼
        r"D:\qqaibot\.bot_runtime\astrbot.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\astrbot\astrbot.exe"),
    ]
    for g in guesses:
        if os.path.exists(g):
            return _pair(g)
    return "", "", ""


def detect_all() -> dict:
    """一次把所有能探测到的都探出来。探测不到的留空，由用户自己选。"""
    qq = detect_qq()
    nap = detect_napcat()
    ae, pyw, cwd = detect_astrbot()
    return {
        "qq_exe": qq,
        "napcat_dir": nap,
        "astrbot_exe": ae,
        "astrbot_pyw": pyw,
        "astrbot_cwd": cwd,
    }


def merge_detected(into: dict | None = None) -> dict:
    """在已有配置上补齐探测到的值。已有的非空值不覆盖。"""
    data = dict(into) if into else load()
    found = detect_all()
    for k, v in found.items():
        if not (data.get(k) or "") and v:
            data[k] = v
    return data


def missing(cfg: dict) -> list[str]:
    """哪些关键配置还缺着。启动服务前要先补齐。"""
    needs = []
    if not cfg.get("qq_exe") or not os.path.exists(cfg["qq_exe"]):
        needs.append("qq_exe")
    if not cfg.get("napcat_dir") or not os.path.exists(cfg["napcat_dir"]):
        needs.append("napcat_dir")
    if not cfg.get("astrbot_exe") or not os.path.exists(cfg["astrbot_exe"]):
        needs.append("astrbot_exe")
    return needs


LABELS = {
    "qq_exe": "QQ 程序位置",
    "napcat_dir": "NapCat 目录",
    "astrbot_exe": "AstrBot 程序位置",
    "astrbot_pyw": "Python 无窗口解释器",
    "astrbot_cwd": "AstrBot 数据目录",
}

# ----------------------------------------------------------------------
# 开机自启
# ----------------------------------------------------------------------
#
# 做法：在 Startup 目录放一个指向本程序的快捷方式。
#
# 为什么不用注册表 Run 键：
#     Startup 目录里的东西用户在"任务管理器 → 启动"里能看到、
#     能直接禁掉，出问题好收拾。写注册表虽然也行，但用户
#     不好找、不好关，卸载时也容易留下。
#
# 打包成 exe 和源码运行两种情况分开处理：
#     exe：直接指向 exe 文件
#     源码：指向 pythonw.exe，参数是脚本路径（pythonw 不出黑框）

LNK_NAME = "dsh-bridge.lnk"


def startup_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(
        base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )


def lnk_path() -> str:
    return os.path.join(startup_dir(), LNK_NAME)


def is_autostart() -> bool:
    return os.path.exists(lnk_path())


def set_autostart(on: bool) -> tuple[bool, str]:
    """开关开机自启。返回 (成功, 说明)。"""
    target_lnk = lnk_path()

    if not on:
        if os.path.exists(target_lnk):
            try:
                os.remove(target_lnk)
            except OSError as e:
                return False, f"删不掉：{e}"
        return True, "已关闭开机自启。"

    # 打开：先确定要指向什么
    if getattr(sys, "frozen", False):
        exe = sys.executable
        args = ""
        workdir = os.path.dirname(exe)
    else:
        # 源码运行：用 pythonw 跑 desktop_qt.py，避免开个黑框
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pyw):
            pyw = sys.executable
        exe = pyw
        args = f'"{os.path.join(app_dir(), "desktop_qt.py")}"'
        workdir = app_dir()

    try:
        import subprocess

        ps = (
            f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{target_lnk}');"
            f"$s.TargetPath='{exe}';"
            f"$s.Arguments='{args}';"
            f"$s.WorkingDirectory='{workdir}';"
            f"$s.Save()"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            return False, "建快捷方式失败"
    except Exception as e:  # noqa: BLE001
        return False, f"建快捷方式出错：{e}"

    if os.path.exists(target_lnk):
        return True, "已设置开机自启。"
    return False, "快捷方式没建成"
