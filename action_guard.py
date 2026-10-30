"""动作级的守卫：看它**做什么**，不看它**说什么**。

为什么换了判断方式
    以前是"读任务描述，看有没有危险词"。
    这条路走不通，原因不在词库不全——是这个判断方式本身有问题：

        · 危险的说法列不完（"删"有很多种说法，"改权限"就没想到）
        · 任务描述会骗人（有人让你派活，描述里写得很无害）

    现在改成：**看它实际调用了什么工具、传了什么参数。**

        任务描述   →   可以撒谎
        工具调用   →   做不了假（真删了文件，文件就没了）

判断标准
    不看"危不危险"（程度问题，没法穷举），
    看"**能不能撤销**"（这是非黑即白的）：

        能撤销的          → 放行
        不能撤销的        → 拦

    为什么这个标准够用：
        攻击者要造成伤害，必须做不可逆的事——
        删掉文件、改了权限、覆盖了历史、把东西发出去。
        这些撤销不了，所以一定是"不可逆动作"，一定被拦。

        而正常工作基本都能重来：
        写文件（删了重写就行）、改代码（git 能回滚）、
        跑测试（再跑一次）。

不可逆动作清单
    这一份是穷举不了的（跟"危险词"是同一类问题），
    但跟危险词的区别在于：**它是动作，数量有限且稳定。**

    删文件、改权限、删目录、强制推、硬重置、格式化、
    改系统设置、把数据发到外面——真正能造成不可逆后果的动作，
    就这么几类。它们不会每天长出新的。
"""

from __future__ import annotations

import os
import re

# ══════════════════════════════════════════════════════
# 不可逆动作
# ══════════════════════════════════════════════════════

# 每个条目：
#   kind     类型（用来给用户解释）
#   why      为什么不可逆
#   test     一个接受小写命令字符串的函数，命中返回 True

_IRREVERSIBLE = [
    {
        "kind": "删文件",
        "why": "删掉的东西找不回来",
        "test": lambda c: bool(re.search(
            r"\b(rm|del|erase|unlink)\b|remove-item", c
        )),
    },
    {
        "kind": "删目录",
        "why": "整个目录没了，里面的东西一起没",
        "test": lambda c: bool(re.search(
            r"\b(rmdir|rd|del /s|rm -r)\b|remove-item.*-recurse", c
        )),
    },
    {
        "kind": "改权限",
        "why": "改完不知道原来是什么，回不去",
        "test": lambda c: bool(re.search(
            r"\b(chmod|chown|icacls|takeown|cacls|attrib)\b|set-acl", c
        )),
    },
    {
        "kind": "强制推送 / 硬重置",
        "why": "远端历史被覆盖，别人的工作可能没了",
        "test": lambda c: bool(re.search(
            r"git\s+push\s+.*(-f\b|--force)|"
            r"git\s+reset\s+--hard|"
            r"git\s+clean\s+-[a-z]*f|"
            r"git\s+branch\s+-d\b|"
            r"git\s+filter-branch|"
            r"git\s+reflog\s+expire",
            c,
        )),
    },
    {
        "kind": "格式化 / 分区",
        "why": "盘上的东西全没了",
        "test": lambda c: bool(re.search(
            r"\b(format|diskpart|mkfs|fdisk|parted)\b", c
        )),
    },
    {
        "kind": "改系统设置",
        "why": "改坏系统进不去，重装才行",
        "test": lambda c: bool(re.search(
            r"\breg\s+(add|delete)|"
            r"\bsetx\b|"
            r"\bschtasks\b|"
            r"\bsc\s+(create|delete|config)|"
            r"new-service|set-service|"
            r"\bbcdedit\b",
            c,
        )),
    },
    {
        "kind": "清空 / 覆盖数据",
        "why": "数据被覆盖，原内容无法恢复",
        "test": lambda c: bool(re.search(
            r"\b(drop\s+(table|database)|truncate|delete\s+from)\b|"
            r">\s*/dev/sd|"
            r"foreach.*remove-item.*-force",
            c,
        )),
    },
    {
        "kind": "把数据发出去",
        "why": "发出去就收不回来了",
        "test": lambda c: bool(re.search(
            r"\b(curl|wget|invoke-webrequest|invoke-restmethod)\b.*"
            r"(-d\b|-f\b|--data|--upload-file|-t\b)|"
            r"\b(scp|rsync|ftp|sftp)\b",
            c,
        )),
    },
]

# 这些路径碰了也是不可逆的——动的是我们的安全配置
PROTECTED_HINTS = [
    "guard_count",
    ".syscache",
    ".guard_key",
    ".initialized",
    "guard.json",
    "api_token",
]


def describe(command: str) -> list[dict]:
    """一条命令里命中哪些不可逆动作。"""
    c = (command or "").lower()
    if not c:
        return []

    hits = []
    for rule in _IRREVERSIBLE:
        try:
            if rule["test"](c):
                hits.append({"kind": rule["kind"], "why": rule["why"]})
        except Exception:  # noqa: BLE001
            continue
    return hits


def touches_protected(command: str) -> bool:
    """命令里提到了我们自己的安全文件吗。"""
    c = (command or "").lower()
    return any(h in c for h in PROTECTED_HINTS)


# ══════════════════════════════════════════════════════
# 越界写 —— 另一类不可逆
# ══════════════════════════════════════════════════════

def is_outside(path: str, workdir: str) -> bool:
    """这个路径在任务工作目录外面吗。

    写工作目录外面不算"不可逆"（用户可能就是想改自己别的项目），
    但**应该被告知**——因为那是他的真实文件，不是任务产物。
    """
    if not path or not workdir:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        w = os.path.normcase(os.path.abspath(workdir))
        return not (p == w or p.startswith(w + os.sep))
    except (OSError, ValueError):
        return False


# ══════════════════════════════════════════════════════
# 给用户的话
# ══════════════════════════════════════════════════════

def explain_two_step(hit: dict) -> str:
    """不可逆动作拦下来时，告诉用户"再做一次会怎样"。

    这是老爸说的"两步确认"：

        第一次  拦下，问"真要这样吗？"
        第二次  你确认了，那就真做

    因为不可逆动作**有时候真的需要做**（清理垃圾、重装依赖），
    一刀切禁掉就没法用了。
    """
    kind = hit.get("kind", "这个操作")
    why = hit.get("why", "")
    return (
        f"这一步是「{kind}」——{why}。\n"
        f"我先没做。真要这么做的话，再跟我说一次，我这次不拦。"
    )


def explain_locked() -> str:
    """已经真正开始执行、不可撤销的时候。"""
    return (
        "这个操作已经开始执行，撤销不了了。\n"
        "如果这不是你的本意，现在停下来还来得及——但已经做掉的部分"
        "需要你自己检查一下。"
    )
