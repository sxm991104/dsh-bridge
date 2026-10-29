"""路径安全的回归测试。

这块曾经真的被攻破过——任务名写 "../PWNED" 在桥目录外面
建出了文件夹。所以这里的测试要**压到极致**：
各种编码、各种平台的路径写法、各种诡异字符，全都得挡住。

同时也要保证**正常的名字不能被误伤**——误伤比漏放还烦人，
用户起个正常名字派不出去，会以为程序坏了。
"""

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from safe_path import is_inside, safe_join, safe_name  # noqa: E402


# ══════════════════════════════════════════════════════
# 正常名字：必须放行
# ══════════════════════════════════════════════════════

OK_NAMES = [
    "登录页",
    "登录 页面",
    "v1.2 修复",
    "登录（新版）",
    "项目【二期】",
    "[测试]",
    "test_01",
    "my-project",
    "A",
    "1",
    "中文English混合123",
    "带空格 的名字",
    "点.在.中间",
    "a" * 80,          # 正好 80 字
]


@pytest.mark.parametrize("name", OK_NAMES)
def test_good_names_pass(name: str) -> None:
    """这些都得能用。"""
    assert safe_name(name) == name.strip()


# ══════════════════════════════════════════════════════
# 路径穿越：必须拦住
# ══════════════════════════════════════════════════════

TRAVERSAL_NAMES = [
    # 经典
    "../PWNED",
    "../../PWNED",
    "../../../etc/passwd",
    "..\\PWNED",
    "..\\..\\windows",
    "a/../../PWNED",
    "a\\..\\..\\PWNED",
    # 绝对路径
    "C:\\Windows\\System32",
    "/etc/passwd",
    "\\\\server\\share",
    "//server/share",
    # 单点/双点
    "..",
    ".",
    "...",
    # 混合
    "test/../../PWNED",
    "test\\..\\..\\PWNED",
    "./PWNED",
    ".\\PWNED",
]


@pytest.mark.parametrize("name", TRAVERSAL_NAMES)
def test_traversal_blocked(name: str) -> None:
    """所有路径穿越尝试都得被拒。"""
    with pytest.raises(ValueError):
        safe_name(name)


# ══════════════════════════════════════════════════════
# Unicode 变体：看起来像点/斜杠，但编码不同
# ══════════════════════════════════════════════════════

UNICODE_ATTACKS = [
    "\uFF0E\uFF0EPWNED",       # 全角句号
    "\u2024\u2024/PWNED",       # 一点
    "..\uFF0FPWNED",            # 全角斜杠
    "..\u2215PWNED",            # 除号
    "..\uFF3CPWNED",            # 全角反斜杠
    "..\u200B/PWNED",           # 零宽空格
    "..\u200E/PWNED",           # 左至右标记
    "..\uFEFF/PWNED",           # BOM
    "a\u0301/../PWNED",         # 组合字符
    "\u002E\u002E/PWNED",       # 普通点（控制组）
]


@pytest.mark.parametrize("name", UNICODE_ATTACKS)
def test_unicode_variants_blocked(name: str) -> None:
    """Unicode 变体不能绕过白名单。"""
    with pytest.raises(ValueError):
        safe_name(name)


# ══════════════════════════════════════════════════════
# Windows 特殊名字
# ══════════════════════════════════════════════════════

WINDOWS_RESERVED = [
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM9",
    "LPT1", "LPT2", "LPT9",
    "con", "Con", "CON.txt",
    "nul.log",
    "COM1.dat",
]


@pytest.mark.parametrize("name", WINDOWS_RESERVED)
def test_windows_reserved_blocked(name: str) -> None:
    """Windows 设备名不能当目录名用。"""
    with pytest.raises(ValueError):
        safe_name(name)


# ══════════════════════════════════════════════════════
# 其他危险输入
# ══════════════════════════════════════════════════════

BAD_INPUTS = [
    "",                  # 空
    "   ",               # 全空格
    "abc\x00def",        # NUL 字符
    "abc\ndef",          # 换行
    "abc\tdef",          # 制表符
    "abc.",              # 结尾点
    "-h",                # 命令行参数
    "--help",
    "-",
    "--",
    "a" * 81,            # 太长
    "a" * 500,
    "test?name",         # 通配符
    "test*name",
    "test|name",
    'test"name',
    "test<name>",
    "test:name",
    "abc\x1b[31m",       # ANSI 转义
]


@pytest.mark.parametrize("name", BAD_INPUTS)
def test_bad_inputs_blocked(name: str) -> None:
    """这些都是不合法名字。"""
    with pytest.raises(ValueError):
        safe_name(name)


def test_non_string_rejected() -> None:
    """传非字符串得报错，不能崩。"""
    for bad in (None, 123, [], {}, 1.5):
        with pytest.raises(ValueError):
            safe_name(bad)  # type: ignore[arg-type]


def test_whitespace_stripped() -> None:
    """两头空格会被去掉——这是可接受的改写。"""
    assert safe_name("  登录页  ") == "登录页"
    assert safe_name("\t项目\n") == "项目"


# ══════════════════════════════════════════════════════
# is_inside：真实路径判定
# ══════════════════════════════════════════════════════

def test_is_inside_normal() -> None:
    root = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=root) as d:
        child = os.path.join(d, "sub")
        assert is_inside(child, d)
        assert is_inside(d, d)            # 自己算在里面
        assert not is_inside(root, d)     # 父目录不算


def test_is_inside_prefix_trick() -> None:
    """'C:\\a' 不能匹配上 'C:\\ab'。"""
    root = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=root) as d:
        sibling = d + "x"
        assert not is_inside(sibling, d)


def test_is_inside_traversal() -> None:
    """用 .. 拼出来的路径要判成"不在里面"。"""
    root = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=root) as d:
        escape = os.path.join(d, "..", "escape")
        assert not is_inside(escape, d)


# ══════════════════════════════════════════════════════
# safe_join：综合
# ══════════════════════════════════════════════════════

def test_safe_join_creates_correct_path() -> None:
    root = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=root) as d:
        p = safe_join(d, "登录页")
        assert p == os.path.join(d, "登录页")
        assert is_inside(p, d)


@pytest.mark.parametrize("name", TRAVERSAL_NAMES[:8])
def test_safe_join_blocks_traversal(name: str) -> None:
    root = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=root) as d:
        with pytest.raises(ValueError):
            safe_join(d, name)


def test_safe_join_no_escape_on_disk() -> None:
    """真刀真枪：建目录，确认没逃出去。"""
    root = tempfile.gettempdir()
    with tempfile.TemporaryDirectory(dir=root) as d:
        sentinel = os.path.join(os.path.dirname(d), "PWNED_TEST_DIR")

        for name in ("../PWNED_TEST_DIR", "..\\PWNED_TEST_DIR"):
            with pytest.raises(ValueError):
                safe_join(d, name)

        assert not os.path.exists(sentinel), "竟然逃出去了"
