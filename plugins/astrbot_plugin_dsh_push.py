"""任务完成时主动推给用户，不用人来问。

为什么需要这个
    现在的流程是「派出去 → 你去问 → 我告诉你好了没」。
    问题很明显：**你得记着来问。**

    真正该是「派出去 → 它跑完 → 自己告诉你」。

怎么做的
    电脑端跑完任务后，往 outbox.jsonl 追加一条完成通知。
    这边有个轻量的检查循环，看到新通知就主动发消息给用户。

    三个关键决定：

    一、**用文件做中转，不直接发消息。**
       电脑端那个进程不知道 QQ 的会话信息，也不该知道。
       它只管写"我做完了"，谁去通知是这边的事。
       而且用户不在线时，通知躺在文件里，不会丢。

    二、**已送出的标记写在文件里（delivered 字段）。**
       不另存一个"已读游标"，因为那样两份状态容易不一致。
       每次读回来、改、写回去。虽然重写整份，但通知量很小。

    三、**检查间隔 20 秒，但只在有活的时候才真的查。**
       没有待推送任务时不读文件，省得空转。
"""

from __future__ import annotations

import asyncio
import json
import os

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_dsh_push"

BRIDGE_DIR = r"C:\Users\19836\Downloads\qqaibot-bridge"
OUTBOX = os.path.join(BRIDGE_DIR, "outbox.jsonl")

# 用户 QQ（私聊推送目标）
DEFAULT_TARGET = "3638576873"

# 检查间隔（秒）。
#
# ⚠️ 原来是 20 秒。
#    一个 1 秒就跑完的任务，你要等最多 20 秒才收到推送。
#    用户反馈"推送很慢"就是这个。
#
#    改成 3 秒。代价是每秒多读几次一个很小的文件
#    （outbox 通常只有几十条），CPU 开销可以忽略。
POLL_SECONDS = 3



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

@register(PLUGIN_NAME, "dsh", "任务跑完主动推给用户", "1.0.0")
class DshPushPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.target = str(cfg.get("target_qq", DEFAULT_TARGET) or DEFAULT_TARGET)
        # 最近一次有人在哪个会话里说话。
        #
        # 为什么需要这个：
        #   推送是定时轮询触发的（每 20 秒查一次 outbox），
        #   那时候手上没有 event，不知道对方在哪个平台。
        #   原来写死 qq-napcat，换到飞书就发不出去。
        #
        #   现在记住"最后说话的会话"，推送到那里去。
        #   存成 umo 字符串，形如 feishu:FriendMessage:ou_xxx
        self._last_umo = ""
        # 每个平台各记一个会话，发不出去时换平台。
        self._umo_by_platform: dict[str, str] = {}
        self.poll = int(cfg.get("poll_seconds", POLL_SECONDS) or POLL_SECONDS)
        self._task: asyncio.Task | None = None
        logger.info(f"[推送] 已加载。目标={self.target} 间隔={self.poll}s")

    async def initialize(self) -> None:
        self._load_sessions()

        if not self.enabled:
            logger.info("[推送] 已关闭，不启动检查")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("[推送] 检查循环已启动")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[推送] 已卸载")

    # ------------------------------------------------------------------

    def _read_outbox(self) -> list[dict]:
        if not os.path.exists(OUTBOX):
            return []
        rows = []
        try:
            with open(OUTBOX, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return rows

    def _mark_delivered(self, ids: set[str]) -> None:
        """把这几条标成已送达。整份重写，通知量小，不值得做增量。"""
        rows = self._read_outbox()
        if not rows:
            return
        changed = False
        for r in rows:
            if r.get("id") in ids and not r.get("delivered"):
                r["delivered"] = True
                changed = True
        if not changed:
            return
        tmp = OUTBOX + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, OUTBOX)
        except OSError as e:
            logger.warning(f"[推送] 标记已读失败: {e}")

    def _pending(self) -> list[dict]:
        return [r for r in self._read_outbox() if not r.get("delivered")]

    @classmethod
    def _fmt(cls, rec: dict) -> str:
        """把一条通知排成人话。

        ⚠️ 显示的是**任务名**，不是任务内容。
            原来用 task（任务正文）当标签，于是推送出来是
            「假任务，测试用，请不要真正执行任何文件操作。内容【写一个 text 文件…】」
            一长串，用户根本看不出是哪个活。
            任务名是用户自己起、或者派活时确认过的，那才是他记得住的东西。

            正文只在没有名字时兜底（老记录可能没 name）。
        """
        name = (rec.get("name") or rec.get("id") or "").strip()
        task = (rec.get("task") or "")[:60]
        label = name or task
        status = rec.get("status")
        secs = rec.get("seconds")
        result = (rec.get("result") or "").strip()
        err = (rec.get("error") or "").strip()

        # 标上这活是用哪个模型跑的。
        #
        # 为什么要有：
        #   同时开了好几套模型配置（DeepSeek / Kimi / 通义…），
        #   推送只说「任务名 做完了」，用户不知道这是哪套跑的，
        #   看到结果不对劲时也没法判断该去动哪个配置。
        #
        #   用户的原话：「推送没有带上 deepseek---（任务名）kimi---（任务名）」
        who = cls._profile_label(rec.get("profile") or "")
        prefix = f"[{who}] " if who else ""

        if status == "ok":
            head = f"{prefix}「{label}」做完了" + (f"（{secs:.0f} 秒）" if isinstance(secs, (int, float)) else "")
            return f"{head}：\n{result}" if result else head
        if status == "empty":
            return f"{prefix}「{label}」跑完了，但它一句话都没说。"
        if status == "timeout":
            return f"{prefix}「{label}」超时了，可能卡住了。"
        return f"{prefix}「{label}」没跑成：{(err or status)[:200]}"

    @staticmethod
    def _profile_label(pid: str) -> str:
        """把配置 id 翻译成人认的名字。

        outbox 里存的是 id（比如 "p1793200224206"），
        推送里要显示名字（"DeepSeek"）。

        读不到就返回空——宁可只显示任务名，
        也不要显示一串用户看不懂的 id。
        """
        if not pid:
            return ""
        try:
            import urllib.request

            with urllib.request.urlopen(
                urllib.request.Request("http://127.0.0.1:8790/api/engine/profiles", headers={"X-Token": _api_token()}),
                timeout=5,
            ) as r:
                d = json.loads(r.read())
            for prof in d.get("profiles") or []:
                if prof.get("id") == pid:
                    return prof.get("name") or ""
        except Exception:  # noqa: BLE001
            pass
        return ""

    @staticmethod
    def _fmt_batch(recs: list[dict]) -> str:
        """把一批完成通知合并成一条消息，**按模型分组**。

        格式是这样：

            DeepSeek
              · 登录页 —— 完成（12 秒）
              · 接口核对 —— 完成（8 秒）
            ────────────────────────────────
            Kimi
              · 数据分析 —— 失败：超时

        为什么要分组
            同时开好几套模型配置的时候，一批推送里混着好几个模型的
            任务。原来按"成功/失败"分组，结果是这样：

                · 登录页 ✅（DeepSeek 跑的）
                · 接口核对 ✅（Kimi 跑的）
                · 数据分析 ✅（DeepSeek 跑的）

            用户看不出哪个是哪个——而这恰恰是最要紧的信息：
            DeepSeek 很快，Kimi 很慢，看到某套慢了才知道该动哪套配置。

            用户的原话：「不要堆在一起，要分开」。

        分档还是保留（防止刷屏）：
            1-3 个   → 逐条说清楚（结果正文也给）
            4-20 个  → 按模型分组列清单
            20+ 个   → 按模型只报统计 + 失败的
        """
        n = len(recs)
        ok_recs = [r for r in recs if r.get("status") == "ok"]
        bad_recs = [r for r in recs if r.get("status") != "ok"]

        def _name(r):
            # 优先任务名（用户记得住的），其次 id，最后才用任务正文兜底。
            return (r.get("name") or r.get("id") or r.get("task") or "")[:30]

        # 只有 1 条才单独说。
        #
        # ⚠️ 原来这里是 n <= 3 都逐条说。用户看到的是这样：
        #       [DeepSeek] 「任务A」做完了（1 秒）：你好
        #       [DeepSeek] 「任务B」做完了（1 秒）：你好
        #       [Kimi] 「任务C」做完了（3 秒）：你好
        #    三条并排，每行都要自己看前缀才知道是哪个模型跑的。
        #
        #    用户要的是分组：
        #       3 个任务完成：2 成功，1 失败
        #       ────────────
        #       DeepSeek
        #         · 任务A —— 完成（1 秒）
        #         · 任务B —— 完成（1 秒）
        #       ────────────
        #       Kimi
        #         · 任务C —— 完成（3 秒）
        #
        #    所以门槛降到 1：两条以上就分组。
        if n == 1:
            return DshPushPlugin._fmt(recs[0])

        # 把记录按模型分组。顺序按组里第一个任务的时间，谁先完成谁在前。
        groups: dict[str, list[dict]] = {}
        order: list[str] = []
        for r in recs:
            who = DshPushPlugin._profile_label(r.get("profile") or "") or "默认"
            if who not in groups:
                groups[who] = []
                order.append(who)
            groups[who].append(r)

        SEP = "─" * 32

        # 二档：一批，按模型分组列清单
        if n <= 20:
            head = f"{n} 个任务完成：{len(ok_recs)} 成功，{len(bad_recs)} 失败"
            blocks = [head]
            for who in order:
                items = groups[who]
                lines = [who]
                for r in items:
                    nm = _name(r)
                    st = r.get("status")
                    secs = r.get("seconds")
                    if st == "ok":
                        suffix = f"（{secs:.0f} 秒）" if isinstance(secs, (int, float)) else ""
                        lines.append(f"  · {nm} —— 完成{suffix}")
                    elif st == "empty":
                        lines.append(f"  · {nm} —— 跑完了但没说活")
                    elif st == "timeout":
                        lines.append(f"  · {nm} —— 卡住了（超时）")
                    elif st == "queued":
                        lines.append(f"  · {nm} —— 还在排队")
                    elif st == "running":
                        lines.append(f"  · {nm} —— 还在跑")
                    elif st == "cancelled":
                        lines.append(f"  · {nm} —— 被取消了")
                    else:
                        err = (r.get("error") or st or "失败")[:40]
                        lines.append(f"  · {nm} —— 失败：{err}")
                blocks.append("\n".join(lines))
            return f"\n{SEP}\n".join(blocks)

        # 三档：大批，按模型只报统计 + 失败的
        blocks = [f"{n} 个任务完成：{len(ok_recs)} 成功，{len(bad_recs)} 失败"]
        for who in order:
            items = groups[who]
            ok_n = sum(1 for r in items if r.get("status") == "ok")
            bad_n = len(items) - ok_n
            lines = [f"{who}：{len(items)} 个（{ok_n} 成功，{bad_n} 失败）"]
            for r in items:
                if r.get("status") == "ok":
                    continue
                err = (r.get("error") or r.get("status") or "")[:40]
                lines.append(f"  · {_name(r)} —— 失败：{err}")
            blocks.append("\n".join(lines))
        blocks.append("要看哪个的详情，跟我说名字。")
        return f"\n{SEP}\n".join(blocks)


    async def _loop(self) -> None:
        """检查循环。只推新完成的，推完标记。"""
        warned = False
        while True:
            try:
                await asyncio.sleep(self.poll)

                pending = await asyncio.to_thread(self._pending)
                if not pending:
                    continue

                # 合并推送：一次把攒下的都报掉，而不是挤牙膏
                # 上限 100 条，避免极端情况下列表太长
                batch = pending[:100]
                text = self._fmt_batch(batch)
                sent_ids: set[str] = set()
                ok = await self._notify(text)
                if ok:
                    for rec in batch:
                        sent_ids.add(rec.get("id", ""))

                if sent_ids:
                    await asyncio.to_thread(self._mark_delivered, sent_ids)
                    logger.info(f"[推送] 推了 {len(sent_ids)} 条完成通知")
                warned = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if not warned:
                    logger.warning(f"[推送] 循环出错（会继续重试）: {type(e).__name__}: {e}")
                    warned = True
                await asyncio.sleep(60)

    async def _notify(self, text: str) -> bool:
        """把完成通知当成一条"收到的消息"塞进事件队列。

        ⚠️ 这里返工过一次，区别很重要：

            原来：任务跑完 → 直接 send_message 推到用户 QQ。
                  结果是用户知道了，**但我不知道**。
                  用户想让我知道，只能截图发给我——绕一大圈。

            现在：任务跑完 → 构造一个假事件塞进事件队列，
                  让它走完整条处理管线（会经过我），
                  由"我自己"看完再决定怎么告诉用户。

            这个区别不只是技术上的：前者是"机器回执"，
            后者是我真的知道了这件事，能用我自己的话讲出来。

        怎么实现的：
            context.get_event_queue() 拿到队列，
            构造一个 AstrBotMessage（sender 标成"电脑端"），
            包成 AstrIPlatformEvent 塞进去。
        """
        try:
            import time  # noqa: PLC0415

            from astrbot.api.message_components import Plain  # noqa: PLC0415
            from astrbot.core.platform.astrbot_message import (  # noqa: PLC0415
                AstrBotMessage,
                MessageMember,
                MessageType as AstrMsgType,
            )
            from astrbot.core.platform.message_type import MessageType  # noqa: PLC0415
            from astrbot.core.platform.platform import PlatformStatus  # noqa: PLC0415

            # 先判断：这个平台上"引用"能不能用。
            #
            # 踩过：push 是把一条事件塞进队列，走跟正常回复一样的链路。
            # 而 reply_with_quote 是全局开关，于是推送也被加了引用。
            # QQ 支持引用没事；**飞书不支持，整条消息发送失败**：
            #     Failed to send the message chain
            # 用户那边什么都收不到，日志里却写着"推了 N 条"。
            #
            # 所以：目标平台不支持引用的话，别走 pipeline，直接发。
            platform_name = ""
            if self._last_umo:
                platform_name = self._last_umo.split(":", 1)[0]

            NO_QUOTE_PLATFORMS = ("feishu", "lark", "telegram", "slack", "discord")
            if any(p in platform_name.lower() for p in NO_QUOTE_PLATFORMS):
                logger.info(f"[推送] {platform_name} 不支持引用，直接发不走 pipeline")
                return await self._direct_send(text)

            # 找平台适配器。
            #
            # 原来这里只找 aiocqhttp（QQ）。换到飞书以后找不到，
            # 整条"入队"路径就废了，只能退回直接发。
            #
            # 现在按"你最后说话的那个平台"去找：
            #   _last_umo 形如 feishu:FriendMessage:ou_xxx
            #   从里面取出平台 id，再在适配器列表里找它。
            #
            # 一个平台都没有时才退回直接发。
            platform = None
            want_platform = ""
            if self._last_umo:
                want_platform = self._last_umo.split(":", 1)[0]

            try:
                insts = self.context.platform_manager.get_insts()
            except Exception:  # noqa: BLE001
                insts = []

            for p in insts:
                try:
                    meta = p.meta()
                    pid = meta.id or ""
                    pname = meta.name or ""
                    # 先按 umo 里的平台 id 精确匹配
                    if want_platform and (pid == want_platform or pname == want_platform):
                        platform = p
                        break
                    # 没记过 umo 时，退回找 QQ（老行为）
                    if not want_platform and pname == "aiocqhttp":
                        platform = p
                        break
                except Exception:  # noqa: BLE001
                    continue

            if platform is None:
                logger.warning(
                    f"[推送] 找不到平台（想找 {want_platform or 'aiocqhttp'}），退回直接发消息"
                )
                return await self._direct_send(text)

            meta = platform.meta()

            # 构造消息体
            # session_id 也要按平台来：
            #   飞书是 open_id，QQ 是 QQ 号。
            #   从 umo 里取最后一段就是会话 id。
            sid = self.target
            if self._last_umo:
                parts = self._last_umo.split(":")
                if len(parts) >= 3:
                    sid = parts[2]

            abm = AstrBotMessage()
            abm.type = AstrMsgType.FRIEND_MESSAGE
            abm.self_id = ""
            abm.session_id = sid
            abm.message_id = f"push-{int(time.time()*1000)}"
            abm.sender = MessageMember(user_id=sid, nickname="电脑端")
            abm.message = [Plain(f"[电脑端任务完成] {text}")]
            abm.message_str = f"[电脑端任务完成] {text}"
            # ⚠️ 这三个字段必须清空，否则回复链会给它加引用。
            #
            #    踩过：推送是"插一条事件进队列"，走的是和正常回复
            #    一样的链路。而 reply_with_quote 是全局开关，
            #    于是推送消息也被包了一层 Reply。
            #    QQ 支持引用，没事；**飞书不支持，整条消息发送失败**：
            #        Failed to send the message chain
            #    用户那边什么都收不到。
            #
            #    推送不需要引用——它不是"回复某句话"，
            #    是"通知你一件事"。
            abm.raw_message = None
            abm.message_id = f"push-{int(time.time()*1000)}"
            # 清掉 peer id / 引用相关字段
            for _attr in ("reply", "quote", "parent_id", "source_message"):
                try:
                    setattr(abm, _attr, None)
                except Exception:  # noqa: BLE001
                    pass
            abm.timestamp = int(time.time())

            # create_event(message) 只收一个参数——消息对象本身。
            # 之前多传了个 message_type，报
            # "takes 2 positional arguments but 3 were given"。
            event = platform.create_event(abm)
            await self.context.get_event_queue().put(event)
            logger.info("[推送] 已把完成通知放进事件队列")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[推送] 入队失败（退回直接发）: {type(e).__name__}: {e}")
            return await self._direct_send(text)

    async def _direct_send(self, text: str) -> bool:
        """直接发消息给你。

        发到哪：你最后说话的那个会话。
        原来这里写死 platform_name="qq-napcat" + QQ 号，
        换到飞书就发不出去——飞书的会话 id 是 open_id，不是 QQ 号。

        现在用统一会话标识（umo）：
            有 umo 就按 umo 发（不管你在哪个平台）
            没有就退回旧办法（QQ 号），至少不会比以前差
        """
        try:
            from astrbot.api.message_components import Plain  # noqa: PLC0415
            from astrbot.core.message.message_event_result import MessageChain  # noqa: PLC0415
            from astrbot.core.platform.message_session import MessageSession  # noqa: PLC0415
            from astrbot.core.platform.message_type import MessageType  # noqa: PLC0415

            chain = MessageChain(chain=[Plain(text)])

            # 挨个平台试，哪个通就发哪个。
            #
            # 顺序：最后说话的那个优先，然后其它平台。
            # 原来只发一个——那个平台挂了（比如 NapCat 掉线），
            # 消息就彻底丢了，哪怕飞书那边还好好的。
            atlas = getattr(self, "_umo_by_platform", None) or {}
            candidates = []
            if self._last_umo:
                candidates.append(self._last_umo)
            for umo in atlas.values():
                if umo not in candidates:
                    candidates.append(umo)

            for umo in candidates:
                try:
                    session = MessageSession.from_str(umo)
                    ok = await self.context.send_message(session, chain)
                    if ok:
                        logger.info(f"[推送] 已发到 {umo}")
                        return True
                    logger.warning(f"[推送] {umo} 没发出去，换下一个")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"[推送] {umo} 发送出错: {type(e).__name__}: {e}")

            # 兜底：老办法，只管 QQ
            session = MessageSession(
                platform_name="qq-napcat",
                message_type=MessageType.FRIEND_MESSAGE,
                session_id=self.target,
            )
            ok = await self.context.send_message(session, chain)
            return bool(ok)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[推送] 兜底发送也失败: {type(e).__name__}: {e}")
            return False

    # ------------------------------------------------------------------
    # 关键：让 QQ 端的"我"也知道这件事
    # ------------------------------------------------------------------

    # 会话记忆存哪
    def _sessions_path(self) -> str:
        return os.path.join(BRIDGE_DIR, ".push_sessions.json")

    def _save_sessions(self) -> None:
        try:
            with open(self._sessions_path(), "w", encoding="utf-8") as f:
                json.dump(self._umo_by_platform or {}, f, ensure_ascii=False)
        except OSError:
            pass

    def _load_sessions(self) -> None:
        """启动时把上次记的会话读回来。

        ⚠️ 为什么需要落盘：
            AstrBot 重启后 _last_umo 是空的。这时候如果用户
            还没说话就先有任务跑完，推送找不到目标，
            只能记一句"找不到平台"然后丢掉。

            实测就这样：派任务 → 任务完成 → 推送失败。

        读回来的会话不一定还可用（比如对方改过号），
        但那也比完全不知道发哪强——发不出去会再试下一个平台。
        """
        try:
            p = self._sessions_path()
            if not os.path.exists(p):
                return
            d = json.loads(open(p, encoding="utf-8").read())
            if isinstance(d, dict):
                self._umo_by_platform = {
                    str(k): str(v) for k, v in d.items() if v
                }
                # 最后说话的那个排前面
                if self._umo_by_platform:
                    self._last_umo = list(self._umo_by_platform.values())[-1]
                logger.info(
                    f"[推送] 恢复会话记忆：{list(self._umo_by_platform.keys())}"
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[推送] 恢复会话失败：{type(e).__name__}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _remember_session(self, event):
        """记住你在每个平台上的会话。

        为什么记所有平台而不只是最后一个：
            原来只记一个。你在 QQ 说了话，目标就切到 QQ；
            那之后 QQ 的 NapCat 一挂，推送就彻底发不出去了——
            明明飞书还通着，消息却丢了。

            现在每个平台各记一份，发的时候挨个试，
            哪个平台活着就发哪个。
        """
        try:
            umo = event.unified_msg_origin
            if not umo:
                return
            plat = umo.split(":", 1)[0]
            atlas = getattr(self, "_umo_by_platform", None)
            if atlas is None:
                atlas = {}
                self._umo_by_platform = atlas
            if atlas.get(plat) != umo:
                atlas[plat] = umo
                self._last_umo = umo
                logger.info(f"[推送] 记住会话：{umo}")
                # 落盘。不落盘的话，AstrBot 一重启就忘了你上次在
                # 哪个会话说过话，推送只能退回"找不到平台"。
                self._save_sessions()
        except Exception:  # noqa: BLE001
            pass
        # 不 return、不 stop，让消息继续走正常流程

    async def push_status(self, event):
        """看看有没有还没推给你的完成通知。

        想知道“刚才那些活到底通知了没”的时候用。
        """
        try:
            pending = await asyncio.to_thread(self._pending)
            rows = await asyncio.to_thread(self._read_outbox)
        except Exception as e:  # noqa: BLE001
            return f"读不到通知箱：{e}"

        if not rows:
            return "通知箱是空的，还没有任务跑完。"

        done = [r for r in rows if r.get("delivered")]
        return (
            f"通知箱里 {len(rows)} 条，已推送 {len(done)} 条，"
            f"待推 {len(pending)} 条。"
        )
