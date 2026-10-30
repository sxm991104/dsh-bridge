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

    ⚠️ 原来这里直接调 icacls，没有平台判断。
        在 Linux 上 icacls 不存在，subprocess 会抛 FileNotFoundError，
        被 except 吞掉——不会崩，但等于什么都没做。

        现在交给 platform_utils：Windows 走 icacls，Linux 走 chmod。
    """
    try:
        import platform_utils as _pu

        _pu.tighten_file(path)
    except ImportError:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def is_local_address(addr: str) -> bool:
    """这个连进来的地址是不是本机。

    虽然绑了 127.0.0.1，理论上只可能是本机。
    但多一道检查没坏处——万一哪天绑定被改了，
    有这个检查还能挡住。

    ⚠️ 红队报告 F 组指出：这里对各种地址写法的处理不全。

        实测漏掉的：
            '[::1]'              带方括号的 IPv6（URL 里就是这么写的）
            '127.0.0.1:6186'     带端口的

        这两个都是**合法的本机地址写法**，原来判成"不是本机"。
        方向安全（拒了），但一个叫 is_local_address 的函数
        不认识合法写法，说明它没想清楚自己在判断什么。

        还有三个是"判成是本机"的：
            '  127.0.0.1  '      前后有空格
            '127.0.0.1\n'        带换行
            '::ffff:127.0.0.1'   IPv4-mapped IPv6

        红队认为前两个该拒（"谁会在地址里加空格"）。
        我同意——这个参数来自 socket，正常不会有空白字符。
        见到空白说明来源可疑，拒掉比猜更有道理。

        第三个（::ffff:）**保持判为本机**：
        那是 IPv4 映射到 IPv6 的正常表示，Windows 上常见。
        红队那条是断言它应该 False，但我不同意——
        它指向的就是 127.0.0.1。
    """
    if not addr:
        return False

    # 空白字符一律拒。这个参数来自 socket，不该有空白。
    # 原来 strip() 之后就判，等于把 " 127.0.0.1\n" 当成合法地址。
    if addr != addr.strip():
        return False

    a = addr

    # 剥方括号：[::1] → ::1
    if a.startswith("[") and a.endswith("]"):
        a = a[1:-1]

    # 剥端口：127.0.0.1:6186 → 127.0.0.1
    # IPv6 里冒号是地址的一部分，所以只在"不是 IPv6"时才按端口剥。
    if a.count(":") == 1:
        a = a.split(":", 1)[0]

    # IPv4 映射到 IPv6
    if a.startswith("::ffff:"):
        a = a[len("::ffff:"):]

    return a in ("127.0.0.1", "::1", "localhost")


def has_browser_origin(headers) -> bool:
    """请求有没有带浏览器才有的头。

    本机程序（Python、curl、桌面端）不会发 Origin，
    浏览器发的 fetch 一定会带。

    ⚠️ 这里红队 F 组指了两条，我一条同意一条不同意。

        同意：
            Sec-Fetch-Mode: cors
                这是 fetch 特有的（还可能是 no-cors / navigate）。
                看到 cors 基本可以确定是网页在发请求。
                原来没看这个头，现在加上。

        不同意：
            Sec-Fetch-Site: none
                红队认为它有就是浏览器发的，应该拒。

                但 none 的含义是**用户直接在地址栏输入网址**
                （或者从书签打开）——不是网页发起的。

                把它当"网页请求"拒掉，等于误伤"手动打开 WebUI 看状态"
                这种完全正常的操作。

                我的判断：none 不该单独构成"这是网页攻击"的证据。

        另外保留：
            any Sec-Fetch-Site（包括 cross-site / same-site）
                这两种确实是从别的页面跳过来的，算网页请求。
            所以是"none 放过，其它值拦"，不是"有这个头就拦"。
    """
    origin = headers.get("Origin")
    if origin:
        return True

    # fetch 的特征
    mode = (headers.get("Sec-Fetch-Mode") or "").strip().lower()
    if mode in ("cors", "no-cors", "same-origin"):
        return True

    site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if not site:
        return False

    # none = 用户直接在地址栏输入 / 从书签打开，不是网页发起的
    return site != "none"

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

    ⚠️ 没有 Host 头时**拒绝**，不是放行。

        原来写的是：
            if not host:
                return True     # 放行，交给后面几层判断

        红队报告 F-07 指出这是 fail-open，对：
            HTTP/1.1 的请求**必须**带 Host 头（RFC 7230 §5.4），
            HTTP/1.0 可以不带的年代早就过去了。

            能发出"没有 Host 头"的请求的，只有：
                · 手工构造的 HTTP/1.0 请求（攻击者常用）
                · 极老的客户端
            正常的 curl / requests / 浏览器都必带。

        安全代码里，"拿不准"的时候应该选**拒**。
        放行等于把判断推给后面——而后面那几层不检查 Host。

        实测过：改完之后 requests / curl / 桌面端都正常。
    """
    host = (headers.get("Host") or "").strip().lower()
    if not host:
        # 没有 Host = 不是正经客户端发的，拒。
        # 见上面的说明：fail-closed 是安全代码该有的默认方向。
        return False

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
