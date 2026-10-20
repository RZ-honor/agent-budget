# -*- coding: utf-8 -*-
"""项目内所有落盘位置的唯一来源。"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]          # projects/agent-budget
REPO = PROJECT.parents[1]                              # agenitic_mem 仓库根
ARTIFACTS = Path(os.environ.get("MSB_ARTIFACTS") or PROJECT / "artifacts")
REPORTS = Path(os.environ.get("MSB_REPORTS") or PROJECT / "reports")
PYLIBS = Path(os.environ.get("MSB_PYLIBS") or PROJECT / "third_party" / "pylibs")
VENDOR_MINI = Path(os.environ.get("MSB_MINI_SWE") or PROJECT / "third_party" / "mini-swe-agent")
AGENT_HARNESS = Path(os.environ.get("MSB_AGENT_HARNESS") or PROJECT / "third_party" / "agent-harness")
