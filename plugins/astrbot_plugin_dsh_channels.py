"""按需启停平台通道。

为什么做这个
    原来点一次「启动」，QQ 和飞书一起起来：
        QQ  → 拉起 QQ 客户端 + NapCat（几百兆）
        飞书 → 纯网络连接（几十兆）
    只用飞书的用户，QQ 那部分白占内存，低配机器直接带不动。

    所以拆开：想用哪个点哪个，没点的不启动。

怎么做的
    AstrBot 的 PlatformManager 有现成的两个方法：
        terminate_platform(id)   停掉一个平台
        load_platform(config)    加载一个平台（会看 enable 字段）

    这两个可以**热调**，不用重启 AstrBot（重启要 90 秒）。

    但插件本身跑在 AstrBot 进程里，桌面端在外面调不到，
    所以这里开几个 HTTP 接口给桌面端用。

安全
    这个服务只监听 127.0.0.1，外部连不上。
    另外带一个 token，桌面端启动时随机生成，写进本地文件。
    ——虽然本地工具，但公网暴露的教训刚吃过，能防就防。
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_dsh_channels"
LISTEN_PORT = 6186

# token 存在固定位置，桌面端读它
TOKEN_PATH = os.path.join(
    os.environ.get("APPDATA") or os.path.expanduser("~"),
    "dsh-bridge",
    "channel_token.txt",
)


@register(PLUGIN_NAME, "dsh", "按需启停 QQ / 飞书通道", "1.0.0")
class ChannelsPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._token = ""

    async def initialize(self) -> None:
        # 把这个事件循环存下来——HTTP 线程里要用它跑 async 方法
        import asyncio

        self._loop = asyncio.get_running_loop()
        self._token = self._load_or_make_token()
        try:
            self._start_server()
            logger.info(f"[通道] 启停接口已就绪：http://127.0.0.1:{LISTEN_PORT}")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[通道] 启停接口起不来：{type(e).__name__}: {e}")

    def _load_or_make_token(self) -> str:
        """读已有的 token，没有就生成一个。

        放文件的目的是桌面端能拿到——它跟 AstrBot 是两个进程。
        """
        try:
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            if os.path.exists(TOKEN_PATH):
                t = open(TOKEN_PATH, encoding="utf-8").read().strip()
                if t:
                    return t
        except OSError:
            pass

        t = secrets.token_urlsafe(24)
        try:
            os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                f.write(t)
        except OSError:
            logger.warning("[通道] token 写不下来，桌面端可能连不上")
        return t

    # ── 状态 ──────────────────────────────────────────────

    def _platforms(self) -> list[dict]:
        """当前配置里的平台，附上"现在连着没有"。"""
        cfg = self.context.get_config()
        items = cfg.get("platform") or []
        insts = {}
        try:
            for inst in self.context.platform_manager.get_insts():
                meta = getattr(inst, "meta", None)
                if meta is not None:
                    insts[meta().id] = True
        except Exception:  # noqa: BLE001
            pass

        out = []
        for p in items:
            pid = p.get("id") or ""
            out.append({
                "id": pid,
                "type": p.get("type") or "",
                "enabled": bool(p.get("enable")),
                "connected": pid in insts,
            })
        return out

    # ── 启停 ──────────────────────────────────────────────

    async def _set_enabled(self, platform_id: str, enable: bool) -> dict:
        """把某个平台打开或关掉。

        先改配置里的 enable，再决定要不要真正连上。
        改配置是必须的——不然重启之后又回去了。
        """
        cfg = self.context.get_config()
        items = cfg.get("platform") or []

        target = None
        for p in items:
            if p.get("id") == platform_id:
                target = p
                break
        if target is None:
            return {"ok": False, "error": f"没有这个平台：{platform_id}"}

        target["enable"] = bool(enable)
        try:
            cfg.save_config()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"保存配置失败：{e}"}

        pm = self.context.platform_manager

        try:
            if enable:
                await pm.reload(target)
            else:
                await pm.terminate_platform(platform_id)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"操作失败：{type(e).__name__}: {e}"}

        return {"ok": True, "id": platform_id, "enabled": bool(enable)}

    # ── HTTP 接口 ─────────────────────────────────────────

    def _start_server(self) -> None:
        plugin = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 别把访问日志刷进 AstrBot 日志
                pass

            def _json(self, obj: dict, code: int = 200) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                # 只认本机 + 对的 token
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    return False
                return self.headers.get("X-Token", "") == plugin._token

            def do_GET(self):  # noqa: N802
                if not self._authed():
                    return self._json({"ok": False, "error": "没权限"}, 403)

                path = urlparse(self.path).path
                if path == "/channels":
                    return self._json({"ok": True, "platforms": plugin._platforms()})
                return self._json({"ok": False, "error": "没有这个接口"}, 404)

            def do_POST(self):  # noqa: N802
                if not self._authed():
                    return self._json({"ok": False, "error": "没权限"}, 403)

                path = urlparse(self.path).path
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:  # noqa: BLE001
                    body = {}

                if path == "/channels/enable":
                    pid = (body.get("id") or "").strip()
                    if not pid:
                        return self._json({"ok": False, "error": "没说哪个平台"}, 400)
                    # 这里得回到 AstrBot 的事件循环里跑，
                    # 因为启停是 async、而且会碰事件循环里的东西
                    fut = plugin._run_in_loop(plugin._set_enabled(pid, True))
                    return self._json(fut, 200 if fut.get("ok") else 500)

                if path == "/channels/disable":
                    pid = (body.get("id") or "").strip()
                    if not pid:
                        return self._json({"ok": False, "error": "没说哪个平台"}, 400)
                    fut = plugin._run_in_loop(plugin._set_enabled(pid, False))
                    return self._json(fut, 200 if fut.get("ok") else 500)

                return self._json({"ok": False, "error": "没有这个接口"}, 404)

        self._server = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="channels"
        )
        self._thread.start()

    def _run_in_loop(self, coro) -> dict:
        """HTTP 线程里拿不到当前事件循环，把活丢回主循环去跑。

        踩过的坑：在 HTTP 线程里 await 会报 "no running event loop"。
        必须用 run_coroutine_threadsafe 把协程交给主循环。

        主循环从哪来：initialize() 是在 AstrBot 的事件循环里被调的，
        那时候存一份 self._loop 下来。用 asyncio.get_event_loop()
        在别的线程里拿到的不是同一个循环，拿到也跑不了。
        """
        import asyncio

        loop = getattr(self, "_loop", None)
        if loop is None or loop.is_closed():
            return {"ok": False, "error": "主事件循环不可用"}

        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return fut.result(timeout=90)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def terminate(self) -> None:
        try:
            if self._server:
                self._server.shutdown()
                self._server = None
            logger.info("[通道] 启停接口已关闭")
        except Exception:  # noqa: BLE001
            pass
