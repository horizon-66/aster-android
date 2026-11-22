"""电脑操作的三档权限（2026-11-17 定）：

  1 = 只看不动：读文件、看屏幕；不写、不跑命令
  2 = 动手但每次问：写文件/跑命令都要挂起等你点确认（默认）
  3 = 全开不问：直接做，但黑名单仍硬拦，所有动作进日志

存储：memory/permissions.json {"level": 2, "updated": ts}
红线（哪一档都不许）：删系统文件、提权、转账、对外发布——在 tool_policy.BLACK 里硬拦。
"""
from __future__ import annotations
import json, os, time

LEVELS = {1: "仅可查看", 2: "工作区内修改", 3: "完全权限"}
DEFAULT = 2


def _home() -> str:
    h = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")
    os.makedirs(os.path.join(h, "logs"), exist_ok=True)
    return h


def _path() -> str:
    return os.path.join(_home(), "permissions.json")


def level() -> int:
    try:
        d = json.load(open(_path(), encoding="utf-8"))
        v = int(d.get("level") or DEFAULT)
        return v if v in LEVELS else DEFAULT
    except Exception:
        return DEFAULT


def set_level(v: int) -> dict:
    v = int(v)
    if v not in LEVELS:
        raise ValueError("档位只能是 1/2/3")
    d = {"level": v, "updated": int(time.time())}
    json.dump(d, open(_path(), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    _log({"ts": int(time.time()), "level": v, "action": "set"})
    return d


def _log(rec: dict) -> None:
    try:
        with open(os.path.join(_home(), "logs", "perm.ndjson"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _inside_workspace(path: str) -> bool:
    """只认「这件事自己的目录」和 workspaces 底下。"""
    try:
        root = os.path.realpath(os.path.join(_home(), "workspaces"))
        ws = os.environ.get("ASTER_WORKSPACE") or ""
        p = os.path.realpath(path or ".")
        if ws and p.startswith(os.path.realpath(ws) + os.sep):
            return True
        return p.startswith(root + os.sep)
    except Exception:
        return False


def decide(tool: str, args: dict) -> dict:
    """在执行前问一句：这个动作在当前档位怎么处理。

    返回 {"allow": bool, "need_confirm": bool, "reason": str}
    """
    lv = level()
    write_tools = ("write_file", "run_cmd", "delete_file", "run_skill")
    is_write = tool in write_tools
    if lv == 1 and is_write:
        _log({"ts": int(time.time()), "tool": tool, "level": lv, "verdict": "deny"})
        return {"allow": False, "need_confirm": False, "reason": "当前是「只看」档，不允许动手"}
    if lv == 2 and is_write:
        # 工作区内直接做；动到外面才问（跟 DSH 一样的三档语义）
        _p = str((args or {}).get("path") or (args or {}).get("cwd") or "")
        if tool in ("write_file", "delete_file", "run_cmd") and _p and _inside_workspace(_p):
            _log({"ts": int(time.time()), "tool": tool, "level": lv, "verdict": "allow_in_ws"})
            return {"allow": True, "need_confirm": False, "reason": ""}
        _log({"ts": int(time.time()), "tool": tool, "level": lv, "verdict": "ask"})
        return {"allow": False, "need_confirm": True,
                "reason": "这个动作会动到工作区外面，需要你确认"}
    _log({"ts": int(time.time()), "tool": tool, "level": lv, "verdict": "allow"})
    return {"allow": True, "need_confirm": False, "reason": ""}
