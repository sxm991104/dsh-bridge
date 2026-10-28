"""Agent 循环：让模型自己决定用什么工具、干几轮。

这是整个引擎的心脏。

一轮大概是这样：
    用户的话 → 模型 → 它说要 read_file("x.py")
              → 我们真去读 → 把内容还给它
              → 它再说要 edit_file(...)
              → 我们真去改 → 把结果还给它
              → 它说"改完了" → 结束

外面的世界看起来只是"派了个任务，等了一会儿，拿到结果"。

几条必须守住的规矩
    1. 每轮最多跑 max_steps 轮，防止模型陷进死循环。
    2. 工具调用必须有对应的 tool 消息回填，否则下一次请求会被
       接口拒掉（400: tool messages following tool_calls）。
    3. 截断的 tool_calls 参数不能让整个任务崩——当成工具失败告诉它。
    4. 上下文长了先压缩，别让请求超限。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import session as sess
from . import tools
from .llm import LLM, LLMError

SYSTEM_PROMPT = """你是一个在用户电脑上干活的助手。

你有这些工具：读写文件、改文件、看目录、搜内容、跑命令。
自己做判断，该用就用，不用先问。做完把结果说清楚。

几条规矩：
- 改文件之前先读，看清楚再动。
- 只改该改的地方。不要顺手重构别的东西。
- 拿不到的信息就说拿不到，别猜。
- 命令跑失败了，看错误信息自己想办法，别反复试同一个。
- 最后回一句话说明结果：做成了什么，或者卡在哪。"""


@dataclass
class Progress:
    """跑的过程中往外报的东西。界面靠它显示"它在干什么"。"""
    steps: int = 0
    doing: str = ""
    messages: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    compressed: bool = False


def run(task: str, workdir: str, llm: LLM, max_steps: int = 40,
        on_progress=None, should_stop=None) -> Progress:
    """跑一个任务，返回过程记录。

    on_progress(Progress) 每轮调一次，用来报"现在在做什么"。
    should_stop() 返回 True 就中止（用户点了停）。
    """
    prog = Progress()

    # 0) 先把工作目录定下来。
    #    模型给的相对路径都相对这里解析，不是相对"启动桥的那个目录"。
    tools.set_workdir(workdir)

    # 1) 接上历史
    stored = sess.load(workdir)
    messages: list[dict] = list(stored.get("messages") or [])

    # 2) 系统提示。每次都在最前面，且只有一个。
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

    messages.append({"role": "user", "content": task})

    # 3) 开跑
    for step in range(max_steps):
        if should_stop and should_stop():
            prog.doing = "被中止"
            break

        prog.steps = step + 1

        # 上下文太长就先压一下
        messages, did = sess.compress(messages)
        if did:
            prog.compressed = True

        prog.doing = "思考中"
        if on_progress:
            on_progress(prog)

        try:
            reply = llm.chat(messages, tools=tools.TOOL_SPECS)
        except LLMError as e:
            # 模型调不通是致命错误，整轮结束
            messages.append({
                "role": "assistant",
                "content": f"（模型调用失败：{e}）",
            })
            prog.doing = f"失败：{e}"
            _save(workdir, messages, prog)
            return prog

        prog.prompt_tokens += reply.prompt_tokens
        prog.completion_tokens += reply.completion_tokens

        # 没有工具调用 → 它说完了，收工
        if not reply.tool_calls:
            messages.append({"role": "assistant", "content": reply.content or ""})
            prog.doing = "完成"
            break

        # 有工具调用：先把这条 assistant 消息记下来
        # （assistant 的 tool_calls 和后面 tool 消息必须成对，少一个都会被接口拒）
        messages.append({
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in reply.tool_calls
            ],
        })

        # 逐个执行，结果回填
        for tc in reply.tool_calls:
            prog.doing = _describe(tc.name, tc.arguments)
            if on_progress:
                on_progress(prog)

            result = tools.execute(tc.name, tc.arguments)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    else:
        # for 走完了都没 break：说明撞到 max_steps
        prog.doing = f"跑了 {max_steps} 轮还没结束，停下来了"

    _save(workdir, messages, prog)

    # 把这一轮的对话留给调用方（结果就是最后一条 assistant 的话）
    prog.messages = messages
    return prog


def _save(workdir: str, messages: list[dict], prog: Progress) -> None:
    """存盘。存失败不该让任务失败——最多下次接不上历史。"""
    try:
        sess.save(workdir, messages, prog.prompt_tokens, prog.completion_tokens)
    except Exception:  # noqa: BLE001
        pass


def _describe(name: str, args: str) -> str:
    """把工具调用翻成一句人话，给界面显示。"""
    import json

    try:
        a = json.loads(args or "{}")
    except json.JSONDecodeError:
        a = {}

    if name == "run_command":
        return f"跑命令：{str(a.get('command') or '')[:60]}"
    if name == "read_file":
        return f"读文件：{a.get('path') or ''}"
    if name == "write_file":
        return f"写文件：{a.get('path') or ''}"
    if name == "edit_file":
        return f"改文件：{a.get('path') or ''}"
    if name == "list_dir":
        return f"看目录：{a.get('path') or ''}"
    if name == "search_files":
        return f"搜：{a.get('pattern') or ''}"
    return f"用工具：{name}"


def final_text(prog: Progress) -> str:
    """从过程里捞出最终要给人看的那段话。

    模型有时候最后一轮还在调工具，没有留下 assistant 文字。
    这种情况就把最后一条工具结果或者状态描述当结果。
    """
    for m in reversed(prog.messages):
        if m.get("role") != "assistant":
            continue
        if m.get("tool_calls"):
            continue
        text = (m.get("content") or "").strip()
        if text:
            return text
    return prog.doing or "（没有输出）"
