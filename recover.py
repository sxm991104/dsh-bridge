"""桥启动时，把"卡死"的任务状态纠正过来。

问题
    进程被强杀、机器断电、桥崩溃——这些情况下进程没了，
    但 tasks.jsonl 里还写着 status="running"。

    结果界面一直转圈，用户以为它还在跑。实测有过一条跑了 27 小时还显示运行中。

怎么判断"还活着"
    不能只看进程在不在——同一个进程号可能被别的程序用了。

    所以两重判断：
        1. progress 文件里记的 pid 还存在吗
        2. 那个进程的命令行里有没有我们这条任务的名字

    两个都过了才算活着。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# 判定为"太久没动"的阈值。
#
# 为什么要有这个：
#   心跳本来会每几秒更新一次 .progress.json。
#   如果协议变了、或者桥自己出了 bug 导致不写心跳，
#   光看 pid 会以为还活着，实际上早就不动了。
#
#   默认 2 小时。一个任务正常跑 30 分钟就算长了。
STALE_SECONDS = 2 * 3600


def _pid_alive(pid: int) -> bool:
    """这个进程号还在吗。

    ⚠️ 在 Windows 上 os.kill(pid, 0) 不可靠——它可能把信号发过去，
        也可能因为权限问题误判。用 tasklist 查更稳。
    """
    if not pid or pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=0x08000000,
            )
            out = r.stdout or ""
            # 有输出且不是"没有运行的任务"
            return str(pid) in out and "No tasks" not in out and "没有" not in out
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def _progress_path(workspaces: str, task_id: str) -> str:
    return os.path.join(workspaces, task_id, ".progress.json")


def _read_progress(workspaces: str, task_id: str) -> dict:
    p = _progress_path(workspaces, task_id)
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _looks_dead(workspaces: str, task_id: str) -> tuple[bool, str]:
    """这条任务是不是已经死了。返回 (死了吗, 原因)。"""
    prog = _read_progress(workspaces, task_id)

    # 没有 progress 文件 = 从来没真正开始过（或者被清掉了）
    if not prog:
        return True, "没有进度记录"

    pid = prog.get("pid")
    if pid and _pid_alive(int(pid)):
        # 进程还在。再看心跳是不是太久没动
        hb = prog.get("heartbeat_at") or ""
        if hb:
            try:
                from datetime import datetime

                t = datetime.fromisoformat(hb)
                age = time.time() - t.timestamp()
                if age > STALE_SECONDS:
                    return True, f"进程还在但 {int(age // 60)} 分钟没动静"
            except (ValueError, TypeError):
                pass
        return False, ""

    return True, "进程已经退出"


def recover(workspaces: str, read_tasks, write_tasks, now_iso: str) -> list[dict]:
    """扫一遍，把卡在 running 的任务纠正过来。

    返回被纠正的清单，方便日志里说一声。
    """
    fixed: list[dict] = []

    try:
        tasks = read_tasks()
    except Exception:  # noqa: BLE001
        return fixed

    changed = False
    for t in tasks:
        if t.get("status") != "running":
            continue

        tid = t.get("id") or ""
        if not tid:
            continue

        dead, why = _looks_dead(workspaces, tid)
        if not dead:
            continue

        t["status"] = "interrupted"
        t["error"] = f"中途断了（{why}）"
        t["finished_at"] = now_iso
        t["seconds"] = None
        changed = True
        fixed.append({"id": tid, "why": why})

    if changed:
        try:
            write_tasks(tasks)
        except Exception:  # noqa: BLE001
            pass

    return fixed
