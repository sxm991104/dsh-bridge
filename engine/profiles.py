"""任务端的模型配置：支持多套，随时切换。

为什么改成多套
    原来只有一套配置，想换模型就得把地址、Key、模型名全重填一遍。
    但实际使用中会想要：
        · 便宜的任务用便宜的模型
        · 难的活换成强的
        · 手上同时有几个厂商的 Key

    所以改成"配置列表"：
        每个配置有名字、地址、Key、模型
        有一个"当前用哪个"
        界面上能加、能删、能切换

存储格式（在用户目录下的 dsh-bridge/engine.json）
    {
      "active": "kimi",                 ← 当前用哪个（按 id）
      "profiles": [
        {"id": "kimi", "name": "Kimi", "base_url": "...", "api_key": "...",
         "model": "kimi-k2.6", "max_steps": 40},
        ...
      ]
    }

    兼容旧格式：如果读到的是老的单套配置（顶层就有 base_url），
    自动转成列表里的一项，不丢用户已经填好的东西。
"""

from __future__ import annotations

import json
import os
import time

APP_NAME = "dsh-bridge"

DEFAULT_MAX_STEPS = 40


def config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "engine.json")


def _new_id() -> str:
    return f"p{int(time.time() * 1000)}"


DEFAULT_CONCURRENCY = 1


def _blank_profile(name: str = "") -> dict:
    return {
        "id": _new_id(),
        "name": name or "新配置",
        "base_url": "",
        "api_key": "",
        "model": "",
        "max_steps": DEFAULT_MAX_STEPS,
        # 这套配置有没有启用。多套可以同时启用——它们不是"切换"关系，
        # 是"同时都在干活"，各自有各自的并发额度。
        "enabled": True,
        # 这套最多同时跑几个任务。满了就排队。
        # 默认 1：保守，用户自己按服务商的限额往上加。
        "concurrency": DEFAULT_CONCURRENCY,
    }


def load_all() -> dict:
    """读全部配置。老格式会自动迁移。"""
    p = config_path()
    data: dict = {}

    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}

    if not isinstance(data, dict):
        data = {}

    # 已经是新格式
    if isinstance(data.get("profiles"), list):
        data.setdefault("active", "")
        # 补齐缺字段
        for prof in data["profiles"]:
            if not isinstance(prof, dict):
                continue
            prof.setdefault("id", _new_id())
            prof.setdefault("name", "未命名")
            prof.setdefault("base_url", "")
            prof.setdefault("api_key", "")
            prof.setdefault("model", "")
            prof.setdefault("max_steps", DEFAULT_MAX_STEPS)
            prof.setdefault("enabled", True)
            prof.setdefault("concurrency", DEFAULT_CONCURRENCY)
        return data

    # 老格式：顶层就是一套配置
    if data.get("base_url") or data.get("api_key") or data.get("model"):
        prof = {
            "id": "default",
            "name": "默认",
            "base_url": data.get("base_url") or "",
            "api_key": data.get("api_key") or "",
            "model": data.get("model") or "",
            "max_steps": int(data.get("max_steps") or DEFAULT_MAX_STEPS),
            "enabled": True,
            "concurrency": DEFAULT_CONCURRENCY,
        }
        return {"active": "default", "profiles": [prof]}

    return {"active": "", "profiles": []}


def save_all(data: dict) -> None:
    p = config_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def active_profile() -> dict:
    """当前用哪套。没有就返回空 dict。"""
    data = load_all()
    active = data.get("active") or ""
    for prof in data.get("profiles") or []:
        if prof.get("id") == active:
            return prof
    # active 指向不存在的东西时，退回第一个
    profiles = data.get("profiles") or []
    return profiles[0] if profiles else {}


def add_profile(prof: dict) -> dict:
    """加一套配置。返回新加的那套（带生成的 id）。"""
    data = load_all()
    newp = {
        "id": prof.get("id") or _new_id(),
        "name": (prof.get("name") or "新配置").strip(),
        "base_url": (prof.get("base_url") or "").strip(),
        "api_key": (prof.get("api_key") or "").strip(),
        "model": (prof.get("model") or "").strip(),
        "max_steps": int(prof.get("max_steps") or DEFAULT_MAX_STEPS),
        "enabled": bool(prof.get("enabled", True)),
        "concurrency": max(1, min(20, int(prof.get("concurrency") or DEFAULT_CONCURRENCY))),
    }
    data.setdefault("profiles", []).append(newp)
    # 第一套自动设为当前
    if not data.get("active"):
        data["active"] = newp["id"]
    save_all(data)
    return newp


def update_profile(pid: str, patch: dict) -> bool:
    """改一套配置。key 传空表示不改。"""
    data = load_all()
    for prof in data.get("profiles") or []:
        if prof.get("id") != pid:
            continue
        for k in ("name", "base_url", "model"):
            if k in patch and isinstance(patch[k], str):
                prof[k] = patch[k].strip()
        # key 单独处理：空字符串表示"不改"，不是"清空"
        k = patch.get("api_key")
        if isinstance(k, str) and k.strip():
            prof["api_key"] = k.strip()
        if "max_steps" in patch:
            try:
                prof["max_steps"] = int(patch["max_steps"])
            except (TypeError, ValueError):
                pass
        if "enabled" in patch:
            prof["enabled"] = bool(patch["enabled"])
        if "concurrency" in patch:
            try:
                n = int(patch["concurrency"])
                prof["concurrency"] = max(1, min(20, n))
            except (TypeError, ValueError):
                pass
        save_all(data)
        return True
    return False


def delete_profile(pid: str) -> bool:
    """删一套。删的如果是当前用的，切到剩下的第一个。"""
    data = load_all()
    before = len(data.get("profiles") or [])
    data["profiles"] = [p for p in (data.get("profiles") or [])
                        if p.get("id") != pid]
    if len(data["profiles"]) == before:
        return False
    if data.get("active") == pid:
        data["active"] = data["profiles"][0]["id"] if data["profiles"] else ""
    save_all(data)
    return True


def set_active(pid: str) -> bool:
    data = load_all()
    if not any(p.get("id") == pid for p in (data.get("profiles") or [])):
        return False
    data["active"] = pid
    save_all(data)
    return True


def is_configured(prof: dict | None = None) -> bool:
    """这套配置能不能用（三样都填了）。"""
    p = prof if prof is not None else active_profile()
    return bool(p.get("base_url") and p.get("api_key") and p.get("model"))

# ----------------------------------------------------------------------
# 按 id 取、按启用状态筛
# ----------------------------------------------------------------------

def get_profile(pid: str) -> dict:
    """按 id 取一套。没有就返回空 dict。"""
    for prof in (load_all().get("profiles") or []):
        if prof.get("id") == pid:
            return prof
    return {}


def enabled_profiles() -> list[dict]:
    """所有启用的、而且配好了的配置。派活时从这里面挑。"""
    out = []
    for prof in (load_all().get("profiles") or []):
        if not prof.get("enabled"):
            continue
        if not (prof.get("base_url") and prof.get("api_key") and prof.get("model")):
            continue
        out.append(prof)
    return out


def default_profile() -> dict:
    """没指定用哪套时的默认选择。

    启用的里面挑第一个。全都没启用就退回第一套。
    """
    eps = enabled_profiles()
    if eps:
        return eps[0]
    profiles = load_all().get("profiles") or []
    return profiles[0] if profiles else {}
