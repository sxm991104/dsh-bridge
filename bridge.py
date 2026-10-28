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

# 怎么调 dsh —— 这里有个必须绕开的坑。
#
# ⚠️ 不要直接用 dsh.cmd 传任务文本。
#
#    dsh.cmd 最后一行是：
#        "%_prog%" "...\bin.js" %*
#    %* 会把参数交给 cmd 展开，**多行文本里的换行会被当成命令分隔符截掉**。
#
#    实测症状：任务是
#        第一行：这是一句带冒号的话。
#        第二行，带逗号。
#        第三行带"引号"。
#    电脑端只收到第一行，后面全丢。而且不报错——它会一本正经地
#    回答"就这一句，然后呢？"，看起来像是它在偷懒，其实是文本没了。
#    今天因为这个撞了三次，前两次都被误判成"标点导致截断"。
#
#    改成直接 node bin.js，参数作为独立 argv 传过去，多行完好。
#
# 查找顺序：
#     1. 环境变量 DSH_CMD（自定义）
#     2. node + bin.js（推荐，绕开 cmd 的参数展开）
#     3. dsh.cmd / dsh（兜底，但会截断多行任务）
def _resolve_dsh() -> list[str]:
    """返回一条可执行的命令前缀（list），后面再拼参数。"""
    from shutil import which

    env_cmd = os.environ.get("DSH_CMD", "").strip()
    if env_cmd:
        return [env_cmd]

    # 优先：node + bin.js
    appdata = os.environ.get("APPDATA", "")
    node = which("node")
    if node and appdata:
        binjs = os.path.join(
            appdata, "npm", "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js"
        )
        if os.path.exists(binjs):
            return [node, binjs]

    # 兜底：.cmd（注意会截断多行任务）
    for name in ("dsh.cmd", "dsh"):
        p = which(name)
        if p:
            return [p]

    return []


DSH_CMD = _resolve_dsh()


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


def find_dsh() -> list[str]:
    """返回命令前缀。空列表表示找不到。"""
    return list(DSH_CMD)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sessions_dir() -> str:
    """每个任务一个会话记录文件，放这个目录。"""
    d = os.path.join(BRIDGE_DIR, ".sessions")
    os.makedirs(d, exist_ok=True)
    return d


def _task_session_path(task_id: str) -> str:
    return os.path.join(_sessions_dir(), f"{task_id}.session")


def _task_workdir(task_id: str) -> str:
    """每个任务一个工作目录。

    为什么要独立目录：
        之前所有任务都跑在 BRIDGE_DIR 里，同时开三个任务改同一个文件
        会互相踩。目录分开之后，"多开"才是真的互不干扰。

    目录位置：<桥目录>/workspaces/<task_id>/
    """
    d = os.path.join(BRIDGE_DIR, "workspaces", task_id)
    os.makedirs(d, exist_ok=True)
    return d


def read_task_session(task_id: str) -> str:
    """读某个任务自己的会话号。没有返回空（表示下次新建）。"""
    try:
        with open(_task_session_path(task_id), encoding="utf-8-sig") as f:
            return f.read().strip().lstrip("\ufeff")
    except OSError:
        return ""


def save_task_session(task_id: str, sid: str) -> None:
    """把会话号记到任务自己身上。"""
    sid = (sid or "").strip().lstrip("\ufeff")
    if not sid:
        return
    try:
        with open(_task_session_path(task_id), "w", encoding="utf-8", newline="") as f:
            f.write(sid)
    except OSError:
        pass


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


# ── 任务文件的进程间锁 ──
#
# ⚠️ 这是压测暴露出来的真问题，不是理论担心：
#     10 个任务同时跑，5 个卡在 pending 永远不动。
#     原因：_write_tasks 用「同一个 tmp 文件名 + os.replace」，
#     多个进程同时写会互相覆盖——A 写了状态，B 随后把它盖回旧值，
#     于是任务看起来一直是 pending，其实进程早就跑完退了。
#
#     之前的"多开"只测到三个，没撞上；十个一撞就现原形。
#
# 用 msvcrt（Windows 自带）对锁文件加独占锁。拿不到就等，等不到就放弃那次写入——
# 宁可这一条更新失败，也不能把整个文件写坏。
if os.name == "nt":
    import msvcrt


class _TaskLock:
    """给 tasks.jsonl 的读改写加独占锁。"""

    def __init__(self, path: str):
        self.path = path + ".lock"
        self.fh = None

    def __enter__(self):
        try:
            self.fh = open(self.path, "a+b")
            # 等最多 10 秒；每个写操作都很快，正常不会等这么久
            for _ in range(200):
                try:
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except OSError:
                    time.sleep(0.05)
        except OSError:
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


def _write_tasks(tasks: list[dict]) -> None:
    """原子写 tasks.jsonl。带重试，因为 Windows 会拒绝覆盖正被占用的文件。

    ⚠️ 这里是压测压出来的坑，值得写清楚：

        压测时 10 个任务同时跑，3-5 个进程直接崩了，报
            PermissionError: [WinError 5] 拒绝访问
            os.replace('tasks.jsonl.tmp.xxx', 'tasks.jsonl')

        Windows 和 POSIX 不一样：**目标文件正被另一个进程打开时，
        os.replace 会失败**，而 POSIX 允许原子替换。
        10 个进程同时读改写同一个 jsonl，撞上就是崩。

        之前的文件锁没解决问题，因为我只在 _update_task 里加了锁，
        而 _read_tasks 会被多处调用，句柄开合时机对不齐——
        锁住的只是自己那段代码，挡不住别人已经打开的文件。

        真正管用的做法：**失败就重试**。别人写完就关，等一下就能覆盖。
        最多重试 50 次（约 5 秒），还不行就放弃这次写入——
        宁可丢一条状态更新，也不能让整个进程崩掉。
    """
    tmp = f"{TASKS}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            for t in tasks:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")
    except OSError:
        return

    last_err = None
    for attempt in range(50):
        try:
            os.replace(tmp, TASKS)
            return
        except PermissionError as e:
            # 别人正打开着，等一下再来
            last_err = e
            time.sleep(0.1 * min(attempt + 1, 5))
        except OSError as e:
            last_err = e
            break

    # 实在写不进去：清掉自己的 tmp，不让它堆积
    try:
        os.remove(tmp)
    except OSError:
        pass
    _log_quiet(f"写 tasks.jsonl 失败（重试 50 次）: {last_err}")


def _log_quiet(msg: str) -> None:
    """记一条日志到 bridge.log，失败也不抛。"""
    try:
        with open(os.path.join(BRIDGE_DIR, "bridge.log"), "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except OSError:
        pass


def _update_task(task_id: str, **fields) -> dict | None:
    # 整段「读 → 改 → 写」都要在锁里，否则并发时后写的会盖掉先写的
    with _TaskLock(TASKS):
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


def _state_dir() -> str:
    """每个任务一个状态文件，供前台快速查询。

    ⚠️ 为什么要有这个，而不是让前台去翻 tasks.jsonl / DSH 会话文件：
        · tasks.jsonl 有几百条，翻它越来越慢
        · DSH 会话文件几十 MB，解压一次要好几秒
        · 前台是"用户一问就要立刻答"的角色，不能等
    所以让任务自己维护一份小而全的状态，前台只读一个文件，毫秒级。

    这也是"100 个任务同时跑"能撑住的前提：
    各写各的，互不干扰；前台只读被问的那一个。
    """
    d = os.path.join(BRIDGE_DIR, "state")
    os.makedirs(d, exist_ok=True)
    return d


def _state_path(task_id: str) -> str:
    # 任务名可能带斜杠之类的字符，换成安全的
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in task_id)
    return os.path.join(_state_dir(), f"{safe}.json")


# DeepSeek 官方价（元 / 百万 token）。
# 缓存命中便宜十倍，但界面上给用户看的是"最坏情况"，所以按未命中算。
_PRICE_FRESH = 4.0 / 1_000_000
_PRICE_CACHED = 0.5 / 1_000_000


def estimate_session_cost(session_id: str) -> dict:
    """按会话大小估一次对话要花多少钱。

    ⚠️ 这是"估算"，不是账单。
        精确账单在 .dshw-usage.json 里，但那是全局记账、
        没有任务 ID，关联不上具体任务。
        所以这里用会话体积反推——够回答"这个窗口还划不划算"。

    为什么在跑完时算、而不是派活前算：
        派活前前台要立刻答（0.2 秒），不能卡在解压几十 MB 的文件上。
        跑完时算，前台问的时候直接读 state，就是毫秒级。

    返回：
        chars      会话字符数
        tokens     估算 token 数（中文约 1.6 字符/token）
        cost_cny   每轮对话估算花费（按未命中缓存算，最坏情况）
    """
    if not session_id:
        return {}
    try:
        import glob
        dsh_home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
        pat = os.path.join(dsh_home, "sessions", "**", session_id, "*.zstd")
        found = glob.glob(pat, recursive=True)
        if not found:
            return {}
        raw = open(found[0], "rb").read()
        try:
            import zstandard as zstd
            reader = zstd.ZstdDecompressor().stream_reader(raw)
            chunks = []
            while True:
                c = reader.read(4 * 1024 * 1024)
                if not c:
                    break
                chunks.append(c)
            text = b"".join(chunks).decode("utf-8", "replace")
        except ImportError:
            # 没有 zstandard 就跳过，别因为算钱把主流程搞挂
            return {}
    except Exception:  # noqa: BLE001
        return {}

    chars = len(text)
    tokens = int(chars / 1.6)
    return {
        "chars": chars,
        "tokens": tokens,
        "cost_cny": round(tokens * _PRICE_FRESH, 4),
    }


def write_state(task_id: str, **fields) -> None:
    """更新任务状态文件（原子写，读的一方不会读到半截）。"""
    path = _state_path(task_id)
    cur = {}
    try:
        with open(path, encoding="utf-8") as f:
            cur = json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    cur.update(fields)
    cur["id"] = task_id
    cur["updated_at"] = _now()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def read_state(task_id: str) -> dict:
    """读任务状态。读不到返回空 dict。"""
    try:
        with open(_state_path(task_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def list_states() -> list[dict]:
    """列出所有任务状态（前台列任务用）。"""
    out = []
    try:
        for fn in os.listdir(_state_dir()):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(_state_dir(), fn), encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
    return out


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
    dsh_cmd = find_dsh()
    if not dsh_cmd:
        return {"status": "error", "error": "找不到 dsh 命令（也找不到 node + bin.js）"}

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
        # 命令前缀 + 参数。dsh_cmd 通常是 [node, bin.js]，
        # 这样任务文本作为独立 argv 传进去，多行不会被截断。
        args = [*dsh_cmd, "--profile", "qqme", "--json"]
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
                # 每个任务自己的工作目录：多开时不互相踩文件
                cwd=(_task_workdir(task_id) if task_id else BRIDGE_DIR),
                shell=False,
                env=child_env,
            )
        except Exception as e:  # noqa: BLE001
            _r = {
                "status": "error",
                "result": "",
                "error": f"{type(e).__name__}: {e}",
                "seconds": 0,
                "session_id": sid,
            }
            notify_outbox(task_id, _r, task)
            return (_r, sid)

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
            _r = {
                "status": "timeout",
                "result": "",
                "error": f"超过 {timeout} 秒没跑完",
                "seconds": elapsed,
                "session_id": sid,
            }
            notify_outbox(task_id, _r, task)
            return (_r, sid)

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
            _r = {
                "status": "error",
                "result": "",
                "error": error_msg,
                "seconds": elapsed,
                "session_id": real_sid or sid,
            }
            notify_outbox(task_id, _r, task)
            return (_r, real_sid or sid)

        # 事件流拼不出答案时，退回清洗过的 stdout
        if not final_text:
            final_text = _clean(out)
        if not final_text:
            final_text = _clean(errtxt)

        # 走到这里还"有答案"，得先确认它到底是答案还是进程崩溃的堆栈。
        # 没有真 final 事件 + 输出像崩溃 + 返回码非 0 → 这是失败，不是结果。
        # 崩溃检测之前，先看是不是"机器扛不住了"。
        #
        # 压测暴露的场景：同时开一百个任务，内存爬到 91%，
        # 进程被系统干掉或启动失败，但原来的代码只报个空结果，
        # 用户完全不知道为什么失败。
        # 这里识别出内存不足，给一句人话，让他知道自己该少开几个。
        _low_mem = any(k in (out + "\n" + errtxt).lower() for k in (
            "out of memory", "memoryerror", "cannot allocate",
            "not enough memory", "insufficient memory", "enomem",
        ))
        if _low_mem:
            _r = {
                "status": "error",
                "result": "",
                "error": "机器内存不够，这个任务没跑成。同时开的任务太多了，"
                         "少开几个再试，或者关掉些别的程序。",
                "seconds": elapsed,
                "session_id": real_sid or sid,
            }
            notify_outbox(task_id, _r, task)
            return (_r, real_sid or sid)

        if final_text and not had_final:
            crash = _looks_like_crash(out + "\n" + errtxt)
            if crash and proc.returncode != 0:
                _r = {
                    "status": "error",
                    "result": "",
                    "error": f"dsh 没跑起来（{crash}，返回码 {proc.returncode}）："
                    + final_text[:400],
                    "seconds": elapsed,
                    "session_id": real_sid or sid,
                }
                notify_outbox(task_id, _r, task)
                return (_r, real_sid or sid)

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

    # 会话号按任务取：每个任务一个窗口，各记各的上下文。
    # 没有任务号时（极少）退回全局会话，保持旧行为。
    sid = read_task_session(task_id) if task_id else _read_session_id()
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
        if task_id:
            save_task_session(task_id, used_sid)
        else:
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
    # ⚠️ 这里必须是 "同 id 就覆盖"，不是 append。
    #
    #    原来是 tasks.append(rec)，同一个 id 派第二次就多一条记录。
    #    但 cmd_run / _update_task 都是 "找到第一条就 break"，
    #    于是第二次派活时：run 拿到的是最老那条（状态已 ok）→
    #    直接 return，新任务根本没执行；update 也永远改的是老那条。
    #
    #    实测症状：同一个任务名连派三次，后两次结果空、状态却显示 ok。
    #
    #    改成覆盖后，"一个任务 = 一条记录 = 一个窗口"，
    #    这才对得上"一任务一会话"的设计。
    with _TaskLock(TASKS):
        tasks = _read_tasks()
        for i, t in enumerate(tasks):
            if t.get("id") == task_id:
                rec["created_at"] = t.get("created_at") or rec["created_at"]
                tasks[i] = rec
                break
        else:
            tasks.append(rec)
        _write_tasks(tasks)
    # 同步一份轻量状态，前台查得快
    write_state(
        task_id,
        status="pending",
        task=(task_text or "")[:200],
        created_at=rec["created_at"],
        turns=0,
        compactions=0,
        result="",
        error="",
    )
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
        rec_nf = {"id": task_id, "status": "error", "error": "找不到这个任务"}
        notify_outbox(task_id, rec_nf)
        return rec_nf

    if target.get("status") in ("ok", "error", "timeout", "empty"):
        return target

    _update_task(task_id, status="running", started_at=_now())
    write_state(task_id, status="running", started_at=_now())

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

    # 跑完记下最终状态（前台查进度、判断"值不值得接着用"都靠它）
    _st = final or {}
    # 顺手算一下这个窗口现在多贵——前台被问"还划算吗"时直接读
    _sid = _st.get("session_id") or ""
    _cost = estimate_session_cost(_sid) if _sid else {}
    write_state(
        task_id,
        status=_st.get("status") or rec.get("status"),
        result=(_st.get("result") or "")[:1000],
        error=(_st.get("error") or "")[:500],
        seconds=_st.get("seconds"),
        session_id=_sid,
        finished_at=_st.get("finished_at") or _now(),
        # 每轮对话大概多少 token / 多少钱（估算，按未命中缓存算最坏情况）
        context_tokens=_cost.get("tokens"),
        cost_cny=_cost.get("cost_cny"),
    )

    # 写完成通知（成功失败都写）
    notify_outbox(task_id, final or rec, rec.get("task", ""))

    return final or {"id": task_id, **rec}


def notify_outbox(task_id: str, rec: dict, task_text: str = "") -> None:
    """写一条完成通知进 outbox，成功失败都写。

    ⚠️ 原来只在 cmd_run 的最后写一次。
       而 _invoke_dsh 在超时、报错、找不到 dsh 时会提前 return，
       那些路径根本走不到推送点——**失败的活从来没有回过话**。
       用户派了活，跑挂了，什么都不说，只能自己去查。

       这个函数抽出来，让所有结束路径都能调。
    """
    st = (rec or {}).get("status") or ""
    try:
        note = {
            "id": task_id,
            "name": task_id,
            "status": st,
            "ok": st == "ok",
            "task": (task_text or (rec or {}).get("task") or "")[:200],
            "result": (rec or {}).get("result", "") or "",
            "error": (rec or {}).get("error", "") or "",
            "seconds": (rec or {}).get("seconds"),
            "finished_at": (rec or {}).get("finished_at") or _now(),
            "delivered": False,
        }
        with open(OUTBOX, "a", encoding="utf-8") as f:
            f.write(json.dumps(note, ensure_ascii=False) + "\n")
    except OSError:
        pass


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


def cmd_stats(task_id: str) -> dict:
    """查一个任务的轻量状态。前台用它回答「红队到哪一步了」。

    只读 state/<名字>.json，不碰 tasks.jsonl、不碰 DSH 会话文件——
    前台是"一问就要答"的角色，慢一点用户就能感觉到。
    """
    st = read_state(task_id)
    if not st:
        return {"status": "not-found", "id": task_id}
    return st


def cmd_list(limit: int = 50) -> list[dict]:
    """列出所有任务状态。前台用它回答「我派了哪些活」。

    只返回最要紧的几个字段，不带结果正文——那会让输出变得巨大。
    """
    out = []
    for st in list_states()[:limit]:
        out.append({
            "id": st.get("id"),
            "status": st.get("status"),
            "task": (st.get("task") or "")[:60],
            "seconds": st.get("seconds"),
            "updated_at": st.get("updated_at"),
        })
    return out


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
    p_st = sub.add_parser("stats", help="查一个任务的轻量状态（前台用）")
    p_st.add_argument("--id", required=True)
    p_ls = sub.add_parser("list", help="列出所有任务（前台用）")
    p_ls.add_argument("--limit", type=int, default=50)
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
    if args.cmd == "stats":
        print(json.dumps(cmd_stats(args.id), ensure_ascii=False))
        return 0
    if args.cmd == "list":
        print(json.dumps(cmd_list(args.limit), ensure_ascii=False))
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
