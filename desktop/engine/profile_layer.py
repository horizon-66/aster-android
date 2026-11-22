"""成长层：把零散记忆压成「画像」，以后每轮先带画像，再带零散条目。

每攒够 N 条新记忆，就让云端把这些记忆总结成几段话：
  他在意什么 / 他讨厌什么 / 他的习惯与作息 / 你和他之间的关系
画像替换旧的（不是越堆越长），保持在 600 字以内。
"""
from __future__ import annotations
import json, os, time

N = 20          # 攒够多少条新记忆提炼一次


def _home() -> str:
    h = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")
    os.makedirs(h, exist_ok=True)
    return h


def _path() -> str:
    return os.path.join(_home(), "profile.json")


def load() -> dict:
    try:
        return json.load(open(_path(), encoding="utf-8"))
    except Exception:
        return {"text": "", "built_at": 0, "seen": 0}


def save(d: dict) -> None:
    try:
        json.dump(d, open(_path(), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def need(count: int) -> bool:
    d = load()
    return count - int(d.get("seen") or 0) >= N


def build(memories: list, names: dict) -> dict:
    """调云端提炼画像。失败就保留旧的。"""
    import brain_call
    if not memories:
        return {"ok": False, "why": "no_memory"}
    lines = "\n".join("· " + str(m.get("text") or "")[:100] for m in memories[-80:])
    prompt = (
        "下面是" + (names.get("user") or "他") + "说过的事，请提炼成一段「他是谁」的画像，供你自己记住用。\n"
        "要求：分四小块——他在意什么、他讨厌什么、他的习惯与作息、你们之间的关系。\n"
        "只用给到的事实，不编；总长 400 字以内；不要评价、不要建议。\n\n" + lines
    )
    r = brain_call.text(prompt, max_tokens=700, temperature=0.4, force=True)
    if not r.get("ok") or not (r.get("text") or "").strip():
        return {"ok": False, "why": r.get("why") or "empty"}
    d = {"text": r["text"].strip()[:1200], "built_at": int(time.time()),
         "seen": len(memories)}
    save(d)
    return {"ok": True, "text": d["text"]}
