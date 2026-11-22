"""本地只做判断：意图 + 关键词 + 要不要记。用最小的 prompt，不生成回答。

输出严格 JSON：
  {"intent": "chat|ask|do", "keywords": ["..."], "remember": true|false, "why": "一句话"}
"""
from __future__ import annotations
import json, os, re, urllib.request

OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
def _pick_model() -> str:
    env = os.environ.get("ASTER_ROUTER_MODEL") or os.environ.get("ASTER_MODEL")
    if env:
        return env
    home0 = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")
    try:
        import json as _j2
        m0 = (_j2.load(open(os.path.join(home0, "router.json"), encoding="utf-8")) or {}).get("model")
        if m0:
            return str(m0)
    except Exception:
        pass
    home = os.environ.get("ASTER_HOME") or os.path.expanduser(
        "~/Library/Application Support/Aster/memory")
    try:
        import json as _j
        m = (_j.load(open(os.path.join(home, "model.json"), encoding="utf-8")) or {}).get("model")
        if m:
            return str(m)
    except Exception:
        pass
    return "qwen2.5:7b"


MODEL = _pick_model()
PROMPT = (
    '把这句话抽成 JSON，只输出 JSON，不要解释：\n'
    '{"intent":"chat|ask|do","need":"local_ok|cloud_heavy","needs":["最多3条"],'
    '"user_emotion":"一个词","tone":"一个词","keywords":["最多4个"],"remember":true|false}\n'
    "intent：闲聊 chat；问事实 ask；要我动手 do（写、改、跑、整理、记住、订提醒）。\n"
    "need：要出长文/改代码/跑命令/查资料核对 = cloud_heavy；顺口一问、闲聊 = local_ok。\n"
    "带「吗/呢/？/是不是/有没有/能不能」的问句，intent 一律 ask，别判 do。\n"
    "keywords 去掉「你/帮我/一下」这类虚词。remember 只在新事实（偏好/经历/约定）时为 true。\n"
)


def _post(payload: dict, timeout: int = 20) -> dict:
    req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def parse(text: str) -> dict:
    """纯规则兜底（模型挂了也能用）。"""
    t = text or ""
    intent = "chat"
    if re.search(r"(怎么回事|为什么|什么时候|多少|哪|谁|是什么|能不能查|查一下|搜一下)", t):
        intent = "ask"
    if re.search(r"(帮我|给我|做一个|做个|写一个|写个|整理|列|改|发我)", t):
        intent = "do"
    kws = []
    for w in ("帮我", "麻烦你", "你", "一下", "查一查", "查一下", "查查", "看看", "的", "了", "吗", "呢", "啊"):
        t = t.replace(w, " ")
    for frag in re.split(r"[\s，,。！!？?、]+", t):
        frag = frag.strip()
        if 2 <= len(frag) <= 8:
            kws.append(frag)
    remember = bool(re.search(r"(我(喜欢|讨厌|住|在|叫|有|是|最近|明天|昨天|想)|记住|记一下|以后)", text or ""))
    emo = "平静"
    if re.search(r"(累|困|疲)", t2 := (text or "")):
        emo = "累"
    elif re.search(r"(烦|气|讨厌|滚)", t2):
        emo = "烦"
    elif re.search(r"(难过|难受|委屈|想哭)", t2):
        emo = "难过"
    elif re.search(r"(开心|高兴|哈哈|太好了)", t2):
        emo = "高兴"
    tone = {"累": "心疼", "烦": "克制", "难过": "安慰", "高兴": "轻松"}.get(emo, "轻松")
    return _fix({"via": "rule-parse", "intent": intent, "need": ("cloud_heavy" if intent == "do" or len(text) > 300
                                            else "local_ok"),
            "needs": [], "user_emotion": emo, "tone": tone,
            "keywords": kws[:4], "remember": remember}, text)


def compress(text: str, limit: int = 160) -> str:
    """长输入先压成要点：云端只读这一段，不读原文。

    这是 7B 最值钱的活——把几万字压成几句话。
    """
    t = (text or "").strip()
    if len(t) <= 350:
        return t
    try:
        d = _post({"model": MODEL,
                   "prompt": ("把下面这段话压成 " + str(limit) + " 字以内的要点，只留事实、要求、"
                              "关键数字；不要评价、不要客气话。直接输出要点。\n\n" + t[:6000]),
                   "stream": False,
                   "options": {"temperature": 0.2, "num_predict": 260,
                               "seed": int(os.environ.get("ASTER_SEED", "42"))}}, timeout=40)
        out = (d.get("response") or "").strip()
        return out[:limit * 2] if out else t[:limit]
    except Exception:
        return t[:limit]


# 快路：规则先兜一遍，专治小模型的三个惯犯错误（把「做事」当提问、把「问过去」当闲聊）
DO_HINT = re.compile(r"(帮我|替我|给我(写|做|整理|查)|写一|写个|写一个|整理一下|整理成|跑一下|跑个|改一下|修一下|"
                     r"记住|记一下|提醒|订个|定个|设个|删掉|搬|备份)")
# 本地立刻能办的小事：设提醒、记事、删文件——不需要云端
SIMPLE_DO = re.compile(r"(提醒|闹钟|记住|记一下|记下来|删掉|删除|备份)")
# 必须云端出活的：写、跑、改、整理、统计、转换、生成
# 中文里的礼貌请求：「能不能帮我…」「麻烦帮我…」是派活，不是提问
POLITE_DO = re.compile(r"(能不能|可不可以|可以帮我|麻烦(你)?帮|请你?帮)")
# 「帮我看下日志/代码/报错」= 要分析，得给材料
CODE_LOOK = re.compile(r"(日志|代码|报错|异常|堆栈|接口|脚本)")
LOOKUP_HINT = re.compile(r"(多久|多长时间|多远|哪些|为什么|怎么才能|怎么办|怎么办理|能不能放|"
                         r"有科学依据|调休|续航|攻略|政策|规定|标准是什么|多少钱一|几号|几点开始|"
                         r"缓解|预防|推荐|作用|是多少|多少钱|收费|办法|方法|技巧|怎么|如何)")
RUN_HINT = re.compile(r"(跑一下|跑个|运行一下|运行|执行一下|执行)")
HEAVY_DO = re.compile(r"(写|跑|运行|执行|改|整理|清理|去重|排序|合并|提取|统计|转换|翻译|生成|"
                      r"分析|汇总|大纲|总结|代码|脚本|搭建|部署)")
RECALL_HINT2 = re.compile(r"(记得|上次|之前|说过|那事|那件事|后来|叫什么|进展到哪|还记得)")


QUESTION_TAIL = re.compile(r"(是不是|有没有|能不能|要不要|吗|呢|\?|？)\s*$")
QUESTION_ANY = re.compile(r"(是不是|有没有|能不能|要不要)")
ASK_ONLY = re.compile(r"(查一下|搜一下|查查|搜搜|问问|看一下|看看|告诉我|说说|解释|介绍一下|多少钱|怎么样)")


def _rule_fast(text: str):
    """规则能不能直接定性。拿不准就返回 None，交回模型。

    每条规则有 id，命中时记进 cls["rules"]——评测时能看出哪条从没触发、哪条总在误伤。
    R1 做事词 R2 简单活 R3 重活 R4 要跑代码 R5 查问词 R6 疑问词 R7 问过去 R8 短句形状
    """
    t = (text or "").strip()
    if not t or len(t) > 120:
        return None
    fired = []
    if ASK_ONLY.search(t):              # R5「帮我查一下」是问，不是派活
        fired.append("R5_ask_only")
        do_hit = False
        t = t.replace("帮我", "")
    else:
        do_hit = bool(DO_HINT.search(t))
        if do_hit:
            fired.append("R1_do_hint")
    recall_hit = bool(RECALL_HINT2.search(t))
    if do_hit and recall_hit:          # 正反信号都在，别抢
        return None
    if do_hit:
        if QUESTION_TAIL.search(t) or QUESTION_ANY.search(t):   # R6 疑问语气就不是任务
            return None
        kw = [x for x in re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,6}", t)][:4]
        _need = "local_ok" if (SIMPLE_DO.search(t) and not HEAVY_DO.search(t)) else "cloud_heavy"
        if SIMPLE_DO.search(t) and not HEAVY_DO.search(t):
            fired.append("R2_simple_do")
        if RUN_HINT.search(t):
            fired.append("R4_run_hint")
        elif HEAVY_DO.search(t):
            fired.append("R3_heavy_do")
        return {"intent": "do", "need": _need, "needs": [], "user_emotion": "",
                "tone": "认真", "keywords": kw, "remember": False, "via": "rule",
                "rules": fired or ["R1_do_hint"]}
    if len(t) <= 12 and not QUESTION_ANY.search(t) and not ASK_ONLY.search(t) \
            and not recall_hit and not LOOKUP_HINT.search(t):
        # R8 真实聊天里大量是「到了」「待着的」「真可爱」这种，光看形状就够判
        return {"intent": "chat", "need": "local_ok", "needs": [], "user_emotion": "",
                "tone": "轻松", "keywords": [t[:6]], "remember": False,
                "via": "rule", "rules": ["R8_shape_short"]}
    if "R5_ask_only" in fired and not recall_hit:
        kw = [x for x in re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,6}", t)][:4]
        _r = ["R5_ask_only"] + (["R4_run_hint"] if RUN_HINT.search(t) else []) \
             + (["R10_lookup"] if LOOKUP_HINT.search(t) else [])
        return {"intent": "ask", "need": "cloud_heavy", "needs": [], "user_emotion": "",
                "tone": "认真", "keywords": kw, "remember": False, "via": "rule",
                "rules": _r}
    if QUESTION_ANY.search(t) or QUESTION_TAIL.search(t):
        kw = [x for x in re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,6}", t)][:4]
        _need6 = "cloud_heavy" if (recall_hit or LOOKUP_HINT.search(t) or RUN_HINT.search(t)) else "local_ok"
        _r6 = ["R6_question"] + (["R10_lookup"] if LOOKUP_HINT.search(t) else [])
        return {"intent": "ask", "need": _need6,
                "needs": [], "user_emotion": "", "tone": "轻松", "keywords": kw,
                "remember": False, "via": "rule", "rules": _r6}
    if recall_hit:
        kw = [x for x in re.findall(r"[\u4e00-\u9fa5A-Za-z]{2,6}", t)][:4]
        return {"intent": "ask", "need": "cloud_heavy", "needs": [], "user_emotion": "",
                "tone": "轻松", "keywords": kw, "remember": False, "via": "rule",
                "rules": ["R7_recall"]}
    return None


def _fix(cls: dict, text: str) -> dict:
    t = text or ""
    _fired = list(cls.get("rules") or [])
    _askish = bool(ASK_ONLY.search(t) or QUESTION_ANY.search(t) or QUESTION_TAIL.search(t))
    if _askish and cls.get("intent") == "do":
        cls["intent"] = "ask"          # 小模型爱把问句当任务，这里拨回去
        cls["need"] = "local_ok"
    # R11 礼貌请求是派活（「能不能帮我把…整理一下」不是问句）
    if POLITE_DO.search(t) and HEAVY_DO.search(t):
        cls["intent"] = "do"
        cls["need"] = "cloud_heavy"
        _fired.append("R11_polite_do")
    # R12 要看日志/代码/报错 = 得带材料，别省
    if CODE_LOOK.search(t) and re.search(r"(看|查|分析|判断|确认)", t):
        cls["need"] = "cloud_heavy"
        _fired.append("R12_code_look")

    # R10 要查证才能答的问题：抬成重简报（判轻的代价是答不出来）
    if LOOKUP_HINT.search(t):
        cls["need"] = "cloud_heavy"
        _fired.append("R10_lookup")
    if DO_HINT.search(t) and not _askish and not t.startswith(("怎么", "为什么", "是不是")):
        cls["intent"] = "do"
        cls["need"] = "local_ok" if (SIMPLE_DO.search(t) and not HEAVY_DO.search(t)) else "cloud_heavy"
        if SIMPLE_DO.search(t) and not HEAVY_DO.search(t):
            _fired.append("R2_simple_do")
        elif HEAVY_DO.search(t):
            _fired.append("R3_heavy_do")
    if RECALL_HINT2.search(t) and "cloud_heavy" != cls.get("need"):
        cls["need"] = "cloud_heavy"
    if len(t) > 300:
        cls["need"] = "cloud_heavy"
    if RUN_HINT.search(t):
        cls["need"] = "cloud_heavy"      # R4 要跑东西就别省，省了等于没跑
        _fired.append("R4_run_hint")
    if RECALL_HINT2.search(t):
        _fired.append("R7_recall")
    if _askish:
        _fired.append("R6_question")
    if _fired:
        cls["rules"] = sorted(set(_fired))
    return cls


def classify(text: str) -> dict:
    fast = _rule_fast(text)
    if fast:
        return fast
    """先让本地小模型判一次；失败或超时就退回规则。"""
    if os.environ.get("ASTER_ROUTER", "1") != "1":
        return parse(text)
    try:
        d = _post({"model": MODEL, "prompt": PROMPT + "\n他说：" + (text or "")[:200],
                   "stream": False, "format": "json",
                   "options": {"temperature": 0.1, "num_predict": 120,
                               "seed": int(os.environ.get("ASTER_SEED", "42"))}})
        raw = (d.get("response") or "").strip()
        r = json.loads(raw)
        it = r.get("intent")
        if it not in ("chat", "ask", "do"):
            it = parse(text)["intent"]
        kws = [str(x)[:12] for x in (r.get("keywords") or []) if str(x).strip()][:4]
        nd = str(r.get("need") or "").strip()
        if nd not in ("local_ok", "cloud_heavy"):
            nd = "cloud_heavy" if (it == "do" or len(text) > 300) else "local_ok"
        return _fix({"via": "model", "intent": it, "need": nd,
                "needs": [str(x)[:40] for x in (r.get("needs") or [])][:3],
                "user_emotion": str(r.get("user_emotion") or "")[:8],
                "tone": str(r.get("tone") or "")[:8],
                "keywords": kws or parse(text)["keywords"],
                "remember": bool(r.get("remember")) or parse(text)["remember"]}, text)
    except Exception:
        return _fix(parse(text), text)
