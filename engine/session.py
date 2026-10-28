"""会话：把对话存下来，下次接着聊。

为什么必须有自己的会话
    DeepSeek Harness 那一套里，会话是它的核心。
    用户说"继续"，就是要接着上次的上下文往下走——
    没有会话，模型每次都是第一次听见这件事，得重新摸一遍。
    实测过这个代价：同一件事重开窗口，做出来的东西跟之前对不上。

存在哪
    workspaces/<任务名>/.session.json
    跟任务的工作目录放一起：一个任务 = 一个目录 = 一份记忆。
    删任务目录，记忆跟着没，不会有孤儿数据。

存什么
    完整的 messages 数组（user / assistant / tool），原样存。
    不存"摘要版"——需要压缩是运行时的事，不是存储的事。
"""

from __future__ import annotations

import json
import os
import time

SESSION_FILE = ".session.json"


def _path(workdir: str) -> str:
    return os.path.join(workdir, SESSION_FILE)


def load(workdir: str) -> dict:
    """读会话。没有就返回一个空的。"""
    p = _path(workdir)
    if not os.path.exists(p):
        return {"messages": [], "created_at": _now(), "updated_at": _now(),
                "total_prompt_tokens": 0, "total_completion_tokens": 0}

    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        # 文件坏了就当新会话。不能因为存档损坏就彻底跑不了。
        return {"messages": [], "created_at": _now(), "updated_at": _now(),
                "total_prompt_tokens": 0, "total_completion_tokens": 0}

    if not isinstance(data, dict):
        data = {}
    data.setdefault("messages", [])
    data.setdefault("created_at", _now())
    data.setdefault("updated_at", _now())
    data.setdefault("total_prompt_tokens", 0)
    data.setdefault("total_completion_tokens", 0)
    return data


def save(workdir: str, messages: list[dict], prompt_tokens: int = 0,
         completion_tokens: int = 0) -> None:
    """写回会话。先写临时文件再换，别写一半断电。"""
    os.makedirs(workdir, exist_ok=True)
    data = load(workdir)
    data["messages"] = messages
    data["updated_at"] = _now()
    data["total_prompt_tokens"] = int(data.get("total_prompt_tokens") or 0) + prompt_tokens
    data["total_completion_tokens"] = int(data.get("total_completion_tokens") or 0) + completion_tokens

    p = _path(workdir)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except OSError:
        # Windows 上目标文件被别的进程打开时会失败，重试几次
        for _ in range(10):
            time.sleep(0.05)
            try:
                os.replace(tmp, p)
                return
            except OSError:
                continue


def clear(workdir: str) -> None:
    """清空会话（用户要求换新窗口时用）。"""
    p = _path(workdir)
    try:
        os.remove(p)
    except OSError:
        pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def estimate_tokens(messages: list[dict]) -> int:
    """粗略估 token 数。

    不引入分词器——那要多装包。经验值：
        中文约 1 字 = 1 token
        英文约 4 字符 = 1 token
    估个大概够用了：压缩阈值不需要那么精确，留足余量就行。
    """
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += _text_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += _text_tokens(str(part.get("text") or ""))
        # 工具调用也算
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            total += _text_tokens(fn.get("name") or "")
            total += _text_tokens(fn.get("arguments") or "")
        total += 4         # 每条消息的固定开销
    return total


def _text_tokens(s: str) -> int:
    if not s:
        return 0
    cjk = sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff")
    other = len(s) - cjk
    return cjk + max(1, other // 4)


def compress(messages: list[dict], keep_last: int = 8,
             budget_tokens: int = 60_000) -> tuple[list[dict], bool]:
    """上下文太长时压缩。

    策略：把最早那一批消息拼成一段摘要式文本，塞成一条 system 消息，
    保留最近 keep_last 条原样。

    为什么不调模型去摘要：
        那要多花一轮 token、多等一次响应，而且摘要本身也可能丢关键信息。
        这里做的是"保留原文的前半部分要点"——把早期的 user 指令
        和 assistant 的关键结论留下来，中间的来回对答砍掉。
        够用，且不会引入新的不确定性。

    返回 (新messages, 是否压缩过)
    """
    if not messages:
        return messages, False

    if estimate_tokens(messages) <= budget_tokens:
        return messages, False

    # 最近的一些原样保留
    tail = messages[-keep_last:] if len(messages) > keep_last else messages[:]
    head = messages[: len(messages) - len(tail)] if len(messages) > keep_last else []

    if not head:
        return messages, False

    # 从被砍掉的部分里捞要点：
    #   · 用户说过的话（那是需求，最重要）
    #   · 助手最后说的话（那是结论）
    lines = []
    for m in head:
        role = m.get("role")
        content = m.get("content")
        text = content if isinstance(content, str) else ""
        text = text.strip().replace("\n", " ")
        if not text:
            continue
        if role == "user":
            lines.append("用户说过：" + text[:250])
        elif role == "assistant":
            lines.append("之前做过的结论：" + text[:250])

    # 太多就只留头尾几条
    if len(lines) > 30:
        lines = lines[:12] + ["…（中间省略了一些）"] + lines[-12:]

    digest = (
        "【之前聊过什么，以下是压缩后的摘要】\n"
        + "\n".join(lines)
        + "\n【摘要结束，以下是最近的对话】"
    )

    new = [{"role": "system", "content": digest}] + tail
    return new, True
