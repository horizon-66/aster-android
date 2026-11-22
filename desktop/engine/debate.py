"""多方议事：把同一个议题分发给几家模型，按轮对抗，最后出结构化结论。

设计上的三条硬规矩：
1. 默认 2 轮，最多 4 轮——轮数一多，钱翻倍、模型还会忘了自己说过什么
2. 熔断：某一轮出现「我同意」「你说得对」这类字眼占比过高，或与上一轮高度重复，提前收
3. 结论不给自由总结，固定三段：共识 / 分歧 / 建议
"""
from __future__ import annotations

import json
import os
import re
import time

try:
    import providers as pv
except Exception:  # pragma: no cover
    pv = None

HOME = os.environ.get("ASTER_HOME") or os.path.expanduser("~/Library/Application Support/Aster/memory")
OUT = os.path.join(HOME, "debates")
JOBS: dict = {}
import threading as _th
_CTX = _th.local()   # 当前线程绑定的任务（worker 里设置）   # 运行中的议事：id -> {stop, notes, log, state}

AGREE = ("我同意", "同意你的", "你说得对", "完全赞同", "没有异议", "agree with", "you are right")

NEUTRAL_PROMPT = (
    "你是这场议事中的一位与会者，没有预设立场。\n"
    "规则：\n"
    "1. 先自己判断这件事的关键在哪，再给结论；\n"
    "2. 如果别人说得对，就承认并补上他漏的那一块；如果不对，指出具体哪一句；\n"
    "3. 最多 200 字，不要客套，不要复述。"
)

ROLE_PROMPT = (
    "你是这场议事中的「{role}」。立场固定，不要客套，不要复述别人。\n"
    "规则：\n"
    "1. 只谈议题，最多 200 字；\n"
    "2. 必须指出对方上一轮里你不同意的那一句，并说明为什么；\n"
    "3. 如果确实被说服，就明说被哪一句说服、因此改了什么；\n"
    "4. 不要写「总的来说」「综上所述」这种收尾。"
)

JUDGE_PROMPT = (
    "你是这场议事的记录者，不做评判者。请只输出下面三块，每块不超过三条，短句：\n"
    "【共识】\n- …\n【分歧】\n- …\n【建议】\n- …\n"
    "不许添加别的段落，不许写总结陈词。"
)


def cfg_label(name: str) -> str:
    try:
        return (pv.get(name) or {}).get("label") or name
    except Exception:
        return name


def _say_line(text: str) -> None:
    """议事过程也写进对话记录：回来能看到他们怎么吵的，不只一个结论。"""
    try:
        p2 = os.path.join(HOME, "chat.ndjson")
        with open(p2, "a", encoding="utf-8") as f:
            f.write(json.dumps({"who": "her", "text": text, "ts": int(time.time()),
                                "kind": "debate"}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _norm(t: str) -> str:
    return re.sub(r"[\s，。、；：！？,.;:!?\"'（）()]", "", t or "")


def _similar(a: str, b: str) -> float:
    """粗算两段话的重合度，够用来判断是不是在复读。"""
    sa, sb = set(_norm(a)), set(_norm(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(1, min(len(sa), len(sb)))


def _agrees(text: str) -> bool:
    t = (text or "").lower()
    return any(g.lower() in t for g in AGREE)


def _normalize_seats(seats: list) -> list:
    seen, out = {}, []
    for s0 in seats or []:
        if not s0.get("provider"):
            continue
        n = seen.get(s0["provider"], 0) + 1
        seen[s0["provider"]] = n
        role = (s0.get("role") or "").strip()
        if n > 1 and (not role or role == out[-1].get("role")):
            role = "另一种看法" if False else "另一视角"
        out.append({**s0, "role": role})
    return out


def run(topic: str, seats: list, rounds: int = 2, max_tokens: int = 500) -> dict:
    """seats: [{"provider": "deepseek", "role": "创新派"}, ...]"""
    if pv is None:
        return {"ok": False, "error": "providers 模块没起来"}
    rounds = max(1, min(4, int(rounds or 2)))
    seats = _normalize_seats(seats)
    if len(seats) < 2:
        return {"ok": False, "error": "至少要两个人"}
    missing = [s["provider"] for s in seats if s["provider"] not in pv.usable()]
    if missing:
        return {"ok": False, "error": "这些还没配置好：" + "、".join(missing)}

    t_start = time.time()
    tokens_in = tokens_out = 0
    cost = 0.0
    transcript = []           # [{round, provider, role, text, ms}]
    per_round = []
    history_text = ""
    stop_reason = ""
    cost_calls = 0

    job = getattr(_CTX, "job", None)
    for r in range(1, rounds + 1):
        if job is not None and job.get("stop"):
            stop_reason = "你叫停了"
            break
        if job is not None and job.get("notes"):
            hint = job["notes"][-1]
            job["log"].append({"round": r, "kind": "note", "text": hint})
        round_msgs = []
        for i, seat in enumerate(seats):
            others = [t for t in per_round if t["provider"] != seat["provider"]]
            peer_text = "\n\n".join(f"【{t['role']}】{t['text']}" for t in others) or "（对方还没开口，你先立论）"
            user = f"议题：{topic}\n\n对方上一轮：\n{peer_text}"
            if job is not None and job.get("notes"):
                user += "\n\n出题人刚插了一句，必须回应：\n" + job["notes"][-1]
            if r > 1 and history_text:
                user += f"\n\n前情提要（只作参考，不要复述）：\n{history_text[-600:]}"
            _role = (seat.get("role") or "").strip()
            _sys = ROLE_PROMPT.format(role=_role) if _role else NEUTRAL_PROMPT
            msgs = [{"role": "system", "content": _sys},
                    {"role": "user", "content": user}]
            res = pv.call(seat["provider"], msgs, max_tokens=max_tokens, timeout=120,
                          model=seat.get("model") or "")
            cost_calls += 1
            tokens_in += int((res.get("tokens") or {}).get("in") or 0)
            tokens_out += int((res.get("tokens") or {}).get("out") or 0)
            cost += float(res.get("cost") or 0)
            text = res.get("text") if res.get("ok") else ""
            if not res.get("ok"):
                text = f"（这一轮没答上来：{res.get('error', '未知')}）"
            rec = {"round": r, "provider": seat["provider"], "role": seat.get("role") or "参与者",
                   "text": text, "ms": res.get("ms", 0), "ok": bool(res.get("ok"))}
            transcript.append(rec)
            per_round.append(rec)
            _tk = res.get("tokens") or {}
            _say_line("【第" + str(r) + "轮 · " + str(cfg_label(rec["provider"])) + "】" + (text or "")[:160] +
                      " · " + str((_tk.get("in") or 0) + (_tk.get("out") or 0)) + " tokens · ¥" +
                      str(res.get("cost") or 0))
            if job is not None:
                job["log"].append({"round": r, "role": rec["role"], "who": rec["provider"],
                                   "text": text or "", "kind": "speak"})
            round_msgs.append(rec)

        # —— 熔断 1：全员互相附和
        if round_msgs and all(_agrees(t["text"]) for t in round_msgs):
            stop_reason = "全员附和，提前收"
            break
        # —— 熔断 2：跟上一轮几乎一样
        if r > 1:
            prev = [t["text"] for t in transcript if t["round"] == r - 1]
            same = sum(1 for t in round_msgs if any(_similar(t["text"], p) > 0.86 for p in prev))
            if same >= max(1, len(round_msgs) - 1):
                stop_reason = "这轮和上轮几乎一样，提前收"
                break
        history_text += "\n".join(f"{t['role']}：{t['text'][:120]}" for t in round_msgs) + "\n"

    # —— 裁判：固定三段
    judge_input = "\n\n".join(f"第{t['round']}轮 · {t['role']}（{t['provider']}）：{t['text']}" for t in transcript)
    judge = pv.call(seats[0]["provider"],
                    [{"role": "system", "content": JUDGE_PROMPT},
                     {"role": "user", "content": f"议题：{topic}\n\n全部发言：\n{judge_input}"}],
                    max_tokens=600, timeout=120)
    cost_calls += 1
    verdict = (judge or {}).get("text") or "（记录者没给出结论）"

    result = {"ok": True, "topic": topic, "rounds": rounds, "stop": stop_reason,
              "seats": [{"provider": s["provider"], "role": s.get("role"), "model": s.get("model")} for s in seats],
              "transcript": transcript, "verdict": verdict,
              "calls": cost_calls, "ms": int((time.time() - t_start) * 1000),
              "tokens": {"in": tokens_in, "out": tokens_out}, "cost": round(cost, 4)}
    try:
        os.makedirs(OUT, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(OUT, f"debate-{stamp}.json")
        open(path, "w", encoding="utf-8").write(json.dumps(result, ensure_ascii=False, indent=2))
        result["saved"] = path
    except Exception:
        pass
    return result


# —————————————— 出方案模式：作者写、评审打、作者改，最后成稿 ——————————————

AUTHOR_PROMPT = (
    "你是「{name}」，这场要交出一份能直接用的方案。\n"
    "写作要求：\n"
    "1. 先给结论，再给理由；\n"
    "2. 分点写，每点一句判断 + 一句依据；\n"
    "3. 不许空话，不许「视情况而定」这种废话；\n"
    "4. 需要取舍的地方，明确写出你选了哪边、放弃了什么。"
)

REVIEW_PROMPT = (
    "你是「{name}」，负责挑这份方案的硬伤。\n"
    "只做三件事：\n"
    "1. 指出最致命的两个问题（要引原句）；\n"
    "2. 指出被忽略的前提；\n"
    "3. 给出可执行的替换写法；\n"
    "不要夸奖，不要总结，不要客套。"
)

REVISE_PROMPT = (
    "你是「{name}」。下面是你的方案和评审意见。\n"
    "按要求改：逐条回应，采纳的写进正文，不采纳的写一句为什么。\n"
    "输出改后的完整方案，末尾附一节「本次修改」，列 3 条以内。"
)


def produce(topic: str, seats: list, rounds: int = 2, max_tokens: int = 1200) -> dict:
    """seats[0] 是作者，其余是评审。轮数即「写-评-改」的循环次数。"""
    if pv is None:
        return {"ok": False, "error": "providers 模块没起来"}
    seats = _normalize_seats(seats)
    if len(seats) < 2:
        return {"ok": False, "error": "至少要一个作者加一个评审"}
    missing = [s["provider"] for s in seats if s["provider"] not in pv.usable()]
    if missing:
        return {"ok": False, "error": "这些还没配置好：" + "、".join(missing)}
    rounds = max(1, min(3, int(rounds or 2)))

    author = seats[0]
    reviewers = seats[1:]
    log = []
    draft = ""
    t0 = time.time()
    calls = 0
    tokens_in = tokens_out = 0
    cost = 0.0

    job = getattr(_CTX, "job", None)
    for r in range(1, rounds + 1):
        if job is not None and job.get("stop"):
            break
        if r == 1:
            msgs = [{"role": "system", "content": AUTHOR_PROMPT.format(name=author.get("role") or "作者")},
                    {"role": "user", "content": f"任务：就「{topic}」出一份方案，直接写正文。"}]
        else:
            msgs = [{"role": "system", "content": REVISE_PROMPT.format(name=author.get("role") or "作者")},
                    {"role": "user", "content": f"任务：{topic}\n\n你的上一版：\n{draft}\n\n评审意见：\n{critique}"}]
        if job is not None and job.get("notes"):
            msgs = msgs + [{"role": "user", "content": "出题人插了一句：" + job["notes"][-1]}]
        res = pv.call(author["provider"], msgs, max_tokens=max_tokens, timeout=180,
                      model=author.get("model") or "")
        calls += 1
        tokens_in += int((res.get("tokens") or {}).get("in") or 0)
        tokens_out += int((res.get("tokens") or {}).get("out") or 0)
        cost += float(res.get("cost") or 0)
        draft = res.get("text", "") if res.get("ok") else f"（作者没写完：{res.get('error','')}）"
        log.append({"round": r, "who": author["provider"], "role": author.get("role") or "作者",
                    "kind": "draft", "text": draft})
        _say_line("【第" + str(r) + "轮 · 方案】" + (draft or "")[:160])

        critique = ""
        for rv in reviewers:
            rmsgs = [{"role": "system", "content": REVIEW_PROMPT.format(name=rv.get("role") or "评审")},
                     {"role": "user", "content": f"任务：{topic}\n\n待评方案：\n{draft}"}]
            rr = pv.call(rv["provider"], rmsgs, max_tokens=800, timeout=180, model=rv.get("model") or "")
            calls += 1
            tokens_in += int((rr.get("tokens") or {}).get("in") or 0)
            tokens_out += int((rr.get("tokens") or {}).get("out") or 0)
            cost += float(rr.get("cost") or 0)
            txt = rr.get("text", "") if rr.get("ok") else f"（评审没说话：{rr.get('error','')}）"
            critique += f"\n【{rv.get('role') or '评审'}】\n{txt}\n"
            log.append({"round": r, "who": rv["provider"], "role": rv.get("role") or "评审",
                        "kind": "review", "text": txt})
            _say_line("【第" + str(r) + "轮 · 评审】" + (txt or "")[:160])

    result = {"ok": True, "mode": "produce", "topic": topic, "rounds": rounds,
              "seats": [{"provider": s["provider"], "role": s.get("role"), "model": s.get("model")} for s in seats],
              "final": draft, "log": log, "calls": calls, "ms": int((time.time() - t0) * 1000),
              "tokens": {"in": tokens_in, "out": tokens_out}, "cost": round(cost, 4)}
    try:
        os.makedirs(OUT, exist_ok=True)
        path = os.path.join(OUT, "plan-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
        open(path, "w", encoding="utf-8").write(json.dumps(result, ensure_ascii=False, indent=2))
        result["saved"] = path
    except Exception:
        pass
    return result


# —————————————— 可中断、可插话的运行 ——————————————

def start_job(kind: str, topic: str, seats: list, rounds: int = 2) -> dict:
    """起一个后台线程跑议事/出方案，过程中可以停、可以插一句。"""
    import threading
    import uuid
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "kind": kind, "topic": topic, "seats": seats, "rounds": rounds,
           "stop": False, "notes": [], "log": [], "state": "running", "result": None, "t0": time.time()}
    JOBS[jid] = job

    def worker():
        _CTX.job = job
        try:
            if kind == "produce":
                res = produce(topic, seats, rounds=rounds)
            else:
                res = run(topic, seats, rounds=rounds)
            job["result"] = res
            job["state"] = "done" if res.get("ok") else "failed"
        except Exception as e:  # pragma: no cover
            job["state"] = "failed"
            job["result"] = {"ok": False, "error": str(e)[:200]}
        job["ms"] = int((time.time() - job["t0"]) * 1000)

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "id": jid}


def job_status(jid: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这条"}
    return {"ok": True, "id": jid, "kind": job["kind"], "state": job["state"],
            "stop": job["stop"], "notes": job["notes"], "log": job["log"],
            "ms": job.get("ms", 0), "result": job["result"]}


def job_stop(jid: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这条"}
    job["stop"] = True
    return {"ok": True}


def job_say(jid: str, text: str) -> dict:
    job = JOBS.get(jid)
    if not job:
        return {"ok": False, "error": "没有这条"}
    t = (text or "").strip()
    if t:
        job["notes"].append(t)
    return {"ok": True, "notes": job["notes"]}
