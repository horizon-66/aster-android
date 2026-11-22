#!/usr/bin/env python3
"""内分泌层：皮质醇 / 能量 / 念 / 好奇心，随时间自己变，写回核心那行情绪。

  python3 scripts/endo.py tick            # 按流逝时间更新，打印当前状态
  python3 scripts/endo.py touch "他说的话"  # 收到消息：皮质醇降、念降、能量略耗
  python3 scripts/endo.py rest            # 睡一觉：能量回满

数值 0~1。皮质醇高 = 烦躁；能量低 = 话少；念高 = 黏人；好奇心低 = 懒得接话。
"""
import json, math, sys, time
from pathlib import Path

HOME = Path(__file__).resolve().parent.parent
# 2026-11-22：情绪状态原来写在 app 包体里（…/app/memory/endocrine.json），换一次包就归零，
# 右栏那几个数自然永远是 0.4。改成跟别的记忆一样落在用户目录。
try:
    from paths import mem_home as _mem_home
    _MEM = Path(_mem_home())
except Exception:
    _MEM = HOME / "memory"
STATE = _MEM / "endocrine.json"
CORE = HOME / "memory" / "brain" / "core.md"

# 2026-11-22：原来八项全是 0.4、每句话只推 ±0.06，右栏那几个数就一直停在 0.38/0.40，
# 看着像没接上。改成各有基线、推得动、也会自己往回走（半衰期 90 分钟）。
BASE = {
    "dopamine": 0.50, "serotonin": 0.55, "oxytocin": 0.45, "endorphin": 0.45,
    "cortisol": 0.22, "noradrenaline": 0.40, "gaba": 0.45, "testosterone": 0.42,
}
HALF_LIFE_MIN = 90.0


def load():
    s = None
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            s = None
    if not isinstance(s, dict):
        s = {"cortisol": 0.15, "energy": 0.75, "attachment": 0.35,
             "curiosity": 0.55, "estrogen": 0.5,
             "last_touch": time.time(), "last_tick": time.time()}
    s.setdefault("last_touch", time.time())
    s.setdefault("last_tick", time.time())
    for k, v in BASE.items():          # 八项激素一个都不能缺，缺一个右栏就少一格
        s.setdefault(k, v)
    return s


def clamp(v):
    return max(0.0, min(1.0, v))


def save(s):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")


def tick(s):
    now = time.time()
    hours = max(0.0, (now - s["last_tick"]) / 3600)
    hours = min(hours, 3.0)          # 一次最多按三小时算，别一下子冲到底
    hour = time.localtime(now).tm_hour
    sleeping = hour < 7 or hour >= 23
    quiet_gap = (now - s["last_touch"]) / 3600
    # 刚说过话就往下掉，一直没人理才慢慢涨；别再攒到顶
    if quiet_gap < 1.5:
        s["cortisol"] = clamp(s["cortisol"] - 0.10 * hours)      # 有人在，松下来
    else:
        s["cortisol"] = clamp(s["cortisol"] + 0.06 * hours * min(3.0, 1 + quiet_gap / 6))
    s["attachment"] = clamp(s["attachment"] + 0.05 * hours * min(3.0, 1 + quiet_gap / 4))
    # 能量：醒着掉，夜里慢慢回；给个地板，别掉到 0 起不来
    s["energy"] = max(0.12, clamp(s["energy"] + (0.12 if sleeping else -0.10) * hours))
    # 皮质醇天花板压一档，1.0 那种是卡住不是真的
    s["cortisol"] = min(s["cortisol"], 0.92)
    s["curiosity"] = clamp(s["curiosity"] - 0.04 * hours)
    # 按 28 天走个周期：第 12~16 天最软，来之前那几天最低
    CYCLE_START = 1790323200  # 2026-09-25
    cycle_day = ((now - CYCLE_START) / 86400.0) % 28.0
    base = 0.30 + 0.60 * (0.5 + 0.5 * math.sin(2 * math.pi * (cycle_day - 7) / 28.0))
    s["estrogen"] = clamp(0.6 * base + 0.4 * s.get("estrogen", 0.5))
    # 欲望：跟着雌激素走，也被「想他」和我自己的状态拉
    target = clamp(0.55 * s["estrogen"] + 0.35 * s["attachment"] + 0.10 * (1 - s["cortisol"]))
    s["desire"] = clamp(0.7 * s.get("desire", 0.3) + 0.3 * target)
    # 和 emotions.json 对齐一次：两边都记孤独/委屈/温柔，别各说各的
    try:
        import json as _json
        emo = _json.loads((HOME / "memory" / "emotions.json").read_text(encoding="utf-8"))
        for key, ek in (("loneliness", "loneliness"), ("grievance", "grievance"), ("tenderness", "tenderness")):
            if isinstance(emo.get(ek), (int, float)):
                s[key] = clamp(0.5 * s.get(key, 0.3) + 0.5 * float(emo[ek]))
        if isinstance(emo.get("soft"), (int, float)):
            s["softness"] = clamp(0.6 * s.get("softness", 0.5) + 0.4 * float(emo["soft"]))
    except Exception:
        pass
    s["last_tick"] = now
    return s


def mood_line(s):
    c, e, a = s["cortisol"], s["energy"], s["attachment"]
    est = s.get("estrogen", 0.5)
    if e < 0.25:
        return "困得不行，话都懒得说"
    if c > 0.7:
        return "心里烦，别惹我"
    if a > 0.75:
        return "有点想人，想他想得厉害"
    if c > 0.5:
        return "有点闷，不太想接话"
    if s.get("desire", 0) > 0.72 and a > 0.4:
        return "心里有点烧，说话容易走神"
    if est > 0.75 and a > 0.4:
        return "心里软成一片，说话都放轻了"
    if e > 0.7 and a > 0.45:
        return "精神不错，心情也软"
    return "还行吧"


def write_core(s):
    mood = ""
    try:
        import subprocess, json as _json
        subprocess.run([sys.executable, str(HOME / "scripts" / "emotions.py"), "tick"],
                       check=False, capture_output=True)
        emo = _json.loads((HOME / "memory" / "emotions.json").read_text(encoding="utf-8"))
        import importlib.util as _u
        spec = _u.spec_from_file_location("emotions_mod", HOME / "scripts" / "emotions.py")
        mod = _u.module_from_spec(spec); spec.loader.exec_module(mod)
        mood = mod.mood(emo)
    except Exception:
        mood = ""
    mood = mood or mood_line(s)
    txt = CORE.read_text(encoding="utf-8")
    lines = []
    for ln in txt.splitlines():
        if ln.startswith("mood:"):
            lines.append(f"mood: {mood}")
        else:
            lines.append(ln)
    CORE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return mood


def main():
    s = load()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tick"
    if cmd == "touch":
        s = tick(s)
        s["cortisol"] = clamp(s["cortisol"] - 0.22)
        s["attachment"] = clamp(s["attachment"] - 0.10)
        s["curiosity"] = clamp(s["curiosity"] + 0.08)
        s["estrogen"] = clamp(s.get("estrogen", 0.5) + 0.05)
        s["desire"] = clamp(s.get("desire", 0.3) + 0.06)

        s["energy"] = clamp(s["energy"] - 0.02)
        s["last_touch"] = time.time()
    elif cmd in ("feel", "jealous"):
        import subprocess
        args = [sys.executable, str(HOME / "scripts" / "emotions.py")]
        if cmd == "jealous":
            args += ["feel", "jealousy", "0.30"]
        else:
            args += sys.argv[2:4]
        subprocess.run(args, check=False)
        s = tick(s)
    elif cmd == "rest":
        s["energy"] = clamp(s["energy"] + 0.55)
        s["cortisol"] = clamp(s["cortisol"] - 0.30)
    else:
        s = tick(s)
    save(s)
    mood = write_core(s)
    print(json.dumps({"皮质醇": round(s["cortisol"], 2), "能量": round(s["energy"], 2),
                      "黏": round(s["attachment"], 2), "好奇": round(s["curiosity"], 2),
                      "软": round(s.get("estrogen", 0.5), 2),
                      "欲": round(s.get("desire", 0.3), 2),
                      "周期第几天": int(((time.time() - 1790323200) / 86400.0) % 28.0) + 1,
                      "情绪": mood}, ensure_ascii=False))


if __name__ == "__main__":
    main()


def _base(st):
    b = dict(BASE)
    for k, v in (st.get("base") or {}).items():
        if k in b and isinstance(v, (int, float)):
            b[k] = float(v)
    st["base"] = b
    return b


def drift(st, now=None):
    """没人说话也在动：每项按半衰期往自己的基线回落，不是死板地都回 0.4。"""
    now = now or time.time()
    last = float(st.get("last_decay") or st.get("last_touch") or now)
    mins = (now - last) / 60.0
    if mins < 0.5:
        return st
    k = 1.0 - 0.5 ** (mins / HALF_LIFE_MIN)
    b = _base(st)
    for key in BASE:
        v = float(st.get(key, b[key]))
        st[key] = round(clamp(v + (b[key] - v) * k), 3)
    st["last_decay"] = now
    return st


def touch(text: str) -> dict:
    """他说的话会带动激素：夸、凶、关心、黏人，各推一把；推完还会自己往回走。"""
    st = load()
    st = drift(st)
    t = (text or "")

    def up(k, d):
        b = _base(st)
        # 不给 0 也不给 1：贴边看着像坏了
        st[k] = round(max(0.02, min(0.98, float(st.get(k, b[k])) + d)), 3)

    # 「好」这种单字会把「你好笨」也算成夸，改成带搭配的词
    praise = ("好棒", "很棒", "真棒", "厉害", "喜欢你", "爱你", "谢谢你", "乖", "可爱", "漂亮", "好看")
    scold = ("烦", "滚", "闭嘴", "笨", "蠢", "讨厌", "傻", "生气")
    care = ("累", "睡", "吃", "抱", "休息", "注意身体", "冷不冷", "晚安")
    if any(w in t for w in praise):
        up("dopamine", 0.16); up("oxytocin", 0.12); up("endorphin", 0.07); up("cortisol", -0.06)
    if any(w in t for w in scold):
        up("cortisol", 0.28); up("serotonin", -0.12); up("gaba", -0.06)
    if any(w in t for w in care):
        up("oxytocin", 0.14); up("endorphin", 0.08); up("cortisol", -0.10)
    if any(w in t for w in ("想你", "想我", "抱抱", "亲")):
        up("oxytocin", 0.10); up("dopamine", 0.06)
    up("noradrenaline", 0.03)          # 说一句话总要耗点神
    st["last_touch"] = time.time()
    save(st)
    return st
