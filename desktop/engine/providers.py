"""多家模型供应商：配置、密钥、连通性。

密钥存 macOS 钥匙串（service=aster-prov-<名字>），配置文件只存 base/model。
默认只有 DeepSeek 有官方地址，其余需要用户自己填 key 才能用。
"""
from __future__ import annotations

import json
import os
import subprocess

HOME = os.environ.get("ASTER_HOME") or os.path.expanduser("~/Library/Application Support/Aster/memory")
CFG = os.path.join(HOME, "providers.json")

DEFAULTS = {
    # —— 国内 ——
    "deepseek": {"label": "DeepSeek", "base": "https://api.deepseek.com/v1", "model": "deepseek-chat",
                 "style": "openai", "group": "国内"},
    "qwen": {"label": "通义千问", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model": "qwen-max", "style": "openai", "group": "国内"},
    "doubao": {"label": "豆包（方舟）", "base": "https://ark.cn-beijing.volces.com/api/v3",
               "model": "", "style": "openai", "group": "国内",
               "hint": "模型那格填接入点 ID（ep-…）或 doubao-pro-32k"},
    "kimi": {"label": "Kimi", "base": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
             "style": "openai", "group": "国内"},
    "zhipu": {"label": "智谱 GLM", "base": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus",
              "style": "openai", "group": "国内"},
    "ernie": {"label": "文心（千帆）", "base": "https://qianfan.baidubce.com/v2", "model": "ernie-4.0-8k",
              "style": "openai", "group": "国内"},
    "hunyuan": {"label": "腾讯混元", "base": "https://api.hunyuan.cloud.tencent.com/v1",
                "model": "hunyuan-pro", "style": "openai", "group": "国内"},
    "spark": {"label": "讯飞星火", "base": "https://spark-api-open.xf-yun.com/v1", "model": "generalv3.5",
              "style": "openai", "group": "国内"},
    "minimax": {"label": "MiniMax", "base": "https://api.minimax.chat/v1", "model": "abab6.5s-chat",
                "style": "openai", "group": "国内"},
    "baichuan": {"label": "百川", "base": "https://api.baichuan-ai.com/v1", "model": "Baichuan4",
                 "style": "openai", "group": "国内"},
    "step": {"label": "阶跃星辰", "base": "https://api.stepfun.com/v1", "model": "step-1-8k",
             "style": "openai", "group": "国内"},
    "sensenova": {"label": "商汤日日新", "base": "https://api.sensenova.cn/compatible-mode/v1",
                  "model": "SenseChat-5", "style": "openai", "group": "国内"},
    "siliconflow": {"label": "硅基流动", "base": "https://api.siliconflow.cn/v1",
                    "model": "deepseek-ai/DeepSeek-V3", "style": "openai", "group": "国内"},
    # —— 海外 ——
    "gpt": {"label": "GPT", "base": "https://api.openai.com/v1", "model": "gpt-4o",
            "style": "openai", "group": "海外"},
    "claude": {"label": "Claude", "base": "https://api.anthropic.com/v1",
               "model": "claude-sonnet-4-20250514", "style": "anthropic", "group": "海外"},
    "gemini": {"label": "Gemini", "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.0-flash", "style": "openai", "group": "海外"},
    "grok": {"label": "Grok", "base": "https://api.x.ai/v1", "model": "grok-2-latest",
             "style": "openai", "group": "海外"},
    "mistral": {"label": "Mistral", "base": "https://api.mistral.ai/v1", "model": "mistral-large-latest",
                "style": "openai", "group": "海外"},
    "groq": {"label": "Groq", "base": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "style": "openai", "group": "海外"},
    "openrouter": {"label": "OpenRouter（聚合）", "base": "https://openrouter.ai/api/v1",
                   "model": "deepseek/deepseek-chat", "style": "openai", "group": "海外",
                   "hint": "模型那格填 OpenRouter 的完整模型名"},
    "custom": {"label": "自定义（OpenAI 兼容）", "base": "", "model": "", "style": "openai",
               "group": "其他", "hint": "地址与模型都自己填"},
    # —— 本地 ——
    "local": {"label": "本机 Ollama", "base": "http://127.0.0.1:11434/v1", "model": "qwen2.5:7b",
              "style": "openai", "needs_key": False, "group": "本地"},
}


# 参考价：人民币 / 百万 token（输入, 输出）。各家会调价，这里只作估算。
PRICES = {
    "deepseek": (1.0, 2.0),
    "doubao": (0.8, 2.0),
    "qwen": (2.4, 9.6),
    "kimi": (2.0, 8.0),
    "zhipu": (5.0, 5.0),
    "ernie": (4.0, 16.0),
    "hunyuan": (4.0, 12.0),
    "spark": (2.0, 8.0),
    "minimax": (1.0, 8.0),
    "baichuan": (8.0, 8.0),
    "step": (5.0, 20.0),
    "sensenova": (3.0, 9.0),
    "siliconflow": (1.0, 4.0),
    "gpt": (18.0, 72.0),
    "claude": (22.0, 110.0),
    "gemini": (3.0, 12.0),
    "grok": (20.0, 80.0),
    "mistral": (8.0, 24.0),
    "groq": (0.5, 1.0),
    "openrouter": (8.0, 30.0),
    "local": (0.0, 0.0),
}


def cost_cny(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = PRICES.get(provider, (5.0, 15.0))
    return round(prompt_tokens / 1e6 * pin + completion_tokens / 1e6 * pout, 4)


def _clean(v: str) -> str:
    """模型名与 key 常被连标点一起复制进来，去掉首尾空白与括号类符号。"""
    return str(v or "").strip().strip("（）()【】[]<>《》,，.。;；:：'\"").strip()


def _load() -> dict:
    try:
        d = json.load(open(CFG, encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    tmp = CFG + ".tmp"
    open(tmp, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2))
    os.replace(tmp, CFG)
    try:
        os.chmod(CFG, 0o600)
    except Exception:
        pass


def keychain_get(name: str) -> str:
    try:
        r = subprocess.run(["security", "find-generic-password", "-s", "aster-prov-" + name, "-w"],
                           capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def keychain_set(name: str, key: str) -> bool:
    try:
        subprocess.run(["security", "add-generic-password", "-U", "-s", "aster-prov-" + name,
                        "-a", "aster", "-w", key], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def keychain_del(name: str) -> None:
    try:
        subprocess.run(["security", "delete-generic-password", "-s", "aster-prov-" + name],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def list_all() -> dict:
    """给界面用：每家一条，带 has_key，绝不回传 key 本体。"""
    saved = _load()
    out = []
    for name, meta in DEFAULTS.items():
        cur = dict(meta)
        cur.update(saved.get(name, {}))
        cur["name"] = name
        cur["has_key"] = bool(keychain_get(name)) if meta.get("needs_key", True) else True
        out.append(cur)
    return {"ok": True, "items": out}


def set_key(name: str, key: str, base: str = "", model: str = "") -> dict:
    if name not in DEFAULTS:
        return {"ok": False, "error": "不认识的供应商"}
    k = (key or "").strip()
    if k == "__keep__":
        k = keychain_get(name)
    if not k:
        return {"ok": False, "error": "key 不能为空"}
    if k != keychain_get(name) and not keychain_set(name, k):
        return {"ok": False, "error": "写不进钥匙串"}
    saved = _load()
    ent = dict(saved.get(name, {}))
    if base:
        ent["base"] = _clean(base)
    if model:
        ent["model"] = _clean(model)
    saved[name] = ent
    _save(saved)
    return {"ok": True}


def clear_key(name: str) -> dict:
    keychain_del(name)
    saved = _load()
    if name in saved:
        saved.pop(name)
        _save(saved)
    return {"ok": True}


def get(name: str) -> dict:
    saved = _load()
    meta = dict(DEFAULTS.get(name, {}))
    meta.update(saved.get(name, {}))
    meta["name"] = name
    meta["key"] = keychain_get(name)
    return meta


def usable() -> list:
    """能真正调用的：本地永远算，其余要有 key。"""
    out = []
    for name, meta in DEFAULTS.items():
        if meta.get("needs_key", True) and not keychain_get(name):
            continue
        out.append(name)
    return out


def call(name: str, messages: list, max_tokens: int = 700, timeout: int = 90,
         model: str = "") -> dict:
    """一次对话调用，返回 {ok, text, ms, model} 或 {ok: False, error}。"""
    import time
    import urllib.error
    import urllib.request

    cfg = get(name)
    if model:
        cfg["model"] = _clean(model)
    if not cfg.get("base"):
        return {"ok": False, "error": "没有地址"}
    if not str(cfg.get("model") or "").strip():
        return {"ok": False, "error": "模型那格还没填（豆包要填接入点 ID，ep- 开头）"}
    if cfg.get("needs_key", True) and not cfg.get("key"):
        return {"ok": False, "error": "没填 key"}
    t0 = time.time()
    _u = {}
    try:
        if cfg.get("style") == "anthropic":
            url = cfg["base"].rstrip("/") + "/messages"
            payload = {"model": cfg["model"], "max_tokens": max_tokens, "messages": messages}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
                "Content-Type": "application/json", "x-api-key": cfg["key"],
                "anthropic-version": "2023-06-01"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read().decode())
            text = "".join(b.get("text", "") for b in d.get("content", []) if isinstance(b, dict))
        else:
            url = cfg["base"].rstrip("/") + "/chat/completions"
            payload = {"model": cfg["model"], "messages": messages, "max_tokens": max_tokens,
                       "temperature": 0.7}
            headers = {"Content-Type": "application/json"}
            if cfg.get("key"):
                headers["Authorization"] = "Bearer " + cfg["key"]
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read().decode())
                text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
                _u = d.get("usage") or {}
            except urllib.error.HTTPError as e:
                # 方舟新版模型只开放 /responses（不是 chat/completions），这里兜一层
                if e.code in (404, 400) and "ark.cn-beijing.volces.com" in url:
                    url2 = cfg["base"].rstrip("/") + "/responses"
                    payload2 = {"model": cfg["model"], "input": [
                        {"role": m.get("role"), "content": [{"type": "input_text", "text": m.get("content", "")}]}
                        for m in messages]}
                    req2 = urllib.request.Request(url2, data=json.dumps(payload2).encode(),
                                                  headers={"Content-Type": "application/json",
                                                           "Authorization": "Bearer " + cfg["key"]})
                    with urllib.request.urlopen(req2, timeout=timeout) as r2:
                        d2 = json.loads(r2.read().decode())
                    parts = []
                    for it in (d2.get("output") or []):
                        for c in (it.get("content") or []):
                            if isinstance(c, dict) and c.get("text"):
                                parts.append(c["text"])
                    text = "".join(parts)
                else:
                    raise
        pin = int(_u.get("prompt_tokens") or 0)
        pout = int(_u.get("completion_tokens") or 0)
        if not pin:
            pin = sum(len(str(m.get("content") or "")) for m in messages) // 2
        if not pout:
            pout = len(text or "") // 2
        return {"ok": True, "text": (text or "").strip(), "ms": int((time.time() - t0) * 1000),
                "model": cfg["model"], "provider": name,
                "tokens": {"in": pin, "out": pout}, "cost": cost_cny(name, pin, pout)}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode()[:160]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code} {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}
