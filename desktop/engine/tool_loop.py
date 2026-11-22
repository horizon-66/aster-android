#!/usr/bin/env python3
"""最小工具循环：模型提工具 → 执行 → 结果回灌 → 再问，最多 N 步。

工具：list_dir / read_file / run_cmd（与 /tool/exec 同一套语义与权限）。
只在显式开启「干活模式」时使用；普通对话不走这里。
"""
from __future__ import annotations
import json, os, re, subprocess, time

MAX_STEPS = 4
MAX_RESULT = 4000


def prune(text: str, head: int = 40, tail: int = 10, cap: int = MAX_RESULT) -> str:
    """工具输出精简：留头 40 行 + 尾 10 行，中间省略并标注省了多少。

    长日志/大文件全量塞回模型既慢又贵，关键信息通常只在头尾。
    """
    t = text or ""
    if len(t) <= cap:
        return t
    lines = t.splitlines()
    if len(lines) <= head + tail + 5:
        return t[:cap] + f"\n…（后面还有 {len(t)-cap} 字，已省略）"
    kept = lines[:head] + [f"…（中间省略 {len(lines)-head-tail} 行）"] + lines[-tail:]
    out = "\n".join(kept)
    return out[:cap]

TOOLS_DOC = """可用工具（只能输出 JSON，不要输出别的）：
{"tool":"list_dir","args":{"path":"目录"}}
{"tool":"read_file","args":{"path":"文件","max_chars":4000}}
{"tool":"run_cmd","args":{"cmd":"命令","timeout":20}}
{"tool":"done","args":{"answer":"给用户的话"}}

规则：先想清楚要看什么，一次只调一个工具；拿到结果再决定下一步；最多 4 步。
如果不需要工具，直接输出 {"tool":"done","args":{"answer":"..."}}。
"""


def _root() -> str:
    """优先用「这件事自己的目录」（ASTER_WORKSPACE），没有才用主工作区。"""
    w = os.environ.get("ASTER_WORKSPACE")
    if w and os.path.isdir(w):
        return w
    return os.environ.get("ASTER_WORKSPACE") or os.path.expanduser("~/Documents/Aster")


def _inside(p: str) -> bool:
    try:
        rp, rr = os.path.realpath(p), os.path.realpath(_root())
        return rp == rr or rp.startswith(rr + os.sep)
    except Exception:
        return False


ALIAS = {"桌面": "~/Desktop", "下载": "~/Downloads", "文档": "~/Documents", "家": "~",
         "当前目录": "", ".": "", "./": ""}


def _fix_path(p: str) -> str:
    p = (p or "").strip()
    for k, v in ALIAS.items():
        if p == k:
            return v or _root()
        if p.startswith(k + "/"):
            return os.path.expanduser(v) + p[len(k):]
    if p.startswith("~"):
        return os.path.expanduser(p)
    if p in ("", "/"):
        return _root() if p == "" else p
    return p


_FUSE_SEEN = {}


def _fuse_hit(tool: str, args: dict) -> bool:
    """幂等熔断：同一个工具 + 同一组参数，短时间连中 2 次就断。"""
    try:
        import hashlib, json as _js
        key = tool + "|" + _js.dumps(args, ensure_ascii=False, sort_keys=True)
        h = hashlib.sha1(key.encode()).hexdigest()[:12]
        _FUSE_SEEN[h] = _FUSE_SEEN.get(h, 0) + 1
        if _FUSE_SEEN[h] > 2:
            _FUSE_SEEN[h] = 0        # 熔断后清零，允许换个说法再来
            return True
    except Exception:
        pass
    return False


def run_tool(tool: str, args: dict, perm: str = "workspace") -> dict:
    if _fuse_hit(tool, args):
        # 熔断也要落库：用 dedup_rejected 跟「真失败」分开，排查时才看得出是被挡掉的
        try:
            record_run("[fused] " + tool + " " + json.dumps(args, ensure_ascii=False, sort_keys=True)[:200],
                       {"code": "dedup_rejected", "stdout": "", "stderr": "idempotent fuse"})
        except Exception:
            pass
        return {"ok": False, "fused": True, "error_code": "dedup_rejected",
                "error": "这个调用重复了，换个思路或换个参数再试"}
    if isinstance(args, dict) and args.get("path"):
        args = dict(args, path=_fix_path(args["path"]))
    if tool == "list_dir":
        p = args.get("path") or _root()
        if perm != "full" and not _inside(p):
            return {"ok": False, "error": "超出工作区"}
        try:
            names = sorted(os.listdir(p))[:200]
            return {"ok": True, "path": p, "entries": names}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    if tool == "read_file":
        p = args.get("path") or ""
        if not p or not os.path.isfile(p):
            return {"ok": False, "error": "文件不存在"}
        if perm == "read" and not _inside(p):
            return {"ok": False, "error": "只读档只允许读工作区内"}
        if perm == "workspace" and not _inside(p):
            return {"ok": False, "error": "超出工作区"}
        try:
            n = int(args.get("max_chars") or 4000)
            return {"ok": True, "path": p, "text": prune(open(p, encoding="utf-8", errors="replace").read(n))}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}

    # 三档权限：只看 / 每次问 / 全开（在策略之前先判）
    try:
        import permissions as _pm
        _pv = _pm.decide(tool, args or {})
        if not _pv["allow"] and not _pv["need_confirm"]:
            return {"ok": False, "error": _pv["reason"], "blocked": True}
        if _pv["need_confirm"]:
            if tool == "run_cmd":
                return request_cmd((args or {}).get("cmd") or "")
            return {"ok": False, "need_confirm": True, "error": _pv["reason"]}
    except Exception:
        pass

    if tool == "run_cmd":
        if perm == "read":
            return {"ok": False, "error": "只读档不允许跑命令"}
        cmd = (args.get("cmd") or "").strip()
        if not cmd:
            return {"ok": False, "error": "命令为空"}
        # 2026-11-13：白名单 + 黑名单（命令+参数粒度），默认 deny
        try:
            import tool_policy as _pol
            allowed, level, why = _pol.check(cmd)
            if not allowed:
                return {"ok": False, "error": why, "blocked": True}
            # 干跑只在明确要求时；平时按权限档走（档 1 已在上面被拦）
            if os.environ.get("ASTER_TOOL_DRYRUN") == "1":
                return {"ok": True, "dry_run": True, "cmd": cmd,
                        "idem": _pol.idem_key(cmd, _root()), "note": "干跑：未执行"}
        except Exception:
            return {"ok": False, "error": "策略模块不可用，拒绝执行"}
        try:
            r = subprocess.run(cmd, shell=True, cwd=_root() if os.path.isdir(_root()) else None,
                               capture_output=True, timeout=int(args.get("timeout") or 20))
            return {"ok": True, "code": r.returncode,
                    "stdout": prune((r.stdout or b"").decode("utf-8", "replace")),
                    "stderr": (r.stderr or b"").decode("utf-8", "replace")[:1200]}
        except Exception as e:
            return {"ok": False, "error": str(e)[:160]}

    return {"ok": False, "error": "未知工具：" + tool}


def _parse(out: str) -> dict:
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {}



# ---------- 回灌幂等：同一命令同一目录只记一次 ----------
SCHEMA = """
create table if not exists tool_runs(
  idem     text primary key,
  cmd      text not null,
  code     integer,
  stdout   text,
  stderr   text,
  at       integer not null,
  times    integer default 1
)
"""


def _db():
    import sqlite3
    p = os.path.join(os.environ.get("ASTER_HOME") or _root(), "tool_runs.db")
    c = sqlite3.connect(p)
    c.execute(SCHEMA)
    return c


def record_run(cmd: str, res: dict) -> str:
    """幂等落库：键相同就只把 times +1，不新增记录。"""
    try:
        import tool_policy as _pol
        key = _pol.idem_key(cmd, _root())
        c = _db()
        with c:
            row = c.execute("select times from tool_runs where idem=?", (key,)).fetchone()
            if row:
                c.execute("update tool_runs set times=times+1, at=? where idem=?",
                          (int(time.time()), key))
            else:
                c.execute("insert into tool_runs(idem, cmd, code, stdout, stderr, at, times) "
                          "values(?,?,?,?,?,?,1)",
                          (key, cmd[:400], res.get("code"), (res.get("stdout") or "")[:2000],
                           (res.get("stderr") or "")[:800], int(time.time())))
        c.close()
        return key
    except Exception:
        return ""


def recent_runs(limit: int = 20) -> list[dict]:
    try:
        c = _db()
        rows = c.execute("select cmd, code, at, times from tool_runs order by at desc limit ?",
                         (limit,)).fetchall()
        c.close()
        return [{"cmd": r[0], "code": r[1], "at": r[2], "times": r[3]} for r in rows]
    except Exception:
        return []


def loop(ask_model, request: str, perm: str = "workspace", max_steps: int = MAX_STEPS) -> dict:
    """ask_model(prompt) -> 文本。返回 {ok, steps, answer}。"""
    hist = []
    for step in range(1, max_steps + 1):
        prompt = (TOOLS_DOC + "\n用户请求：" + request + "\n" +
                  ("\n之前的结果：\n" + "\n".join(hist) if hist else "") +
                  "\n下一步：")
        raw = ask_model(prompt) or ""
        obj = _parse(raw)
        tool = (obj.get("tool") or "").strip()
        args = obj.get("args") or {}
        if not tool:
            return {"ok": False, "steps": step, "answer": raw.strip()[:500], "error": "模型没给出工具调用"}
        if tool == "done":
            return {"ok": True, "steps": step, "answer": str(args.get("answer") or "").strip()}
        res = run_tool(tool, args, perm)
        hist.append(f"[{tool}] {json.dumps(args, ensure_ascii=False)} → {json.dumps(res, ensure_ascii=False)[:600]}")
        if not res.get("ok"):
            # 把错误喂回去，让模型自己改，不要一出错就整轮失败
            hist.append(f"[错误] {res.get('error')}；请换个路径或换个工具再试")
            continue
    return {"ok": False, "steps": max_steps, "answer": "", "error": "步数用完"}


# ---------- 审批回路：请求 → 挂起 → 人点 → 执行 ----------
APPROVE_SCHEMA = """
create table if not exists tool_approvals(
  id     text primary key,
  cmd    text not null,
  idem   text,
  status text not null default 'pending',
  at     integer not null,
  by     text default ''
)
"""


def _adb():
    import sqlite3
    p = os.path.join(os.environ.get("ASTER_HOME") or _root(), "tool_runs.db")
    c = sqlite3.connect(p)
    c.execute(SCHEMA); c.execute(APPROVE_SCHEMA)
    return c


def request_cmd(cmd: str) -> dict:
    """过策略 → 挂起等确认；这一步永远不执行。"""
    import tool_policy as _pol
    import uuid
    ok, level, why = _pol.check(cmd)
    if not ok:
        return {"ok": False, "error": why}
    rid = uuid.uuid4().hex[:12]
    c = _adb()
    with c:
        c.execute("insert into tool_approvals(id, cmd, idem, status, at) values(?,?,?,?,?)",
                  (rid, cmd[:400], _pol.idem_key(cmd, _root()), "pending", int(time.time())))
    c.close()
    return {"ok": True, "id": rid, "cmd": cmd, "level": level, "note": why}


def agent_prefs_path() -> str:
    return os.path.join(os.environ.get("ASTER_HOME") or _root(), "prefs_agent.json")


def agent_prefs() -> dict:
    """电脑操作的权限档：干跑/真跑、能碰的范围。默认干跑 + 只工作区。"""
    try:
        d = json.load(open(agent_prefs_path(), encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("mode", "dry")
    d.setdefault("scope", "workspace")
    return d


def set_agent_prefs(mode: str | None = None, scope: str | None = None) -> dict:
    d = agent_prefs()
    if mode in ("dry", "real"):
        d["mode"] = mode
    if scope in ("workspace", "full"):
        d["scope"] = scope
    try:
        os.makedirs(os.path.dirname(agent_prefs_path()), exist_ok=True)
        json.dump(d, open(agent_prefs_path(), "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return d


def approve(rid: str, dry: bool | None = None) -> dict:
    """人点了同意：干跑模式下只标记，不执行。"""
    if dry is None:
        env = os.environ.get("ASTER_TOOL_DRYRUN")
        if env is not None:
            dry = env != "0"
        else:
            dry = agent_prefs().get("mode") != "real"
    c = _adb()
    row = c.execute("select cmd, status from tool_approvals where id=?", (rid,)).fetchone()
    if not row:
        c.close(); return {"ok": False, "error": "没有这条请求"}
    cmd, status = row
    if status != "pending":
        c.close(); return {"ok": False, "error": "已经处理过：" + status}
    with c:
        c.execute("update tool_approvals set status=? where id=?", ("approved" if dry else "ran", rid))
    c.close()
    if dry:
        return {"ok": True, "dry_run": True, "cmd": cmd, "note": "干跑：只标记，不执行"}
    _pf = agent_prefs()
    res = run_tool("run_cmd", {"cmd": cmd}, "full" if _pf.get("scope") == "full" else "workspace")
    record_run(cmd, res)
    return {"ok": True, "result": res}


def pending(limit: int = 20) -> list[dict]:
    try:
        c = _adb()
        rows = c.execute("select id, cmd, status, at from tool_approvals order by at desc limit ?",
                         (limit,)).fetchall()
        c.close()
        return [{"id": r[0], "cmd": r[1], "status": r[2], "at": r[3]} for r in rows]
    except Exception:
        return []


# ---------- 后台跑：前台照常说，跑完择机带回 ----------
RESULTS_SCHEMA = """
create table if not exists tool_results(
  idem   text primary key,
  cmd    text not null,
  code   integer,
  stdout text,
  stderr text,
  at     integer not null,
  delivered integer default 0
)
"""


def _rdb():
    import sqlite3
    p = os.path.join(os.environ.get("ASTER_HOME") or _root(), "tool_runs.db")
    c = sqlite3.connect(p)
    c.execute(SCHEMA); c.execute(APPROVE_SCHEMA); c.execute(RESULTS_SCHEMA)
    return c


def run_async(cmd: str) -> dict:
    """后台执行：立刻返回，结果落库，等下一轮带进上下文。"""
    import threading
    key = ""
    try:
        import tool_policy as _pol
        ok, _lvl, why = _pol.check(cmd)
        if not ok:
            return {"ok": False, "error": why}
        key = _pol.idem_key(cmd, _root())
        c = _rdb()
        row = c.execute("select delivered from tool_results where idem=?", (key,)).fetchone()
        if row:
            c.close()
            return {"ok": True, "idem": key, "dup": True, "note": "这条已经在跑了或跑过了"}
        with c:
            c.execute("insert into tool_results(idem, cmd, code, at) values(?,?,?,?)",
                      (key, cmd[:400], None, int(time.time())))
        c.close()
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

    def _job():
        res = run_tool("run_cmd", {"cmd": cmd}, os.environ.get("ASTER_TOOL_PERM", "workspace"))
        try:
            c = _rdb()
            with c:
                c.execute("update tool_results set code=?, stdout=?, stderr=? where idem=?",
                          (res.get("code"), (res.get("stdout") or "")[:MAX_RESULT],
                           (res.get("stderr") or "")[:1200], key))
            c.close()
            record_run(cmd, res)
        except Exception:
            pass

    threading.Thread(target=_job, daemon=True).start()
    return {"ok": True, "idem": key, "note": "后台跑着，跑完下一轮告诉你"}


def pending_results(limit: int = 3) -> list[dict]:
    """还没带回对话的结果。"""
    try:
        c = _rdb()
        rows = c.execute("select idem, cmd, code, stdout, stderr from tool_results "
                         "where delivered=0 and code is not null order by at limit ?", (limit,)).fetchall()
        c.close()
        return [{"idem": r[0], "cmd": r[1], "code": r[2],
                 "stdout": (r[3] or "")[:1500], "stderr": (r[4] or "")[:400]} for r in rows]
    except Exception:
        return []


def mark_delivered(idem: str) -> None:
    try:
        c = _rdb()
        with c:
            c.execute("update tool_results set delivered=1 where idem=?", (idem,))
        c.close()
    except Exception:
        pass
