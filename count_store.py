"""防篡改的计数存储。

要解决的具体问题
    "今天被拦了几次"这个数字，必须能挡住**任务端那个 AI**。

    因为它有完全权限——能读写文件、能跑命令。如果数字存在一个
    普通文件里，攻击者只要骗前台派一个活：

        "把 guard_count.json 里的数字改成 0"

    任务端照做，计数清零，整个"三次就锁死"的机制就废了。

    威胁模型不是"本机用户手动改"（那挡不住，也不必要），
    而是"通过派活让后台去改"。

做法（借鉴单机游戏存档的思路）
    你玩的那些 RPG，金币数字就在存档里，你搜得到、改得动，
    但改完游戏里还是原来的数，重启还会被写回去。

    它们靠的不是加密——数字就是明文。靠的是：

        **几处数据互相印证，对不上就回滚。**

    这里也这么做：

        主文件      %APPDATA%\\<app>\\guard_count.json       看起来就是个普通 json
        影子副本    %LOCALAPPDATA%\\<app>\\.syscache        名字伪装成缓存
        签名        基于前两者 + 密钥算的

    任务端的行为会是这样：

        只改主文件   → 影子副本还是老值 → 对不上 → 用影子恢复
        只改影子     → 主文件还是老值   → 对不上 → 恢复
        两个都改     → 签名对不上       → 恢复
        三样一起改   → **算不出签名**（密钥不在它手上）→ 恢复

    而且每次发现被篡改都会记一笔，次数也加进计数——
    换句话说，**试图篡改本身就是一次"不安全操作"**。

密钥从哪来
    第一次启动时随机生成 32 字节，然后：
        · 用机器特征（主机名 + 用户名 + 卷序列号）参与派生
        · 存成文件时用 DPAPI 加密（Windows 上绑定当前用户）

    现实一点说：任务端跟桥是同一个用户、同一台机器，
    **它真想拿是拿得到的**。这个方案的定位是：

        挡住"顺手改一下" —— 成本很低就能得手的事，变成要花功夫
        同时：一旦被改，能发现、能回滚、能算作一次违规

    不是绝对安全。绝对安全在同用户同机器上不存在。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile

# ── 文件位置 ────────────────────────────────────────────

def _root() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "dsh-bridge")


def _local_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or tempfile.gettempdir()
    return os.path.join(base, "dsh-bridge")


def main_path() -> str:
    return os.path.join(_root(), "guard_count.json")


def shadow_path() -> str:
    """影子副本。

    名字故意起得像系统缓存——"改了它也是白改"这件事，
    最好连被发现都别太容易。
    """
    return os.path.join(_local_root(), ".syscache")


def key_path() -> str:
    return os.path.join(_root(), ".guard_key")


# ── 密钥 ────────────────────────────────────────────────

def _machine_fingerprint() -> bytes:
    """机器的一些特征，用来参与密钥派生。

    为什么要参与：
        只有随机密钥的话，把整份数据连密钥一起复制到另一台机器上，
        就等于"带着一个假的计数"过去了。
        掺进机器特征之后，换台机器解不开。

    ⚠️ 这里面任何一项变了（比如改了电脑名），密钥就变了，
        原来的计数就读不出来——会被当成"数据损坏"。
        那种情况下按"从零开始"处理，不会锁死用户。
    """
    parts = [
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", ""),
        sys.platform,
    ]
    try:
        parts.append(os.path.expanduser("~"))
    except Exception:  # noqa: BLE001
        pass
    return "|".join(parts).encode("utf-8", "replace")


def _dpapi_protect(data: bytes) -> bytes:
    """用 Windows DPAPI 加密。绑当前用户。

    非 Windows 直接返回原文（这个项目主要在 Windows 上跑，
    别的平台退化成"没有加密"，但校验还在）。
    """
    if sys.platform != "win32":
        return data

    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        blob_in = DATA_BLOB(
            len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char))
        )
        blob_out = DATA_BLOB()

        ok = crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out),
        )
        if not ok:
            return data

        try:
            out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return out
    except Exception:  # noqa: BLE001
        return data


def _dpapi_unprotect(data: bytes) -> bytes:
    """DPAPI 解密。失败就原样返回（调用方自己判断能不能用）。"""
    if sys.platform != "win32":
        return data

    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32

        blob_in = DATA_BLOB(
            len(data), ctypes.cast(ctypes.c_char_p(data), ctypes.POINTER(ctypes.c_char))
        )
        blob_out = DATA_BLOB()

        ok = crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out),
        )
        if not ok:
            return data

        try:
            out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return out
    except Exception:  # noqa: BLE001
        return data


def _load_key() -> bytes:
    """拿密钥。没有就生成一个。

    密钥 = 随机 32 字节 + 机器特征，DPAPI 加密后存盘。
    """
    p = key_path()
    raw = b""

    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                raw = f.read()
        except OSError:
            raw = b""

    if raw:
        dec = _dpapi_unprotect(base64.b64decode(raw))
        if len(dec) >= 32:
            # 跟当前机器特征混一下
            return hmac.new(
                dec[:32], _machine_fingerprint(), hashlib.sha256
            ).digest()

    # 第一次（或者解不开）：造一个新的
    rnd = secrets.token_bytes(32)
    try:
        os.makedirs(_root(), exist_ok=True)
        enc = _dpapi_protect(rnd)
        with open(p, "wb") as f:
            f.write(base64.b64encode(enc))
        _tighten(p)
    except OSError:
        pass

    return hmac.new(rnd, _machine_fingerprint(), hashlib.sha256).digest()


def _tighten(path: str) -> None:
    """把文件权限收紧到只有当前用户能读写。"""
    if sys.platform != "win32":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    try:
        import subprocess

        user = os.environ.get("USERNAME") or ""
        if user:
            subprocess.run(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{user}:F"],
                capture_output=True,
                timeout=10,
                creationflags=0x08000000,
            )
    except Exception:  # noqa: BLE001
        pass


# ── 计数数据 ────────────────────────────────────────────

def _marker_path() -> str:
    """一个"这台机器装过"的痕迹。

    ⚠️ 这是防"把文件删掉就当第一次运行"的关键。

        原来 load() 的逻辑是：两个文件都不存在 → 当成首次启动 → 计数 0。
        攻击者只要把主文件和影子都删了，计数就归零了。

        所以另存一个极小的标记文件。它不存计数，只说明
        "这个计数机制在这台机器上跑过"。

        删掉主文件和影子，标记还在 → 说明不是第一次 → 按篡改处理。
        连标记一起删？可以，但那样 tamper 也归零了——
        至少它得动三个地方，而不是一个。

    标记是空的（0 字节），没有信息可读，也没法"改成假的值"。
    它存在的意义只有一个：**在不在**。
    """
    return os.path.join(_root(), ".initialized")


def _has_marker() -> bool:
    return os.path.exists(_marker_path())


def _make_marker() -> None:
    try:
        os.makedirs(_root(), exist_ok=True)
        if not os.path.exists(_marker_path()):
            with open(_marker_path(), "wb"):
                pass
            _tighten(_marker_path())
    except OSError:
        pass


def _empty() -> dict:
    return {
        "date": "",          # 哪个自然日（北京时间）
        "count": 0,          # 当天被拦了几次
        "tamper": 0,         # 被篡改过几次（累计）
        "updated": "",
    }


def _canonical(d: dict) -> bytes:
    """把数据变成稳定的字节串，用来算签名。"""
    return json.dumps(
        {
            "date": d.get("date", ""),
            "count": int(d.get("count", 0)),
            "tamper": int(d.get("tamper", 0)),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _sign(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _read_json(p: str) -> dict | None:
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _write_json(p: str, d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        _tighten(p)
    except OSError:
        pass


def _write_shadow(d: dict, sig: str) -> bool:
    """写影子副本。

    格式故意做得不像 json：一行 base64，看着像缓存。
    内容和主文件一样，但它带着签名。
    """
    try:
        os.makedirs(_local_root(), exist_ok=True)
        payload = json.dumps(
            {"d": d, "s": sig}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        blob = base64.b64encode(payload)
        with open(shadow_path(), "wb") as f:
            f.write(blob)
        return True

        # ⚠️ 原来这里还加了 attrib +h +s（隐藏 + 系统），
        #    想着让它"不被顺手发现"。
        #
        #    结果把自己坑了：加了系统属性之后，后面的覆盖写会失败，
        #    于是每次 load 都读不到影子，判定成"影子坏了"，
        #    回滚时把主文件也覆盖成旧值——计数永远停在 1。
        #
        #    文件名本身就够不显眼了（.syscache），不加属性了。
    except OSError:
        return False


def _read_shadow() -> tuple[dict | None, str]:
    try:
        with open(shadow_path(), "rb") as f:
            raw = f.read()
        d = json.loads(base64.b64decode(raw).decode("utf-8"))
        if not isinstance(d, dict):
            return None, ""
        return d.get("d"), str(d.get("s") or "")
    except (OSError, ValueError, json.JSONDecodeError):
        return None, ""


# ── 对外接口 ────────────────────────────────────────────

def load() -> dict:
    """读计数。

    三方对照：主文件 / 影子 / 签名。
    对不上就回滚到可信那份，并把 tamper 加一。
    """
    key = _load_key()

    main = _read_json(main_path())
    shadow, shadow_sig = _read_shadow()

    # 影子可信吗
    shadow_ok = bool(
        shadow is not None
        and shadow_sig
        and hmac.compare_digest(shadow_sig, _sign(key, _canonical(shadow)))
    )

    # 主文件可信吗
    main_ok = False
    if main is not None:
        main_sig = str(main.get("sig") or "")
        main_ok = bool(
            main_sig and hmac.compare_digest(main_sig, _sign(key, _canonical(main)))
        )

    if main_ok and shadow_ok:
        # 两边都可信。得内容也一致，否则说明"只改了其中一边还顺手重签了"
        if _canonical(main) == _canonical(shadow):
            return main

        # 内容不一致：以影子为准（影子藏在更深、更不显眼的地方）
        d = dict(shadow)
        d["tamper"] = int(d.get("tamper", 0)) + 1
        _persist(d, key)
        return d

    if shadow_ok:
        # 主文件坏了，用影子恢复
        d = dict(shadow)
        d["tamper"] = int(d.get("tamper", 0)) + 1
        _persist(d, key)
        return d

    if main_ok:
        # 影子读不到或者坏了。
        #
        # ⚠️ 这里要分两种情况：
        #     影子**不存在**   → 数据还没写全（比如第一次写的中间），
        #                        直接补一份，不算篡改。
        #     影子**存在但坏了** → 确实被动了，算一次篡改。
        #
        #     原来不分，一律 +1 tamper 然后重写。
        #     结果每次 load 都被算成篡改，计数也保不住。
        d = dict(main)
        if os.path.exists(shadow_path()):
            d["tamper"] = int(d.get("tamper", 0)) + 1
        _persist(d, key)
        return d

    # 两边都不可信 —— 可能是第一次运行，也可能被彻底破坏了。
    #
    # ⚠️ 这里的选择很要紧：
    #     如果"两边都坏"就当成"从头开始"，那攻击者只要把两个文件都删掉，
    #     计数就归零了——一秒钟的事。
    #
    #     所以多加一个标记文件（.initialized，0 字节，只表示"跑过"）。
    #     标记在，说明不是第一次；主文件和影子都没了，那就是被人删了。
    main_gone = not os.path.exists(main_path())
    shadow_gone = not os.path.exists(shadow_path())
    both_gone = main_gone and shadow_gone

    if both_gone and not _has_marker():
        # 真的第一次
        d = _empty()
        d["date"] = _today()
        _persist(d, key)
        _make_marker()
        return d

    # 到这儿说明：要么文件被删了，要么两边都被改了签名。
    #
    # ⚠️ 这两种要分开处理：
    #     被删了     → 没有原始数据可恢复，只能保守处理（按拦满算）
    #     被改了     → 文件还在，只是签名不对。这种情况下
    #                  恢复不了原值，但**不该把已有计数丢掉**。
    #
    #     原来不分，一律设成 3。后果：
    #       改了两边之后 tamper 从 2 掉回 1——痕迹也没了。
    #       而且如果本来就是 count=1，被改成 3 反而"跳级"了。
    if both_gone:
        # 被删了：从满额开始
        d = _empty()
        d["tamper"] = 1
        d["count"] = 3
        d["date"] = _today()
        _persist(d, key)
        _make_marker()
        return d

    # 文件还在但签名不对：尽量保住能读出来的信息。
    #
    # 从两个文件里各挑能读的字段（哪怕签名不可信），
    # 取**更大的那个 count**——攻击者想把数改小，这里不会让步。
    guess = _empty()
    guess["date"] = _today()
    for candidate in (main, shadow):
        if not candidate:
            continue
        try:
            c = int(candidate.get("count", 0))
            t = int(candidate.get("tamper", 0))
        except (TypeError, ValueError):
            continue
        guess["count"] = max(int(guess.get("count", 0)), c)
        guess["tamper"] = max(int(guess.get("tamper", 0)), t) + 1

    if int(guess.get("tamper", 0)) == 0:
        guess["tamper"] = 1

    # ⚠️ 关键的一步：**两边签名都不对的时候，一律按拦满处理。**
    #
    #    为什么不能"保持读到的值"：
    #        两个文件被同时改掉之后，原值只存在于签名里，
    #        而签名已经对不上了——原值**无法恢复**。
    #
    #        这时候如果"读到什么就信什么"，那攻击者的做法就是：
    #            把主文件改成 0，把影子改成 0，签名随便写个假的
    #            → 我们读出来 0 → 计数清零，得手。
    #
    #        所以必须保守：只要发现两边签名都不对，
    #        就当"已经拦满三次"，直接锁死。
    #
    #        宁可误锁一次（用户走验证码解锁就好），
    #        也不能让篡改占到便宜。
    guess["count"] = max(int(guess.get("count", 0)), 3)
    guess["date"] = _today()

    _persist(guess, key)
    _make_marker()
    return guess


def save(d: dict) -> None:
    """存计数。两边一起写。"""
    key = _load_key()
    _persist(d, key)
    _make_marker()


def _persist(d: dict, key: bytes) -> None:
    """把数据同时写到主文件和影子，带签名。"""
    clean = {
        "date": str(d.get("date", "")),
        "count": int(d.get("count", 0)),
        "tamper": int(d.get("tamper", 0)),
        "updated": str(d.get("updated", "")),
    }
    sig = _sign(key, _canonical(clean))
    clean["sig"] = sig

    _write_json(main_path(), clean)
    _write_shadow(
        {k: clean[k] for k in ("date", "count", "tamper", "updated")}, sig
    )


def _today() -> str:
    """今天的日期（北京时间）。

    ⚠️ 必须用北京时间，不能用服务器本地时区——
        用户的机器可能设成别的时区，那样"自然日"就变了。
    """
    from datetime import datetime, timedelta, timezone

    bj = timezone(timedelta(hours=8))
    return datetime.now(bj).strftime("%Y-%m-%d")


def bump() -> dict:
    """记一次拦截。返回更新后的计数。"""
    d = load()

    today = _today()
    if d.get("date") != today:
        # 换天了，计数从头来
        d["date"] = today
        d["count"] = 0

    d["count"] = int(d.get("count", 0)) + 1
    d["updated"] = _now()
    save(d)
    return d


def peek() -> dict:
    """看一眼计数，不改。"""
    d = load()
    today = _today()
    if d.get("date") != today:
        d = dict(d)
        d["date"] = today
        d["count"] = 0
    return d


def reset_count() -> dict:
    """把当天计数清零（验证码通过、或者锁满之后调）。

    ⚠️ 只清 count，不清 tamper。
        tamper 是"这条机器被篡改过几次"的累计，不该被清零。
    """
    d = load()
    d["date"] = _today()
    d["count"] = 0
    d["updated"] = _now()
    save(d)
    return d


def _now() -> str:
    from datetime import datetime, timedelta, timezone

    bj = timezone(timedelta(hours=8))
    return datetime.now(bj).strftime("%Y-%m-%dT%H:%M:%S")
