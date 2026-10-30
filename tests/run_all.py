"""一键跑完全部回归测试。

用法：
    python tests/run_all.py

或者直接：
    pytest tests/ -v

这个脚本的好处是不用装 pytest 的命令行参数也能跑，
而且会打印一份"哪个模块测了多少条"的汇总。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# ⚠️ 中文 Windows 的控制台默认是 GBK。
#    脚本里用了 ✅ ❌ 这些符号，GBK 打不出来会直接崩：
#        UnicodeEncodeError: 'gbk' codec can't encode character
#
#    而"别人 clone 下来跑不起来"正是最劝退的事。
#    所以这里强制把 stdout 换成 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    print("=" * 66)
    print("dsh-bridge 回归测试")
    print("=" * 66)

    # 检查 pytest
    try:
        import pytest  # noqa: F401
    except ImportError:
        print("\n  ❌ 没装 pytest。装一下：")
        print("     python -m pip install pytest")
        return 1

    # 先打环境信息。
    #
    # 为什么：CI 失败的时候，第一件想知道的事就是"跑在什么环境里"。
    # 不打出来，就只能靠猜——今天在这上面浪费了太多时间。
    import platform as _pf

    print(f"{'=' * 66}")
    print(f"dsh-bridge 回归测试")
    print(f"{'=' * 66}")
    print(f"  平台: {sys.platform} / {_pf.machine()}")
    print(f"  Python: {sys.version.split()[0]}  ({sys.executable})")
    print(f"  工作目录: {os.getcwd()}")
    print(f"  APPDATA: {os.environ.get('APPDATA') or '(未设置)'}")
    print(f"  HOME: {os.environ.get('HOME') or os.path.expanduser('~')}")
    print()

    t0 = time.time()

    # 按模块跑，这样能看到每个模块的统计
    modules = [
        ("路径安全", "tests/test_safe_path.py"),
        ("危险操作验证码", "tests/test_guard.py"),
        ("动作级守卫", "tests/test_action_guard.py"),
        ("实战 bug", "tests/test_field_bugs.py"),
        ("接口安全", "tests/test_security.py"),
        ("引擎核心", "tests/test_engine.py"),
        ("并发与调度", "tests/test_concurrency.py"),
        ("产出文件", "tests/test_outputs.py"),
        ("压力测试", "tests/test_stress.py"),
    ]

    total_pass = 0
    total_fail = 0
    failed_modules = []

    for label, path in modules:
        full = os.path.join(ROOT, path)
        if not os.path.exists(full):
            print(f"\n  ⚠️ {label}：文件不存在（{path}）")
            continue

        print(f"\n{'─' * 66}")
        print(f"  {label}")
        print(f"{'─' * 66}")

        r = subprocess.run(
            [sys.executable, "-m", "pytest", full, "-v", "--tb=short",
             "-p", "no:cacheprovider"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        out = r.stdout + r.stderr

        # 从输出里找统计行
        for line in out.splitlines():
            if " passed" in line or " failed" in line or " error" in line:
                print(f"  {line.strip()}")
                break

        if r.returncode != 0:
            failed_modules.append(label)

            # ⚠️ 原来只打 "FAILED xxx" 这一行，看不到为什么失败。
            #
            #    在 CI 里就抓瞎了：只知道哪个测试红了，
            #    不知道是断言失败、ImportError 还是环境问题。
            #
            #    而 CI 日志只能从网页看（API 要 token），
            #    所以必须把足够的信息打到 stdout 里。
            #
            #    现在：失败时打完整的 traceback（最后 40 行）。
            print(f"    ---- {label} 失败详情 ----")
            for line in out.splitlines()[-40:]:
                print(f"    {line}")
        else:
            print("    ✅ 全过")

        # 数一下
        import re
        m = re.search(r"(\d+) passed", out)
        if m:
            total_pass += int(m.group(1))
        m = re.search(r"(\d+) failed", out)
        if m:
            total_fail += int(m.group(1))

    # ── 红队扩展（外部独立脚本，不在 pytest 列表里）──
    #
    # 这份是别人写的（见 tests/redteam_ext.py 头部说明）。
    # 它是独立脚本，不是 pytest 用例，所以单独跑。
    print(f"\n{'─' * 66}")
    print("  红队扩展")
    print(f"{'─' * 66}")

    rt = os.path.join(ROOT, "tests", "redteam_ext.py")
    if os.path.exists(rt):
        r = subprocess.run(
            [sys.executable, rt, ROOT],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if "合计" in line:
                print(f"  {line.strip()}")
        # 声明"不同意"的那几条，不当作失败
        known = ("::ffff:127.0.0.1", "提示注入未绕过闸门")
        for line in (r.stdout or "").splitlines():
            t = line.strip()
            if t.startswith("·") and not any(k in t for k in known):
                print(f"    {t}")

    # ── 暴力攻击测试 ──
    #
    # 单独跑，因为它不是 pytest 用例（是自己写的一个脚本）。
    # 它打的是"挡不挡得住"，跟上面那些"功能对不对"是两回事。
    print(f"\n{'─' * 66}")
    print("  暴力攻击测试")
    print(f"{'─' * 66}")

    bf = os.path.join(ROOT, "tests", "brute_force.py")
    if os.path.exists(bf):
        r = subprocess.run(
            [sys.executable, bf],
            cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for line in (r.stdout or "").splitlines():
            if "结果：" in line or "失败清单" in line:
                print(f"  {line.strip()}")
        for line in (r.stdout or "").splitlines():
            if line.strip().startswith("[绕过]") or line.strip().startswith("[误伤]"):
                print(f"    {line.strip()}")
        if r.returncode != 0:
            failed_modules.append("暴力攻击")

    elapsed = time.time() - t0

    print(f"\n{'=' * 66}")
    print(f"  合计：{total_pass} 通过，{total_fail} 失败（{elapsed:.1f} 秒）")
    if failed_modules:
        print(f"  失败模块：{', '.join(failed_modules)}")
    print("=" * 66)

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
