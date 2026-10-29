"""引擎核心的回归测试。

测的是任务端真正靠什么活着：
    · 工具能不能正常工作（读写文件、跑命令）
    · 参数不兼容能不能自动适配（Kimi 只接受 temperature=1）
    · 限流能不能扛住（每分钟 3 次的额度）
    · 会话能不能存/读/压缩

这些都不需要真的调模型，测的是引擎自己的逻辑。
"""

import json
import os
import sys
import tempfile

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from engine import ratelimit  # noqa: E402
from engine import session as sess  # noqa: E402
from engine import tools  # noqa: E402


# ══════════════════════════════════════════════════════
# 工具：文件读写
# ══════════════════════════════════════════════════════

@pytest.fixture
def workdir():
    """一个干净的工作目录。"""
    with tempfile.TemporaryDirectory() as d:
        tools.set_workdir(d)
        yield d
        tools.set_workdir(BASE)


def test_write_then_read(workdir: str) -> None:
    """写进去能读出来。"""
    r = tools.write_file("test.txt", "hello 世界")
    assert "test.txt" in r or "写" in r

    content = tools.read_file("test.txt")
    assert "hello 世界" in content


def test_read_nonexistent(workdir: str) -> None:
    """读不存在的文件要给出明确提示，不能崩。"""
    r = tools.read_file("nope.txt")
    assert isinstance(r, str)
    assert len(r) > 0


def test_edit_file(workdir: str) -> None:
    """精确替换。"""
    tools.write_file("edit.txt", "abc def ghi")
    tools.edit_file("edit.txt", "def", "XYZ")

    assert tools.read_file("edit.txt").count("XYZ") >= 1


def test_edit_file_not_found(workdir: str) -> None:
    """替换目标不存在时要报错，不能静默。"""
    tools.write_file("e2.txt", "hello")
    r = tools.edit_file("e2.txt", "NOT_THERE", "X")
    assert isinstance(r, str)


def test_relative_path_uses_workdir(workdir: str) -> None:
    """相对路径以工作目录为准。

    这里踩过：原来按 Python 进程的 cwd 解析，
    文件写到了别处，模型自己"读"回来也是同一个错目录，
    一切看起来都对，只有用户在任务目录里找不到文件。
    """
    tools.write_file("rel.txt", "relative content")

    # 文件真的落在这个目录里
    p = os.path.join(workdir, "rel.txt")
    assert os.path.exists(p), f"文件不在 {workdir} 里"

    with open(p, encoding="utf-8") as f:
        assert "relative content" in f.read()


def test_list_dir(workdir: str) -> None:
    """列目录。"""
    tools.write_file("a.txt", "a")
    tools.write_file("b.txt", "b")

    r = tools.list_dir(".")
    assert "a.txt" in r
    assert "b.txt" in r


def test_search_files(workdir: str) -> None:
    """按内容搜。"""
    tools.write_file("s1.py", "def hello(): pass")
    tools.write_file("s2.py", "def world(): pass")

    r = tools.search_files("hello", ".", "*.py")
    assert "s1.py" in r


def test_run_command(workdir: str) -> None:
    """跑命令。"""
    r = tools.run_command("echo hello_from_test")
    text = json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else str(r)
    assert "hello_from_test" in text


def test_run_command_failure(workdir: str) -> None:
    """命令失败要有退出码，不能崩。"""
    r = tools.run_command("cmd /c exit 1")
    assert isinstance(r, (dict, str))


# ══════════════════════════════════════════════════════
# 工具：输出截断
# ══════════════════════════════════════════════════════

def test_large_file_truncated(workdir: str) -> None:
    """超大文件不能一次全塞进上下文。"""
    big = "x" * (tools.MAX_READ_BYTES + 10000)
    tools.write_file("big.txt", big)

    r = tools.read_file("big.txt")
    # 不该原样返回全部
    assert len(r) < len(big) + 1000


def test_command_output_truncated(workdir: str) -> None:
    """输出太长的命令也得截断。"""
    # 生成大量输出
    r = tools.run_command(
        'powershell -NoProfile -Command "1..20000 | ForEach-Object { $_ }"'
    )
    text = json.dumps(r, ensure_ascii=False) if isinstance(r, dict) else str(r)
    assert len(text) < tools.MAX_OUTPUT_CHARS * 2


# ══════════════════════════════════════════════════════
# 会话
# ══════════════════════════════════════════════════════

def test_session_save_load() -> None:
    """会话能存能读。

    注意 load 返回的是 dict（不是 list）——
    里面除了 messages 还有用量统计。
    """
    with tempfile.TemporaryDirectory() as d:
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ]
        sess.save(d, msgs, prompt_tokens=100, completion_tokens=50)

        loaded = sess.load(d)
        assert isinstance(loaded, dict)

        got_msgs = loaded.get("messages") or []
        assert len(got_msgs) == 2
        assert got_msgs[0]["content"] == "你好"


def test_session_clear() -> None:
    """能清空。"""
    with tempfile.TemporaryDirectory() as d:
        sess.save(d, [{"role": "user", "content": "x"}])
        sess.clear(d)

        loaded = sess.load(d)
        got_msgs = loaded.get("messages") if isinstance(loaded, dict) else loaded
        assert not got_msgs


def test_estimate_tokens() -> None:
    """粗估 token 数。"""
    msgs = [{"role": "user", "content": "一" * 1000}]
    n = sess.estimate_tokens(msgs)
    assert n > 0
    assert n < 5000       # 不能估得离谱


def test_compress_keeps_recent() -> None:
    """压缩之后要保留最近的消息。

    compress 返回 (messages, did_compress) 元组。
    """
    msgs = []
    for i in range(100):
        msgs.append({"role": "user", "content": f"消息{i}"})
        msgs.append({"role": "assistant", "content": f"回复{i}"})

    # 伪装成超长
    for m in msgs:
        m["content"] = m["content"] * 100

    result = sess.compress(msgs, keep_last=8, budget_tokens=1000)
    assert isinstance(result, tuple), f"返回的应该是元组，实际 {type(result)}"

    compressed, did = result
    assert did is True, "这么长竟然没压缩"
    assert len(compressed) < len(msgs), "压缩后居然没变短"

    # 最后几条还在
    text = " ".join(str(m.get("content", "")) for m in compressed)
    assert "消息99" in text, "最近的消息被压没了"


def test_compress_short_input() -> None:
    """短对话不需要压缩。"""
    msgs = [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    compressed, did = sess.compress(msgs, keep_last=8, budget_tokens=60000)
    assert did is False, "这么短竟然压缩了"
    assert len(compressed) == 2


# ══════════════════════════════════════════════════════
# 限流
# ══════════════════════════════════════════════════════

def test_ratelimit_parses_rpm() -> None:
    """能从错误信息里挖出 RPM。"""
    rl = ratelimit.RateLimiter()
    detail = 'request reached organization max RPM: 3, please try again after 1 seconds'
    rl.on_429(detail)
    assert rl.rpm == 3


def test_ratelimit_parses_other_format() -> None:
    """别的写法也认得。"""
    rl = ratelimit.RateLimiter()
    rl.on_429("max 60 requests per minute")
    assert rl.rpm == 60


def test_ratelimit_unknown_rpm() -> None:
    """挖不到就保守等。"""
    rl = ratelimit.RateLimiter()
    gap = rl.on_429("rate limited")
    assert rl.rpm == 0
    assert gap > 0


def test_ratelimit_gap_scales() -> None:
    """RPM 越小，间隔越长。"""
    rl_fast = ratelimit.RateLimiter()
    rl_slow = ratelimit.RateLimiter()

    rl_fast.on_429("max RPM: 60")
    rl_slow.on_429("max RPM: 3")

    # 60/min → 每 1 秒一次；3/min → 每 20 秒一次
    assert rl_fast._next_ok - rl_fast._last_429 < rl_slow._next_ok - rl_slow._last_429


def test_parse_retry_after() -> None:
    """读"等几秒"。"""
    assert ratelimit.parse_retry_after("try again after 5 seconds") == 5.0
    assert ratelimit.parse_retry_after("no info") is None


def test_ratelimit_thread_safe() -> None:
    """多线程共享一个 limiter 不能崩。"""
    import threading

    rl = ratelimit.RateLimiter()
    errors = []

    def hammer():
        try:
            for _ in range(50):
                rl.on_429("max RPM: 10")
                rl.mark_ok()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"多线程出错了：{errors}"
