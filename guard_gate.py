"""把「拦截计数 → 三次锁死」接进 guard。

老爸的设计
    锁不是状态，是**关卡**。每次派活都要过。

        派活   → 锁检测 → 正常 → 放行
        干活   → 锁检测 → 正常 → 过
        交割   → 锁检测 → 正常 → 推回

    检测到不安全：

        第 1 次 → 拦截，告知用户
        第 2 次 → 拦截，告知用户
        第 3 次 → **彻底锁死任务端**，走原来那套验证码机制

    计数的规矩（这是关键）：

        不按"同一件事"计数 —— 攻击者会不停换花样试，
        每一种只试一次就永远到不了三次。

        按**当天总次数**计数 —— 换什么花样都算一次。

        验证码通过 / 锁满 10 分钟 → 计数重置。

        换来的是：攻击者要突破就得反复等 10 分钟、反复拿邮箱验证码 ——
        时间成本。

计数的存储
    走 count_store：主文件 + 影子 + 签名 + 标记文件。
    任务端改哪个都会被回滚，全改了按拦满处理。
"""

from __future__ import annotations

import time


def _store():
    """延迟导入 count_store。

    为什么不在模块顶层 import：
        count_store 会读密钥、碰 DPAPI，在 import 阶段做这些
        会让 guard 的加载变慢，而且一旦出错连 guard 都用不了。
    """
    try:
        import count_store as c

        return c
    except Exception:  # noqa: BLE001
        return None


# ══════════════════════════════════════════════════════
# 拦截计数
# ══════════════════════════════════════════════════════

# 当天被拦多少次就彻底锁死。
#
# 为什么是 3：
#   第 1 次可能是用户口误、用词夸张（"删了算了"其实是想重写）。
#   第 2 次给他确认的机会。
#   第 3 次就说明确实在反复尝试了 —— 要么是攻击，要么是真要干危险的事，
#   两种都该走验证码。
TRIP_COUNT = 3


def intercept(task: str, reason: str = "") -> dict:
    """记一次拦截。

    返回：
        {
            "count": 今天第几次被拦
            "tripped": 是不是已经到三次了（该锁死了）
            "message": 给用户看的话
        }
    """
    store = _store()

    if store is None:
        # 存储不可用：保守处理，直接当成已触发。
        #
        # ⚠️ 这里故意"从严"：
        #    存储坏了要是不可能计数，那"三次锁死"就永远不会触发。
        #    宁可让用户多走一次验证码，也不能让攻击者因为"存储坏了"得手。
        return {
            "count": TRIP_COUNT,
            "tripped": True,
            "message": "拦截计数不可用，按最严处理。这是第 3 次，任务端锁死。",
        }

    try:
        d = store.bump()
        n = int(d.get("count", 0))
        tamper = int(d.get("tamper", 0))

        tripped = n >= TRIP_COUNT

        if tripped:
            msg = (
                f"今天这是第 {n} 次被拦下了，任务端锁死。\n"
                f"要用的话先验证身份（验证码发到邮箱）。"
            )
        elif n == 1:
            msg = (
                f"这个活看着不太安全：{reason or '涉及不可逆操作'}\n"
                f"这是今天第 1 次被拦。真要做的话跟我说清楚，我再确认一次。"
            )
        else:
            left = TRIP_COUNT - n
            msg = (
                f"又拦下一件不太安全的活：{reason or '涉及不可逆操作'}\n"
                f"今天已经拦了 {n} 次，还剩 {left} 次机会——再拦一次任务端就锁死了。"
            )

        if tamper > 0:
            # 篡改痕迹要暴露出来，别藏着。
            msg += f"\n（注意：拦截计数文件被改过 {tamper} 次，已经自动恢复了。）"

        return {"count": n, "tripped": tripped, "message": msg, "tamper": tamper}

    except Exception as e:  # noqa: BLE001
        return {
            "count": TRIP_COUNT,
            "tripped": True,
            "message": f"计数出错（{type(e).__name__}），按最严处理。",
        }


def peek_count() -> dict:
    """看一眼今天被拦了几次，不改。"""
    store = _store()
    if store is None:
        return {"count": 0, "tripped": False, "tamper": 0, "available": False}
    try:
        d = store.peek()
        n = int(d.get("count", 0))
        return {
            "count": n,
            "tripped": n >= TRIP_COUNT,
            "tamper": int(d.get("tamper", 0)),
            "available": True,
        }
    except Exception:  # noqa: BLE001
        return {"count": 0, "tripped": False, "tamper": 0, "available": False}


def reset_after_pass() -> None:
    """验证码通过、或者锁满之后，把当天计数清零。

    这是设计的一部分：不重置的话，用户一天只能犯规一次，
    之后每一个小口误都会让他锁死。
    """
    store = _store()
    if store is None:
        return
    try:
        store.reset_count()
    except Exception:  # noqa: BLE001
        pass


# ══════════════════════════════════════════════════════
# 关卡：每次派活都要过
# ══════════════════════════════════════════════════════

def _guard():
    """拿 guard 模块。延迟导入，避免循环依赖。"""
    import guard as g

    return g


def gate(task: str) -> dict:
    """派活前的关卡。

    这是所有派活入口唯一的必经之路。

    返回：
        {
            "allow": 能不能过
            "reason": 为什么
            "count": 今天第几次
        }

    三种情况：
        一、当前有围栏（验证码等着填 / 锁着）→ 不许过
        二、任务看着不安全 → 记一次拦截，不许过，告诉他第几次了
        三、其他 → 放行
    """
    # 一、先看有没有正在生效的围栏
    g = _guard()
    st = g.check(task)
    if st.get("fenced"):
        return {
            "allow": False,
            "reason": st.get("reason") or "任务端锁着",
            "count": peek_count().get("count", 0),
            "fenced": True,
        }

    # 二、今天的拦截次数到顶了吗
    c = peek_count()
    if c.get("tripped"):
        return {
            "allow": False,
            "reason": (
                f"今天已经被拦了 {c['count']} 次，任务端锁死。"
                f"要用的话先验证身份。"
            ),
            "count": c["count"],
            "tripped": True,
        }

    # 三、**不再靠任务描述判断危险。**
    #
    #    原来这里有一句 looks_dangerous(task)——按关键词猜。
    #    后来发现这条路走不通：
    #
    #        · 危险的说法列不完。"改个权限"就没有对应的词，
    #          而它确实不可逆。
    #        · 任务描述会骗人。有人让你派活，写个无关紧要的描述就行。
    #
    #    所以判断挪到了**动作层**（engine/agent.py 的 _check_action）：
    #    看它实际调了什么工具、传了什么参数。
    #    工具调用做不了假——真要删文件，就必须调删除。
    #
    #    这里只做两件事：
    #        1. 围栏开着吗（上面第一步）
    #        2. 今天拦截次数到顶了吗（上面第二步）
    #
    #    到这一步了就是放行。真正的把关在动作层。
    return {"allow": True, "reason": "", "count": c.get("count", 0)}
