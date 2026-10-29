"""桌面端的启动/重启流程 —— 重写版。

原来的毛病（用户实测出来的三条）
    1. 点「我先不管」以后按钮还卡在"处理中"
       → 因为按钮状态只等那一个 HTTP 请求回来才复位，
         而那个请求最长要等 90 秒。
    2. 二维码一直显示"正在取二维码…"
       → _on_qr 是挂在 request_once 上的，但请求本身
         在等 /api/services 的结果，链断了。
    3. 还是会弹终端窗口
       → 起 QQ 的时候只给了 NapCatWinBootMain 加 NOWINDOW，
         QQ.exe 是它拉起来的，自己带控制台。

这一版的思路
    把"启动服务"当成一个有终点的状态机，而不是一个请求：
        · 界面每 1.5 秒自己去问服务状态（不依赖长请求）
        · 按钮状态由服务状态决定，不由请求结束决定
        · 二维码独立轮询，和服务状态互不阻塞
        · 起进程时统一走 CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

API = "http://127.0.0.1:8790"


def _token() -> str:
    """读接口的访问 token。

    ⚠️ 这里漏过一次，后果很难查：
        加 token 鉴权之后，这个模块还在用裸 urlopen 请求，
        每次都是 401 → 抛异常 → 走到 except 里 emit({"api": False})。
        界面拿到的是"什么都没开"，按钮全红。

        更难受的是它偶尔能见到真状态——因为别的地方（desktop_qt 的
        Api 类）是带 token 的，两条路同时往界面送数据，
        一个说"全红"一个说"飞书开着"，看起来就是按钮在闪。

    每次现读文件，不缓存：文件和接口服务谁先起来不一定。
    """
    import os

    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    p = os.path.join(base, "dsh-bridge", "api_token.txt")
    try:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    except OSError:
        pass
    return ""


def _get(path: str, timeout: int = 4):
    """带 token 的 GET。失败返回 None。"""
    req = urllib.request.Request(API + path, headers={"X-Token": _token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

# 和 desktop_qt 保持一致
C_TEXT = "#2b2a28"
C_DIM = "#7d7a75"
C_DIM2 = "#a5a29c"
C_LINE = "#e4e3df"
C_WARN = "#c07d10"
C_OK = "#1f9c5f"
C_ACCENT = "#5b5bd6"


def font(size: int = 13, weight: int = 400) -> QFont:
    f = QFont()
    f.setFamilies(["Microsoft YaHei UI", "Segoe UI"])
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    return f


# ----------------------------------------------------------------------
# 无窗口启动
# ----------------------------------------------------------------------

_NOWINDOW = 0x08000000        # CREATE_NO_WINDOW
_NEWGROUP = 0x00000200        # CREATE_NEW_PROCESS_GROUP


def run_hidden(args, cwd=None, env=None, timeout=None):
    """起一个子进程，保证不带控制台窗口。

    ⚠️ 注意 QQ 的情况：
        NapCatWinBootMain.exe 自己是 windowless 的，
        但它会去拉 QQ.exe。QQ.exe 只要被父进程以"有控制台"的
        方式启动，就会带出一个黑框。
        所以这里多给一个 CREATE_NEW_PROCESS_GROUP，
        让子进程不继承父进程的控制台。
    """
    flags = _NOWINDOW | _NEWGROUP
    return subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


# ----------------------------------------------------------------------
# 服务状态轮询（这是整个流程的节拍器）
# ----------------------------------------------------------------------

class ServiceMonitor(QObject):
    """定时问接口要服务状态，把结果广播出去。

    为什么不"启动完等结果"：
        启动 QQ 可能要 90 秒、还要人扫码。用一个长请求去等，
        界面就没法知道中间发生了什么，按钮只能傻等着。
        改成每 1.5 秒问一次状态：
            · 按钮跟着状态走，随时能复位
            · 二维码窗口自己单独轮询，互不干扰
    """

    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy: set[str] = set()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(1500)
        QTimer.singleShot(200, self._tick)

    def _tick(self) -> None:
        # 定时器必须每次都活着。曾经因为一次异常让 timer 停了，
        # 表现就是偶尔"一闪而过"——窗口在，但再也不刷新了。
        if not self.timer.isActive():
            self.timer.start(2000)
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        """问一次状态。

        ⚠️ 失败时**什么都不要发**。
            原来失败会 emit({"api": False})，那个 dict 里
            没有 qq / feishu / bridge 三个键，界面拿到 None，
            把所有按钮判成"没开"——全变红。

            于是出现了"按钮一直闪"：
                这一轮请求成功 → 显示真实状态
                下一轮请求失败 → 全部变红
                再下一轮又成功 → 又变回来

            查这个问题时，后端 20 秒里一次都没变过，
            但界面一直在闪——因为闪的是"请求的成功失败"，
            不是服务状态。

            正确做法：拿不到就别说话。界面保留上一次的已知状态，
            比显示一个假的"全关"好得多。
        """
        try:
            d = _get("/api/services", timeout=4)
        except Exception:  # noqa: BLE001
            # 静默。不发任何状态更新。
            return

        if isinstance(d, dict) and d:
            self.changed.emit(d)


# ----------------------------------------------------------------------
# 扫码窗口：独立轮询，和启动请求完全解耦
# ----------------------------------------------------------------------

class LoginQRDialog(QDialog):
    """把 NapCat 的二维码摆到用户面前，全程自己轮询。

    ⚠️ 这里出过一个很典型的问题：
        取二维码在后台线程做，拿到数据后想更新界面，
        原来是 QTimer.singleShot(0, lambda: self._show(q))——
        看着像"交回主线程"，实际不行：
        从非主线程调 QTimer，那个计时器不会进主线程的事件循环，
        回调永远不执行。表现就是界面一直"正在取二维码…"，
        而后台其实每次都拿到了数据。

        正确做法是用 Signal：跨线程发信号，Qt 会自己把槽
        排到主线程执行。这是 Qt 里唯一可靠的线程 → 界面通道。
    """

    finished_ok = Signal()
    got_qr = Signal(object)      # dict: 二维码数据或错误
    logged_in = Signal()         # 登录成功
    stopped = Signal()           # 用户放弃了，QQ 已停

    # 用类型存临时状态，避免连字典的拷贝问题
    _last_error: str = ""


    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("QQ 需要登录")
        self.setModal(False)                      # 非模态：关了也不影响主界面
        self.setFixedSize(330, 430)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)

        title = QLabel("扫码登录 QQ")
        title.setFont(font(16, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        tip = QLabel("用手机 QQ 扫下面这个码")
        tip.setFont(font(12))
        tip.setStyleSheet(f"color:{C_DIM};")
        tip.setAlignment(Qt.AlignCenter)
        lay.addWidget(tip)

        self.img = QLabel("正在取二维码…")
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setFixedSize(230, 230)
        self.img.setStyleSheet(
            f"background:#fff; border:1px solid {C_LINE}; border-radius:10px;"
        )
        lay.addWidget(self.img, 0, Qt.AlignHCenter)

        self.status = QLabel("")
        self.status.setFont(font(11.5))
        self.status.setStyleSheet(f"color:{C_DIM2};")
        self.status.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.status)

        lay.addStretch()
        # 「不管了」= 放弃这次登录，不是"留着窗口不管"。
        # 用户点了它，就是不想继续了；那就应该把 QQ 关掉、
        # 别再有个进程半死不活地挂着等扫码。
        close = QPushButton("不管了，停掉")
        close.setObjectName("ghost")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self._give_up)
        lay.addWidget(close, 0, Qt.AlignHCenter)

        self._got = False
        self._renewing = False
        # 信号连到槽上——这是唯一的线程 → 界面通道
        self.got_qr.connect(self._show)
        self.logged_in.connect(self._ok)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(2000)
        QTimer.singleShot(100, self._tick)

    def _tick(self) -> None:
        threading.Thread(target=self._fetch, daemon=True).start()

    def _fetch(self) -> None:
        # 先看登录没登录
        try:
            d = _get("/api/services", timeout=4)
            if isinstance(d, dict) and d.get("qq"):
                self.logged_in.emit()
                return
        except Exception:  # noqa: BLE001
            pass

        # 没登录就去拿二维码
        try:
            q = _get("/api/qrcode", timeout=6)
        except Exception as e:  # noqa: BLE001
            self.got_qr.emit({"ok": False, "error": f"取不到二维码：{type(e).__name__}"})
            return

        self.got_qr.emit(q)

    def _show(self, q: dict) -> None:
        if not isinstance(q, dict) or not q.get("ok"):
            msg = (q or {}).get("error") if isinstance(q, dict) else "取不到"
            # 二维码过期是常见情况（用户磨蹭一会儿就过了）。
            # 光提示"重启一次换个新的"没用——用户不知道点哪。
            # 这里直接替他重启 QQ，自动拿一张新的。
            if msg and "过期" in str(msg) and not self._renewing:
                self._say("二维码过期了，正在换一张新的…")
                self._renew_qr()
                return
            self._say(msg or "取不到二维码")
            return

        import base64

        try:
            raw = base64.b64decode(q["png"])
            pm = QPixmap()
            if not pm.loadFromData(raw):
                self._say("二维码图片解不开")
                return
            self.img.setPixmap(pm.scaled(210, 210, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self._got = True
            # 显示倒计时，不是"已经过了多久"。
            # 用户关心的是"还来得及吗"，不是"这码多老了"。
            left = q.get("left")
            if isinstance(left, int):
                self.status.setText(f"扫完自动进去（还剩 {left} 秒）")
                if left <= 30:
                    self.status.setStyleSheet(f"color:{C_WARN};")
                else:
                    self.status.setStyleSheet(f"color:{C_DIM2};")
            else:
                self.status.setText("扫完自动进去")
        except Exception as e:  # noqa: BLE001
            self._say(f"显示失败：{type(e).__name__}")

    def _renew_qr(self) -> None:
        """重启 QQ，换一张新二维码。

        用户的处境是：码摆在面前，他刷了会儿手机回来发现过期了。
        这时候让他自己去"点启动 → 重启"绕太远，程序直接做掉。
        """
        self._renewing = True

        def run():
            try:
                import json as _json
                import urllib.request as _u

                req = _u.Request(
                    API + "/api/restart",
                    data=_json.dumps({"target": "qq"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                _u.urlopen(req, timeout=200).read()
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._renewing = False

        threading.Thread(target=run, daemon=True).start()

    def _say(self, text: str) -> None:
        self.status.setText(text)

    def _give_up(self) -> None:
        """放弃登录：停掉 QQ，让状态回到"没启动"。

        为什么不只是关窗口：
            关窗口只是没人看它了，QQ 还在后台等扫码，
            界面上的状态也还卡着。用户下次点启动又会撞上同一个半成品。
            既然说"不管了"，就干脆回到干净状态。
        """
        self.timer.stop()
        self.status.setText("正在停掉…")

        def do_stop():
            try:
                import json as _json
                import urllib.request as _u

                req = _u.Request(
                    API + "/api/stop",
                    data=_json.dumps({"target": "qq"}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                _u.urlopen(req, timeout=30).read()
            except Exception:  # noqa: BLE001
                pass
            self.stopped.emit()

        import threading

        threading.Thread(target=do_stop, daemon=True).start()
        self.close()

    def _ok(self) -> None:
        self.timer.stop()
        self.status.setText("登录成功")
        self.finished_ok.emit()
        QTimer.singleShot(500, self.close)

    def closeEvent(self, e):  # noqa: N802
        self.timer.stop()
        super().closeEvent(e)
