"""任务端的权限级别。

老爸的设计
    默认**工作区权限**：只能在它自己的任务目录里干活。
    要碰工作区外面，得单独给那个窗口提权。

为什么默认要收
    任务端有完全权限是必须的（有些活就是要改 D:\\项目\\xxx）。
    但"默认全开"等于：一次成功的诱导 = 整个硬盘。

    "默认最小 + 按需提权"的差别在哪：
        攻击者要造成伤害，得先让用户同意提权。
        而提权是个明确动作，用户看得见。

三级
    workspace   只能在工作目录里（默认）
    home        能碰用户目录（文档、桌面这些）
    full        什么都能碰

    为什么不直接两档（工作区 / 全部）：
        中间这档很实用——"帮我整理桌面上的文件"不需要全盘权限，
        但工作区权限确实不够。
"""

from __future__ import annotations

import json
import os

LEVELS = ("workspace", "home", "full")

LEVEL_LABEL = {
    "workspace": "工作区",
    "home": "用户目录",
    "full": "完全",
}

LEVEL_HINT = {
    "workspace": "只能动它自己那个任务目录里的文件",
    "home": "能动你用户目录下的东西（文档、桌面、下载）",
    "full": "整个电脑都能动",
}


def _cfg_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "dsh-bridge", "task_perms.json")


def load() -> dict:
    """读每个窗口的权限设置。

    格式：
        {"别名的权限": "workspace", "另一个": "full"}

    没设置的按默认（workspace）。
    """
    p = _cfg_path()
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(d: dict) -> None:
    p = _cfg_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass


def get_level(name: str) -> str:
    """这个窗口是什么权限。没设置过就是 workspace。"""
    lv = (load().get(name) or "").strip().lower()
    return lv if lv in LEVELS else "workspace"


def set_level(name: str, level: str) -> bool:
    """给一个窗口换权限。"""
    lv = (level or "").strip().lower()
    if lv not in LEVELS:
        return False
    d = load()
    if lv == "workspace":
        # 默认值不存，保持配置干净
        d.pop(name, None)
    else:
        d[name] = lv
    save(d)
    return True


def allowed_roots(level: str, workdir: str) -> list[str]:
    """这个权限级别下，能碰哪些根目录。

    ⚠️ 这里返回的是"允许的最上层"。
        具体判断交给 is_allowed()。
    """
    roots: list[str] = []

    if workdir:
        roots.append(workdir)

    if level in ("home", "full"):
        roots.append(os.path.expanduser("~"))

    if level == "full":
        # 全盘：用根路径覆盖一切
        if os.name == "nt":
            for drive in "CDEFGHIJ":
                p = f"{drive}:\\"
                if os.path.exists(p):
                    roots.append(p)
        else:
            roots.append("/")

    return roots


def is_allowed(path: str, level: str, workdir: str) -> bool:
    """这个路径在当前权限下能不能碰。"""
    if not path:
        return False

    try:
        target = os.path.normcase(os.path.abspath(path))
    except (OSError, ValueError):
        return False

    for root in allowed_roots(level, workdir):
        try:
            r = os.path.normcase(os.path.abspath(root))
        except (OSError, ValueError):
            continue

        # ⚠️ 根目录本身已经带分隔符了（"c:\\"），不能再补一个。
        #
        #    原来写成 r + os.sep，于是根目录变成 "c:\\"（两个反斜杠），
        #    而正常路径是 "c:\\windows\\..."（一个反斜杠），
        #    永远匹配不上 —— **完全权限那一档整个失效了**。
        #
        #    实测：设成 full 之后，连 C 盘都碰不到。
        if not r.endswith(os.sep):
            r = r + os.sep

        if target == r.rstrip(os.sep) or target.startswith(r):
            return True

    return False


def explain_denied(path: str, level: str) -> str:
    """被权限挡住时说什么。"""
    label = LEVEL_LABEL.get(level, level)
    hint = LEVEL_HINT.get(level, "")
    return (
        f"[权限不够] 这个文件不在「{label}」权限能碰的范围内：{path}\n"
        f"当前权限：{label}——{hint}\n"
        f"要碰它的话，需要把这个窗口的权限提一档。"
    )
