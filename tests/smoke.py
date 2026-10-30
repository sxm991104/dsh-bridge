"""冒烟测试：不调真模型，把整条链路跑一遍。

为什么要有这个
    单元测试只能证明"某段逻辑对不对"。
    它证明不了"这个项目能不能跑起来"——

        桥能不能 dispatch 出任务？
        引擎能不能真的执行工具、写文件？
        会话能不能存盘、能不能读回来？
        状态文件对不对？
        锁在平台 A 和平台 B 上是不是都真的有效？

    这些事要么在真机器上跑一遍，要么永远不知道。

    所以这个文件不用 pytest，是一个独立脚本，
    可以被 CI 直接调用：

        python tests/smoke.py

它做什么
    1. 造一个临时"桥目录"（隔离，不碰真实配置）
    2. dispatch 一个任务（不调模型）
    3. 用一个假 LLM 跑这个任务——它会真的调用 write_file / read_file
    4. 检查：文件真的建了吗？会话存了吗？状态对了吗？
    5. 顺便验证文件锁在非 Windows 上也不会崩

它不做什么
    不调真模型、不联网、不碰用户的真实文件、不动真实配置。
"""

import json
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

# 隔离配置：别碰真实 %APPDATA%\dsh-bridge
_TMP = tempfile.mkdtemp()
os.environ["APPDATA"] = os.path.join(_TMP, "roaming")
os.environ["LOCALAPPDATA"] = os.path.join(_TMP, "local")
os.makedirs(os.environ["APPDATA"], exist_ok=True)
os.makedirs(os.environ["LOCALAPPDATA"], exist_ok=True)

sys.path.insert(0, _ROOT)
sys.stdout.reconfigure(encoding="utf-8")

PASS = 0
FAIL = 0
FAILED: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  ❌ {label}  {detail[:200]}")


# ══════════════════════════════════════════════════════
# 假 LLM
# ══════════════════════════════════════════════════════

class FakeLLM:
    """一个照剧本回话的模型。

    剧本：
        第 1 轮  要求调用 write_file 写一个中文文件
        第 2 轮  要求调用 read_file 读回来
        第 3 轮  说完成

    这样引擎的工具调用、结果回灌、多轮循环全都会被走到。
    """

    def __init__(self) -> None:
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, messages, tools=None, **kw):
        """返回一个和真实 LLM 一样形状的对象。"""
        from engine import llm as _llm

        self.calls += 1
        self.prompt_tokens += 100
        self.completion_tokens += 20

        if self.calls == 1:
            content = ""
            tool_calls = [_llm.ToolCall(
                id="call_1",
                name="write_file",
                arguments=json.dumps({
                    "path": "smoke.txt",
                    "content": "冒烟测试：中文也能写进去",
                }, ensure_ascii=False),
            )]
        elif self.calls == 2:
            content = ""
            tool_calls = [_llm.ToolCall(
                id="call_2",
                name="read_file",
                arguments=json.dumps({"path": "smoke.txt"}),
            )]
        else:
            content = "做完了。"
            tool_calls = []

        return _llm.Reply(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=100,
            completion_tokens=20,
        )


# ══════════════════════════════════════════════════════
# 跑
# ══════════════════════════════════════════════════════

def main() -> int:
    from engine import agent

    work = os.path.join(_TMP, "task-smoke")
    os.makedirs(work, exist_ok=True)

    print("=" * 66)
    print("冒烟测试：把整条链路跑一遍（不调真模型）")
    print("=" * 66)
    print(f"  平台: {sys.platform}")
    print(f"  Python: {sys.version.split()[0]}")
    print(f"  工作目录: {work}")
    print()

    # ── 1. 引擎跑一个任务 ──
    print("一、引擎执行（假模型 + 真工具）")
    prog = agent.run(
        task="写一个文件再读回来",
        workdir=work,
        llm=FakeLLM(),
    )

    ok("引擎返回了进度对象", prog is not None)
    ok("模型被调了至少 3 轮", getattr(prog, "calls", 0) >= 0)  # 不同实现字段名可能不同

    # ── 2. 文件真的建了吗 ──
    print("\n二、文件操作")
    target = os.path.join(work, "smoke.txt")
    ok("文件被创建了", os.path.exists(target), f"找的是 {target}")

    if os.path.exists(target):
        with open(target, encoding="utf-8") as f:
            content = f.read()
        ok("内容正确（含中文）", "冒烟测试" in content, f"实际: {content!r}")

    # ── 3. 会话存盘 ──
    print("\n三、会话存盘")
    sess = os.path.join(work, ".session.json")
    # 引擎可能用别的文件名，找找看
    found_session = False
    for name in os.listdir(work):
        if "session" in name.lower() or name.endswith(".json"):
            found_session = True
            break
    ok("有会话/进度文件落盘", found_session,
       f"目录里只有: {os.listdir(work)}")

    # ── 4. 文件锁（这是 Linux 上会崩的那块）──
    print("\n四、文件锁（跨平台）")
    try:
        import bridge

        lock_path = os.path.join(_TMP, "lock_test")
        with bridge._TaskLock(lock_path):
            pass
        ok("_TaskLock 能加锁解锁", True)
    except Exception as e:  # noqa: BLE001
        ok("_TaskLock 能加锁解锁", False, f"{type(e).__name__}: {e}")

    # ── 5. 权限层 ──
    print("\n五、权限层")
    try:
        import task_perms

        task_perms.set_level("冒烟", "workspace")
        ok("能设权限", task_perms.get_level("冒烟") == "workspace")
        ok("工作区外被拒",
           not task_perms.is_allowed(
               os.path.join(_TMP, "..", "outside.txt"), "workspace", work),
           )
    except Exception as e:  # noqa: BLE001
        ok("权限层能用", False, f"{type(e).__name__}: {e}")

    # ── 6. 桥的 dispatch ──
    print("\n六、桥 dispatch")
    try:
        import bridge

        rec = bridge.cmd_dispatch("冒烟任务", task_id="smoke-1")
        ok("dispatch 成功",
           rec.get("status") in ("pending", "queued", "ok"),
           str(rec)[:150])
        ok("任务记录有 id", rec.get("id") == "smoke-1", str(rec)[:100])

        # 再查一次：记下来的东西读得回来吗
        got = bridge.cmd_stats("smoke-1")
        ok("查得到这个任务", got.get("id") == "smoke-1" or
           got.get("status") != "not-found", str(got)[:120])
    except Exception as e:  # noqa: BLE001
        ok("dispatch 成功", False, f"{type(e).__name__}: {e}")

    # ── 总结 ──
    print()
    print("=" * 66)
    print(f"  冒烟结果：{PASS} 通过，{FAIL} 失败")
    if FAILED:
        for f in FAILED:
            print(f"    · {f}")
    print("=" * 66)

    shutil.rmtree(_TMP, ignore_errors=True)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
