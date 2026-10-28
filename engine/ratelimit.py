"""限流：让引擎扛过任何厂商的频率限制。

为什么必须做这个
    用户会拿任何厂商的 Key 来用。实测 kimi-k2.6 的免费额度是
    **每分钟 3 次请求**，而跑一个任务要好几轮，很容易撞上。

    如果只是抛个 429 让用户看，等于说"你的 Key 不够好，换一个"——
    那是把我们的问题推给用户。

    但也不能傻等：
        · 服务端说"after 1 second"，等 1 秒其实没用——
          它的限制是每分钟 3 次，等 1 秒还在同一个窗口里。
        · 等太久又显得卡死。

所以这里做两件事：
    1. 从错误信息里读它的真实限制（"max RPM: 3" 这种）
    2. 按限制算出"下一次安全时刻"，而不是听它的 after N seconds
"""

from __future__ import annotations

import re
import threading
import time


class RateLimiter:
    """按分钟配额排队。

    用法：
        limiter = RateLimiter()
        limiter.on_429(error_detail)     # 撞限流了，它会记下配额
        limiter.wait()                   # 发请求前等一下，保证不超
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 每分钟允许多少次。0 表示还不知道。
        self._rpm = 0
        # 最近一次被限流的时间
        self._last_429 = 0.0
        # 下一次最早什么时候能发
        self._next_ok = 0.0

    def on_429(self, detail: str) -> float:
        """撞到 429 了。返回建议等待的秒数。

        先试着从错误信息里挖出真实配额：
            "request reached organization max RPM: 3"
        """
        with self._lock:
            m = re.search(r"max\s*RPM[:\s]+(\d+)", detail or "", re.I)
            if not m:
                m = re.search(r"(\d+)\s*(?:requests?|calls?)\s*per\s*minute", detail or "", re.I)
            if m:
                try:
                    rpm = int(m.group(1))
                    if 0 < rpm <= 6000:
                        self._rpm = rpm
                except ValueError:
                    pass

            if self._rpm > 0:
                # 按配额算：一分钟 rpm 次，那每次之间至少隔 60/rpm 秒
                # 多留 20% 余量，免得卡在边界上
                gap = (60.0 / self._rpm) * 1.2
            else:
                # 不知道配额，就用保守的指数退避
                gap = 5.0

            now = time.time()
            self._last_429 = now
            self._next_ok = now + gap
            return gap

    def wait(self) -> None:
        """发请求前调用。如果还没到安全时刻就等。"""
        with self._lock:
            target = self._next_ok
        now = time.time()
        if target > now:
            time.sleep(min(60.0, target - now))

    def mark_ok(self) -> None:
        """请求成功了。清掉"下次最早时间"。

        不清配额——配额是这家服务商的客观事实，
        留着它才能算出正确的间隔。
        """
        with self._lock:
            self._next_ok = 0.0

    @property
    def rpm(self) -> int:
        return self._rpm


def parse_retry_after(detail: str) -> float | None:
    """从错误信息里读"该等几秒"。

    各家写法：
        kimi:    please try again after 1 seconds
        openai:  Retry-After 头（读不到，只能看正文）
    读不到返回 None。
    """
    m = re.search(r"after\s+(\d+(?:\.\d+)?)\s*second", detail or "", re.I)
    if m:
        try:
            return max(1.0, float(m.group(1)))
        except ValueError:
            return None
    return None
