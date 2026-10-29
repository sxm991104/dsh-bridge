"""本机接口的安全防护。

背景
    这个程序跑在用户自己的电脑上，接口本来是给桌面端用的。
    但它能做的事很重：派任务、读写文件、跑命令、开关平台。

    如果哪天暴露到公网，等于把整台电脑交出去。

防护分三层，一层不够就用两层，能加就加：

    第一层  绑死 127.0.0.1
            代码里写死，不接受任何参数改。
            就算有人改了配置文件也绑不到外网。

    第二层  本机 token
            第一次启动随机生成，存本地文件（只有当前用户读得到）。
            桌面端从文件读，请求时带上。
            别的程序就算知道端口，没有 token 也调不动。

    第三层  拦掉浏览器跨源请求
            Web 页面的 fetch 会带 Origin 头，本机程序不会。
            带 Origin 的一律拒——防止用户打开一个恶意网页，
            那个网页偷偷往 localhost:8790 发请求。

为什么第三层是必须的
    浏览器对 localhost 的请求**不受同源策略限制发不出去**——
    能发，只是读不到响应。而派活不需要读响应。
    所以一个恶意网页只要知道端口，就能 POST 一个任务过来。
    token 能挡住它，但 Origin 检查是更早的一道门。
"""

from __future__ import annotations

import os
import secrets

# 绑死的地址。不给命令行参数、不给配置项——就是这么定的。
HOST = "127.0.0.1"

TOKEN_FILENAME = "api_token.txt"


def token_path(app_dir: str) -> str:
    """token 存哪。

    放 %APPDATA%\\dsh-bridge\\ 里，只有当前用户能读。
    不放程序目录——程序目录可能被同步、被备份、被发出去。
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, app_dir)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, TOKEN_FILENAME)


def load_or_create_token(app_dir: str) -> str:
    """读已有的 token，没有就生成一个。

    每次启动都复用同一个，不然桌面端每次都要重新读。
    """
    p = token_path(app_dir)
    try:
        if os.path.exists(p):
            t = open(p, encoding="utf-8").read().strip()
            if len(t) >= 20:
                return t
    except OSError:
        pass

    t = secrets.token_urlsafe(32)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(t)
        # Windows 上再收一下 ACL：只让当前用户读
        _restrict_to_me(p)
    except OSError:
        pass
    return t


def _restrict_to_me(path: str) -> None:
    """把文件权限收紧到只有当前用户能读。

    Windows 上 %APPDATA% 本身就在用户目录下，默认继承的权限
    已经比较严了。这里再显式设一次，防止父目录权限被改松。
    """
    try:
        import subprocess

        user = os.environ.get("USERNAME") or ""
        if not user:
            return
        subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{user}:F"],
            capture_output=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        pass


def is_local_address(addr: str) -> bool:
    """这个连进来的地址是不是本机。

    虽然绑了 127.0.0.1，理论上只可能是本机。
    但多一道检查没坏处——万一哪天绑定被改了，
    有这个检查还能挡住。
    """
    if not addr:
        return False
    a = addr.strip()
    if a.startswith("::ffff:"):
        a = a[len("::ffff:"):]
    return a in ("127.0.0.1", "::1", "localhost")


def has_browser_origin(headers) -> bool:
    """请求有没有带浏览器才有的头。

    本机程序（Python、curl、桌面端）不会发 Origin，
    浏览器发的 fetch 一定会带。

    注意 Sec-Fetch-Site：现代浏览器发本地请求时会带这个，
    值是 cross-site 或者 same-site，也是网页发来的信号。
    """
    origin = headers.get("Origin")
    if origin:
        return True
    site = headers.get("Sec-Fetch-Site")
    return bool(site)

def host_is_local(headers) -> bool:
    """请求的 Host 头是不是本机。

    防的是 DNS rebinding：
        攻击者有个域名 evil.com，把它的 DNS 指到 127.0.0.1。
        用户打开 evil.com，浏览器认为自己在跟 evil.com 说话，
        里面发的请求 Host 头就是 evil.com——但实际打到了你本机的服务。

        这时候 Origin 也可能是 evil.com（被我们的规则拦了），
        但如果攻击者用 <img>、<script> 这种标签，
        浏览器根本不发 Origin / Sec-Fetch-Site。

        所以再加这道 Host 检查：只接受 localhost / 127.0.0.1 / [::1]。

    允许带端口（Host 头本来就是 "host:port" 的形式）。
    """
    host = (headers.get("Host") or "").strip().lower()
    if not host:
        # HTTP/1.0 或者手工构造的请求可能没有 Host。
        # 没有 Host 说明不是正经浏览器发的，放行给后面几层判断。
        return True

    # 去掉端口
    if host.startswith("["):
        # IPv6：[::1]:8790
        end = host.find("]")
        name = host[1:end] if end > 0 else host
    else:
        name = host.split(":", 1)[0]

    return name in ("127.0.0.1", "localhost", "::1")

def token_matches(got: str, want: str) -> bool:
    """比较 token，用恒定时间。

    用普通的 == 比较字符串时，Python 遇到第一个不同的字符就返回。
    理论上攻击者可以靠"比较耗时"一个字节一个字节地猜出 token。

    实际的网络环境下这个几乎不可能成功（噪声远大于差异），
    但这是标准做法，代价只是几十纳秒，没理由不做。

    ⚠️ 还有一个更要紧的点：
        如果 want 是空字符串，不能返回 True。
        token 没生成出来的时候（比如文件写失败），
        want 会是空——那等于"谁都能进"。
        这里直接判死。
    """
    if not want or not got:
        return False

    import hmac

    try:
        return hmac.compare_digest(got.encode("utf-8"), want.encode("utf-8"))
    except (TypeError, ValueError):
        return False
