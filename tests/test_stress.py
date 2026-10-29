"""压力测试：把系统推到极限。

这些不是"功能对不对"，是"撑不撑得住"：
    · 大量并发请求
    · 超大输入
    · 快速连续操作
    · 资源耗尽

跑得慢，但能暴露真正的问题。
"""

import json
import os
import sys
import tempfile
import threading
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import guard  # noqa: E402
import safe_path  # noqa: E402


# ══════════════════════════════════════════════════════
# 压力 1：海量名字校验
# ══════════════════════════════════════════════════════

def test_10k_random_names() -> None:
    """一万个随机名字，不能崩、不能漏放危险名。"""

    import random
    import string

    random.seed(42)

    chars = string.printable + "中文测试\uFF0E\u200B\u2215"

    bad_passed = []
    for _ in range(10000):
        n = "".join(random.choice(chars) for _ in range(random.randint(0, 20)))
        try:
            safe_path.safe_name(n)
        except ValueError:
            continue
        # 通过了——检查有没有带危险字符的漏了
        if any(c in n for c in "/\\:*?\"<>|"):
            bad_passed.append(n)

    assert not bad_passed, f"危险名漏放了：{bad_passed[:5]}"


def test_10k_safe_names_pass() -> None:
    """一万个合法名字不能被误伤。

    ⚠️ 这条测试修过一次，因为断言写错了。

        第一版：随机生成 1 万个"看起来合法"的名字，要求 90% 通过。
        结果只过了 8457 个，我以为有 bug。

        实际查下来：
            761 个以 - 开头   → 拒得对（防命令行参数注入）
            739 个结尾是点    → 拒得对（Windows 会静默去掉点，名字会对不上）
             27 个是 "."      → 拒得对

        全部都是正确行为。是我的生成器里包含了 15% 的
        "本来就该拒"的字符串。

    现在：生成时避开这几种情况，要求 100% 通过。
    这样如果真有误伤，测试立刻报出来。
    """
    import random

    random.seed(7)
    chars = "abc中文测试09 _-."

    tested = 0
    failed = []
    for _ in range(10000):
        raw = "".join(
            random.choice(chars) for _ in range(random.randint(1, 40))
        )
        n = raw.strip()

        # 这几种本来就该拒，生成时跳过
        if not n:
            continue
        if n.startswith("-"):
            continue
        if n.endswith("."):
            continue
        if n in (".", ".."):
            continue
        # Windows 保留名
        if n.split(".")[0].upper() in safe_path._RESERVED:
            continue

        tested += 1
        try:
            safe_path.safe_name(n)
        except ValueError as e:
            failed.append((n, str(e)))

    assert tested > 5000, f"只生成了 {tested} 个名字，样本太少"
    assert not failed, f"误伤了 {len(failed)} 个：{failed[:5]}"


# ══════════════════════════════════════════════════════
# 压力 2：并发验证码操作
# ══════════════════════════════════════════════════════

def test_concurrent_verify_attack() -> None:
    """模拟有人暴力猜验证码。

    10 个线程同时猜，每线程猜 100 次。
    必须：只有 3 次能真正"试"，之后就锁死。
    """
    guard._state.pending.clear()
    guard._state.locked_until.clear()
    guard._state.fails.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()

    task = "压力测试任务"
    fp = guard._fingerprint(task)
    guard._state.pending[fp] = guard.PendingCode(
        code="555555", task=task, purpose="", expires=time.time() + 120
    )

    results = []
    lock = threading.Lock()

    def guesser():
        local = []
        for i in range(100):
            try:
                ok, msg = guard.verify(task, f"{i:06d}")
                local.append((ok, msg))
            except Exception as e:  # noqa: BLE001
                local.append((False, f"崩溃:{e}"))
        with lock:
            results.extend(local)

    threads = [threading.Thread(target=guesser) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 统计
    wrong_tries = [r for r in results if "还能试" in r[1]]
    assert len(wrong_tries) <= 3, \
        f"被猜了 {len(wrong_tries)} 次，超过 3 次上限"

    # 最后一定是锁死的
    r = guard.check(task)
    assert r["fenced"] is True

    # 清理
    guard._state.pending.clear()
    guard._state.locked_until.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()


def test_concurrent_check_doesnt_corrupt() -> None:
    """大量并发 check，状态不能被搞乱。"""
    guard._state.pending.clear()
    guard._state.locked_until.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()

    task = "并发检查"
    fp = guard._fingerprint(task)
    guard._state.pending[fp] = guard.PendingCode(
        code="123456", task=task, purpose="", expires=time.time() + 60
    )

    errors = []

    def checker():
        try:
            for _ in range(200):
                r = guard.check("随机的任务")
                assert isinstance(r, dict)
                assert "fenced" in r
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=checker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 check 崩了：{errors[:3]}"

    guard._state.pending.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()


# ══════════════════════════════════════════════════════
# 压力 3：超大输入
# ══════════════════════════════════════════════════════

def test_huge_task_text() -> None:
    """超大任务文本不能把危险词检测搞崩。"""
    big = "正常的活 " * 100000      # 50 万字符

    t0 = time.time()
    r = guard.looks_dangerous(big)
    elapsed = time.time() - t0

    assert r is False
    assert elapsed < 2.0, f"检测用了 {elapsed:.1f} 秒，太慢"


def test_huge_dangerous_text() -> None:
    """超长的危险文本也要能认出来。"""
    big = "正常的活 " * 50000 + " rm -rf / "
    assert guard.looks_dangerous(big) is True


def test_many_pending_codes() -> None:
    """大量待验证的码不能把系统拖垮。

    正常最多一两个，但万一有 bug 堆了 1000 个，
    check 不能变成 O(n) 的噩梦。
    """
    guard._state.pending.clear()

    for i in range(1000):
        fp = guard._fingerprint(f"任务{i}")
        guard._state.pending[fp] = guard.PendingCode(
            code="000000", task=f"任务{i}", purpose="",
            expires=time.time() + 3600,
        )

    t0 = time.time()
    for _ in range(100):
        guard.check("随便")
    elapsed = time.time() - t0

    assert elapsed < 3.0, f"1000 个 pending 时 check 太慢：{elapsed:.1f}s"

    guard._state.pending.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()


# ══════════════════════════════════════════════════════
# 压力 4：文件系统
# ══════════════════════════════════════════════════════

def test_many_files_in_workspace() -> None:
    """工作目录里塞 500 个文件，list_dir 不能挂。"""
    from engine import tools

    with tempfile.TemporaryDirectory() as d:
        old = tools._WORKDIR
        tools.set_workdir(d)
        try:
            for i in range(500):
                with open(os.path.join(d, f"file{i}.txt"), "w") as f:
                    f.write(f"content {i}")

            t0 = time.time()
            r = tools.list_dir(".")
            elapsed = time.time() - t0

            assert elapsed < 3.0, f"列 500 个文件用了 {elapsed:.1f}s"
            assert "file0.txt" in r
        finally:
            tools.set_workdir(old)


def test_deep_directory_tree() -> None:
    """很深的目录树。"""
    from engine import tools

    with tempfile.TemporaryDirectory() as d:
        old = tools._WORKDIR
        tools.set_workdir(d)
        try:
            p = d
            for i in range(20):
                p = os.path.join(p, f"level{i}")
                os.makedirs(p, exist_ok=True)
            with open(os.path.join(p, "deep.txt"), "w") as f:
                f.write("deep")

            r = tools.read_file(
                os.path.relpath(os.path.join(p, "deep.txt"), d)
            )
            assert "deep" in r
        finally:
            tools.set_workdir(old)


# ══════════════════════════════════════════════════════
# 压力 5：连续快速操作
# ══════════════════════════════════════════════════════

def test_rapid_send_verify_cycles() -> None:
    """快速来回：发码→验证→发码→验证，来回 100 次不能乱。"""
    guard._state.pending.clear()
    guard._state.locked_until.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()

    task = "快速循环"
    for i in range(100):
        fp = guard._fingerprint(task)
        guard._state.pending[fp] = guard.PendingCode(
            code=f"{i:06d}", task=task, purpose="",
            expires=time.time() + 60,
        )
        ok, msg = guard.verify(task, f"{i:06d}")
        assert ok is True, f"第 {i} 轮验证失败：{msg}"

    # 最后应该是干净的
    r = guard.check("无关")
    assert r["fenced"] is False

    guard._state.pending.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()
