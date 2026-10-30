"""收集任务产出的文件。

任务跑完，它自己的工作目录里可能躺着做出来的东西：
    报告.docx、数据.xlsx、截图.png、代码……

用户要的是这些文件本身，不是一句"做完了"。

这个模块负责：
    · 扫描任务工作目录
    · 判断哪些是"产出"（不是环境文件、不是缓存）
    · 按类型分好，给推送那边用
"""

from __future__ import annotations

import os

# 单个文件超过这个大小就不推了。
#
# 为什么不推大文件：
#   QQ 的主动消息有频次限制，大文件上传慢还容易失败；
#   而且用户多半也不指望在手机上下一个 200MB 的东西。
#   超了就在推送里说一声"有文件，太大没发，去电脑上看"。
MAX_FILE_BYTES = 30 * 1024 * 1024      # 30MB

# 一次最多推几个。
# 推太多会变成刷屏，而且每个都要上传、都要占主动消息频次。
MAX_FILES = 5

# 这些文件不是"产出"，是跑任务留下的垃圾。
SKIP_NAMES = {
    ".progress.json", ".session.json", ".state.json",
    "tasks.jsonl", "outbox.jsonl", "bridge.log",
    ".DS_Store", "Thumbs.db", "desktop.ini",
    "node_modules", "__pycache__", ".git", ".venv", "venv",
}

# 这些后缀肯定不是产出（临时文件、缓存）
SKIP_SUFFIXES = (
    ".tmp", ".temp", ".log", ".lock", ".pyc", ".pyo",
    ".swp", ".swo", ".bak~",
)

# 这些是产出，而且用户多半想在手机上看。
# 图片单独分出来——图片可以直接展示，文件是"一个卡片"。
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

# 文档类——这些最值得推
DOC_SUFFIXES = (
    ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".pdf", ".csv", ".md", ".txt", ".json", ".xml",
    ".html", ".htm", ".zip", ".rar", ".7z",
)


def _looks_like_output(name: str) -> bool:
    """这个文件名看起来像"做出来的东西"吗。"""
    low = name.lower()

    if name in SKIP_NAMES or low in {n.lower() for n in SKIP_NAMES}:
        return False

    if low.endswith(SKIP_SUFFIXES):
        return False

    # 隐藏文件一般不推
    if name.startswith("."):
        return False

    return True


def collect(workdir: str, max_files: int = MAX_FILES,
            max_bytes: int = MAX_FILE_BYTES) -> dict:
    """扫一个任务目录，找出产出文件。

    返回：
        {
            "files":     [{"path", "name", "size", "kind"}],  要推的
            "skipped":   [{"name", "why"}],                  没推的原因
            "total_size": int,
        }

    kind 是下面三种之一：
        "image"  图片（可以直接展示）
        "doc"    文档/表格/压缩包（最值得推）
        "other"  其他文件
    """
    result: dict = {"files": [], "skipped": [], "total_size": 0}

    if not workdir or not os.path.isdir(workdir):
        return result

    candidates: list[tuple[str, str, int]] = []

    # 只扫顶层 + 一层子目录里最近改的。
    #
    # 不递归扫全部：
    #   任务目录里可能有 node_modules、构建产物、几百个中间文件。
    #   全扫一遍慢，而且会把垃圾当产出。
    #   两层的深度对"用户想看的成果"已经够了。
    try:
        entries = list(os.scandir(workdir))
    except OSError:
        return result

    for e in entries:
        try:
            if e.is_file():
                candidates.append((e.path, e.name, e.stat().st_size))
            elif e.is_dir() and not e.name.startswith(".") and e.name not in SKIP_NAMES:
                # 只往下看一层
                try:
                    for sub in os.scandir(e.path):
                        if sub.is_file():
                            candidates.append((
                                sub.path,
                                os.path.join(e.name, sub.name),
                                sub.stat().st_size,
                            ))
                except OSError:
                    continue
        except OSError:
            continue

    # 按修改时间倒序——最后做出来的东西最可能是成果
    def _mtime(p: str) -> float:
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0

    candidates.sort(key=lambda c: _mtime(c[0]), reverse=True)

    for path, name, size in candidates:
        base = os.path.basename(name)

        if not _looks_like_output(base):
            continue

        if size > max_bytes:
            result["skipped"].append({
                "name": name,
                "why": f"太大（{size // 1024 // 1024}MB，超过 "
                       f"{max_bytes // 1024 // 1024}MB）",
            })
            continue

        if size == 0:
            result["skipped"].append({"name": name, "why": "空文件"})
            continue

        if len(result["files"]) >= max_files:
            result["skipped"].append({
                "name": name, "why": f"一次最多推 {max_files} 个",
            })
            continue

        low = base.lower()
        if low.endswith(IMAGE_SUFFIXES):
            kind = "image"
        elif low.endswith(DOC_SUFFIXES):
            kind = "doc"
        else:
            kind = "other"

        result["files"].append({
            "path": os.path.abspath(path),
            "name": name,
            "size": size,
            "kind": kind,
        })
        result["total_size"] += size

    return result


def describe(batch: dict) -> str:
    """一句话说清楚有哪些产出，给推送文字用。

    没文件就返回空串——推送文字保持原样，不要多一句废话。
    """
    files = batch.get("files") or []
    if not files:
        return ""

    n = len(files)
    if n == 1:
        return f"还有一个文件：{files[0]['name']}"
    return f"还有 {n} 个文件"
