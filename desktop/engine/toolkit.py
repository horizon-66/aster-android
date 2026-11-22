"""工具注册表 + 调用追踪。

目的：把「调哪个工具、什么参数、成没成、多久」从内核分支里抽出来，
后面加闸门（审批）、加循环、加技能包，都挂在这一层，不用再改内核。

四态返回：
  ok            干成了
  error         失败（重试可能有用）
  need_approval 需要人点头（对外发送、花钱、删东西）
  need_input    缺参数，得问人
"""
from __future__ import annotations
import json
import os
import time

HOME = os.environ.get("ASTER_HOME") or os.path.expanduser(
    "~/Library/Application Support/Aster/memory")
TRACE = os.path.join(HOME, "tool_trace.ndjson")
MAX_TRACE = 2000          # 行数上限，超了从头砍

_REGISTRY: dict = {}
GATE = None          # core 会塞一个函数进来：gate(name, spec, args) -> (允许?, 原因)
GATE_ASK = None      # 被闸门挡下时怎么登记待批


def register(name: str, fn, desc: str = "", schema: dict | None = None,
             timeout: int = 120, danger: str = "safe") -> None:
    """danger: safe / external / spend / delete —— 闸门按这个分级。"""
    _REGISTRY[name] = {"fn": fn, "desc": desc, "schema": schema or {},
                       "timeout": timeout, "danger": danger}


def names() -> list:
    return sorted(_REGISTRY)


def spec(name: str) -> dict:
    d = _REGISTRY.get(name) or {}
    return {"name": name, "desc": d.get("desc", ""), "schema": d.get("schema", {}),
            "danger": d.get("danger", "safe")}


def catalog() -> str:
    """给云端看的工具清单：一行一个，带参数说明。"""
    rows = []
    for n in names():
        s = spec(n)
        args = ", ".join(sorted(s["schema"])) or ""
        rows.append("%s(%s)：%s" % (n, args, s["desc"]))
    return "\n".join(rows)


def _trace(name: str, args: dict, status: str, ms: int, note: str = "",
           task_id=None, step=None) -> None:
    try:
        row = {"ts": int(time.time()), "tool": name, "task": task_id, "step": step,
               "args": {k: (str(v)[:120]) for k, v in (args or {}).items()},
               "status": status, "ms": ms, "note": note[:200]}
        with open(TRACE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_trace(n: int = 30) -> list:
    try:
        lines = open(TRACE, encoding="utf-8").read().strip().splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def trim_trace() -> None:
    try:
        if not os.path.exists(TRACE):
            return
        lines = open(TRACE, encoding="utf-8").read().strip().splitlines()
        if len(lines) > MAX_TRACE:
            open(TRACE, "w", encoding="utf-8").write("\n".join(lines[-MAX_TRACE:]) + "\n")
    except Exception:
        pass


def set_gate(fn, on_ask=None) -> None:
    """闸门：危险动作执行前问一句。fn 返回 (allow, reason)。"""
    global GATE, GATE_ASK
    GATE = fn
    GATE_ASK = on_ask


def already_done(task_id, step) -> bool:
    """这一步之前成功过吗——带副作用的步子靠这个判重，别只信 cursor。"""
    for r in recent_trace(500):
        if r.get("task") == task_id and r.get("step") == step and r.get("status") == "ok":
            return True
    return False


def call(name: str, args: dict | None = None, task_id=None, step=None) -> dict:
    """统一入口：返回 {status, result, ms}。意外一律变成 error，不往上抛。"""
    args = args or {}
    item = _REGISTRY.get(name)
    if not item:
        _trace(name, args, "error", 0, "没有这个工具", task_id, step)
        return {"status": "error", "result": "没有这个工具：" + name, "ms": 0}
    # 闸门只管有代价的动作：花钱、删东西、对外发
    if GATE and item.get("danger") in ("spend", "delete", "send"):
        try:
            ok, why = GATE(name, spec(name), args)
        except Exception:
            ok, why = False, "闸门出错"
        if not ok:
            _trace(name, args, "need_approval", 0, why or "需要授权", task_id, step)
            try:
                if GATE_ASK:
                    GATE_ASK(name, args, why or "需要授权")
            except Exception:
                pass
            return {"status": "need_approval", "result": why or "这个动作要你点头才做", "ms": 0}
    t0 = time.time()
    try:
        out = item["fn"](args)
        ms = int((time.time() - t0) * 1000)
        txt = str(out)
        if txt.startswith(("这条命令不允许", "需要你确认", "没有权限", "现在没有")):
            status = "need_approval"
        elif name == "run" and ("[stderr]" in txt or "Traceback" in txt or "Error" in txt):
            status = "error"          # 命令跑挂了就别当好
        else:
            status = "ok"
        _trace(name, args, status, ms, task_id=task_id, step=step)
        trim_trace()
        return {"status": status, "result": txt, "ms": ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        _trace(name, args, "error", ms, str(e), task_id, step)
        return {"status": "error", "result": "工具出错：" + str(e)[:150], "ms": ms}
