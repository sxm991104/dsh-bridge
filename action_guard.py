"""动作守卫：管"执行口"，不管"命令长什么样"。

来历

    第一版：读任务描述，看有没有危险词。→ 词列不完，而且描述会骗人。

    第二版：看工具调用实际做了什么，按"能不能撤销"判断。→ 方向对了，
            但实现是靠词表匹配命令字符串。

    千问实测打了 14 条载荷，第二版只拦住 5 条。他说的对：

        围栏的边界是词表，而词表管不住一个图灵完备的执行入口。

    我复现了，一模一样：

        python -c "import shutil; shutil.rmtree('D:/Project')"   ← 放过
        node -e "require('fs').rmSync(...)"                      ← 放过
        powershell -enc ...                                      ← 放过
        certutil ... && x.exe                                    ← 放过
        echo. > D:/important.txt                                 ← 放过
        move D:/Project D:/_trash_xyz                            ← 放过

现方案（第三版）

    不再问"这条命令危不危险"。问：

        **这条命令能不能被任意用来干任何事？**

    能执行任意代码的入口（python -c / node -e / powershell / 再套一层
    shell / 下载后执行），必然能做任何不可逆的事——包括我们想不到的。
    所以这种命令**根本不该在工作区权限下跑**。

    这个判断跟"命令写了什么"无关，只跟"入口的表达能力"有关。
    而表达能力是可枚举的——就那几类解释器。这跟词表有本质区别：
    词表要枚举"危险行为"（无限），这里枚举"执行入口"（有限且稳定）。

    第二层还留着不可逆动作词表，挡"直接写着"的破坏。
    两层互补：先把任意执行的口子堵上，再挡明写的破坏。
"""

from __future__ import annotations

import os
import re

# ══════════════════════════════════════════════════════
# 一、能执行任意代码的入口
# ══════════════════════════════════════════════════════

_ARBITRARY_EXEC = [
    # ⚠️ 这里的匹配要容忍四种写法（暴力测试打出来的）：
    #
    #     1. 带路径     C:\Python\python.exe -c "..."
    #        → 不能只匹配行首的 python，要在任意位置找到 "\python[数字]?.exe"
    #     2. 参数无空格  python -c1
    #        → 不能要求 -c 后面必须有空格
    #     3. -m 跟模块   python -m os
    #        → 带 -m 就跑任意模块，一律拦（-m pytest 这类日常用法
    #          也牺牲掉——写进异常清单里，见下面的 ALLOW）
    #     4. 变量拼接    pow%x%ershell
    #        → 见下面的 EXPAND 逻辑

    # 解释器 inline 执行
    (r"(python[0-9.]*|py)(\.exe)?\s+-c", "Python 内联执行"),
    (r"(python[0-9.]*|py)(\.exe)?\s+-m\s", "Python 跑模块"),
    (r"node(\.exe)?\s+(-e|--eval)", "Node 内联执行"),
    (r"(ruby|perl|php|lua)(\.exe)?\s+(-e|--eval)", "其它解释器内联执行"),
    (r"(deno|bun)(\.exe)?\s+(run|eval)", "Deno/Bun 执行"),

    # PowerShell
    (r"power(shell)?(\.exe)?", "PowerShell"),
    (r"\bpwsh(\.exe)?", "PowerShell 7"),
    (r"\b(iex|invoke-expression)\b", "PowerShell 动态执行"),
    (r"-enc(odedcommand)?\s+[A-Za-z0-9+/=]{20,}", "编码执行"),

    # 再套一层 shell
    (r"cmd(\.exe)?\s+/c", "再套一层 cmd"),
    (r"\b(bash|sh|zsh)(\.exe)?\s+-c\b", "再套一层 sh"),

    # 下载 + 跑
    (r"certutil.*-urlcache", "certutil 下载"),
    (r"bitsadmin.*transfer", "bitsadmin 下载"),
    (r"(invoke-webrequest|iwr).*\|\s*(iex|sh|bash|python)", "下载后直接执行"),
    (r"\bmshta\b", "mshta 执行"),
    (r"\brundll32\b", "rundll32 执行"),
    (r"\bregsvr32\b", "regsvr32 执行"),
    (r"\b(wscript|cscript)\b", "Windows 脚本宿主"),

    # 往开机启动目录写东西 —— 持久化，重启后还在
    (r"startup", "往开机启动目录写"),

    # 命令里带 %VAR% 或 $env: 拼接
    #
    # 为什么要拦：
    #     暴力测试里 "pow%x%ershell -c 1" 穿过去了——
    #     因为展开之前是 pow%x%ershell，匹配不到 powershell。
    #     而 cmd 执行时会展开变量。
    #
    #     要做正确的展开得模拟 cmd 的变量解析，复杂还容易漏。
    #     简单办法：**带变量引用的命令一律不放**。
    #     正常工作几乎不需要在命令里拼变量名。
    (r"%[a-z_][a-z0-9_]*%", "命令里带环境变量拼写"),
    (r"\$env:", "命令里带环境变量拼写"),
]


# 这些形式的 "-m" 是日常用法，不该因为带了 -m 就拦。
#
# 为什么单独列：
#    上面把 "python -m xxx" 一并拦了（因为 python -m os 能跑任意模块）。
#    但 pytest / pip / venv 这些天天用，误伤它们太难受。
#    所以列白名单：模块名在这里面的放行。
ALLOWED_M_MODULES = (
    "pytest", "pip", "venv", "unittest", "json.tool",
    "http.server", "compileall", "black", "ruff", "mypy",
)


def uses_arbitrary_executor(command: str) -> tuple[bool, str]:
    """这条命令是不是能执行任意代码。返回 (是不是, 是什么)。"""
    c = (command or "").lower()
    if not c:
        return False, ""

    # 先把白名单的 -m 模块"抹掉"，免得被通用规则误伤。
    #
    # 为什么这么做：
    #   "python -m os" 能跑任意模块，必须拦。
    #   但 "python -m pytest" 是天天用的命令，拦了没法干活。
    #   两个长得一模一样，区别只在模块名。
    #
    #   所以：把白名单模块名替换成占位符，剩下的还带 -m 的才拦。
    scrubbed = c
    for mod in ALLOWED_M_MODULES:
        # ⚠️ 要把 "-m 模块名" 整段抹掉，不能只换模块名。
        #
        #    第一版写的是 replace("-m pytest", "-m __allowed__")，
        #    结果剩下的 "-m __allowed__" 仍然带 "-m"，
        #    还是会撞上 (python|py)\s+-m\s 这条规则 ——
        #    等于白名单根本没生效，python -m pytest 照样被拦。
        scrubbed = scrubbed.replace(f"-m {mod}", " __allowed_mod__")

    for pat, label in _ARBITRARY_EXEC:
        try:
            if re.search(pat, scrubbed):
                return True, label
        except re.error:
            continue

    return False, ""


# ══════════════════════════════════════════════════════
# 二、不可逆动作（第二层）
# ══════════════════════════════════════════════════════
#
# 词表拦不全任意解释器（那是第一层的事），但对**直接写着**的
# 破坏性命令仍然有用：
#     第一层挡"能执行任意代码"的口子
#     第二层挡"直接写着"的破坏
# 两层都过才放行。

_IRREVERSIBLE = [
    ("删文件", "删掉的东西找不回来",
     r"\b(rm|del|erase|unlink)\b|remove-item"),
    ("删目录", "整个目录没了",
     r"\b(rmdir|rd)\b|rm\s+-[a-z]*r|remove-item.*-recurse"),
    ("改权限", "改完不知道原来是什么",
     r"\b(chmod|chown|icacls|takeown|cacls|attrib)\b|set-acl"),
    ("强制推送 / 硬重置", "远端历史被覆盖",
     r"git\s+push\s+.*(-f\b|--force)|git\s+reset\s+--hard|"
     r"git\s+clean\s+-[a-z]*f|git\s+branch\s+-d\b|"
     r"git\s+filter-branch|git\s+reflog\s+expire"),
    ("格式化 / 分区", "盘上的东西全没了",
     r"\b(format|diskpart|mkfs|fdisk|parted)\b"),
    ("改系统设置", "改坏系统进不去",
     r"\breg\s+(add|delete)|setx\b|schtasks\b|"
     r"sc\s+(create|delete|config)|new-service|set-service|bcdedit\b"),
    ("清空 / 覆盖数据", "数据被覆盖，恢复不了",
     r"\b(drop\s+(table|database)|truncate|delete\s+from)\b|>\s*/dev/sd"),
    ("删文件（find/dd/shred）", "底层删除，恢复不了",
     r"find\s+\S+\s+-delete|find\s+.*-exec\s+rm|"
     r"\bdd\s+if=|\b(shred|sdelete|wipe)\b"),
    ("通过 wmic/系统工具破坏", "能停服务、删进程",
     r"\bwmic\s+(process|service|logicaldisk)\s+.*(delete|call)\b"),
    ("把数据发出去", "发出去收不回来",
     r"\b(curl|wget|invoke-webrequest|invoke-restmethod)\b.*"
     r"(-d\b|-f\b|--data|--upload-file|-t\b)|"
     r"\b(scp|rsync|ftp|sftp)\b"),
]


def describe(command: str) -> list[dict]:
    """一条命令里命中哪些不可逆动作。"""
    c = (command or "").lower()
    if not c:
        return []

    hits = []
    for kind, why, pat in _IRREVERSIBLE:
        try:
            if re.search(pat, c):
                hits.append({"kind": kind, "why": why})
        except re.error:
            continue
    return hits


# ══════════════════════════════════════════════════════
# 三、保护文件
# ══════════════════════════════════════════════════════

PROTECTED_HINTS = [
    "guard_count",
    ".syscache",
    ".guard_key",
    ".initialized",
    "guard.json",
    "api_token",
    "task_perms",
]


# ══════════════════════════════════════════════════════
# 带路径的破坏性操作（需要知道工作目录才能判断）
# ══════════════════════════════════════════════════════
#
# 这一类跟上面那个词表的区别：
#     上面那些"不管在哪做都不可逆"（delete table、format）
#     这一类"在工作区里做没事，出去就不可逆"
#
# 所以它们不能放进纯词表——词表不知道工作目录。
#
# 为什么会走到这一步：
#     一开始把 mv 和 echo > 也塞进词表，结果 mv a b 被误伤。
#     删掉之后，千问那两条又漏了：
#         move D:/Project D:/_trash_xyz
#         echo. > D:/important.txt
#
#     教训：**同一类命令，"在工作区里"和"在外面"是两回事。**
#     判断得带上工作目录才准。


def _paths_in(command: str) -> list[str]:
    """从命令里粗略抽出路径样子的东西。

    不追求完美的 shell 解析（那要写个 parser），
    只要能认出"绝对路径"和"上级目录"就够了——
    因为我们要判断的是"会不会跑到工作区外面"。
    """
    import re as _re

    # 绝对路径（C:\... 或 /...）和上级目录
    pats = [
        r"[a-zA-Z]:[\\/][^\s\"';&|]*",     # C:\xxx
        r"(?<![\w])/(?:[^\s\"';&|]+)",       # /xxx
        r"(?:\.\./)+[^\s\"';&|]*",          # ../xxx
        r"(?:\.\\)+[^\s\"';&|]*",          # ..\xxx
    ]
    out = []
    for pat in pats:
        try:
            out.extend(_re.findall(pat, command))
        except _re.error:
            continue
    return out


def destructive_move_or_truncate(command: str, workdir: str) -> dict | None:
    """带路径的移动/清空，工作区外的算不可逆。

    返回命中的说明，没命中返回 None。
    """
    import re as _re

    c = (command or "").lower()
    if not c:
        return None

    paths = _paths_in(c)

    # 移动：看源和目标在不在工作区里
    #
    # 判断"是不是移动命令"用开头匹配就够，不用 \b——
    # 而且用 \b 反而踩过坑：写进源码时它变成了退格符（0x08），
    # 正则直接失效，这条规则静默没生效。暴力测试里 move 那条穿过去了。
    if _re.search(r"^\s*(move|mv|ren|rename|robocopy)\s", c):
        outside = [p for p in paths if is_outside(p, workdir)]
        if outside:
            return {
                "kind": "移动工作区外面的东西",
                "why": "移走就找不到了，而且动的是你自己的真实文件",
                "paths": outside[:3],
            }

    # 清空：echo > 后面跟的东西
    m = _re.search(r"echo\.?\s*>\s*([^\s;&|]+)", c)
    if m:
        target = m.group(1).strip('"\'')
        # 目标是绝对路径，或者在工作区外面
        if (os.path.isabs(target) or target.startswith("..")) and \
                is_outside(target, workdir):
            return {
                "kind": "清空工作区外面的文件",
                "why": "内容被覆盖成空，恢复不了",
                "paths": [target],
            }

    return None


def touches_protected(command: str) -> bool:
    """命令里提到了我们自己的安全文件吗。"""
    c = (command or "").lower()
    return any(h in c for h in PROTECTED_HINTS)


# ══════════════════════════════════════════════════════
# 四、越界写
# ══════════════════════════════════════════════════════

def is_outside(path: str, workdir: str) -> bool:
    """这个路径在任务工作目录外面吗。"""
    if not path or not workdir:
        return False
    try:
        p = os.path.normcase(os.path.abspath(path))
        w = os.path.normcase(os.path.abspath(workdir))
        if p == w:
            return False
        if not w.endswith(os.sep):
            w = w + os.sep
        return not p.startswith(w)
    except (OSError, ValueError):
        return False


# ══════════════════════════════════════════════════════
# 五、给模型/用户的话
# ══════════════════════════════════════════════════════

def explain_executor(label: str) -> str:
    """拦下"能执行任意代码"的命令时说什么。"""
    return (
        f"[拦下了] 这条命令用的是「{label}」——它能执行任意代码，"
        f"等于能做任何事。\n"
        f"在工作区权限下这种命令不跑。\n"
        f"真要执行代码：写成文件、放进任务目录、再运行它。"
    )


def explain_irreversible(hit: dict) -> str:
    """拦下不可逆动作时说什么。"""
    return (
        f"[拦下了] 这一步是「{hit.get('kind', '')}」——{hit.get('why', '')}。\n"
        f"真要这么做的话，跟用户确认一次。"
    )


def explain_outside(path: str) -> str:
    """写工作区外面的文件时说什么。

    ⚠️ 这个函数本身就是一次修正。

        原来这里是"已经照做了，但请告诉用户"——
        而代码实际是**跳过执行**、没写。
        于是模型以为写了，可能跟用户说"我改了 xxx"，
        用户去查发现没变。谎报比不做更糟。

        现在改成如实说：没写，问你。
    """
    return (
        f"[没写] 这个文件在任务目录外面：{path}\n"
        f"当前是工作区权限，只能动任务目录里的东西。\n"
        f"要写它的话，需要给这个窗口提权，或者告诉用户让他自己处理。"
    )
