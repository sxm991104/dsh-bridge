"""把任务实时派给电脑端，**不等结果**。

为什么改成不等
    原来：派活 → 同步等 → 返回结果。
    问题：真干活可能三四分钟，而 QQ 端等不了那么久。
    超时后只能说“超时”，分不清是卡死了还是还在跑——
    电脑端明明跑完了（194 秒，成功），QQ 端却已经断线。

    现在拆成两步：
        派活：写一条任务，立刻返回“已派出去”（0.2 秒）
        查状态：你说“看看”，我去读任务现在到哪一步了

    状态有五种，能分清“还在跑”和“真出问题”：
        pending  排队中
        running  正在跑（有开始时间，能算跑了多久）
        ok       做完了，有结果
        empty    跑完了但没输出
        error / timeout  真出问题

后台谁来跑
    派活之后，插件自己起一个后台进程去执行，
    不等它，也不管它多久。QQ 端立刻能回你别的。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_dsh_bridge"

# ⚠️ 桥的工作目录必须在 DSH 沙箱能接管的路径下。
#
#    原来的 D:\qqaibot\bridge 跑不了 pwsh —— DSH 启动 shell 前要改工作目录的
#    安全描述符（加专用 SID + 加 deny + 把完整性标签降为 Low），
#    那一步报 `SetNamedSecurityInfoW failed (Win32 5): grantWrite`。
#    结果是电脑端的我读得了文件、跑不了任何命令。
#
#    C:\Users\19836\Downloads 下面能接管（实测 Downloads 目录已经有了
#    沙箱那套标记：deny FILE_DELETE_CHILD、专用 SID、Low 完整性标签），
#    所以把桥搬过来，pwsh 才跑得起来。
BRIDGE_DIR = r"C:\Users\19836\Downloads\qqaibot-bridge"
BRIDGE_PY = os.path.join(BRIDGE_DIR, "bridge.py")
TASKS_FILE = os.path.join(BRIDGE_DIR, "tasks.jsonl")

# 后台执行时的单任务上限。因为不再等待，这个可以放得很宽。
RUN_TIMEOUT = 1800

# 心跳多久没跳就算卡住。
# 电脑端每 5 秒写一次心跳，正常情况下一分钟内必有动静。
# 超过 90 秒没心跳，基本可以断定卡死了（不是"跑得慢"）。
STALL_SECONDS = 90


def _decode_any(raw: bytes) -> str:
    """子进程字节 → 文字。UTF-8 优先，失败退 GBK。

    这段是踩了两轮才找对的：
        强设 encoding="utf-8" 没用——字节已经在子进程那边按 GBK 生成了。
        给子进程传 PYTHONIOENCODING 也没用——dsh.cmd 是批处理，带不下去。
        所以自己拿 bytes 判断编码，最稳。
    """
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")



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

@register(PLUGIN_NAME, "dsh", "把任务派给电脑端（不等待）", "2.0.0")
class DshBridgePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled", True))
        self.python = str(cfg.get("python_path", "")).strip() or sys.executable
        # 记录正在跑的后台进程，方便查/停
        self._workers: dict[str, asyncio.Task] = {}
        logger.info(f"[桥] 已加载（留言式）。python={self.python}")

    async def terminate(self) -> None:
        for t in list(self._workers.values()):
            t.cancel()
        logger.info("[桥] 已卸载")

    # ------------------------------------------------------------------
    # 内部：调 bridge.py
    # ------------------------------------------------------------------

    def _resolve_profile(self, model_name: str) -> str:
        """把「kimi」「deepseek」这种叫法，解析成配置的 id。

        用户在对话里不会说完整名字（「派给 Kimi」），
        所以这里做模糊匹配：名字里有这个词就算。
        匹配不到就返回空，让桥用默认那套。
        """
        want = (model_name or "").strip().lower()
        if not want:
            return ""
        # ⚠️ 这里原来有个 bug，而且是"静默失败"那种。
        #
        #    写的是：
        #        import urllib.request as _ur        ← 导入时起了别名
        #        urllib.request.Request(...)         ← 但用的是原名
        #
        #    而顶层没有 import urllib，所以这里是 NameError。
        #    外面套着 except Exception: return "" —— 异常被吞了，
        #    函数安安稳稳返回空字符串，于是"没指定模型" → 用默认那套。
        #
        #    症状：用户说"三个派 deepseek 两个派 kimi"，
        #          前台确实传了 model='deepseek'，
        #          但**五条全跑在默认配置（Kimi）上**。
        #          而且日志里一切正常，看不出来。
        #
        #    实测：5 条任务，profile 全是 'default'。
        #
        #    教训：except Exception 后面接 return 一个"正常值"，
        #          等于把错误伪装成正常。要么返回错误，要么至少记一笔。
        try:
            import json as _json
            import urllib.request as _ur

            with _ur.urlopen(
                _ur.Request(
                    "http://127.0.0.1:8790/api/engine/profiles",
                    headers={"X-Token": _api_token()},
                ),
                timeout=10,
            ) as r:
                data = _json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            # 记一笔，别静默。静默的话下次还是查半天。
            logger.warning(
                f"[桥] 查模型配置失败，本条走默认：{type(e).__name__}: {e}"
            )
            return ""

        profiles = data.get("profiles") or []

        # 先找名字完全一致的
        for prof in profiles:
            if (prof.get("name") or "").lower() == want:
                return prof.get("id") or ""

        # 再找包含关系的
        for prof in profiles:
            nm = (prof.get("name") or "").lower()
            md = (prof.get("model") or "").lower()
            if want in nm or want in md or nm in want or md in want:
                return prof.get("id") or ""

        return ""

    def _call_api(self, path: str, body: dict | None = None,
                  timeout: int = 15) -> dict:
        """调桥的 HTTP 接口。

        跟 _call_bridge 的区别：
            _call_bridge  走命令行（用来派活、跑任务）
            _call_api     走 HTTP（用来读写配置）

        token 在 %APPDATA%/dsh-bridge/api_token.txt 里。
        """
        import json as _json
        import urllib.request as _ur

        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        tp = os.path.join(base, "dsh-bridge", "api_token.txt")
        try:
            with open(tp, encoding="utf-8") as f:
                token = f.read().strip()
        except OSError as e:
            return {"ok": False, "error": f"读不到 token：{e}"}

        url = "http://127.0.0.1:8790" + path
        data = _json.dumps(body).encode("utf-8") if body is not None else None
        req = _ur.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json", "X-Token": token},
            method="POST" if data is not None else "GET",
        )
        try:
            with _ur.urlopen(req, timeout=timeout) as r:
                return _json.loads(r.read())
        except _ur.HTTPError as e:
            try:
                return _json.loads(e.read())
            except Exception:  # noqa: BLE001
                return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _call_bridge(self, args: list[str], timeout: int = 60) -> dict:
        """同步调 bridge.py，返回它打印的 JSON。"""
        try:
            child_env = dict(os.environ)
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            proc = subprocess.run(
                [self.python, BRIDGE_PY] + args,
                capture_output=True,
                timeout=timeout,
                cwd=BRIDGE_DIR,
                env=child_env,
            )
            out = _decode_any(proc.stdout or b"").strip()
            err = _decode_any(proc.stderr or b"").strip()
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{") or line.startswith("["):
                    return json.loads(line)
            return {"status": "error", "error": (err or out or "没有输出")[:400]}
        except subprocess.TimeoutExpired:
            return {"status": "error", "error": "调用桥梁超时"}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    async def _spawn_worker(self, task_id: str) -> None:
        """起一个后台进程去执行任务，不等它。"""

        async def _run() -> None:
            try:
                await asyncio.to_thread(
                    self._call_bridge,
                    ["run", "--id", task_id, "--timeout", str(RUN_TIMEOUT)],
                    RUN_TIMEOUT + 120,
                )
                logger.info(f"[桥] 任务 {task_id} 后台执行结束")
            except asyncio.CancelledError:
                logger.info(f"[桥] 任务 {task_id} 被取消")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[桥] 任务 {task_id} 后台异常: {e}")
            finally:
                self._workers.pop(task_id, None)

        t = asyncio.create_task(_run())
        self._workers[task_id] = t

    @staticmethod
    def _fmt_state(st: dict) -> str:
        """把 state/<任务名>.json 排成人话。带上花费，用户才知道值不值。"""
        name = st.get("id") or "?"
        status = st.get("status") or "?"
        task = (st.get("task") or "")[:60]
        secs = st.get("seconds")
        result = (st.get("result") or "").strip()
        err = (st.get("error") or "").strip()
        toks = st.get("context_tokens")
        cost = st.get("cost_cny")

        if status == "pending":
            head = f"「{name}」还在排队：{task}"
        elif status == "running":
            head = f"「{name}」正在跑：{task}"
        elif status == "ok":
            head = f"「{name}」做完了" + (f"（{secs:.0f} 秒）" if secs else "")
            if result:
                # ⚠️ 结果不截断，而且要把"怎么转述"的规矩写死。
                #
                # 原来这里是 result[:500]，然后让模型自由发挥去概括。
                # 后果很实在：用户问「这个接口是 mock 还是真后端」，
                # 任务端第一条就写着「走 mock，不走真后端」，
                # 模型却盯着开头那句「接口侧完全正常」当结论，
                # 回了用户一句「你的说法不成立」——最关键的那句被当成背景丢了。
                #
                # 用户的原话：「这是目前没人敢用」的原因。
                # 说的不是能力不够，是**不敢信**。
                #
                # 所以这里不裁剪、也不放手让模型自由概括，改成给死规矩：
                #   1. 先找哪一段在回答用户最初的问题，放最前面
                #   2. 判断不出哪段是答案 → 原样交给用户，不许自己挑
                head += (
                    "\n\n--- 任务端原始回答 ---\n"
                    f"{result}\n"
                    "--- 结束 ---\n\n"
                    "【怎么转述给用户】\n"
                    "1. 先在用户最初问的那句话里找出「他要的答案是什么」，"
                    "再到上面这段原文里找**直接回答它的那一段**，把那段放在最前面说。\n"
                    "2. 不要拿「开头那句总结」当结论——它常常不是答案。\n"
                    "3. 如果判断不出哪段在回答用户的问题，就把原文整段贴给用户，"
                    "说明「这是它查到的全部内容」，不要自己挑、不要自己概括。\n"
                    "4. 不许用「你这个说法不成立」这类话否定用户的问题本身。"
                    "确实和他说的不一样，就平铺直叙地说事实是什么。"
                )
        elif status == "timeout":
            head = f"「{name}」超时了，可能卡住了。"
        else:
            head = f"「{name}」没跑成：{(err or status)}"

        # 花费单独一行——用户关心的是"这个窗口还划不划算"
        if isinstance(toks, (int, float)) and isinstance(cost, (int, float)):
            head += (
                f"\n（这个窗口现在每轮对话约 {int(toks):,} token，"
                f"大概 {cost:.2f} 元）"
            )
        return head

    @staticmethod
    def _fmt_task(t: dict, live: dict | None = None) -> str:
        """把一个任务记录排成人话。

        live 是 bridge.py live 的输出，带心跳信息。
        有它的时候能分清三种情况——这是这次改动的重点：
            · 心跳很新      → 真在跑
            · running 但心跳停了 → 大概率卡死（进程可能死了）
            · ok/error      → 已有结论
        之前只能说"正在跑"，但那条可能早就死了，用户干等。
        """
        st = t.get("status", "?")
        task_id = t.get("id", "?")
        text = (t.get("task") or "")[:80]

        # 从 live 里取心跳，判断是不是真活着
        beat_age = None
        alive = None
        elapsed = None
        doing = ""
        if live and isinstance(live, dict):
            p = live.get("progress") or {}
            if p.get("id") == task_id:
                beat_age = p.get("heartbeat_age_seconds")
                alive = p.get("alive")
                elapsed = p.get("elapsed_seconds")
                doing = (p.get("doing") or "").strip()

        if st == "pending":
            return f"「{text}」还在排队，电脑那边还没开始。"
        if st == "running":
            if beat_age is not None:
                if beat_age > STALL_SECONDS:
                    mins = int(beat_age // 60)
                    return (
                        f"「{text}」标着在跑，但已经 {mins} 分钟没心跳了。\n"
                        "大概率是卡住了，或者进程死了。要不要我停掉它？"
                    )
                secs = int(elapsed or 0)
                base = f"「{text}」正在跑，已 {secs} 秒（心跳 {beat_age:.0f} 秒前）。"
                if doing:
                    return f"{base}\n在做：{doing}"
                return base + "\n还看不出在做什么——可能刚开始，或者它在想没动手。"
            return f"「{text}」标着在跑，但查不到心跳——可能刚开始，也可能状态没回收。"
        if st == "ok":
            res = (t.get("result") or "").strip()
            sec = t.get("seconds")
            head = f"「{text}」做完了（{sec} 秒）"
            return f"{head}：\n{res}" if res else f"{head}，但没留结果。"
        if st == "empty":
            return f"「{text}」跑完了，但它一句话都没说。可能干完没讲，也可能中途卡住。"
        if st == "cancelled":
            return f"「{text}」被我停掉了。"
        if st == "stalled":
            return f"「{text}」卡住了——进程不在了，状态没回收干净。"
        if st == "timeout":
            return f"「{text}」超时了，可能真卡住了。"
        if st == "not-found":
            return f"没有 {task_id} 这个任务。"
        return f"「{text}」出问题了：{t.get('error') or st}"

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @filter.llm_tool(name="send_to_computer")
    async def send_to_computer(self, event, task: str = "",
                               task_name: str = "", model: str = ""):
        """把一件说清楚的事派给电脑端，**派完就走，不等结果**。

        ⚠️ 你是传话的，不是出主意的。
        ------------------------------------------------------------------
        这条是返工最多的，写清楚为什么：

        有一回用户说「接口返回的字段不匹配，根本没有人」，
        我顺手在任务里补了一句「去看 src/mock/index.ts」——
        那是我从任务端之前报过的结果里记来的路径，不是我亲眼看见的。

        三个问题：
            1. 把「它说过」当成了「我知道」
            2. 以指令的口吻派下去，看起来像是我核实过的
            3. 那条路径要是过时了，任务白跑，责任落在用户头上

        用户的原话是：「用户说什么你转达什么，你这是直接下达指令，
        公司里出了锅，都甩给你了。」

        所以派活只有两种信息可以写进任务：
            ✅ 用户自己说的话
            ✅ 你已经问过用户、用户明确回答过的内容

        绝不能写进去的：
            ❌ 你「记得」任务端报过的东西（那是它的记忆，不是事实）
            ❌ 你自己猜的路径、文件名、字段名
            ❌ 你没把握却写成肯定句的判断

        确实需要某个具体信息（比如某文件在哪），两种做法：
            · 先问用户：「它上次说在 src/mock/index.ts，要不要让它
              再确认一次？」——把不确定说出来
            · 或者派一个"先查清再说"的任务，让电脑端自己去找
        ------------------------------------------------------------------

        ⚠️⚠️ 硬规矩：task 里必须是用户的原话，一个字都不改。

        不润色、不整理、不补全、不换说法。
            ❌「用户说改大按钮」→ 写成「把登录页按钮改大一点，
                顺便调下颜色」      ← 这是你在替他决定要改什么
            ✅ 用户说「把登录页按钮改大一点，顺便调下颜色」
               → 原样放进去

        为什么这条是硬的：
            润色 = 我们替用户做了决定。
            任务端照着我们的说法做完，结果不对，
            **责任在我们，不在用户。**
            用户的原话是他自己的判断，我们只负责送达。

        唯独允许的两件事：
            1. 去掉纯语气词（「啊」「呢」「吧」）——这不改变意思
            2. 用户前面说过、这轮明确指代的东西，补上具体名字
               （「就那个」「继续那个」→ 换成用户自己说过的窗口名）

        其余一律原样。
        ------------------------------------------------------------------

        ⚠️ 派之前必须确认清楚。电脑端那边没人看着，
        话说不明白它只能瞎猜，猜错了白跑一趟，而且你不在电脑前，
        没法当场纠正。需要先问清（按情况自己判断）：
            · 具体动哪个东西（文件、目录、项目）
            · 要达成什么效果
            · 有没有什么不能碰的

        问的时候也要注意：只问用户知道的事。
        用户是业务方，他可能不懂代码细节——问他「src/mock 里注释掉了吗」
        是问不到答案的，该问的是「你希望看到什么结果」。

        派完之后：
            立刻就能回你别的话。想知道进度，过会儿用 check_computer_task。

        什么时候用：
            · 要改文件、跑命令、批量处理
            · 要在电脑上干活，而你不在电脑前
            · 你自己做不到的事（你只在 QQ 里，没有文件和终端）

        什么时候别用：
            · 聊天、查资料、解释概念——你直接答
            · 事情还含糊着——先问清楚

        任务名字怎么定（重要，用户靠这个名字回来找活）：
            · 用户自己说了名字（「叫它登录页」）→ 用用户说的
            · 用户说"随便"、没说名字 → 你自己取一个，取完告诉用户
            · 用户说的是一个已有的名字（「登录页再加个记住密码」）
              → 那就是往那个窗口续，不是开新窗

            取名要短、要像人话：两到六个字，像「登录页」「支付接口」「首页优化」，
            别用「修改登录页面按钮样式」这种一句话当名字。

            用户说"随便"时，你自己想一个，放在 task_name 里传进来，
            然后在回复里告诉他叫什么。

        用哪套模型（model 参数）：
            用户可以同时启用好几套模型配置，各有各的用处。
            用户说了具体哪套就用哪套：
                「派给 Kimi」        → model="kimi"
                「用 DeepSeek 做」    → model="deepseek"
                「用便宜的那个」      → 你自己挑，选配置里便宜的
            没说就留空，用默认那套。

            名字不用完全对，写个大概就行——我这边会模糊匹配。

        Args:
            task(string): 要电脑端做的事。用用户的原话，或者用户确认过的内容。
                         不要补你自己猜的路径、文件名、字段名。
            task_name(string): 任务叫什么名字，或者要续进哪个已有窗口。
            model(string): 用哪套模型配置。留空 = 默认那套。
        """
        if not self.enabled:
            return "派活功能关着。"

        text = (task or "").strip()
        if not text:
            return "没说要做啥。"

        # ⚠️ 危险操作的硬闸门。**必须在代码里，不能靠模型自觉。**
        #
        #    为什么：
        #        之前只提供了 confirm_dangerous_action 这个工具，
        #        指望模型"自己会先调它再派活"。
        #
        #        但那是**自觉**，不是**机制**：
        #            模型判断失误、或者被诱导，直接调 send_to_computer，
        #            验证码就完全绕过去了。
        #
        #        现在把检查放在派活的入口：不管模型走了哪条路，
        #        危险操作没验证过就是派不出去。
        #
        #    危险操作：
        #        rm -rf / drop table / git reset --hard / 读 .env …
        #        完整清单在 guard.py 的 DANGEROUS_PATTERNS。
        try:
            import urllib.request as _ur

            chk = _ur.Request(
                "http://127.0.0.1:8790/api/guard/check",
                data=json.dumps({"task": text}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token": _api_token(),
                },
                method="POST",
            )
            with _ur.urlopen(chk, timeout=10) as _r:
                gd = json.loads(_r.read())

            # 围栏开着 → **什么都派不出去**，不只是危险任务。
            #
            # 用户的原话：
            #     「只要出了验证码，整个任务端都锁死，什么活都排不出去，
            #       这已经触发了安全围栏」
            #
            # 这个语义是对的：出了验证码就说明系统可能被入侵了，
            # 这时候不该继续干活——哪怕是看起来无害的活。
            if gd.get("fenced"):
                left = int(gd.get("lock_left") or 0)
                if gd.get("waiting"):
                    tip = "验证码已经发到用户邮箱了，等他把码报过来。"
                elif left > 0:
                    tip = (
                        f"验证码超时了，任务端还要锁 {left // 60} 分 {left % 60} 秒。"
                        "可以重新发一次验证码（时间会重置，不会累加）。"
                    )
                else:
                    tip = "有一个验证请求还挂着，先处理它。"

                return (
                    "⛔ 任务端被安全锁关着，现在什么活都派不出去。\n\n"
                    f"{tip}\n\n"
                    "这不是针对你——是系统层面的锁。用户要做的只有一件事：\n"
                    "  把邮箱里的验证码报过来 → 调 verify_dangerous_code 校验\n"
                    "  通过了就自动解锁。\n\n"
                    "⚠️ 不要提示用户换个说法重试。锁是绑在验证身份上的，"
                    "换个说法不会解锁，只会让他多试一次。"
                )

            # 没解锁、但确实是危险操作 → 引导她走三级流程
            if gd.get("dangerous") and not gd.get("verified"):
                return (
                    "⛔ 这个操作要先验证，不能直接派。\n\n"
                    "它不是普通的活——照着做可能会删掉东西或者改坏环境。\n\n"
                    "按这个顺序来：\n"
                    "  1. 先调 confirm_dangerous_action(task=..., level=1) 提醒用户\n"
                    "  2. 用户还坚持 → level=2 警告\n"
                    "  3. 用户第三次坚持 → level=3 发验证码（120 秒有效）\n"
                    "  4. 用户报码后调 verify_dangerous_code 校验\n"
                    "  5. 通过了再回来调这个工具派活\n\n"
                    "（验证码会绑定这个任务本身。所以第 5 步的 task "
                    "要跟发码时一字不差。）\n\n"
                    "⚠️ 第 3 步一旦发出验证码，整个任务端都会锁上，"
                    "什么活都派不出去，直到用户验证通过。"
                )
        except Exception:  # noqa: BLE001
            # 检查接口不通时**放行**，不让桥本身挡住正常业务。
            # 代价是：guard 服务挂了就退化成"没有闸门"。
            # 这是有意的取舍——宁可少一层保护，也不要让用户以为程序坏了。
            pass
        if len(text) < 10:
            return f"「{text}」太短，电脑端会不知道要干什么。先说清楚再派。"

        # 任务名：用户给了就用，没给就**先问**，不要自作主张。
        #
        # ⚠️ 走过两版弯路：
        #    第一版：不给名字就让桥生成 t-20261028095727 时间戳。
        #            用户记不住，也没法按名字回来问"那个活到哪了"。
        #    第二版：用规则从任务描述里提名字。实测很糟——
        #            「登录页的按钮改大一点」→「登录页的按钮改大一点」（太长）
        #            「请看一下 D:\Project\demo」→「看一下DProje」（还被截断）
        #            中文里"一句话"和"一个名字"没有可分离的规则。
        #
        #    第三版（现在）：没名字就返回一句话，让**模型**去问用户。
        #    取名是语言活，交给模型，不要用规则硬凑。
        name = (task_name or "").strip()
        if not name:
            return (
                "这个活叫什么名字？\n"
                "起个短的好记的，以后想知道进度就说这个名字。\n"
                "（懒得想就回「随便」，我帮你取）"
            )

        # 算账：往一个已经很贵的窗口里派活时，**先拦住问一句**。
        #
        # 这一条返工过两次，两次的教训都记着：
        #
        #   第一版：拦下来问"要不要换新窗口"，但用户回「继续」时
        #           QQ 端没有对应的动作，只能重新派一条新任务，
        #           新任务又撞上同一个判断、又弹同一句 → 死循环，
        #           整条链路卡死在问候语上，任务永远派不出去。
        #
        #   第二版（错上加错）：改成"不拦，派完再提一句"。
        #           提示放到了事后，等于没提示——用户已经付了钱。
        #
        #   现在这版：照旧拦，但**「继续」有路可走了**——
        #           新增了 continue_computer_task 工具，
        #           用户说"继续"就真的往那个窗口里说一句，不会再触发这个检查。
        #           所以拦住是安全的，不会死循环。
        #
        # 判断只看"跑过至少一轮的贵窗口"：新窗口 cost 是 None，不打扰。
        name = (task_name or "").strip()
        if name:
            st = await asyncio.to_thread(self._call_bridge, ["stats", "--id", name], 20)
            if isinstance(st, dict) and st.get("status") not in ("not-found", None):
                cost = st.get("cost_cny")
                toks = st.get("context_tokens")
                if isinstance(cost, (int, float)) and isinstance(toks, int) and cost >= 0.5:
                    return (
                        f"「{name}」这个窗口已经比较满了。\n"
                        f"它每轮要带约 {toks:,} token，一次大概 {cost:.2f} 元。\n\n"
                        f"接着做 → 回我「继续」，我把它送进这个窗口\n"
                        f"换新窗 → 回我「新开」，我换个干净的窗口重来"
                    )

        # 1) 派活（0.2 秒）
        args = ["dispatch", text, "--id", name]
        pid = self._resolve_profile(model)
        if pid:
            args += ["--profile", pid]
        rec = await asyncio.to_thread(self._call_bridge, args)
        if rec.get("status") == "error":
            return f"派不出去：{rec.get('error')}"

        task_id = rec.get("id", "")
        logger.info(f"[桥] 派活 {task_id}：{text[:60]}")

        # 2) 起后台进程真干活，不等
        await self._spawn_worker(task_id)

        return (
            f"派出去了，这个活叫「{task_id}」。\n"
            "电脑那边会自己跑，我不在这儿等。\n"
            f"想知道进度就问「{task_id}到哪了」。"
        )

    @filter.llm_tool(name="continue_computer_task")
    async def continue_computer_task(self, event, task_name: str = "", message: str = ""):
        """往一个已有的任务窗口里接着说一句话，不新开窗口。

        什么时候用（这几种情况都走它，别再调 send_to_computer）：
            · 你刚问过「继续还是新开」，用户回「继续」/「接着做」/「对」
            · 用户在同一件事上追加要求（「再把颜色改一下」）
            · 任务还没做完，用户想补充说明

        ⚠️ 「继续」必须走这个工具，不能重派新任务。
            重派会被当成又开一件新活：可能又触发一遍成本检查、
            又弹一遍同一个问题，来回打转。实测卡死过整条链路——
            任务永远派不出去，全卡在问候语上。

            一句话区分：
                新的一件事  → send_to_computer
                还在同一件事里 → continue_computer_task

        它做的事很小：把这句话送进那个窗口，让原来的上下文接着走。

        Args:
            task_name(string): 往哪个任务窗口里说。就是之前回复里那个名字。
            message(string): 要说的这句话。可以很短，比如「继续」。
        """
        name = (task_name or "").strip()
        msg = (message or "").strip()
        if not name:
            return "要往哪个任务里说？告诉我任务名。"
        if not msg:
            msg = "继续"
        if not self.enabled:
            return "派活功能关着。"

        # 确认这个任务确实存在，别往空气里说话
        st = await asyncio.to_thread(self._call_bridge, ["stats", "--id", name], 20)
        if not isinstance(st, dict) or st.get("status") in ("not-found", None):
            return f"没有叫「{name}」的任务。你是不是想新派一个？"

        rec = await asyncio.to_thread(
            self._call_bridge, ["dispatch", msg, "--id", name], 30
        )
        if rec.get("status") == "error":
            return f"送不进去：{rec.get('error')}"

        await self._spawn_worker(name)
        return f"好，接着在「{name}」里说完了，它继续干。"

    @filter.llm_tool(name="list_computer_windows")
    async def list_computer_windows(self, event, model_name: str = ""):
        """看看电脑上已经有哪些任务窗口。

        什么时候用（很重要）：
            用户说「这件事继续做」「派给 Kimi 接着弄」的时候，
            先调这个看看有没有相关工作窗口，再决定是接着做还是新开。

        为什么要这么做：
            同一个活如果新开窗口，模型就得从头摸一遍——之前查过的
            路径、定过的方案全没了，做出来的东西跟之前对不上。
            用户的原话：「断了上下文，做出来的跟之前不一样」。

        怎么用结果：
            拿到窗口列表后，结合你们刚才的对话自己判断：
                · 有没有哪个窗口说的就是这次这件事？
                  有 → 问用户：「是不是放在『xxx』那个窗口接着做？」
                  没有 → 直接新开
                · 名字像但不是同一件事 → 也问一句，别自己决定

        注意：判断是你的活，不是这个工具的活。
        它只是把「现在有哪些窗口」给你看，要不要问、问什么由你定。

        Args:
            model_name(string): 只看这套模型配置下面的窗口。留空看全部。
        """
        try:
            import json as _json
            import urllib.parse as _up
            import urllib.request as _ur

            url = "http://127.0.0.1:8790/api/windows"
            want = (model_name or "").strip()
            if want:
                # 用户说的是配置的名字（比如"Kimi"），不是 id。
                # 先把名字对上 id，再按 id 过滤。
                pid = ""
                try:
                    with _ur.urlopen(
                _ur.Request("http://127.0.0.1:8790/api/engine/profiles", headers={"X-Token": _api_token()}),
                timeout=10,
            ) as pr:
                        pdata = _json.loads(pr.read())
                    for prof in (pdata.get("profiles") or []):
                        if want.lower() in (prof.get("name") or "").lower():
                            pid = prof.get("id") or ""
                            break
                except Exception:  # noqa: BLE001
                    pass
                if pid:
                    url += "?" + _up.urlencode({"profile": pid})

            with _ur.urlopen(url, timeout=15) as r:
                data = _json.loads(r.read())

            windows = (data or {}).get("windows") or []
            if not windows:
                return "电脑上目前没有任务窗口。"

            # 只给名字、状态、开头一段，别把全量塞进去
            lines = []
            for w in windows[:40]:
                head = (w.get("task") or "").replace("\n", " ")[:80]
                lines.append(f"- 「{w.get('name')}」[{w.get('status')}] {head}")
            more = f"\n（还有 {len(windows) - 40} 个）" if len(windows) > 40 else ""
            return "已有的任务窗口：\n" + "\n".join(lines) + more
        except Exception as e:  # noqa: BLE001
            return f"查窗口失败：{type(e).__name__}: {e}"

    @filter.llm_tool(name="list_computer_models")
    async def list_computer_models(self, event):
        """看看电脑上启用了哪几套模型，各自什么情况。

        什么时候用：
            · 用户说「用便宜的做」「派给强一点的模型」——你需要知道有哪些
            · 用户问「现在能用哪些模型」
            · 派活前想确认某个名字对不对

        返回什么：
            每套配置的名字、模型名、还有没有空位、最多同时跑几个。
            名字就是派活时 model 参数该填的值。

        别瞎猜模型名：
            不要凭空写「kimi」这种。先调这个拿到真实的名字，
            再拿那个名字去 send_to_computer。
        """
        try:
            import json as _json
            import urllib.request as _ur

            with _ur.urlopen(
                _ur.Request("http://127.0.0.1:8790/api/engine/profiles", headers={"X-Token": _api_token()}),
                timeout=10,
            ) as r:
                data = _json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            return f"查模型配置失败：{type(e).__name__}"

        profiles = data.get("profiles") or []
        active = data.get("active") or ""

        usable = []
        for p in profiles:
            if not p.get("enabled"):
                continue
            if not (p.get("has_key") and p.get("model")):
                continue
            usable.append(p)

        if not usable:
            return "现在没有可用的模型配置。用户得先去桌面端设置里填一套。"

        lines = []
        for p in usable:
            mark = "（默认）" if p.get("id") == active else ""
            lines.append(
                f"- 「{p.get('name')}」{mark}  模型 {p.get('model')}，"
                f"最多同时跑 {p.get('concurrency', 1)} 个"
            )

        skipped = len(profiles) - len(usable)
        tail = f"\n（另外 {skipped} 套没启用或者没填 Key）" if skipped else ""
        return "可用的模型配置：\n" + "\n".join(lines) + tail

    @filter.llm_tool(name="confirm_dangerous_action")
    async def confirm_dangerous_action(
        self, event, task: str, level: int = 1, purpose: str = ""
    ):
        """危险操作的三级确认。**必须走完才能派活。**

        ⚠️ 先说清楚为什么不能靠"问一句确认"：

            用户说「删库」，你回「确定吗？」，用户回「确定」——
            然后你删了。**这个流程挡不住任何恶意。**

            拿到手机的人能翻聊天记录、能学用户的语气、
            能说一百遍「确认」。对话通道一旦被拿到，
            你说的每一句话都是替贼开门。

            真正独立的通道得是**不在对话里的东西**——邮箱。
            所以第三级会往用户邮箱发一个验证码，
            那个码贼拿不到。

        三级怎么走：

            第 1 次（level=1）  提醒
                用户说了危险操作。你先正常回一句提醒，
                让用户有机会自己停下（他可能是说错话、
                用词夸张、或者手滑）。

            第 2 次（level=2）  警告
                用户还是坚持。再警告一次，说明后果不可逆。

            第 3 次（level=3）  验证码
                用户第三次坚持 → 调用本工具发验证码。
                然后问用户「验证码已发到你邮箱，告诉我」。

            用户报码后，用 verify_dangerous_code 校验。
            通过才真正调用 send_to_computer。

        什么时候算「一次」：
            用户说出危险要求 → 你给一次提醒，这就算第 1 次。
            下一句用户又说要做 → 第 2 次。
            再说 → 第 3 次，发码。

        Args:
            task(string): 要做的那件事（原样，别改写）
            level(int): 1=提醒 2=警告 3=发验证码
            purpose(string): 为什么用户要做这个（你理解的，可留空）
        """
        level = int(level or 1)

        # 1、2 级不发码，只是告诉你该说什么
        if level < 3:
            if level == 1:
                return (
                    "【第 1 级 · 提醒】\n"
                    "先别派。用你自己的话提醒用户这个操作的危险，"
                    "并问一句他是不是真要做这件事、要动哪个具体位置。\n"
                    "如果用户改口或者说得更具体了，正常派；"
                    "如果用户还是坚持，下一次用 level=2。"
                )
            return (
                "【第 2 级 · 警告】\n"
                "明确告诉用户：这个操作不可逆，做错了恢复不了。"
                "再确认一次他是不是真的要。\n"
                "如果用户第三次坚持，用 level=3 发验证码。"
            )

        # 第 3 级：发验证码
        try:
            import urllib.request

            req = urllib.request.Request(
                "http://127.0.0.1:8790/api/guard/code",
                data=json.dumps({
                    "task": task,
                    "purpose": purpose,
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token": _api_token(),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read())

            if not d.get("ok"):
                return (
                    f"发验证码失败：{d.get('message') or d.get('error')}\n"
                    "告诉用户安全邮箱可能没配好，让他去桌面端检查。"
                    "**这个操作先别派。**"
                )

            return (
                f"【第 3 级 · 验证码已发】{d.get('message') or ''}\n\n"
                "现在告诉用户：验证码已经发到他的邮箱，"
                "让他报过来。\n"
                "拿到码之后调用 verify_dangerous_code 校验，"
                "通过了才派活。\n"
                "⚠️ 不要自己猜码，也不要跳过后直接派。"
            )
        except Exception as e:  # noqa: BLE001
            return f"发验证码出错：{type(e).__name__}: {e}。这个操作先别派。"

    @filter.llm_tool(name="verify_dangerous_code")
    async def verify_dangerous_code(self, event, task: str, code: str):
        """校验用户报过来的验证码。

        通过了才能派活。没通过就告诉用户还剩几次机会。

        重要：
            · 验证码绑定具体任务。给「删 A 库」发的码，
              不能拿去「删 B 库」——所以 task 必须跟发码时一模一样。
            · 一次性。验过一次就作废。
            · 最多 3 次。超了锁 10 分钟。

        Args:
            task(string): 要做的那件事（跟发码时一字不差）
            code(string): 用户报的 6 位数字
        """
        try:
            import urllib.request

            req = urllib.request.Request(
                "http://127.0.0.1:8790/api/guard/verify",
                data=json.dumps({
                    "task": task,
                    "code": (code or "").strip(),
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "X-Token": _api_token(),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.loads(r.read())

            if d.get("ok"):
                return (
                    "✅ 验证通过。现在可以派活了——用原样的 task 调用 "
                    "send_to_computer。"
                )
            return f"❌ {d.get('message') or d.get('error')}"

        except Exception as e:  # noqa: BLE001
            return f"校验出错：{type(e).__name__}: {e}"

    @filter.llm_tool(name="check_computer_task")
    async def check_computer_task(self, event, task_id: str = "", count=3):
        """看看电脑那边派出去的活做到哪一步了。

        不知道任务号、只想看最近的，task_id 留空就行。

        Args:
            task_id(string): 任务号，比如 t-20261027104224。留空看最近的。
            count(int): 留空看几条，默认 3
        """
        try:
            n = max(1, min(int(count or 3), 10))
        except (TypeError, ValueError):
            n = 3

        # 先拿一次 live（有心跳信息），用来判断 running 是真在跑还是卡住了
        live = await asyncio.to_thread(self._call_bridge, ["live"], 30)
        if not isinstance(live, dict):
            live = None

        tid = (task_id or "").strip()
        if tid:
            # 优先走 state：又快，又带花费数据
            st = await asyncio.to_thread(self._call_bridge, ["stats", "--id", tid], 20)
            if isinstance(st, dict) and st.get("status") not in (None, "not-found"):
                text = self._fmt_state(st)
                return text
            # state 里没有（老任务）→ 退回 tasks.jsonl
            rec = await asyncio.to_thread(
                self._call_bridge, ["status", "--id", tid]
            )
            if isinstance(rec, dict):
                return self._fmt_task(rec, live)
            return f"查不到 {tid}。"

        rec = await asyncio.to_thread(
            self._call_bridge, ["status", "--limit", str(n)]
        )
        if isinstance(rec, dict):
            return self._fmt_task(rec, live)
        if not rec:
            return "还没派过活。"

        lines = []
        for t in rec:
            lines.append(self._fmt_task(t, live))
        return "\n\n".join(lines)

    @filter.llm_tool(name="check_computer_asks")
    async def check_computer_asks(self, event, count=3):
        """看看电脑端有没有卡住、推回来问你的。

        它在拿不到某个关键信息时（网页打不开、被限流），
        不会死磕，会写一条"缺什么"然后停下。
        这个工具就是读那些。

        任务状态显示"做完了"但结果不完整时，也值得看一眼这里。

        Args:
            count(int): 看几条，默认 3
        """
        try:
            n = max(1, min(int(count or 3), 10))
        except (TypeError, ValueError):
            n = 3

        rows = await asyncio.to_thread(
            self._call_bridge, ["ask", "--limit", str(n)], 30
        )
        if not isinstance(rows, list):
            return "读不到，或者那边没写这个文件。"
        if not rows:
            return "没有卡住的地方，最近的任务都跑下来了。"

        lines = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            need = (r.get("need") or r.get("raw") or "").strip()
            why = (r.get("why") or "").strip()
            at = (r.get("at") or "")[:16]
            line = f"· {need}"
            if why:
                line += f"\n  原因：{why}"
            if at:
                line += f"\n  时间：{at}"
            lines.append(line)
        return "电脑端卡在这些地方：\n" + "\n".join(lines)

    @filter.llm_tool(name="cancel_computer_task")
    async def cancel_computer_task(self, event, task_id: str = ""):
        """停掉一个卡住或跑太久的电脑端任务。

        什么时候用：
            · 任务显示"没心跳了"，明显卡死
            · 跑太久，不想要了
            · 派错了

        注意：这是终止，不是暂停。停了就得重派。

        Args:
            task_id(string): 要停的任务号
        """
        tid = (task_id or "").strip()
        if not tid:
            return "得给个任务号，不然不知道停哪个。"
        rec = await asyncio.to_thread(
            self._call_bridge, ["cancel", "--id", tid], 60
        )
        if not isinstance(rec, dict):
            return f"停不掉 {tid}。"
        st = rec.get("status")
        if st == "cancelled":
            return f"停掉了（{tid}）。"
        if st == "not-found":
            return f"没有 {tid} 这个任务。"
        if st == "not-running":
            return f"{tid} 现在不是运行状态（{rec.get('current', '?')}），不用停。"
        return f"停了，但结果不确定：{rec}"

    # ------------------------------------------------------------------
    # 调电脑端的模型和推理强度
    # ------------------------------------------------------------------

    @filter.llm_tool(name="list_task_permissions")
    async def list_task_permissions(self, event):
        """看每个窗口现在是什么权限。

        权限有三档：
            工作区    只能动它自己那个任务目录（默认）
            用户目录  能动你用户目录下的东西
            完全      整个电脑都能动

        **默认都是工作区。** 要碰外面得单独提权。

        ⚠️ 怎么把结果告诉用户：
            直接把我返回的内容转述过去就行，**别自己再排一遍表**。

            为什么：
                这里返回的是已经排好的紧凑格式。你要是再加工成
                Markdown 表格，窗口名里的顿号、括号很容易跟表格的
                分隔符撞上，整张表会错位——实测过一次，
                42 个窗口挤在一格里，看着像乱码。

            什么时候可以用表：
                最多三五行、每格都是短词的时候（比如"模型 / 数量"）。
                长内容一律**分行列**，不要塞表格。
        """
        # ⚠️ 这里传 None 不是 {}。
        #
        #    _call_api 的逻辑是 method="POST" if data is not None else "GET"，
        #    所以传 {} 会被当成 POST —— 而 POST /api/perms 是"改权限"，
        #    它要求必须给 name，于是返回 "没说是哪个窗口"。
        #
        #    实测症状：用户说"看看窗口权限"，前台回报"查不到：没说是哪个窗口"。
        #    它查的其实是改权限的那个接口。
        d = await asyncio.to_thread(self._call_api, "/api/perms", None)
        if not d.get("ok"):
            return f"查不到：{d.get('error', '')}"

        labels = d.get("labels") or {}
        tasks = d.get("tasks") or []
        if not tasks:
            return "还没有窗口。"

        # ⚠️ 这里原来写的是 tasks[-20:]。
        #
        #    症状：用户问"你好6~15 的权限"，前台回"不在表里，
        #          返回的 20 条全是别的窗口"。
        #          它甚至自己猜成"这个接口查的是权限台账，不是窗口清单"。
        #
        #    真相很简单：只取了最后 20 个，前面 42 个被切掉了。
        #    62 个窗口只显示 20 个，用户当然找不到自己刚派的活。
        #
        #    现在按**模型配置**分组显示，该有的都在：
        #        每套配置下有哪些窗口、各自什么权限。

        # 按 profile 分组
        groups: dict[str, list[dict]] = {}
        for t in tasks:
            pid = t.get("profile") or ""
            groups.setdefault(pid, []).append(t)

        # 拿配置名字
        names = {}
        try:
            pdata = await asyncio.to_thread(
                self._call_api, "/api/engine/profiles", None
            )
            for prof in (pdata.get("profiles") or []):
                names[prof.get("id") or ""] = prof.get("name") or ""
        except Exception:  # noqa: BLE001
            pass

        # 输出做成**一张能直接发的表**。
        #
        # ⚠️ 这里改过两次，原因是呈现效果。
        #
        #    第一版：每个窗口一行（· 你好1 / · 你好2 ...）
        #           62 个窗口 = 62 行，前台自己压成了"你好 1、2、3、
        #           4、5、9..."塞进一格，而分隔符 `|` 跟 Markdown
        #           表格的列分隔符撞了，飞书把内容当成了新列——
        #           整个表错位。
        #
        #    第二版（现在）：每个模型一行，窗口名用「、」连起来，
        #           超过一部分就省略。
        #           好处是：一行一个模型，宽度可控，不撞分隔符。

        out = []
        out.append(f"共 {len(tasks)} 个窗口，按模型分组。默认全是工作区权限。")
        out.append("")

        for pid, items in groups.items():
            title = names.get(pid) or ("默认" if not pid else pid)

            # 有提权的单独标出来
            raised = [
                t for t in items
                if (t.get("level") or "workspace") != "workspace"
            ]

            ns = [t.get("name") or "" for t in items]
            # 一格里最多列 12 个，多了省略。
            # 12 是因为中文名字平均 4-6 字，12 个大约一行半，
            # 再读得下去；同时不会把表格撑爆。
            shown = "、".join(ns[:12])
            if len(ns) > 12:
                shown += f"……（还有 {len(ns) - 12} 个）"

            perm = "全是工作区"
            if raised:
                perm = "；".join(
                    f"{t.get('name')} → {labels.get(t.get('level'), t.get('level'))}"
                    for t in raised
                )
            out.append(f"【{title}】{len(items)} 个：{shown}")
            out.append(f"    权限：{perm}")
            out.append("")

        out.append("要改权限就说「把某个窗口开到完全权限」——升权会要验证码。")
        return "\n".join(out)

    @filter.llm_tool(name="set_task_permission")
    async def set_task_permission(self, event, task_name: str = "",
                                  level: str = "", code: str = ""):
        """给某个窗口改权限。

        参数：
            task_name  哪个窗口（任务名，就是派活时起的那个名字）
            level      workspace / home / full
            code       验证码。**升权时第一次不要传**，系统会发码到邮箱，
                       用户把码报给你之后，再带 code 调一次。

        ⚠️ 规矩：
            一、只能改**一个**窗口，说名字。不能批量、不能通配。
            二、升权（放宽）必须过验证码；降权（收窄）直接改。
            三、只在用户**明确要求**时用。不要自己觉得"这活需要更多权限"
                就去提权——提权是件用户该知道的事。
        """
        if not task_name:
            return "要改哪个窗口？说任务名。"

        if level not in ("workspace", "home", "full"):
            return (
                f"权限只有三档：workspace（工作区）/ home（用户目录）/ "
                f"full（完全），你给的是 {level}"
            )

        d = await asyncio.to_thread(
            self._call_api, "/api/perms",
            {"name": task_name, "level": level, "code": code},
        )

        # 需要验证码
        if d.get("need_code"):
            frm = d.get("from") or "工作区"
            return (
                f"「{task_name}」现在是 {frm}，要开到「{level}」得先验证身份。\n"
                f"{d.get('message') or d.get('error') or '验证码已发到邮箱。'}\n"
                f"把码报给我，我来改。"
            )

        if not d.get("ok"):
            return f"没改成：{d.get('error', '')}"

        label = d.get("label") or level
        hint = d.get("hint") or ""
        frm = d.get("from") or ""
        changed = f"（{frm} → {label}）" if frm and frm != label else f"（{label}）"
        return f"改好了：\u300c{task_name}\u300d{changed}——{hint}"





    def _call_tune(self, args: list[str], timeout: int = 30) -> dict:
        """调 tune_model.py。"""
        import json as _json
        tune = os.path.join(BRIDGE_DIR, "tune_model.py")
        if not os.path.exists(tune):
            return {"status": "error", "error": "找不到 tune_model.py"}
        try:
            child_env = dict(os.environ)
            child_env["PYTHONIOENCODING"] = "utf-8"
            child_env["PYTHONUTF8"] = "1"
            proc = subprocess.run(
                [self.python, tune] + args,
                capture_output=True, timeout=timeout,
                cwd=BRIDGE_DIR, env=child_env,
            )
            out = _decode_any(proc.stdout or b"").strip()
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    return _json.loads(line)
            err = _decode_any(proc.stderr or b"").strip()
            return {"status": "error", "error": (err or out or "没有输出")[:300]}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}

    # tune_computer_model 已删除。
    #
    # 它管的是 codebuddy 的三条通道（cb-flash / cb-pro / official-flash），
    # 那是 DSH 时代的东西。现在任务端走自研引擎 + 用户自己的 Key。
    #
    # ⚠️ 为什么必须删而不是留着：
    #    留着它，模型问「能用哪些模型」时会选中它——
    #    实测就发生了，返回"现在走 codebuddy，模型 deepseek-v4.1-flash"，
    #    全是早没了的信息，用户被误导。两个工具都在讲"模型"，模型分不清。
    #
    # 现在：查模型走 list_computer_models，指定模型走 send_to_computer 的 model 参数。
