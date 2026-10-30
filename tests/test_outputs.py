"""任务产出文件的收集测试。

这块决定"做完的东西能不能送到用户手上"。漏了就是把成果弄丢了，
多收了就是把垃圾推给用户。两边都烦人，所以边界要测清楚。
"""

import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import outputs  # noqa: E402


@pytest.fixture
def workspace():
    """一个干净的任务工作目录。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        yield d


def _touch(path: str, size: int = 100) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
    with open(path, "wb") as f:
        f.write(b"x" * size)


# ══════════════════════════════════════════════════════
# 该收的
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("name,kind", [
    ("报告.docx", "doc"),
    ("数据.xlsx", "doc"),
    ("演示.pptx", "doc"),
    ("说明.pdf", "doc"),
    ("数据.csv", "doc"),
    ("笔记.md", "doc"),
    ("截图.png", "image"),
    ("照片.jpg", "image"),
    ("动画.gif", "image"),
    ("打包.zip", "doc"),
    ("脚本.sh", "other"),
])
def test_collects_outputs(workspace: str, name: str, kind: str) -> None:
    """这些都应该被收集，而且分类正确。"""
    _touch(os.path.join(workspace, name))
    r = outputs.collect(workspace)

    names = [f["name"] for f in r["files"]]
    assert name in names, f"{name} 没被收集"

    got = next(f for f in r["files"] if f["name"] == name)
    assert got["kind"] == kind, f"{name} 分类错了：{got['kind']}"


def test_collects_from_subdir(workspace: str) -> None:
    """一层子目录里的也要收。"""
    _touch(os.path.join(workspace, "out", "结果.docx"))
    r = outputs.collect(workspace)

    assert any("结果.docx" in f["name"] for f in r["files"])


# ══════════════════════════════════════════════════════
# 不该收的
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("name", [
    ".progress.json",
    ".session.json",
    ".state.json",
    "tasks.jsonl",
    "outbox.jsonl",
    "bridge.log",
    "temp.tmp",
    "build.log",
    ".DS_Store",
    "Thumbs.db",
    "cache.pyc",
])
def test_skips_internal_files(workspace: str, name: str) -> None:
    """这些是跑任务留下的垃圾，不能推给用户。"""
    _touch(os.path.join(workspace, name))
    r = outputs.collect(workspace)

    names = [f["name"] for f in r["files"]]
    assert name not in names, f"{name} 被当成产出了"


def test_skips_hidden_files(workspace: str) -> None:
    """隐藏文件不推。"""
    _touch(os.path.join(workspace, ".hidden"))
    r = outputs.collect(workspace)
    assert not any(f["name"].startswith(".") for f in r["files"])


def test_skips_node_modules(workspace: str) -> None:
    """node_modules 里的东西不能推。"""
    _touch(os.path.join(workspace, "node_modules", "junk.js"))
    r = outputs.collect(workspace)
    assert not any("junk" in f["name"] for f in r["files"])


def test_skips_empty_files(workspace: str) -> None:
    """空文件推回去没意义。"""
    _touch(os.path.join(workspace, "empty.txt"), 0)
    r = outputs.collect(workspace)

    assert not any(f["name"] == "empty.txt" for f in r["files"])
    assert any(s["name"] == "empty.txt" for s in r["skipped"])


# ══════════════════════════════════════════════════════
# 限制
# ══════════════════════════════════════════════════════

def test_respects_max_files(workspace: str) -> None:
    """一次最多推 max_files 个。"""
    for i in range(20):
        _touch(os.path.join(workspace, f"file{i}.docx"))

    r = outputs.collect(workspace, max_files=5)
    assert len(r["files"]) == 5
    assert len(r["skipped"]) > 0


def test_respects_max_bytes(workspace: str) -> None:
    """超大文件跳过，并说明原因。"""
    big = os.path.join(workspace, "big.zip")
    with open(big, "wb") as f:
        f.seek(2000)
        f.write(b"x")

    r = outputs.collect(workspace, max_bytes=1000)
    assert not any(f["name"] == "big.zip" for f in r["files"])

    skipped = next((s for s in r["skipped"] if s["name"] == "big.zip"), None)
    assert skipped is not None
    assert "大" in skipped["why"]


def test_sorted_by_mtime(workspace: str) -> None:
    """最新的排前面——最后做出来的最可能是成果。"""
    old = os.path.join(workspace, "old.docx")
    new = os.path.join(workspace, "new.docx")

    _touch(old)
    time.sleep(0.05)
    _touch(new)

    r = outputs.collect(workspace)
    names = [f["name"] for f in r["files"]]
    assert names.index("new.docx") < names.index("old.docx")


# ══════════════════════════════════════════════════════
# 边界
# ══════════════════════════════════════════════════════

def test_missing_dir() -> None:
    """目录不存在不能崩。"""
    r = outputs.collect(r"D:\肯定\不存在\的\路径\x")
    assert r["files"] == []
    assert r["total_size"] == 0


def test_empty_dir(workspace: str) -> None:
    """空目录。"""
    r = outputs.collect(workspace)
    assert r["files"] == []


def test_paths_are_absolute(workspace: str) -> None:
    """路径必须是绝对路径——推送那边要直接用。"""
    _touch(os.path.join(workspace, "a.docx"))
    r = outputs.collect(workspace)

    for f in r["files"]:
        assert os.path.isabs(f["path"])


def test_total_size(workspace: str) -> None:
    """字节数要对。"""
    _touch(os.path.join(workspace, "a.docx"), 100)
    _touch(os.path.join(workspace, "b.docx"), 200)
    r = outputs.collect(workspace)

    assert r["total_size"] == 300


# ══════════════════════════════════════════════════════
# describe
# ══════════════════════════════════════════════════════

def test_describe_empty() -> None:
    """没文件时返回空串——推送文字保持原样。"""
    assert outputs.describe({"files": []}) == ""
    assert outputs.describe({}) == ""


def test_describe_single() -> None:
    s = outputs.describe({"files": [{"name": "报告.docx"}]})
    assert "报告.docx" in s


def test_describe_multiple() -> None:
    s = outputs.describe({"files": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    assert "3" in s
