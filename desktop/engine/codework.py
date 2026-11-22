"""编码工作台：多家模型轮流出方案、互相挑漏洞，最后收敛；每步留痕，可按步回滚。

每一步都落盘：谁说的、说了什么、花了多少 token/钱。
每轮结束给工作目录拍一张快照，回滚就是把那张快照贴回来。
"""
from __future__ import annotations

import json
import os
import re
import sys
import shutil
import threading
import time
import uuid

try:
    import providers as pv
except Exception:  # pragma: no cover
    pv = None

HOME = os.environ.get("ASTER_HOME") or os.path.expanduser("~/Library/Application Support/Aster/memory")
ROOT = os.path.join(HOME, "codework")
JOBS: dict = {}
_CTX = threading.local()

PLAN_FIRST_PROMPT = (
    "先不要写代码。用 6 行以内说清方案：\n"
    "1. 你打算怎么做（一句）；\n"
    "2. 会改哪些文件；\n"
    "3. 关键取舍（为什么这样、不那样）；\n"
    "4. 怎么验证做成了。\n"
    "不要客套。"
)

PROPOSE = (
    "你是「{name}」，请就下面的任务给出一版可运行的代码或改动方案。\n"
    "要求：\n"
    "1. 先一句话说清思路，再给代码；\n"
    "2. 代码放在 ``` 里，标明语言与文件路径；\n"
    "3. 用到的假设要写出来；\n"
    "4. 不要客套，不要复述任务。"
)

CRITIQUE = (
    "你是「{name}」，只做一件事：挑这份方案的硬伤。\n"
    "1. 指最致命的两个问题，引原句；\n"
    "2. 指出没写出来的前提；\n"
    "3. 给出可直接替换的写法或补丁片段；\n"
    "不要夸奖，不要总结。"
)

MERGE = (
    "你是「{name}」，把下面的提案与批评合成一版最终代码。\n"
    "规则：\n"
    "1. 采纳的部分写进代码，不采纳的用一句话注明理由；\n"
    "2. 只输出：一段「决策记录」（每条一行），然后完整代码；\n"
    "3. 代码放 ``` 里，标明文件路径。"
)


def _dir(job_id: str) -> str:
    return os.path.join(ROOT, job_id)


def _save(job: dict) -> None:
    try:
        os.makedirs(_dir(job["id"]), exist_ok=True)
        p = os.path.join(_dir(job["id"]), "job.json")
        open(p, "w", encoding="utf-8").write(json.dumps(job, ensure_ascii=False, indent=2))
    except Exception:
        pass


def _snapshot(job: dict, label: str) -> str:
    """把工作目录整份拍下来；回滚就是贴回来。"""
    src = os.path.join(_dir(job["id"]), "work")
    dst = os.path.join(_dir(job["id"]), "snap-" + label)
    try:
        if os.path.isdir(src):
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            return dst
    except Exception:
        pass
    return ""


def _files(job: dict, code: str) -> list:
    """从代码块里认出「文件路径」，真写进工作目录。"""
    work = os.path.join(_dir(job["id"]), "work")
    os.makedirs(work, exist_ok=True)
    code = code or ""
    if code.count("```") % 2 == 1:      # 被截断的代码块，补一个收尾，别把内容丢掉
        code += "\n```"
    written = []
    for m in re.finditer(r"```([a-zA-Z0-9+#-]*)\s*(?:\n|$)(.*?)```", code or "", re.S):
        body = m.group(2)
        first = body.splitlines()[0] if body.splitlines() else ""
        rel = ""
        mm = re.search(r"(?:^|[\s（(])([\w./-]+\.(?:py|js|ts|tsx|jsx|html|css|json|md|sh|yml|yaml))", first)
        if mm:
            rel = mm.group(1)
            body = "\n".join(body.splitlines()[1:])
        if not rel:
            rel = "draft-" + str(len(written) + 1) + ".txt"
        rel = rel.lstrip("/").replace("..", "")
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        open(path, "w", encoding="utf-8").write(body)
        written.append(rel)
    return written


PLAN_PROMPT = (
    "把这份结论拆成可执行的步骤清单。\n"
    "规则：每行一条，格式固定为「- 动作 | 验收标准」，5 到 8 条；\n"
    "动作以动词开头，验收标准要能被验证。只输出清单，不要解释。"
)


def _plan(job: dict, seats: list, final: str) -> list:
    """终稿 → 可勾选的步骤清单，落盘，下次还能接着做。"""
    if not seats or not final:
        return []
    seat = seats[0]
    res = _call(job, seat["provider"],
                  [{"role": "system", "content": PLAN_PROMPT},
                   {"role": "user", "content": final[:3000]}],
                  max_tokens=500, timeout=120, model=seat.get("model") or "")
    text = res.get("text") or ""
    items = []
    for line in text.splitlines():
        line = line.strip().lstrip("-·*0123456789. ")
        if not line or "|" not in line:
            continue
        act, _, acc = line.partition("|")
        items.append({"id": "s" + str(len(items) + 1), "text": act.strip(),
                      "accept": acc.strip(), "done": False})
    if items:
        job["plan"] = items
    job["tokens"]["in"] += int((res.get("tokens") or {}).get("in") or 0)
    job["tokens"]["out"] += int((res.get("tokens") or {}).get("out") or 0)
    job["cost"] = round(float(job["cost"]) + float(res.get("cost") or 0), 4)
    return items


VERIFY_PROMPT = (
    "你是验收员。对照验收标准，逐条判断已完成的产出是否满足。\n"
    "只输出每行一条：编号|满足 或 不满足|一句理由（20 字内）。\n"
    "不许含糊，不许写「建议」。"
)


def _read_work(job: dict, limit: int = 4000) -> str:
    work = os.path.join(_dir(job["id"]), "work")
    if not os.path.isdir(work):
        return ""
    chunks = []
    for root, _d, files in os.walk(work):
        for f in files:
            p2 = os.path.join(root, f)
            rel = os.path.relpath(p2, work)
            try:
                body = open(p2, encoding="utf-8", errors="replace").read()
            except Exception:
                continue
            chunks.append(f"### {rel}\n{body[:1200]}")
    return "\n\n".join(chunks)[:limit]


def verify(jid: str) -> dict:
    """让模型自己对照验收标准打勾，人只做复核。"""
    job = JOBS.get(jid)
    if not job:
        try:
            job = json.load(open(os.path.join(_dir(jid), "job.json"), encoding="utf-8"))
            JOBS[jid] = job
        except Exception:
            return {"ok": False, "error": "没有这条"}
    plan = job.get("plan") or []
    if not plan:
        return {"ok": False, "error": "还没有方案清单"}
    seats = [s for s in job.get("seats", []) if s.get("provider") in pv.usable()]
    if not seats:
        return {"ok": False, "error": "没有可用的模型"}
    if not job.get("files"):
        for st2 in job.get("steps", []):
            got = _files(job, st2.get("text") or "")
            if got:
                job["files"] = got
        _save(job)
    code = _read_work(job)
    listing = "\n".join(
        f"{i+1}|{it['text']}|{it.get('accept', '')}" for i, it in enumerate(plan))
    res = _call(job, seats[0]["provider"],
                  [{"role": "system", "content": VERIFY_PROMPT},
                   {"role": "user", "content": f"验收标准：\n{listing}\n\n产出：\n{code}"}],
                  max_tokens=600, timeout=150, model=seats[0].get("model") or "")
    text = res.get("text") or ""
    for line in text.splitlines():
        parts = [x.strip() for x in line.replace("｜", "|").split("|")]
        if len(parts) < 2:
            continue
        try:
            idx = int(re.sub(r"[^0-9]", "", parts[0]) or 0) - 1
        except Exception:
            continue
        if 0 <= idx < len(plan):
            plan[idx]["done"] = ("不满足" not in parts[1]) and ("满足" in parts[1] or "通过" in parts[1])
            plan[idx]["reason"] = parts[2] if len(parts) > 2 else ""
            plan[idx]["by"] = "model"
    job["plan"] = plan
    job["tokens"]["in"] += int((res.get("tokens") or {}).get("in") or 0)
    job["tokens"]["out"] += int((res.get("tokens") or {}).get("out") or 0)
    job["cost"] = round(float(job["cost"]) + float(res.get("cost") or 0), 4)
    _save(job)
    return {"ok": True, "plan": plan, "raw": text[:400]}


def toggle(jid: str, item_id: str, done: bool) -> dict:
    job = JOBS.get(jid) or {}
    if not job:
        try:
            job = json.load(open(os.path.join(_dir(jid), "job.json"), encoding="utf-8"))
            JOBS[jid] = job
        except Exception:
            return {"ok": False, "error": "没有这条"}
    for it in job.get("plan") or []:
        if it["id"] == item_id:
            it["done"] = bool(done)
    _save(job)
    return {"ok": True, "plan": job.get("plan") or []}


def _normalize_seats(seats: list) -> list:
    """同一家模型可以坐两个席位，但立场不能一样，否则两票同一个人。"""
    seen = {}
    out = []
    for s0 in seats or []:
        if not s0.get("provider"):
            continue
        n = seen.get(s0["provider"], 0) + 1
        seen[s0["provider"]] = n
        role = (s0.get("role") or "").strip()
        if n > 1 and (not role or role == out[-1].get("role")):
            role = "评审"
        out.append({**s0, "role": role})
    return out


def start_job(task: str, seats: list, rounds: int = 3, budget: float = 0.0) -> dict:
    jid = uuid.uuid4().hex[:10]
    seats = _normalize_seats(seats)
    job = {"id": jid, "task": task, "seats": seats, "rounds": max(1, min(5, int(rounds or 3))),
           "state": "running", "steps": [], "notes": [], "stop": False, "t0": time.time(),
           "tokens": {"in": 0, "out": 0}, "cost": 0.0, "files": [], "snaps": [],
           "budget": float(budget or 0)}
    JOBS[jid] = job
    _save(job)

    def worker():
        _CTX.job = job
        try:
            _run(job)
            job["state"] = "done"
        except Exception as e:  # pragma: no cover
            job["state"] = "failed"
            job["error"] = str(e)[:200]
        job["ms"] = int((time.time() - job["t0"]) * 1000)
        _save(job)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "id": jid}


def _over_budget(job: dict) -> bool:
    b = float(job.get("budget") or 0)
    return bool(b) and float(job.get("cost") or 0) >= b


def _call(job: dict, provider: str, msgs: list, **kw) -> dict:
    """所有模型调用都从这里走：先查预算，超了就不发。"""
    if _over_budget(job):
        job["stop"] = True
        job["stop_reason"] = "到预算上限了"
        return {"ok": False, "error": "超预算", "tokens": {}, "cost": 0}
    return pv.call(provider, msgs, **kw)


def _lint(job: dict) -> dict:
    """写出来的东西先过一遍语法检查，别把坏文件当成品。"""
    import subprocess
    work = os.path.join(_dir(job["id"]), "work")
    out = {}
    py = sys.executable if "sys" in globals() else "python3"
    for root, _d, files in os.walk(work):
        for f in files:
            p2 = os.path.join(root, f)
            rel = os.path.relpath(p2, work)
            if f.endswith(".py"):
                r = subprocess.run([py, "-m", "py_compile", p2], capture_output=True, text=True, timeout=30)
                out[rel] = "ok" if r.returncode == 0 else (r.stderr or "语法错误")[:300]
    job["lint"] = out
    _save(job)
    return out


def _digest(text: str) -> str:
    """一句话说清这步干了什么：有文件名报文件名，没有就取首句。"""
    t = re.sub(r"```.*?```", "", text or "", flags=re.S)
    t = re.sub(r"\s+", " ", t).strip()
    m = re.search(r"([\w./-]+\.(?:py|js|ts|html|css|json|md|sh))", text or "")
    head = t[:70]
    return (("写出 " + m.group(1) + " · ") if m else "") + head


def _chat_line(text: str) -> None:
    """进度直接写进对话记录，切页、重开都在，不靠界面重放。"""
    try:
        p2 = os.path.join(HOME, "chat.ndjson")
        with open(p2, "a", encoding="utf-8") as f:
            f.write(json.dumps({"who": "her", "text": text, "ts": int(time.time()),
                                "kind": "progress"}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _step(job: dict, kind: str, seat: dict, text: str, res: dict) -> None:
    st = {"n": len(job["steps"]) + 1, "kind": kind, "who": seat["provider"],
          "digest": _digest(text),
          "role": seat.get("role") or ("作者" if kind != "critique" else "评审"),
          "text": text, "tokens": res.get("tokens") or {}, "cost": res.get("cost") or 0}
    job["steps"].append(st)
    _chat_line("#" + str(st["n"]) + " " + str(st["role"]) + " · " + str(st["who"]) +
               (" · " + st["digest"] if st.get("digest") else ""))
    job["tokens"]["in"] += int((res.get("tokens") or {}).get("in") or 0)
    job["tokens"]["out"] += int((res.get("tokens") or {}).get("out") or 0)
    job["cost"] = round(float(job["cost"]) + float(res.get("cost") or 0), 4)
    _save(job)


def go(jid: str) -> dict:
    """看清方案之后放行。"""
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这条"}
    job["go"] = True
    return {"ok": True}


def _run(job: dict) -> None:
    if pv is None:
        raise RuntimeError("providers 模块没起来")
    seats = [s for s in job["seats"] if s.get("provider")]
    missing = [s["provider"] for s in seats if s["provider"] not in pv.usable()]
    if missing:
        raise RuntimeError("这些还没配置好：" + "、".join(missing))
    if job.get("plan_first", True):
        seat0 = seats[0]
        res0 = _call(job, seat0["provider"],
                     [{"role": "system", "content": PLAN_FIRST_PROMPT},
                      {"role": "user", "content": "任务：" + job["task"]}],
                     max_tokens=700, timeout=120, model=seat0.get("model") or "")
        plan_text = res0.get("text") or "（没给出方案）"
        job["stage"] = "plan"
        job["plan_text"] = plan_text
        _chat_line("方案（先看，再动手）：\n" + plan_text[:400])
        _save(job)
        waited = 0
        while not job.get("go") and not job.get("stop") and waited < 900:
            time.sleep(2)
            waited += 2
        if job.get("stop"):
            job["stop_reason"] = job.get("stop_reason") or "方案阶段被叫停"
            return
        _chat_line("好，按这个方案动手。")

    proposals = []
    critiques = []
    for r in range(1, job["rounds"] + 1):
        if job.get("stop"):
            break
        note = job["notes"][-1] if job.get("notes") else ""
        for seat in seats:
            if job.get("stop") or _over_budget(job):
                if _over_budget(job):
                    job["stop"] = True
                    job["stop_reason"] = "到预算上限了"
                break
            user = f"任务：{job['task']}"
            if note:
                user += f"\n\n出题人插话（必须回应）：{note}"
            if proposals:
                user += "\n\n上一轮方案：\n" + proposals[-1][:2500]
            if critiques:
                user += "\n\n上一轮批评：\n" + "\n".join(critiques)[-1500:]
            msgs = [{"role": "system", "content": PROPOSE.format(name=seat.get("role") or seat["provider"])},
                    {"role": "user", "content": user}]
            res = _call(job, seat["provider"], msgs, max_tokens=2400, timeout=240,
                          model=seat.get("model") or "")
            text = res.get("text") or ("" if res.get("ok") else f"（没答上来：{res.get('error','')}）")
            _step(job, "propose", seat, text, res)
            proposals.append(text)
            job["files"] = _files(job, text) or job["files"]
        # 挑漏洞
        if len(seats) > 1:
            critiques = []
            for seat in seats:
                if job.get("stop"):
                    break
                msgs = [{"role": "system", "content": CRITIQUE.format(name=seat.get("role") or seat["provider"])},
                        {"role": "user", "content": f"任务：{job['task']}\n\n待评方案：\n" + proposals[-1][:3000]}]
                res = _call(job, seat["provider"], msgs, max_tokens=900, timeout=180,
                              model=seat.get("model") or "")
                text = res.get("text") or ("" if res.get("ok") else f"（没答上来：{res.get('error','')}）")
                _step(job, "critique", seat, text, res)
                critiques.append(f"【{seat.get('role') or seat['provider']}】{text}")
        snap = _snapshot(job, "r" + str(r))
        if snap:
            job["snaps"].append({"round": r, "path": snap, "label": "第 " + str(r) + " 轮"})
        _save(job)
        if job.get("stop"):
            break
    # 收敛
    if not job.get("stop") and seats:
        seat = seats[0]
        msgs = [{"role": "system", "content": MERGE.format(name=seat.get("role") or seat["provider"])},
                {"role": "user", "content": f"任务：{job['task']}\n\n最后一版方案：\n{proposals[-1] if proposals else ''}"
                                            f"\n\n批评：\n" + "\n".join(critiques)[-2000:]}]
        res = _call(job, seat["provider"], msgs, max_tokens=3000, timeout=260, model=seat.get("model") or "")
        text = res.get("text") or ""
        _step(job, "merge", seat, text, res)
        job["final"] = text
        job["files"] = _files(job, text) or job["files"]
        if not job.get("files") and text:
            work = os.path.join(_dir(job["id"]), "work")
            os.makedirs(work, exist_ok=True)
            open(os.path.join(work, "final.md"), "w", encoding="utf-8").write(text)
            job["files"] = ["final.md"]
        try:
            _lint(job)
        except Exception:
            pass
        _plan(job, seats, text)
        try:
            verify(job["id"])
        except Exception:
            pass
        snap = _snapshot(job, "final")
        if snap:
            job["snaps"].append({"round": "final", "path": snap, "label": "终稿"})
        _save(job)


def _alive(jid: str) -> bool:
    return bool(JOBS.get(jid)) and JOBS[jid].get("state") == "running"


def resume(jid: str) -> dict:
    """被打断的活接着跑：沿用同一份步骤记录，继续往下走。"""
    import threading
    job = JOBS.get(jid)
    if not job:
        try:
            job = json.load(open(os.path.join(_dir(jid), "job.json"), encoding="utf-8"))
        except Exception:
            return {"ok": False, "error": "没有这条"}
    if job.get("state") == "running":
        return {"ok": False, "error": "还在跑"}
    job["state"] = "running"
    job["stop"] = False
    job["go"] = True
    job.pop("stop_reason", None)
    JOBS[jid] = job
    _save(job)

    def worker():
        _CTX.job = job
        try:
            _run(job)
            job["state"] = "done"
        except Exception as e:
            job["state"] = "failed"
            job["error"] = str(e)[:200]
        job["ms"] = int((time.time() - job["t0"]) * 1000)
        _save(job)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "id": jid}


def status(jid: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        # 从磁盘捡回来
        try:
            job = json.load(open(os.path.join(_dir(jid), "job.json"), encoding="utf-8"))
            JOBS[jid] = job
        except Exception:
            return {"ok": False, "error": "没有这条"}
    if job.get("state") == "running" and jid not in JOBS:
        job["state"] = "interrupted"
    job["workdir"] = os.path.join(_dir(jid), "work")
    return {"ok": True, **job}


def stop(jid: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这条"}
    job["stop"] = True
    return {"ok": True}


def say(jid: str, text: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这条"}
    t = (text or "").strip()
    if t:
        job["notes"].append(t)
    return {"ok": True, "notes": job["notes"]}


def rollback(jid: str, label: str) -> dict:
    """把工作目录退回某张快照。"""
    src = os.path.join(_dir(jid), "snap-" + str(label))
    dst = os.path.join(_dir(jid), "work")
    if not os.path.isdir(src):
        return {"ok": False, "error": "没有这张快照"}
    try:
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        return {"ok": True, "restored": label, "files": sorted(os.listdir(dst))}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
