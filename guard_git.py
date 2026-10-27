"""给项目加防护：别让 AI 的临时文件/思考记录混进 git。

为什么需要这个
    电脑端的我（通过 dsh qqme 跑的那个）在项目里干活时，
    可能在项目目录里留下临时文件、草稿、笔记。
    这些一旦被 `git add -A` 收进去，就变成了提交记录的一部分。

    而你在家的时候看不到这些文件，等最后让我"提交"，
    那些垃圾就已经进了仓库。

    这不是假设——常见的垃圾有：
        .dsh-notes.md        我自己记的笔记
        .tmp/                临时目录
        *.bak / *_backup.*   顺手备份的
        .session-*.jsonl     会话落盘
        scratch/             草稿
        *.orig / *.rej       冲突残留

    所以给每个项目加一个 .gitignore 片段，把这些挡在外面。

用法
    python guard_git.py check  <项目路径>    看看有没有防护
    python guard_git.py apply  <项目路径>    加上防护（追加到 .gitignore）
    python guard_git.py list                 列出所有受保护的项目
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 这些模式必须挡住。写在 .gitignore 里，进不了暂存区。
GUARD_BLOCK = """
# === DSH AI 工作防护（自动添加，勿删） ===
# AI 在项目里干活时留下的临时文件、草稿、会话落盘，一律不入库。
.dsh-notes.md
.dsh-*
.session-*
*.session.jsonl*
.scratch/
scratch/
.tmp/
tmp-ai/
*.orig
*.rej
*.bak-ai*
*_backup_ai*

# 别把整个会话记录目录拖进来
.sessions/
bridge-tasks/
# === 防护结束 ===
"""

MARKER = "# === DSH AI 工作防护（自动添加，勿删） ==="


def find_git_root(path: str) -> str | None:
    """往上找 .git，返回项目根。"""
    cur = os.path.abspath(path)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")) or os.path.isfile(
            os.path.join(cur, ".git")
        ):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def check(path: str) -> dict:
    root = find_git_root(path)
    if not root:
        return {"path": path, "has_git": False, "guarded": False, "note": "不是 git 仓库"}

    gi = os.path.join(root, ".gitignore")
    if not os.path.exists(gi):
        return {"path": root, "has_git": True, "guarded": False, "note": "没有 .gitignore"}

    try:
        with open(gi, encoding="utf-8", errors="ignore") as f:
            txt = f.read()
    except OSError:
        return {"path": root, "has_git": True, "guarded": False, "note": "读不到 .gitignore"}

    guarded = MARKER in txt
    return {
        "path": root,
        "has_git": True,
        "guarded": guarded,
        "note": "已有防护" if guarded else "缺防护",
    }


def apply_guard(path: str) -> dict:
    info = check(path)
    if not info["has_git"]:
        return {**info, "changed": False}
    if info["guarded"]:
        return {**info, "changed": False, "note": "已经有防护，没动"}

    gi = os.path.join(info["path"], ".gitignore")
    try:
        # 先确保结尾有换行，不然追加会粘在上一行
        existing = ""
        if os.path.exists(gi):
            with open(gi, encoding="utf-8", errors="ignore") as f:
                existing = f.read()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        with open(gi, "w", encoding="utf-8") as f:
            f.write(existing + GUARD_BLOCK)
    except OSError as e:
        return {**info, "changed": False, "note": f"写不进去: {e}"}

    return {**info, "guarded": True, "changed": True, "note": "已加防护"}


def scan_common_roots() -> list[dict]:
    """扫一下常见的项目根，报告哪些没防护。"""
    # 扫描范围：可用环境变量 GUARD_ROOTS 覆盖（分号分隔）
    env_roots = os.environ.get("GUARD_ROOTS", "").strip()
    if env_roots:
        roots = [x for x in env_roots.split(";") if x.strip()]
    else:
        home = os.path.expanduser("~")
        roots = [
            os.path.join(home, "Downloads"),
            os.path.join(home, "Projects"),
            "D:\\Project",
        ]
    out = []
    for base in roots:
        if not os.path.isdir(base):
            continue
        for name in os.listdir(base):
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                continue
            info = check(p)
            if info["has_git"]:
                out.append(info)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="git 防护：别让 AI 垃圾进仓库")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_c = sub.add_parser("check")
    p_c.add_argument("path")

    p_a = sub.add_parser("apply")
    p_a.add_argument("path")

    sub.add_parser("list")

    args = ap.parse_args()

    if args.cmd == "check":
        print(json.dumps(check(args.path), ensure_ascii=False))
        return 0
    if args.cmd == "apply":
        print(json.dumps(apply_guard(args.path), ensure_ascii=False))
        return 0
    if args.cmd == "list":
        print(json.dumps(scan_common_roots(), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
