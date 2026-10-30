"""桥的 HTTP 接口，给桌面面板用。

为什么要有这一层
    面板如果每次都去起一个 python 进程调 bridge.py，会有两个问题：
        1. 慢——每次几百毫秒，界面上点点点就卡
        2. 吵——一堆进程来回起，日志也乱

    所以桥开一个常驻的本地 HTTP 服务，面板直接请求它。
    只监听 127.0.0.1，不对外。

设计上守住的一条
    **面板只认这层接口，不认 AstrBot。**
    以后 AstrBot 换成自研的，面板不用改；后台从 DeepSeek 换成别的，面板也不用改。
    桥是那个稳定的中间层——今天把任务端从 codebuddy 换到官方 API，
    桥一行没动，就是这个道理。

接口
    GET  /api/tasks              任务列表（带状态、花费、时长）
    GET  /api/task/<名字>        单个任务详情
    GET  /api/task/<名字>/log    这个任务的历史轮次
    POST /api/dispatch           派活   {task, name}
    POST /api/continue           续话   {name, message}
    POST /api/cancel             停     {name}
    GET  /api/live               正在跑的任务 + 当前动作
    GET  /api/outbox             未读的完成通知
    GET  /api/health             存活检查
    GET  /                       面板页面（浏览器打开就是界面）

    前端轮询 /api/tasks 就行，几百毫秒一次都不重——读的是 state 目录里的小文件。

启动
    python api_server.py            # 默认 8790
    python api_server.py --port 9000
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote

def _app_dir() -> str:
    """程序所在目录。源码运行时是脚本目录，打包成 exe 后是 exe 所在目录。

    ⚠️ 打包之后 sys._MEIPASS 是 PyInstaller 每次启动解压出来的临时目录，
        每次都不一样。数据文件（tasks.jsonl、state/、workspaces/）如果
        跟着它走，用户下次启动就看不到上次的任务了。
        所以：
            只读资源（html）  → 从 _MEIPASS 读
            运行数据          → 固定在 exe 旁边
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _res_dir() -> str:
    """只读资源目录（html 等）。打包后在 _MEIPASS 里。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BRIDGE_DIR = _app_dir()
sys.path.insert(0, BRIDGE_DIR)

import bridge  # noqa: E402

# 桌面端要能自己起停服务，不经过任何命令行窗口。
# launcher 模块就是干这个的，它把 bat 里的活重做成了 Python。
sys.path.insert(0, os.path.join(BRIDGE_DIR, "desktop"))
try:
    from launcher import Launcher, find_qq, port_open, QQ_PORT, ASTRBOT_PORT  # noqa: E402

    _launcher = Launcher()
except Exception:  # noqa: BLE001
    _launcher = None

DEFAULT_PORT = 8790


def _json(obj, status: int = 200) -> tuple[int, bytes]:
    return status, json.dumps(obj, ensure_ascii=False).encode("utf-8")


# ── 服务状态 ──
#
# 桌面端要给普通人看"现在能不能用"，所以这里只回答三个是/否：
#   qq     QQ 登录了没（NapCat 的 3000 端口通 = 登录着）
#   bot    对话服务在不在（AstrBot 的 6185）
#   bridge 后台任务端在不在（能用 dsh 跑东西）
#
# 不返回任何配置细节、密码、key——这是给人看的面板，不是运维台。

_NAPCAT_PORT = 3000
_BOT_PORT = 6185


def _port_ok(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _qrcode_patterns() -> list[str]:
    """二维码在哪。跟着用户配的 NapCat 目录走，不写死。"""
    import paths

    nap = paths.get("napcat_dir")
    pats = []
    if nap:
        pats.append(os.path.join(nap, "cache", "qrcode.png"))
    # 兜底：常见位置下再找一层
    for root in (r"D:\qqaibot\napcat", r"C:\qqaibot\napcat"):
        pats.append(os.path.join(root, "*", "cache", "qrcode.png"))
        pats.append(os.path.join(root, "*", "*", "cache", "qrcode.png"))
    return pats


def _listening_pids(port: int) -> list[str]:
    """找到监听某个端口的进程（Windows）。"""
    import subprocess

    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True, timeout=15,
        ).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return []
    pids = []
    for line in out.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            pids.append(line.split()[-1])
    return list(dict.fromkeys(pids))


def https_status() -> dict:
    """三个开关各自的状态。

    QQ 和飞书看的是"这条通道真的连上没有"，不是"进程在不在"。
    进程起着但连接断了，用户一样用不了，就该显示成关的。

    任务端没有常驻进程，永远是通的——它每次派活才起。
    """
    qq_ok = False
    fs_ok = False
    try:
        from desktop import channels as _ch

        d = _ch.status()
        if d.get("ok"):
            for p in d.get("platforms") or []:
                if p.get("type") == "aiocqhttp":
                    qq_ok = bool(p.get("connected"))
                elif p.get("type") == "lark":
                    fs_ok = bool(p.get("connected"))
        else:
            # 启停接口还没起来（AstrBot 刚启动或者没起），
            # 退回看端口——至少能反映"宿主开着没有"
            qq_ok = _port_ok(_NAPCAT_PORT) and _port_ok(_BOT_PORT)
            fs_ok = _port_ok(_BOT_PORT)
    except Exception:  # noqa: BLE001
        qq_ok = _port_ok(_NAPCAT_PORT)
        fs_ok = _port_ok(_BOT_PORT)

    return {
        "qq": qq_ok,
        "feishu": fs_ok,
        "bridge": True,
    }


# 本机接口的访问 token。main() 启动时生成/读取，_guard() 用它校验。
_API_TOKEN = ""


class Handler(BaseHTTPRequestHandler):
    server_version = "dsh-bridge-api/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        # 面板轮询很频繁，别把每条都写进控制台
        pass

    # ── 工具 ──

    def _send(self, status: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # ⚠️ 这里原来是 Access-Control-Allow-Origin: "*"。
        #
        #    通配符的意思是"任何网站都可以跨源读我的响应"。
        #    这个接口能派活、能读任务内容，不该让随便哪个网页读。
        #
        #    改成只回显本机来源。面板页面由同一个服务发出（同源），
        #    本来就不需要 CORS；留一个本机来源是给调试用的。
        origin = self.headers.get("Origin", "")
        if origin.startswith(("http://127.0.0.1", "http://localhost")):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"")

    def _guard(self) -> bool:
        """三层防护，过不了就直接回绝。

        第一层  地址必须是本机（绑了 127.0.0.1，这里是第二道确认）
        第二层  带浏览器 Origin 头的拒绝（防恶意网页偷调）
        第三层  token 必须对

        为什么 token 放最后检查：
            前面两层是便宜的判断，token 要读文件、比字符串。
            先便宜的、后贵的。

        例外：/api/health 不需要 token。
            它是给"服务活没活着"用的，不碰任何数据，
            桌面端和启动器都要在拿 token 之前问它。
        """
        import security as _sec

        # 第一层：只接受本机连接
        if not _sec.is_local_address(self.client_address[0]):
            self._send(403, b'{"error":"only local connections"}')
            return False

        # DNS rebinding 检查：Host 头必须是本机名。
        #
        # 攻击者把 evil.com 的 DNS 指到 127.0.0.1，
        # 用户打开 evil.com 时，请求会打到我们这里，
        # 但 Host 头是 evil.com。这种请求直接拒。
        if not _sec.host_is_local(self.headers):
            self._send(403, b'{"error":"bad host"}')
            return False

        # 第二层：浏览器一律拒绝。
        #
        # 这条挡的是：用户打开了一个恶意网页，那个网页
        # 往 localhost:8790 POST 一个任务。浏览器能发出去
        # （跨源只拦读响应，不拦发请求），而派活不需要读响应。
        if _sec.has_browser_origin(self.headers):
            self._send(403, b'{"error":"browser requests not allowed"}')
            return False

        # 第三层：token
        if getattr(self, "path", "") in ("/api/health",):
            return True

        # 恒定时间比较 + 空 token 一律拒。
        #
        # ⚠️ 原来写的是 `if want and got != want`——
        #    want 为空时整句短路成 False，等于**放行所有人**。
        #    token 生成失败（文件写不进去）的时候就是这个状态。
        #    现在 token_matches 明确处理了空的情况。
        got = self.headers.get("X-Token", "")
        if not _sec.token_matches(got, _API_TOKEN):
            self._send(401, b'{"error":"bad or missing X-Token"}')
            return False

        return True

    # 请求体最大多少。
    #
    # 实测过：不限制的话，一个 2MB 的 task 字段会被原样接受，
    # 然后喂给模型——那个任务的 token 费用能烧掉一大笔钱。
    # 而且超大请求体本身也是种资源消耗。
    #
    # 1MB 足够用了：最长的任务描述也就几万字。
    MAX_BODY = 1024 * 1024

    @staticmethod
    def _str_field(body: dict, key: str, default: str = "") -> str:
        """从请求体里取一个字符串字段。

        ⚠️ 为什么不能直接用 body.get(key)：
            攻击者传 {"task": 123} 时，拿到的是 int，
            后面调 .strip() 就崩，连接断掉。
            {"name": [1,2]} 同理。
            这类崩溃不算能利用，但是拒绝服务——请求不是干净地
            回 400，而是把连接弄断。

        非字符串一律当没给（走下面的"必填"检查回 400）。
        """
        v = body.get(key)
        if not isinstance(v, str):
            return default
        return v

    def _body(self) -> dict:
        """读请求体。

        ⚠️ 必须校验解析出来的类型。
            原来直接 json.loads 就往回返，于是：
                body = null   → None，后面 body.get() 崩
                body = []     → list，同样崩
                body = 123    → int，崩
            这些请求会让连接直接断掉（不是干净地回错误码）。
            攻击者拿这个就能做拒绝服务。
        """
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0

        if n <= 0:
            return {}

        if n > self.MAX_BODY:
            # 不读、直接拒。读了就白消耗内存。
            self._send(413, b'{"error":"request body too large"}')
            return None

        try:
            raw = self.rfile.read(n)
        except OSError:
            return {}

        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, b'{"error":"bad json"}')
            return None

        # 必须是对象（dict）。数组、字符串、数字、null 一律不当请求体。
        if not isinstance(data, dict):
            self._send(400, b'{"error":"body must be a json object"}')
            return None

        return data

    # ── 路由 ──

    def do_GET(self):  # noqa: N802
        if not self._guard():
            return
        path = unquote(urlparse(self.path).path)

        # 根路径直接给面板页面。
        # 为什么由这个服务来发：双击 html 打开的话是 file:// 协议，
        # fetch 到 127.0.0.1 会被浏览器当成跨源请求拦掉。
        # 同一个服务发页面 + 接口，就没有这个问题，用户也不用装任何东西。
        if path in ("/", "/index.html"):
            # 根路径给桌面版界面（desktop_ui.html）；
            # /panel 给早先那个简易版，留着方便对比调试
            for fn in ("desktop_ui.html", "panel.html"):
                f = os.path.join(BRIDGE_DIR, fn)
                if os.path.exists(f):
                    try:
                        return self._send(200, open(f, "rb").read(),
                                          "text/html; charset=utf-8")
                    except OSError:
                        continue
            return self._send(404, b'<h1>UI not found</h1>', "text/html; charset=utf-8")

        if path == "/panel":
            f = os.path.join(_res_dir(), "panel.html")
            if os.path.exists(f):
                return self._send(200, open(f, "rb").read(), "text/html; charset=utf-8")
            return self._send(404, b'<h1>not found</h1>', "text/html; charset=utf-8")

        if path == "/api/health":
            st, b = _json({"ok": True, "time": datetime.now().isoformat(timespec="seconds")})
            return self._send(st, b)

        if path == "/api/tasks":
            return self._tasks()

        if path.startswith("/api/windows"):
            return self._windows(path)

        if path == "/api/live":
            try:
                st, b = _json(bridge.cmd_live())
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/channels":
            try:
                from desktop import channels as _ch

                d = _ch.status()
                st, b = _json(d)
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            return self._send(st, b)

        if path == "/api/services":
            try:
                return self._send(*_json(https_status()))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"error": str(e)}, 500))

        if path == "/api/model":
            return self._model_get()

        if path == "/api/paths":
            return self._paths_get()

        if path == "/api/engine":
            return self._engine_get()

        if path == "/api/engine/profiles":
            return self._profiles_get()


        if path == "/api/perms":
            try:
                return self._send(*_json(self._perms_get()))
            except Exception as e:  # noqa: BLE001
                return self._send(
                    *_json({"ok": False, "error": str(e)}, 500)
                )

        if path == "/api/guard":
            try:
                import guard

                c = guard.load_config()
                return self._send(*_json({
                    "ok": True,
                    "enabled": c.get("enabled"),
                    "email": c.get("email") or "",
                    "email_masked": guard._mask_email(c.get("email") or ""),
                    "smtp_host": c.get("smtp_host") or "",
                    "smtp_port": c.get("smtp_port") or 465,
                    "smtp_user": c.get("smtp_user") or "",
                    "has_pass": bool(c.get("smtp_pass")),
                    "ready": guard.is_guard_ready(),
                }))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/autostart":
            try:
                import paths

                return self._send(*_json({
                    "ok": True,
                    "enabled": paths.is_autostart(),
                }))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/qrcode":
            return self._qrcode()

        if path == "/api/outbox":
            return self._outbox()

        if path.startswith("/api/task/"):
            rest = path[len("/api/task/"):]
            if rest.endswith("/log"):
                return self._task_log(rest[: -len("/log")])
            return self._task(rest)

        return self._send(404, b'{"error":"not found"}')

    def do_POST(self):  # noqa: N802
        """所有 POST 入口。

        外面包一层兜底：任何没预料到的异常，都回一个 500，
        而不是让连接直接断掉。

        为什么要这层：
            实测过 {"task": 123} 这种类型不对的请求能崩到连接断开。
            那算是拒绝服务——客户端拿不到任何错误码，
            只能看到"连接被重置"，排查时一脸茫然。

            内部处理函数各自还有 try，这层是最后一道。
        """
        try:
            self._do_post()
        except Exception as e:  # noqa: BLE001
            try:
                self._send(
                    500,
                    json.dumps(
                        {"error": f"{type(e).__name__}"}, ensure_ascii=False
                    ).encode("utf-8"),
                )
            except Exception:  # noqa: BLE001
                pass

    def _do_post(self):  # noqa: N802
        if not self._guard():
            return
        path = urlparse(self.path).path
        body = self._body()
        if body is None:
            # _body 已经回过错误了（413 太大 / 400 不是对象），别再往下走
            return

        if path == "/api/dispatch":
            task = self._str_field(body, "task").strip()
            name = self._str_field(body, "name").strip()
            profile = self._str_field(body, "profile").strip()
            if not task:
                return self._send(400, b'{"error":"task is required"}')
            if not name:
                return self._send(400, b'{"error":"name is required"}')
            # 名字先过一遍校验，不合法就直接拒，别让它在后面炸
            try:
                from safe_path import safe_name as _sn

                _sn(name)
            except ValueError as e:
                return self._send(
                    400,
                    json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"),
                )
            try:
                st, b = _json(self._dispatch_with_concurrency(task, name, profile))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/continue":
            name = self._str_field(body, "name").strip()
            msg = self._str_field(body, "message", "继续").strip()
            if not name:
                return self._send(400, b'{"error":"name is required"}')
            try:
                rec = bridge.cmd_dispatch(msg, name)
                self._spawn(name)
                st, b = _json(rec)
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine":
            try:
                st, b = _json(self._engine_set(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine/profiles":
            try:
                st, b = _json(self._profiles_act(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine/models":
            try:
                st, b = _json(self._engine_models(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/engine/test":
            try:
                st, b = _json(self._engine_test(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/perms":
            try:
                return self._send(*_json(self._perms_set(body)))
            except Exception as e:  # noqa: BLE001
                return self._send(
                    *_json({"ok": False, "error": str(e)}, 500)
                )

        if path == "/api/guard":
            try:
                return self._send(*_json(self._guard_set(body)))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/test":
            try:
                import guard

                ok, msg = guard.send_code_email(
                    "这是一封测试邮件，不需要做任何事",
                    purpose="测试安全邮箱能不能收到",
                )
                return self._send(*_json({"ok": ok, "message": msg}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/code":
            try:
                import guard

                task = self._str_field(body, "task")
                purpose = self._str_field(body, "purpose")
                if not task:
                    return self._send(400, b'{"ok":false,"error":"task is required"}')
                if not guard.is_guard_ready():
                    return self._send(*_json({
                        "ok": False,
                        "message": "安全邮箱没配好。让用户去桌面端 设置 → 危险操作验证 里填。",
                    }))
                ok, msg = guard.send_code_email(task, purpose=purpose)
                return self._send(*_json({"ok": ok, "message": msg}, 200 if ok else 400))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/check":
            try:
                import guard

                task = self._str_field(body, "task")
                if not task:
                    return self._send(400, b'{"ok":false,"error":"task is required"}')
                r = guard.check(task)
                return self._send(*_json({"ok": True, **r}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/selftest":
            # 自检接口：在服务进程内跑一遍 guard 的完整状态机。
            #
            # 为什么需要它：
            #    guard 的状态在**服务进程的内存里**。
            #    测试脚本 import 的是另一份，两边内存不通——
            #    实测过好几次"测试说没锁、服务说锁着"，
            #    就是两份状态在打架。
            #
            #    这个接口让测试跑在**服务自己的那份状态里**，
            #    测完自己清理干净。
            #
            # 安全：
            #   · 需要 token（跟其他接口一样）
            #   · 只在本机
            #   · 只做自检，不接受"把某个任务设成已通过"这种参数
            #     （那才是真后门）
            try:
                return self._send(*_json(self._guard_selftest()))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/guard/verify":
            try:
                import guard

                task = self._str_field(body, "task")
                code = self._str_field(body, "code")
                ok, msg = guard.verify(task, code)
                return self._send(*_json({"ok": ok, "message": msg}))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/autostart":
            try:
                import paths

                ok, msg = paths.set_autostart(bool(body.get("on")))
                return self._send(*_json({
                    "ok": ok,
                    "enabled": paths.is_autostart(),
                    "note": msg,
                }))
            except Exception as e:  # noqa: BLE001
                return self._send(*_json({"ok": False, "error": str(e)}, 500))

        if path == "/api/paths":
            try:
                st, b = _json(self._paths_set(body))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/start":
            target = self._str_field(body, "target").strip()
            try:
                st, b = _json(self._start(target))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/stop":
            target = (body.get("target") or "").strip()
            try:
                st, b = _json(self._stop(target))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/restart":
            target = (body.get("target") or "").strip()
            try:
                st, b = _json(self._restart(target))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/model":
            preset = (body.get("preset") or "").strip()
            effort = (body.get("effort") or "").strip()
            try:
                import tune_model

                rec = tune_model.apply(preset, effort)
                st, b = _json(rec, 200 if rec.get("status") == "ok" else 400)
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        if path == "/api/cancel":
            name = (body.get("name") or "").strip()
            if not name:
                return self._send(400, b'{"error":"name is required"}')
            try:
                st, b = _json(bridge.cmd_cancel(name))
            except Exception as e:  # noqa: BLE001
                st, b = _json({"error": f"{type(e).__name__}: {e}"}, 500)
            return self._send(st, b)

        return self._send(404, b'{"error":"not found"}')

    # ── 具体处理 ──

    def _windows(self, path: str):
        """列出某套配置下面的所有窗口。

        前台（QQ/飞书那边的丫头）拿这个判断"这个活该不该放进已有窗口"。
        判断逻辑在前台，这里只负责把事实给她：
            · 窗口叫什么
            · 它开头那段需求是什么（方便她对照上下文）
            · 现在什么状态

        ?profile=xxx 只看那套；不给就看全部。
        """
        from urllib.parse import parse_qs, urlparse

        q = parse_qs(urlparse(path).query)
        want = (q.get("profile") or [""])[0]

        tasks = bridge._read_tasks()
        out = []
        for t in tasks:
            pid = t.get("profile") or ""
            if want and pid != want:
                continue
            out.append({
                "name": t.get("id") or "",
                "profile": pid,
                "status": t.get("status") or "",
                "task": (t.get("task") or "")[:300],
                "created_at": t.get("created_at") or "",
                "finished_at": t.get("finished_at") or "",
            })

        # 最近动过的排前面
        out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return self._send(*_json({"ok": True, "windows": out}))

    def _tasks(self):
        """任务列表：把 state 和 tasks.jsonl 合起来给前端。

        state 里有花费数据，tasks.jsonl 里有完整记录，两边合一下。
        """
        try:
            states = {s.get("id"): s for s in bridge.list_states()}
            tasks = bridge._read_tasks()
            out = []
            for t in tasks:
                tid = t.get("id", "")
                s = states.get(tid) or {}
                out.append({
                    "name": tid,
                    "status": s.get("status") or t.get("status"),
                    "task": t.get("task") or "",
                    "seconds": s.get("seconds") or t.get("seconds"),
                    "started_at": t.get("started_at"),
                    "finished_at": t.get("finished_at"),
                    "updated_at": s.get("updated_at") or t.get("updated_at"),
                    # 花费：只有跑过的任务才有
                    "context_tokens": s.get("context_tokens"),
                    "cost_cny": s.get("cost_cny"),
                    "result": s.get("result") or "",
                    "error": s.get("error") or "",
                })
            # 最近更新的排前面
            out.sort(key=lambda x: x.get("updated_at") or "", reverse=True)
            self._send(*_json(out))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _task(self, name: str):
        try:
            s = bridge.read_state(name)
            if not s:
                return self._send(*_json({"error": "not found"}, 404))
            self._send(*_json(s))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _task_log(self, name: str):
        """这个任务的历史轮次。

        最后一轮在 state 里，完整历史在 tasks.jsonl。
        这里给的是 tasks.jsonl 里这个任务的那条（含最新一次结果）。
        """
        try:
            tasks = bridge._read_tasks()
            rec = next((t for t in tasks if t.get("id") == name), None)
            if not rec:
                return self._send(*_json({"error": "not found"}, 404))
            self._send(*_json(rec))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _dispatch_with_concurrency(self, task: str, name: str,
                                   profile: str) -> dict:
        """派活，并按这套配置的并发额度决定立刻跑还是排队。

        为什么要排队而不是拒绝：
            用户派完就关窗口了，不会回来看"哎呀满了要重试"。
            排着队，等有空位后台自己会拉起来。

        状态用 queued，跟 pending 的区别：
            pending  刚派，还没轮到（通常马上就跑）
            queued   这套满了，明确在等
        """
        import engine.profiles as P
        import engine.scheduler as S

        # ⚠️ 顺序很重要：先看有没有空位，再派活。
        #
        #    原来反了：先 cmd_dispatch 写进去（状态 pending），
        #    再检查 has_slot。而 pending 是算占位的，
        #    等于"我自己刚占了一个位子，然后检查还有没有空位"——
        #    conc=1 的配置永远显示"满了"，第一个任务就排队，再也不会跑。
        #
        #    所以先定好用哪套、看它现在几个占位、还有没有位置，
        #    最后才真正写进去。
        pid = (profile or "").strip()
        if not pid:
            pid = (P.default_profile() or {}).get("id") or ""

        prof = P.get_profile(pid) if pid else {}
        limit = int((prof or {}).get("concurrency") or 1)

        has_room = S.has_slot(pid, limit) if pid else True

        rec = bridge.cmd_dispatch(task, name, pid)

        if has_room:
            self._spawn(name)
            return rec

        # 满了：标成排队
        bridge._update_task(name, status="queued")
        bridge.write_state(name, status="queued", profile=pid)
        rec["status"] = "queued"
        rec["queue_position"] = S.queue_position(pid, name)
        rec["queue_total"] = limit
        return rec

    def _profiles_get(self):
        """把全部模型配置交给界面。

        key 不回传原文，只给尾号——界面上不需要看到完整 key。
        """
        from engine.profiles import load_all

        data = load_all()
        out = []
        for prof in data.get("profiles") or []:
            key = prof.get("api_key") or ""
            out.append({
                "id": prof.get("id"),
                "name": prof.get("name"),
                "base_url": prof.get("base_url"),
                "model": prof.get("model"),
                "has_key": bool(key),
                "key_tail": key[-4:] if len(key) >= 4 else "",
                "max_steps": prof.get("max_steps") or 40,
                "enabled": prof.get("enabled", True),
                "concurrency": prof.get("concurrency", 1),
            })
        return self._send(*_json({
            "ok": True,
            "active": data.get("active") or "",
            "profiles": out,
        }))

    def _profiles_act(self, body: dict) -> dict:
        """加、改、删、切换模型配置。

        action 可以是：
            add     新增一套（需要 name / base_url / api_key / model）
            update  改一套（需要 id，其余字段可选）
            delete  删一套（需要 id）
            switch  切换到某套（需要 id）
        """
        from engine.profiles import (
            add_profile, delete_profile, set_active, update_profile,
        )

        action = (body.get("action") or "").strip()

        if action == "add":
            prof = add_profile(body)
            return {"ok": True, "id": prof["id"]}

        if action == "update":
            pid = (body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "没说要改哪一套"}
            return {"ok": update_profile(pid, body)}

        if action == "delete":
            pid = (body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "没说要删哪一套"}
            return {"ok": delete_profile(pid)}

        if action == "switch":
            pid = (body.get("id") or "").strip()
            if not pid:
                return {"ok": False, "error": "没说要切到哪一套"}
            return {"ok": set_active(pid)}

        return {"ok": False, "error": f"不认识的动作：{action}"}

    def _engine_get(self):
        """任务端现在用哪个模型。

        key 不回传完整值，只回一个"配没配"和尾号——
        界面上不需要看到完整的 key，也不该看到。
        """
        try:
            import engine_bridge

            c = engine_bridge.load_config()
            key = c.get("api_key") or ""
            return self._send(*_json({
                "ok": True,
                "base_url": c.get("base_url") or "",
                "model": c.get("model") or "",
                "max_steps": c.get("max_steps") or 40,
                "has_key": bool(key),
                "key_tail": (key[-4:] if len(key) >= 4 else ""),
                "configured": engine_bridge.is_configured(),
                "config_path": engine_bridge.config_path(),
            }))
        except Exception as e:  # noqa: BLE001
            return self._send(*_json({"ok": False, "error": str(e)}, 500))

    def _engine_models(self, body: dict):
        """列出这个接口上可用的模型，给界面做下拉框。

        用 POST + JSON 传参，不用 query string。
        踩过：把 base_url 塞在 URL 里，里面的 "//" 在解析时丢了，
        结果一直读到已保存的旧地址，查出来的是另一家的模型列表。
        JSON 里传字符串没有这种坑。

        key 留空就用配置里存的——用户只改地址时不用重输。
        """
        from engine.models import list_models, pick_default

        base = (body.get("base_url") or "").strip()
        key = (body.get("api_key") or "").strip()

        import engine_bridge

        cur = engine_bridge.load_config()
        base = base or cur.get("base_url") or ""
        key = key or cur.get("api_key") or ""

        ok, models, err = list_models(base, key)
        if not ok:
            return self._send(*_json({"ok": False, "error": err}))

        return self._send(*_json({
            "ok": True,
            "models": models,
            "default": pick_default(models),
        }))

    def _engine_set(self, body: dict) -> dict:
        """保存模型配置。

        如果没传 api_key（界面上留空表示"不改"），
        就保留原来的 key——不能让用户为了改个地址把 key 重打一遍。
        """
        import engine_bridge

        cur = engine_bridge.load_config()
        base = (body.get("base_url") or "").strip()
        model = (body.get("model") or "").strip()
        key = (body.get("api_key") or "").strip()

        cur["base_url"] = base
        cur["model"] = model
        if key:
            cur["api_key"] = key
        try:
            cur["max_steps"] = int(body.get("max_steps") or cur.get("max_steps") or 40)
        except (TypeError, ValueError):
            cur["max_steps"] = 40

        engine_bridge.save_config(cur)
        return {
            "ok": True,
            "configured": engine_bridge.is_configured(),
        }

    def _engine_test(self, body: dict) -> dict:
        """真的发一次请求，确认这套配置能用。

        为什么要在界面上放这个：
            用户填完不知道自己填对没有。等派了一个活失败了才
            发现地址写错，那就太晚了。
        """
        import engine_bridge
        from engine import test_connection

        cur = engine_bridge.load_config()
        base = (body.get("base_url") or cur.get("base_url") or "").strip()
        model = (body.get("model") or cur.get("model") or "").strip()
        key = (body.get("api_key") or cur.get("api_key") or "").strip()

        if not base or not model or not key:
            return {"ok": False, "error": "三样都得填：接口地址、Key、模型名"}

        ok, msg = test_connection(base, key, model)
        return {"ok": ok, "message": msg}

    def _guard_set(self, body: dict) -> dict:
        """保存安全邮箱配置。

        ⚠️ 授权码不回传。
            传空表示"不改"——用户不想为了改个邮箱把授权码重打一遍。
        """
        import guard

        cur = guard.load_config()
        for k in ("email", "smtp_host", "smtp_user"):
            v = self._str_field(body, k)
            if v:
                cur[k] = v.strip()

        port = body.get("smtp_port")
        if isinstance(port, (int, str)) and str(port).strip():
            try:
                cur["smtp_port"] = int(port)
            except (TypeError, ValueError):
                pass

        # 授权码：空 = 不改
        pw = self._str_field(body, "smtp_pass")
        if pw and pw.strip():
            cur["smtp_pass"] = pw.strip()

        cur["enabled"] = bool(body.get("enabled", True))
        guard.save_config(cur)

        return {
            "ok": True,
            "ready": guard.is_guard_ready(),
            "email_masked": guard._mask_email(cur.get("email") or ""),
        }

    def _guard_selftest(self) -> dict:
        """在服务进程里跑一遍 guard 的完整状态机。

        为什么在这里跑而不是在外面跑：
            guard 的状态在进程内存里。测试脚本 import 的是另一份，
            两边不通——实测过好几次"测试说没锁、服务说锁着"。

            这个函数跑在服务自己的进程里，用的是同一份状态。

        会做什么：
            塞临时的 pending / 锁，验证状态机对不对，然后清干净。
            **不会发真邮件**（`send_code_email` 会发信，这里绕开它，
            直接构造 PendingCode）。

        清理是 finally 保证的——即使断言中途抛了，也会清干净。
        这一点很要紧：围栏没清的话，后面所有派活都会被挡，
        用户会以为程序坏了。
        """
        import time as _t

        import guard

        results = []

        def ck(desc: str, got, want) -> None:
            ok = (got in want) if isinstance(want, (tuple, list)) else (got == want)
            results.append({"desc": desc, "ok": ok, "got": got, "want": want})

        def reset() -> None:
            guard._state.pending.clear()
            guard._state.locked_until.clear()
            guard._state.fails.clear()
            guard._locked.clear()
            guard._force_locked.clear()
            guard._verified.clear()

        try:
            reset()

            # ── A. 平时 ──
            r = guard.check("把按钮改大")
            ck("平时 fenced=False", r["fenced"], False)
            ck("平时可以派", r["verified"], True)

            # ── B. 发码 → 全站锁 ──
            danger = "__selftest__ 删掉用户表"
            fp = guard._fingerprint(danger)
            guard._state.pending[fp] = guard.PendingCode(
                code="123456", task=danger, purpose="", expires=_t.time() + 120
            )
            r = guard.check(danger)
            ck("危险任务被锁", r["fenced"], True)
            ck("在等用户填", r["waiting"], True)
            ck("可以重发码", r["can_resend"], True)

            r = guard.check("把按钮改大")
            ck("★ 普通任务也被锁", r["fenced"], True)

            # ── C. 验证通过 → 解锁 ──
            ok, _ = guard.verify(danger, "123456")
            ck("验证通过", ok, True)
            r = guard.check(danger)
            ck("危险任务解锁", r["fenced"], False)
            r = guard.check("把按钮改大")
            ck("★ 普通任务解锁", r["fenced"], False)

            # 一次性
            ok2, _ = guard.verify(danger, "123456")
            ck("验证码不能复用", ok2, False)

            # ── D. 过期 → 锁 10 分钟 ──
            reset()
            guard._state.pending[fp] = guard.PendingCode(
                code="111111", task=danger, purpose="", expires=_t.time() - 1
            )
            r = guard.check(danger)
            ck("过期后仍锁", r["fenced"], True)
            ck("锁约 600 秒", 580 < r["lock_left"] <= 600, True)
            ck("普通过期可以重发", r["can_resend"], True)

            # ── E. 错三次 → 强制锁 ──
            reset()
            guard._state.pending[fp] = guard.PendingCode(
                code="222222", task=danger, purpose="", expires=_t.time() + 120
            )
            o1, m1 = guard.verify(danger, "000000")
            o2, m2 = guard.verify(danger, "000000")
            o3, m3 = guard.verify(danger, "000000")
            ck("第1次错被拒", o1, False)
            ck("提示还剩2次", "2" in m1, True)
            ck("第2次错被拒", o2, False)
            ck("提示还剩1次", "1" in m2, True)
            ck("第3次错被拒", o3, False)

            r = guard.check(danger)
            ck("错三次后强制锁", r["forced"], True)
            ck("强制锁不许重发", r["can_resend"], False)

            ok4, msg4 = guard.send_code_email(danger, "")
            ck("强制锁期间重发被拒", ok4, False)

            r = guard.check("普通任务")
            ck("强制锁期间普通任务也锁", r["fenced"], True)

            # ── F. 锁满 → 自动解 ──
            for k in list(guard._force_locked.keys()):
                guard._force_locked[k] = _t.time() - 1
            for k in list(guard._locked.keys()):
                guard._locked[k] = _t.time() - 1
            guard._state.locked_until.clear()
            r = guard.check("普通任务")
            ck("锁满自动解", r["fenced"], False)
            ck("可以重发码了", r["can_resend"], True)

        finally:
            # 不管上面成没成，都清干净
            reset()

        passed = sum(1 for x in results if x["ok"])
        return {
            "ok": passed == len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "results": results,
        }

    # ── 权限 ──
    def _perms_get(self) -> dict:
        """列出每个窗口的权限。"""
        import task_perms as tp

        cfg = tp.load()
        tasks = []
        try:
            import bridge as _b

            for t in _b._read_tasks():
                nm = t.get("id") or ""
                if not nm:
                    continue
                tasks.append({
                    "name": nm,
                    "status": t.get("status"),
                    "level": tp.get_level(nm),
                    # 加 profile：前端要按模型分组显示。
                    # 原来没这个字段，前台只能给一张扁平表，
                    # 分不清哪个窗口跑在哪套模型上。
                    "profile": t.get("profile") or "",
                })
        except Exception:  # noqa: BLE001
            pass

        tasks.sort(key=lambda x: str(x["name"]))

        return {
            "ok": True,
            "levels": list(tp.LEVELS),
            "labels": tp.LEVEL_LABEL,
            "hints": tp.LEVEL_HINT,
            "default": "workspace",
            "tasks": tasks,
            "custom": cfg,
        }

    def _perms_set(self, body: dict) -> dict:
        """改一个窗口的权限。

        body:
            {"name": "任务名", "level": "workspace|home|full", "code": "可选"}

        ⚠️ **升权必须过验证码。**

            为什么：
                权限档位是攻击者唯一需要拿下的那一个点。

                如果他能在对话里直接说"帮我把那个窗口的权限调一下"，
                那整套围栏就白做了——他不需要绕过围栏，
                他只要让前台把围栏拆了。而且这个请求会伪装成
                一次完全正常的远程办公操作。

            分两种情况：
                降权（收窄）  直接改。因为它只会让人更安全。
                升权（放宽）  必须先过码，一次一码。

            码绑的是"给『某个任务名』开『某个级别』"这一件事，
            不能拿去干别的，也不能换个任务名复用。
        """
        import task_perms as tp

        name = (body.get("name") or "").strip()
        level = (body.get("level") or "").strip().lower()
        code = (body.get("code") or "").strip()

        if not name:
            return {"ok": False, "error": "没说是哪个窗口"}
        if level not in tp.LEVELS:
            return {"ok": False, "error": f"不认识的权限级别：{level}"}

        # 只能改点名的**一个**窗口。
        #
        # 通配符、批量，一律不认——
        # "一次只开一扇门"是这套设计的前提。要是能一次开一片，
        # 那攻击者只要成功一次就等于全开了。
        if any(ch in name for ch in ("*", "?", "%")):
            return {
                "ok": False,
                "error": "一次只能改一个窗口，不能批量",
            }

        cur = tp.get_level(name)
        rank = {"workspace": 0, "home": 1, "full": 2}
        widening = rank.get(level, 0) > rank.get(cur, 0)

        purpose = f"perm:{name}:{level}"

        if widening:
            # 升权：必须有码
            try:
                import guard
            except ImportError:
                return {"ok": False, "error": "guard 模块不可用，没法验证"}

            if not code:
                # 第一次来：发码
                ok, msg = guard.send_code_email(purpose, "改权限")
                return {
                    "ok": False,
                    "need_code": True,
                    "name": name,
                    "from": cur,
                    "to": level,
                    "message": msg if ok else f"发码失败：{msg}",
                }

            # 带码来了：验
            ok, msg = guard.verify(purpose, code)
            if not ok:
                return {
                    "ok": False,
                    "need_code": True,
                    "error": msg,
                }

        if not tp.set_level(name, level):
            return {"ok": False, "error": "没改成功"}

        return {
            "ok": True,
            "name": name,
            "from": cur,
            "level": level,
            "label": tp.LEVEL_LABEL.get(level, level),
            "hint": tp.LEVEL_HINT.get(level, ""),
            "verified": widening,
        }

    def _paths_get(self):
        """把路径配置交给设置页。

        返回两组：
            values  当前用的值（用户配过的或探测到的）
            detected 这一次探测到的（给"重新探测"按钮用）
        """
        import paths

        data = paths.load()
        # 空着的字段顺手补一下探测结果，但先不落盘——
        # 用户点"保存"才算数，免得在别的盘留下痕迹
        filled = paths.merge_detected(data)
        return self._send(*_json({
            "ok": True,
            "values": filled,
            "detected": paths.detect_all(),
            "missing": paths.missing(filled),
            "labels": paths.LABELS,
            "config_path": paths.config_path(),
        }))

    def _paths_set(self, body: dict) -> dict:
        """保存路径配置。只接受认识的键。"""
        import paths

        data = paths.load()
        for k in paths.DEFAULTS:
            if k in body and isinstance(body[k], str):
                data[k] = body[k].strip()
        paths.save(data)

        # 保存完让 launcher 重新读一遍
        global _launcher
        if _launcher is not None:
            try:
                _launcher.refresh_paths()
            except Exception:  # noqa: BLE001
                pass

        return {"ok": True, "missing": paths.missing(data)}

    def _detect_paths(self):
        """重新探测一遍。"""
        import paths

        found = paths.detect_all()
        return {"ok": True, "detected": found}

    def _qrcode(self):
        """把 NapCat 生成的登录二维码给桌面端。

        为什么需要这个：
            QQ 重启后如果登录态丢了，NapCat 会在 QQ 窗口里画二维码等人扫。
            但那个窗口在后台、或者干脆没显出来，用户根本看不见。
            实测过：用户看到的是"重启中…"，其实程序在等扫码，
            两边都僵在那儿。

            NapCat 会把二维码落盘到 cache/qrcode.png，
            所以直接读这个文件交给界面显示（base64），
            用户在自己屏幕上就扫得到。
        """
        import base64
        import glob

        # 二维码可能在不同版本的目录下，找最新的一张
        patterns = _qrcode_patterns()
        files = []
        for pat in patterns:
            files.extend(glob.glob(pat))
        if not files:
            return self._send(*_json({"ok": False, "error": "还没有二维码"}))

        newest = max(files, key=os.path.getmtime)
        age = time.time() - os.path.getmtime(newest)
        # QQ 的登录码大约 3 分钟有效。留 15 秒余量，
        # 免得用户刚看到"还剩 3 秒"就过期了。
        ttl = 180
        left = max(0, ttl - age)
        if left <= 5:
            return self._send(*_json({
                "ok": False,
                "error": "二维码已经过期了",
                "left": 0,
            }))

        try:
            raw = open(newest, "rb").read()
        except OSError as e:
            return self._send(*_json({"ok": False, "error": f"读不了二维码：{e}"}))

        return self._send(*_json({
            "ok": True,
            "png": base64.b64encode(raw).decode("ascii"),
            "age": int(age),
            "left": int(left),      # 还剩多少秒
            "ttl": ttl,
        }))

    @staticmethod
    def _clear_qrcode() -> None:
        """登录成功后把二维码文件删掉。

        留在那儿有两个问题：
            · 界面下次打开还显示一张已经没用的码
            · 那个文件里有这次登录的临时凭证
        """
        import glob

        for pat in _qrcode_patterns():
            for f in glob.glob(pat):
                try:
                    os.remove(f)
                except OSError:
                    pass

    def _model_get(self):
        """当前任务端用的模型。设置页开的时候要看。"""
        try:
            import tune_model

            cur = tune_model.get_current()
            return self._send(*_json({
                "preset": cur.get("preset") if cur.get("preset") not in (None, "(未知组合)") else "",
                "provider_now": cur.get("provider"),
                "model_now": cur.get("model"),
                "effort_now": cur.get("reasoning_effort"),
            }))
        except Exception as e:  # noqa: BLE001
            return self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    def _ensure_host(self) -> dict:
        """确保宿主（AstrBot）在跑。两条通道都要它。"""
        if port_open(ASTRBOT_PORT, timeout=1):
            return {"ok": True}

        if _launcher is None:
            return {"ok": False, "error": "启动模块没加载上"}

        ok, msg = _launcher.start_bot()
        if not ok:
            return {"ok": False, "error": msg}

        import time as _t

        for _ in range(100):
            _t.sleep(1)
            if port_open(ASTRBOT_PORT, timeout=0.5):
                # 端口通了还不够，插件的启停接口也要就绪
                try:
                    from desktop import channels as _ch

                    _ch.wait_ready(timeout=60)
                except Exception:  # noqa: BLE001
                    pass
                return {"ok": True}

        return {"ok": False, "error": "对话服务没起来，可能启动报错了。"}

    def _set_channel(self, platform_id: str, on: bool) -> dict:
        """在 AstrBot 里打开或关掉一条通道（热操作，不重启）。"""
        try:
            from desktop import channels as _ch

            if on:
                return _ch.enable(platform_id)
            return _ch.disable(platform_id)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _ensure_channel(self, platform_id: str) -> dict:
        """确保某条通道是开着的。"""
        r = self._set_channel(platform_id, True)
        return r

    def _start_feishu(self) -> dict:
        """只开飞书：起宿主 + 关掉 QQ 平台。"""
        host = self._ensure_host()
        if not host.get("ok"):
            return host

        # 打开飞书
        r = self._set_channel("feishu", True)
        if not r.get("ok"):
            return {"ok": False, "error": f"打开飞书通道失败：{r.get('error')}"}

        # 顺手把 QQ 平台关掉——用户点的是"只开飞书"。
        # 注意只关连接，不杀 QQ 客户端：
        # 那是用户自己的 QQ，随便杀会让他正在用的微信/QQ 全断。
        self._set_channel("qq-napcat", False)

        return {"ok": True, "note": "飞书通道开了（没带 QQ，省内存）。"}

    def _start(self, target: str) -> dict:
        """启动一个服务。全程不弹任何窗口。

        为什么要等端口：
            光看进程起来了不算数。QQ 起来了但 NapCat 没注入进去、
            AstrBot 起来了但适配器没连上，都会卡在半路，
            用户看到"已启动"却发现用不了。
            所以这里一直等到端口真的通（或超时）再回话。
        """
        if _launcher is None:
            return {"ok": False, "error": "启动模块没加载上"}

        if target == "qq":
            # 先看 3000 通不通。通了才是真登录好了。
            if port_open(QQ_PORT, timeout=1):
                self._clear_qrcode()
                return {"ok": True, "note": "QQ 已经在跑了。"}

            # 进程在、但端口不通 → 起了没登录，或者注入失败。
            # 这种情况不要重开进程，直接让界面去扫码。
            try:
                import subprocess as _sp

                cnt = _sp.run(
                    ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                    capture_output=True, timeout=10,
                ).stdout.decode("utf-8", "replace").count("QQ.exe")
            except Exception:  # noqa: BLE001
                cnt = 0

            if cnt > 0:
                return {
                    "ok": True,
                    "need_login": True,
                    "note": "QQ 开着但没登录，扫一下码。",
                }

            ok, msg = _launcher.start_qq()
            if not ok:
                return {"ok": False, "error": msg}
            if port_open(QQ_PORT, timeout=1):
                # NapCat 通了，再把 AstrBot 里这条连接打开
                self._ensure_channel("qq-napcat")
                return {"ok": True, "note": "QQ 已经起来了。"}
            # 等最多 90 秒
            import time as _t

            for _ in range(90):
                _t.sleep(1)
                if port_open(QQ_PORT, timeout=0.5):
                    # 起来了，把通道打开、飞书关掉
                    self._ensure_host()
                    self._set_channel("qq-napcat", True)
                    self._set_channel("feishu", False)
                    return {"ok": True, "note": "QQ 起来了，NapCat 也接上了。"}
            # 要扫码 —— 但通道的事也得处理完。
            # 踩过：这里提前 return，飞书那条没关掉，
            # 用户点了「启动 QQ」，结果两个都开着。
            self._ensure_host()
            self._set_channel("qq-napcat", True)
            self._set_channel("feishu", False)
            return {
                "ok": True,
                "need_login": True,
                "note": "QQ 起来了，但还要扫码登录。",
            }

        if target == "feishu":
            # 飞书通道很轻：只需要宿主在跑，然后把这条连接打开。
            #
            # 跟原来不同的是：不再顺带把 QQ 也拉起来。
            # 只用飞书的人不该背着 QQ 客户端那几百兆。
            return self._start_feishu()

        if target == "bot":
            # 老接口名留着，转给 feishu
            return self._start_feishu()

        if target == "bridge":
            return {"ok": True, "note": "任务端不用单独启动，派活时自己会起。"}

        return {"ok": False, "error": f"不认识的服务：{target}"}

    def _stop(self, target: str) -> dict:
        if _launcher is None:
            return {"ok": False, "error": "启动模块没加载上"}

        if target == "qq":
            ok, msg = _launcher.stop_qq()
            return {"ok": ok, "note": msg}
        if target == "bot":
            ok, msg = _launcher.stop_bot()
            return {"ok": ok, "note": msg}
        if target == "bridge":
            return {"ok": True, "note": "任务端没有常驻进程。"}
        return {"ok": False, "error": f"不认识的服务：{target}"}

    def _restart(self, target: str) -> dict:
        """重启某个服务。

        为什么放在这里而不是让用户自己去开窗口：
            不懂电脑的人找不到那个黑窗口，也不知道该按什么顺序启动。

        重启 QQ 要走完整的关闭 + 重新注入流程，
        不能直接杀——杀了之后 NapCat 就注入不进去了。
        """
        import subprocess

        if target == "qq":
            # 重启 QQ 比看起来麻烦，这里踩过三个坑：
            #
            #   1) 只 taskkill QQ.exe 收不干净。
            #      QQ 会拉一串子进程（实测十几个），父进程没了，
            #      子进程还攥着内存共享段，NapCat 就注入不进去。
            #      必须先杀掉所有 QQ 相关进程，等它们真的消失。
            #
            #   2) 杀完立刻拉起会失败。要轮询确认进程数归零再启动，
            #      固定 sleep 2 秒不够。
            #
            #   3) 不能直接返回"正在重启"就完事。
            #      用户点完按钮得知道到底成没成，所以这里等
            #      3000 端口真的起来（或超时）再回话。
            import paths as _p

            nap = _p.get("napcat_dir")
            launcher = os.path.join(nap, "NapCatWinBootMain.exe") if nap else ""
            if not launcher or not os.path.exists(launcher):
                return {"ok": False, "error": "还没设置 NapCat 在哪，去设置页选一下"}

            def _qq_count() -> int:
                try:
                    out = subprocess.run(
                        ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/NH"],
                        capture_output=True, timeout=15,
                    ).stdout.decode("utf-8", "replace")
                except Exception:  # noqa: BLE001
                    return 0
                return out.count("QQ.exe")

            # 1) 杀掉所有 QQ 进程
            try:
                subprocess.run(["taskkill", "/F", "/T", "/IM", "QQ.exe"],
                               capture_output=True, timeout=25)
            except Exception:  # noqa: BLE001
                pass

            # 2) 等进程真的走干净（最多 20 秒）
            gone = False
            for _ in range(40):
                time.sleep(0.5)
                if _qq_count() == 0:
                    gone = True
                    break
            if not gone:
                return {
                    "ok": False,
                    "error": "QQ 关不掉，可能还有窗口没退。手动关掉 QQ 再试一次。",
                }

            # 3) 拉起来
            subprocess.Popen(
                ["cmd", "/c", launcher],
                cwd=os.path.dirname(launcher),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

            # 4) 等 3000 端口起来（最多 90 秒）
            for _ in range(90):
                time.sleep(1)
                if _port_ok(_NAPCAT_PORT):
                    # 登录成功，二维码没用了，顺手删掉
                    self._clear_qrcode()
                    return {"ok": True, "note": "QQ 已经起来了，登录也接上了。"}
            return {
                "ok": False,
                "error": "QQ 进程起来了，但 NapCat 没接上（3000 端口不通）。"
                         "可能要在 QQ 窗口里手动登录一下。",
            }

        if target == "bot":
            ports = _listening_pids(_BOT_PORT)
            for pid in ports:
                try:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=20)
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(2)
            if _launcher is None:
                return {"ok": False, "error": "启动模块没加载上"}
            ok, msg = _launcher.stop_bot()
            ok2, msg2 = _launcher.start_bot()
            if not ok2:
                return {"ok": False, "error": msg2}
            return {"ok": True, "note": msg2}
            subprocess.Popen(["cmd", "/c", "start", "", bat],
                             cwd=os.path.dirname(bat), shell=False)
            return {"ok": True, "note": "对话服务正在重启，大约 30 秒。"}

        if target == "bridge":
            return {"ok": True, "note": "任务端是每次派活时新起的，不用重启。"}

        return {"ok": False, "error": f"不认识的服务：{target}"}

    def _outbox(self):
        try:
            rows = []
            if os.path.exists(bridge.OUTBOX):
                for line in open(bridge.OUTBOX, encoding="utf-8"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not r.get("delivered"):
                        rows.append(r)
            self._send(*_json(rows[-50:]))
        except Exception as e:  # noqa: BLE001
            self._send(*_json({"error": f"{type(e).__name__}: {e}"}, 500))

    @staticmethod
    def _spawn(name: str) -> None:
        """起一个后台进程真干活，不等它。"""
        import subprocess

        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        try:
            subprocess.Popen(
                [sys.executable, os.path.join(BRIDGE_DIR, "bridge.py"),
                 "run", "--id", name, "--timeout", "1800"],
                cwd=BRIDGE_DIR, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    global _API_TOKEN

    import security as _sec

    ap = argparse.ArgumentParser(description="桥的 HTTP 接口")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    # ⚠️ 没有 --host 参数了。
    #
    #    原来可以传 --host 0.0.0.0 把接口开给整个局域网，
    #    等于把"能派活、能读写文件、能跑命令"的手伸到网上。
    #    这个程序是给本机用的，地址写死。
    args = ap.parse_args()

    _API_TOKEN = _sec.load_or_create_token("dsh-bridge")

    # 先把"上辈子"留下的烂摊子收拾掉。
    #
    # ⚠️ 为什么要做这个：
    #     进程被强杀、机器断电、桥崩溃——这些情况下进程没了，
    #     但 tasks.jsonl 里还写着 status="running"。
    #     界面就一直转圈，用户以为它还在跑。
    #
    #     实测有一次一条任务从 10:51 挂到第二天，界面还显示"运行中"。
    #     用户看到的是"被锁的活 运行中"——那条其实是另一条已取消的任务，
    #     但底下这条真卡死的在列表里，看着就是没跑完。
    try:
        import bridge as _bridge
        import recover as _recover

        _fixed = _recover.recover(
            workspaces=os.path.join(BRIDGE_DIR, "workspaces"),
            read_tasks=_bridge._read_tasks,
            write_tasks=_bridge._write_tasks,
            now_iso=_bridge._now(),
        )
        if _fixed:
            print(f"[api] 收回 {len(_fixed)} 条卡住的任务：")
            for it in _fixed:
                print(f"       {it['id']} —— {it['why']}")
        else:
            print("[api] 没有卡住的任务")
    except Exception as e:  # noqa: BLE001
        print(f"[api] 清理卡住任务失败（不影响启动）：{type(e).__name__}: {e}")

    # 起一个后台线程，定期把排队中的任务拉起来。
    #
    # 为什么放在接口服务里而不是桥里：
    #   桥是一次性的（跑完就退出），接口服务是常驻的。
    #   排队轮转需要一个一直在的东西来做。
    try:
        import engine.scheduler as S

        S.start_pump_in_background(interval=5.0)
        print("[api] 排队轮转已启动（每 5 秒检查空位）")
    except Exception as e:  # noqa: BLE001
        print(f"[api] 排队轮转没起来：{e}")

    srv = ThreadingHTTPServer((_sec.HOST, args.port), Handler)
    print(f"[api] 桥接口已启动：http://{_sec.HOST}:{args.port}")
    print(f"[api] 只接受本机连接，请求需带 X-Token")
    print(f"[api] token 在：{_sec.token_path('dsh-bridge')}")
    print(f"[api] 任务目录：{BRIDGE_DIR}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[api] 退出")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
