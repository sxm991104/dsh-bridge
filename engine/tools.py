"""给模型用的工具。

一共六个。不多不少：

    read_file     读文件（带行号，方便定位）
    write_file    整篇写
    edit_file     精确替换一段
    list_dir      列目录
    search_files  按内容搜
    run_command   跑一条命令

为什么只要这六个
    DSH 有几十个，但任务端真正用到的就是这几个。
    工具越多，模型越容易挑错；而且每个工具的描述都要占上下文。

    另外 `edit_file` 单独列出来是有原因的：
    让模型"整篇重写"来改一个字符，既慢又容易改坏别的地方。
    精确替换能让它只动该动的那一行。

安全上的取舍
    这里不做沙箱。任务端本来就是要在整台机器上干活的，
    加个只能写一个目录的限制，反而挡住正当用途（实测过：
    DSH 的 workspace-write 模式下，让它去 D 盘建个文件会直接报
    Access denied，用户以为程序坏了）。

    真正该做的是**让人能看见它干了什么**——所以每个工具都返回
    明确的结果，界面和日志里都能追。
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys

# 单个文件读多少就算太大。超过这个数只给开头和结尾，
# 免得一次把上下文塞满。
MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000

# 当前任务的工作目录。
#
# ⚠️ 这一条是实测踩出来的：
#    模型说 write_file("test.txt")，用的是相对路径。
#    而工具按 Python 进程的 cwd 去解析——那是启动桥的目录，
#    不是任务的工作目录。结果文件写到了别处，
#    模型自己还"读"回来了（因为读也是同一个错目录），
#    一切看起来都对，只有用户在任务目录里找不到文件。
#
#    所以每个任务开始前必须把 _WORKDIR 设成它自己的工作目录，
#    所有相对路径都以那里为准。
_WORKDIR: str = os.getcwd()

# 当前任务的权限级别。
#
# 老爸的设计：**默认工作区权限。**
#
#     工作区    只能动它自己那个任务目录
#     用户目录  能动 ~ 下面的东西
#     完全      整个电脑
#
# 要提权得单独给那个窗口设——不是默认就全开。
_LEVEL: str = "workspace"


def set_workdir(path: str) -> None:
    """设定这一轮任务的工作目录。agent 开跑前调用。"""
    global _WORKDIR
    if path and os.path.isdir(path):
        _WORKDIR = path


def workdir() -> str:
    return _WORKDIR


def set_level(level: str) -> None:
    """设定这一轮任务的权限级别。agent 开跑前调用。"""
    global _LEVEL
    if level in ("workspace", "home", "full"):
        _LEVEL = level


def level() -> str:
    return _LEVEL


def _resolve(path: str) -> str:
    """把模型给的路径解析成绝对路径。**顺便做权限检查。**

    相对路径以任务工作目录为准；绝对路径原样用。
    ~ 会展开成用户目录。

    ⚠️ 权限检查放在这里，是因为**所有文件工具都经过它**：

        read_file / write_file / edit_file / list_dir / search_files
        run_command 的 cwd

        一个地方把关，比每个工具各加一遍靠谱得多——
        漏一个就是漏洞。

    默认权限是"工作区"，也就是只能在任务自己的目录里动。
    要碰外面，得给那个窗口单独提权。
    """
    p = (path or "").strip().strip('"')
    if not p:
        return _WORKDIR

    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(_WORKDIR, p)
    p = os.path.normpath(p)

    try:
        import task_perms as _tp

        if not _tp.is_allowed(p, _LEVEL, _WORKDIR):
            # 抛出去，让调用方当"工具失败"处理——
            # 模型会看到这段说明，知道是权限不够，不是文件不存在。
            raise PermissionError(_tp.explain_denied(p, _LEVEL))
    except ImportError:
        pass

    return p


def _nowindow_flags() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


def _build_args(command: str) -> list[str]:
    """把用户写的命令变成 subprocess 的参数列表。

    ⚠️ 这里踩过一个很隐蔽的坑，值得完整写下来。

        现象：`python -c "print('中文')"` 通过 run_command 跑，
              返回码 0，**但一个字都没输出**。

        排查：
            · 直接 subprocess.run([python, "-c", "print(...)"])  → 正常
            · 靠 subprocess.run(["cmd", "/c", 完整命令])        → 空输出

        根因：**cmd 会剥掉命令里的双引号。**

            cmd /c python -c "print('中文')"

            cmd 认为双引号只是"给参数分组用的"，会把它去掉。
            于是 python 收到的是：
                -c   print('中文')     ← 少了引号，而且被拆成两个参数
            它把这个当成语法错误或者空程序，安安静静退出，什么都不打印。

        第一次遇到是 `echo 中文` 乱码，修好解码之后又冒出这个。
        两个是完全不同的问题，长得很像：
            乱码    → 字节解错了
            空输出   → 参数被 cmd 吃掉了

    **修法：不用 cmd 当中间层。**

        命令里如果包含了双引号，就自己拆成参数列表——
        Windows 上引号本来就是用来分组的，我们自己拆比让 cmd 拆更准。

        没有双引号的简单命令，还是走 cmd /c
        （这样 echo、dir、type 这些内置命令仍然能用）。
    """
    if sys.platform != "win32":
        return ["/bin/sh", "-c", command]

    # 有双引号 → 自己拆，别让 cmd 动手
    if '"' in command:
        parts = _split_command(command)
        if parts:
            return parts

    return ["cmd", "/c", command]


def _split_command(command: str) -> list[str]:
    """按 cmd 的规则拆参数：双引号包起来的部分算一个参数。

    比如：
        python -c "print('中文')"
        → ["python", "-c", "print('中文')"]

    拆不出来（引号不配对）就返回空列表，让调用方回退到 cmd。
    """
    parts: list[str] = []
    cur: list[str] = []
    in_quote = False

    for ch in command:
        if ch == '"':
            in_quote = not in_quote
            continue
        if ch in (" ", "\t") and not in_quote:
            if cur:
                parts.append("".join(cur))
                cur = []
            continue
        cur.append(ch)

    if in_quote:
        # 引号没配平，不拆
        return []

    if cur:
        parts.append("".join(cur))

    if not parts:
        return []

    # 第一个词如果是已知的内置命令（echo/dir/cd…），还是得交给 cmd
    builtin = {"echo", "dir", "type", "copy", "del", "cd", "chdir",
               "set", "cls", "ver", "vol", "md", "mkdir", "rd", "rmdir",
               "ren", "rename", "move", "tasklist", "taskkill", "where"}
    if parts[0].lower() in builtin:
        return []

    return parts


def _decode_output(raw: bytes) -> str:
    """把子进程的输出字节变成文字。

    ⚠️ 这里踩过一个很隐蔽的坑，值得写清楚。

        现象：`echo 中文测试` 出来是 `���Ĳ���`。

        排查过程：
            · 直接跑 subprocess.run(["cmd","/c","echo 中文"]) → 正确
            · 通过 run_command 跑 → 乱码
            · 对比参数，差在 creationflags
            · 带 CREATE_NO_WINDOW → 子进程输出 GBK 字节
              不带                 → 输出 UTF-8 字节

        为什么加了 CREATE_NO_WINDOW 就变成 GBK：
            cmd.exe 的输出编码跟着控制台的代码页走。
            带 CREATE_NO_WINDOW 时不继承父进程的控制台，
            自己起一个默认代码页 936（简体中文 GBK）。
            不带时继承了父进程的（我们设了 UTF-8）。

        试过但不可靠的做法：
            · 统一按 UTF-8 解 → 带 creationflags 时乱码
            · 统一按 GBK 解   → 不带时乱码
            · 强制给子进程设 PYTHONIOENCODING → 只管 Python 子进程，
              cmd.exe 不吃这个变量

        最终做法：**先试 UTF-8，失败再用 GBK**。

            UTF-8 的字节序列有很强的校验性——中文乱码成 GBK
            很难恰好是合法 UTF-8。所以"严格试 UTF-8"是可靠的判据。

            实测两种都能正确解：
                UTF-8 字节 → 严格解码成功 → 对
                GBK 字节   → 严格解码抛异常 → 转 GBK → 对
    """
    if not raw:
        return ""

    # 先严格试 UTF-8
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 再试 GBK（简体中文 Windows 的默认代码页）
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        pass

    # 都不行：用 UTF-8 + replace，至少不崩
    return raw.decode("utf-8", "replace")


# ----------------------------------------------------------------------
# 工具的实现
# ----------------------------------------------------------------------

def read_file(path: str, offset: int = 1, limit: int = 0) -> str:
    p = _resolve(path)
    if not os.path.exists(p):
        return f"文件不存在：{p}"
    if os.path.isdir(p):
        return f"这是个目录，不是文件：{p}"

    size = os.path.getsize(p)
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        return f"读不了：{e}"

    total = len(lines)
    start = max(0, offset - 1)
    end = total if limit <= 0 else min(total, start + limit)
    chunk = lines[start:end]

    body = "".join(f"{i + start + 1:>6}\t{ln}" for i, ln in enumerate(chunk))
    if size > MAX_READ_BYTES:
        body = body[:MAX_READ_BYTES] + "\n…（文件太大，只读了前面一段）"

    head = f"{p}（共 {total} 行，这里是第 {start + 1}~{end} 行）\n"
    return head + body


def write_file(path: str, content: str) -> str:
    p = _resolve(path)
    parent = os.path.dirname(os.path.abspath(p))
    try:
        os.makedirs(parent, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
    except OSError as e:
        return f"写不了：{e}"
    return f"已写入 {p}（{len(content)} 字符）"


def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
    p = _resolve(path)
    if not os.path.exists(p):
        return f"文件不存在：{p}"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return f"读不了：{e}"

    n = text.count(old)
    if n == 0:
        return (
            "没找到要替换的内容。\n"
            "注意 old 必须和文件里的原文完全一致（包括空格和缩进）。\n"
            "建议先用 read_file 看一眼要改的那几行。"
        )
    if n > 1 and not replace_all:
        return (
            f"这段内容在文件里出现了 {n} 次，替换哪一个不明确。\n"
            "把 old 写长一点、带上前后文，让它唯一；"
            "或者明确说 replace_all=true。"
        )

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(updated)
    except OSError as e:
        return f"写不了：{e}"

    hit = n if replace_all else 1
    return f"已改 {p}（替换了 {hit} 处）"


def list_dir(path: str) -> str:
    p = _resolve(path)
    if not os.path.isdir(p):
        return f"目录不存在：{p}"
    try:
        names = sorted(os.listdir(p))
    except OSError as e:
        return f"看不了：{e}"

    out = []
    for n in names[:400]:
        full = os.path.join(p, n)
        if os.path.isdir(full):
            out.append(f"{n}/")
        else:
            try:
                out.append(f"{n}  ({os.path.getsize(full)} B)")
            except OSError:
                out.append(n)
    more = f"\n…还有 {len(names) - 400} 项" if len(names) > 400 else ""
    return f"{p}/\n" + "\n".join(out) + more


def search_files(pattern: str, path: str = ".", glob_filter: str = "") -> str:
    """按内容搜。用 Python 自己走目录，不依赖 grep（有些机器没有）。"""
    root = _resolve(path)
    if not os.path.isdir(root):
        return f"目录不存在：{root}"

    hits = []
    scanned = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            # 跳过明显的噪音目录
            dirnames[:] = [
                d for d in dirnames
                if d not in (".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build")
            ]
            for fn in filenames:
                if glob_filter and not fnmatch.fnmatch(fn, glob_filter):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > 2_000_000:
                        continue
                    with open(full, encoding="utf-8", errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if pattern in line:
                                hits.append(f"{full}:{i}: {line.strip()[:200]}")
                                if len(hits) >= 100:
                                    raise StopIteration
                except (OSError, UnicodeDecodeError):
                    continue
                scanned += 1
                if scanned > 5000:
                    raise StopIteration
    except StopIteration:
        pass

    if not hits:
        return f"没找到包含「{pattern}」的地方（看了 {scanned} 个文件）"
    return "\n".join(hits) + f"\n（共 {len(hits)} 处）"


def run_command(command: str, cwd: str = "", timeout: int = 120) -> str:
    """跑一条命令。

    Windows 上走 cmd /c，别的系统走 sh -c。
    stdout 和 stderr 合在一起返回——模型需要看到错误信息才能自己纠。
    """
    work_dir = _resolve(cwd) if cwd else _WORKDIR
    if not os.path.isdir(work_dir):
        work_dir = _WORKDIR

    # 让命令里能直接用 $WORKDIR / %WORKDIR%
    env = dict(os.environ)
    env["WORKDIR"] = work_dir

    args = _build_args(command)

    try:
        r = subprocess.run(
            args,
            cwd=work_dir,
            env=env,
            capture_output=True,
            timeout=max(5, min(timeout, 600)),
            creationflags=_nowindow_flags(),
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（{timeout} 秒）被中止：{command}"
    except OSError as e:
        return f"起不来：{e}"

    out = _decode_output(r.stdout or b"")
    err = _decode_output(r.stderr or b"")
    text = out + (("\n[stderr]\n" + err) if err.strip() else "")
    text = text.strip() or "(没有输出)"

    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n…（输出太长，截断到 {MAX_OUTPUT_CHARS} 字符）"

    return f"[退出码 {r.returncode}]\n{text}"


# ----------------------------------------------------------------------
# 给模型看的工具说明
# ----------------------------------------------------------------------

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读一个文件的内容，带行号。改文件之前先读一下要改的地方。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "offset": {"type": "integer", "description": "从第几行开始读，默认 1"},
                    "limit": {"type": "integer", "description": "读多少行，默认全读"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "新建文件或整篇覆盖。只想改几行的话用 edit_file，别用这个。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string", "description": "完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "把文件里的一段原文换成新的。old 必须和原文完全一致"
                "（含缩进空格），建议先用 read_file 看清楚。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string", "description": "要被替换的原文"},
                    "new": {"type": "string", "description": "换成的内容"},
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有出现，默认只换第一处",
                    },
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "看一个目录里有什么文件。不确定路径时先用它探路。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "在目录里按内容搜索字符串，返回命中的文件和行号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "要搜的字符串"},
                    "path": {"type": "string", "description": "在哪个目录下搜，默认当前目录"},
                    "glob_filter": {"type": "string", "description": "只搜匹配的文件，比如 *.py"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "跑一条命令行命令，返回输出和退出码。"
                "适合跑测试、git、装依赖。别用它读写文件——那是别的工具的活。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string", "description": "在哪个目录下跑"},
                    "timeout": {"type": "integer", "description": "超时秒数，默认 120"},
                },
                "required": ["command"],
            },
        },
    },
]

_DISPATCH = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "search_files": search_files,
    "run_command": run_command,
}


def execute(name: str, arguments: str) -> str:
    """执行一次工具调用。

    模型的参数偶尔会出问题：JSON 不完整、字段名写错、类型不对。
    这里全部吞掉异常，把错误当成工具结果返回给它——
    模型看到"你参数写错了"会自己改，直接抛异常则整轮就断了。
    """
    fn = _DISPATCH.get(name)
    if fn is None:
        return f"没有这个工具：{name}"

    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as e:
        return (
            f"参数不是合法 JSON：{e}\n"
            f"收到的是：{arguments[:300]}\n"
            "注意 JSON 里字符串要用双引号，路径里的反斜杠要写成 \\\\"
        )

    if not isinstance(args, dict):
        return f"参数应该是个对象，收到的是 {type(args).__name__}"

    # 只保留函数认识的参数，多余的丢掉（模型有时会自作主张加字段）
    import inspect

    try:
        accepted = set(inspect.signature(fn).parameters)
        args = {k: v for k, v in args.items() if k in accepted}
    except (TypeError, ValueError):
        pass

    try:
        return str(fn(**args))
    except TypeError as e:
        return f"参数不对：{e}"
    except Exception as e:  # noqa: BLE001
        return f"执行出错：{type(e).__name__}: {e}"
