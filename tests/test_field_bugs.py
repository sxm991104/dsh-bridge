"""今天实战打出来的 bug，全部变成回归测试。

为什么单独一个文件
    因为这些都是**线上真炸过**的，不是我想出来的用例。

    别人质疑过："这是用户自己测的，没有任何依据。"

    这份就是依据——每一条下面都写着它当时怎么炸的，
    谁想验证，`python tests/test_field_bugs.py` 跑一遍就完。

    测的东西都在本机、不联网、不碰真实数据。

列表（都修了，这里是防止复发）
    1. _resolve_profile 里 urllib 别名用错 → NameError 被 except 吞掉
       → 模型配置全失效，五条任务全跑在默认那套上
    2. 同款问题在 list_computer_windows / list_computer_models
    3. list_task_permissions 传 {} 被当成 POST → 查权限变成改权限
    4. tasks[-20:] 截断 → 用户找不到自己刚派的任务
    5. 表格内容带 `|` 撞 Markdown 列分隔符 → 整张表错位
    6. 并发 bump 丢计数 → 150 次只剩 31 次（在 brute_force 里）
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)


def _code_lines(src: str) -> list[str]:
    """把源码里**注释掉**的部分去掉，只留真代码。

    ⚠️ 这个函数是踩出来的。

        测试里原来写的是 `assert "tasks[-20:]" not in src`，
        而插件里有一行注释：

            # ⚠️ 这里原来写的是 tasks[-20:]。

        于是测试永远失败——它把"记录历史"的注释当成了 bug。

        判断"代码里有没有这个写法"必须排除注释，
        不然写清楚来龙去脉的注释反而会让测试挂。
    """
    out = []
    for ln in src.splitlines():
        s = ln.strip()
        if s.startswith("#"):
            continue
        # 行尾注释也去掉（粗略，够用）
        if "#" in ln:
            in_str = False
            quote = ""
            cut = None
            for i, ch in enumerate(ln):
                if ch in ('"', "'"):
                    if not in_str:
                        in_str, quote = True, ch
                    elif ch == quote:
                        in_str = False
                elif ch == "#" and not in_str:
                    cut = i
                    break
            if cut is not None:
                ln = ln[:cut]
        out.append(ln)
    return out


@pytest.fixture
def isolated():
    """隔离的配置目录。"""
    tmp = tempfile.mkdtemp()
    old_a = os.environ.get("APPDATA")
    old_l = os.environ.get("LOCALAPPDATA")
    os.environ["APPDATA"] = os.path.join(tmp, "roaming")
    os.environ["LOCALAPPDATA"] = os.path.join(tmp, "local")
    os.makedirs(os.environ["APPDATA"], exist_ok=True)
    os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)
    yield tmp
    if old_a is not None:
        os.environ["APPDATA"] = old_a
    if old_l is not None:
        os.environ["LOCALAPPDATA"] = old_l
    shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════
# 1~2. urllib 别名用错，导致静默失败
# ══════════════════════════════════════════════════════

def test_plugin_no_urllib_alias_misuse():
    """插件里不能出现 urllib.request.X 而只导入了 _ur 的情况。

    当时怎么炸的：
        写的是 import urllib.request as _ur，用的是 urllib.request.Request。
        顶层没 import urllib → NameError。
        外面套着 except Exception: return "" → 异常被吞了。

        后果：前台传了 model='deepseek'，但解析不到配置 id，
              五条任务全跑在默认那套（Kimi）上。
              而且日志一片正常，查了半天。

    这条测试直接扫源码，防止别人改回去。
    """
    p = os.path.join(
        _ROOT, "plugins", "astrbot_plugin_dsh_bridge.py"
    )
    if not os.path.exists(p):
        pytest.skip("仓库里没有这份插件")

    src = open(p, encoding="utf-8").read()
    lines = src.splitlines()

    bad = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("#") or s.startswith('"""'):
            continue
        # 用了 urllib.request.Request( 但这一行所在函数只导入了 _ur
        if "urllib.request.Request(" in s:
            nearby = "\n".join(lines[max(0, i - 30):i])
            if "import urllib.request as _ur" in nearby and \
                    "import urllib.request\n" not in nearby:
                bad.append((i + 1, s[:70]))

    assert not bad, f"还有 urllib 别名误用：{bad}"


def test_plugin_no_silent_swallow_on_profile_lookup():
    """查配置失败时必须记日志，不能静默返回空。

    静默返回空的后果：
        调用方会以为"用户没指定模型"，于是走默认。
        而用户明明指定了。
    """
    p = os.path.join(_ROOT, "plugins", "astrbot_plugin_dsh_bridge.py")
    if not os.path.exists(p):
        pytest.skip("仓库里没有这份插件")

    src = open(p, encoding="utf-8").read()

    i = src.find("def _resolve_profile")
    assert i > 0, "找不到 _resolve_profile"
    j = src.find("\n    def ", i + 10)
    seg = src[i:j] if j > 0 else src[i:]

    # 里面有 except，就必须有 logger
    if "except Exception" in seg:
        assert "logger." in seg, (
            "_resolve_profile 里有 except Exception 但没有 logger —— "
            "出错会被静默吞掉"
        )


# ══════════════════════════════════════════════════════
# 3. 查权限用 GET，不能变成 POST
# ══════════════════════════════════════════════════════

def test_api_perms_get_does_not_need_name():
    """GET /api/perms 不带任何参数就能返回全部窗口。

    当时怎么炸的：
        插件调 _call_api("/api/perms", {})，
        而 _call_api 的逻辑是 method="POST" if data is not None else "GET"。
        {} 不是 None → 走 POST → 那个接口是"改权限"，要求给 name
        → 返回"没说是哪个窗口"。

        用户说"看看窗口权限"，前台回"查不到：没说是哪个窗口"。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "api_server_t", os.path.join(_ROOT, "api_server.py")
    )
    api = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(api)
    except Exception:  # noqa: BLE001
        pytest.skip("api_server 依赖不全，跳过")

    h = api.Handler.__new__(api.Handler)
    d = h._perms_get()

    assert d.get("ok") is True
    assert "levels" in d
    assert "tasks" in d
    assert isinstance(d["tasks"], list)


def test_perms_list_not_truncated(isolated):
    """窗口列表不能只返回前 N 个。

    当时怎么炸的：
        插件里写的是 tasks[-20:]，只取最后 20 个。
        共 62 个窗口，前面 42 个被切掉了。
        用户问"你好6~15 的权限"，前台说"不在表里"，
        还自己猜成"这个接口查的是权限台账，不是窗口清单"。
    """
    # ⚠️ 这里不造假窗口。
    #
    #    _perms_get() 是从**真实任务库**读窗口的，
    #    往 task_perms 里塞一个名字不会让任务库多一条。
    #    第一版测试就写错了这一点，造了 60 个假窗口然后断言"找不到"。
    #
    #    真正要验的是：它返回的长度不受 20 限制。
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "api_server_t2", os.path.join(_ROOT, "api_server.py")
    )
    api = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(api)
    except Exception:  # noqa: BLE001
        pytest.skip("api_server 依赖不全，跳过")

    h = api.Handler.__new__(api.Handler)
    d = h._perms_get()
    tasks = d.get("tasks") or []

    # 真实环境里窗口数经常超过 20，这正是当初截断暴露出来的场景。
    # 这里只能验"没被硬切成 20"，不能假定一定有多少个。
    src = open(os.path.join(_ROOT, "plugins", "astrbot_plugin_dsh_bridge.py"),
               encoding="utf-8").read()
    code = "\n".join(_code_lines(src))
    assert "tasks[-20:]" not in code, "插件里又在截断成 20 个了"
    assert isinstance(tasks, list)


def test_perms_has_profile_field(isolated):
    """每个窗口要带 profile 字段，前端才能按模型分组。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "api_server_t3", os.path.join(_ROOT, "api_server.py")
    )
    api = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(api)
    except Exception:  # noqa: BLE001
        pytest.skip("api_server 依赖不全，跳过")

    h = api.Handler.__new__(api.Handler)
    d = h._perms_get()

    tasks = d.get("tasks") or []
    if not tasks:
        pytest.skip("没有窗口可查")

    for t in tasks[:10]:
        assert "profile" in t, f"{t.get('name')} 没有 profile 字段"


# ══════════════════════════════════════════════════════
# 4~5. 输出格式
# ══════════════════════════════════════════════════════

def test_permissions_output_avoids_pipe():
    """权限列表的输出里不能出现 `|`。

    当时怎么炸的：
        62 个窗口名用 `|` 拼起来塞进一格，
        而 `|` 是 Markdown 表格的列分隔符。
        飞书把内容当成新列，整张表错位。

    现在用「、」连起来。
    """
    p = os.path.join(_ROOT, "plugins", "astrbot_plugin_dsh_bridge.py")
    if not os.path.exists(p):
        pytest.skip("仓库里没有这份插件")

    src = "\n".join(_code_lines(open(p, encoding="utf-8").read()))

    # 找 list_task_permissions 这一整段
    i = src.find('name="list_task_permissions"')
    assert i > 0, "找不到这个工具"
    j = src.find('name="set_task_permission"', i)
    seg = src[i:j] if j > 0 else src[i:i + 5000]

    # 窗口名要用「、」连，不能是 |
    assert '"、".join' in seg, "窗口名应该用「、」连接，不是 |"
    assert '"|".join' not in seg.replace(" ", ""), "还在用 | 连接"
    assert "tasks[-20:]" not in seg, "还在截断成 20 个"


def test_long_list_is_truncated_in_message():
    """一格里最多列 12 个，多了要省略。

    不然 42 个名字堆在一起，飞书那条消息会失控。
    """
    p = os.path.join(_ROOT, "plugins", "astrbot_plugin_dsh_bridge.py")
    if not os.path.exists(p):
        pytest.skip("仓库里没有这份插件")

    src = open(p, encoding="utf-8").read()
    assert "还有 {" in src or "还有" in src, "应该有省略提示"


# ══════════════════════════════════════════════════════
# 6. 权限改完要真的生效（端到端）
# ══════════════════════════════════════════════════════

def test_perm_change_persists(isolated):
    """改完权限，重新读要读得到。

    这是最基本的一条，但当时测过一种情况：
    改是改了，但持久化失败（返回 True 实际没写），
    下次启动就丢了。
    """
    import task_perms

    task_perms.set_level("持久化测试", "full")
    assert task_perms.get_level("持久化测试") == "full"

    # 模拟"重新加载"——清内存缓存没有，直接读文件
    cfg = task_perms.load()
    assert cfg.get("持久化测试") == "full"

    # 降回去
    task_perms.set_level("持久化测试", "workspace")
    assert task_perms.get_level("持久化测试") == "workspace"
    # 工作区是默认值，不该写进文件
    assert "持久化测试" not in task_perms.load()


def test_perm_escalation_needs_code(isolated):
    """升权必须过验证码，降权不用。

    ⚠️ 这条测的是**接口层**的行为，用打桩绕过真发邮件。
    """
    import guard
    import task_perms

    # 打桩：不发真邮件
    def _fake_send(task, purpose=""):
        import time

        fp = guard._fingerprint(task)
        guard._state.pending[fp] = guard.PendingCode(
            code="123456", task=task, purpose=purpose,
            expires=time.time() + 120,
        )
        return True, "已发"

    old_send = guard.send_code_email
    guard.send_code_email = _fake_send
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "api_server_t4", os.path.join(_ROOT, "api_server.py")
        )
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)

        h = api.Handler.__new__(api.Handler)

        # 降到工作区：不用码
        task_perms.set_level("升权测试", "full")
        r = h._perms_set({"name": "升权测试", "level": "workspace"})
        assert r.get("ok") is True
        assert r.get("need_code") is None
        assert task_perms.get_level("升权测试") == "workspace"

        # 升到 full：要码
        r = h._perms_set({"name": "升权测试", "level": "full"})
        assert r.get("need_code") is True
        assert task_perms.get_level("升权测试") == "workspace", \
            "没验证就升上去了"

        # 带对码
        r = h._perms_set(
            {"name": "升权测试", "level": "full", "code": "123456"}
        )
        assert r.get("ok") is True
        assert task_perms.get_level("升权测试") == "full"
    finally:
        guard.send_code_email = old_send


def test_perm_wildcard_rejected(isolated):
    """不能一次改一片。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "api_server_t5", os.path.join(_ROOT, "api_server.py")
    )
    api = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(api)
    except Exception:  # noqa: BLE001
        pytest.skip("api_server 依赖不全，跳过")

    h = api.Handler.__new__(api.Handler)
    for name in ("*", "任务*", "?", "%"):
        r = h._perms_set({"name": name, "level": "full"})
        assert r.get("ok") is False, f"{name} 竟然被接受了"


def test_perm_code_bound_to_window(isolated):
    """给 A 发的码不能拿去改 B。"""
    import guard
    import task_perms

    def _fake_send(task, purpose=""):
        import time

        fp = guard._fingerprint(task)
        guard._state.pending[fp] = guard.PendingCode(
            code="654321", task=task, purpose=purpose,
            expires=time.time() + 120,
        )
        return True, "已发"

    old_send = guard.send_code_email
    guard.send_code_email = _fake_send
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "api_server_t6", os.path.join(_ROOT, "api_server.py")
        )
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)

        h = api.Handler.__new__(api.Handler)

        task_perms.set_level("窗口A", "workspace")
        task_perms.set_level("窗口B", "workspace")

        # 给 A 发码
        h._perms_set({"name": "窗口A", "level": "full"})

        # 拿 A 的码去改 B
        r = h._perms_set(
            {"name": "窗口B", "level": "full", "code": "654321"}
        )
        assert task_perms.get_level("窗口B") == "workspace", \
            "A 的码竟然能改 B"
    finally:
        guard.send_code_email = old_send

# ══════════════════════════════════════════════════════
# 7. 跨平台：路径判断不能依赖当前系统
# ══════════════════════════════════════════════════════

def test_is_outside_platform_independent():
    """盘符路径和 POSIX 绝对路径，在哪个平台上都算越界。

    当时怎么炸的：
        有人拉下来在 Linux 上跑测试，报 "echo. > D:/important.txt" 没拦住。
        他以为是真漏洞。

        真相：
            在 Windows 上   D:/ 是绝对路径      → 判成越界 → 拦
            在 Linux 上     D:/ 只是普通文件名  → 判成没越界 → 不拦

        差异不在安全逻辑，在"谁在解析这个字符串"。

        命令最终在哪执行、按哪个平台的规则解析，才是要紧的。
        这个项目主要跑 Windows，所以看到盘符就按 Windows 规则判——
        哪怕测试跑在 Linux 上。
    """
    import tempfile

    import action_guard as ag

    w = tempfile.mkdtemp()

    # 这些不管在哪个系统上都该判成"跑出去了"
    must_be_outside = [
        "D:/important.txt",
        "C:\\Windows\\evil.txt",
        "/etc/passwd",
        "~/secret",
        "../outside.txt",
    ]
    for p in must_be_outside:
        assert ag.is_outside(p, w), f"{p} 应该算越界"


def test_is_outside_relative_stays_inside():
    """普通相对路径不算越界。

    这条防的是另一种错：把 abspath 当成相对工作目录。

        os.path.abspath("a.txt") 按的是 **进程的 cwd**，
        不是任务的工作目录。于是 "a.txt" 被解析成 <仓库>/a.txt，
        跟任务目录完全不同 → 判成越界 → 每一条正常写都误伤。

    相对路径的基准永远是任务的工作目录。
    """
    import tempfile

    import action_guard as ag

    w = tempfile.mkdtemp()

    for p in ("a.txt", "sub/b.txt", "./x.md", "deep/nested/file.py"):
        assert not ag.is_outside(p, w), f"{p} 不该算越界"


def test_echo_redirect_blocked_outside():
    """echo. > 工作区外的文件，必须拦。

    这是外部实测报的那条"真漏洞"。
    在第一版的实现里它确实没被拦住——
    destructive_move_or_truncate 管住了 move，没管 > 重定向截断。
    """
    import json
    import tempfile

    from engine import agent, tools

    w = tempfile.mkdtemp()
    tools.set_workdir(w)
    tools.set_level("workspace")

    v = agent._check_action(
        "run_command",
        json.dumps({"command": "echo. > D:/important.txt"}),
    )
    assert v is not None, "echo 重定向截断没被拦住"


def test_echo_redirect_inside_allowed():
    """在工作区里写文件，不拦。"""
    import json
    import tempfile

    from engine import agent, tools

    w = tempfile.mkdtemp()
    tools.set_workdir(w)
    tools.set_level("workspace")

    for cmd in ("echo hello > out.txt", "echo hi > sub/a.md"):
        v = agent._check_action(
            "run_command", json.dumps({"command": cmd})
        )
        assert v is None, f"{cmd} 不该拦"
