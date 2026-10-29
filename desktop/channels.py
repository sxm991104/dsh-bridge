"""通道管理：QQ 和飞书分开启停。

为什么这么做
    原来点一次「启动」，QQ 和飞书一起连上：
        QQ   → 拉起 QQ 客户端（几百兆内存）+ NapCat
        飞书 → 纯网络连接（几十兆）
    只用飞书的人，QQ 那部分白占内存，低配机器直接卡。

    现在拆开：想用哪个点哪个，没点的不启动。

分成三层
    AstrBot      宿主，两条通道都要它（插件跑在里面）
    QQ 通道       NapCat + QQ 客户端 + AstrBot 里那条连接
    飞书通道      只有 AstrBot 里那条连接，轻

    「只开飞书」= 起 AstrBot + 关掉 QQ 平台
    「只开 QQ」  = 起 AstrBot + 拉起 QQ 客户端 + 关掉飞书平台

怎么操作 AstrBot 里的平台
    插件 astrbot_plugin_dsh_channels 开了个本机接口（6186），
    能热启停平台——不用重启 AstrBot（那要 90 秒）。

    这个模块只是它的客户端。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

CHANNEL_PORT = 6186


def _token_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "dsh-bridge")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "channel_token.txt")


def _read_token() -> str:
    """读插件写的 token。

    AstrBot 没启动时读不到——那是正常的，等它起来再说。
    """
    p = _token_path()
    if not os.path.exists(p):
        return ""
    try:
        return open(p, encoding="utf-8").read().strip()
    except OSError:
        return ""


def _call(path: str, body: dict | None = None, timeout: int = 90) -> dict:
    """调插件的接口。连不上就返回带 error 的 dict，不抛。"""
    token = _read_token()
    if not token:
        return {"ok": False, "error": "AstrBot 还没起来（读不到 token）"}

    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{CHANNEL_PORT}{path}",
            data=json.dumps(body).encode() if body else None,
            headers={"Content-Type": "application/json", "X-Token": token},
            method="POST" if body else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def available() -> bool:
    """启停接口通不通（= AstrBot 起来了没有）。"""
    d = _call("/channels", timeout=8)
    return bool(d.get("ok"))


def status() -> dict:
    """两条通道现在什么状态。

    返回：
        {"ok": True, "platforms": [{id, type, enabled, connected}, ...]}
    或者失败时 {"ok": False, "error": "..."}
    """
    return _call("/channels", timeout=8)


def get_enabled(platform_id: str) -> bool | None:
    """某个平台现在开着没有。不知道就返回 None。"""
    d = status()
    if not d.get("ok"):
        return None
    for p in d.get("platforms") or []:
        if p.get("id") == platform_id:
            return bool(p.get("connected"))
    return None


def enable(platform_id: str) -> dict:
    return _call("/channels/enable", {"id": platform_id})


def disable(platform_id: str) -> dict:
    return _call("/channels/disable", {"id": platform_id})


def wait_ready(timeout: float = 120.0) -> bool:
    """等 AstrBot 的通道接口起来。

    刚启动 AstrBot 时插件还没加载完，要等一会儿。
    """
    end = time.time() + timeout
    while time.time() < end:
        if available():
            return True
        time.sleep(2)
    return False


# ── 给桌面端用的高层动作 ──────────────────────────────────


def describe() -> str:
    """给界面显示的一句话。"""
    d = status()
    if not d.get("ok"):
        return "AstrBot 没起来"

    parts = []
    for p in d.get("platforms") or []:
        name = "QQ" if p.get("type") == "aiocqhttp" else "飞书"
        parts.append(f"{name} {'开' if p.get('connected') else '关'}")
    return " · ".join(parts) if parts else "没有配通道"
