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

    t0 = time.time()

    # 按模块跑，这样能看到每个模块的统计
    modules = [
        ("路径安全", "tests/test_safe_path.py"),
        ("危险操作验证码", "tests/test_guard.py"),
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
            # 只打失败的详情
            for line in out.splitlines():
                if line.startswith("FAILED") or line.startswith("ERROR"):
                    print(f"    {line.strip()}")
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

    elapsed = time.time() - t0

    print(f"\n{'=' * 66}")
    print(f"  合计：{total_pass} 通过，{total_fail} 失败（{elapsed:.1f} 秒）")
    if failed_modules:
        print(f"  失败模块：{', '.join(failed_modules)}")
    print("=" * 66)

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
