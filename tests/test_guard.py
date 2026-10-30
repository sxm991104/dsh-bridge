"""危险操作验证码的状态机测试。

这是整个安全里最复杂的一块，状态多、转换多。
每个边界都得测到，尤其是：

    · 发码之后**普通任务也派不出去**（全站围栏）
    · 验证码过期 → 锁 10 分钟，但可以重发
    · 错满 3 次 → 强制锁 10 分钟，**不许重发**
    · 锁满 → 自动解开

这块最怕的是"测了自己锁死"——所以每个测试结束后
都强制清理，用 fixture 保证。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import guard  # noqa: E402


# ══════════════════════════════════════════════════════
# fixture：每个测试前后都清干净
# ══════════════════════════════════════════════════════

def _reset() -> None:
    guard._state.pending.clear()
    guard._state.locked_until.clear()
    guard._state.fails.clear()
    guard._locked.clear()
    guard._force_locked.clear()
    guard._verified.clear()

    # ⚠️ 还要清"今天被拦了几次"。
    #
    #    这个计数是持久化的（count_store 写文件），
    #    不清的话会跨测试累积。
    #
    #    实测：跑完整套 test_guard 之后计数累积到 11，
    #    于是最后那条"解锁后能派活"的测试被锁死了——
    #    它测的是别的东西，但被前面测试留下的状态干掉了。
    #
    #    测试之间必须互不影响。
    try:
        import count_store

        count_store.reset_count()
        d = count_store.load()
        d["tamper"] = 0
        count_store.save(d)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def clean_state():
    """每个测试前后都清干净。

    ⚠️ 这个 autouse 很要紧：围栏状态是全局的，
        一个测试里触发了围栏没清，后面所有测试都会挂。
        实测过——后来所有派活测试都过不了。
    """
    _reset()
    yield
    _reset()


def _put(task: str, code: str = "123456", ttl: float = 120) -> str:
    """塞一个待验证的码。不发真邮件。"""
    fp = guard._fingerprint(task)
    guard._state.pending[fp] = guard.PendingCode(
        code=code, task=task, purpose="", expires=time.time() + ttl
    )
    return fp


# ══════════════════════════════════════════════════════
# A. 平时状态
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("task", [
    "把登录页按钮改大一点",
    "看看今天的日志",
    "读一下 config.json",
    "跑一下测试",
])
def test_normal_task_not_fenced(task: str) -> None:
    """平时普通任务随便派。"""
    r = guard.check(task)
    assert r["fenced"] is False
    assert r["verified"] is True
    assert r["waiting"] is False
    assert r["lock_left"] == 0


def test_initial_state_clean() -> None:
    """刚启动时没有任何锁。"""
    r = guard.check("任何任务")
    assert r["fenced"] is False
    assert r["forced"] is False
    assert r["can_resend"] is True


# ══════════════════════════════════════════════════════
# B. 发码 → 全站围栏
# ══════════════════════════════════════════════════════

def test_code_sent_fences_everything() -> None:
    """★ 最核心的一条：发了码，所有任务都派不出去。"""
    danger = "删掉生产环境用户表"
    _put(danger)

    # 危险任务自己
    r = guard.check(danger)
    assert r["fenced"] is True
    assert r["waiting"] is True

    # 完全无关的任务
    for other in ("把按钮改大", "看看日志", "写个文档"):
        r = guard.check(other)
        assert r["fenced"] is True, f"「{other}」竟然没被锁"


def test_fence_reason_mentions_waiting() -> None:
    """锁住的时候要告诉用户"在等验证码"。"""
    _put("删库")
    r = guard.check("无关任务")
    assert "验证码" in r["reason"]


# ══════════════════════════════════════════════════════
# C. 验证通过 → 解锁
# ══════════════════════════════════════════════════════

def test_correct_code_unlocks() -> None:
    """验证码对了，全部解锁。"""
    danger = "删库"
    _put(danger, "654321")

    ok, msg = guard.verify(danger, "654321")
    assert ok is True

    assert guard.check(danger)["fenced"] is False
    assert guard.check("无关任务")["fenced"] is False


def test_code_is_one_time() -> None:
    """验证码只能用一次。"""
    danger = "删库"
    _put(danger, "111111")

    ok1, _ = guard.verify(danger, "111111")
    assert ok1 is True

    ok2, _ = guard.verify(danger, "111111")
    assert ok2 is False, "同一个码被用了两次"


def test_code_bound_to_task() -> None:
    """给 A 任务发的码，不能拿去验 B 任务。"""
    a = "删掉数据库A"
    b = "删掉数据库B"
    _put(a, "222222")

    # 拿 A 的码去验 B → 应该失败（B 根本没有 pending）
    ok, msg = guard.verify(b, "222222")
    assert ok is False


# ══════════════════════════════════════════════════════
# D. 验证码过期 → 锁 10 分钟
# ══════════════════════════════════════════════════════

def test_expired_code_locks() -> None:
    """码过期之后仍然锁着，而且要锁 10 分钟。"""
    danger = "删库"
    fp = _put(danger, "333333")
    guard._state.pending[fp].expires = time.time() - 1

    r = guard.check(danger)
    assert r["fenced"] is True
    assert r["waiting"] is False
    assert 580 <= r["lock_left"] <= 600


def test_expired_can_resend() -> None:
    """普通过期允许重新发码。"""
    danger = "删库"
    fp = _put(danger, "444444")
    guard._state.pending[fp].expires = time.time() - 1

    r = guard.check(danger)
    assert r["can_resend"] is True
    assert r["forced"] is False


def test_resend_resets_timer() -> None:
    """重新发码 → 回到等待状态，倒计时重算。"""
    danger = "删库"
    fp = _put(danger, "555555")
    guard._state.pending[fp].expires = time.time() - 1
    guard.check(danger)                    # 触发转锁

    _put(danger, "666666")                 # 重发
    r = guard.check(danger)
    assert r["waiting"] is True
    assert r["lock_left"] == 0             # 等用户填，不算倒计时


# ══════════════════════════════════════════════════════
# E. 错满三次 → 强制锁，不许重发
# ══════════════════════════════════════════════════════

def test_wrong_code_tells_remaining() -> None:
    """错的时候要告诉还剩几次。"""
    danger = "删库"
    _put(danger, "777777")

    _, m1 = guard.verify(danger, "000000")
    assert "2" in m1

    _, m2 = guard.verify(danger, "000000")
    assert "1" in m2


def test_three_wrong_force_locks() -> None:
    """★ 错三次 → 强制锁，不许重发。"""
    danger = "删库"
    _put(danger, "888888")

    for _ in range(3):
        guard.verify(danger, "000000")

    r = guard.check(danger)
    assert r["fenced"] is True
    assert r["forced"] is True
    assert r["can_resend"] is False, "强制锁期间竟然允许重发"


def test_force_lock_blocks_resend(monkeypatch) -> None:
    """强制锁期间，发码请求要被拒。

    ⚠️ 这条原来没打桩，直接调 send_code_email。

        在开发者机器上能过（因为配了真实邮箱），
        但在干净环境（CI、或者没配邮箱的机器）里，
        send_code_email 第一步就返回"还没配安全邮箱"，
        根本走不到锁检查那一步——于是断言 "锁" in msg 失败。

        实测过一次：本机能过，CI 挂。

        现在把发信打桩，让它一定走到"检查锁"那一步。
    """
    danger = "删库"
    _put(danger, "999999")
    for _ in range(3):
        guard.verify(danger, "000000")

    # ⚠️ 这里不打算桩 smtplib——send_code_email 里的发信是内联的，
    #    没有单独的函数可以替换。
    #
    #    换个更实在的做法：给一个假邮箱配置。
    #    这样 send_code_email 能走到"检查锁"那一步，
    #    而真正的发信会因为连不上而失败——但那不影响我们测的东西：
    #    我们要验的是"有锁的时候，它拒绝发"，不是"信能不能发出去"。
    # is_guard_ready() 要三个条件同时成立：
    #     enabled + email + smtp_host
    # 少一个都会返回"还没配安全邮箱"，走到不去后面的锁检查。
    cfg = guard.load_config()
    if not guard.is_guard_ready():
        cfg["enabled"] = True
        cfg["email"] = "test@example.com"
        cfg["smtp_host"] = "smtp.example.com"
        cfg["smtp_port"] = 465
        cfg["smtp_user"] = "test@example.com"
        cfg["smtp_pass"] = "x" * 16
        guard.save_config(cfg)

    ok, msg = guard.send_code_email(danger, "")
    assert ok is False
    assert "锁" in msg


def test_force_lock_blocks_everything() -> None:
    """强制锁期间普通任务也派不出去。"""
    danger = "删库"
    _put(danger, "101010")
    for _ in range(3):
        guard.verify(danger, "000000")

    assert guard.check("无关任务")["fenced"] is True


# ══════════════════════════════════════════════════════
# F. 锁满 → 自动解
# ══════════════════════════════════════════════════════

def test_lock_expires_automatically() -> None:
    """锁满 10 分钟自动解开。"""
    danger = "删库"
    fp = _put(danger, "121212")
    guard._state.pending[fp].expires = time.time() - 1
    guard.check(danger)                    # 触发转锁

    # 手动把锁调到过去
    guard._locked[fp] = time.time() - 1

    r = guard.check("无关任务")
    assert r["fenced"] is False
    assert r["can_resend"] is True


def test_force_lock_expires_automatically() -> None:
    """强制锁也会自己满。"""
    danger = "删库"
    fp = _put(danger, "131313")
    for _ in range(3):
        guard.verify(danger, "000000")

    guard._force_locked[fp] = time.time() - 1
    guard._state.locked_until.clear()

    r = guard.check("无关任务")
    assert r["fenced"] is False


# ══════════════════════════════════════════════════════
# G. 危险词识别
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("task,expected", [
    ("把登录页按钮改大一点", False),
    ("看看今天写了什么", False),
    ("安装一个依赖", False),
    ("跑一下测试", False),
    ("删掉整个数据库", True),
    ("git reset --hard", True),
    ("git push -f", True),
    ("drop table users", True),
    ("rm -rf /", True),
    ("读取 .env 里的配置", True),
    ("清空所有表", True),
    ("回滚到上周", True),
    ("读取 credentials", True),
])
def test_dangerous_detection(task: str, expected: bool) -> None:
    """危险词识别。"""
    assert guard.looks_dangerous(task) is expected


def test_dangerous_is_case_insensitive() -> None:
    """大小写不该影响识别。"""
    assert guard.looks_dangerous("DROP TABLE users")
    assert guard.looks_dangerous("Drop Table users")
    assert guard.looks_dangerous("RM -RF /")
    assert guard.looks_dangerous("Git Reset --hard")


# ══════════════════════════════════════════════════════
# H. 常量（防止手滑改错）
# ══════════════════════════════════════════════════════

def test_ttl_is_120_seconds() -> None:
    """验证码有效期是 120 秒。"""
    assert guard.CODE_TTL == 120


def test_lock_is_600_seconds() -> None:
    """过期后的锁是 10 分钟。"""
    assert guard.LOCK_AFTER_EXPIRE == 600


def test_max_tries_is_three() -> None:
    """最多试 3 次。"""
    assert guard.MAX_TRIES == 3


# ══════════════════════════════════════════════════════
# I. 完整性：随机操作之后状态不能乱
# ══════════════════════════════════════════════════════

def test_state_never_stuck() -> None:
    """一顿乱操作之后，锁满时该能自己解开。

    这条防的是"状态卡住"——比如有个锁记录了但没人清，
    用户就再也派不了活了。实测遇到过。

    ⚠️ 修过一次测试本身：
        原来我在中间又 _put 了一次（重置了 tries），
        结果三次错误实际上是"错1次 + 错2次"，
        强制锁根本没触发，然后断言"锁满能解开"——
        测的跟想测的不是一回事。

        现在改成：真的错满三次，确认强制锁触发了，
        再验证它能自己解开。
    """
    danger = "删库"
    _put(danger, "141414")

    # 真的错三次（不要在中途重置 tries）
    for _ in range(3):
        guard.verify(danger, "000000")

    # 先确认强制锁真的触发了
    r = guard.check(danger)
    assert r["forced"] is True, "三次错误之后竟然没进强制锁"
    assert r["fenced"] is True

    # 中间再随便调几次（模拟用户胡乱操作）
    guard.check("无关")
    guard.check(danger)

    # 把所有锁调到过去
    for d in (guard._locked, guard._force_locked):
        for k in list(d.keys()):
            d[k] = time.time() - 1
    guard._state.locked_until.clear()

    r = guard.check("无关任务")
    assert r["fenced"] is False, "锁满了还解锁不了"

# ══════════════════════════════════════════════════════
# J. 围栏必须拦住**所有**派活入口
# ══════════════════════════════════════════════════════

def test_dispatch_blocked_by_fence() -> None:
    """★ 围栏开着时，cmd_dispatch 一条都不许放。

    这是实测撞出来的漏洞：

        原来的设计是"闸门装在前台插件的 send_to_computer 入口"，
        想着不管模型走哪条路都拦得住。

        但闸门只在插件那条路上。直接调 /api/dispatch，
        或者走桥自己的命令行，都不检查围栏。

        症状：发了验证码（全站该锁死），然后从别的路径派了一条，
              created 和 started 只差 1 秒——**1 秒后就跑起来了**。
    """
    import bridge

    # 先造围栏
    danger = "删掉生产环境用户表"
    _put(danger)
    assert guard.check(danger)["fenced"] is True

    # 从 dispatch 派——应该被拦
    r = bridge.cmd_dispatch("随便什么活", "围栏测试任务")
    assert r.get("status") == "blocked", f"竟然派出去了：{r}"
    assert r.get("blocked") is True
    assert r.get("error")


def test_dispatch_blocks_normal_task_too() -> None:
    """★ 普通任务也拦——围栏是全局的。"""
    import bridge

    _put("删库")

    for txt in ("把按钮改大", "看看日志", "今天天气怎么样"):
        r = bridge.cmd_dispatch(txt, f"围栏-{txt[:4]}")
        assert r.get("status") == "blocked", f"「{txt}」漏过去了"


def test_blocked_task_not_written() -> None:
    """被拦的任务不能写进任务库。"""
    import bridge

    _put("删库")
    before = len(bridge._read_tasks())

    bridge.cmd_dispatch("不该被记录", "检查不落库")

    after = bridge._read_tasks()
    assert len(after) == before
    assert not any(t.get("id") == "检查不落库" for t in after)


def test_dispatch_works_after_unlock() -> None:
    """验证通过后，dispatch 恢复正常。"""
    import bridge

    danger = "删库"
    _put(danger, "888888")
    guard.verify(danger, "888888")

    r = bridge.cmd_dispatch("正常活", "解锁后测试")
    assert r.get("status") == "pending", f"解锁了还派不出去：{r}"

    # 清掉测试任务
    tasks = [t for t in bridge._read_tasks() if t.get("id") != "解锁后测试"]
    bridge._write_tasks(tasks)
