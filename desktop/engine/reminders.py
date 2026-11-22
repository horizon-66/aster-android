"""提醒（最小版）：到点弹一条。不做冲突检测、不做重复、不接日历。

支持的说法（够用就好）：
  明天下午三点提醒我开会 / 今晚八点叫我 / 三十分钟后提醒我喝水
存储：memory/reminders.json
"""
from __future__ import annotations
import json, os, re, time
from datetime import datetime, timedelta


def _home() -> str:
    h = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")
    os.makedirs(h, exist_ok=True)
    return h


def _path() -> str:
    return os.path.join(_home(), "reminders.json")


def _load() -> list:
    try:
        return json.load(open(_path(), encoding="utf-8")).get("items") or []
    except Exception:
        return []


def _save(items: list) -> None:
    try:
        json.dump({"items": items}, open(_path(), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass


def cn_num(x: str):
    """把「十五 / 二十 / 三十」这类中文数字换成整数。"""
    if not x:
        return None
    if x in CN:
        return CN[x]
    if x == "十":
        return 10
    if "十" in x:
        a, _, b = x.partition("十")
        tens = CN.get(a, 1) if a else 1
        ones = CN.get(b, 0) if b else 0
        return tens * 10 + ones
    return CN.get(x)


CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
      "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _hour(t: str):
    m = re.search(r"(\d{1,2})[:：点](\d{1,2})?", t)
    if m:
        return int(m.group(1)), int(m.group(2) or 0)
    m = re.search(r"([一二两三四五六七八九十]+)点(半)?", t)
    if m:
        h = cn_num(m.group(1))
        if h is not None:
            return h, 30 if m.group(2) else 0
    return None


def parse(text: str) -> dict | None:
    t = (text or "").strip()
    if not re.search(r"(提醒我|叫我|到点)", t):
        return None
    now = datetime.now()
    when = None
    base = None

    m = re.search(r"(\d+)\s*分钟后", t)
    if m:
        base = now + timedelta(minutes=int(m.group(1)))
    if "半小时后" in t:
        base = now + timedelta(minutes=30)
    if re.search(r"半分钟后", t):
        base = now + timedelta(seconds=30)
    m = re.search(r"([一二两三四五六七八九十]+|\d+)\s*分钟(?:之后|以后)", t)
    if m and base is None:
        v = cn_num(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else None)
        if v:
            base = now + timedelta(minutes=v)
    m = re.search(r"([一二两三四五六七八九十]+)\s*分钟后", t)
    if m and base is None:
        v = cn_num(m.group(1))
        if v:
            base = now + timedelta(minutes=v)
    m = re.search(r"(\d+)\s*小时后", t)
    if m:
        base = now + timedelta(hours=int(m.group(1)))
    if base is None:
        day = now
        if "明天" in t:
            day = now + timedelta(days=1)
        elif "后天" in t:
            day = now + timedelta(days=2)
        elif "今晚" in t:
            day = now
        hhmm = _hour(t)
        if hhmm is None:
            return None
        h, mi = hhmm
        if "下午" in t and h < 12:
            h += 12
        if "晚上" in t and h < 12:
            h += 12
        base = day.replace(hour=h, minute=mi, second=0, microsecond=0)
        if base <= now and "今晚" not in t and "明天" not in t:
            base += timedelta(days=1)     # 说的时间已经过了，当作明天
    what = re.sub(r"^.*?(提醒我|叫我|到点)", "", t).strip(" ，。！!")
    return {"at": int(base.timestamp()), "text": (what or t)[:80], "created": int(time.time())}


WEEK = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def _next_repeat(kind: str, hh: int, mm: int, weekday: int | None = None) -> int:
    now = datetime.now()
    base = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if base <= now:
        base = base + timedelta(days=1)
    if kind == "workday":
        while base.weekday() >= 5:
            base = base + timedelta(days=1)
    elif kind == "week" and weekday is not None:
        while base.weekday() != weekday:
            base = base + timedelta(days=1)
    return int(base.timestamp())


CN_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _num(x) -> int:
    x = str(x or "0").strip()
    if x.isdigit():
        return int(x)
    if x in CN_DIGIT:
        return CN_DIGIT[x]
    if "十" in x:
        a, _, b = x.partition("十")
        return (CN_DIGIT.get(a, 1) if a else 1) * 10 + (CN_DIGIT.get(b, 0) if b else 0)
    return CN_DIGIT.get(x, 0)


def add(text: str) -> dict:
    t = text or ""
    m = re.search(r"(每天|每个工作日|工作日|每周[一二三四五六日天])\s*(上午|下午|晚上|早上)?\s*"
                  r"([0-9]{1,2}|[一二三四五六七八九十]{1,3})(?:[:：点]([0-9]{1,2}|[一二三四五六七八九十]{1,3})?)?", t)
    if m:
        kind = "daily" if m.group(1) == "每天" else ("workday" if "工作日" in m.group(1) else "week")
        hh = _num(m.group(3)); mm = _num(m.group(4) or 0)
        if "半" in t[m.end():m.end() + 3] or "半" in (m.group(0) or ""):
            mm = 30
        if m.group(2) in ("下午", "晚上") and hh < 12:
            hh += 12
        wd = WEEK.get(m.group(1)[-1]) if kind == "week" else None
        at = _next_repeat(kind, hh, mm, wd)
        it = {"text": t, "at": at, "repeat": {"kind": kind, "hh": hh, "mm": mm, "wd": wd}}
        items = _load(); items.append(it); _save(items)
        return {"ok": True, **it, "readable": datetime.fromtimestamp(at).strftime("%m-%d %H:%M")}
    r = parse(text)
    if not r:
        return {"ok": False, "error": "没听懂时间"}
    items = _load()
    items.append(r)
    _save(items)
    return {"ok": True, **r, "readable": datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")}


def due(now: float | None = None) -> list:
    now = now or time.time()
    items = _load()
    hit = [x for x in items if x["at"] <= now]
    if hit:
        keep = [x for x in items if x["at"] > now]
        for x in hit:
            rep = x.get("repeat")
            if rep:
                nx = dict(x)
                nx["at"] = _next_repeat(rep.get("kind"), rep.get("hh"), rep.get("mm"), rep.get("wd"))
                keep.append(nx)     # 周期提醒：响过就排下一次
        _save(keep)
    return hit


def all_items() -> list:
    return _load()
