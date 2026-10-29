"""任务名/目录名 的安全校验。

漏洞
    接口收一个 task_name，直接拿去拼路径：
        workspaces/<task_name>/
        state/<task_name>.json
    如果 task_name 是 "../PWNED"，就落到了父目录。

    实测确实逃出去了：在桥目录之外建出了文件夹。
    能往系统目录写、能覆盖用户文件，这是最严重的一类漏洞。

修法
    不是"过滤掉 .."（不好使，编码、Windows 的 \\ / 混用、Unicode 变体都能绕），
    而是**白名单**：只允许明确列出来的字符，其余一律拒。

    同时最后还要做一次真实路径校验——拼出来的绝对路径
    必须真的在允许的根目录底下。两道，双保险。
"""

from __future__ import annotations

import os
import re

# 任务名允许的字符。
#
# 中文、英文、数字、空格、连字符、下划线、点。
# 不要斜杠、不要反斜杠、不要冒号（Windows 的盘符分隔）、
# 不要控制字符、不要通配符。
#
# ⚠️ 点允许，是为了支持 "v1.2 修复" 这种正常的名字。
#    但单独的 "." 和 ".." 会被下面单独拦掉。
_ALLOWED = re.compile(r"^[\w\u4e00-\u9fff \-.（）()【】\[\]]+$")

# Windows 上的保留设备名。这些名字在 Windows 里不能当文件名，
# 会指向设备（比如 con 是控制台），建目录会出错或者行为诡异。
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

MAX_NAME_LEN = 80


def safe_name(name: str) -> str:
    """把一个用户给的名字变成能安全当目录名的东西。

    不安全就直接抛——**不要静默改写**。
    改写会让用户以为"我刚才起的名字"就是任务名，
    结果实际存的是另一个，后面按名字找就找不到了。
    """
    if not isinstance(name, str):
        raise ValueError("名字必须是文字")

    # 两头空格去掉。
    #
    # 这是可接受的"改写"：用户起名时多打了一个空格，
    # 去掉不影响他要表达的意思，也不会让他按名字找不到
    # （他说的名字本来就是没空格那个）。
    # 结尾是点或空格会被 Windows 悄悄去掉这一点，留给下面单独拦。
    n = name.strip()

    if not n:
        raise ValueError("名字不能是空的")

    if len(n) > MAX_NAME_LEN:
        raise ValueError(f"名字太长了（最多 {MAX_NAME_LEN} 个字）")

    # 单独的 . 和 .. —— 这两个是路径意义上的"当前/上级目录"
    if n in (".", ".."):
        raise ValueError("这个名字不能用")

    # 以 - 开头的名字。
    #
    # 为什么拦：任务名会被当成命令行参数值传下去
    #     bridge.py run --id "<名字>"
    # 名字是 "--help" 时，argparse 会把它当成另一个参数名，
    # 报"参数缺值"，worker 起不来，任务永远卡在排队。
    #
    # 这不是安全漏洞（argparse 挡住了真正的注入），
    # 但是个让人摸不着头脑的功能问题：任务派出去了、状态一直排队。
    if n.startswith("-"):
        raise ValueError("名字不能以 - 开头")

    # 白名单之外一律拒
    if not _ALLOWED.match(n):
        raise ValueError(
            "名字里不能有 / \\ : * ? \" < > | 这些符号"
        )

    # Windows 保留名（大小写不敏感，而且 "con.txt" 也算）
    stem = n.split(".")[0].upper()
    if stem in _RESERVED:
        raise ValueError("这个名字跟系统设备重名，换一个")

    # 结尾是点：Windows 会悄悄去掉，导致"名字对不上"
    if n.endswith("."):
        raise ValueError("名字结尾不能是点")

    return n


def is_inside(path: str, root: str) -> bool:
    """这个路径是不是真的在 root 底下。

    第二道防线：就算名字校验被绕过了，
    只要拼出来的实际路径跑到了根目录外面，这里也能拦住。

    必须用 realpath（解析软链接和 ..），
    不能只比较字符串前缀——"C:\\a\\b\\..\\c" 字符串上看起来在 a 底下。
    """
    try:
        p = os.path.normcase(os.path.realpath(path))
        r = os.path.normcase(os.path.realpath(root))
    except (OSError, ValueError):
        return False

    # 必须完全相等，或者在 root 后面紧跟分隔符
    # （防止 "C:\\a" 匹配上 "C:\\ab"）
    return p == r or p.startswith(r + os.sep)


def safe_join(root: str, name: str) -> str:
    """把 name 拼到 root 底下，并确认真的在 root 里。

    先校验名字，再拼，再验一次真实路径。
    """
    n = safe_name(name)
    p = os.path.join(root, n)
    if not is_inside(p, root):
        raise ValueError("这个名字不能用")
    return p
