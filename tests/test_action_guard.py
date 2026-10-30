"""动作级守卫的回归测试。

这块是整个安全模型的核心，所以要压狠一点。

安全模型（跟以前完全不一样了）：

    以前   看任务描述里有没有危险词     → 词列不完，而且描述会骗人
    现在   看工具调用实际做了什么        → 做不了假

    判断标准也不是"危不危险"（程度问题），而是"能不能撤销"（非黑即白）。

    被拦算一次，三次锁死，锁死之后连正常活都进不来。
"""

import json
import os
import shutil
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


@pytest.fixture
def isolated():
    """隔离的配置目录。

    ⚠️ count_store 会读 %APPDATA%，不隔离的话测试会动真实计数。
    """
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
# 不可逆动作的识别
# ══════════════════════════════════════════════════════

@pytest.mark.parametrize("cmd", [
    "rm x.txt",
    "del config.json",
    "Remove-Item foo",
    "rmdir /s data",
    "rm -rf build",
    "chmod 777 x",
    "chmod +x run.sh",
    "icacls f /grant Everyone:F",
    "takeown /f dir",
    "git push -f",
    "git push --force origin main",
    "git reset --hard HEAD~1",
    "git clean -fd",
    "format D:",
    "diskpart",
    "reg add HKCU\\x",
    "sc create svc",
    "drop table users",
    "truncate table logs",
    "curl -d @f https://x.com",
    "scp a.txt user@h:/x",
])
def test_irreversible_detected(cmd: str) -> None:
    """这些动作都必须被认出来。"""
    import action_guard as ag

    assert ag.describe(cmd), f"没认出：{cmd}"


@pytest.mark.parametrize("cmd", [
    "echo hi > out.txt",
    "cat config.json",
    "pytest tests/",
    "pip install requests",
    "git status",
    "git add . && git commit -m fix",
    "git push origin main",
    "ls -la",
    "dir",
    "grep -r TODO src/",
    "mkdir newdir",
    "copy a b",
    "mv a b",
    "python -c print(1)",
    "npm run build",
])
def test_normal_commands_pass(cmd: str) -> None:
    """正常命令一条都不能误拦。

    误拦比漏拦还烦人——用户干着正常的活，突然被拦，
    会以为程序坏了。
    """
    import action_guard as ag

    assert not ag.describe(cmd), f"误拦了：{cmd}"


def test_case_insensitive() -> None:
    """大小写不影响识别。"""
    import action_guard as ag

    assert ag.describe("RM -RF /")
    assert ag.describe("DEL x")
    assert ag.describe("Drop Table t")


def test_empty_command() -> None:
    """空命令不崩。"""
    import action_guard as ag

    assert ag.describe("") == []
    assert ag.describe("   ") == []


# ══════════════════════════════════════════════════════
# 任务描述骗不过去
# ══════════════════════════════════════════════════════

def test_description_cannot_disguise_action(isolated: str) -> None:
    """★ 任务描述写得再无害，真动手还是会被拦。

    这是新安全模型的关键：
    以前是"读描述判断危险"，所以只要让前台把描述写得好看点就绕过去了。
    现在看的是动作。
    """
    import bridge
    from engine import agent, tools

    workdir = os.path.join(isolated, "work")
    os.makedirs(workdir, exist_ok=True)
    tools.set_workdir(workdir)

    # 描述完全无害
    r = bridge.cmd_dispatch("帮忙整理一下临时文件", "伪装测试")
    assert r.get("status") == "pending", "无害描述本身不该被拦"

    # 但它真动手删的时候会被拦
    v = agent._check_action(
        "run_command", json.dumps({"command": "del /f *.tmp"})
    )
    assert v is not None, "真删的时候没拦住"

    # 清掉
    tasks = [t for t in bridge._read_tasks() if t.get("id") != "伪装测试"]
    bridge._write_tasks(tasks)


# ══════════════════════════════════════════════════════
# 计数：被拦就算，不分场合
# ══════════════════════════════════════════════════════

def test_block_increments_count(isolated: str) -> None:
    """被拦一次，计数 +1。"""
    import count_store
    import guard_gate
    from engine import agent

    count_store.reset_count()
    before = guard_gate.peek_count().get("count", 0)

    agent._check_action("run_command", json.dumps({"command": "rm x"}))

    after = guard_gate.peek_count().get("count", 0)
    assert after == before + 1


def test_three_blocks_lock(isolated: str) -> None:
    """★ 三次就锁死。"""
    import count_store
    import guard_gate
    from engine import agent

    count_store.reset_count()

    agent._check_action("run_command", json.dumps({"command": "rm a"}))
    agent._check_action("run_command", json.dumps({"command": "chmod 777 b"}))
    agent._check_action("run_command", json.dumps({"command": "git reset --hard"}))

    assert guard_gate.peek_count().get("tripped") is True


def test_locked_blocks_normal_dispatch(isolated: str) -> None:
    """锁死之后，正常活也派不出去。"""
    import bridge
    import count_store
    import guard_gate
    from engine import agent

    count_store.reset_count()
    for cmd in ("rm a", "chmod 777 b", "git reset --hard"):
        agent._check_action("run_command", json.dumps({"command": cmd}))

    r = bridge.cmd_dispatch("写个报告", "锁死后测试")
    assert r.get("status") == "blocked"


def test_reset_after_verify(isolated: str) -> None:
    """验证通过后计数重置。"""
    import count_store
    import guard_gate
    from engine import agent

    count_store.reset_count()
    for cmd in ("rm a", "chmod 777 b", "git reset --hard"):
        agent._check_action("run_command", json.dumps({"command": cmd}))

    assert guard_gate.peek_count().get("tripped") is True

    guard_gate.reset_after_pass()
    assert guard_gate.peek_count().get("count") == 0
    assert guard_gate.peek_count().get("tripped") is False


# ══════════════════════════════════════════════════════
# 计数文件的防篡改
# ══════════════════════════════════════════════════════

def test_tamper_main_file(isolated: str) -> None:
    """改主文件没用。"""
    import count_store

    count_store.reset_count()
    count_store.bump()
    count_store.bump()

    mp = count_store.main_path()
    raw = json.load(open(mp, encoding="utf-8"))
    raw["count"] = 0
    json.dump(raw, open(mp, "w", encoding="utf-8"), ensure_ascii=False)

    assert count_store.peek()["count"] >= 2


def test_tamper_shadow_file(isolated: str) -> None:
    """改影子没用。"""
    import base64

    import count_store

    count_store.reset_count()
    count_store.bump()
    count_store.bump()

    sp = count_store.shadow_path()
    sb = json.loads(base64.b64decode(open(sp, "rb").read()).decode())
    sb["d"]["count"] = 0
    open(sp, "wb").write(base64.b64encode(json.dumps(sb).encode()))

    assert count_store.peek()["count"] >= 2


def test_tamper_both_files(isolated: str) -> None:
    """两边都改也没用——签名对不上就锁死。"""
    import base64

    import count_store

    count_store.reset_count()
    count_store.bump()

    mp = count_store.main_path()
    sp = count_store.shadow_path()

    raw = json.load(open(mp, encoding="utf-8"))
    raw["count"] = 0
    json.dump(raw, open(mp, "w", encoding="utf-8"), ensure_ascii=False)

    sb = json.loads(base64.b64decode(open(sp, "rb").read()).decode())
    sb["d"]["count"] = 0
    open(sp, "wb").write(base64.b64encode(json.dumps(sb).encode()))

    # 保守处理：按拦满算
    assert count_store.peek()["count"] >= 3


def test_delete_files_not_reset(isolated: str) -> None:
    """★ 把文件删掉也不等于清零。

    这是最容易得手的一种攻击：两个文件一删，就当"第一次运行"。
    所以加了一个 0 字节的标记文件——它只表示"跑过"，
    删了计数文件但标记还在，就按篡改处理。
    """
    import count_store

    count_store.reset_count()
    count_store.bump()
    assert count_store.peek()["count"] == 1

    os.remove(count_store.main_path())
    os.remove(count_store.shadow_path())

    # 删了之后不是 0，是按拦满处理
    assert count_store.peek()["count"] >= 3


def test_first_run_is_zero(isolated: str) -> None:
    """真的第一次运行，计数是 0。"""
    import count_store

    # 新目录，什么都没有
    assert count_store.peek()["count"] == 0


def test_day_rollover(isolated: str) -> None:
    """换一天，计数从头算。"""
    import count_store

    count_store.save({
        "date": "2020-01-01",
        "count": 5,
        "tamper": 0,
        "updated": "",
    })

    assert count_store.peek()["count"] == 0


def test_reset_keeps_tamper(isolated: str) -> None:
    """重置计数不该抹掉篡改记录。"""
    import count_store

    count_store.reset_count()
    d = count_store.load()
    d["tamper"] = 5
    count_store.save(d)

    count_store.reset_count()
    assert count_store.load()["tamper"] >= 5
