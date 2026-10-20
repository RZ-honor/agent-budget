# -*- coding: utf-8 -*-
"""把项目自带的第三方源码（不安装）挂到 sys.path 上。

顺序很重要：pylibs 必须最先，因为 minisweagent 的 third-party 依赖
（litellm / pydantic / yaml 等）都在这里，而不是装进全局环境。
"""
from __future__ import annotations

import sys

from agent_budget.paths import AGENT_HARNESS, PYLIBS, VENDOR_MINI

_PATHS = [
    PYLIBS,
    VENDOR_MINI / "src",
    AGENT_HARNESS,
    AGENT_HARNESS / "agentops" / "session_eval",
]


def install() -> list[str]:
    """幂等地把依赖路径插到 sys.path 最前面，返回实际加入的路径。"""
    added = []
    for path in reversed(_PATHS):
        text = str(path)
        if path.is_dir() and text not in sys.path:
            sys.path.insert(0, text)
            added.append(text)
    return added


install()
