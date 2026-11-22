#!/usr/bin/env python3
"""状态 = 从激素算出来的派生量。**状态不是激素，激素不是状态。**

激素（生理物质，8 种，0~1，自己回落到基线）：
    多巴胺 dopamine · 血清素 serotonin · 催产素 oxytocin · 内啡肽 endorphin
    皮质醇 cortisol · 去甲肾上腺素 noradrenaline · GABA gaba · 睾酮 testosterone

状态（给人看的，由激素 + 情绪算出，不独立存储）：
    精力 · 依恋 · 好奇 · 柔和 · 渴望 · 妒意 · 孤独感

  python3 scripts/state.py show
  python3 scripts/state.py json
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from paths import mem_home  # noqa: E402
MEM = mem_home()
AFFECT = os.path.join(MEM, "affect.json")
EMO = os.path.join(MEM, "emotions.json")

# 激素的真名（界面上要显示的就是这八个）
HORMONES = {
    "dopamine": "多巴胺",
    "serotonin": "血清素",
    "oxytocin": "催产素",
    "endorphin": "内啡肽",
    "cortisol": "皮质醇",
    "noradrenaline": "去甲肾上腺素",
    "gaba": "GABA",
    "testosterone": "睾酮",
}

# 状态名 → 由哪些激素算（权重）＋情绪修正
RULES = {
    "精力":   {"dopamine": .35, "noradrenaline": .35, "serotonin": .30, "minus": ["cortisol"]},
    "依恋":     {"oxytocin": 1.0, "minus": []},
    "好奇":   {"dopamine": .6, "noradrenaline": .4, "minus": ["gaba"]},
    "柔和":     {"oxytocin": .5, "serotonin": .3, "gaba": .2, "minus": ["cortisol"]},
    "渴望":     {"testosterone": .7, "dopamine": .3, "minus": []},
    "妒意":     {"testosterone": .5, "cortisol": .5, "minus": []},
    "孤独感":   {"oxytocin": .7, "serotonin": .3, "minus": []},   # 低 oxytocin 才是孤独
}


def _load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def clamp(v):
    return max(0.0, min(1.0, v))


def compute(hormones: dict | None = None, emotions: dict | None = None) -> dict:
    h = hormones or _load(AFFECT, {}).get("hormones", {})
    e = emotions or _load(EMO, {})
    out = {}
    for name, rule in RULES.items():
        val = 0.0
        for k, w in rule.items():
            if k == "minus":
                continue
            val += w * float(h.get(k, 0.5))
        for k in rule.get("minus", []):
            val -= 0.3 * float(h.get(k, 0.5))
        if name == "孤独感":
            val = 1.0 - clamp(val)          # 低催产素 → 高孤独
        # 情绪修正：难过抬孤独感、心疼抬柔和、生气抬妒意
        if name == "孤独感":
            val += 0.3 * float(e.get("loneliness", 0))
        if name == "柔和":
            val += 0.2 * float(e.get("tenderness", 0))
        if name == "妒意":
            val += 0.2 * float(e.get("jealousy", 0))
        out[name] = round(clamp(val), 2)
    return out


def show() -> str:
    h = _load(AFFECT, {}).get("hormones", {})
    st = compute()
    lines = ["激素（物质，8 项）"]
    for k, cn in HORMONES.items():
        v = float(h.get(k, 0))
        lines.append(f"  {cn:<6} {'█' * int(round(v * 10)):<10} {v:.2f}")
    lines.append("")
    lines.append("状态（由激素算出，不是物质）")
    for k, v in st.items():
        lines.append(f"  {k:<4} {'█' * int(round(v * 10)):<10} {v:.2f}")
    return "\n".join(lines)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "json":
        print(json.dumps({"hormones": {HORMONES[k]: round(float(v), 2)
                                       for k, v in _load(AFFECT, {}).get("hormones", {}).items()
                                       if k in HORMONES},
                          "state": compute()}, ensure_ascii=False, indent=1))
        return
    print(show())


if __name__ == "__main__":
    main()
