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
    from engine.profiles import config_path as _cp

    return _cp()


def _load_profile_config(pid: str) -> dict:
    """按 id 取一套配置。空 id 或找不到就用默认那套。"""
    from engine.profiles import default_profile, get_profile

    prof = get_profile(pid) if pid else {}
    if not prof:
        prof = default_profile()
    if not prof:
        return {"base_url": "", "api_key": "", "model": "",
                "max_steps": 40, "_empty": True}
    return {
        "base_url": prof.get("base_url") or "",
        "api_key": prof.get("api_key") or "",
        "model": prof.get("model") or "",
        "max_steps": int(prof.get("max_steps") or 40),
        "_id": prof.get("id") or "",
        "_name": prof.get("name") or "",
    }


def load_config() -> dict:
    """当前正在用的那套配置。

    返回的还是老形状（base_url / api_key / model / max_steps），
    这样调用方不用改。多套的管理在 engine.profiles 里。
    """
    from engine.profiles import active_profile

    prof = active_profile()
    if not prof:
        return {"base_url": "", "api_key": "", "model": "",
                "max_steps": 40, "_empty": True}
    return {
        "base_url": prof.get("base_url") or "",
        "api_key": prof.get("api_key") or "",
        "model": prof.get("model") or "",
        "max_steps": int(prof.get("max_steps") or 40),
        "_id": prof.get("id") or "",
        "_name": prof.get("name") or "",
    }


def save_config(cfg: dict) -> None:
    """改当前那套。

    兼容老调用（传一个完整 dict 进来）：如果当前没有配置，
    就新建一套；有的话就更新它。
    """
    from engine.profiles import (
        active_profile, add_profile, update_profile, set_active,
    )

    prof = active_profile()
    if not prof:
        add_profile({
            "name": cfg.get("_name") or "默认",
            "base_url": cfg.get("base_url") or "",
            "api_key": cfg.get("api_key") or "",
            "model": cfg.get("model") or "",
            "max_steps": cfg.get("max_steps") or 40,
        })
        return

    update_profile(prof["id"], {
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "api_key": cfg.get("api_key") or "",
        "max_steps": cfg.get("max_steps") or 40,
    })


def is_configured() -> bool:
    from engine.profiles import is_configured as _ic

    return _ic()


def dispatch(task: str, task_id: str, workdir: str,
             profile_id: str = "") -> dict:
    """跑一个任务，返回跟原来 _invoke_dsh 一模一样的形状。

    workdir   这个任务自己的工作目录。相对路径都以它为准。
    profile_id 用哪套配置（空表示默认那套）。
               多套配置同时启用时，每个任务跑在自己的模型上。
    """
    t0 = time.time()

    cfg = _load_profile_config(profile_id)
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
            # 这个窗口的权限级别从这里查。
            # 默认工作区——只能在任务目录里动，要碰外面得单独提权。
            task_name=task_id,
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
    # 判断失败不能只看一个前缀——万一那段话被别的东西包了一层就漏了。
    # 这里三条都算：
    #   1. final_text 拿到的是"模型调用失败"
    #   2. prog.doing 里写着"失败"
    #   3. 没有 assistant 说过任何话（等于整轮没产出）
    failed = (
        text.startswith("（模型调用失败")
        or "模型调用失败" in text
        or (prog.doing or "").startswith("失败")
        or not any(
            m.get("role") == "assistant" and (m.get("content") or "").strip()
            for m in (prog.messages or [])
        )
    )

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
