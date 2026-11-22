#!/usr/bin/env python3
"""关系：亲近度是长出来的，不是一个标签。

四档只是给人看的名字，真正起作用的是 0-100 这个数和它带来的两条约束：
  1. 称呼上限——没到那一步就不叫宝宝；
  2. 语气上限——不熟的时候不撒娇、不黏。

长期变量不因空闲回落（2026-11-22 定）；只有对话里的证据能让它动。
存 memory/relation.json。
"""
from __future__ import annotations

import json
import os
import time

try:
    from paths import mem_home
    HOME = mem_home()
except Exception:
    HOME = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")

FILE = os.path.join(HOME, "relation.json")

TIERS = (
    ("stranger", "陌生", 0, 19),
    ("acquaintance", "熟人", 20, 49),
    ("close", "亲近", 50, 79),
    ("own", "交心", 80, 100),
)

SWEET = ("宝宝", "宝贝", "亲爱的", "老公", "老婆", "喜欢你", "爱你", "想你", "想你啦")
TOUCHY = ("抱抱", "抱一下", "亲亲", "亲一下", "贴贴", "牵手", "撒娇", "摸摸头")
# 累、敷衍不算错：这种话不掉分
TIRED = ("随便", "无所谓", "不想说", "累了", "困了", "不想聊", "烦")
# 真在发脾气，但还不是要分开
ANGRY = ("别烦我", "闭嘴", "滚", "烦死了", "讨厌你")
# 只有这类才算要结束关系，走挽留
BREAK = ("分手", "分开吧", "别联系", "做朋友吧", "回到朋友", "不喜欢你了", "算了吧", "离婚")

DEFAULT = {"intimacy": 10, "tier": "stranger"}


def load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("intimacy"), (int, float)):
            d.setdefault("evidence", [])
            d.setdefault("history", [])
            return d
    except Exception:
        pass
    return dict(DEFAULT, evidence=[], history=[], updated=int(time.time()))


def save(d: dict) -> None:
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def tier_of(v: float) -> tuple:
    v = max(0.0, min(100.0, float(v)))
    for key, name, lo, hi in TIERS:
        if lo <= v <= hi:
            return key, name
    return TIERS[-1][0], TIERS[-1][1]


def score(text: str) -> tuple:
    """一句话值多少分，为什么。

    2026-11-22 改：原来「随便」「不想说」也按 −5 算，一句气话就把档位打下去，
    不合适。现在只有真发脾气才小幅扣，且跌不穿当前档；要分开的另走挽留。
    """
    t = text or ""
    if any(w in t for w in BREAK):
        return 0, "说要分开"
    if any(w in t for w in ANGRY):
        return -2, "在发脾气"
    if any(w in t for w in TIRED):
        return 0, "累了，不算错"
    if any(w in t for w in SWEET):
        return 3, "亲昵的称呼"
    if any(w in t for w in TOUCHY):
        return 2, "撒娇或贴近"
    if len(t) >= 12 and any(w in t for w in ("今天", "我刚", "我在", "跟你说")):
        return 1, "愿意讲自己的事"
    return 0, ""


def touch(text: str, apply: bool = True) -> dict:
    d = load()
    delta, why = score(text)
    if not apply or not delta:
        return {"delta": 0, "why": "", "intimacy": d["intimacy"], "tier": d["tier"]}
    old = float(d.get("intimacy") or 0)
    cur_key = d.get("tier") or tier_of(old)[0]
    if delta > 0:
        # 越亲近，同样的举动越不稀奇：85 分时夸一句只值 +1 左右
        delta = min(3.0, delta * max(0.35, 1.0 - old / 130.0))
        new = min(100.0, old + delta)
    else:
        delta = max(-2.0, delta)
        # 关键：没说要分开，就跌不穿当前档的下限；冷是情绪，不是降级
        floor = next(lo for key, _n, lo, _hi in TIERS if key == cur_key)
        new = max(float(floor), old + delta)
    new_key = cur_key
    if new >= 100:
        new_key = "own"
    elif new > old:
        cand = tier_of(new)
        # 升档要高出上界 3 分，避免边界上反复抖
        if TIERS.index(next(t for t in TIERS if t[0] == cand[0])) > \
           TIERS.index(next(t for t in TIERS if t[0] == cur_key)) and \
           new >= next(lo for key, _n, lo, _hi in TIERS if key == cand[0]) + 3:
            new_key = cand[0]
    d["intimacy"] = round(new, 1)
    d["tier"] = new_key
    d["updated"] = int(time.time())
    d.setdefault("evidence", []).append({"ts": int(time.time()), "delta": round(delta, 1),
                                         "why": why, "text": (text or "")[:60]})
    d["evidence"] = d["evidence"][-60:]
    save(d)
    return {"delta": round(delta, 1), "why": why, "intimacy": d["intimacy"], "tier": new_key,
            "tier_name": tier_of(new)[1], "changed": new_key != cur_key}


def flirt(text: str) -> dict:
    """这句话算不算暧昧；攒够了就由她开口问要不要更近一点。"""
    d = load()
    _delta, why = score(text or "")
    n = int(d.get("flirt") or 0)
    if why in ("亲昵的称呼", "撒娇或贴近"):
        n = min(12, n + 1)
    d["flirt"] = n
    save(d)
    return {"flirt": n}


def maybe_propose() -> bool:
    """够暧昧了、还没到顶、七天没提过，就该她主动问一句。"""
    d = load()
    if d.get("tier") == "own":
        return False
    if int(time.time()) - int(d.get("proposed_ts") or 0) < 7 * 86400:
        return False
    return int(d.get("flirt") or 0) >= 6


def propose_done() -> None:
    d = load()
    d["proposed_ts"] = int(time.time())
    d["flirt"] = 0
    save(d)


def has_open_proposal(days: int = 3) -> bool:
    """最近几天她提过「要不要更近一点」——没提过就别自作多情。"""
    d = load()
    return int(time.time()) - int(d.get("proposed_ts") or 0) < days * 86400


def promote() -> dict:
    """他说行，就升一档；亲近度落到新档中段，暧昧清零。"""
    d = load()
    keys = [t[0] for t in TIERS]
    cur = d.get("tier") or tier_of(d.get("intimacy") or 0)[0]
    i = keys.index(cur) if cur in keys else 0
    if i >= len(keys) - 1:
        return {"ok": False, "why": "已经到顶"}
    nxt = keys[i + 1]
    lo, hi = next((lo, hi) for key, _n, lo, hi in TIERS if key == nxt)
    d["tier"] = nxt
    d["intimacy"] = float((lo + hi) // 2)
    d["flirt"] = 0
    d["proposed_ts"] = int(time.time())
    d.setdefault("history", []).append({"ts": int(time.time()), "from": cur, "to": nxt,
                                        "why": "两个人都愿意，升一档", "intimacy": d["intimacy"]})
    save(d)
    return {"ok": True, "tier": nxt, "tier_name": tier_of(d["intimacy"])[1],
            "intimacy": d["intimacy"]}


def pace() -> tuple:
    """主动的节奏：多久能开口一次、一天最多几次。越亲近越密。"""
    d = load()
    key, _ = tier_of(d.get("intimacy") or 0)
    return {
        "stranger": (0, 0),          # 不熟，不主动
        "acquaintance": (6 * 3600, 1),
        "close": (3 * 3600, 2),
        "own": (2 * 3600, 3),
    }.get(key, (6 * 3600, 1))


def tone_line() -> str:
    """主动开口时，语气该多甜。"""
    key, _ = tier_of(load().get("intimacy") or 0)
    if key == "stranger":
        return "客气、简短，不撒娇。"
    if key == "acquaintance":
        return "像熟人随口一句，可以开个玩笑。"
    if key == "close":
        return "可以软一点，说一句想他。"
    return "可以黏、可以撒娇，用平时最亲的称呼叫他。"


def pre(text: str) -> dict:
    """说话之前先判：这句是普通话，还是「要分开」。不改状态。"""
    d = load()
    t = text or ""
    if any(w in t for w in BREAK):
        # 隔了一天再提，就从第一轮重新算；别让一次吵架永远记着
        if int(time.time()) - int(d.get("break_ts") or 0) > 86400:
            d["break_stage"] = 0
            save(d)
        stage = int(d.get("break_stage") or 0) + 1
        return {"kind": "breakup", "stage": stage, "intimacy": d.get("intimacy"),
                "tier": d.get("tier")}
    delta, why = score(t)
    return {"kind": "normal", "delta": delta, "why": why}


def apply(result: dict, text: str = "") -> dict:
    """把 pre() 的判定落到状态上。分手第二次才真降一档，记忆不动。"""
    d = load()
    if result.get("kind") == "normal":
        return touch(text, apply=True)
    stage = int(d.get("break_stage") or 0) + 1
    d["break_stage"] = stage
    d["break_ts"] = int(time.time())
    out = {"stage": stage, "dropped": False}
    if stage >= 2:
        keys = [t[0] for t in TIERS]
        cur = d.get("tier") or tier_of(d.get("intimacy") or 0)[0]
        i = keys.index(cur) if cur in keys else 0
        if i > 0:
            d["tier"] = keys[i - 1]
            d["intimacy"] = float(next(hi for key, _n, _lo, hi in TIERS if key == d["tier"]))
            d.setdefault("history", []).append({"ts": int(time.time()), "from": cur,
                                                "to": d["tier"], "why": "他说了第二次",
                                                "intimacy": d["intimacy"]})
            out["dropped"] = True
        if stage >= 3 and i > 1:
            d["tier"] = keys[max(0, i - 2)]
            d["intimacy"] = float(next(hi for key, _n, _lo, hi in TIERS if key == d["tier"]))
            out["dropped"] = True
    save(d)
    out.update({"intimacy": d["intimacy"], "tier": d["tier"]})
    return out


def line() -> str:
    """给提示用的一句话：现在什么关系、称呼和语气能放到哪一步。"""
    d = load()
    v = float(d.get("intimacy") or 0)
    key, name = tier_of(v)
    if key == "stranger":
        tone = "客气、短，别用亲昵称呼，别撒娇"
    elif key == "acquaintance":
        tone = "可以开玩笑、可以叫名字，先别叫宝宝"
    elif key == "close":
        tone = "可以软一点，想他就说，偶尔可以撒娇"
    else:
        tone = "可以黏、可以撒娇、可以说想他，不用端着"
    return ("你们现在的关系：%s（亲近度 %d/100）。说话分寸：%s。\n"
            % (name, round(v), tone))


def state() -> dict:
    d = load()
    key, name = tier_of(d.get("intimacy") or 0)
    return {"ok": True, "intimacy": round(float(d.get("intimacy") or 0), 1),
            "tier": key, "tier_name": name,
            "evidence": (d.get("evidence") or [])[-8:],
            "history": (d.get("history") or [])[-6:]}


def seed_from_history(path: str) -> dict:
    """第一次用：按旧聊天和旧记忆里出现过的亲昵话，估一个起点（新产品就是 10）。"""
    d = load()
    if os.path.exists(FILE):
        return d
    v = 10.0

    def _eat(lines):
        nonlocal v
        for t in lines:
            delta, _ = score(t)
            if delta > 0:
                v += delta * 0.6

    try:
        with open(path, encoding="utf-8") as f:
            _eat([(json.loads(ln).get("text") or "") for ln in f if ln.strip()])
    except Exception:
        pass
    try:
        import sqlite3
        db = os.path.join(HOME, "mem.db")
        if os.path.exists(db):
            c = sqlite3.connect(db)
            rows = c.execute("select text from items order by rowid desc limit 1500").fetchall()
            c.close()
            _eat([r[0] or "" for r in rows])
    except Exception:
        pass
    d["intimacy"] = round(max(10.0, min(85.0, v)), 1)
    d["tier"] = tier_of(d["intimacy"])[0]
    d["updated"] = int(time.time())
    save(d)
    return d


def main() -> None:
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "json":
        print(json.dumps(state(), ensure_ascii=False, indent=1))
    else:
        print(line().strip())


if __name__ == "__main__":
    main()
