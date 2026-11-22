#!/usr/bin/env python3
"""所有「记忆住哪」的判断只走这里，别在各模块里各算一遍。

优先级：
  1. $ASTER_HOME（App 里指向 ~/Library/Application Support/Aster/memory）
  2. 脚本同目录下的 memory/（仓库里的 scripts/… ，打包后的 engine/…）
"""
from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))


def mem_home() -> str:
    h = os.environ.get("ASTER_HOME")
    if h:
        os.makedirs(h, exist_ok=True)
        return h
    # 打包后没有 ASTER_HOME 时，不能写进 app 包体（会被当成用户数据夹带）
    if "Contents/Resources" in HERE:
        d = os.path.expanduser("~/Library/Application Support/Aster/memory")
    else:
        d = os.path.join(HERE, "memory")
    os.makedirs(d, exist_ok=True)
    return d


def mem_file(*parts: str) -> str:
    return os.path.join(mem_home(), *parts)
