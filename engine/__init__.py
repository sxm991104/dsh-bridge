"""dsh-bridge 自带的任务引擎。

为什么自己写
    原来任务端依赖 DeepSeek Harness：用户想用，得先装 DSH、
    配 profile、配 provider。对很多人这一步就劝退了。

    这个引擎只做一件事：给一个 base_url + api_key + model，
    就能在电脑上干活。任何 OpenAI 兼容的接口都行。

    它不带界面——那是 desktop 那边的事。

结构
    llm.py      调模型，兼容所有 OpenAI 格式
    tools.py    六个工具：读/写/改/列/搜/跑命令
    session.py  会话存盘 + 上下文压缩
    agent.py    工具循环（心脏）

用法
    from engine import LLM, run, final_text

    llm = LLM(base_url, api_key, model)
    prog = run("把登录页按钮改大", workdir=r"D:\\proj", llm=llm)
    print(final_text(prog))
"""

from .agent import Progress, final_text, run
from .llm import LLM, LLMError, Reply, ToolCall, test_connection
from .session import clear as clear_session
from .session import estimate_tokens

__all__ = [
    "LLM",
    "LLMError",
    "Reply",
    "ToolCall",
    "test_connection",
    "run",
    "final_text",
    "Progress",
    "clear_session",
    "estimate_tokens",
]
