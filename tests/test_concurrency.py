"""并发、调度、模型配置的回归测试。

这块的问题都是"跑起来才暴露"的：
    · 路径少算一级 → 并发控制静默失效
    · 先派活后检查 → 自己给自己占位
    · 回调存共享变量 → 并发时互相覆盖

所以测试要真的并发跑，不能只测单线程。
"""

import json
import os
import sys
import tempfile
import threading
import time

import pytest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import engine.scheduler as sched  # noqa: E402
from engine import profiles as prof_mod  # noqa: E402


# ══════════════════════════════════════════════════════
# profiles：配置管理
# ══════════════════════════════════════════════════════

@pytest.fixture
def clean_profiles():
    """临时换一个配置路径，别动真实配置。"""
    import engine.profiles as P

    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = d
        try:
            yield d
        finally:
            if old:
                os.environ["APPDATA"] = old
            else:
                os.environ.pop("APPDATA", None)


def test_add_and_load(clean_profiles: str) -> None:
    """加一套配置能读回来。"""
    p = prof_mod.add_profile({
        "name": "测试",
        "base_url": "https://api.test.com",
        "api_key": "sk-test",
        "model": "test-model",
    })
    assert p["id"]

    all_data = prof_mod.load_all()
    assert len(all_data["profiles"]) == 1
    assert all_data["profiles"][0]["name"] == "测试"


def test_update_profile(clean_profiles: str) -> None:
    """改一套。"""
    p = prof_mod.add_profile({"name": "改之前", "model": "m1"})
    ok = prof_mod.update_profile(p["id"], {"name": "改之后"})
    assert ok

    got = prof_mod.get_profile(p["id"])
    assert got["name"] == "改之后"


def test_update_keeps_key_when_empty(clean_profiles: str) -> None:
    """key 传空表示不改，不是清空。

    这条很重要：用户改个名字，不该把 key 弄丢。
    """
    p = prof_mod.add_profile({"name": "x", "api_key": "sk-original"})
    prof_mod.update_profile(p["id"], {"name": "y", "api_key": ""})

    got = prof_mod.get_profile(p["id"])
    assert got["api_key"] == "sk-original"


def test_delete_profile(clean_profiles: str) -> None:
    p = prof_mod.add_profile({"name": "要删的"})
    assert prof_mod.delete_profile(p["id"])
    assert prof_mod.get_profile(p["id"]) == {}


def test_concurrency_clamped(clean_profiles: str) -> None:
    """并发数会被夹在 1-20 之间。"""
    p = prof_mod.add_profile({"name": "x"})

    prof_mod.update_profile(p["id"], {"concurrency": 0})
    assert prof_mod.get_profile(p["id"])["concurrency"] >= 1

    prof_mod.update_profile(p["id"], {"concurrency": 999})
    assert prof_mod.get_profile(p["id"])["concurrency"] <= 20


def test_default_profile(clean_profiles: str) -> None:
    """默认配置选第一个启用的。"""
    prof_mod.add_profile({"name": "A", "model": "m1", "base_url": "x",
                          "api_key": "k", "enabled": False})
    prof_mod.add_profile({"name": "B", "model": "m2", "base_url": "x",
                          "api_key": "k", "enabled": True})

    d = prof_mod.default_profile()
    assert d.get("name") == "B"


def test_enabled_only(clean_profiles: str) -> None:
    """只返回启用且配好的。"""
    prof_mod.add_profile({"name": "好的", "model": "m", "base_url": "u",
                          "api_key": "k", "enabled": True})
    prof_mod.add_profile({"name": "关的", "model": "m", "base_url": "u",
                          "api_key": "k", "enabled": False})
    prof_mod.add_profile({"name": "缺key", "model": "m", "base_url": "u",
                          "enabled": True})

    names = [p["name"] for p in prof_mod.enabled_profiles()]
    assert "好的" in names
    assert "关的" not in names
    assert "缺key" not in names


def test_migration_from_old_format() -> None:
    """老格式（单套配置）能自动升级成多套。"""
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = d
        try:
            os.makedirs(os.path.join(d, "dsh-bridge"), exist_ok=True)
            old_cfg = {
                "base_url": "https://api.deepseek.com",
                "api_key": "sk-old",
                "model": "deepseek-chat",
            }
            with open(os.path.join(d, "dsh-bridge", "engine.json"), "w") as f:
                json.dump(old_cfg, f)

            data = prof_mod.load_all()
            assert len(data["profiles"]) == 1
            assert data["profiles"][0]["model"] == "deepseek-chat"
            assert data["profiles"][0]["api_key"] == "sk-old"
        finally:
            if old:
                os.environ["APPDATA"] = old


# ══════════════════════════════════════════════════════
# scheduler：并发和排队
# ══════════════════════════════════════════════════════

def test_scheduler_path_is_bridge_root() -> None:
    """★ scheduler 的路径必须指向桥根目录，不是 engine/。

    这条踩过：写成 dirname(__file__) 算出来是 engine/ 自己，
    于是读的是 engine/tasks.jsonl——那个文件不存在，
    读出来永远空列表，**并发控制完全失效**。
    而且不报错，日志一片安宁。
    """
    p = sched._tasks_path()
    assert os.path.basename(os.path.dirname(p)) != "engine", \
        f"scheduler 路径错了：{p}"
    assert p.endswith("tasks.jsonl")


def test_running_count_includes_pending() -> None:
    """★ 占位数要包含 pending。

    踩过：只数 running，于是连着派 3 个任务时它们全是 pending，
    检查都通过 → 3 个一起起来，并发上限等于没有。
    """
    with tempfile.TemporaryDirectory() as d:
        old_dir = sched.BRIDGE_DIR
        sched.BRIDGE_DIR = d
        try:
            tasks = [
                {"id": "a", "profile": "p1", "status": "running"},
                {"id": "b", "profile": "p1", "status": "pending"},
                {"id": "c", "profile": "p1", "status": "ok"},
                {"id": "d", "profile": "p2", "status": "running"},
            ]
            with open(os.path.join(d, "tasks.jsonl"), "w") as f:
                for t in tasks:
                    f.write(json.dumps(t) + "\n")

            assert sched.running_count("p1") == 2, "pending 没算进去"
            assert sched.running_count("p2") == 1
        finally:
            sched.BRIDGE_DIR = old_dir


def test_has_slot() -> None:
    """有空位判断。"""
    with tempfile.TemporaryDirectory() as d:
        old_dir = sched.BRIDGE_DIR
        sched.BRIDGE_DIR = d
        try:
            with open(os.path.join(d, "tasks.jsonl"), "w") as f:
                f.write(json.dumps({"id": "a", "profile": "p",
                                    "status": "running"}) + "\n")

            assert sched.has_slot("p", 2) is True
            assert sched.has_slot("p", 1) is False
            assert sched.has_slot("p", 0) is False    # 0 也能当 1 处理
            assert sched.has_slot("other", 1) is True
        finally:
            sched.BRIDGE_DIR = old_dir


def test_queue_position() -> None:
    """排队位置。"""
    with tempfile.TemporaryDirectory() as d:
        old_dir = sched.BRIDGE_DIR
        sched.BRIDGE_DIR = d
        try:
            tasks = [
                {"id": "a", "profile": "p", "status": "queued",
                 "created_at": "2026-01-01T00:00:01"},
                {"id": "b", "profile": "p", "status": "queued",
                 "created_at": "2026-01-01T00:00:02"},
                {"id": "c", "profile": "p", "status": "ok",
                 "created_at": "2026-01-01T00:00:03"},
            ]
            with open(os.path.join(d, "tasks.jsonl"), "w") as f:
                for t in tasks:
                    f.write(json.dumps(t) + "\n")

            assert sched.queue_position("p", "a") == 1
            assert sched.queue_position("p", "b") == 2
            assert sched.queue_position("p", "c") == 0    # 不在队列
        finally:
            sched.BRIDGE_DIR = old_dir


def test_pump_respects_concurrency() -> None:
    """pump 启动任务时不能超过并发上限。"""
    with tempfile.TemporaryDirectory() as d:
        old_dir = sched.BRIDGE_DIR
        sched.BRIDGE_DIR = d

        # 造 5 个排队任务
        tasks = [
            {"id": f"t{i}", "profile": "p", "status": "queued",
             "created_at": f"2026-01-01T00:00:0{i}"}
            for i in range(5)
        ]
        with open(os.path.join(d, "tasks.jsonl"), "w") as f:
            for t in tasks:
                f.write(json.dumps(t) + "\n")

        started = []

        def fake_start(task_id: str) -> None:
            started.append(task_id)
            # 模拟"进程起来了"
            recs = []
            for line in open(os.path.join(d, "tasks.jsonl")):
                r = json.loads(line)
                if r["id"] == task_id:
                    r["status"] = "running"
                recs.append(r)
            with open(os.path.join(d, "tasks.jsonl"), "w") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")

        old_start = sched.start_worker
        sched.start_worker = fake_start
        try:
            import engine.profiles as P
            old_load = P.load_all
            P.load_all = lambda: {
                "active": "p",
                "profiles": [{
                    "id": "p", "name": "测试", "enabled": True,
                    "base_url": "u", "api_key": "k", "model": "m",
                    "concurrency": 2,
                }],
            }
            try:
                n = sched.pump()
                assert n == 2, f"应该启动 2 个，实际 {n}"
                assert len(started) == 2
            finally:
                P.load_all = old_load
        finally:
            sched.start_worker = old_start
            sched.BRIDGE_DIR = old_dir


# ══════════════════════════════════════════════════════
# 并发安全：多个线程同时读写
# ══════════════════════════════════════════════════════

def test_concurrent_task_writes() -> None:
    """多线程同时调桥的 _write_tasks，不能崩。

    ⚠️ 这条测的是**桥自己的写入函数**，不是我手写的。

        第一次写这个测试时，我在测试里自己实现了
        "写 tmp + os.replace"，结果 5 个线程一起跑就崩：
            PermissionError(13, '另一个程序正在使用此文件')

        但那测的是我的测试代码，不是产品代码。
        桥的 _write_tasks 有重试（50 次，约 5 秒），
        所以要用它来测才有意义。
    """
    import bridge

    with tempfile.TemporaryDirectory() as d:
        old = bridge.TASKS
        bridge.TASKS = os.path.join(d, "tasks.jsonl")
        try:
            errors = []

            def writer(tid: str):
                try:
                    for i in range(20):
                        # 每次都全量写（模拟真实的"读-改-写"）
                        tasks = [
                            {"id": f"{tid}-{j}", "profile": "p",
                             "status": "ok"}
                            for j in range(i + 1)
                        ]
                        bridge._write_tasks(tasks)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{tid}: {type(e).__name__}: {e}")

            threads = [
                threading.Thread(target=writer, args=(f"t{i}",))
                for i in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            assert not errors, f"并发写崩了：{errors[:3]}"
            assert os.path.exists(bridge.TASKS), "文件没了"

            # 内容还是合法的 JSON 行
            with open(bridge.TASKS, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        json.loads(line)     # 不崩就说明没写坏
        finally:
            bridge.TASKS = old


def test_write_tasks_retries_on_permission_error() -> None:
    """os.replace 报 PermissionError 时要重试，不能直接放弃。"""
    import bridge

    with tempfile.TemporaryDirectory() as d:
        old_tasks = bridge.TASKS
        bridge.TASKS = os.path.join(d, "tasks.jsonl")
        try:
            calls = {"n": 0}
            real_replace = os.replace

            def flaky_replace(src, dst):
                """前三次故意失败。"""
                calls["n"] += 1
                if calls["n"] <= 3:
                    raise PermissionError(13, "另一个程序正在使用此文件")
                return real_replace(src, dst)

            old_replace = os.replace
            os.replace = flaky_replace
            try:
                bridge._write_tasks([{"id": "x", "status": "ok"}])
            finally:
                os.replace = old_replace

            assert calls["n"] > 3, "没重试"
            assert os.path.exists(bridge.TASKS), "重试之后应该写进去了"
        finally:
            bridge.TASKS = old_tasks
