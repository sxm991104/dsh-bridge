"""调整电脑端那个我用的模型和推理强度。

为什么需要这个
    电脑端跑的每一个任务，用的模型和推理强度是固定的：
        model: deepseek-v4.1-flash
        reasoningEffort: max

    max 意味着它每次都往死里想。简单的活（建个文件）也这样，
    又慢又费 token。而复杂的活（改代码、查问题）又需要它想清楚。

    所以应该能按活的大小调：小事用 medium，大事才用 max。

怎么调的
    改 $DSH_HOME/profiles/qqme/cordis.patch.yml（或用 DSH_PROFILE_FILE 指定）里的两行：
        model:            deepseek-v4.1-flash  /  deepseek-v4-pro
        reasoningEffort:  low / medium / high / max

    每次 `dsh qqme` 都是新进程，会重新读这个文件，
    所以改完**不用重启任何服务**，下一个任务立刻生效。

⚠️ 只改这两行，别碰别的。
    这个文件里还有人格提示词（personaPrefix）那一大段，
    改坏了电脑端那个我就变味了。所以这里用精确替换，
    不是整份重写 YAML（那会把注释和格式全冲掉）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime

def _profile_path() -> str:
    """定位 qqme 的 profile 配置文件。

    优先用环境变量 DSH_PROFILE_FILE 指定；否则按 DSH_HOME 拼默认位置。
    开源版不能把某台机器的用户名路径写死。
    """
    env = os.environ.get("DSH_PROFILE_FILE", "").strip()
    if env:
        return env
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return os.path.join(home, "profiles", "qqme", "cordis.patch.yml")


PROFILE = _profile_path()

# 可选的模型。加新模型时先确认 llm-codebuddy 的 models 列表里有它，
# 不然 dsh 会报 model unavailable。
MODELS = {
    "deepseek-v4.1-flash": "快，日常够用（默认）",
    "deepseek-v4-pro": "强，慢一些，适合复杂的活",
}

# ⚠️ 这里没有 medium —— 实测 deepseek-v4.1-flash 不支持它。
#    设成 medium 会报 UNSUPPORTED_REASONING_EFFORT，整个任务白跑。
#    low / high / max 三个都实测可用。
EFFORTS = {
    "low": "几乎不想，最快，适合搬砖活",
    "high": "认真想",
    "max": "往死里想，最慢也最费（默认）",
}


def _read(path: str = PROFILE) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def get_current(path: str = PROFILE) -> dict:
    """读出当前的 model 和 reasoningEffort。"""
    try:
        txt = _read(path)
    except OSError as e:
        return {"status": "error", "error": f"读不到配置: {e}"}

    model = ""
    effort = ""
    # 只认 agent-default-model 那一段里的，别抓到 models 列表里的 id
    in_block = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("- id: agent-default-model"):
            in_block = True
            continue
        if in_block:
            if s.startswith("- id:") and "agent-default-model" not in s:
                break
            m = re.match(r"^model:\s*(\S+)", s)
            if m:
                model = m.group(1)
            m = re.match(r"^reasoningEffort:\s*(\S+)", s)
            if m:
                effort = m.group(1)

    return {
        "status": "ok",
        "model": model or "(未设置)",
        "reasoning_effort": effort or "(未设置)",
        "path": path,
    }


def apply(
    model: str = "",
    effort: str = "",
    path: str = PROFILE,
) -> dict:
    """改配置。只改指定的一两项，其余不动。"""
    if model and model not in MODELS:
        return {
            "status": "error",
            "error": f"不支持的模型：{model}。可选：{', '.join(MODELS)}",
        }
    if effort and effort.lower() not in EFFORTS:
        return {
            "status": "error",
            "error": f"不支持的推理强度：{effort}。可选：{', '.join(EFFORTS)}",
        }

    try:
        txt = _read(path)
    except OSError as e:
        return {"status": "error", "error": f"读不到配置: {e}"}

    # 先备份。这个文件里有人格提示词，改坏了很麻烦。
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{stamp}")
    except OSError as e:
        return {"status": "error", "error": f"备份失败，不敢改: {e}"}

    lines = txt.splitlines()
    out = []
    in_block = False
    changed = {"model": False, "reasoningEffort": False}

    for line in lines:
        s = line.strip()

        if s.startswith("- id: agent-default-model"):
            in_block = True
            out.append(line)
            continue
        if in_block and s.startswith("- id:") and "agent-default-model" not in s:
            in_block = False

        if in_block and model and re.match(r"^\s*model:\s*\S+", line):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}model: {model}")
            changed["model"] = True
            continue
        if in_block and effort and re.match(r"^\s*reasoningEffort:\s*\S+", line):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}reasoningEffort: {effort.lower()}")
            changed["reasoningEffort"] = True
            continue

        out.append(line)

    if not any(changed.values()):
        return {"status": "unchanged", "note": "没找到要改的行，或者值本来就是这样"}

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
    except OSError as e:
        return {"status": "error", "error": f"写入失败: {e}"}

    now = get_current(path)
    return {
        "status": "ok",
        "changed": [k for k, v in changed.items() if v],
        **now,
    }


def options() -> dict:
    """列出可选值，供调用方展示。"""
    return {
        "current": get_current(),
        "models": MODELS,
        "efforts": EFFORTS,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="调电脑端的模型和推理强度")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="看当前设置")
    sub.add_parser("options", help="列出可选值")

    p_set = sub.add_parser("set", help="改设置")
    p_set.add_argument("--model", default="", help="模型 id")
    p_set.add_argument("--effort", default="", help="推理强度")

    args = ap.parse_args()

    if args.cmd == "show":
        print(json.dumps(get_current(), ensure_ascii=False))
        return 0
    if args.cmd == "options":
        print(json.dumps(options(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "set":
        rec = apply(args.model, args.effort)
        print(json.dumps(rec, ensure_ascii=False))
        return 0 if rec.get("status") in ("ok", "unchanged") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
