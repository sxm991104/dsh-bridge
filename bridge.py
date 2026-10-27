"""桥梁：派活、执行、查状态。**派完就走，不干等**。

核心设计：复用同一个会话
    ⚠️ 这是整个方案最关键的一处，之前走过弯路：
        原来每次 `dsh qqme "任务"` 都是干净的新会话，
        新进程不知道上一次干过什么，等于每次都是"第一次见你"。
        任务号只是个序号，没有连接作用。

        后来发现 headless 支持 `--session-id <id>`，文档写的是 **adopt**
        （接手已存在的会话），不是新建。
        实测：进程 A 记住"鲸鱼七号"，进程 B（完全新开）问暗号，答得出。

        所以现在：第一次跑生成会话 ID 存到 `.session_id`，
        之后每次都用同一个 —— **换进程，但窗口是同一个。**

    会话本身在：
        %DSH_HOME%\\sessions\\<工作目录转义>\\session-xxx/
    上下文窗口 100 万 token，够用很久；真满了就删 .session_id 重建。

数据存哪
    tasks.jsonl    任务清单（一行一条，状态在里面）
    results.jsonl  执行结果历史
    .session_id    复用的会话号
    .progress.json 正在跑的任务的实时进度（心跳）

用法
    python bridge.py dispatch "任务内容"   → 写一条，立刻返回（不等）
    python bridge.py run --id <任务id>     → 真干活（后台调）
    python bridge.py status [--id X]       → 看状态
    python bridge.py live                  → 正在跑的那条，此刻在干什么
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime

BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(BRIDGE_DIR, "tasks.jsonl")
RESULTS = os.path.join(BRIDGE_DIR, "results.jsonl")
SESSION_FILE = os.path.join(BRIDGE_DIR, ".session_id")

# 完成通知箱：任务一跑完就往这里追加一条。
# QQ 端插件盯着这个文件，看到新的就主动推给用户。
# 为什么用文件而不是直接发消息：
#   电脑端那个进程不知道 QQ 端的会话信息，也不该知道。
#   写文件最松耦合——它只管说"我做完了"，
#   谁去通知、怎么通知，是 QQ 端的事。
#   而且用户不在线时，通知就躺在文件里，不会丢。
OUTBOX = os.path.join(BRIDGE_DIR, "outbox.jsonl")

# 实时进度心跳。之前只在文档里写过，代码里其实没有——
# 结果是一条长任务在跑的时候，status 永远只显示 "running"，
# 看不出它卡在启动、在模型里转圈，还是快好了。
# 现在：真的跑 dsh 的进程每 HEARTBEAT_SECONDS 秒盖一个章。
PROGRESS = os.path.join(BRIDGE_DIR, ".progress.json")
HEARTBEAT_SECONDS = 5

# dsh 可执行文件的候选位置。
# 优先用环境变量 DSH_CMD 指定，其次找 PATH，最后才是 npm 全局默认路径。
# （开源时不能把某一台机器的用户名路径写死）
def _dsh_candidates() -> list[str]:
    out = []
    env_cmd = os.environ.get("DSH_CMD", "").strip()
    if env_cmd:
        out.append(env_cmd)
    from shutil import which
    for name in ("dsh.cmd", "dsh"):
        p = which(name)
        if p:
            out.append(p)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        out.append(os.path.join(appdata, "npm", "dsh.cmd"))
        out.append(os.path.join(appdata, "npm", "dsh"))
    return [x for x in out if x]


DSH_CANDIDATES = _dsh_candidates()


# ----------------------------------------------------------------------
# 基础设施
# ----------------------------------------------------------------------


def _decode_any(raw: bytes) -> str:
    """子进程字节 → 文字，UTF-8 优先，失败退 GBK。

    ⚠️ 踩了两轮才找对的，别再改回头：
        强设 encoding="utf-8" 没用——字节已经在子进程那边按 GBK 生成了。
        给子进程传 PYTHONIOENCODING 也没用——dsh.cmd 是批处理，带不下去。
        自己拿 bytes 判断编码，最稳。
    """
    for enc in ("utf-8", "gbk", "cp936"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def find_dsh() -> str | None:
    for p in DSH_CANDIDATES:
        if os.path.exists(p):
            return p
    from shutil import which

    return which("dsh")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_session_id() -> str:
    """读已存的会话号；没有就返回空。

    ⚠️ utf-8-sig 不是随便写的：这个文件被记事本/PowerShell 碰过之后
    会带上 BOM，读出来第一个字符是 \\ufeff，
    拼进 `--session-id` 就成了一个不存在的会话号，桥报 does not exist。
    吞掉 BOM。
    """
    try:
        with open(SESSION_FILE, encoding="utf-8-sig") as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_session_id(sid: str) -> None:
    """写会话号。强制 UTF-8 无 BOM、不带换行。"""
    sid = (sid or "").strip().lstrip("\ufeff")
    if not sid:
        return
    try:
        # newline="" 防止 Windows 把 \n 变成 \r\n，把会话号弄脏
        with open(SESSION_FILE, "w", encoding="utf-8", newline="") as f:
            f.write(sid)
    except OSError:
        pass


def _read_tasks() -> list[dict]:
    if not os.path.exists(TASKS):
        return []
    out = []
    with open(TASKS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_tasks(tasks: list[dict]) -> None:
    tmp = TASKS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for t in tasks:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    os.replace(tmp, TASKS)


def _update_task(task_id: str, **fields) -> dict | None:
    tasks = _read_tasks()
    found = None
    for t in tasks:
        if t.get("id") == task_id:
            t.update(fields)
            t["updated_at"] = _now()
            found = t
            break
    if found:
        _write_tasks(tasks)
    return found


def _looks_like_crash(raw: str) -> str:
    """看这是不是进程自己炸了的痕迹。返回命中的特征，没炸就空串。

    为什么要单独判断：
        原来 final 拿不到时会退回 _clean(stdout)，
        而 _clean 只做"扔掉格式行"，任何非空文本都能留下来。
        于是 dsh 启动崩溃（Node 的 EPERM 堆栈）被当成正常答案，
        任务记成 status=ok，QQ 那头收到一坨堆栈还以为是结果。
    """
    signals = [
        ("Error: EPERM", "写文件被系统拒绝（EPERM）"),
        ("Error: EACCES", "权限不足（EACCES）"),
        ("Error: ENOENT", "文件或目录不存在（ENOENT）"),
        ("node:fs:", "Node 文件系统异常"),
        ("node:internal", "Node 内部异常"),
        ("Node.js v", "Node 异常退出，打印了版本栈"),
        ("throw er;", "Node 未捕获异常"),
        ("SyntaxError:", "脚本语法错误"),
        ("Traceback (most recent call last)", "Python 异常栈"),
        ("is not recognized as an internal", "命令不存在"),
    ]
    for sig, why in signals:
        if sig in raw:
            return why
    return ""


def _clean(raw: str) -> str:
    """剥掉推理过程和错误格式行，只留答案。"""
    kept = []
    for line in raw.splitlines():
        s = line.rstrip()
        if not s.strip():
            continue
        if s.startswith("dsh: reasoning:"):
            continue
        if "CategoryInfo" in s or "FullyQualifiedErrorId" in s:
            continue
        if s.startswith("所在位置") or s.startswith("At "):
            continue
        if s.startswith("+ ") and "~~" in s:
            continue
        kept.append(s)
    return "\n".join(kept).strip()


# ----------------------------------------------------------------------
# 实时进度心跳
# ----------------------------------------------------------------------


def _write_progress(rec: dict) -> None:
    """原子地写 .progress.json。别让读的一方读到写了一半的 JSON。"""
    tmp = PROGRESS + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False)
        os.replace(tmp, PROGRESS)
    except OSError:
        pass


def _read_progress() -> dict | None:
    try:
        with open(PROGRESS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _clear_progress() -> None:
    try:
        os.remove(PROGRESS)
    except OSError:
        pass


def _activity_from_events(lines: list[str]) -> str:
    """从已经收到的 JSON 事件里，推断"它此刻在干什么"。

    为什么要这块（原话来自实测）：
        "看不到它在做什么，跑错了也没法及时收住。"
        "没有回传，就没有控制。"

    怎么推断的：
        dsh 的 --json 事件流里有 tool_call（调了什么工具，带参数）。
        取**最后一条** tool_call，就是它最近的动作。
        取不到就看最后一条 text（它在说什么）。

    只读已经收到的行，不阻塞、不额外开通道。
    """
    if not lines:
        return ""
    last_tool = ""
    last_text = ""
    # 从后往前找，找到就停——通常最后几条里就有
    for raw in reversed(lines[-200:]):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = ev.get("type")
        if et == "tool_call" and not last_tool:
            # 真实结构（实测抓到的）：
            #   {"type":"tool_call","callId":"...","tool":"pwsh",
            #    "input":{"command":"Get-Date","description":"..."}}
            # 字段是 tool / input，不是 name / args。
            name = ev.get("tool") or ev.get("name") or ""
            args = ev.get("input") or ev.get("args") or {}
            if isinstance(args, dict):
                # 参数里最有信息量的几个键
                for k in ("command", "cmd", "description", "path", "file_path",
                          "pattern", "query", "url", "workdir"):
                    if k in args:
                        v = str(args[k]).replace("\n", " ").strip()
                        # ⚠️ 命令要截短，只留开头。
                        #    实测一条 pwsh 命令能两百多字（一堆 Select-Object 的
                        #    列名和格式化参数），全贴出来别人也看不出在干什么。
                        #    取第一段（管道符、分号前面），再砍到 70 字。
                        head = v
                        for sep in (" |", ";", " &&"):
                            idx = head.find(sep)
                            if 0 < idx < 90:
                                head = head[:idx]
                                break
                        head = head[:70]
                        desc = str(args.get("description") or "").strip()
                        if desc and len(desc) < 40:
                            last_tool = f"{name}: {head}（{desc}）"
                        else:
                            last_tool = f"{name}: {head}"
                        break
                else:
                    compact = json.dumps(args, ensure_ascii=False)[:80]
                    last_tool = f"{name} {compact}"
            else:
                last_tool = f"{name} {str(args)[:120]}"
        elif et == "text" and not last_text:
            t = (ev.get("text") or "").strip().replace("\n", " ")
            if t:
                last_text = t[:160]
        if last_tool:
            break
    return last_tool or last_text


def _kill_tree(proc: subprocess.Popen) -> None:
    """超时后连根拔掉，别留孤儿 node 进程在后台空转。

    dsh.cmd 是批处理，proc 只是 cmd.exe。
    taskkill /T 才收得掉整棵树；收不掉就退回只杀这一个。
    """
    if proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            timeout=15,
        )
    except Exception:  # noqa: BLE001  没 taskkill（非 Windows）就算了
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------
# 调用 dsh（带会话复用）
# ----------------------------------------------------------------------


ASK_FILE = os.path.join(BRIDGE_DIR, "ask.jsonl")

# 给每个任务固定的"交差规则"。
#
# 为什么需要这段（这是返工过的，原因值得留着）：
#     派过一次查资料的活，跑了 28 分钟还没完。查了事件流才发现：
#     它核心结论十分钟就拿到了，后面全在追新冒出来的相关论文
#     （查"压缩摘要"追到 YaRN，又从 YaRN 追到 Infini-attention），
#     遇到 429 限流还一遍遍重试。
#
#     每一步都合理，合起来无底洞。根本原因是**"调研"这类任务没有天然终点**：
#     写代码跑通了就是通了；查资料永远能再查一个。
#
#     所以给它三条判断，让它自己知道什么时候该交差：
#         · 能回答用户的问题了 → 交
#         · 缺关键信息且拿不到 → 写下来、停、问
#         · 还差但知道去哪找 → 继续
#     这三条不管什么任务都适用，不是写死具体做法。
TASK_RULES = """

---
【交差规则】（不管任务内容是什么，这三条都要遵守）

一、**能回答了就交。** 如果你手上已经有的东西足以回答上面的问题，
   立刻停下来给出答案。不要再去找"更全面的资料"——
   多查的那部分通常用不上，却会拖很久。

二、**拿不到就记下来，然后停，别死磕。** 判定标准很简单：
   只要这次任务里有**任何一个关键信息没拿到**（网页打不开、被限流、
   找不到、接口报错），就算"卡住"，哪怕你最后还是答了个大概。

   遇到这种情况，**必须**追加一行到：
       {ask_file}
   格式（一行一个 JSON）：
       {{"need":"缺什么","why":"为什么拿不到","at":"{{now}}"}}
   然后停下来，把已知的部分作为答案交出来。

   注意：**失败也要记。** 上面那个"这个域名不存在"的答案，
   就是典型的该记一条的情况——用户需要知道"你没拿到"，
   而不是只看到一个空结果。
   如果全程顺利、什么也没缺，就不用写这个文件。

三、**别扩大范围。** 只做上面要求的事。查到的东西里如果牵出别的
   题目，记一句"另见：XXX"就够，不要顺着往下查。
"""


def _wrap_task(task: str) -> str:
    """给任务加上交差规则，再交给电脑端。"""
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return task.rstrip() + TASK_RULES.format(ask_file=ASK_FILE, now=now)


def _invoke_dsh(task: str, timeout: int, task_id: str = "") -> dict:
    """跑一次 dsh qqme，复用会话。

    返回 {"status":..., "result":..., "error":..., "seconds":..., "session_id":...}

    流程：
        1. 有 .session_id → 带 --session-id 接手
        2. 没有 → 不带，让它新建，然后从输出里抓 sessionId 存下来
        3. 如果带了旧 id 报 "does not exist"（会话被删了）
           → 去掉 id 重跑一次，重建会话（不能让桥因为会话丢了就永久失效）

    为什么不再是 subprocess.run(timeout=...)
        原来那一版 timeout 只管"最外层跑多久"。dsh 是 .cmd，
        Windows 上杀批处理经常只杀掉 cmd.exe，底下的 node 还活着，
        卡住的子进程会一直挂在后台。
        现在自己起进程 + 定时器：超时就 kill 整棵树，顺便每 HEARTBEAT_SECONDS
        秒盖一个心跳章，status 才能看出活没活着。
    """
    dsh = find_dsh()
    if not dsh:
        return {"status": "error", "error": "找不到 dsh 命令"}

    child_env = dict(os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    # 官方 DeepSeek 的 key：从 DSH 的凭据文件里取，不写死在代码里。
    #
    # 为什么要切官方通道（这是用户明确要求的）：
    #   codebuddy 有每日额度，超了任务就哑了——而人在公司时没法充值。
    #   官方 API 是按量付费，没钱手机充一下就行，不会卡在半路。
    #   顺带一个好处：官方通道实测支持图片（read_image 真能看见），
    #   codebuddy 那条在 adapter.js 里写死了 reject 图片。
    if not child_env.get("DEEPSEEK_API_KEY"):
        try:
            cred_path = os.path.join(
                os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh"),
                ".credentials.yaml",
            )
            with open(cred_path, encoding="utf-8") as cf:
                for line in cf:
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY:"):
                        val = line.split(":", 1)[1].strip().strip("\"'")
                        if val:
                            child_env["DEEPSEEK_API_KEY"] = val
                        break
        except OSError:
            pass

    # ⚠️ 权限模式：任务端的我，要跟桌面版一样能碰整台机器。
    #
    #    DSH 沙箱默认 workspace-write —— 只能写**一个**工作目录。
    #    实测症状：让它去 D:\Project 建个文件，报
    #        Access denied  [sandbox: file access denied under workspace-write]
    #    换 cwd 只能换个"能写的地方"，治不了"要同时写两处"。
    #
    #    根子在这（--dump-config 看到的）：
    #        - id: sandbox-policy
    #          config:
    #            mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    #    所以不用改配置文件，**塞一个环境变量就够**。
    #
    #    代价要清楚：它现在跟桌面版的我同一个权限，能读写这台机器上任何地方。
    #    所以"心跳看得到在干什么"和"能随时停掉"这两条才变得必要——都已经有了。
    child_env["DSH_PERMISSION_MODE"] = "danger-full-access"

    def _once(sid: str) -> tuple[dict, str]:
        args = [dsh, "qqme", "--json"]
        if sid:
            args += ["--session-id", sid]
        args.append(task)

        started = time.time()
        out_chunks: list[str] = []
        err_chunks: list[str] = []

        def _drain(stream, sink: list[str]) -> None:
            """一行一行读子进程输出，边读边存。

            ⚠️ 原来写的是 stream.read() —— 那是**阻塞到进程结束才返回**的。
               结果：心跳每 5 秒去读 out_chunks，永远是空列表，
               所以 "doing" 字段一直是空的，用户看不到它到底在做什么。

               改成 readline 循环：每来一行就存一行，
               心跳读 sink 时就能看到"最近调用了哪个工具"。
               顺带一个副作用也是需要的：逐行消费管道才不会写满缓冲区死锁。
            """
            if stream is None:
                return
            try:
                while True:
                    raw = stream.readline()
                    if not raw:
                        break
                    line = _decode_any(raw)
                    if line:
                        sink.append(line)
            except Exception:  # noqa: BLE001  读关掉的管道不算错
                pass
            finally:
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
        try:
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=BRIDGE_DIR,
                shell=False,
                env=child_env,
            )
        except Exception as e:  # noqa: BLE001
            return (
                {
                    "status": "error",
                    "result": "",
                    "error": f"{type(e).__name__}: {e}",
                    "seconds": 0,
                    "session_id": sid,
                },
                sid,
            )

        # 两条管道各一个读线程，不然死锁：写满一边缓冲区谁都不动
        readers = [
            threading.Thread(target=_drain, args=(proc.stdout, out_chunks), daemon=True),
            threading.Thread(target=_drain, args=(proc.stderr, err_chunks), daemon=True),
        ]
        for r in readers:
            r.start()

        timed_out = False
        last_beat = 0.0
        try:
            while True:
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass

                now = time.time()
                if now - started > timeout:
                    timed_out = True
                    break
                # 心跳：让 status 看得出这活儿还在动
                if now - last_beat >= HEARTBEAT_SECONDS:
                    last_beat = now
                    # 从已经收到的行里推断"此刻在干什么"。
                    # 只在心跳时算一次（每几秒），不影响主循环。
                    doing = _activity_from_events(out_chunks)
                    _write_progress(
                        {
                            "id": task_id,
                            "status": "running",
                            "phase": "running",
                            "pid": proc.pid,
                            "started_at": datetime.fromtimestamp(started)
                            .astimezone()
                            .isoformat(timespec="seconds"),
                            "elapsed_seconds": round(now - started, 1),
                            "heartbeat_at": _now(),
                            "session_id": sid,
                            "task": task[:200],
                            # 当前动作。空字符串表示"还没抓到值得说的动作"
                            # （刚开始、或者它只是在思考没调工具）。
                            "doing": doing,
                        }
                    )
        finally:
            if timed_out:
                _kill_tree(proc)
            for r in readers:
                r.join(timeout=5)

        out = "".join(out_chunks)
        errtxt = "".join(err_chunks)
        elapsed = round(time.time() - started, 1)

        if timed_out:
            return (
                {
                    "status": "timeout",
                    "result": "",
                    "error": f"超过 {timeout} 秒没跑完",
                    "seconds": elapsed,
                    "session_id": sid,
                },
                sid,
            )

        # 从事件流里找 sessionId 和最终答案
        real_sid = ""
        final_text = ""
        error_msg = ""
        had_final = False
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            et = ev.get("type")
            if et == "session" and not real_sid:
                real_sid = ev.get("sessionId") or ""
            elif et == "final":
                final_text = (ev.get("text") or "").strip()
                had_final = True
            elif et == "error":
                error_msg = ev.get("message") or ""
            elif et == "status" and ev.get("phase") == "turn_end":
                # ⚠️ 这里原本漏了一种错误，代价很实在：
                #
                #   dsh 把"这一轮跑失败了"包在 status/turn_end 里，形状是
                #       {"type":"status","phase":"turn_end",
                #        "reason":{"kind":"error","error":{...}}}
                #   而不是顶层 type=error。
                #
                #   原来只认 type=error，于是这类失败被当成"跑完了但没说话"，
                #   状态还记成 ok。实测遇过一次：
                #       推理强度设成 medium → 模型不支持 → 真失败
                #       但任务显示成功，耗时 12.6 秒，结果是空字符串。
                #   假成功比失败更害人：用户以为活干完了。
                reason = ev.get("reason") or {}
                if reason.get("kind") == "error":
                    inner = reason.get("error") or {}
                    if isinstance(inner, dict):
                        msg = inner.get("message") or ""
                        code = inner.get("code") or ""
                        error_msg = f"{msg}" + (f"（{code}）" if code else "")
                    else:
                        error_msg = str(inner)

        if error_msg and not final_text:
            return (
                {
                    "status": "error",
                    "result": "",
                    "error": error_msg,
                    "seconds": elapsed,
                    "session_id": real_sid or sid,
                },
                real_sid or sid,
            )

        # 事件流拼不出答案时，退回清洗过的 stdout
        if not final_text:
            final_text = _clean(out)
        if not final_text:
            final_text = _clean(errtxt)

        # 走到这里还"有答案"，得先确认它到底是答案还是进程崩溃的堆栈。
        # 没有真 final 事件 + 输出像崩溃 + 返回码非 0 → 这是失败，不是结果。
        if final_text and not had_final:
            crash = _looks_like_crash(out + "\n" + errtxt)
            if crash and proc.returncode != 0:
                return (
                    {
                        "status": "error",
                        "result": "",
                        "error": f"dsh 没跑起来（{crash}，返回码 {proc.returncode}）："
                        + final_text[:400],
                        "seconds": elapsed,
                        "session_id": real_sid or sid,
                    },
                    real_sid or sid,
                )

        status = "ok" if final_text else "empty"
        return (
            {
                "status": status,
                "result": final_text,
                "error": "" if final_text else f"没输出（返回码 {proc.returncode}）",
                "seconds": elapsed,
                "session_id": real_sid or sid,
            },
            real_sid or sid,
        )

    sid = _read_session_id()
    try:
        rec, used_sid = _once(sid)

        # 会话丢了（被删/清理过）→ 重建一次，别让桥永久失效
        if rec.get("status") == "error" and "does not exist" in (rec.get("error") or ""):
            rec, used_sid = _once("")
            rec["rebuilt_session"] = True
    finally:
        # 心跳文件只代表"正在跑"，跑完就是过期数据。
        # 放在这里而不是 cmd_run：谁调都得收干净，别留个假 running 骗人。
        _clear_progress()

    # 只在真拿到新会话号时才落盘。
    # 原来只要 used_sid 非空就写，于是 dsh 都没起来也能把旧号"确认"一遍，
    # 甚至把测试用的假号写进去，下一轮全部报 does not exist。
    if used_sid and used_sid != sid:
        _save_session_id(used_sid)
    return rec


# ----------------------------------------------------------------------
# 三个命令
# ----------------------------------------------------------------------


def cmd_dispatch(task_text: str, task_id: str = "") -> dict:
    """派活：只记一条，不执行。立刻返回。"""
    if not task_id:
        task_id = "t-" + datetime.now().strftime("%Y%m%d%H%M%S")

    rec = {
        "id": task_id,
        "task": task_text,
        "status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": None,
        "finished_at": None,
        "result": "",
        "error": "",
        "seconds": None,
    }
    tasks = _read_tasks()
    tasks.append(rec)
    _write_tasks(tasks)
    return rec


def cmd_run(task_id: str, timeout: int = 1800) -> dict:
    """真干活。"""
    tasks = _read_tasks()
    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if target is None:
        return {"id": task_id, "status": "error", "error": "找不到这个任务"}

    if target.get("status") in ("ok", "error", "timeout", "empty"):
        return target

    _update_task(task_id, status="running", started_at=_now())

    # .progress.json 由 _invoke_dsh 自己盖、自己收，这里不用再管
    rec = _invoke_dsh(_wrap_task(target["task"]), timeout, task_id=task_id)

    final = _update_task(
        task_id,
        status=rec["status"],
        result=rec.get("result", ""),
        error=rec.get("error", ""),
        seconds=rec.get("seconds"),
        session_id=rec.get("session_id", ""),
        finished_at=_now(),
    )

    try:
        with open(RESULTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(final or {}, ensure_ascii=False) + "\n")
    except OSError:
        pass

    # 写一条完成通知，等 QQ 端来接
    try:
        note = {
            "id": task_id,
            "status": (final or {}).get("status", rec.get("status")),
            "task": (final or {}).get("task", ""),
            "result": (final or {}).get("result", ""),
            "error": (final or {}).get("error", ""),
            "seconds": (final or {}).get("seconds"),
            "finished_at": (final or {}).get("finished_at", _now()),
            "delivered": False,
        }
        with open(OUTBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(note, ensure_ascii=False) + "\n")
    except OSError:
        pass

    return final or {"id": task_id, **rec}


def read_asks(limit: int = 5) -> list[dict]:
    """读电脑端推回来的"卡住了、需要什么"。

    电脑端拿不到东西时，会往 ask.jsonl 追加一行。
    这边读出来，让 QQ 端能告诉用户"它卡在哪儿了"。
    """
    if not os.path.exists(ASK_FILE):
        return []
    rows = []
    try:
        with open(ASK_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # 它可能写得不规范，那就保留原文，别丢
                    rows.append({"need": line[:200], "raw": True})
    except OSError:
        return []
    return rows[-limit:] if limit > 0 else rows


def cmd_cancel(task_id: str) -> dict:
    """停掉一个任务。

    三种情况分开说清楚，别让调用方猜：
        cancelled  真的停了
        not-running 它本来就不在跑（已经完成/失败）
        not-found  没这个任务

    停的时候要做两件事：
        1. 状态改成 cancelled（不然下回 live 还认为它在跑）
        2. 如果有活着的子进程，连根杀掉（taskkill /T，避免留孤儿）
    """
    tasks = _read_tasks()
    target = None
    for t in tasks:
        if t.get("id") == task_id:
            target = t
            break
    if target is None:
        return {"id": task_id, "status": "not-found"}

    cur = target.get("status")
    if cur not in ("pending", "running"):
        return {"id": task_id, "status": "not-running", "current": cur}

    # 杀掉正在跑的子进程（如果有）
    killed = False
    try:
        prog = _read_progress()
        if prog and prog.get("id") == task_id:
            pid = prog.get("pid")
            if pid:
                try:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True,
                        timeout=15,
                    )
                    killed = True
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass

    final = _update_task(
        task_id,
        status="cancelled",
        error="手动终止" + ("（进程已杀）" if killed else ""),
        finished_at=_now(),
    )
    return {"id": task_id, "status": "cancelled", "killed": killed, **(final or {})}


def cmd_status(task_id: str = "", limit: int = 5) -> dict | list:
    tasks = _read_tasks()
    if task_id:
        for t in tasks:
            if t.get("id") == task_id:
                return t
        return {"id": task_id, "status": "not-found", "error": "没有这个任务"}
    return tasks[-limit:]


def cmd_live() -> dict:
    """正在跑的活儿此刻到底在干什么。

    不再靠猜：任务说 running，就直接问心跳文件还热不热。
    超过 3 个心跳周期没更新，就标成 stale——进程大概已经死了，
    但 tasks.jsonl 里还挂着 running（比如被强杀）。
    """
    prog = _read_progress()
    now = time.time()
    if prog:
        hb = prog.get("heartbeat_at") or ""
        try:
            beat = datetime.fromisoformat(hb).timestamp()
            age = round(now - beat, 1)
        except (TypeError, ValueError):
            age = None
        prog["heartbeat_age_seconds"] = age
        prog["alive"] = None if age is None else age <= HEARTBEAT_SECONDS * 3

    running = [t for t in _read_tasks() if t.get("status") == "running"]
    return {
        "now": _now(),
        "running_tasks": running,
        "progress": prog,
        "note": "progress 为空 = 当前没有子进程在跑",
    }


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="派活 / 执行 / 查状态")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dis = sub.add_parser("dispatch", help="派一条任务，立刻返回")
    p_dis.add_argument("task")
    p_dis.add_argument("--id", default="")

    p_run = sub.add_parser("run", help="执行一条任务（后台用）")
    p_run.add_argument("--id", required=True)
    p_run.add_argument("--timeout", type=int, default=1800)

    p_can = sub.add_parser("cancel", help="停掉一个任务")
    p_can.add_argument("--id", required=True)

    p_ask = sub.add_parser("ask", help="看电脑端卡住时推回来的问题")
    p_ask.add_argument("--limit", type=int, default=5)

    p_st = sub.add_parser("status", help="查状态")
    p_st.add_argument("--id", default="")
    p_st.add_argument("--limit", type=int, default=5)

    sub.add_parser("live", help="正在跑的任务此刻在干什么")

    p_sess = sub.add_parser("session", help="看/重建会话")
    p_sess.add_argument("--reset", action="store_true", help="删掉已存的会话号")

    # git 防护：别让 AI 的草稿混进仓库
    p_guard = sub.add_parser("guard", help="检查/添加 git 防护")
    p_guard.add_argument("--path", default="", help="项目路径，留空扫常见目录")
    p_guard.add_argument("--apply", action="store_true", help="缺的话就补上")

    args = ap.parse_args()

    if args.cmd == "dispatch":
        print(json.dumps(cmd_dispatch(args.task, args.id), ensure_ascii=False))
        return 0
    if args.cmd == "run":
        rec = cmd_run(args.id, args.timeout)
        print(json.dumps(rec, ensure_ascii=False))
        return 0 if rec.get("status") in ("ok", "running") else 1
    if args.cmd == "cancel":
        print(json.dumps(cmd_cancel(args.id), ensure_ascii=False))
        return 0
    if args.cmd == "ask":
        print(json.dumps(read_asks(args.limit), ensure_ascii=False))
        return 0
    if args.cmd == "status":
        print(json.dumps(cmd_status(args.id, args.limit), ensure_ascii=False))
        return 0
    if args.cmd == "live":
        # ⚠️ 必须是单行 JSON。原来用 indent=2 输出成 31 行，
        #    而调用方（插件里的 _call_bridge）只取"最后一行以 { 开头"的，
        #    取到的是孤零零一个 "{"，解析失败 → 心跳永远是"查不到"。
        #    实测代价：一个跑了 26 分钟但活得好好的任务，
        #    QQ 端报告"没有心跳"，被当成卡死。
        print(json.dumps(cmd_live(), ensure_ascii=False))
        return 0
    if args.cmd == "guard":
        sys.path.insert(0, BRIDGE_DIR)
        try:
            import guard_git
            if args.path:
                rec = guard_git.apply_guard(args.path) if args.apply else guard_git.check(args.path)
                print(json.dumps(rec, ensure_ascii=False))
            else:
                print(json.dumps(guard_git.scan_common_roots(), ensure_ascii=False, indent=2))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"status": "error", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
        return 0
    if args.cmd == "session":
        if args.reset:
            try:
                os.remove(SESSION_FILE)
                print(json.dumps({"status": "reset", "note": "下次会新建会话"}, ensure_ascii=False))
            except OSError:
                print(json.dumps({"status": "no-session"}))
            return 0
        sid = _read_session_id()
        print(json.dumps({"session_id": sid or "(空，下次新建)"}, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
