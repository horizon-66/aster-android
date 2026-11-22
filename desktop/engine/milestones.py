"""纪念日与共同经历。

两件事分开记：
  1. 第一次——第一次说话、第一次说想你、第一次吵架、第一次一起出门。只记一次，不覆盖。
  2. 数得出来的——认识多少天、第几个月、第一百天。由第一个「第一次」推出来。

存在 memory/milestones.json；`upcoming()` 给最近要到的日子，聊天时带进提示，
她就能自己提一句「后天是我们第一百天」。
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

FILE = os.path.join(HOME, "milestones.json")

# 触发词：只在「第一次」出现时记一条
FIRSTS = (
    ("first_affection", ("想你", "喜欢你", "爱你", "抱抱", "亲")),
    ("first_fight", ("吵架", "烦死", "滚", "不理你", "讨厌你")),
    ("first_night", ("晚安", "睡了", "睡觉")),
)


def _load() -> dict:
    try:
        with open(FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            d.setdefault("events", {})
            return d
    except Exception:
        pass
    return {"events": {}, "created": int(time.time())}


def _save(d: dict) -> None:
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def mark(key: str, note: str = "") -> bool:
    """记一条「第一次」。已经有了就不动，返回 False。"""
    d = _load()
    if key in d["events"]:
        return False
    d["events"][key] = {"ts": int(time.time()), "note": note}
    _save(d)
    return True


def _earliest() -> int:
    """尽量找回「最早那天」：先看聊天记录，再看记忆库。"""
    best = 0
    try:
        p = os.path.join(HOME, "chat.ndjson")
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    ts = int(json.loads(line).get("ts") or 0)
                except Exception:
                    continue
                if ts and (best == 0 or ts < best):
                    best = ts
    except Exception:
        pass
    try:
        import sqlite3
        db = os.path.join(HOME, "mem.db")
        if os.path.exists(db):
            c = sqlite3.connect(db)
            row = c.execute("select min(created) from items where created > 0").fetchone()
            c.close()
            if row and row[0] and (best == 0 or int(row[0]) < best):
                best = int(row[0])
    except Exception:
        pass
    return best


def ensure() -> dict:
    """第一次跑：把「认识那天」记下来（尽量回溯到最早那条记录）。"""
    d = _load()
    if "first_met" not in d["events"]:
        d["events"]["first_met"] = {"ts": int(_earliest() or d.get("created") or time.time()),
                                    "note": "第一次说话"}
        _save(d)
    return d


def detect(text: str) -> list:
    """从一句话里认「第一次」；返回这次新记下的 key。"""
    t = text or ""
    out = []
    for key, words in FIRSTS:
        if any(w in t for w in words) and mark(key, t[:40]):
            out.append(key)
    return out


def days_together() -> int:
    d = ensure()
    ts = int((d["events"].get("first_met") or {}).get("ts") or time.time())
    return max(0, (int(time.time()) - ts) // 86400) + 1


def upcoming(within_days: int = 7) -> list:
    """最近要到的纪念日：满月、百天、周年。"""
    d = ensure()
    start = int((d["events"].get("first_met") or {}).get("ts") or time.time())
    now = int(time.time())
    out = []
    # 满月：按自然月往后推
    t = time.localtime(start)
    for n in range(1, 25):
        y = t.tm_year + (t.tm_mon - 1 + n) // 12
        m = (t.tm_mon - 1 + n) % 12 + 1
        try:
            at = int(time.mktime((y, m, t.tm_mday, t.tm_hour, t.tm_min, 0, 0, 0, -1)))
        except Exception:
            continue
        if 0 <= at - now <= within_days * 86400:
            out.append({"name": str(n) + " 个月", "at": at, "days": (at - now) // 86400})
    # 百天、周年
    for name, n in (("一百天", 100), ("两百天", 200), ("三百天", 300), ("一周年", 365)):
        at = start + (n - 1) * 86400
        if 0 <= at - now <= within_days * 86400:
            out.append({"name": name, "at": at, "days": (at - now) // 86400})
    out.sort(key=lambda x: x["at"])
    return out


def line() -> str:
    """给提示用的一句。"""
    n = days_together()
    ups = upcoming()
    if ups:
        u = ups[0]
        when = "今天" if u["days"] <= 0 else ("明天" if u["days"] == 1 else str(u["days"]) + " 天后")
        return "你们认识第 %d 天；%s是「%s」，可以顺口提一句。\n" % (n, when, u["name"])
    return "你们认识第 %d 天。\n" % n


def state() -> dict:
    d = ensure()
    ev = d.get("events") or {}
    return {
        "ok": True,
        "days": days_together(),
        "firsts": [{"key": k, "at": v.get("ts"), "note": v.get("note")}
                   for k, v in sorted(ev.items(), key=lambda kv: kv[1].get("ts") or 0)],
        "upcoming": upcoming(30),
    }


if __name__ == "__main__":
    print(json.dumps(state(), ensure_ascii=False, indent=1))
