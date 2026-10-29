"""dsh-bridge 桌面端。

界面定位
    给不会配置的人用的一个 Windows 程序。核心诉求是"看着舒服、敢信"，
    不是"功能堆满"。

配色思路
    纯白刺眼，纯黑压抑，取中间：
    · 底色用偏暖的深灰蓝，不是死黑，长时间看不会累
    · 卡片靠明度和圆角分层，不靠粗边框
    · 主色是低饱和的紫蓝，只在真正要动手的地方出现
    · 状态色降低饱和度，做成柔和的胶囊

设计上守住的
    · 只连桥的 HTTP 接口，不碰 AstrBot —— 换掉 AstrBot 时这里一行不用改
    · 后台线程做网络请求，界面永远不卡
    · 出错说人话，不甩异常

启动
    python desktop_qt.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

API = "http://127.0.0.1:8790"

# 启动/扫码这套流程放在 desktop/svc_flow.py 里，
# 因为它和主界面是两套节奏：主界面每 3 秒刷一次任务，
# 而启动服务需要独立跟踪几秒到几十秒，混在一起容易互相拖住。
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "desktop"))
try:
    from svc_flow import LoginQRDialog, ServiceMonitor
except Exception:  # noqa: BLE001
    LoginQRDialog = None
    ServiceMonitor = None


# ────────────────────────────────────────────────────────────────────
# 配色：偏暖的深灰蓝，中间调
# ────────────────────────────────────────────────────────────────────
C_BG      = "#f6f6f4"      # 页面底（近白，带一点暖灰，不刺眼）
C_SURFACE = "#ffffff"      # 卡片（纯白，浮在底色上）
C_RAISED  = "#f0f0ed"      # 悬停 / 抬起来
C_INPUT   = "#fafaf8"      # 输入框内底

C_LINE    = "#e4e3df"      # 细边（很淡，靠明度分层不靠线）
C_LINE_HI = "#d2d0cb"      # 悬停边

C_TEXT    = "#2b2a28"      # 主文字（近黑，带暖，不用纯黑）
C_TEXT2   = "#4f4d49"      # 次要
C_DIM     = "#7d7a75"      # 描述
C_DIM2    = "#a5a29c"      # 最弱

C_OK      = "#1f9c5f"      # 绿（白底上要够深才看得清）
C_RUN     = "#2b7fd4"      # 蓝
C_BAD     = "#d64545"      # 红
C_WARN    = "#c07d10"      # 琥珀
C_ACCENT  = "#5b5bd6"      # 主强调（靛蓝，白底上稳重）
C_ACCENT2 = "#4a4ac4"      # 强调悬停
C_ACCENT3 = "#3d3da8"      # 强调按下
C_GRAD_A  = "#5b5bd6"      # 渐变色起
C_GRAD_B  = "#8b5cf6"      # 渐变色止

STATUS_TEXT = {
    "ok": "完成", "running": "运行中", "pending": "排队",
    "timeout": "超时", "error": "失败", "empty": "无输出", "cancelled": "已停",
}


def status_color(s: str) -> str:
    if s == "ok":
        return C_OK
    if s == "running":
        return C_RUN
    if s == "pending":
        return C_DIM2
    return C_BAD


def money(v) -> str:
    return f"¥{v:.2f}" if isinstance(v, (int, float)) else ""


def toks(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v/10000:.1f} 万" if v >= 10000 else f"{v:,.0f}"


def secs(v) -> str:
    if not isinstance(v, (int, float)):
        return "—"
    return f"{v:.0f} 秒" if v < 60 else f"{v/60:.1f} 分"


def font(size: int = 13, weight: int = 400) -> QFont:
    f = QFont()
    f.setFamilies(["Microsoft YaHei UI", "Segoe UI", "PingFang SC"])
    f.setPixelSize(size)
    f.setWeight(QFont.Weight(weight))
    return f


# ────────────────────────────────────────────────────────────────────
# 后台请求
# ────────────────────────────────────────────────────────────────────

def _read_api_token() -> str:
    """读桥接口的访问 token。

    桥启动时会生成它（在用户目录下的 dsh-bridge/api_token.txt），
    桌面端每次启动读一次，请求时带上。

    ⚠️ 为什么要 token：
        那个接口能派活、能读写文件、能跑命令。
        只靠"绑了 127.0.0.1"是单薄的一道墙——
        本机上任何一个程序、任何一个网页都能来调。
        加个 token，本机别的程序就算知道端口也调不动。
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


# 启动时读一次，之后每次请求会重新读。
#
# ⚠️ 为什么不能只在启动时读一次：
#     桌面端和接口服务是两个进程，谁先起来不确定。
#     用户先开桌面端、后开接口服务的时候，桌面端启动时
#     那个文件还不存在，读到空字符串 → 之后所有请求都 401。
#     表现是"界面全空，像坏了"。
#
#     文件很小（44 字节），每次读一下几乎不花时间。
_API_TOKEN_CACHE = {"t": _read_api_token()}


def _token() -> str:
    """拿 token。没有就去读，读到就缓存。"""
    if _API_TOKEN_CACHE["t"]:
        return _API_TOKEN_CACHE["t"]
    t = _read_api_token()
    if t:
        _API_TOKEN_CACHE["t"] = t
    return t


# 兼容旧引用
class _TokenProxy:
    def __str__(self) -> str:
        return _token()

    def __eq__(self, other) -> bool:
        return _token() == other

    def __bool__(self) -> bool:
        return bool(_token())


API_TOKEN = _TokenProxy()


class Api(QObject):
    # request_once 的专用通道。
    #
    # ⚠️ 这里犯过一次重复的错：
    #    request_once 跑在后台线程，拿到数据后想回主线程调 callback，
    #    一开始写的是 QTimer.singleShot(0, lambda: callback(data))。
    #    和之前扫码头那次一模一样——从非主线程调 QTimer，
    #    回调根本不会执行。表现就是设置页的路径框永远空着。
    #
    #    规律：**后台线程要碰界面，只能走 Signal。没有第二种办法。**
    _once = Signal(object)
    """后台请求。

    两种用法：
        get/post      结果走全局 done 信号，给周期性刷新用
        request_once  结果只回调一次，给"重启服务"这种一次性动作用

    ⚠️ 为什么要有 request_once：
        一开始重启服务也连的是全局 done。结果轮询刷新（每 3 秒一次）
        的返回值全灌进了重启那个回调，于是每收到一次数据就弹一个
        "没成"对话框——实测叠了 84 层模态窗，界面直接卡死。

        教训：凡是"只该发生一次"的动作，就不能挂在会反复触发的信号上。
    """

    done = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pending_cb = None
        self._once.connect(self._dispatch_once)

    def _dispatch_once(self, payload) -> None:
        """收到请求结果，调它自己的回调。"""
        data, cb = payload if isinstance(payload, tuple) else (payload, None)
        if cb:
            try:
                cb(data)
            except Exception:  # noqa: BLE001
                pass

    def get(self, path: str) -> None:
        threading.Thread(target=self._get, args=(path,), daemon=True).start()

    def _get(self, path: str) -> None:
        try:
            req = urllib.request.Request(
                API + path, headers={"X-Token": _token()}
            )
            with urllib.request.urlopen(req, timeout=6) as r:
                self.done.emit(path, json.loads(r.read()))
        except Exception:  # noqa: BLE001
            self.done.emit(path, None)

    def request_once(self, path: str, body: dict | None = None,
                     timeout: int = 20, callback=None) -> None:
        """只回调一次。不碰全局 done 信号。"""

        def run():
            try:
                if body is None:
                    req = urllib.request.Request(
                        API + path, headers={"X-Token": _token()}
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        data = json.loads(r.read())
                else:
                    req = urllib.request.Request(
                        API + path,
                        data=json.dumps(body).encode(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Token": _token(),
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=timeout) as r:
                        data = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    data = json.loads(e.read())
                except Exception:  # noqa: BLE001
                    data = {"ok": False, "error": f"HTTP {e.code}"}
            except Exception as e:  # noqa: BLE001
                data = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            if callback:
                # ⚠️ 回调必须跟着数据一起传，不能存到一个共享变量里。
                #
                #    原来写的是 self._pending_cb = callback，然后 emit。
                #    两个请求同时发出时，后一个会把前一个的回调覆盖掉，
                #    于是前一个的数据回来时调的是别人的回调——
                #    表现是"这次打开设置页什么都没有，下次又有了"。
                #    这种间歇性的 bug 最难查，因为它是竞态。
                #
                #    改成把回调放进信号参数里发出去，每个请求各回各的。
                self._once.emit((data, callback))

        threading.Thread(target=run, daemon=True).start()

    def post(self, path: str, body: dict, timeout: int = 20) -> None:
        threading.Thread(
            target=self._post, args=(path, body, timeout), daemon=True
        ).start()

    def _post(self, path: str, body: dict, timeout: int = 20) -> None:
        try:
            req = urllib.request.Request(
                API + path,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Token": _token(),
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self.done.emit(path, json.loads(r.read()))
        except urllib.error.HTTPError as e:
            try:
                self.done.emit(path, json.loads(e.read()))
            except Exception:  # noqa: BLE001
                self.done.emit(path, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.done.emit(path, {"error": f"{type(e).__name__}: {e}"})


# ────────────────────────────────────────────────────────────────────
# 自绘组件
# ────────────────────────────────────────────────────────────────────

class GlowDot(QWidget):
    """带光晕的状态点。"""

    def __init__(self, color: str = C_DIM2, size: int = 8):
        super().__init__()
        self._color = color
        self._size = size
        self.setFixedSize(size + 8, size + 8)

    def set_color(self, color: str) -> None:
        # 颜色没变就不重绘。轮询很频繁，这个判断省掉大量无谓的 update()
        if self._color == color:
            return
        self._color = color
        self.update()

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx, cy = self.width() / 2, self.height() / 2
        r = self._size / 2

        glow = QColor(self._color)
        glow.setAlpha(55)
        p.setBrush(glow)
        p.setPen(Qt.NoPen)
        p.drawEllipse(int(cx - r - 3), int(cy - r - 3), self._size + 6, self._size + 6)

        p.setBrush(QColor(self._color))
        p.drawEllipse(int(cx - r), int(cy - r), self._size, self._size)


class StatusBadge(QLabel):
    """圆角胶囊状态徽章。"""

    def __init__(self, status: str):
        super().__init__(STATUS_TEXT.get(status, status))
        c = status_color(status)
        bg = QColor(c)
        bg.setAlpha(20)
        border = QColor(c)
        border.setAlpha(70)
        self.setStyleSheet(
            f"color:{c}; background:{bg.name(QColor.HexArgb)};"
            f"border:1px solid {border.name(QColor.HexArgb)};"
            "border-radius:9px; padding:1px 9px; font-size:11px;"
        )
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)


class GradientBar(QWidget):
    """顶栏下方一条细渐变线，让界面不那么平。"""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(2)

    def paintEvent(self, _):  # noqa: N802
        p = QPainter(self)
        g = QLinearGradient(0, 0, self.width(), 0)
        c1 = QColor(C_GRAD_A)
        c2 = QColor(C_GRAD_B)
        c1.setAlpha(200)
        c2.setAlpha(90)
        g.setColorAt(0.0, c1)
        g.setColorAt(0.45, c2)
        g.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), g)


class TaskRow(QFrame):
    """任务列表的一行。

    设计上要同时满足两件事：
        1. 看着清楚：状态色条 + 名字 + 摘要 + 时间花费
        2. 刷新便宜：所有子控件都存成属性，能原地改文字，
           而不是每次销毁重建

    第 2 点不是过度优化。实测 100 行重建要 435 毫秒，
    列表三秒刷一次，界面就有一大半时间在重建控件——
    屏幕上持续闪烁，点什么都没反应。
    """

    clicked = Signal(str)

    @staticmethod
    def _badge_style(status: str) -> str:
        c = status_color(status)
        bg = QColor(c)
        bg.setAlpha(20)
        border = QColor(c)
        border.setAlpha(70)
        return (
            f"color:{c}; background:{bg.name(QColor.HexArgb)};"
            f"border:1px solid {border.name(QColor.HexArgb)};"
            "border-radius:9px; padding:1px 9px; font-size:11px;"
        )

    def __init__(self, t: dict):
        super().__init__()
        self.name = t.get("name", "")
        self.setObjectName("taskrow")
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(60)

        st = t.get("status", "")
        color = status_color(st)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 16, 0)
        root.setSpacing(0)

        # 左侧状态色条
        bar_wrap = QVBoxLayout()
        bar_wrap.setContentsMargins(8, 12, 0, 12)
        self._bar = QFrame()
        self._bar.setFixedWidth(3)
        self._bar.setStyleSheet(f"background:{color}; border-radius:1px;")
        bar_wrap.addWidget(self._bar)
        root.addLayout(bar_wrap)

        body = QVBoxLayout()
        body.setContentsMargins(12, 10, 0, 10)
        body.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)

        self._dot = GlowDot(color, 6)
        top.addWidget(self._dot, 0, Qt.AlignVCenter)

        self._name = QLabel(self.name)
        self._name.setFont(font(13, QFont.DemiBold))
        self._name.setStyleSheet(f"color:{C_TEXT};")
        top.addWidget(self._name)

        self._badge = QLabel(STATUS_TEXT.get(st, st))
        self._badge.setStyleSheet(self._badge_style(st))
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        top.addWidget(self._badge)

        top.addStretch()
        body.addLayout(top)

        self._desc = QLabel((t.get("task") or "").replace("\n", " "))
        self._desc.setFont(font(12))
        self._desc.setStyleSheet(f"color:{C_DIM};")
        self._desc.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._desc.setToolTip((t.get("task") or "")[:600])
        body.addWidget(self._desc)

        root.addLayout(body, 1)

        right = QVBoxLayout()
        right.setContentsMargins(12, 12, 0, 12)
        right.setSpacing(4)
        right.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._time = QLabel(secs(t.get("seconds")))
        self._time.setFont(font(11))
        self._time.setStyleSheet(f"color:{C_DIM2};")
        self._time.setAlignment(Qt.AlignRight)
        right.addWidget(self._time)

        cost_v = t.get("cost_cny")
        hot = isinstance(cost_v, (int, float)) and cost_v >= 0.5
        self._cost = QLabel(money(cost_v))
        self._cost.setFont(font(11))
        self._cost.setStyleSheet(
            f"color:{C_WARN if hot else C_DIM2}; font-size:11px;"
        )
        self._cost.setAlignment(Qt.AlignRight)
        right.addWidget(self._cost)

        root.addLayout(right)

    def update_data(self, t: dict) -> None:
        """原地更新内容，不重建控件。

        列表每几秒刷一次，两次之间绝大多数行根本没变。
        只改真正变了的文字，开销接近零。
        """
        st = t.get("status", "")
        color = status_color(st)

        self.name = t.get("name", "")
        self._bar.setStyleSheet(f"background:{color}; border-radius:1px;")
        self._dot.set_color(color)

        self._badge.setText(STATUS_TEXT.get(st, st))
        self._badge.setStyleSheet(self._badge_style(st))

        self._name.setText(self.name)

        desc = (t.get("task") or "").replace("\n", " ")
        if self._desc.text() != desc:
            self._desc.setText(desc)
            self._desc.setToolTip((t.get("task") or "")[:600])

        tm = secs(t.get("seconds"))
        if self._time.text() != tm:
            self._time.setText(tm)

        cost_v = t.get("cost_cny")
        cm = money(cost_v)
        if self._cost.text() != cm:
            self._cost.setText(cm)
            hot = isinstance(cost_v, (int, float)) and cost_v >= 0.5
            self._cost.setStyleSheet(
                f"color:{C_WARN if hot else C_DIM2}; font-size:11px;"
            )

    def mousePressEvent(self, e):  # noqa: N802
        self.clicked.emit(self.name)
        super().mousePressEvent(e)


class BusyIndicator(QWidget):
    """一个转圈的小弧，表示"我在动"。

    为什么要这个：
        用户点完按钮如果只是文字变一下、然后干等，
        他分不清"正在启动"还是"卡死了"。转圈是通用的"别急"信号。

    ⚠️ 定时器不能常驻：
        原来 start(60) 写在构造函数里，每个卡片一创建就挂一个
        60ms 的定时器，谁也停不了。卡片重建时越堆越多，
        几十个定时器一起调 update()，整个窗口就在闪。
        改成跟着可见性开关。
    """

    def __init__(self, size: int = 16):
        super().__init__()
        self._angle = 0
        self._size = size
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start(60)

    def hideEvent(self, e):  # noqa: N802
        self._timer.stop()
        super().hideEvent(e)

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, _):  # noqa: N802
        from PySide6.QtGui import QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._size / 2 - 2
        pen = QPen(QColor(C_ACCENT))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.translate(self._size / 2, self._size / 2)
        p.rotate(self._angle)
        p.drawArc(int(-r), int(-r), int(r * 2), int(r * 2), 0, 270 * 16)


class PathRow(QWidget):
    """一行路径设置：标签 + 输入框 + 浏览按钮。

    让人手打一条 Windows 路径是在为难人，特别是 AppData 下面
    那种长路径，错一个字符就找不到。点"选择"用系统对话框选。
    """

    changed = Signal()

    def __init__(self, key: str, label: str, hint: str = "",
                 pick_dir: bool = False, pick_name: str = ""):
        super().__init__()
        self.key = key
        self.pick_dir = pick_dir
        self.pick_name = pick_name

        # 高度必须钉死：放进 QVBoxLayout 又没有弹簧时，
        # Qt 会把控件压到最小高度，标签挤成一条线。
        # 高度要跟着输入框一起变：16(标签) + 38(输入框) + 3(间距) + 余量
        self.setFixedHeight(62)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        top = QLabel(label)
        top.setFixedHeight(16)
        top.setFont(font(11))
        top.setStyleSheet(f"color:{C_DIM2};")
        lay.addWidget(top)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.edit = QLineEdit()
        self.edit.setMinimumHeight(36)
        self.edit.setPlaceholderText(hint or "自动探测不到，点右边选择")
        # textChanged 带一个字符串参数，changed 是无参信号，
        # 直接连会报错，用 lambda 吞掉。
        self.edit.textChanged.connect(lambda _t: self.changed.emit())
        row.addWidget(self.edit, 1)

        btn = QPushButton("选择…")
        btn.setObjectName("ghost")
        btn.setMinimumHeight(36)
        btn.setMinimumWidth(70)
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._browse)
        row.addWidget(btn)
        lay.addLayout(row)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, v: str) -> None:
        self.edit.setText(v or "")

    def _browse(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        if self.pick_dir:
            path = QFileDialog.getExistingDirectory(self, "选择目录", self.value() or "")
        else:
            filt = (f"{self.pick_name or '程序'} (*.exe);;所有文件 (*.*)"
                    if self.pick_name else "")
            path, _ = QFileDialog.getOpenFileName(
                self, "选择文件", self.value() or "", filt)
        if path:
            self.set_value(path)


class ServiceCard(QFrame):
    """服务页的一张卡。

    按钮有两个状态，跟着服务真实状态走：
        没起来 → 「启动」
        起来了 → 「重启」

    ⚠️ 这里返工过一次：
        原来有两个同名的 set_working 方法叠着，后一个把前一个覆盖了，
        所以不管成功失败，按钮永远停在"重启中…"。
        Python 不会报错，两个方法默默后者胜——这类错误只能靠跑出来发现。
        现在合并成一个 mark_working / clear_working，职责分开。
    """

    action_clicked = Signal(str, str)

    def __init__(self, key: str, title: str, desc: str):
        super().__init__()
        self.key = key
        self.setObjectName("card")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(14)

        self.dot = GlowDot(C_DIM2, 9)
        lay.addWidget(self.dot, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(3)
        nm = QLabel(title)
        nm.setFont(font(13, QFont.DemiBold))
        nm.setStyleSheet(f"color:{C_TEXT};")
        ds = QLabel(desc)
        ds.setFont(font(11.5))
        ds.setStyleSheet(f"color:{C_DIM};")
        text.addWidget(nm)
        text.addWidget(ds)
        lay.addLayout(text, 1)

        self.btn = QPushButton("启动")
        self.btn.setObjectName("ghost")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self._on_click)
        lay.addWidget(self.btn)

        # 转圈动画：告诉用户"正在处理，别急"。
        # 光换个文字不够——用户会怀疑是不是卡住了。
        self._spin = BusyIndicator()
        self._spin.hide()
        lay.addWidget(self._spin)

        self._running = False
        self._working = False

    def _on_click(self) -> None:
        if self._working:
            return
        self.action_clicked.emit(self.key, "restart" if self._running else "start")

    def _refresh(self) -> None:
        """按当前状态更新按钮外观。

        ⚠️ 只在真的变了的时候才动控件。
            原来是每次都 setText + unpolish + polish，
            polish 会强制整个控件重新计算样式并重绘。
            服务状态是每 1.5 秒轮询一次的，于是按钮每 1.5 秒
            就闪一下——看起来就是"桌面端在自己乱闪"。

            现在记住上一次的目标状态，一样就直接返回。
        """
        want_working = self._working
        want_text = ("启动中…" if want_working
                     else ("重启" if self._running else "启动"))
        want_obj = "working" if want_working else "ghost"

        if (getattr(self, "_last_text", None) == want_text
                and getattr(self, "_last_obj", None) == want_obj
                and getattr(self, "_last_enabled", None) == (not want_working)):
            return

        self._last_text = want_text
        self._last_obj = want_obj
        self._last_enabled = not want_working

        self.btn.setText(want_text)
        self.btn.setEnabled(not want_working)
        self.btn.setObjectName(want_obj)
        self.btn.style().unpolish(self.btn)
        self.btn.style().polish(self.btn)

        if want_working:
            self._spin.show()
        else:
            self._spin.hide()

    def set_ok(self, ok: bool) -> None:
        """服务真实状态变了。

        状态一回来就把 working 清掉——不依赖任何请求的结果。
        这就是"点了我先不管还是卡在重启中"那个问题的解药。

        没变化时什么也不做：轮询每 1.5 秒来一次，
        每次都重绘一遍就是闪。
        """
        ok = bool(ok)
        if self._running == ok and not self._working:
            return
        self._running = ok
        self.dot.set_color(C_OK if ok else C_BAD)
        self._working = False
        self._refresh()

    def mark_working(self) -> None:
        self._working = True
        self.dot.set_color(C_WARN)
        self._refresh()

    def clear_working(self) -> None:
        self._working = False
        self._refresh()


# ────────────────────────────────────────────────────────────────────
# 任务详情
# ────────────────────────────────────────────────────────────────────


class DetailDialog(QDialog):
    def __init__(self, task: dict, api: Api, parent=None):
        super().__init__(parent)
        self.api = api
        self.task = task
        self.setWindowTitle(task.get("name", "任务"))
        self.resize(780, 640)

        st = task.get("status", "")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 22)
        lay.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(10)
        title = QLabel(task.get("name", ""))
        title.setFont(font(17, QFont.Bold))
        title.setStyleSheet(f"color:{C_TEXT};")
        head.addWidget(title)
        head.addWidget(StatusBadge(st))
        head.addStretch()
        lay.addLayout(head)

        metrics = QHBoxLayout()
        metrics.setSpacing(24)
        for k, v in (
            ("耗时", secs(task.get("seconds"))),
            ("上下文", toks(task.get("context_tokens"))),
            ("每轮", money(task.get("cost_cny")) or "—"),
        ):
            col = QVBoxLayout()
            col.setSpacing(2)
            kl = QLabel(k)
            kl.setFont(font(10.5))
            kl.setStyleSheet(f"color:{C_DIM2};")
            vl = QLabel(v)
            vl.setFont(font(14, QFont.DemiBold))
            vl.setStyleSheet(f"color:{C_TEXT2};")
            col.addWidget(kl)
            col.addWidget(vl)
            metrics.addLayout(col)
        metrics.addStretch()
        lay.addLayout(metrics)

        lay.addWidget(self._label("任务内容"))
        box = QTextEdit()
        box.setPlainText(task.get("task") or "")
        box.setReadOnly(True)
        box.setFixedHeight(130)
        lay.addWidget(box)

        is_err = bool(task.get("error"))
        lay.addWidget(self._label("失败原因" if is_err else "结果"))
        out = QTextEdit()
        body = task.get("error") or task.get("result") or ""
        out.setPlainText(body or ("还在跑，等它结束" if st == "running" else "没有结果"))
        out.setReadOnly(True)
        if is_err:
            out.setStyleSheet(
                f"QTextEdit {{ background:{C_INPUT}; border:1px solid #5a3a3a;"
                f"color:{C_BAD}; border-radius:10px; padding:14px; font-size:13px; }}"
            )
        lay.addWidget(out, 1)

        btns = QHBoxLayout()
        btns.addStretch()
        if st == "running":
            b = QPushButton("停掉")
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(self._cancel)
            btns.addWidget(b)
        if st in ("ok", "timeout", "error", "empty"):
            b = QPushButton("在同一个窗口继续")
            b.setObjectName("primary")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(self._continue)
            btns.addWidget(b)
        close = QPushButton("关闭")
        close.setObjectName("ghost")
        close.setCursor(Qt.PointingHandCursor)
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        lay.addLayout(btns)

    def _label(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setFont(font(11))
        lb.setStyleSheet(f"color:{C_DIM2};")
        return lb

    def _cancel(self) -> None:
        if QMessageBox.question(self, "确认", f"停掉「{self.task['name']}」？") != QMessageBox.Yes:
            return
        self.api.post("/api/cancel", {"name": self.task["name"]})
        self.accept()

    def _continue(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        msg, ok = QInputDialog.getText(self, "继续", "接着说一句：", text="继续")
        if ok and msg.strip():
            self.api.post("/api/continue", {"name": self.task["name"], "message": msg.strip()})
            self.accept()


# ────────────────────────────────────────────────────────────────────
# 主窗口
# ────────────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("dsh-bridge")
        self.resize(1200, 800)
        self.setMinimumSize(940, 640)
        self.tasks: list[dict] = []
        self.filter = "all"
        self._qr_dlg = None
        self._list_limit = 50      # 一次渲染多少行，不够可以再加
        # 模型配置列表
        self._profiles = []
        self._active_pid = ""
        self._selected_pid = ""
        self._profile_error = ""
        self.api = Api()
        self.api.done.connect(self._on_api)

        self._apply_style()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_topbar())
        root.addWidget(GradientBar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_tasks_page())
        self.stack.addWidget(self._build_services_page())
        self.stack.addWidget(self._build_settings_page())
        body.addWidget(self.stack, 1)

        root.addLayout(body, 1)

        self.t = QTimer(self)
        self.t.timeout.connect(self.refresh)
        self.t.start(3000)
        QTimer.singleShot(150, self.refresh)

        # 服务状态用独立节拍器盯着。
        # 它每 1.5 秒问一次，状态一变按钮就跟着变——
        # 这是"启动没反应/一直处理中"那个问题的根本解法。
        if ServiceMonitor is not None:
            self.monitor = ServiceMonitor(self)
            self.monitor.changed.connect(self._on_services)

    # ── 样式 ──
    def _apply_style(self) -> None:
        self.setStyleSheet(f"""
            QMainWindow, QDialog {{ background:{C_BG}; }}
            QWidget {{
                color:{C_TEXT};
                font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;
            }}

            QFrame#card {{
                background:{C_SURFACE}; border:1px solid {C_LINE};
                border-radius:12px;
            }}
            QFrame#taskrow {{
                background:{C_SURFACE}; border:1px solid {C_LINE};
                border-radius:10px;
            }}
            QFrame#taskrow:hover {{
                background:{C_RAISED}; border-color:{C_LINE_HI};
            }}

            QPushButton#primary {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {C_ACCENT}, stop:1 #8b5cf6);
                border:none; color:#ffffff;
                padding:9px 22px; border-radius:8px;
                font-size:13px; font-weight:600;
            }}
            QPushButton#primary:hover {{
                background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 {C_ACCENT2}, stop:1 #7c4ce8);
            }}
            QPushButton#primary:pressed {{ background:{C_ACCENT3}; }}
            QPushButton#primary:disabled {{
                background:{C_RAISED}; color:{C_DIM2};
            }}

            QPushButton#working {{
                background:{C_INPUT}; border:1px solid {C_ACCENT}; color:{C_ACCENT};
                padding:7px 16px; border-radius:8px; font-size:12.5px;
            }}

            QPushButton#ghost {{
                background:transparent; border:1px solid {C_LINE};
                color:{C_TEXT2}; padding:7px 16px; border-radius:8px;
                font-size:12.5px;
            }}
            QPushButton#ghost:hover {{
                background:{C_RAISED}; border-color:{C_LINE_HI}; color:{C_TEXT};
            }}

            QPushButton#nav {{
                background:transparent; border:none; color:{C_DIM};
                text-align:left; padding:10px 16px; border-radius:8px;
                font-size:13px;
            }}
            QPushButton#nav:hover {{ background:{C_SURFACE}; color:{C_TEXT2}; }}
            QPushButton#nav:checked {{
                background:{C_RAISED}; color:{C_TEXT}; font-weight:600;
            }}

            QPushButton#switchoff {{
                background:{C_INPUT}; border:1px solid {C_LINE}; color:{C_DIM};
                padding:7px 18px; border-radius:15px; font-size:12.5px;
            }}
            QPushButton#switchoff:hover {{ border-color:{C_LINE_HI}; color:{C_TEXT2}; }}
            QPushButton#switchon {{
                background:{C_ACCENT}; border:1px solid {C_ACCENT}; color:#fff;
                padding:7px 18px; border-radius:15px; font-size:12.5px;
                font-weight:600;
            }}

            QPushButton#chip {{
                background:transparent; border:1px solid transparent;
                color:{C_DIM}; padding:5px 14px; border-radius:14px;
                font-size:12px;
            }}
            QPushButton#chip:hover {{ color:{C_TEXT2}; background:{C_SURFACE}; }}
            QPushButton#chip:checked {{
                background:{C_RAISED}; border-color:{C_LINE_HI}; color:{C_TEXT};
            }}

            /* ⚠️ 输入框这里踩了一个坑：
               给 QLineEdit 设 setFixedHeight 之后，Qt 会把文字
               按默认的内边距画，结果上半截跑到框外面，
               看起来像"文字被遮挡"——截了三次图才发现不是截断，是位置错了。

               正确做法：**不要 setFixedHeight**，让 Qt 按字体和 padding
               自己算高度；只给一个最小高度兜底。
               这样文字永远在框里，居中。 */
            QLineEdit, QPlainTextEdit, QTextEdit, QComboBox {{
                background:{C_INPUT}; border:1px solid {C_LINE};
                color:{C_TEXT}; border-radius:9px;
                padding:7px 13px;
                font-size:13px; selection-background-color:{C_ACCENT};
            }}
            QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus {{
                border-color:{C_ACCENT};
            }}
            QComboBox::drop-down {{ border:none; width:22px; }}
            QComboBox QAbstractItemView {{
                background:{C_SURFACE}; border:1px solid {C_LINE_HI};
                border-radius:8px; selection-background-color:{C_ACCENT};
                color:{C_TEXT}; outline:none; padding:4px;
            }}

            QScrollArea {{ border:none; background:transparent; }}
            QScrollBar:vertical {{ background:transparent; width:10px; margin:2px; }}
            QScrollBar::handle:vertical {{
                background:{C_LINE_HI}; border-radius:3px; min-height:30px;
            }}
            QScrollBar::handle:vertical:hover {{ background:{C_DIM2}; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}

            QMessageBox, QInputDialog {{ background:{C_SURFACE}; }}
            QMessageBox QLabel, QInputDialog QLabel {{ color:{C_TEXT}; }}
            QMessageBox QPushButton, QInputDialog QPushButton {{
                background:{C_RAISED}; border:1px solid {C_LINE_HI};
                color:{C_TEXT}; padding:6px 18px; border-radius:7px;
                font-size:12.5px; min-width:64px;
            }}
            QMessageBox QPushButton:hover, QInputDialog QPushButton:hover {{
                background:{C_LINE}; 
            }}

            QToolTip {{
                background:{C_RAISED}; color:{C_TEXT};
                border:1px solid {C_LINE_HI}; padding:7px 10px;
                border-radius:6px; font-size:12px;
            }}
        """)

    # ── 顶栏 ──
    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setFixedHeight(54)
        bar.setStyleSheet(f"background:{C_SURFACE}; border-bottom:1px solid {C_LINE};")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(22, 0, 22, 0)
        lay.setSpacing(12)

        mark = QLabel("dsh-bridge")
        mark.setFont(font(14, QFont.Bold))
        mark.setStyleSheet(f"color:{C_TEXT};")
        lay.addWidget(mark)
        lay.addStretch()

        # 顶栏三个状态胶囊。
        #
        # ⚠️ 第一版这里出了问题：QFrame 默认会被父布局拉满高度，
        #    胶囊变成三个高瘦的长条，中间还露出奇怪的竖线。
        #    必须显式 setFixedHeight，并且用 AlignVCenter 传到布局里。
        self.pills = {}
        for key, label in (("qq", "QQ"), ("feishu", "飞书"), ("bridge", "任务端")):
            pill = QFrame()
            pill.setFixedHeight(26)
            pill.setStyleSheet(
                f"QFrame {{ background:{C_RAISED}; border:1px solid {C_LINE};"
                f"border-radius:13px; }}"
            )
            pl = QHBoxLayout(pill)
            pl.setContentsMargins(10, 0, 12, 0)
            pl.setSpacing(6)
            dot = GlowDot(C_DIM2, 6)
            lb = QLabel(label)
            lb.setFont(font(11.5))
            lb.setStyleSheet(f"color:{C_DIM}; background:transparent; border:none;")
            pl.addWidget(dot)
            pl.addWidget(lb)
            pill.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
            self.pills[key] = dot
            lay.addWidget(pill, 0, Qt.AlignVCenter)

        return bar

    # ── 侧栏 ──
    def _build_sidebar(self) -> QWidget:
        side = QFrame()
        side.setFixedWidth(184)
        side.setStyleSheet(f"background:{C_SURFACE}; border-right:1px solid {C_LINE};")
        lay = QVBoxLayout(side)
        lay.setContentsMargins(12, 16, 12, 16)
        lay.setSpacing(4)

        self.nav_btns = []
        for text, idx in (("任务", 0), ("服务", 1), ("设置", 2)):
            b = QPushButton(text)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=idx: self.switch(i))
            lay.addWidget(b)
            self.nav_btns.append(b)
        self.nav_btns[0].setChecked(True)

        lay.addStretch()
        ver = QLabel("v1.3.0")
        ver.setFont(font(10))
        ver.setStyleSheet(f"color:{C_DIM2}; padding:0 16px;")
        lay.addWidget(ver)
        return side

    def switch(self, idx: int) -> None:
        self.stack.setCurrentIndex(idx)
        for i, b in enumerate(self.nav_btns):
            b.setChecked(i == idx)
        if idx == 2:
            self._load_profiles()
            self._load_paths()
            self._load_guard()
            self._refresh_autostart()

    # ── 任务页 ──
    def _build_tasks_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)

        head = QVBoxLayout()
        head.setSpacing(4)
        h = QLabel("任务")
        h.setFont(font(19, QFont.Bold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("派出去让电脑干的活，都在这儿。")
        sub.setFont(font(12))
        sub.setStyleSheet(f"color:{C_DIM};")
        head.addWidget(h)
        head.addWidget(sub)
        lay.addLayout(head)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(10)

        l1 = QLabel("要做什么")
        l1.setFont(font(11))
        l1.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(l1)

        self.ed_task = QPlainTextEdit()
        self.ed_task.setPlaceholderText("比如：把登录页的按钮改大一点，顺便调下颜色")
        self.ed_task.setFixedHeight(74)
        cl.addWidget(self.ed_task)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("叫它什么？比如：登录页")
        row.addWidget(self.ed_name, 1)

        self.btn_send = QPushButton("派出去")
        self.btn_send.setObjectName("primary")
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.clicked.connect(self._dispatch)
        row.addWidget(self.btn_send)
        cl.addLayout(row)

        tip = QLabel("起个短的好记的名字。以后想知道进度就说这个名字。")
        tip.setFont(font(10.5))
        tip.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(tip)

        lay.addWidget(card)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        self.chip_btns = []
        for key, text in (("all", "全部"), ("running", "运行中"),
                          ("ok", "已完成"), ("bad", "失败")):
            c = QPushButton(text)
            c.setObjectName("chip")
            c.setCheckable(True)
            c.setCursor(Qt.PointingHandCursor)
            c.clicked.connect(lambda _=False, k=key: self._set_filter(k))
            chips.addWidget(c)
            self.chip_btns.append((key, c))
        self.chip_btns[0][1].setChecked(True)
        chips.addStretch()
        lay.addLayout(chips)

        # ⚠️ 这里不能加 addStretch()。
        #
        #    setWidgetResizable(True) 的意思是"让内容撑满可视区域"。
        #    而内容里如果有个弹簧，Qt 会陷入这样的循环：
        #        内容高度取决于可用空间 → 可用空间取决于滚动条
        #        → 滚动条取决于内容高度 → 回到起点
        #    算不完就一直在算，屏幕上表现为**整个区域持续闪烁**。
        #
        #    实测：光看代码看不出问题，因为它不是"多画了一次"，
        #    而是布局引擎在反复迭代。
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder = QWidget()
        self.list_lay = QVBoxLayout(holder)
        self.list_lay.setContentsMargins(0, 0, 8, 0)
        self.list_lay.setSpacing(8)
        # 不用 SetMinAndMaxSize：那会让每加一个控件都把整棵树重新算一遍。
        # 一百个任务行 = 一百次全树重算，实测三千多次 PaletteChange、
        # 一千多次 Resize，屏幕上就是持续闪烁。
        # 让 QScrollArea 自己按内容高度调整就够了。
        self.scroll.setWidget(holder)
        lay.addWidget(self.scroll, 1)

        return page

    def _set_filter(self, key: str) -> None:
        self.filter = key
        for k, b in self.chip_btns:
            b.setChecked(k == key)
        self._render_tasks()

    # ── 服务页 ──
    def _build_services_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(14)

        h = QLabel("服务")
        h.setFont(font(19, QFont.Bold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("用哪个开哪个，没开的不会启动。")
        sub.setFont(font(12))
        sub.setStyleSheet(f"color:{C_DIM};")
        lay.addWidget(h)
        lay.addWidget(sub)

        # ⚠️ 这里从"三样都开"改成了"按需开"。
        #
        #    原来点一次启动，QQ 和飞书一起连上：
        #        QQ   → 拉 QQ 客户端（几百兆）
        #        飞书 → 只是网络连接（几十兆）
        #    只用飞书的人白背着 QQ，低配机器直接卡。
        #
        #    现在两条通道各一个开关，没点的不启动。
        #    任务端跟通道无关，单独一个开关。
        self.svc_cards = {}
        for key, title, desc in (
            ("qq", "QQ 通道", "要拉 QQ 客户端，比较占内存。不用 QQ 就关掉"),
            ("feishu", "飞书通道", "纯网络连接，很轻，几秒钟就能开"),
            ("bridge", "任务端", "真正在电脑上干活的那个，跟通道无关"),
        ):
            card = ServiceCard(key, title, desc)
            card.action_clicked.connect(self._svc_action)
            self.svc_cards[key] = card
            lay.addWidget(card)

        # 一行提示：说明为什么拆开
        hint = QLabel(
            "只需要一个平台的话，只开那一个就行——另一个不会启动，省内存。"
        )
        hint.setFont(font(11))
        hint.setStyleSheet(f"color:{C_DIM2};")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        lay.addStretch()
        return page

    # ── 设置页 ──
    def _build_settings_page(self) -> QWidget:
        # 设置页内容长（三张卡 + 五条路径），小窗口下会超出高度。
        # 不包滚动区的话，Qt 会优先把控件压扁来腾地方——
        # 实测出现过输入框被压成一条细线、标签糊在一起。
        #
        # 所以结构是：
        #     page(滚动区) -> inner(真正的内容) -> 各种卡片
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setStyleSheet("QScrollArea { border:none; background:transparent; }")

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(28, 24, 28, 22)
        lay.setSpacing(16)

        h = QLabel("设置")
        h.setFont(font(19, QFont.Bold))
        h.setStyleSheet(f"color:{C_TEXT};")
        sub = QLabel("改了立刻生效，不用重启。")
        sub.setFont(font(12))
        sub.setStyleSheet(f"color:{C_DIM};")
        lay.addWidget(h)
        lay.addWidget(sub)

        # ── 任务端模型 ──
        #
        # 做成"列表 + 详情"：
        #   上面是你的几套配置（每套一个服务商）
        #   点一行选中，下面填这一套的地址、Key、模型
        #   可以新增、删除、切换
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(9)

        mh = QLabel("任务端用哪个模型")
        mh.setFont(font(13, QFont.DemiBold))
        mh.setStyleSheet(f"color:{C_TEXT};")
        cl.addWidget(mh)

        msub = QLabel("可以存好几套，随时切换。当前用的那套会标绿点。")
        msub.setFont(font(11))
        msub.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(msub)

        # 配置列表。
        #
        # ⚠️ 必须包在一个 QWidget 里，不能直接 addLayout。
        #    直接塞 layout 的话，清空时 takeAt + deleteLater
        #    会作用到父容器身上，把别人的控件也删掉——
        #    实测报 "Internal C++ object already deleted"，
        #    整个设置页的输入框全废了。
        self.profile_host = QWidget()
        self.profile_box = QVBoxLayout(self.profile_host)
        self.profile_box.setContentsMargins(0, 0, 0, 0)
        self.profile_box.setSpacing(4)
        cl.addWidget(self.profile_host)

        # 新增按钮
        addrow = QHBoxLayout()
        addrow.setSpacing(8)
        self.btn_add_profile = QPushButton("＋ 新增一套")
        self.btn_add_profile.setObjectName("ghost")
        self.btn_add_profile.setCursor(Qt.PointingHandCursor)
        self.btn_add_profile.clicked.connect(self._add_profile)
        addrow.addWidget(self.btn_add_profile)
        addrow.addStretch()
        cl.addLayout(addrow)

        # 分隔线
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background:{C_LINE};")
        cl.addWidget(line)

        # 编辑区
        self.edit_title = QLabel("选一套来改")
        self.edit_title.setFont(font(11))
        self.edit_title.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(self.edit_title)

        def field2(label: str, placeholder: str, secret: bool = False):
            lb = QLabel(label)
            lb.setFont(font(11))
            lb.setStyleSheet(f"color:{C_DIM2};")
            cl.addWidget(lb)
            ed = QLineEdit()
            ed.setMinimumHeight(36)
            ed.setPlaceholderText(placeholder)
            if secret:
                ed.setEchoMode(QLineEdit.Password)
            cl.addWidget(ed)
            return ed

        self.ed_base = field2("接口地址", "https://api.openai.com")
        self.ed_key = field2("API Key", "留空表示不改", secret=True)

        self.lbl_key_state = QLabel("")
        self.lbl_key_state.setFont(font(10.5))
        self.lbl_key_state.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(self.lbl_key_state)

        lbl_model = QLabel("模型名")
        lbl_model.setFont(font(11))
        lbl_model.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(lbl_model)

        self.cmb_engine_model = QComboBox()
        self.cmb_engine_model.setEditable(True)
        self.cmb_engine_model.setMinimumHeight(36)
        self.cmb_engine_model.lineEdit().setPlaceholderText("选一个，或者直接输入")
        cl.addWidget(self.cmb_engine_model)

        # 快速填
        quick = QHBoxLayout()
        quick.setSpacing(8)
        ql = QLabel("快速填：")
        ql.setFont(font(10.5))
        ql.setStyleSheet(f"color:{C_DIM2};")
        quick.addWidget(ql)
        for name, base, model in (
            ("DeepSeek", "https://api.deepseek.com", "deepseek-chat"),
            ("OpenAI", "https://api.openai.com", "gpt-4o"),
            ("Kimi", "https://api.moonshot.cn", "kimi-k2.6"),
            ("通义", "https://dashscope.aliyuncs.com/compatible-mode", "qwen-plus"),
            ("硅基流动", "https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3"),
        ):
            b = QPushButton(name)
            b.setObjectName("ghost")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(
                lambda _=False, u=base, mo=model: (
                    self.ed_base.setText(u), self._set_model_text(mo)
                )
            )
            quick.addWidget(b)
        quick.addStretch()
        cl.addLayout(quick)

        # 操作按钮
        arow2 = QHBoxLayout()
        arow2.setSpacing(8)

        b_test = QPushButton("测试连接")
        b_test.setObjectName("ghost")
        b_test.setCursor(Qt.PointingHandCursor)
        b_test.clicked.connect(self._test_engine)
        arow2.addWidget(b_test)

        b_save = QPushButton("保存")
        b_save.setObjectName("primary")
        b_save.setCursor(Qt.PointingHandCursor)
        b_save.clicked.connect(self._save_engine)
        arow2.addWidget(b_save)

        self.btn_del_profile = QPushButton("删除这套")
        self.btn_del_profile.setObjectName("ghost")
        self.btn_del_profile.setCursor(Qt.PointingHandCursor)
        self.btn_del_profile.clicked.connect(self._delete_profile)
        arow2.addWidget(self.btn_del_profile)

        self.lbl_engine = QLabel("")
        self.lbl_engine.setFont(font(10.5))
        self.lbl_engine.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_engine.setWordWrap(True)
        arow2.addWidget(self.lbl_engine, 1)
        cl.addLayout(arow2)

        self.lbl_engine_now = QLabel("")
        self.lbl_engine_now.setFont(font(10.5))
        self.lbl_engine_now.setStyleSheet(f"color:{C_DIM2};")
        cl.addWidget(self.lbl_engine_now)

        # ⚠️ 一定要把 card 加进 lay。
        #
        #    漏了这一句的后果很隐蔽：card 建出来了、控件也塞进去了，
        #    但没有任何人引用它，Qt 的垃圾回收会把它连带子控件一起销毁。
        #    表现是 self.ed_base 指向一个已经死掉的 C++ 对象，
        #    一碰就报 "Internal C++ object already deleted"。
        #
        #    这个 bug 查了很久：先怀疑是重复创建、再怀疑是 layout 嵌套，
        #    最后才发现就是少了一行 addWidget。
        lay.addWidget(card)

        # ── 安全验证 ──
        #
        # 危险操作（删库、回滚、读密钥）要验证码。
        # 前两次是提醒和警告，第三次才发码到邮箱。
        #
        # 为什么用邮箱不用短信：
        #   短信要服务商、要钱、要备案。邮箱免费、独立、
        #   而且同样"不在对话里"——这才是关键。
        scard = QFrame()
        scard.setObjectName("card")
        sc = QVBoxLayout(scard)
        sc.setContentsMargins(20, 18, 20, 18)
        sc.setSpacing(9)

        sh = QLabel("危险操作验证")
        sh.setFont(font(13, QFont.DemiBold))
        sh.setStyleSheet(f"color:{C_TEXT};")
        sc.addWidget(sh)

        ssub = QLabel(
            "删库、回滚代码、读密钥这类操作，会发验证码到你邮箱。"
            "前两次只是提醒和警告，第三次坚持才发码。"
        )
        ssub.setFont(font(11))
        ssub.setStyleSheet(f"color:{C_DIM2};")
        ssub.setWordWrap(True)
        sc.addWidget(ssub)

        # 开关
        srow = QHBoxLayout()
        srow.setSpacing(10)
        self.btn_guard = QPushButton("开启")
        self.btn_guard.setObjectName("ghost")
        self.btn_guard.setCursor(Qt.PointingHandCursor)
        self.btn_guard.clicked.connect(self._toggle_guard)
        srow.addWidget(self.btn_guard)
        self.lbl_guard_state = QLabel("")
        self.lbl_guard_state.setFont(font(10.5))
        self.lbl_guard_state.setStyleSheet(f"color:{C_DIM2};")
        srow.addWidget(self.lbl_guard_state, 1)
        sc.addLayout(srow)

        def sfield(label: str, placeholder: str, secret: bool = False):
            lb = QLabel(label)
            lb.setFont(font(11))
            lb.setStyleSheet(f"color:{C_DIM2};")
            sc.addWidget(lb)
            ed = QLineEdit()
            ed.setMinimumHeight(36)
            ed.setPlaceholderText(placeholder)
            if secret:
                ed.setEchoMode(QLineEdit.Password)
            sc.addWidget(ed)
            return ed

        self.ed_guard_email = sfield("收验证码的邮箱", "you@example.com")

        # 服务器不让人填——填完邮箱自动认出来。
        #
        # 让用户去查"你的邮箱 SMTP 服务器是什么"，是个很烦的门槛：
        # 大部分人根本不知道这个词，得去搜文档，搜到了还怕填错。
        # 常见邮箱就那么几家，认一下就完事。
        lbl_host = QLabel("发信服务器")
        lbl_host.setFont(font(11))
        lbl_host.setStyleSheet(f"color:{C_DIM2};")
        sc.addWidget(lbl_host)

        self.lbl_guard_host = QLabel("填上面的邮箱，自动认")
        self.lbl_guard_host.setFont(font(11.5))
        self.lbl_guard_host.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_guard_host.setMinimumHeight(36)
        self.lbl_guard_host.setStyleSheet(
            f"color:{C_DIM2}; padding:8px 13px;"
            f" background:{C_INPUT}; border:1px solid {C_LINE}; border-radius:9px;"
        )
        sc.addWidget(self.lbl_guard_host)

        # 填完邮箱立刻认
        self.ed_guard_email.textChanged.connect(self._detect_guard_host)

        # 发信账号要单独填。
        #
        # 为什么不能默认用收件邮箱：
        #   收件邮箱是"发到哪"，发信账号是"谁发的"。
        #   这俩经常不一样——比如你收件用 163，
        #   但机器人的发信账号是专门注册的一个 QQ 邮箱。
        self.ed_guard_user = sfield(
            "前台 Agent 发信账号", "机器人用哪个邮箱发给你"
        )
        self.ed_guard_pass = sfield(
            "前台 Agent 发信授权码", "这个账号的授权码，不是登录密码",
            secret=True,
        )

        grow = QHBoxLayout()
        grow.setSpacing(8)
        g1 = QPushButton("保存")
        g1.setObjectName("primary")
        g1.setCursor(Qt.PointingHandCursor)
        g1.clicked.connect(self._save_guard)
        grow.addWidget(g1)

        g2 = QPushButton("发封测试邮件")
        g2.setObjectName("ghost")
        g2.setCursor(Qt.PointingHandCursor)
        g2.clicked.connect(self._test_guard)
        grow.addWidget(g2)

        self.lbl_guard_msg = QLabel("")
        self.lbl_guard_msg.setFont(font(10.5))
        self.lbl_guard_msg.setStyleSheet(f"color:{C_DIM2};")
        self.lbl_guard_msg.setWordWrap(True)
        grow.addWidget(self.lbl_guard_msg, 1)
        sc.addLayout(grow)

        ghint = QLabel(
            "常见配置：QQ 邮箱 smtp.qq.com｜163 邮箱 smtp.163.com｜"
            "Gmail smtp.gmail.com"
        )
        ghint.setFont(font(10))
        ghint.setStyleSheet(f"color:{C_DIM2};")
        ghint.setWordWrap(True)
        sc.addWidget(ghint)

        lay.addWidget(scard)

        # ── 启动行为 ──
        acard = QFrame()
        acard.setObjectName("card")
        al = QVBoxLayout(acard)
        al.setContentsMargins(20, 18, 20, 18)
        al.setSpacing(8)

        ah = QLabel("启动行为")
        ah.setFont(font(13, QFont.DemiBold))
        ah.setStyleSheet(f"color:{C_TEXT};")
        al.addWidget(ah)

        arow = QHBoxLayout()
        arow.setSpacing(12)

        self.switch_auto = QPushButton("开机自启")
        self.switch_auto.setCheckable(True)
        self.switch_auto.setCursor(Qt.PointingHandCursor)
        self.switch_auto.setMinimumWidth(110)
        self.switch_auto.clicked.connect(self._toggle_autostart)
        arow.addWidget(self.switch_auto)

        atip = QLabel("打开后，开机时自动把任务台启动起来。")
        atip.setFont(font(11))
        atip.setStyleSheet(f"color:{C_DIM2};")
        arow.addWidget(atip, 1)
        al.addLayout(arow)

        self.lbl_auto = QLabel("")
        self.lbl_auto.setFont(font(10.5))
        self.lbl_auto.setStyleSheet(f"color:{C_DIM2};")
        al.addWidget(self.lbl_auto)

        lay.addWidget(acard)

        # ── 路径配置 ──
        # 这三个是启动服务必须知道的。原来写死在代码里，
        # 换台机器就跑不起来，所以搬到这里让用户自己管。
        pcard = QFrame()
        pcard.setObjectName("card")
        pl = QVBoxLayout(pcard)
        pl.setContentsMargins(20, 18, 20, 18)
        pl.setSpacing(12)

        ph = QLabel("程序装在哪")
        ph.setFont(font(13, QFont.DemiBold))
        ph.setStyleSheet(f"color:{C_TEXT};")
        pl.addWidget(ph)

        psub = QLabel("点「启动」的时候要用到这些。装好之后一般不用改。")
        psub.setFont(font(11))
        psub.setStyleSheet(f"color:{C_DIM2};")
        pl.addWidget(psub)

        self.path_rows = {}
        for key, label, hint, is_dir, name in (
            ("qq_exe", "QQ 程序", "比如 C:\\Program Files\\QQ.exe", False, "QQ.exe"),
            ("napcat_dir", "NapCat 目录", "里面有 NapCatWinBootMain.exe 的那个文件夹", True, ""),
            ("astrbot_exe", "对话服务程序", "astrbot.exe 的位置", False, "astrbot.exe"),
            ("astrbot_pyw", "Python 无窗口解释器", "pythonw.exe，没有它启动会闪一下黑框", False, "pythonw.exe"),
            ("astrbot_cwd", "对话服务数据目录", "AstrBot 的工作目录", True, ""),
        ):
            r = PathRow(key, label, hint, is_dir, name)
            self.path_rows[key] = r
            pl.addWidget(r)

        prow = QHBoxLayout()
        prow.setSpacing(8)

        b1 = QPushButton("重新探测")
        b1.setObjectName("ghost")
        b1.setCursor(Qt.PointingHandCursor)
        b1.clicked.connect(self._detect_paths)
        prow.addWidget(b1)

        b2 = QPushButton("保存")
        b2.setObjectName("primary")
        b2.setCursor(Qt.PointingHandCursor)
        b2.clicked.connect(self._save_paths)
        prow.addWidget(b2)

        self.lbl_paths = QLabel("")
        self.lbl_paths.setFont(font(10.5))
        self.lbl_paths.setStyleSheet(f"color:{C_DIM2};")
        prow.addWidget(self.lbl_paths, 1)
        pl.addLayout(prow)

        lay.addWidget(pcard)

        page.setWidget(inner)
        return page

    # ── 安全验证 ──

    def _load_guard(self) -> None:
        """把安全邮箱配置读出来。"""

        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                return
            self._guard_on = bool(data.get("enabled"))
            self.ed_guard_email.setText(data.get("email") or "")
            self.ed_guard_user.setText(data.get("smtp_user") or "")
            # 服务器不用填，_detect_guard_host 会跟着邮箱自动更新
            self.ed_guard_pass.clear()

            if data.get("has_pass"):
                self.ed_guard_pass.setPlaceholderText("已保存 · 留空表示不改")
            else:
                self.ed_guard_pass.setPlaceholderText(
                    "邮箱设置里那个「授权码」，不是登录密码"
                )

            self._refresh_guard_ui(bool(data.get("ready")))

        self.api.request_once("/api/guard", callback=done)

    def _refresh_guard_ui(self, ready: bool) -> None:
        on = getattr(self, "_guard_on", False)
        self.btn_guard.setText("开启" if not on else "关闭")
        if not ready:
            text = "还没配好 · 危险操作目前不验证"
            color = C_BAD
        elif on:
            text = "已开启 · 危险操作要邮箱验证码"
            color = C_OK
        else:
            text = "配置好了，但开关是关的"
            color = C_WARN
        self.lbl_guard_state.setText(text)
        self.lbl_guard_state.setStyleSheet(f"color:{color}; font-size:10.5px;")

    def _toggle_guard(self) -> None:
        on = not getattr(self, "_guard_on", False)
        self._guard_on = on
        self.api.request_once(
            "/api/guard", {"enabled": on}, callback=lambda _d: self._load_guard()
        )

    # 常见邮箱的 SMTP 服务器
    _SMTP = {
        "qq.com": ("smtp.qq.com", 465),
        "foxmail.com": ("smtp.qq.com", 465),
        "163.com": ("smtp.163.com", 465),
        "126.com": ("smtp.126.com", 465),
        "yeah.net": ("smtp.yeah.net", 465),
        "sina.com": ("smtp.sina.com", 465),
        "sohu.com": ("smtp.sohu.com", 465),
        "aliyun.com": ("smtp.aliyun.com", 465),
        "139.com": ("smtp.139.com", 465),
        "gmail.com": ("smtp.gmail.com", 465),
        "outlook.com": ("smtp-mail.outlook.com", 587),
        "hotmail.com": ("smtp-mail.outlook.com", 587),
        "live.com": ("smtp-mail.outlook.com", 587),
        "yahoo.com": ("smtp.mail.yahoo.com", 465),
        "icloud.com": ("smtp.mail.me.com", 587),
    }

    def _detect_guard_host(self, text: str) -> None:
        """填邮箱时自动显示会用的发信服务器。"""
        text = (text or "").strip().lower()
        if "@" not in text:
            self.lbl_guard_host.setText("填上面的邮箱，自动认")
            self.lbl_guard_host.setStyleSheet(
                f"color:{C_DIM2}; padding:8px 13px;"
                f" background:{C_INPUT}; border:1px solid {C_LINE};"
                f" border-radius:9px;"
            )
            self._guard_host_value = ("", 0)
            return

        domain = text.rsplit("@", 1)[1]
        host, port = self._SMTP.get(domain, ("", 0))

        if host:
            self.lbl_guard_host.setText(f"{host}  端口 {port}   ← 自动识别")
            self.lbl_guard_host.setStyleSheet(
                f"color:{C_OK}; padding:8px 13px;"
                f" background:{C_INPUT}; border:1px solid {C_LINE};"
                f" border-radius:9px;"
            )
            self._guard_host_value = (host, port)
        else:
            self.lbl_guard_host.setText(
                f"认不出 {domain} 的服务器，你可以先保存试试"
            )
            self.lbl_guard_host.setStyleSheet(
                f"color:{C_WARN}; padding:8px 13px;"
                f" background:{C_INPUT}; border:1px solid {C_LINE};"
                f" border-radius:9px;"
            )
            self._guard_host_value = ("", 0)

    def _save_guard(self) -> None:
        host, port = getattr(self, "_guard_host_value", ("", 0))
        body = {
            "email": self.ed_guard_email.text().strip(),
            "smtp_host": host,
            "smtp_port": port,
            "smtp_user": self.ed_guard_user.text().strip(),
            "smtp_pass": self.ed_guard_pass.text().strip(),
            "enabled": True,
        }
        if not body["email"]:
            self.lbl_guard_msg.setText("收验证码的邮箱得填")
            return
        if not host:
            self.lbl_guard_msg.setText(
                "认不出这个邮箱的发信服务器，换一个常见邮箱试试（QQ / 163 / Gmail）"
            )
            return
        if not body["smtp_user"]:
            self.lbl_guard_msg.setText("前台 Agent 发信账号得填")
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_guard_msg.setText("已保存")
                self._guard_on = True
                self._load_guard()
            else:
                self.lbl_guard_msg.setText("保存失败")

        self.lbl_guard_msg.setText("保存中…")
        self.api.request_once("/api/guard", body, callback=done)

    def _test_guard(self) -> None:
        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_guard_msg.setText(data.get("message") or "发出去了")
            else:
                self.lbl_guard_msg.setText(
                    (data or {}).get("message")
                    or (data or {}).get("error")
                    or "发不出去"
                )

        self.lbl_guard_msg.setText("发测试邮件…")
        self.api.request_once("/api/guard/test", {}, timeout=40, callback=done)

    # ── 行为 ──
    def refresh(self) -> None:
        self.api.get("/api/tasks")
        self.api.get("/api/services")

    def _on_qq_stopped(self) -> None:
        """用户放弃扫码，QQ 已经停掉。界面回到干净状态。"""
        card = self.svc_cards.get("qq")
        if card:
            card.clear_working()
            card.set_ok(False)
        self.pills["qq"].set_color(C_BAD)

    def _on_services(self, d: dict) -> None:
        """服务状态变了。按钮和顶栏都跟着刷新。

        每个组件自己判断"有没有变"，没变就不重绘。
        这一层不额外做判断——状态逻辑放组件里，别两处都管。

        ⚠️ 只处理**真的返回了的字段**。
            原来写 bool(d.get(k))，字段缺失时 .get 返回 None，
            bool(None) 是 False——于是"这次没问到"被当成了"这个关了"。

            现在的规则：这个键不在返回里，说明这次没问到它，
            保留上一次的显示，什么都别动。
        """
        for k, dot in self.pills.items():
            if k not in d:
                continue
            dot.set_color(C_OK if d.get(k) else C_BAD)
        for k, card in self.svc_cards.items():
            if k not in d:
                continue
            card.set_ok(bool(d.get(k)))

    def _on_api(self, path: str, data) -> None:
        if path == "/api/tasks" and isinstance(data, list):
            self.tasks = data
            self._render_tasks()
        elif path == "/api/services" and isinstance(data, dict):
            for k, dot in self.pills.items():
                dot.set_color(C_OK if data.get(k) else C_BAD)
            for k, card in self.svc_cards.items():
                card.set_ok(bool(data.get(k)))
        elif path == "/api/model" and isinstance(data, dict):
            if data.get("preset"):
                i = self.cmb_model.findData(data["preset"])
                if i >= 0:
                    self.cmb_model.setCurrentIndex(i)
            self.lbl_now.setText(
                f"现在用的是：{data.get('provider_now')} / {data.get('model_now')}"
            )
        elif path in ("/api/dispatch", "/api/continue", "/api/cancel"):
            self.refresh()

    def _render_tasks(self) -> None:
        """刷新任务列表。

        ⚠️ 这里的关键是**不要重建**。
            一开始是每次把上百个 TaskRow 全销毁再全新建一遍。
            实测：50 行 135 毫秒，100 行 435 毫秒。
            列表每 3 秒刷一次，等于界面有一大半时间在重建控件——
            屏幕上一直闪，而且点什么都没反应（主线程被占着）。

            现在分两种情况：
                · 行数和顺序都没变 → 原地更新每行的文字，开销接近零
                · 变了（新增任务、筛选、第一次）→ 才重建
        """
        shown = self.tasks
        if self.filter == "running":
            shown = [t for t in self.tasks if t.get("status") == "running"]
        elif self.filter == "ok":
            shown = [t for t in self.tasks if t.get("status") == "ok"]
        elif self.filter == "bad":
            shown = [t for t in self.tasks if t.get("status") in ("error", "timeout")]

        limit = self._list_limit
        shown = shown[:limit]
        names = [t.get("name") for t in shown]

        # 结构没变：原地更新
        if getattr(self, "_rows_names", None) == names and self.list_lay.count():
            for i, t in enumerate(shown):
                item = self.list_lay.itemAt(i)
                row = item.widget() if item else None
                if isinstance(row, TaskRow):
                    row.update_data(t)
            return

        # 结构变了：重建
        while self.list_lay.count():
            it = self.list_lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        self._rows_names = names

        holder = self.list_lay.parentWidget()
        if holder is not None:
            holder.setUpdatesEnabled(False)

        if not shown:
            lb = QLabel("这里还没有任务")
            lb.setFont(font(12.5))
            lb.setStyleSheet(f"color:{C_DIM2};")
            lb.setAlignment(Qt.AlignCenter)
            self.list_lay.addWidget(lb)
        else:
            for t in shown:
                row = TaskRow(t)
                row.clicked.connect(self._open_task)
                self.list_lay.addWidget(row)

        total = len([x for x in self.tasks]) if self.filter == "all" else len(
            [x for x in self.tasks if (
                x.get("status") == self.filter
                or (self.filter == "bad" and x.get("status") in ("error", "timeout"))
            )]
        )
        if total > limit:
            more = QPushButton(f"还有 {total - limit} 条，显示更多")
            more.setObjectName("ghost")
            more.setCursor(Qt.PointingHandCursor)

            def show_more():
                self._list_limit = min(self._list_limit + 50, len(self.tasks))
                self._rows_names = None
                self._render_tasks()

            more.clicked.connect(show_more)
            self.list_lay.addWidget(more)

        if holder is not None:
            holder.setUpdatesEnabled(True)

    def _open_task(self, name: str) -> None:
        t = next((x for x in self.tasks if x.get("name") == name), None)
        if t:
            DetailDialog(t, self.api, self).exec()

    def _dispatch(self) -> None:
        task = self.ed_task.toPlainText().strip()
        name = self.ed_name.text().strip()
        if not task:
            QMessageBox.information(self, "还差一步", "先说要做什么。")
            return
        if not name:
            QMessageBox.information(self, "还差一步", "给它起个名字，方便以后找。")
            return
        self.btn_send.setEnabled(False)
        self.btn_send.setText("派发中…")
        self.api.post("/api/dispatch", {"task": task, "name": name})

        def restore():
            self.btn_send.setEnabled(True)
            self.btn_send.setText("派出去")
            self.ed_task.clear()
            self.ed_name.clear()

        QTimer.singleShot(1200, restore)

    def _svc_action(self, key: str, action: str) -> None:
        """启动或重启一个服务。

        流程设计（这一版的关键改动）：
            不在"请求返回"上等结果。请求只是把动作发出去，
            真正的进度靠 ServiceMonitor 每 1.5 秒问一次服务状态。
            这样：
                · 用户中途点「我先不管」，按钮不会卡住
                · QQ 要扫码等一分钟，界面照样能动
                · 状态一真起来，按钮自己和二维码窗口自己收尾
        """
        names = {"qq": "QQ 通道", "feishu": "飞书通道", "bridge": "任务端"}
        verb = "启动" if action == "start" else "重启"

        card = self.svc_cards.get(key)
        if card:
            card.mark_working()

        # 飞书通道不需要扫码，直接走。
        # 它比 QQ 轻得多：宿主起来了、把这条连接打开就行，几秒钟的事。
        # 所以不走下面那套"等扫码"的流程。

        # QQ 起的时候把扫码头摆出来。
        # 它自己会判断：一旦发现 QQ 通了就关掉自己，
        # 所以不需要提前猜"这次要不要扫"。
        if key == "qq" and LoginQRDialog is not None:
            if self._qr_dlg is None or not self._qr_dlg.isVisible():
                self._qr_dlg = LoginQRDialog(self)
                # 用户点「不管了」→ QQ 已经被停掉，
                # 按钮和状态都要跟着回到"没启动"
                self._qr_dlg.stopped.connect(self._on_qq_stopped)
                self._qr_dlg.show()

        path = "/api/start" if action == "start" else "/api/restart"
        label = f"{verb}{names[key]}"

        def done(data):
            if not isinstance(data, dict):
                if card:
                    card.clear_working()
                return
            if data.get("need_login"):
                # 要扫码：把窗口叫到前面来，别让用户自己找
                if self._qr_dlg is not None:
                    self._qr_dlg.raise_()
                    self._qr_dlg.activateWindow()
                return
            if not data.get("ok") and data.get("error"):
                # 只在失败时弹窗。成功不打扰——界面自己会变成"重启"
                QMessageBox.warning(self, f"{label}没成", str(data["error"]))
            if card:
                card.clear_working()

        # 超时分开给：
        #   QQ   → 要等扫码，给足 200 秒
        #   飞书 → 只是连个网络，120 秒足够（含 AstrBot 冷启动）
        #   任务端 → 立刻就回
        timeout = {"qq": 200, "feishu": 150, "bridge": 30}.get(key, 120)
        self.api.request_once(path, {"target": key},
                              timeout=timeout, callback=done)

    def _detect_paths(self) -> None:
        """重新探测一遍路径，把结果填进输入框。

        注意：只填不保存。用户得点"保存"才算数——
        万一探测错了，不会把好用的配置覆盖掉。
        """
        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                self.lbl_paths.setText("探测失败")
                return
            found = data.get("detected") or {}
            n = 0
            for k, row in self.path_rows.items():
                v = found.get(k) or ""
                if v:
                    row.set_value(v)
                    n += 1
            self.lbl_paths.setText(f"探测到 {n} 项，检查一下再保存")

        self.lbl_paths.setText("探测中…")
        self.api.request_once("/api/paths", callback=done)

    def _save_paths(self) -> None:
        body = {k: row.value() for k, row in self.path_rows.items()}

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                miss = data.get("missing") or []
                if miss:
                    self.lbl_paths.setText(f"已保存，但还缺 {len(miss)} 项")
                else:
                    self.lbl_paths.setText("已保存")
            else:
                self.lbl_paths.setText("保存失败")

        self.api.request_once("/api/paths", body, callback=done)

    def _load_profiles(self, attempt: int = 0) -> None:
        """拉配置列表，渲染出来。

        ⚠️ 失败不能就这样算了。
            实测：桌面端刚打开时，如果接口服务还没起来（或者刚好在重启），
            这个请求会失败。原来的代码失败就 return，
            界面上永远显示"还没有配置"——用户以为配置丢了。

            现在：失败就重试（最多 4 次，间隔递增），
            全失败了才显示一句明确的错误，而不是假装空。
        """
        def on_fail(msg: str):
            if attempt < 3:
                # 接口可能还在启动，等一下再试
                QTimer.singleShot(
                    800 * (attempt + 1),
                    lambda: self._load_profiles(attempt + 1),
                )
                if attempt == 0:
                    self.lbl_engine.setText("正在连后台…")
            else:
                self.lbl_engine.setText(f"读不到配置：{msg}")
                # 显示一个明确的状态，别让用户以为配置丢了
                self._profile_error = msg
                self._render_profiles()

        # 每次请求带个序号，只认最后一次的结果。
        # 切页快了的话会有多个请求在飞，老的晚到会把新的覆盖掉——
        # 表现是"刚选的那套又跳回去了"。
        self._prof_seq = getattr(self, "_prof_seq", 0) + 1
        my_seq = self._prof_seq

        def done(data):
            if my_seq != getattr(self, "_prof_seq", 0):
                return  # 已经有更新的请求了，这个结果不要
            if not isinstance(data, dict) or not data.get("ok"):
                on_fail((data or {}).get("error") or "接口没回应")
                return
            self._profile_error = ""
            self._profiles = data.get("profiles") or []
            self._active_pid = data.get("active") or ""
            self._render_profiles()

            # 当前那套填进编辑区
            cur = next(
                (x for x in self._profiles if x["id"] == self._active_pid),
                self._profiles[0] if self._profiles else None,
            )
            if cur:
                self._selected_pid = cur["id"]
                self._fill_editor(cur)

        self.api.request_once("/api/engine/profiles", callback=done)

    def _render_profiles(self) -> None:
        """把配置列表画出来。

        每行三个信息：是不是当前在用的（绿点）、叫什么、用哪个模型。
        不在用的那套给一个「启用」小按钮；在用的只显示状态，不给按钮。

        样式上特意做了三件事：
            · 每行高度统一，不因为有没有警告标签而参差
            · 选中的那行用左侧竖条标识，不用整行灰底（灰底看着像禁用）
            · 「缺 Key」放到行尾固定位置，不挤着模型名
        """
        while self.profile_box.count():
            it = self.profile_box.takeAt(0)
            w = it.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        if not getattr(self, "_profiles", None):
            err = getattr(self, "_profile_error", "")
            lb = QLabel(
                f"读不到配置：{err}（后台可能还没起来，重开一下这个窗口）"
                if err else
                "还没有配置。点下面「＋ 新增一套」。"
            )
            lb.setFont(font(11))
            lb.setWordWrap(True)
            lb.setStyleSheet(
                f"color:{C_WARN if err else C_DIM2}; padding:8px 0;"
            )
            self.profile_box.addWidget(lb)
            return

        for prof in self._profiles:
            is_active = prof["id"] == self._active_pid
            is_sel = prof["id"] == getattr(self, "_selected_pid", "")

            row = QFrame()
            row.setObjectName("profrow")
            row.setCursor(Qt.PointingHandCursor)
            row.setFixedHeight(44)
            row.setStyleSheet(
                "QFrame#profrow {"
                f" background:{C_INPUT};"
                f" border:1px solid {C_LINE_HI if is_sel else C_LINE};"
                f" border-left:3px solid {C_ACCENT if is_sel else 'transparent'};"
                " border-radius:8px;"
                "}"
            )

            rl = QHBoxLayout(row)
            rl.setContentsMargins(12, 0, 12, 0)
            rl.setSpacing(10)

            # 状态点
            dot = GlowDot(C_OK if is_active else C_DIM2, 7)
            rl.addWidget(dot, 0, Qt.AlignVCenter)

            # 名字（固定宽度，让后面的对齐）
            nm = QLabel(prof.get("name") or "未命名")
            nm.setFont(font(12, QFont.DemiBold if is_active else QFont.Normal))
            nm.setStyleSheet(f"color:{C_TEXT if is_active else C_TEXT2};")
            nm.setFixedWidth(110)
            rl.addWidget(nm)

            # 模型名
            md = QLabel(prof.get("model") or "(没填模型)")
            md.setFont(font(11))
            md.setStyleSheet(
                f"color:{C_DIM if prof.get('model') else C_DIM2};"
            )
            rl.addWidget(md, 1)

            # 缺 Key 提示：固定位置，不挤着别的
            if not prof.get("has_key"):
                warn = QLabel("缺 Key")
                warn.setFont(font(10))
                warn.setStyleSheet(f"color:{C_WARN};")
                warn.setFixedWidth(48)
                warn.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                rl.addWidget(warn)

            # 右侧：在用的显示状态，不在用的显示启用按钮
            if is_active:
                tag = QLabel("✓ 正在用")
                tag.setFont(font(10))
                tag.setStyleSheet(f"color:{C_OK};")
                tag.setFixedWidth(88)
                tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                rl.addWidget(tag)
            else:
                act = QPushButton("启用")
                act.setObjectName("ghost")
                act.setCursor(Qt.PointingHandCursor)
                act.setFixedSize(64, 26)
                act.clicked.connect(
                    lambda _=False, pid=prof["id"]: self._switch_profile(pid)
                )
                rl.addWidget(act)

            def on_click(_e, pid=prof["id"]):
                self._selected_pid = pid
                self._render_profiles()
                cur = next((x for x in self._profiles if x["id"] == pid), None)
                if cur:
                    self._fill_editor(cur)

            row.mousePressEvent = on_click
            self.profile_box.addWidget(row)

    def _fill_editor(self, prof: dict) -> None:
        """把某一套填进下面的编辑区。"""
        self.edit_title.setText(f"改「{prof.get('name')}」")
        self.ed_base.setText(prof.get("base_url") or "")
        self._set_model_text(prof.get("model") or "")

        # 下拉框里先放当前模型，等测试连接再拉全量
        self.cmb_engine_model.clear()
        if prof.get("model"):
            self.cmb_engine_model.addItem(prof["model"])
            self.cmb_engine_model.setCurrentIndex(0)

        if prof.get("has_key"):
            self.ed_key.clear()
            self.ed_key.setPlaceholderText(f"已保存 · 尾号 {prof.get('key_tail')}")
            self.lbl_key_state.setText(
                f"✓ 已有 Key（尾号 {prof.get('key_tail')}）  要换就输入新的"
            )
            self.lbl_key_state.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")
            self.lbl_engine_now.setText(
                f"正在改：{prof.get('name')}｜{prof.get('model')}"
            )
        else:
            self.ed_key.clear()
            self.ed_key.setPlaceholderText("还没填")
            self.lbl_key_state.setText("✗ 这套还没填 Key")
            self.lbl_key_state.setStyleSheet(f"color:{C_BAD}; font-size:10.5px;")
            self.lbl_engine_now.setText(f"正在改：{prof.get('name')}")

        self.btn_del_profile.setEnabled(True)

    def _add_profile(self) -> None:
        """新增一套空白配置，然后让用户填。"""
        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已新增一套，填好信息点保存")
                self._load_profiles()
            else:
                self.lbl_engine.setText("新增失败")

        self.lbl_engine.setText("正在新增…")
        self.api.request_once("/api/engine/profiles",
                              {"action": "add", "name": "新配置"},
                              callback=done)

    def _delete_profile(self) -> None:
        pid = getattr(self, "_selected_pid", "")
        if not pid:
            return
        prof = next((x for x in getattr(self, "_profiles", []) if x["id"] == pid), None)
        name = prof.get("name") if prof else pid

        if QMessageBox.question(
            self, "确认", f"删掉「{name}」这套配置？"
        ) != QMessageBox.Yes:
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已删除")
                self._selected_pid = ""
                self._load_profiles()
            else:
                self.lbl_engine.setText("删除失败")

        self.api.request_once("/api/engine/profiles",
                              {"action": "delete", "id": pid},
                              callback=done)

    def _switch_profile(self, pid: str) -> None:
        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已切换")
                self._load_profiles()
            else:
                self.lbl_engine.setText("切换失败")

        self.api.request_once("/api/engine/profiles",
                              {"action": "switch", "id": pid},
                              callback=done)

    def _load_engine(self) -> None:
        """把当前的模型配置读出来填进界面。key 不显示原文。"""
        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                return
            self.ed_base.setText(data.get("base_url") or "")
            self._set_model_text(data.get("model") or "")
            if data.get("has_key"):
                # placeholder 要短。
                # 原来写"已设置（尾号 40e0），留空表示不改"，21 个字，
                # 在 32px 高的输入框里被截断，用户只看到前半截，
                # 以为这儿没值。提示信息挪到下面那行状态标签里。
                self.ed_key.setPlaceholderText(f"已保存 · 尾号 {data.get('key_tail')}")
                self.lbl_key_state.setText(
                    f"✓ 已有 Key（尾号 {data.get('key_tail')}）  要换就输入新的，留空表示不改"
                )
                self.lbl_key_state.setStyleSheet(f"color:{C_OK}; font-size:10.5px;")
                self.lbl_engine_now.setText(
                    f"正在用 {data.get('model')}"
                )
            else:
                self.ed_key.setPlaceholderText("还没填")
                self.lbl_key_state.setText("✗ 还没填 Key")
                self.lbl_key_state.setStyleSheet(f"color:{C_BAD}; font-size:10.5px;")
                self.lbl_engine_now.setText("还没配。填完才能派任务。")

        self.api.request_once("/api/engine", callback=done)

    def _model_text(self) -> str:
        """当前选的模型名。"""
        return self.cmb_engine_model.currentText().strip()

    def _set_model_text(self, name: str) -> None:
        """把模型名放进下拉框。列表里有就选中，没有就填进去。"""
        if not name:
            return
        idx = self.cmb_engine_model.findText(name)
        if idx >= 0:
            self.cmb_engine_model.setCurrentIndex(idx)
        else:
            self.cmb_engine_model.setEditText(name)

    def _fetch_models(self) -> None:
        """去接口问有哪些模型，填进下拉框。

        这是「测试连接」之前的一步：知道有哪些模型，
        用户就不用猜名字了——猜错一个字母就 404。
        """
        base = self.ed_base.text().strip()
        key = self.ed_key.text().strip()
        if not base or not key:
            return

        self.lbl_engine.setText("正在查可用模型…")

        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                # 查不到就让用户手打，不纠缠
                self.lbl_engine.setText("查不到模型列表，手动填模型名吧")
                return
            models = data.get("models") or []
            cur = self._model_text()

            self.cmb_engine_model.blockSignals(True)
            self.cmb_engine_model.clear()
            self.cmb_engine_model.addItems(models)
            self.cmb_engine_model.blockSignals(False)

            # 现在用的模型如果在列表里就选它，否则用接口推荐的默认值
            pick = cur if cur in models else (data.get("default") or "")
            if pick:
                self._set_model_text(pick)

            self.lbl_engine.setText(f"查到 {len(models)} 个模型，选好后点测试")

        self.api.request_once(
            "/api/engine/models",
            {"base_url": base, "api_key": key},
            timeout=40, callback=done,
        )

    def _save_engine(self) -> None:
        """保存到当前选中的那套配置。"""
        pid = getattr(self, "_selected_pid", "")
        if not pid:
            self.lbl_engine.setText("先新增一套配置")
            return

        body = {
            "action": "update",
            "id": pid,
            "base_url": self.ed_base.text().strip(),
            "model": self._model_text(),
            "api_key": self.ed_key.text().strip(),
        }
        if not body["base_url"] or not body["model"]:
            self.lbl_engine.setText("接口地址和模型名都得填")
            return

        def done(data):
            if isinstance(data, dict) and data.get("ok"):
                self.lbl_engine.setText("已保存")
                self.ed_key.clear()
                self._load_profiles()
            else:
                self.lbl_engine.setText("保存失败")

        self.lbl_engine.setText("保存中…")
        self.api.request_once("/api/engine/profiles", body, callback=done)

    def _test_engine(self) -> None:
        """真的发一次请求，确认这套配置能用。

        顺手拉一遍模型列表：如果用户填的模型名在列表里没有，
        说明名字写错了，当场就能看出来，不用等任务报 404。
        """
        self._fetch_models()
        body = {
            "base_url": self.ed_base.text().strip(),
            "model": self._model_text(),
            "api_key": self.ed_key.text().strip(),
        }
        self.lbl_engine.setText("正在连…")

        def done(data):
            if not isinstance(data, dict):
                self.lbl_engine.setText("没收到回应")
                return
            if data.get("ok"):
                self.lbl_engine.setText(data.get("message") or "通了")
            else:
                self.lbl_engine.setText(data.get("message") or data.get("error") or "不通")

        self.api.request_once("/api/engine/test", body, timeout=60, callback=done)

    def _refresh_autostart(self) -> None:
        """问一下现在是不是开机自启，刷新开关外观。"""
        def done(data):
            on = bool((data or {}).get("enabled"))
            self.switch_auto.setChecked(on)
            self.switch_auto.setObjectName("switchon" if on else "switchoff")
            self.switch_auto.setText("开机自启 · 开" if on else "开机自启 · 关")
            # objectName 变了要重新套一遍样式
            self.switch_auto.style().unpolish(self.switch_auto)
            self.switch_auto.style().polish(self.switch_auto)

        self.api.request_once("/api/autostart", callback=done)

    def _toggle_autostart(self) -> None:
        """点了开关：按当前状态翻转。

        ⚠️ 光靠 clicked 信号的默认行为不可靠：
            按钮是可 checkable 的，Qt 会自己翻转一次，
            如果这里再翻转一次就会来回抵消。
            所以统一以"服务端现在是什么状态"为准，取了再设成相反值。
        """
        want = not self.switch_auto.isChecked()

        def done(data):
            ok = bool((data or {}).get("ok"))
            if not ok:
                self.lbl_auto.setText((data or {}).get("error") or "设置失败")
            else:
                self.lbl_auto.setText((data or {}).get("note") or "")
            self._refresh_autostart()

        self.api.request_once("/api/autostart", {"on": want}, callback=done)

    def _load_paths(self) -> None:
        """打开设置页时把当前路径填进去。"""
        def done(data):
            if not isinstance(data, dict) or not data.get("ok"):
                return
            vals = data.get("values") or {}
            for k, row in self.path_rows.items():
                row.set_value(vals.get(k) or "")
            miss = data.get("missing") or []
            if miss:
                names = [data.get("labels", {}).get(m, m) for m in miss]
                self.lbl_paths.setText("还缺：" + "、".join(names))
            else:
                self.lbl_paths.setText("")

        self.api.request_once("/api/paths", callback=done)

    def _load_model(self) -> None:
        self.api.get("/api/model")

    def _save_model(self) -> None:
        self.api.post("/api/model", {
            "preset": self.cmb_model.currentData(),
            "effort": self.cmb_effort.currentData(),
        })
        QTimer.singleShot(800, self._load_model)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("dsh-bridge")
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
