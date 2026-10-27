"""切换任务端（电脑端）那个我用的模型。

为什么模型和 provider 要绑在一起切
    电脑端的模型不是独立存在的，它挂在某条"通道"上：

        codebuddy 通道     → deepseek-v4.1-flash / deepseek-v4-pro
                             （省钱，但有每日额度，且看不了图）
        deepseek-official  → deepseek-flash
                             （按量付费，没额度，能看图）

    所以光改 model 不改 provider，dsh 会报"这个通道没有这个模型"。
    这个工具用"预设"把两者绑在一起，一次切对。

怎么用
    python tune_model.py show                        # 看现在用什么
    python tune_model.py options                     # 列出所有预设
    python tune_model.py set --preset cb-flash       # 切到 codebuddy 4.1-flash
    python tune_model.py set --preset cb-pro         # 切到 codebuddy 4-pro
    python tune_model.py set --preset official-flash # 切回官方 API
    python tune_model.py set --effort max            # 只调推理强度（仅 codebuddy 认）

    每次 `dsh qqme` 都是新进程，会重新读这个文件，
    所以改完不用重启任何服务，下一个任务立刻生效。

⚠️ 只改这个文件里自己的两段，别碰别的。
    人格提示词（personaPrefix）那一大段也在这个文件里，
    改坏了电脑端那个我就变味了。这里只重建文件头的注释和
    agent-default-model 那一个块，其余原样保留。
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
    """定位 qqme 的 profile 配置文件。"""
    env = os.environ.get("DSH_PROFILE_FILE", "").strip()
    if env:
        return env
    home = os.environ.get("DSH_HOME") or os.path.join(os.path.expanduser("~"), ".dsh")
    return os.path.join(home, "profiles", "qqme", "cordis.patch.yml")


PROFILE = _profile_path()

# ----------------------------------------------------------------------
# 预设：provider + model 绑在一起
# ----------------------------------------------------------------------

PRESETS = {
    "cb-flash": {
        "provider": "codebuddy",
        "model": "deepseek-v4.1-flash",
        "effort_default": "high",
        "label": "codebuddy 4.1-flash — 省钱，日常够用；有每日额度，看不了图",
    },
    "cb-pro": {
        "provider": "codebuddy",
        "model": "deepseek-v4-pro",
        "effort_default": "high",
        "label": "codebuddy 4-pro — 省钱且更强、更慢；同样有每日额度、看不了图",
    },
    "official-flash": {
        "provider": "deepseek-official",
        "model": "deepseek-flash",
        "effort_default": "",
        "label": "官方 API deepseek-flash — 按量付费，没有每日额度，能看图",
    },
}

# 兼容旧叫法：直接给模型名也能认出是哪个预设
MODEL_TO_PRESET = {
    "deepseek-v4.1-flash": "cb-flash",
    "deepseek-v4-pro": "cb-pro",
    "deepseek-flash": "official-flash",
}

# ⚠️ 没有 medium —— 实测 deepseek-v4.1-flash 不支持它。
#    设成 medium 会报 UNSUPPORTED_REASONING_EFFORT，整个任务白跑。
EFFORTS = {
    "low": "几乎不想，最快，适合搬砖活",
    "high": "认真想（默认）",
    "max": "往死里想，最慢也最费",
}

# 文件头的注释也按预设重写，不然切完通道，注释还写着旧通道的理由
_HEADER_CB = """# qqme：headless 运行时，用 codebuddy 模型 + 我的人格
#
# 为什么用 codebuddy：
#   · 省钱——走 CodeBuddy 订阅，不烧官方 API 的按量费用
#   · 代价：有每日额度上限，超了任务会哑；这条通道看不了图
# 额度用完或要看图时，切到官方：
#   python tune_model.py set --preset official-flash
"""

_HEADER_OFFICIAL = """# qqme：headless 运行时，用官方 DeepSeek（按量付费） + 我的人格
#
# 为什么用官方：
#   · 没有每日额度，按量付费，没钱手机充值就行
#   · 支持图片（read_image 真能看见）
# 想省钱时切回 codebuddy：
#   python tune_model.py set --preset cb-flash
"""


def _read(path: str = PROFILE) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def get_current(path: str = PROFILE) -> dict:
    """读出当前的 provider / model / reasoningEffort。"""
    try:
        txt = _read(path)
    except OSError as e:
        return {"status": "error", "error": f"读不到配置: {e}"}

    provider = ""
    model = ""
    effort = ""
    in_block = False
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("- id: agent-default-model"):
            in_block = True
            continue
        if in_block:
            if s.startswith("- id:") and "agent-default-model" not in s:
                break
            m = re.match(r"^provider:\s*(\S+)", s)
            if m:
                provider = m.group(1)
            m = re.match(r"^model:\s*(\S+)", s)
            if m:
                model = m.group(1)
            m = re.match(r"^reasoningEffort:\s*(\S+)", s)
            if m:
                effort = m.group(1)

    preset = ""
    for pid, spec in PRESETS.items():
        if spec["provider"] == provider and spec["model"] == model:
            preset = pid
            break

    return {
        "status": "ok",
        "provider": provider or "(未设置)",
        "model": model or "(未设置)",
        "reasoning_effort": effort or "(未设置)",
        "preset": preset or "(未知组合)",
        "path": path,
    }


def _build_block(spec: dict, effort: str) -> list[str]:
    lines = [
        "- id: agent-default-model",
        '  name: "@deepseek-ai/dsh-agent-default-model"',
        "  config:",
        f"    provider: {spec['provider']}",
        f"    model: {spec['model']}",
    ]
    if effort:
        lines.append(f"    reasoningEffort: {effort}")
    return lines


def apply(
    preset_id: str = "",
    effort: str = "",
    path: str = PROFILE,
) -> dict:
    """按预设重建 agent-default-model 块（可选带推理强度）。"""
    if preset_id and preset_id not in PRESETS:
        return {
            "status": "error",
            "error": f"不支持的预设：{preset_id}。可选：{', '.join(PRESETS)}",
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

    has_bom = txt.startswith("\ufeff")
    body = txt.lstrip("\ufeff")
    lines = body.splitlines()

    # 定位 agent-default-model 块
    start = None
    end = None
    for i, line in enumerate(lines):
        if line.strip().startswith("- id: agent-default-model"):
            start = i
            for j in range(i + 1, len(lines)):
                if lines[j].strip().startswith("- id:"):
                    end = j
                    break
            if end is None:
                end = len(lines)
            break
    if start is None:
        return {"status": "error", "error": "找不到 agent-default-model 块，不敢改"}

    # 决定用哪个 spec 和 effort
    cur = get_current(path)
    if preset_id:
        spec = PRESETS[preset_id]
        if effort:
            eff = effort.lower()
        else:
            eff = spec.get("effort_default", "")
        if spec["provider"] != "codebuddy" and eff:
            return {
                "status": "error",
                "error": "官方通道不认 reasoningEffort，别设。只切换预设就行。",
            }
    else:
        # 只调 effort：provider 不变，必须是 codebuddy 才认
        if cur.get("provider") != "codebuddy":
            return {
                "status": "error",
                "error": (
                    f"当前走的是 {cur.get('provider')}，不认推理强度。"
                    "要先切到 cb-flash / cb-pro。"
                ),
            }
        eff = (effort or "high").lower()
        spec = {
            "provider": cur.get("provider"),
            "model": cur.get("model"),
        }

    # 先备份
    try:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, f"{path}.bak-{stamp}")
    except OSError as e:
        return {"status": "error", "error": f"备份失败，不敢改: {e}"}

    # 重写文件头的注释 + 替换块
    header = _HEADER_CB if spec["provider"] == "codebuddy" else _HEADER_OFFICIAL
    first_row = next(
        (i for i, l in enumerate(lines) if l.strip().startswith("- id:")), 0
    )
    new_lines = (
        header.splitlines()
        + lines[first_row:start]
        + _build_block(spec, eff)
        + lines[end:]
    )
    new_txt = "\n".join(new_lines) + "\n"
    if has_bom:
        new_txt = "\ufeff" + new_txt

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_txt)
    except OSError as e:
        return {"status": "error", "error": f"写入失败: {e}"}

    now = get_current(path)
    return {
        "status": "ok",
        **now,
    }


def options() -> dict:
    """列出可选值，供调用方展示。"""
    return {
        "current": get_current(),
        "presets": {pid: s["label"] for pid, s in PRESETS.items()},
        "efforts": EFFORTS,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="切换任务端的模型/通道")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="看当前设置")
    sub.add_parser("options", help="列出可选值")

    p_set = sub.add_parser("set", help="改设置")
    p_set.add_argument("--preset", default="", help="预设名")
    p_set.add_argument("--model", default="", help="（旧写法）模型名，自动映射成预设")
    p_set.add_argument("--effort", default="", help="推理强度")

    args = ap.parse_args()

    if args.cmd == "show":
        print(json.dumps(get_current(), ensure_ascii=False))
        return 0
    if args.cmd == "options":
        print(json.dumps(options(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "set":
        preset = args.preset
        if not preset and args.model:
            preset = MODEL_TO_PRESET.get(args.model, "")
            if not preset:
                print(json.dumps(
                    {"status": "error", "error": f"不认识的模型名：{args.model}"},
                    ensure_ascii=False))
                return 1
        if not preset and not args.effort:
            print(json.dumps(
                {"status": "error", "error": "至少要给 --preset 或 --effort 其中一个"},
                ensure_ascii=False))
            return 1
        rec = apply(preset, args.effort)
        print(json.dumps(rec, ensure_ascii=False))
        return 0 if rec.get("status") == "ok" else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
