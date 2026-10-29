"""把任务完成通知同时做两件事：

1. 用户能看到（直接发，快、格式清晰、按模型分组）
2. 前台丫头也能知道（写进她的上下文，不打扰用户）

为什么不用"塞进事件队列"那套
    原来推送是构造一个假事件塞进队列，让它走完整管线。
    好处是丫头真的"看到了"，代价很大：

        · 每条推送都要过一遍 LLM（实测 5-30 秒才发出来）
        · 丫头会对每条通知回一句，一个任务收两条消息
        · 飞书不支持引用，整条推送会发送失败

    而用户真正要的是"**知道哪些活干完了，好判断能不能继续
    往同一个窗口派活**"。这不是对话，是状态更新。

    所以拆成两条路：
        用户看的   → 直接发，2 秒内到，分组清晰
        丫头记的   → 写进一个"待处理消息"队列，
                    她下次被叫到时，这段内容会挂在她的上下文里

怎么让丫头"知道"
    在用户消息前面挂一段系统提示。这个位置有个好处：
    它不会破坏 prompt 缓存（因为它是用户消息的一部分，
    每轮本来就要重算用户消息）。

    ⚠️ 但要注意别滚雪球：
        有一段历史踩过——把"印象"挂到用户消息前，
        而用户消息会被写进对话历史，于是每轮多存一份，
        滚到 526 份副本。

        所以这里挂的必须是**临时内容**：
        只在这一轮的请求里拼上去，不写回对话历史。
"""

from __future__ import annotations

import json
import os

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

BRIDGE_DIR = r"C:\Users\19836\Downloads\qqaibot-bridge"
OUTBOX = os.path.join(BRIDGE_DIR, "outbox.jsonl")

PLUGIN_NAME = "astrbot_plugin_dsh_digest"



def _api_token() -> str:
    """读桥接口的访问 token。

    ⚠️ 这个漏过两次，都是同一个原因：
        给桥接口加了 token 鉴权之后，各处调用点忘了跟着改。
        表现是请求 401 → 被 except 吞掉 → 返回空。
        而空值在很多地方是"合法"的（比如"找不到模型名"），
        于是什么都不报错，只是功能悄悄失效。

        第一次是 ServiceMonitor（按钮一直闪红）。
        第二次是推送的 _profile_label（格式里显示不出模型名）。

    每次现读文件，不缓存——文件和接口服务谁先起来不一定。
    """
    import os

    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = os.path.join(base, "dsh-bridge", "api_token.txt")
    try:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    except OSError:
        pass
    return ""

@register(PLUGIN_NAME, "dsh", "把任务完成情况告诉前台丫头（不打扰用户）", "1.0.0")
class DshDigestPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # 已经告诉过丫头的任务（按 id 记），避免重复
        self._told: set[str] = set()
        # 本轮要挂上去的内容（临时，不写历史）
        self._pending: list[str] = []

    # ── 读 outbox，找出"刚完成、还没告诉她"的 ──
    def _init_baseline(self) -> None:
        """启动时把已有的任务都记成"已经知道了"。

        ⚠️ 不这么做的话，第一次注入会把 outbox 里所有历史任务
            当成"刚完成"，一次灌 20 条给她——上下文白白撑大，
            而且全是几天前的旧事。

            实测：重启后第一句就"告诉了丫头 20 个任务的状态"，
            那些任务是昨天跑的，跟当前对话毫无关系。

        只对启动时已有的生效。之后新完成的照常告诉。
        """
        try:
            if not os.path.exists(OUTBOX):
                return
            with open(OUTBOX, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tid = str(r.get("id") or "")
                    if tid:
                        self._told.add(tid)
            logger.info(f"[摘要] 启动基线：{len(self._told)} 个历史任务不再重复告知")
        except OSError:
            pass

    def _new_done(self) -> list[dict]:
        out = []
        try:
            if not os.path.exists(OUTBOX):
                return out
            with open(OUTBOX, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    tid = str(r.get("id") or "")
                    if not tid or tid in self._told:
                        continue
                    st = r.get("status")
                    if st in ("ok", "error", "timeout", "empty", "cancelled"):
                        out.append(r)
        except OSError:
            pass
        return out[-20:]   # 一次最多带 20 条，别把上下文撑爆

    @staticmethod
    def _profile_label(pid: str) -> str:
        if not pid:
            return ""
        try:
            import urllib.request

            with urllib.request.urlopen(
                urllib.request.Request("http://127.0.0.1:8790/api/engine/profiles", headers={"X-Token": _api_token()}),
                timeout=4,
            ) as r:
                d = json.loads(r.read())
            for prof in d.get("profiles") or []:
                if prof.get("id") == pid:
                    return prof.get("name") or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _build_digest(self, recs: list[dict]) -> str:
        """把完成情况排成一段给她看的话。"""
        lines = []
        for r in recs:
            who = self._profile_label(r.get("profile") or "")
            name = (r.get("name") or r.get("id") or "")[:30]
            st = r.get("status")
            secs = r.get("seconds")

            if st == "ok":
                suffix = f"（{secs:.0f} 秒）" if isinstance(secs, (int, float)) else ""
                lines.append(f"- [{who or '默认'}] {name} 完成{suffix}")
            elif st == "timeout":
                lines.append(f"- [{who or '默认'}] {name} 卡住了（超时）")
            elif st == "empty":
                lines.append(f"- [{who or '默认'}] {name} 跑完了但没说话")
            elif st == "cancelled":
                lines.append(f"- [{who or '默认'}] {name} 被取消了")
            else:
                err = (r.get("error") or st or "")[:60]
                lines.append(f"- [{who or '默认'}] {name} 失败：{err}")
        return "\n".join(lines)

    async def initialize(self) -> None:
        self._init_baseline()

    @filter.on_llm_request()
    async def _inject_digest(self, event, req):
        """每轮请求前，把"刚干完的活"挂到消息最前面。

        ⚠️ 挂在**这一轮的请求**上，不写回对话历史。
           写进历史会导致滚雪球（历史里越积越多份）。
        """
        recs = self._new_done()
        if not recs:
            return

        digest = self._build_digest(recs)
        if not digest:
            return

        note = (
            "【电脑端任务刚有变化】\n"
            f"{digest}"
        )

        injected = []

        # 位置一：系统提示词
        #
        # ⚠️ 光挂这里不够。
        #    实测挂了三次（日志都写了"告诉了丫头 N 个任务"），
        #    但她回答时还是说"我这边看不到"。
        #    说明这个字段在后面的组装里被覆盖了，或者
        #    模型没把它当回事。
        #
        #    所以同时挂到两个地方，哪个生效都行。
        try:
            sys_prompt = getattr(req, "system_prompt", None) or ""
            req.system_prompt = f"{sys_prompt}\n\n{note}"
            injected.append("system_prompt")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[摘要] 挂系统提示失败：{type(e).__name__}: {e}")

        # 位置二：用户消息本身
        #
        # 这个是硬保证——用户消息一定会进上下文。
        # 放在最前面，像"用户说完话又补了一句背景"。
        try:
            p0 = getattr(req, "prompt", None)
            if isinstance(p0, str):
                req.prompt = f"{note}\n\n---\n\n{p0}"
                injected.append("prompt")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[摘要] 挂用户消息失败：{type(e).__name__}: {e}")

        if not injected:
            logger.warning("[摘要] 两个位置都没挂上，这次算了")
            return

        # 记下来，别重复告诉她
        for r in recs:
            self._told.add(str(r.get("id") or ""))

        logger.info(
            f"[摘要] 告诉了丫头 {len(recs)} 个任务的状态（挂在 {'+'.join(injected)}）"
        )
