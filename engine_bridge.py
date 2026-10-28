"""把自研引擎接到桥上。

桥原来调 DeepSeek Harness，返回一个固定形状的 dict。
现在换成自己的引擎，但**返回值形状必须一模一样**——
cmd_run、通知、状态文件全都吃这个形状，改一点就全乱。

所以这一层只做一件事：转换。

    engine.run(...)  →  {"status","result","error","seconds","session_id"}

引擎不依赖 DSH，也不依赖 Node。用户只要有一个
OpenAI 兼容的 base_url + key + model 就能跑。

配置从哪来
    %APPDATA%\\dsh-bridge\\engine.json

    跟 desktop 那边的 paths.py 分开存：
    那个管"程序装在哪"，这个管"用哪个模型"，是两回事。
"""

from __future__ import annotations

import json
import os
import sys
import time

BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BRIDGE_DIR)

from engine import LLM, final_text, run as engine_run  # noqa: E402
from engine import session as eng_session  # noqa: E402

APP_NAME = "dsh-bridge"


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "engine.json")


DEFAULTS = {
    # 留空表示还没配，界面上要提示用户去填
    "base_url": "",
    "api_key": "",
    "model": "",
    "max_steps": 40,
}


def load_config() -> dict:
    data = dict(DEFAULTS)
    p = config_path()
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                disk = json.load(f)
            if isinstance(disk, dict):
                for k in DEFAULTS:
                    if k in disk:
                        data[k] = disk[k]
        except (OSError, json.JSONDecodeError):
            pass
    return data


def save_config(cfg: dict) -> None:
    p = config_path()
    tmp = p + ".tmp"
    clean = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def is_configured() -> bool:
    c = load_config()
    return bool(c.get("base_url") and c.get("api_key") and c.get("model"))


# ----------------------------------------------------------------------
# 派活
# ----------------------------------------------------------------------

def dispatch(task: str, task_id: str, workdir: str) -> dict:
    """跑一个任务，返回跟原来 _invoke_dsh 一模一样的形状。

    workdir 是这个任务自己的工作目录。相对路径都以它为准。
    """
    t0 = time.time()

    cfg = load_config()
    if not is_configured():
        return {
            "status": "error",
            "result": "",
            "error": "还没配置模型。去桌面端的「设置 → 任务端模型」填一下地址和 Key。",
            "seconds": 0.0,
            "session_id": "",
        }

    os.makedirs(workdir, exist_ok=True)

    try:
        llm = LLM(
            base_url=cfg["base_url"],
            api_key=cfg["api_key"],
            model=cfg["model"],
        )
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error", "result": "",
            "error": f"模型配置有问题：{e}",
            "seconds": round(time.time() - t0, 1), "session_id": "",
        }

    import bridge as _br  # 延迟导入，避免循环

    last_beat = [0.0]

    def on_progress(prog):
        """把引擎的进度写成桥的心跳文件。

        桥的 live 命令、桌面的"正在做什么"，全靠这个文件。
        格式必须跟原来 DSH 那条路一模一样，否则 live 读不出来。

        限流到 1 秒一次：太频繁会跟任务线程抢同一个文件。
        """
        now = time.time()
        if now - last_beat[0] < 1.0:
            return
        last_beat[0] = now
        try:
            _br._write_progress({
                "id": task_id,
                "status": "running",
                "phase": "running",
                "pid": os.getpid(),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "elapsed_seconds": round(now - t0, 1),
                "heartbeat_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "session_id": workdir,
                "task": task[:200],
                "doing": prog.doing,
                "step": prog.steps,
            })
        except Exception:  # noqa: BLE001
            pass

    try:
        prog = engine_run(
            task=task,
            workdir=workdir,
            llm=llm,
            max_steps=int(cfg.get("max_steps") or 40),
            on_progress=on_progress,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "status": "error",
            "result": "",
            "error": f"{type(e).__name__}: {e}",
            "seconds": round(time.time() - t0, 1),
            "session_id": workdir,
        }

    text = final_text(prog)
    failed = text.startswith("（模型调用失败")

    return {
        "status": "error" if failed else "ok",
        "result": "" if failed else text,
        "error": text if failed else "",
        "seconds": round(time.time() - t0, 1),
        # 引擎的"会话"就是工作目录本身——记忆存在那里面
        "session_id": workdir,
        # 额外信息，界面有用（虽然桥的核心逻辑不读）
        "steps": prog.steps,
        "prompt_tokens": prog.prompt_tokens,
        "completion_tokens": prog.completion_tokens,
        "compressed": prog.compressed,
    }


def reset_session(workdir: str) -> None:
    """清掉这个任务的记忆（用户要求换新窗口时）。"""
    eng_session.clear(workdir)


def context_info(workdir: str) -> dict:
    """这个任务的上下文现在多大。界面拿去显示花费和占用。"""
    data = eng_session.load(workdir)
    msgs = data.get("messages") or []
    toks = eng_session.estimate_tokens(msgs)
    return {
        "messages": len(msgs),
        "context_tokens": toks,
        "total_prompt_tokens": data.get("total_prompt_tokens") or 0,
        "total_completion_tokens": data.get("total_completion_tokens") or 0,
        "updated_at": data.get("updated_at") or "",
    }
