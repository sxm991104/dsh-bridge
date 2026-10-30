"""接口安全层的回归测试。

这块被自攻过 20 轮，找到过 5 个真漏洞。所以这里的测试
按"攻击者会怎么试"来写，而不是"功能对不对"。

重点：
    · 身份认证能不能被绕过
    · 浏览器侧攻击（CSRF / DNS rebinding）
    · 请求体炸弹 / 畸形请求
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import security  # noqa: E402


# ══════════════════════════════════════════════════════
# 本机地址判定
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("addr", [
    "127.0.0.1",
    "::1",
    "localhost",
    "::ffff:127.0.0.1",     # IPv4-mapped
    "[::1]",                # 带方括号的 IPv6（红队 F 组）
    "127.0.0.1:6186",       # 带端口（红队 F 组）
])
def test_local_address_accepted(addr: str) -> None:
    assert security.is_local_address(addr) is True


@pytest.mark.parametrize("addr", [
    "",
    None,
    "0.0.0.0",
    "192.168.1.1",
    "8.8.8.8",
    "10.0.0.1",
    "::ffff:192.168.1.1",
    "127.0.0.2",            # 这个技术上也是 loopback，但我们只认 .1
    "127.0.0.1.evil.com",   # 域名伪装
    "localhost.evil.com",
    " 127.0.0.1 ",          # 带空白（红队 F 组：这个参数来自 socket，不该有空白）
    "127.0.0.1\n",          # 带换行
])
def test_non_local_rejected(addr) -> None:
    assert security.is_local_address(addr) is False


# ══════════════════════════════════════════════════════
# 浏览器请求识别
# ══════════════════════════════════════════════════════

class FakeHeaders(dict):
    """模拟 HTTP 头。

    大小写不敏感——真实 HTTP 头就是这样。
    """
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


def test_origin_detected() -> None:
    """带 Origin 的请求被识别成浏览器。"""
    h = FakeHeaders({"Origin": "https://evil.com"})
    assert security.has_browser_origin(h) is True


def test_sec_fetch_site_detected() -> None:
    """带 Sec-Fetch-Site 的也算浏览器。"""
    h = FakeHeaders({"Sec-Fetch-Site": "cross-site"})
    assert security.has_browser_origin(h) is True


def test_no_browser_headers() -> None:
    """本机程序不带这些头。"""
    h = FakeHeaders({"Content-Type": "application/json"})
    assert security.has_browser_origin(h) is False


def test_empty_headers() -> None:
    assert security.has_browser_origin(FakeHeaders()) is False


def test_both_headers() -> None:
    """两个都带也认。"""
    h = FakeHeaders({
        "Origin": "https://evil.com",
        "Sec-Fetch-Site": "cross-site",
    })
    assert security.has_browser_origin(h) is True


# ══════════════════════════════════════════════════════
# Host 头（防 DNS rebinding）
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "127.0.0.1:8790",
    "localhost",
    "localhost:8790",
    "[::1]",
    "[::1]:8790",
    "LOCALHOST",
    "127.0.0.1:1",
])
def test_local_host_accepted(host: str) -> None:
    h = FakeHeaders({"Host": host})
    assert security.host_is_local(h) is True


@pytest.mark.parametrize("host", [
    "evil.com",
    "evil.com:8790",
    "192.168.1.61",
    "192.168.1.61:8790",
    "attacker.localhost.evil.com",
    "localhost.evil.com",
    "127.0.0.1.evil.com",
    "evil.com#localhost",
])
def test_evil_host_rejected(host: str) -> None:
    """DNS rebinding 的典型特征：Host 头不是本机名。"""
    h = FakeHeaders({"Host": host})
    assert security.host_is_local(h) is False


def test_no_host_header_rejected() -> None:
    """没有 Host 头时拒绝。

    ⚠️ 这条原来叫 test_no_host_header_allowed，断言"放行"。

        当时的理由是：HTTP/1.0 或者手工构造的请求可能没有 Host，
        这种请求不是正经浏览器发的，后面还有 Origin 和 token 两道门。

        红队报告 F-07 指出这是 fail-open，对：

            · HTTP/1.1 的请求**必须**带 Host 头（RFC 7230 §5.4）
            · 能发出"没有 Host 头"的，只有手工构造的 HTTP/1.0 请求
              或者极老的客户端
            · 正常的 curl / requests / 浏览器 / 桌面端都必带

        安全代码里拿不准的时候应该选拒。
        "放行给后面几层"等于把判断推给不检查 Host 的层。

        实测过：改完之后 requests / curl / 桌面端全正常。
    """
    h = FakeHeaders()
    assert security.host_is_local(h) is False


def test_empty_host_rejected() -> None:
    """空 Host 头同样拒绝。"""
    h = FakeHeaders({"Host": "   "})
    assert security.host_is_local(h) is False


# ══════════════════════════════════════════════════════
# token 比较
# ══════════════════════════════════════════════════════

def test_token_match() -> None:
    assert security.token_matches("abc123", "abc123") is True


def test_token_mismatch() -> None:
    assert security.token_matches("abc123", "abc124") is False


def test_empty_token_never_matches() -> None:
    """★ 空 token 一律不匹配。

    这里踩过一个大坑：
        原来在 _guard 里写的是 `if want and got != want`，
        want 为空时整句短路成 False——**等于放行所有人**。
        现在比较逻辑放在 token_matches 里，空的一律判死。
    """
    assert security.token_matches("", "") is False
    assert security.token_matches("", "abc") is False
    assert security.token_matches("abc", "") is False


def test_token_case_sensitive() -> None:
    """token 大小写敏感。"""
    assert security.token_matches("ABC", "abc") is False


def test_token_prefix_not_enough() -> None:
    """前缀对不代表全对。"""
    full = "abcdefghijklmnop"
    assert security.token_matches(full[:8], full) is False


# ══════════════════════════════════════════════════════
# 常量
# ══════════════════════════════════════════════════════

def test_host_is_hardcoded() -> None:
    """绑定地址必须是 127.0.0.1。"""
    assert security.HOST == "127.0.0.1"
