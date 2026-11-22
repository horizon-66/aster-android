"""天气：城市名 → 经纬度 → 当前与明日预报（open-meteo，免费无 key）。"""
from __future__ import annotations
import json, re, urllib.parse, urllib.request

GEO = "https://geocoding-api.open-meteo.com/v1/search"
FC = "https://api.open-meteo.com/v1/forecast"

CODES = {0: "晴", 1: "少云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
         51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
         71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "强阵雨", 82: "暴雨",
         95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷暴"}


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Aster/1.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def city_of(text: str) -> str:
    t = text or ""
    for w in ("帮我", "麻烦你", "你", "查一下", "查下", "查一查", "查查", "查", "搜一下", "搜",
              "看看", "看一下", "一下",
              "今天", "明天", "后天", "的", "天气", "气温", "怎么样", "如何", "多少度", "会下雨吗"):
        t = t.replace(w, " ")
    for frag in re.split(r"[\s，,。！!？?]+", t):
        frag = frag.strip()
        if 2 <= len(frag) <= 6 and re.fullmatch(r"[\u4e00-\u9fa5]+", frag):
            return frag
    return ""


def lookup(city: str, day: str = "today") -> dict:
    if not city:
        return {"ok": False, "why": "no_city"}
    try:
        u = GEO + "?" + urllib.parse.urlencode({"name": city, "count": 1, "language": "zh"})
        g = _get(u)
        res = (g.get("results") or [])
        if not res:
            return {"ok": False, "why": "city_not_found", "city": city}
        lat, lon = res[0]["latitude"], res[0]["longitude"]
        name = res[0].get("name") or city
        u2 = FC + "?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "timezone": "Asia/Shanghai",
            "current": "temperature_2m,weather_code",
            "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
            "forecast_days": 3})
        d = _get(u2)
    except Exception as e:
        return {"ok": False, "why": str(e)[:80]}
    cur = d.get("current") or {}
    dl = d.get("daily") or {}
    idx = {"today": 0, "day1": 1, "day2": 2}.get(day, 0)
    try:
        hi = (dl.get("temperature_2m_max") or [])[idx]
        lo = (dl.get("temperature_2m_min") or [])[idx]
        code = (dl.get("weather_code") or [])[idx]
        rain = (dl.get("precipitation_probability_max") or [None])[idx]
    except Exception:
        hi = lo = code = rain = None
    return {"ok": True, "city": name,
            "now_temp": cur.get("temperature_2m"), "now": CODES.get(cur.get("weather_code"), ""),
            "day_hi": hi, "day_lo": lo, "day_desc": CODES.get(code, ""),
            "rain_prob": rain, "day": day}


def say(city: str, day: str = "today") -> str:
    r = lookup(city, day)
    if not r.get("ok"):
        if r.get("why") == "city_not_found":
            return f"没查到「{city}」这个地方，换个说法我再试"
        return "天气这会儿查不到，网络或接口有问题"
    import time as _t
    _off = {"today": 0, "day1": 1, "day2": 2}.get(r.get("day") or "today", 0)
    when = "今天" if _off == 0 else _t.strftime("%m月%d日", _t.localtime(_t.time() + 86400 * _off))
    bits = [f"{r['city']}{when}{r.get('day_desc') or ''}"]
    if r.get("day_hi") is not None:
        bits.append(f"{r['day_lo']:.0f}~{r['day_hi']:.0f}°")
    if r.get("now_temp") is not None:
        bits.append(f"现在 {r['now_temp']:.0f}°")
    if r.get("rain_prob"):
        bits.append(f"降水概率 {r['rain_prob']}%")
    return "，".join(bits)
