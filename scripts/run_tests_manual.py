# -*- coding: utf-8 -*-
"""在没有 pytest 的环境里直接跑不依赖 pytest 的测试函数。"""
import importlib
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

module = importlib.import_module(sys.argv[1])
tests = [(n, f) for n, f in vars(module).items() if n.startswith("test_") and callable(f)]
ok = fail = 0
for name, fn in tests:
    try:
        fn()
        ok += 1
    except Exception:
        fail += 1
        print(f"FAIL {name}")
        traceback.print_exc()
print(f"{sys.argv[1]}: {ok} passed, {fail} failed, total {len(tests)}")

