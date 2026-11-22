"""大脑调用（第 5 部分）：把简报卡片递给 14B 或云端，只收结构化结果。

配置：memory/cloud.json
  {"base_url": "https://api.deepseek.com", "model": "deepseek-chat",
   "api_key": "sk-...", "daily_budget_cny": 10, "enabled": true}

没有配置 / 关掉 / 预算用完 → available() 为 False，上层降级到本地。
测试可用 ASTER_BRAIN_DRYRUN=1 返回一个固定 JSON，不真花钱。
"""
from __future__ import annotations
import json, os, time, urllib.request

LOG = "brain.ndjson"


def _home() -> str:
    h = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")
    os.makedirs(os.path.join(h, "logs"), exist_ok=True)
    return h


def config() -> dict:
    try:
        d = json.load(open(os.path.join(_home(), "cloud.json"), encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("daily_budget_cny", 10)
    d.setdefault("base_url", d.get("base") or "https://api.deepseek.com")
    d.setdefault("model", "deepseek-chat")
    return d


def _keychain_key() -> str:
    """key 存在 macOS 钥匙串（service=aster-cloud），配置文件里只留服务名。"""
    try:
        import subprocess
        r = subprocess.run(["security", "find-generic-password", "-s", "aster-cloud", "-w"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _api_key(c: dict) -> str:
    return c.get("api_key") or _keychain_key()


def prefs() -> dict:
    """界面上的开关：memory/brain.json {"enabled": bool, "daily_budget_cny": n}"""
    try:
        d = json.load(open(os.path.join(_home(), "brain.json"), encoding="utf-8"))
    except Exception:
        d = {}
    d.setdefault("enabled", False)
    d.setdefault("daily_budget_cny", 10)
    d.setdefault("noticed", False)
    return d


def set_prefs(enabled: bool | None = None, budget: float | None = None) -> dict:
    d = prefs()
    if enabled is not None:
        if enabled and not d.get("enabled"):
            d["noticed"] = False        # 每次从关到开，重新提示一次费用
        d["enabled"] = bool(enabled)
    if budget is not None:
        d["daily_budget_cny"] = max(0.0, float(budget))
    try:
        json.dump(d, open(os.path.join(_home(), "brain.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
    except Exception:
        pass
    return d


def enabled() -> bool:
    if os.environ.get("ASTER_BRAIN") == "1":
        return True
    return bool(prefs().get("enabled"))


def available() -> bool:
    if not enabled():
        return False
    if os.environ.get("ASTER_BRAIN_DRYRUN") == "1":
        return True
    c = config()
    if c.get("enabled") is False:
        return False
    return bool(_api_key(c))


def _spent_today() -> float:
    p = os.path.join(_home(), "logs", LOG)
    day = time.strftime("%Y-%m-%d")
    total = 0.0
    try:
        for line in open(p, encoding="utf-8"):
            d = json.loads(line)
            if d.get("day") == day:
                total += float(d.get("cost_cny") or 0)
    except Exception:
        pass
    return total


def _log(rec: dict) -> None:
    try:
        rec.setdefault("day", time.strftime("%Y-%m-%d"))
        with open(os.path.join(_home(), "logs", LOG), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def call(brief: dict, level: int) -> dict:
    """返回 {"ok", "answer", "why", "cost_cny", "latency_s"}。"""
    t0 = time.time()
    if not available():
        return {"ok": False, "why": "no_config"}
    c = config()
    _budget = float(prefs().get("daily_budget_cny") or c.get("daily_budget_cny") or 10)
    if _spent_today() >= _budget:
        return {"ok": False, "why": "budget"}
    if os.environ.get("ASTER_BRAIN_DRYRUN") == "1":
        _fid = (brief.get("allowed_facts") or [{}])[0].get("id")
        _pts = ([{"text": (brief["allowed_facts"][0].get("text") or "")[:40], "ref_id": _fid}]
                if _fid else [])
        ans = {"decision": "先接住他的累，再问一句要不要歇会儿",
               "points": _pts, "risks": [], "emotion_delta": {}}
        _log({"ts": int(time.time()), "level": level, "ok": True, "why": "dryrun",
              "cost_cny": 0, "latency_s": round(time.time() - t0, 2)})
        return {"ok": True, "answer": ans, "why": "dryrun", "cost_cny": 0,
                "latency_s": round(time.time() - t0, 2)}

    sys_prompt = (
        "你是一个只做判断的分析器。只回 JSON，不要解释、不要台词。\n"
        '格式：{"decision": "一句话决定", "output": "需要交付的正文（润色/总结/翻译这类题把成品放这里，不需要就留空）", '
        '"points": [{"text": "事实", "ref_id": "mem_01"}], "risks": [], "emotion_delta": {}}\n'
        "只能引用 allowed_facts 里的 id；没有出处的事实不许写进 points。"
        "如果他要的是成品（改写、总结、清单），output 里直接给成品，不要在 decision 里写「我来润色」。 "
    )
    body = {
        "model": c.get("model"),
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": json.dumps(brief, ensure_ascii=False)}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    _base = c["base_url"].rstrip("/")
    _url = _base + ("/chat/completions" if _base.endswith("/v1") else "/v1/chat/completions")
    req = urllib.request.Request(_url,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _api_key(c)},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=float((brief.get("constraints") or {}).get("timeout_s", 8))) as r:
            d = json.loads(r.read().decode())
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        ans = json.loads(text)
        usage = d.get("usage") or {}
        cost = round((usage.get("prompt_tokens", 0) * 1e-6 * 1 + usage.get("completion_tokens", 0) * 2e-6 * 1), 5)
        _log({"ts": int(time.time()), "level": level, "ok": True, "cost_cny": cost,
              "latency_s": round(time.time() - t0, 2), "tokens": usage})
        return {"ok": True, "answer": ans, "why": "cloud", "cost_cny": cost,
                "latency_s": round(time.time() - t0, 2)}
    except Exception as e:
        _log({"ts": int(time.time()), "level": level, "ok": False, "why": str(e)[:80],
              "latency_s": round(time.time() - t0, 2)})
        return {"ok": False, "why": str(e)[:80]}


def compare(question: str, sources: list) -> dict:
    """把几处检索结果交给大脑：列优劣、说清便宜的差在哪、贵的贵在哪、给一句推荐。"""
    if not available():
        return {"ok": False, "why": "off"}
    c = config()
    if _spent_today() >= float(prefs().get("daily_budget_cny") or 10):
        return {"ok": False, "why": "budget"}
    facts = "\n".join(f"- {x}" for x in (sources or [])[:6])
    prompt = (f"他想买：{question}\n\n我查到的资料：\n{facts}\n\n"
              "用中文回答，尽量短：\n"
              "1) 一句话结论（资料里没有明确价格就直说没有具体报价）\n"
              "2) 便宜的差在哪（一两条）\n3) 贵的贵在哪（一两条）\n"
              "4) 各自适合谁\n5) 一句推荐\n"
              "只依据上面的资料，不要编价格。")
    body = {"model": c.get("model"), "messages": [
        {"role": "system", "content": "你是做购物对比的助手，说话干脆，只用给到的资料。"},
        {"role": "user", "content": prompt}], "temperature": 0.3}
    _base = c["base_url"].rstrip("/")
    _url = _base + ("/chat/completions" if _base.endswith("/v1") else "/v1/chat/completions")
    req = urllib.request.Request(_url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _api_key(c)}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        text = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = d.get("usage") or {}
        cost = round(usage.get("prompt_tokens", 0) * 1e-6 + usage.get("completion_tokens", 0) * 2e-6, 5)
        _log({"ts": int(time.time()), "level": 3, "ok": True, "why": "compare",
              "cost_cny": cost, "tokens": usage})
        return {"ok": True, "text": text.strip(), "cost_cny": cost}
    except Exception as e:
        _log({"ts": int(time.time()), "level": 3, "ok": False, "why": str(e)[:80]})
        return {"ok": False, "why": str(e)[:80]}


# 各家现价（元 / 百万 token：输入、输出）。本地模型不计费。
# 价格会变，用户可以用 providers.json 里的 price_in/price_out 覆盖。
PRICES = {
    "deepseek": (2.0, 8.0),
    "qwen": (2.4, 9.6),
    "doubao": (0.8, 2.0),
    "openai": (1.1, 4.4),
    "gemini": (0.5, 1.5),
    "ollama": (0.0, 0.0),
}
DEFAULT_PRICE = (2.0, 8.0)


def _price(c: dict) -> tuple:
    """这家供应商怎么算钱：先用配置里的覆盖，再查表，最后给个默认。"""
    try:
        pin = c.get("price_in")
        pout = c.get("price_out")
        if pin is not None and pout is not None:
            return float(pin), float(pout)
    except Exception:
        pass
    return PRICES.get(str(c.get("provider") or c.get("name") or "").lower(), DEFAULT_PRICE)


def text(prompt: str, max_tokens: int = 1200, temperature: float = 0.8,
         force: bool = False) -> dict:
    """自由文本生成（写文章、写脚本这类）：只回正文，不要 JSON。

    force=True：这类活本地写不好，用户明确要求「直接就上云」——只要配了 key 就走云端，
    仍然受每日预算约束。
    """
    if force:
        if not _api_key(config()):
            return {"ok": False, "why": "no_key"}
    elif not available():
        return {"ok": False, "why": "off"}
    c = config()
    if _spent_today() >= float(prefs().get("daily_budget_cny") or 10):
        return {"ok": False, "why": "budget"}
    body = {"model": c.get("model"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature, "max_tokens": max_tokens}
    _base = c["base_url"].rstrip("/")
    _url = _base + ("/chat/completions" if _base.endswith("/v1") else "/v1/chat/completions")
    req = urllib.request.Request(_url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer " + _api_key(c)}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        usage = d.get("usage") or {}
        _pin, _pout = _price(c)
        _pt = int(usage.get("prompt_tokens") or 0)
        _ct = int(usage.get("completion_tokens") or 0)
        cost = round(_pt / 1e6 * _pin + _ct / 1e6 * _pout, 6)
        _log({"ts": int(time.time()), "level": 3, "ok": True, "why": "text",
              "provider": c.get("provider") or "", "cost_cny": cost, "tokens": usage})
        return {"ok": bool(out.strip()), "text": out.strip(), "cost_cny": cost}
    except Exception as e:
        _log({"ts": int(time.time()), "level": 3, "ok": False, "why": str(e)[:80]})
        return {"ok": False, "why": str(e)[:80]}
