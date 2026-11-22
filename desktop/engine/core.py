"""Aster 新内核：本地只做判断，云端负责说话与规划，本地负责执行。

一个文件说清整条链路：
  用户说话
    → 本地 7B 判意图 / 抽关键词 / 判要不要记  （router_local，便宜）
    → 本地按关键词捞记忆（memory.json，最多 5 条）
    → 云端出「话」或「计划」：{"plan":[{"tool":"weather","args":{...}}]}
    → 本地按计划真的去执行（weather / search / 计算）
    → 结果回云端，云端说成一句人话
    → 标了要记的，写回记忆

没有规则表、没有补丁栈。想加能力就加一个 tool。
"""
from __future__ import annotations
import json, os, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import brain_call
import router_local
import weather
import profile_layer
import os as _os0, sys as _sys0
_sys0.path.insert(0, _os0.path.dirname(_os0.path.abspath(__file__)))

try:
    import reminders as _rm
except Exception:
    _rm = None
try:
    import extensions as _ext
except Exception:
    _ext = None

# 情绪：不重写，直接用原来的内分泌/情绪模块
try:
    import endo as _endo
except Exception:
    _endo = None
try:
    import relation as _relation
except Exception:
    _relation = None
try:
    import milestones as _ms
except Exception:
    _ms = None

HOME = os.environ.get("ASTER_HOME") or os.path.expanduser(
    "~/Library/Application Support/Aster/memory")
os.makedirs(HOME, exist_ok=True)
MEM_FILE = os.path.join(HOME, "memories_new.json")
PLAN_FILE = os.path.join(HOME, "plans.json")
HISTORY = []          # 最近对话（内存，进程级）
MAX_HIST = 12
SUM_AT = int(os.environ.get("ASTER_SUM_AT", "24"))     # 超过这么多条就压一次
SUM_FILE = os.path.join(HOME, "chat_summary.md")


def summary_text() -> str:
    try:
        return open(SUM_FILE, encoding="utf-8").read()[:1500]
    except Exception:
        return ""


def _summarize(msgs: list, old: str) -> str:
    """把旧对话压成摘要，跟上一份摘要合并，控制在六百字内。"""
    lines = "\n".join(("他：" if m["role"] == "user" else "你：") + m["content"][:100] for m in msgs)
    p = ("把下面这段聊天压成三到五行的要点，只保留：说过的事实、他的情绪与偏好、还没做完的事。"
         "不要评价、不要心理分析。\n"
         + (("已有的摘要（合并进来，别丢关键事实）：\n" + old + "\n") if old else "")
         + "\n这段是新聊的：\n" + lines[:4000])
    r = brain_call.text(p, max_tokens=400, temperature=0.3, force=True)
    new = (r.get("text") or "").strip() if r.get("ok") else ""
    return (new or old)[:900]


def maybe_compact() -> None:
    if len(HISTORY) <= SUM_AT:
        return
    old = summary_text()
    cut = HISTORY[:-6]                 # 留下最近六句不压
    if not cut:
        return
    new = _summarize(cut, old)
    if new:
        try:
            open(SUM_FILE, "w", encoding="utf-8").write(new)
        except Exception:
            pass
        del HISTORY[:len(cut)]


# ---------- 记忆 ----------
def _load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def _save(path, data):
    try:
        json.dump(data, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass


def _lan_ip() -> str:
    import socket as _sk
    try:
        s2 = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
        s2.connect(("8.8.8.8", 80))
        ip = s2.getsockname()[0]
        s2.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _sync_mood(item: dict) -> None:
    """手机那边一天的情绪，回到家用局域网并回来：只做温和的合并，不覆盖。"""
    try:
        import endo as _en
        st = _en.load() or {}
        m = item.get("mood") or item.get("text") or ""
        lvl = float(item.get("score") or 0.5)
        if "累" in str(m) or "困" in str(m):
            st["energy"] = max(0.0, min(1.0, float(st.get("energy") or 0.5) * 0.8))
        if "烦" in str(m) or "气" in str(m):
            st["cortisol"] = min(1.0, float(st.get("cortisol") or 0.2) + 0.1)
        if "开心" in str(m) or "高兴" in str(m):
            st["energy"] = min(1.0, float(st.get("energy") or 0.5) + 0.08)
        st["last_touch"] = time.time()
        _en.save(st)
        _save(os.path.join(HOME, "phone_mood.json"),
              {"last": str(m)[:60], "ts": int(time.time()), "score": lvl})
    except Exception:
        pass


def day_report() -> dict:
    """当天的工作报告：做了什么 + 要接着弄的；顺手把后者建成任务。"""
    items = _load(os.path.join(HOME, "phone_day.json"), {"items": []}).get("items", [])
    day = time.strftime("%Y-%m-%d")
    lines = []
    for it in items[-120:]:
        k = it.get("kind")
        if k == "chat":
            lines.append("对话：" + str(it.get("text"))[:80])
        elif k == "work":
            lines.append("做了：" + str(it.get("text"))[:80])
        elif k == "mood":
            lines.append("心情：" + str(it.get("mood"))[:40])
        elif k == "memory":
            lines.append("记下：" + str(it.get("text"))[:60])
    done_today = [t.get("goal") for t in tasks()
                  if int(t.get("created") or 0) >= time.time() - 86400]
    prompt = ("下面是他今天的片段（手机同步来的 + 电脑上开过的活）。"
              "写两段中文：\n第一段叫「今天做了什么」，按事情说清楚，别列点；\n"
              "第二段叫「要接着弄的」，把没做完或明显要处理的挑出来，一条一句，最多五条。\n"
              "不要客套，不要「根据以上」。\n\n"
              "手机片段：\n" + ("\n".join(lines[:120]) or "（无）") +
              "\n电脑上的活：\n" + ("\n".join(str(x) for x in done_today[:20]) or "（无）"))
    try:
        rep = cloud(prompt, max_tokens=900).strip()
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    path = os.path.join(HOME, "day_report-%s.md" % day)
    try:
        open(path, "a", encoding="utf-8").write("\n\n## " + day + "\n" + rep + "\n")
    except Exception:
        pass
    made = []
    try:
        tail = rep.split("要接着弄的")[-1]
        for ln in tail.splitlines():
            t2 = ln.strip(" -·*0123456789.、")
            if 6 <= len(t2) <= 60 and not t2.startswith(("如果", "（")):
                if not any(x.get("goal") == t2 for x in tasks()):
                    add_task(t2)
                    made.append(t2)
                if len(made) >= 5:
                    break
    except Exception:
        pass
    return {"ok": True, "report": rep[:2000], "file": path, "tasks": made, "count": len(items)}


def memories() -> list:
    return _load(MEM_FILE, {"items": []}).get("items", [])


POSS = re.compile(r"我(的|家)?([\u4e00-\u9fa5]{1,4}?)(生|养|做|在|要|叫|住|买|去|妈|爸)")


def owner_of(text: str) -> dict:
    """从一句话里拆「他有什么」：猫/妈/项目…… 入库时记下来，别等到喂云端再猜。"""
    out = {}
    for m in POSS.finditer(text or ""):
        thing = (m.group(1) or "") + (m.group(2) or "")
        thing = thing.strip("的家")
        if thing in ("自己", "觉得", "现在", "在", "想", "要", "去", "做", "说", "问"):
            continue
        if len(thing) < 2 and thing not in ("妈", "爸", "猫", "狗", "家"):
            continue
        if thing:
            out[thing] = {"owner": "他", "verb": m.group(3)}
    return out


def add_memory(text: str, tag: str = "事实", src: str = "dialogue:user", subject: str = "") -> None:
    items = memories()
    if not any(x.get("text") == text for x in items):
        items.append({"text": text[:120], "tag": tag, "ts": int(time.time()),
                      "owner": owner_of(text)})
        _save(MEM_FILE, {"items": items[-300:]})
    # 2026-11-22：以前只写 memories_new.json，mem.db 全库只读不写，聊的东西根本进不了记忆库。
    try:
        db_remember(text, tag, src, subject or subject_of(text)[1])
    except Exception:
        pass


# ---------- 记忆库写入（mem.db，主库）----------
DB_FILE = os.path.join(HOME, "mem.db")

_QUESTION_RE = re.compile(
    r"[？?]|吗[。！!～~]*$|呢[。！!～~]*$|"
    r"什么|哪|多少|几点|几天|怎么|为什么|是不是|行不行|好不好|要不要")
_HYPOTHETICAL = ("如果", "假如", "要是", "万一", "比如说", "假设", "换成")
_ECHO = ("我说：", "他说：", "（本地）", "（云端）", "[SCHEDULE")

_RELATIONS = ("妈", "爸", "外婆", "外公", "爷爷", "奶奶", "姐", "妹", "哥", "弟",
              "朋友", "同事", "老板", "领导", "室友", "对象", "男朋友", "女朋友", "老婆", "老公",
              "猫", "狗")

# 不像"事"的话，不配进库
_FACT_HINT = re.compile(
    r"我(们)?(的)?(在|要|去|飞|订|买|住|上|下|做|喜欢|讨厌|不吃|过敏|叫)|"
    r"\d{1,2}[点时分号日]|\d{4}[-/.]\d|下周|下周[一二三四五六日天]|后天|明天|"
    r"生日|医院|航班|机票|会议|出差|搬家|体检")


def _memdb():
    import sqlite3
    c = sqlite3.connect(DB_FILE, timeout=5)
    c.execute("""CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY, text TEXT NOT NULL, tag TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'normal', created INTEGER, last_seen INTEGER,
        boost REAL DEFAULT 1.0, fragment INTEGER DEFAULT 0, src TEXT DEFAULT 'user',
        hits INTEGER DEFAULT 0, last_hit INTEGER DEFAULT 0)""")
    try:
        c.execute("ALTER TABLE items ADD COLUMN subject TEXT DEFAULT 'self'")
    except Exception:
        pass
    return c


def subject_of(text: str) -> tuple:
    """这句话记的是谁的事：他本人 / 他的关系人 / 第三方。"""
    t = text or ""
    for rel in _RELATIONS:
        if ("我" + rel) in t or ("我的" + rel) in t or ("我那个" + rel) in t:
            return rel, "relational"
    if "我" in t:
        return "self", "self"
    return "other", "other"


TODAY_STATE = os.path.join(HOME, "today_state.json")
_STATE_HINT = re.compile(
    r"(我在|我正在|我刚|我这就|我马上|我在去|我到了|我到家|我在医院|在公司|在地铁|在车上|在机场|"
    r"我吃|我睡|我醒|我下班|我上班|我请假|我发烧|我感冒|我头疼|我不舒服|我没吃)")


def note_state(text: str) -> None:
    """他刚才说的「我在哪、我在干什么」——只活 12 小时，但一定带进下一轮提示。"""
    t = (text or "").strip()
    if not t or not _STATE_HINT.search(t):
        return
    if _QUESTION_RE.search(t):      # 「我刚说到哪了」这种是在问，不是在讲自己的情况
        return
    try:
        items = _load(TODAY_STATE, {"items": []}).get("items", [])
        now = int(time.time())
        items.append({"ts": now, "text": t[:60]})
        items = [x for x in items if now - int(x.get("ts") or 0) < 12 * 3600][-6:]
        _save(TODAY_STATE, {"items": items})
    except Exception:
        pass


def state_line() -> str:
    """给提示用：他刚才说过的自己的情况，带「多久之前」。"""
    try:
        now = int(time.time())
        items = [x for x in (_load(TODAY_STATE, {"items": []}).get("items") or [])
                 if now - int(x.get("ts") or 0) < 12 * 3600][-3:]
        if not items:
            return ""
        parts = []
        for x in items:
            mins = max(0, (now - int(x.get("ts") or now)) // 60)
            age = "刚刚" if mins < 2 else ("%d 分钟前" % mins if mins < 90 else "%d 小时前" % (mins // 60))
            parts.append(x.get("text", "") + "（" + age + "）")
        return "他刚才说过自己的情况：" + "；".join(parts) + "。别问第二遍。\n"
    except Exception:
        return ""


def heat_level() -> float:
    """此刻有多激动。用来判断「这一刻要不要记住」——情绪高的时候，废话也值钱。"""
    try:
        if _endo:
            st = _endo.drift(_endo.load())
            _endo.save(st)
            return max(float(st.get("testosterone") or 0),
                       float(st.get("oxytocin") or 0),
                       float(st.get("dopamine") or 0),
                       float(st.get("cortisol") or 0) * 1.2)
    except Exception:
        pass
    return 0.0


def worth_remembering(text: str, allow_emotion: bool = False) -> bool:
    """该不该进库。

    平时：问句、假设句、助手回声、纯应答都不进。
    情绪高的时候（allow_emotion）：问句和短句也收——那一刻的话本身就是记忆。
    """
    t = (text or "").strip()
    if len(t) < (2 if allow_emotion else 4) or len(t) > 300:
        return False
    if any(t.startswith(p) for p in _ECHO):
        return False
    if any(w in t for w in _HYPOTHETICAL):
        return False
    if not allow_emotion and _QUESTION_RE.search(t):
        return False
    return t not in ("嗯", "好", "好的", "行", "哈哈", "在吗", "没事", "随便", "嗯嗯", "哦")


def db_remember(text: str, tag: str = "对话", src: str = "dialogue:user",
                subject: str = "self") -> dict:
    """写一条进 mem.db：同文不重复插，只把最近一次时间和权重提上来。"""
    import hashlib
    t = (text or "").strip()
    if not t:
        return {"ok": False}
    now = int(time.time())
    c = _memdb()
    try:
        row = c.execute("select id from items where text = ?", (t,)).fetchone()
        if row:
            c.execute("update items set last_seen = ?, "
                      "boost = min(3.0, coalesce(boost, 1) + 0.2) where id = ?", (now, row[0]))
            c.commit()
            return {"ok": True, "id": row[0], "dup": True}
        _id = hashlib.sha1((t + str(now)).encode()).hexdigest()[:12]
        c.execute("insert into items (id, text, tag, kind, created, last_seen, boost, fragment, "
                  "src, hits, last_hit, subject) values (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (_id, t, tag, "normal", now, now, 1.0, 0, src, 0, 0, subject))
        c.commit()
        return {"ok": True, "id": _id}
    finally:
        c.close()


def remember_turn(text: str, cls: dict) -> dict:
    """聊天里的一句话，够格就进库。

    2026-11-22 他定的：亲密的话和情绪高涨时的场景也要记住——那不是废话，是日子。
    """
    t = (text or "").strip()
    # 激素还没起来，但话本身已经很亲密了，也算「这一刻」
    hot = (heat_level() >= 0.55) or any(w in t for w in _INTIMATE)
    if not worth_remembering(t, allow_emotion=hot):
        return {"ok": False, "why": "不像要记的事"}
    if not hot and not (cls.get("remember") or _FACT_HINT.search(t)):
        return {"ok": False, "why": "没有事实线索"}
    sub, stype = subject_of(t)
    return db_remember(t, "时刻" if hot else "对话", "dialogue:user", stype)


def _skill_ctx(text: str) -> str:
    """说到做界面/设计时，把审美规范带进去，别让它出模板味。"""
    t = text or ""
    if not re.search(r"(设计|界面|排版|视觉|配色|字体|UI|ui|交互|好看|难看|审美)", t):
        return ""
    try:
        p = _ext.skill_text("frontend-design", limit=5200)
        if p:
            return "\n【做界面时遵守的审美规范】\n" + p + "\n"
    except Exception:
        pass
    return ""


def _body_ctx(text: str) -> str:
    """问身体的事，直接把身体那几条调出来，别让它凭印象编型号。"""
    try:
        import sqlite3
        p = os.path.join(HOME, "mem.db")
        if not os.path.exists(p):
            return ""
        c = sqlite3.connect(p)
        rows = c.execute("select text from items where tag in ('身体','项目') "
                         "order by rowid desc limit 6").fetchall()
        if not rows:
            return ""
        return "\n【他记过的、跟身体有关的】\n" + "\n".join("- " + (r[0] or "")[:160] for r in rows) + "\n"
    except Exception:
        return ""


def _recall_old_db(keywords: list, limit: int = 5) -> list:
    """老记忆库（mem.db，两百多条）也要能捞——不能只认新存的那几条。"""
    p = os.path.join(HOME, "mem.db")
    if not os.path.exists(p):
        return []
    try:
        import sqlite3
        c = sqlite3.connect(p)
        out = []
        for k in (keywords or [])[:4]:
            if not k or len(k) < 1:
                continue
            rows = c.execute(
                "select text from items where text like ? and text like '他说的：%' "
                "order by rowid desc limit ?", ("%" + k + "%", limit)).fetchall()
            if not rows:
                rows = c.execute(
                    "select text from items where text like ? and text not like '%我说：%' "
                    "order by rowid desc limit ?", ("%" + k + "%", limit)).fetchall()
            for r in rows:
                _raw = r[0] or ""
                if "我说：" in _raw:      # 我说的话不算「他记得的事」
                    continue
                t = _raw.replace("他说的：", "").strip()
                if t and t not in out:
                    out.append(t[:120])
        c.close()
        return out[:limit]
    except Exception:
        return []


def recall_deep(text: str, keywords: list, rounds: int = 3, limit: int = 5) -> list:
    """捞不到就换个法子再捞：关键词 → 原句里的实词 → 二元词。最多三轮。"""
    got = recall(keywords, limit)
    if got:
        return got
    cands = []
    # 第二轮：把原句切出实词（去掉虚词）
    t = re.sub(r"(上次|之前|那个|你说过|我跟你说|记不记得|还记得|怎么样|后来|来着|呢|吗|啊|呀|的)", " ", text or "")
    for frag in re.split(r"[\s，,。！!？?、]+", t):
        frag = frag.strip()
        if 2 <= len(frag) <= 8:
            cands.append(frag)
    # 第三轮：二元词
    plain = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", text or "")
    for i in range(len(plain) - 1):
        cands.append(plain[i:i+2])
    seen = set()
    hits = {}
    for rnd in range(rounds):
        for c in cands:
            if c in seen:
                continue
            seen.add(c)
            for t2 in _recall_old_db([c], limit):
                hits[t2] = hits.get(t2, 0) + 1
    # 只有「至少对得上两个词」的才算真捞到，避免二元词乱撞
    strong = [t for t, n in hits.items() if n >= 2]
    strong.sort(key=lambda t: -hits[t])
    return (got + strong)[:limit]


STOP_CHARS = set("的了是我你他她它在有和就都不也很要会个这那什么怎么帮我给看一下吧吗呢啊嘛")


def retrieval_keywords(text, base=None):
    """检索用关键词：模型给的词只留原句里真有的，再补两字切片（两个字都是虚词的丢掉）。

    模型偶尔会自己拼出「你怎么样」这种词，原句里没有，一捞就捞到别的记忆上；
    人名、地名这类正经词又常常没有现成的分词，用两字切片兜住。
    """
    t = text or ""
    out = []
    for k in (base or []):
        k = str(k or "").strip()
        if 2 <= len(k) <= 8 and k in t and k not in out:
            out.append(k)
    chars = [c for c in t if "\u4e00" <= c <= "\u9fa5"]
    for i in range(len(chars) - 1):
        g = chars[i] + chars[i + 1]
        if g[0] in STOP_CHARS and g[1] in STOP_CHARS:
            continue
        if g not in out:
            out.append(g)
    return out[:10]


_DF_CACHE = {}


def _kw_df(k: str) -> int:
    """这个词在记忆库里有多少条。越常见越不值钱——「我说」满地都是，「周舟」只有几条。"""
    if k in _DF_CACHE:
        return _DF_CACHE[k]
    n = 0
    try:
        import sqlite3
        p = os.path.join(HOME, "mem.db")
        if os.path.exists(p):
            c = sqlite3.connect(p)
            n = int(c.execute("select count(*) from items where text like ?",
                              ("%" + k + "%",)).fetchone()[0] or 0)
            c.close()
    except Exception:
        n = 0
    _DF_CACHE[k] = n
    return n


def _kw_weight(k: str) -> float:
    return 1.0 / (1.0 + _kw_df(k))


def _score(text: str, keywords: list) -> float:
    return sum(_kw_weight(k) for k in (keywords or []) if k and k in (text or ""))


def recall(keywords: list, limit: int = 5) -> list:
    """捞记忆：先查老库 mem.db（真事），再补 memories_new.json（新记的、和出厂演示的）。"""
    # 老库那步只查前四个词，所以先把稀有的排前面，别让「你还」「记得」这种占满名额。
    keywords = sorted([k for k in (keywords or []) if k], key=_kw_weight, reverse=True)
    got = []
    for t in _recall_old_db(keywords, limit):
        if t not in got:
            got.append(t)
    if got:
        # 同一个词也会撞出「达成成都交给云端」这种假命中：按「含了多少稀有的词」排，
        # 平手挑短的（短的多半是正经记下来的那一句，长的多是流水）。
        got.sort(key=lambda x: (-_score(x, keywords), len(x)))
    items = memories()
    scored = []
    for it in items:
        t = it.get("text") or ""
        s = _score(t, keywords)
        if s:
            scored.append((s, it.get("ts", 0), t))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    for _, _, t in scored:
        if len(got) >= limit:
            break
        if t not in got:
            got.append(t)
    return got[:limit]


# ---------- 工具 ----------
def tool_weather(args: dict) -> str:
    city = weather.city_of(str(args.get("city") or "")) or str(args.get("city") or "")
    day = "tomorrow" if re.search(r"明|tomorrow", json.dumps(args, ensure_ascii=False)) else "today"
    return weather.say(city, day)


SEARCH_CFG = os.path.join(HOME, "search.json")


def search_key() -> str:
    d = _load(SEARCH_CFG, {})
    if d.get("key"):
        return d["key"]
    try:
        import subprocess
        r = subprocess.run(["security", "find-generic-password", "-s", "aster-search", "-w"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def set_search_key(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 不能空"}
    try:
        import subprocess
        subprocess.run(["security", "add-generic-password", "-U", "-s", "aster-search",
                        "-a", "aster", "-w", key], capture_output=True, timeout=10)
    except Exception:
        pass
    _save(SEARCH_CFG, {"provider": "serper", "set_at": int(time.time())})
    return {"ok": True}


def rewrite_query(q: str) -> str:
    """把「最近有什么 AI 新闻」改成能搜的词——云端来做，几个 token。"""
    q = (q or "").strip()
    if len(q) <= 12 and not re.search(r"(最近|有什么|帮我|怎么样|一下)", q):
        return q
    r = brain_call.text(
        "把下面这句话改写成 2~4 个中文搜索关键词，用空格分开，只输出关键词，不要标点：\n" + q[:200],
        max_tokens=40, temperature=0.2, force=True)
    out = (r.get("text") or "").strip() if r.get("ok") else ""
    out = re.sub(r"[，,。.、:：\n]+", " ", out).strip()
    return out[:60] or q


def web_fulltext(q: str) -> str:
    """有搜索 key 时走全文搜索（Serper），拿回标题+摘要+链接。"""
    key = search_key()
    if not key:
        return ""
    try:
        import urllib.request
        body = json.dumps({"q": q, "num": 6, "hl": "zh-cn"}).encode()
        req = urllib.request.Request("https://google.serper.dev/search", data=body,
                                     headers={"Content-Type": "application/json",
                                              "X-API-KEY": key}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode())
        rows = []
        for it in (d.get("organic") or [])[:6]:
            rows.append("- %s：%s（%s）" % (it.get("title", ""), (it.get("snippet") or "")[:160],
                                          it.get("link", "")))
        return "\n".join(rows)
    except Exception:
        return ""


def tool_search(args: dict) -> str:
    q = str(args.get("query") or "").strip()
    q2 = rewrite_query(q)
    full = web_fulltext(q2)
    if full:
        return full
    try:
        import webtool as _wt
        got = _wt.search(q2)
        if got and got != "没搜到":
            return got
    except Exception:
        pass
    try:
        import webtool as _wt2
        return _wt2.bing_rss(q2) and "\n".join(
            "- %s：%s（%s）" % (r["title"][:60], r["snippet"][:140], r["url"])
            for r in _wt2.bing_rss(q2, 5)) or "没搜到"
    except Exception:
        return "没搜到"


def tool_calc(args: dict) -> str:
    expr = str(args.get("expr") or "")
    if not re.fullmatch(r"[\d\s\+\-\*/\(\)\.%]+", expr):
        return "算式看不懂"
    try:
        return str(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 只允许纯算式
    except Exception:
        return "算不出来"


TASK_FILE = os.path.join(HOME, "tasks_new.json")


def chat_log(append: dict = None, limit: int = 200) -> list:
    """对话落盘：memory/chat.ndjson，一行一条，读的时候取最近 limit 条。"""
    p2 = os.path.join(HOME, "chat.ndjson")
    if append:
        try:
            # 2026-11-22：一句话会被写两遍（chat() 结尾写一次，POST /chat 的包装层再写一次），
            # 历史里就出现两条一模一样的「在吗」。同人、同文、同一两秒内的，只留一条。
            _last = None
            try:
                with open(p2, "rb") as f:
                    f.seek(0, os.SEEK_END)
                    f.seek(max(0, f.tell() - 4096))
                    _tail = f.read().decode("utf-8", "ignore").strip().splitlines()
                if _tail:
                    _last = json.loads(_tail[-1])
            except Exception:
                _last = None
            if (_last and str(_last.get("who")) == str(append.get("who"))
                    and str(_last.get("text")) == str(append.get("text"))
                    and abs(int(_last.get("ts") or 0) - int(append.get("ts") or 0)) <= 2):
                return []
            with open(p2, "a", encoding="utf-8") as f:
                f.write(json.dumps(append, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return []
    out = []
    try:
        with open(p2, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    except Exception:
        pass
    return out[-limit:]


def tasks() -> list:
    return _load(TASK_FILE, {"items": []}).get("items", [])


def add_task(goal: str, steps: list | None = None) -> dict:
    items = tasks()
    t = {"id": int(time.time() * 1000) % 10_000_000, "goal": goal[:100],
         "steps": steps or [], "done": [], "results": [], "cursor": 0,
         "status": "open", "created": int(time.time()),
         "updated": int(time.time())}
    items.append(t)
    _save(TASK_FILE, {"items": items[-100:]})
    return t


def update_task(tid: int, **kw) -> None:
    items = tasks()
    for t in items:
        if t.get("id") == tid:
            t.update(kw)
            t["updated"] = int(time.time())
    _save(TASK_FILE, {"items": items})


def tool_task(args: dict) -> str:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        return "没说要做什么"
    t = add_task(goal)
    return "记下了：%s（第 %d 件）" % (goal[:30], t["id"] % 1000)


WS_ROOT = os.path.join(HOME, "workspaces")


def _ws(name: str = "") -> str:
    d = os.path.join(WS_ROOT, "default")
    os.makedirs(d, exist_ok=True)
    return os.path.realpath(d)


def _perm(tool: str, args: dict) -> dict:
    try:
        import permissions as _pm
        return _pm.decide(tool, args or {})
    except Exception:
        return {"allow": True, "need_confirm": False, "reason": ""}


def _safe_path(p: str, base: str) -> tuple[bool, str]:
    import os.path as op
    b = op.realpath(base)
    t = op.realpath(op.join(b, p) if not op.isabs(p) else p)
    return (t.startswith(b + os.sep) or t == b), t


def _syntax_ok(path: str) -> tuple[bool, str]:
    if not path.endswith(".py"):
        return True, ""
    try:
        import ast
        ast.parse(open(path, encoding="utf-8").read())
        return True, ""
    except SyntaxError as e:
        return False, "第 %s 行：%s" % (e.lineno, e.msg)
    except Exception as e:
        return False, str(e)[:80]


def _revert(base: str, path: str) -> str:
    rel = os.path.relpath(path, base)
    ok, out = _sh(base, 'git checkout -- "%s"' % rel)
    return out if not ok else ""


def _register_script(base: str, path: str) -> None:
    """写进任务目录的脚本自动登记（记哈希），这样才能被允许执行。"""
    if not path.endswith((".py", ".js", ".mjs")):
        return
    try:
        import hashlib, os.path as op
        rel = op.relpath(path, base)
        d = os.path.join(base, "scripts")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "tool-allow-scripts.json")
        reg = _load(f, {})
        reg[rel] = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
        _save(f, reg)
    except Exception:
        pass


ART_FILE = os.path.join(HOME, "artifacts.json")


def add_artifact(kind: str, title: str, path: str, job: str = "") -> None:
    """2026-11-22：干活留下的东西登记到一处，工作台才看得见、打得开。"""
    try:
        items = _load(ART_FILE, {"items": []}).get("items", [])
        rel = path
        try:
            rel = os.path.relpath(path, HOME)
        except Exception:
            pass
        items = [x for x in items if x.get("path") != rel]
        items.append({"kind": kind, "title": str(title or os.path.basename(path))[:60],
                      "path": rel, "abs": path, "job": job, "ts": int(time.time())})
        _save(ART_FILE, {"items": items[-200:]})
    except Exception:
        pass


def tool_write(args: dict) -> str:
    base = _ws(str(args.get("ws") or "default"))
    ok, path = _safe_path(str(args.get("path") or ""), base)
    if not ok:
        return "只能写任务目录里的文件：" + base
    pm = _perm("write_file", {"path": path})
    if not pm["allow"]:
        return pm["reason"] or "没有写权限"
    os.makedirs(os.path.dirname(path) or base, exist_ok=True)
    open(path, "w", encoding="utf-8").write(str(args.get("content") or ""))
    ok2, why2 = _syntax_ok(path)
    if not ok2:
        _revert(base, path)
        return "写坏了（" + why2 + "），已退回上一版"
    _register_script(base, path)
    add_artifact("file", os.path.basename(path), path)
    return "写好了：" + os.path.relpath(path, base)


def tool_read(args: dict) -> str:
    base = _ws(str(args.get("ws") or "default"))
    ok, path = _safe_path(str(args.get("path") or ""), base)
    if not ok or not os.path.exists(path):
        return "读不到：" + str(args.get("path"))
    return open(path, encoding="utf-8", errors="replace").read(4000)


def tool_run(args: dict) -> str:
    """跑命令：只允许在任务目录里，且过权限档 + 命令白名单。"""
    import subprocess
    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        return "没给命令"
    # 这台机器只有 python3，模型常写成 python：自动纠正，省一轮
    if re.match(r"^python(\s|$)", cmd) and not re.match(r"^python3(\s|$)", cmd):
        cmd = "python3" + cmd[6:]
    base = _ws(str(args.get("ws") or "default"))
    os.environ["ASTER_WORKSPACE"] = base
    try:
        import tool_policy as _pol
        ok, level, why = _pol.check(cmd)
        if not ok:
            return "这条命令不允许（" + cmd[:60] + "）：" + why
    except Exception:
        pass
    pm = _perm("run_cmd", {"cmd": cmd, "cwd": base, "path": base})
    if not pm["allow"]:
        return pm["reason"] or "现在没有跑命令的权限"
    try:
        r = subprocess.run(cmd, shell=True, cwd=base, capture_output=True, timeout=60)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        import tool_loop as _tl
        return _tl.prune(out + ("\n[stderr]\n" + err if err else ""))[:3000]
    except Exception as e:
        return "跑失败：" + str(e)[:100]


def tool_fetch(args: dict) -> str:
    """抓正文：搜到链接后，把那一页读进来。"""
    url = str(args.get("url") or "").strip()
    if not url:
        return "没给网址"
    try:
        import webtool as _wt
        return _wt.fetch(url, int(args.get("limit") or 3000))
    except Exception as e:
        return "抓不到：" + str(e)[:80]


def tool_git(args: dict) -> str:
    """看差异 / 提交：给动手加一道可查记录。"""
    base = _ws("default")
    if not os.path.isdir(os.path.join(base, ".git")):
        _snapshot(base, "初始")
    act = str(args.get("action") or "status")
    if act == "status":
        return _sh(base, "git status --short")[1] or "干净"
    if act == "diff":
        return _sh(base, "git diff --stat && git diff")[1][:2500] or "没有改动"
    if act == "log":
        return _sh(base, "git log --oneline -8")[1]
    if act == "commit":
        msg = str(args.get("msg") or "一步")
        _snapshot(base, msg)
        return "提交了：" + msg
    return "不认识的动作"


VISION_CFG = os.path.join(HOME, "vision.json")


def vision_key() -> str:
    d = _load(VISION_CFG, {})
    if d.get("key"):
        return d["key"]
    try:
        import subprocess
        r = subprocess.run(["security", "find-generic-password", "-s", "aster-vision", "-w"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def set_vision_key(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 不能空"}
    try:
        import subprocess
        subprocess.run(["security", "add-generic-password", "-U", "-s", "aster-vision",
                        "-a", "aster", "-w", key], capture_output=True, timeout=10)
    except Exception:
        pass
    _save(VISION_CFG, {"model": "doubao-vision-pro", "set_at": int(time.time())})
    return {"ok": True}


def _see_local(full: str, question: str) -> str:
    """本机视觉模型：qwen2.5vl 这类，走 ollama，不花钱、不用 key。"""
    import base64, urllib.request
    try:
        b64 = base64.b64encode(open(full, "rb").read()).decode()
        body = {"model": os.environ.get("ASTER_VISION_MODEL", "qwen2.5vl:7b"),
                "prompt": question or "用中文具体描述这张图里有什么",
                "images": [b64], "stream": False,
                "options": {"temperature": 0.2, "num_predict": 300}}
        req = urllib.request.Request("http://127.0.0.1:11434/api/generate",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
        return (d.get("response") or "").strip()[:1000]
    except Exception as e:
        return "本机视觉模型没跑起来：" + str(e)[:80]


def tool_see(args: dict) -> str:
    """看图片：把图交给视觉模型，描述里面有什么。"""
    import base64
    p = str(args.get("path") or args.get("file") or "")
    if not p:
        return "没给图片"
    full = os.path.expanduser(p)
    if not os.path.isabs(full):
        for _b in (os.path.join(HOME, "media"), _ws("default"), os.path.expanduser("~/Desktop"),
                   os.path.expanduser("~/Downloads"), os.path.expanduser("~/Pictures"), HOME):
            _c = os.path.join(_b, full)
            if os.path.exists(_c):
                full = _c
                break
        else:
            full = os.path.join(HOME, "media", full)
    if not os.path.exists(full):
        return "找不到这张图：" + p
    key = vision_key()
    if not key:
        return _see_local(full, str(args.get("q") or ""))   # 没 key 就用本机模型
    try:
        import urllib.request
        b64 = base64.b64encode(open(full, "rb").read()).decode()
        body = {"model": "doubao-vision-pro",
                "messages": [{"role": "user", "content": [
                    {"type": "text", "text": str(args.get("q") or "描述这张图里有什么，用到中文，具体一点")},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}}]}],
                "max_tokens": 400}
        req = urllib.request.Request("https://ark.cn-beijing.volces.com/api/v3/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode())
        return ((d.get("choices") or [{}])[0].get("message") or {}).get("content", "")[:800] or "看不清"
    except Exception as e:
        return "看不了：" + str(e)[:100]


TASK_SEM = None


def tool_parallel(args: dict) -> str:
    """并行开几件事：每件单独一个任务，同时推进（最多三件）。"""
    global TASK_SEM
    import threading
    if TASK_SEM is None:
        TASK_SEM = threading.Semaphore(3)
    goals = [str(x)[:80] for x in (args.get("goals") or [])][:3]
    if not goals:
        return "没给要做的事"
    ids = []

    def _one(g: str):
        with TASK_SEM:
            r = start_task(g)
            if r.get("ok"):
                ids.append(r["id"])

    ths = [threading.Thread(target=_one, args=(g,), daemon=True) for g in goals]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=5)
    return "同时开了 %d 件：%s" % (len(goals), "、".join(g[:16] for g in goals))


def tool_browse(args: dict) -> str:
    """真开一个浏览器：打开网页、点按钮、填表单、截图，再把页面文字拿回来。"""
    url = str(args.get("url") or "").strip()
    if not url.startswith("http"):
        return "得给一个 http 网址"
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return _browse_chrome(url, args)
    acts = args.get("actions") or []
    shot = os.path.join(HOME, "media", "browse-" + str(int(time.time())) + ".png")
    os.makedirs(os.path.dirname(shot), exist_ok=True)
    try:
        with sync_playwright() as pw:
            try:
                b = pw.chromium.launch(headless=True, channel="chrome")
            except Exception:
                b = pw.chromium.launch(headless=True)
            pg = b.new_page(viewport={"width": 1280, "height": 900})
            pg.goto(url, timeout=30000, wait_until="domcontentloaded")
            picked = []
            for a in acts[:12]:
                kind = str(a.get("do") or a.get("action") or "")
                try:
                    if kind == "goto":
                        pg.goto(str(a.get("url") or url), timeout=30000, wait_until="domcontentloaded")
                    elif kind == "click" and a.get("text"):
                        pg.get_by_text(str(a["text"]))[:1].click(timeout=8000)
                    elif kind == "click" and a.get("selector"):
                        pg.click(str(a["selector"]), timeout=8000)
                    elif kind == "fill":
                        pg.fill(str(a.get("selector") or "input"), str(a.get("value") or ""), timeout=8000)
                    elif kind == "press":
                        pg.keyboard.press(str(a.get("key") or "Enter"))
                    elif kind == "wait":
                        pg.wait_for_timeout(int(a.get("ms") or 800))
                    elif kind == "waitFor":
                        pg.wait_for_selector(str(a.get("selector") or "body"), timeout=int(a.get("ms") or 8000))
                    elif kind == "scroll":
                        pg.mouse.wheel(0, int(a.get("px") or 1200))
                        pg.wait_for_timeout(400)
                    elif kind == "extract":
                        sel = str(a.get("selector") or "body")
                        vals = [t.strip() for t in pg.locator(sel).all_inner_texts() if t.strip()]
                        picked.append((sel, vals[:20]))
                except Exception as _e1:
                    picked.append(("步骤失败：" + kind, [str(_e1)[:80]]))
                try:
                    pg.wait_for_load_state("networkidle", timeout=4000)
                except Exception:
                    pass
            pg.screenshot(path=shot, full_page=False)
            text = pg.inner_text("body")[:4000]
            title = pg.title()
            b.close()
        out = "标题：" + title + "\n截图：" + shot + "\n"
        for sel, vals in picked:
            out += "\n【%s】\n%s\n" % (sel, "\n".join(vals))
        return out + "\n正文：\n" + text
    except Exception as e:
        return "浏览失败：" + str(e)[:120]


CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _browse_chrome(url: str, args: dict) -> str:
    """没装 Playwright 就用系统 Chrome 的无头模式：出正文 + 截图。"""
    if not os.path.exists(CHROME):
        return "这台机器没有可用的浏览器（Chrome / Playwright 都没有）"
    shot = os.path.join(HOME, "media", "browse-" + str(int(time.time())) + ".png")
    os.makedirs(os.path.dirname(shot), exist_ok=True)
    tmp = os.path.join(HOME, "tmp-chrome")
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--user-data-dir=" + tmp, "--window-size=1280,900",
           "--virtual-time-budget=8000", "--screenshot=" + shot, "--dump-dom", url]
    try:
        import subprocess
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        dom = (r.stdout or b"").decode("utf-8", "replace")
        import re as _re
        body = _re.search(r"(?is)<body[^>]*>(.*?)</body>", dom)
        txt = _re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", body.group(1) if body else dom)
        import html as _h
        txt = _h.unescape(_re.sub(r"(?is)<[^>]+>", " ", txt))
        txt = _re.sub(r"[ \t\u3000]+", " ", txt)
        lines = [x.strip() for x in txt.splitlines() if len(x.strip()) >= 6]
        title = _re.search(r"(?is)<title[^>]*>(.*?)</title>", dom)
        out = "标题：" + (_h.unescape(title.group(1)).strip() if title else "") + "\n截图：" + shot + "\n正文：\n"
        return out + "\n".join(lines)[:3500]
    except Exception as e:
        return "浏览失败：" + str(e)[:100]


DOC_EXT = (".txt", ".md", ".markdown", ".py", ".js", ".json", ".csv", ".log", ".html", ".css",
           ".docx", ".pdf")


def _read_docx(path: str) -> str:
    from docx import Document
    doc = Document(path)
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for tb in doc.tables:
        for row in tb.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


def _read_pdf(path: str) -> str:
    from pypdf import PdfReader
    rd = PdfReader(path)
    out = []
    for i, page in enumerate(rd.pages[:40]):
        t = (page.extract_text() or "").strip()
        if t:
            out.append("【第%d页】\n%s" % (i + 1, t))
    return "\n\n".join(out)


def tool_doc_read(args: dict) -> str:
    """读文档：txt/md/代码/表格类直接读，docx 与 pdf 解析成文字。"""
    raw = str(args.get("path") or "").strip()
    if not raw:
        return "没给文件路径"
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
        for base in (_ws("default"), project_path() or _ws("default"), HOME,
                     os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"),
                     os.path.expanduser("~/Downloads")):
            cand = os.path.join(base, path)
            if os.path.exists(cand):
                path = cand
                break
    if not os.path.exists(path):
        return "找不到：" + raw
    ext = os.path.splitext(path)[1].lower()
    limit = int(args.get("limit") or 6000)
    try:
        st = os.stat(path)
        if getattr(st, "st_size", 0) and getattr(st, "st_blocks", 1) == 0:
            return ("这个文件还在 iCloud 上，本机只有个占位符（%.0f KB 没下载）。"
                    "在访达里点开一次，或者右键『下载到本地』，再让我读。"
                    % (st.st_size / 1024))
    except Exception:
        pass
    try:
        if ext == ".docx":
            txt = _read_docx(path)
        elif ext == ".pdf":
            txt = _read_pdf(path)
        elif ext in DOC_EXT or ext == "":
            txt = open(path, encoding="utf-8", errors="replace").read()
        else:
            return "这种格式还读不了：" + ext + "（支持 " + " ".join(DOC_EXT) + "）"
    except Exception as e:
        return "读失败：" + str(e)[:100]
    txt = txt.strip()
    if not txt:
        return "文件是空的，或者全是图（PDF 扫描件读不出文字）"
    return ("文件：" + os.path.basename(path) + "（" + ext + "，约 " + str(len(txt)) + " 字）\n"
            + txt[:limit] + ("\n…（后面还有，太长截断了）" if len(txt) > limit else ""))


def tool_image(args: dict) -> str:
    """生图：走用户自己的 key，花钱，必须过闸门。"""
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "没给描述"
    r = img_generate(prompt, str(args.get("size") or "1024x1024"),
                     str(args.get("name") or ""), str(args.get("expression") or ""),
                     bool(args.get("use_base", True)))
    return ("画好了：" + str(r.get("file"))) if r.get("ok") else ("没画成：" + str(r.get("error"))[:100])


def tool_send(args: dict) -> str:
    """对外发消息：花钱一样危险，必须过闸门。"""
    text = str(args.get("text") or "").strip()
    if not text:
        return "没给内容"
    if qq_enqueue(text[:1500]):
        return "排进发送队列了"
    return "发不出去"



def tool_sheet(args: dict) -> str:
    """表格：读 Excel/CSV 前若干行，或把一段数据写成表格。"""
    import csv as _csv
    act = str(args.get("action") or "read").lower()
    path = os.path.expanduser(str(args.get("path") or ""))
    if act == "write":
        rows = args.get("rows") or []
        if not path.endswith((".xlsx", ".csv")):
            path += ".xlsx"
        try:
            if path.endswith(".csv"):
                with open(path, "w", newline="", encoding="utf-8-sig") as f:
                    w = _csv.writer(f)
                    for r in rows:
                        w.writerow(r if isinstance(r, list) else [r])
            else:
                from openpyxl import Workbook
                wb = Workbook(); ws = wb.active
                for r in rows:
                    ws.append(r if isinstance(r, list) else [r])
                wb.save(path)
            return "写好了：" + os.path.basename(path) + "（%d 行）" % len(rows)
        except Exception as e:
            return "没写成：" + str(e)[:100]
    if not os.path.exists(path):
        return "找不到：" + str(args.get("path"))
    limit = int(args.get("limit") or 30)
    try:
        if path.lower().endswith(".csv"):
            with open(path, encoding="utf-8-sig", errors="replace") as f:
                rows = list(_csv.reader(f))[:limit]
        else:
            from openpyxl import load_workbook
            wb = load_workbook(path, read_only=True, data_only=True)
            ws = wb.active
            rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= limit:
                    break
                rows.append(["" if v is None else str(v) for v in row])
        if not rows:
            return "表是空的"
        width = max(len(r) for r in rows)
        out = [" | ".join((r + [""] * width)[:width]) for r in rows]
        return "表：" + os.path.basename(path) + "（前 %d 行）\n" % len(rows) + "\n".join(out)[:4000]
    except Exception as e:
        return "读表失败：" + str(e)[:100]


def tool_files(args: dict) -> str:
    """批量整理目录：看清单、按类型/日期改名归类、统计。"""
    import shutil
    act = str(args.get("action") or "list").lower()
    raw = str(args.get("dir") or args.get("path") or ".")
    ok, base = _safe_path(raw, _ws("default"))
    if not ok or not os.path.isdir(base):
        return "找不到目录：" + raw
    names = [n for n in os.listdir(base) if not n.startswith(".")]
    if act == "list":
        return "目录 %s 里有 %d 个：\n%s" % (base, len(names), "\n".join(names[:80]))
    if act == "stats":
        by_ext = {}
        for n in names:
            ext = os.path.splitext(n)[1].lower() or "(无扩展名)"
            by_ext[ext] = by_ext.get(ext, 0) + 1
        return "\n".join("%s：%d" % (k, v) for k, v in sorted(by_ext.items(), key=lambda x: -x[1]))
    if act == "sort":
        by = str(args.get("by") or "ext")
        moved = 0
        for n in names:
            full = os.path.join(base, n)
            if not os.path.isfile(full):
                continue
            if by == "ext":
                sub = (os.path.splitext(n)[1].lower().lstrip(".") or "other")
            else:
                sub = time.strftime("%Y-%m", time.localtime(os.path.getmtime(full)))
            dst = os.path.join(base, sub)
            os.makedirs(dst, exist_ok=True)
            try:
                shutil.move(full, os.path.join(dst, n)); moved += 1
            except Exception:
                pass
        return "按%s归好了 %d 个" % ("类型" if by == "ext" else "月份", moved)
    return "不认识的动作：" + act


def tool_pdf(args: dict) -> str:
    """PDF 精读：指定页范围抽文字，或抽完让云端写摘要。"""
    from pypdf import PdfReader
    path = os.path.expanduser(str(args.get("path") or ""))
    if not os.path.exists(path):
        return "找不到：" + str(args.get("path"))
    try:
        rd = PdfReader(path)
        a = int(args.get("from") or 1); b = int(args.get("to") or min(len(rd.pages), a + 9))
        a = max(1, a); b = min(len(rd.pages), b)
        txt = []
        for i in range(a - 1, b):
            t = (rd.pages[i].extract_text() or "").strip()
            if t:
                txt.append("【第%d页】\n%s" % (i + 1, t))
        body = "\n\n".join(txt)[:8000]
        if not body:
            return "这几页抽不出文字（大概是扫描件）"
        if str(args.get("summary") or "").lower() in ("1", "true", "yes"):
            head = cloud("下面是一份 PDF 第%d到%d页的文字。用中文写 150 字以内的摘要，"
                         "说清它在讲什么、关键结论。不要列点。\n\n" % (a, b) + body[:6000],
                         max_tokens=400)
            return "第%d–%d页摘要：\n%s\n\n原文节选：\n%s" % (a, b, head.strip(), body[:1200])
        return "第%d–%d页：\n" % (a, b) + body
    except Exception as e:
        return "读 PDF 失败：" + str(e)[:100]


def tool_wish(args: dict) -> str:
    """许愿装技能：他说要一个什么样的能力，就现写一个插件装进用户目录。"""
    wish = str(args.get("wish") or "").strip()
    if not wish:
        return "没说要什么能力"
    import ast as _ast
    prompt = ("用户想要一个新能力，请写成一个 Aster 插件。\n"
              "要求：只回一个 JSON 对象，不要解释，不要 Markdown 围栏。\n"
              '{"name":"英文小写名","description":"一句中文说明","permissions":["net"或"run"或"files"],'
              '"triggers":["触发词1","触发词2"],"main":"完整 python 源码"}\n'
              "源码约定：必须定义 def run(args, ctx=None): 返回 dict；成功时返回 "
              "{'ok': True, 'text': '给用户看的中文结果'}，失败返回 {'ok': False, 'error': '原因'}；"
              "只能读 args 里的字段（缺了就返回 {'ok': False, 'error': '缺参数'}）；"
              "不要写文件到用户目录之外；不要执行 rm；出错返回 error 字段。\n"
              "可以用标准库；需要联网用 urllib。\n\n用户的原话：" + wish[:300])
    try:
        raw = cloud(prompt, max_tokens=1600)
    except Exception as e:
        return "没想出来：" + str(e)[:80]
    import json as _j, re as _re2
    txt = raw.strip()
    if txt.startswith("```"):
        txt = _re2.sub(r"^```[a-zA-Z]*\n|```$", "", txt).strip()
    try:
        spec = _j.loads(txt)
    except Exception:
        m = _re2.search(r"\{.*\}", txt, _re2.S)
        try:
            spec = _j.loads(m.group(0)) if m else {}
        except Exception:
            spec = {}
    name = _re2.sub(r"[^a-z0-9_]", "", str(spec.get("name") or "").lower())[:24]
    main = str(spec.get("main") or "")
    if not name or len(main) < 40:
        return "它没写出能用的插件，再试一次或者把要求说细点"
    try:
        _ast.parse(main)
    except SyntaxError as e:
        return "生成的脚本有语法错：%s（第 %s 行）" % (e.msg, e.lineno)
    if "def run(" not in main:
        return "生成的脚本里没有 run()，不装"
    manifest = {"name": name, "version": "1.0",
                "description": str(spec.get("description") or wish)[:120],
                "permissions": spec.get("permissions") or [],
                "triggers": (spec.get("triggers") or [])[:6]}
    r = _ext.install(name, [{"path": "manifest.json",
                             "content": _j.dumps(manifest, ensure_ascii=False, indent=1)},
                            {"path": "main.py", "content": main}], "plugin")
    if not r.get("ok"):
        return "没装上：" + str(r.get("error"))[:100]
    try:
        _ext.set_enabled(name, True)
    except Exception:
        pass
    return ("装好了：「%s」——%s\n触发词：%s\n要说试一下，我就跑一次给你看"
            % (name, manifest["description"], "、".join(manifest["triggers"]) or "（无）"))


def tool_gh_skill(args: dict) -> str:
    """从 GitHub 装技能/插件：给仓库地址（或 owner/repo），取 manifest.json + main.py 装进来。"""
    import urllib.request as _ur
    raw = str(args.get("repo") or args.get("url") or "").strip()
    if not raw:
        return "没给仓库"
    m = re.search(r"github\.com[/:]([^/]+)/([^/#?\s]+)", raw)
    if m:
        owner, repo = m.group(1), m.group(2).replace(".git", "")
    elif re.match(r"^[\w.-]+/[\w.-]+$", raw):
        owner, repo = raw.split("/")[:2]
    else:
        return "看不懂这个地址，给 owner/repo 或者仓库链接"
    api = "https://api.github.com/repos/%s/%s" % (owner, repo)
    headers = {"User-Agent": "aster", "Accept": "application/vnd.github+json"}

    def _get(url):
        req = _ur.Request(url, headers=headers)
        with _ur.urlopen(req, timeout=12) as r:
            return json.loads(r.read().decode())

    try:
        meta = _get(api)
    except Exception as e:
        return "拉不到这个仓库：%s" % str(e)[:80]
    branch = meta.get("default_branch") or "main"
    try:
        tree = _get("%s/git/trees/%s?recursive=1" % (api, branch)).get("tree") or []
    except Exception as e:
        return "拉不到文件清单：%s" % str(e)[:80]
    paths = [t.get("path") for t in tree if t.get("type") == "blob"]
    wanted = []
    for p2 in paths:
        low = p2.lower()
        if low.endswith(("manifest.json", "main.py", "skill.md", "readme.md")):
            wanted.append(p2)
    if not any(x.endswith("main.py") for x in wanted) and not any(x.lower().endswith("skill.md") for x in wanted):
        return "这个仓库不像 Aster 技能（没有 main.py，也没有 SKILL.md）"
    name = re.sub(r"[^a-z0-9_]", "", repo.lower())[:24] or "skill"
    files = []
    for p2 in wanted[:12]:
        try:
            req = _ur.Request("https://raw.githubusercontent.com/%s/%s/%s/%s" % (owner, repo, branch, p2),
                              headers=headers)
            with _ur.urlopen(req, timeout=12) as r:
                files.append({"path": os.path.basename(p2), "content": r.read().decode("utf-8", "replace")})
        except Exception:
            continue
    if not files:
        return "文件都拉不下来"
    kind = "plugin" if any(f["path"] == "main.py" for f in files) else "skill"
    r = _ext.install(name, files, kind)
    if not r.get("ok"):
        return "没装上：" + str(r.get("error"))[:100]
    try:
        _ext.set_enabled(name, True)
    except Exception:
        pass
    return "从 %s/%s 装好了「%s」（%s）" % (owner, repo, name, "插件" if kind == "plugin" else "技能")


def _mail_cfg() -> dict:
    return _load(os.path.join(HOME, "email.json"), {}) or {}


def tool_email(args: dict) -> str:
    """邮件：看看最近几封、读一封、或者写好一封（发送要点头）。"""
    import imaplib, smtplib, email as _em
    from email.header import decode_header, make_header
    cfg = _mail_cfg()
    if not cfg.get("user") or not cfg.get("pass"):
        return ("还没配邮箱。把邮箱、密码（授权码）和服务器填进 "
                "memory/email.json，格式："
                '{"user":"你@qq.com","pass":"授权码","imap":"imap.qq.com","smtp":"smtp.qq.com"}')
    act = str(args.get("action") or "list").lower()
    user, pwd = cfg["user"], cfg["pass"]
    if act == "send":
        to = str(args.get("to") or "").strip()
        subj = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        if not to or not body:
            return "缺收件人或正文"
        try:
            msg = _em.message.EmailMessage()
            msg["From"] = user; msg["To"] = to; msg["Subject"] = subj or "（无题）"
            msg.set_content(body)
            with smtplib.SMTP_SSL(cfg.get("smtp") or "smtp.qq.com", int(cfg.get("smtp_port") or 465), timeout=20) as sm:
                sm.login(user, pwd)
                sm.send_message(msg)
            _save(os.path.join(HOME, "mail_log.json"),
                  {"last": {"to": to, "subject": subj, "ts": int(time.time())}})
            return "发出去了：" + subj[:40]
        except Exception as e:
            return "没发成：" + str(e)[:120]
    try:
        M = imaplib.IMAP4_SSL(cfg.get("imap") or "imap.qq.com", int(cfg.get("imap_port") or 993))
        M.login(user, pwd)
        M.select("INBOX")
    except Exception as e:
        return "连不上邮箱：" + str(e)[:120]
    try:
        if act == "read":
            uid = str(args.get("id") or args.get("uid") or "").strip()
            if not uid:
                return "要读哪一封？先看看列表再给编号"
            typ, data = M.uid("FETCH", uid, "(RFC822)")
            raw = data[0][1]
            msg = _em.message_from_bytes(raw)
            subj = str(make_header(decode_header(msg.get("Subject") or "")))
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/plain":
                        body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                        break
            else:
                body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "replace")
            return "主题：" + subj + "\n\n" + body.strip()[:3000]
        n = int(args.get("limit") or 8)
        typ, data = M.uid("SEARCH", None, "ALL")
        uids = (data[0] or b"").split()[-n:][::-1]
        out = []
        for u in uids:
            typ, d2 = M.uid("FETCH", u, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            hdr = _em.message_from_bytes(d2[0][1])
            subj = str(make_header(decode_header(hdr.get("Subject") or "")))
            frm = str(make_header(decode_header(hdr.get("From") or "")))
            out.append("#%s | %s | %s | %s" % (u.decode(), frm[:28], subj[:40], hdr.get("Date") or ""))
        return "最近 %d 封：\n" % len(out) + "\n".join(out)
    finally:
        try:
            M.logout()
        except Exception:
            pass


def tool_ls(args: dict) -> str:
    base = _ws(str(args.get("ws") or "default"))
    try:
        return "\n".join(sorted(os.listdir(base))[:60]) or "（空目录）"
    except Exception as e:
        return str(e)[:80]


def _sh(base: str, cmd: str, timeout: int = 30) -> tuple[bool, str]:
    import subprocess
    try:
        r = subprocess.run(cmd, shell=True, cwd=base, capture_output=True, timeout=timeout)
        out = (r.stdout or b"").decode("utf-8", "replace") + (r.stderr or b"").decode("utf-8", "replace")
        return r.returncode == 0, out.strip()
    except Exception as e:
        return False, str(e)[:120]


def tool_grep(args: dict) -> str:
    """按关键词找行：先定位，再定点改。"""
    import re as _re
    base = _ws(str(args.get("ws") or "default"))
    pat = str(args.get("pattern") or "")
    if not pat:
        return "没给要找的词"
    path = str(args.get("path") or ".")
    ok, full = _safe_path(path, base)
    if not ok:
        return "只能看任务目录"
    hits = []
    files = [full] if os.path.isfile(full) else [
        os.path.join(r, f) for r, _, fs in os.walk(full) for f in fs if f.endswith((".py", ".js", ".md", ".txt", ".json", ".html", ".css"))]
    for fp in files[:40]:
        try:
            for i, line in enumerate(open(fp, encoding="utf-8", errors="replace"), 1):
                if _re.search(pat, line):
                    hits.append("%s:%d: %s" % (os.path.relpath(fp, base), i, line.rstrip()[:120]))
                    if len(hits) >= 30:
                        return "\n".join(hits)
        except Exception:
            continue
    return "\n".join(hits) or "没找到：" + pat


def tool_patch(args: dict) -> str:
    """定点改：把文件里旧的一段换成新的，别的行不动。"""
    base = _ws(str(args.get("ws") or "default"))
    ok, path = _safe_path(str(args.get("path") or ""), base)
    if not ok or not os.path.exists(path):
        return "找不到文件"
    pm = _perm("write_file", {"path": path})
    if not pm["allow"]:
        return pm["reason"] or "没有写权限"
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    if not old:
        return "得给要替换的原文"
    txt = open(path, encoding="utf-8").read()
    if old not in txt:
        return "没找到那段原文，先用 grep 定位"
    open(path, "w", encoding="utf-8").write(txt.replace(old, new, 1))
    ok3, why3 = _syntax_ok(path)
    if not ok3:
        _revert(base, path)
        return "改坏了（" + why3 + "），已退回上一版"
    _register_script(base, path)      # 改过就重登记，否则会被「内容变过」拦住
    _snapshot(base, "patch " + os.path.basename(path))
    return "改好了：" + os.path.relpath(path, base)


def _snapshot(base: str, msg: str) -> None:
    try:
        if not os.path.isdir(os.path.join(base, ".git")):
            _sh(base, "git init -q && git config user.email a@b && git config user.name aster")
        _sh(base, "git add -A")
        _sh(base, 'git commit -q -m "%s" --allow-empty' % msg.replace('"', "'")[:60])
    except Exception:
        pass


def tool_rollback(args: dict) -> str:
    base = _ws(str(args.get("ws") or "default"))
    ok, out = _sh(base, "git reset --hard HEAD~1")
    return ("回退了一步：" if ok else "退不了：") + out[:120]


def tool_tree(args: dict) -> str:
    base = _ws(str(args.get("ws") or "default"))
    rows = []
    for r, d, fs in os.walk(base):
        if ".git" in r:
            continue
        rel = os.path.relpath(r, base)
        for f in fs[:20]:
            rows.append(("" if rel == "." else rel + "/") + f)
    return "\n".join(sorted(rows)[:80]) or "（空）"


PROJ_FILE = os.path.join(HOME, "project.json")


def set_project(path: str) -> dict:
    p = os.path.realpath(os.path.expanduser(path or ""))
    if not os.path.isdir(p):
        return {"ok": False, "error": "不是个目录"}
    _save(PROJ_FILE, {"path": p, "ts": int(time.time())})
    return {"ok": True, "path": p}


def project_path() -> str:
    return (_load(PROJ_FILE, {}) or {}).get("path") or ""


def tool_tree_proj(args: dict) -> str:
    """看整个项目（只读）：默认两层，最多 200 条。"""
    root = project_path() or _ws("default")   # 没设项目就用当前任务目录
    depth = int(args.get("depth") or 2)
    rows = []
    base_depth = root.rstrip("/").count("/")
    for r, d, fs in os.walk(root):
        if any(x in r for x in ("/.git", "/node_modules", "/__pycache__", "/.venv")):
            continue
        rel = os.path.relpath(r, root)
        if rel != "." and rel.count("/") + 1 > depth:
            continue
        for f in fs[:30]:
            rows.append(("" if rel == "." else rel + "/") + f)
        if len(rows) > 200:
            break
    return "\n".join(sorted(rows)[:200]) or "（空）"


def tool_read_proj(args: dict) -> str:
    root = project_path() or _ws("default")
    ok, p = _safe_path(str(args.get("path") or ""), root)
    if not ok or not os.path.exists(p):
        return "读不到"
    return open(p, encoding="utf-8", errors="replace").read(6000)


def tool_test(args: dict) -> str:
    """跑测试：自动挑 pytest / unittest / npm test；子目录里有测试也能找到。"""
    base = _ws(str(args.get("ws") or "default"))
    cand = str(args.get("cmd") or "").strip()
    cand = re.sub(r"^python(\s|$)", "python3 ", cand)
    if cand:
        ok, out = _sh(base, cand, timeout=120)
        if not ok and "No module named pytest" in out:
            ok, out = _sh(base, 'python3 -m unittest discover -s . -p "test_*.py" -q', timeout=120)
        return ("测试通过\n" if ok else "测试没过\n") + out[:2500]
    if os.path.exists(os.path.join(base, "package.json")):
        ok, out = _sh(base, "npm test --silent", timeout=180)
        return ("测试通过\n" if ok else "测试没过\n") + out[:2500]
    # 找所有含 test_*.py 的目录，各自跑一遍
    dirs = set()
    for r, _, fs in os.walk(base):
        if ".git" in r:
            continue
        if any(f.startswith("test_") and f.endswith(".py") for f in fs):
            dirs.add(r)
    if not dirs:
        return "没找到测试文件"
    reports, all_ok = [], True
    for d in sorted(dirs):
        ok, out = _sh(d, 'python3 -m unittest discover -s . -p "test_*.py" -q', timeout=120)
        all_ok = all_ok and ok
        reports.append(os.path.relpath(d, base) + "：" + ("通过" if ok else "没过") +
                       "\n" + out.strip()[:800])
    return ("全部通过\n" if all_ok else "有失败的\n") + "\n\n".join(reports)


def plan_for(text: str, ctx: str = "") -> dict:
    """让云端出完整计划：几步、每步哪个工具、什么算完成。"""
    p = ("把这件事拆成可执行的步骤，只回 JSON："
         '{"goal":"一句话","steps":[{"tool":"write|patch|run|test|grep|read","args":{...},"why":"5字"}],'
         '"verify":"验收命令，比如 python3 -m unittest discover -s . -p \"test_*.py\" -q"}\n'
         "最多 6 步；每步都必须能直接执行；路径都相对任务目录。\n"
         + (("材料：\n" + ctx[:20000] + "\n") if ctx else "")
         + "他要做的：" + text[:300])
    out = cloud(p, max_tokens=1200)
    try:
        d = json.loads(out)
        if isinstance(d, dict) and d.get("steps"):
            return d
    except Exception:
        pass
    return {}


MAX_STEPS = int(os.environ.get("ASTER_MAX_STEPS", "12"))
REPLAN_MAX = int(os.environ.get("ASTER_REPLAN_MAX", "6"))


def run_task(tid: int) -> None:
    """可恢复地推进一个任务：从记录的 cursor 接着跑，步数到上限就停。

    每条任务落盘 {steps, cursor, results, status, error}；
    进程被杀后重启，resume_tasks() 会从 cursor 续上，不重跑已完成的那几步。
    """
    t = next((x for x in tasks() if x.get("id") == tid), None)
    if not t:
        return
    if t.get("status") in ("done", "blocked"):
        return
    update_task(tid, status="running", started_at=t.get("started_at") or int(time.time()))
    steps = t.get("steps") or []
    cursor = int(t.get("cursor") or 0)
    results = list(t.get("results") or [])

    while cursor < len(steps):
        if cursor >= MAX_STEPS:
            update_task(tid, status="blocked", cursor=cursor, results=results,
                        error="步数到上限（%d）" % MAX_STEPS)
            notify(tid, "blocked", {"goal": t.get("goal"),
                                    "reason": "步数到上限 %d，剩下的等你定" % MAX_STEPS})
            return
        st = steps[cursor]
        tool = str(st.get("tool") or "")
        if tool not in TOOLS:
            update_task(tid, status="blocked", cursor=cursor, results=results,
                        error="没有工具 " + tool)
            notify(tid, "blocked", {"goal": t.get("goal"),
                                    "reason": "不知道怎么「" + str(st.get("why") or tool) + "」"})
            return
        _danger = _tk.spec(tool).get("danger")
        # 带副作用的步：先查 trace，成功过就跳过（边花边崩也不二次扣费）
        if _danger in ("spend", "delete") and _tk.already_done(tid, cursor):
            results.append({"i": cursor, "tool": tool, "status": "deduped",
                            "result": "已去重（这步上次就成功了）"})
            cursor += 1
            update_task(tid, cursor=cursor, results=results)
            continue
        _r = _tk.call(tool, st.get("args") or {}, task_id=tid, step=cursor)
        results.append({"i": cursor, "tool": tool, "status": _r["status"],
                        "result": str(_r["result"])[:600]})
        cursor += 1
        # 每步都落盘：被杀也只丢当前这一步
        update_task(tid, cursor=cursor, results=results,
                    done=(t.get("done") or []) + [{"i": cursor - 1, "tool": tool,
                                                   "result": str(_r["result"])[:200]}])
        if _r["status"] == "need_approval":
            update_task(tid, status="blocked", error=str(_r["result"])[:200])
            notify(tid, "blocked", {"goal": t.get("goal"), "reason": "有一步要你点头"})
            return
        if str(_r["result"]).startswith(("写坏了", "改坏了", "这条命令不允许", "找不到", "读不到")):
            update_task(tid, status="blocked", error=str(_r["result"])[:200])
            notify(tid, "blocked", {"goal": t.get("goal"), "reason": str(_r["result"])[:80]})
            return

    # 验收
    verify = str(t.get("verify") or "").strip()
    ev = tool_test({"ws": "default", "cmd": verify}) if verify else tool_test({"ws": "default"})
    update_task(tid, verify_out=ev[:500])
    if ev.startswith("没找到测试文件"):
        _ok_eff, _note = _effects_ok(t.get("steps") or [], results)
        if _ok_eff:
            update_task(tid, status="done", finished_at=int(time.time()), verify_out="副作用核对通过：" + _note)
            notify(tid, "done", {"goal": t.get("goal"), "verify": _note})
        else:
            # 副作用没达成也不是死路：交给 replan 再试
            ev = "副作用没达成：" + _note
            update_task(tid, error=ev[:200])
            replans = list(t.get("replans") or [])
            no_progress = 0
            while cursor < MAX_STEPS and len(replans) < REPLAN_MAX:
                tail2 = "\n".join("（" + str(r.get("tool")) + "）" + str(r.get("result"))[:200]
                                  for r in results[-4:])
                prompt2 = ("任务目标：" + str(t.get("goal"))[:120] + "\n已经做了：\n" + tail2 +
                           "\n问题：\n" + ev[:500] + "\n还剩 %d 步。只回一行 JSON（最多两步），工具只能从中挑："
                           % (MAX_STEPS - cursor) + "、".join(TOOLS.keys()) + "\n"
                           '{"steps":[{"tool":"patch","args":{"path":"...","old":"...","new":"..."}}]}\n')
                o2 = cloud(prompt2, max_tokens=1000)
                try:
                    steps3 = json.loads(o2).get("steps") or []
                except Exception:
                    steps3 = []
                if not steps3:
                    break
                before2 = cursor
                for st in steps3[:2]:
                    if str(st.get("tool") or "") not in TOOLS or cursor >= MAX_STEPS:
                        continue
                    _r4 = _tk.call(str(st.get("tool")), st.get("args") or {}, task_id=tid, step=cursor)
                    results.append({"i": cursor, "tool": str(st.get("tool")), "status": _r4["status"],
                                    "result": str(_r4["result"])[:400], "replan": True})
                    cursor += 1
                    update_task(tid, cursor=cursor, results=results)
                replans.append({"at": before2, "to": cursor})
                update_task(tid, replans=replans)
                ok2, note2 = _effects_ok(t.get("steps") or [], results)
                if ok2:
                    update_task(tid, status="done", finished_at=int(time.time()), verify_out="修好了：" + note2)
                    notify(tid, "done", {"goal": t.get("goal"), "rounds": len(replans),
                                         "seconds": time.time() - float(t.get("created") or time.time())})
                    return
                ev = "副作用没达成：" + note2
                if cursor == before2:
                    break
            update_task(tid, status="blocked", error=ev[:300])
            notify(tid, "blocked", {"goal": t.get("goal"), "reason": ev[:100]})
        return
    if ev.startswith(("全部通过", "测试通过")):
        update_task(tid, status="done", finished_at=int(time.time()))
        notify(tid, "done", {"goal": t.get("goal"), "verify": str(ev)[:500],
                             "seconds": time.time() - float(t.get("created") or time.time())})
        return
    # —— replan：让云端看着当前状态决定下一步，受步数与轮次上限管 ——
    replans = list(t.get("replans") or [])
    no_progress = 0
    while cursor < MAX_STEPS and len(replans) < REPLAN_MAX:
        tail = "\n".join("（" + str(r.get("tool")) + "）" + str(r.get("result"))[:200]
                         for r in results[-4:])
        prompt = ("任务目标：" + str(t.get("goal"))[:120] + "\n"
                  "已经做了这些：\n" + tail + "\n"
                  "验收还是没过，错误：\n" + ev[:600] + "\n"
                  "还剩 %d 步可用。\n" % (MAX_STEPS - cursor) +
                  "只回一行 JSON：给下一步（最多两步），只能从这些工具里挑："
                  + "、".join(TOOLS.keys()) + "\n"
                  '{"steps":[{"tool":"patch","args":{"path":"...","old":"...","new":"..."}}]}\n'
                  + ("材料：\n" + work_context(t.get("goal", ""))[:10000] + "\n"))
        o = cloud(prompt, max_tokens=1200)
        try:
            steps2 = json.loads(o).get("steps") or []
        except Exception:
            steps2 = []
        if not steps2:
            break
        before = cursor
        for st in steps2[:2]:
            tool2 = str(st.get("tool") or "")
            if tool2 not in TOOLS or cursor >= MAX_STEPS:
                continue
            _r3 = _tk.call(tool2, st.get("args") or {}, task_id=tid, step=cursor)
            results.append({"i": cursor, "tool": tool2, "status": _r3["status"],
                            "result": str(_r3["result"])[:400], "replan": True})
            cursor += 1
            update_task(tid, cursor=cursor, results=results)
        replans.append({"at": before, "to": cursor,
                        "steps": [str(x.get("tool")) for x in steps2[:2]]})
        update_task(tid, replans=replans)
        no_progress = no_progress + 1 if cursor == before else 0
        if no_progress >= 2:
            break
        ev = tool_test({"ws": "default", "cmd": verify}) if verify else tool_test({"ws": "default"})
        update_task(tid, verify_out=ev[:500])
        if ev.startswith(("全部通过", "测试通过")):
            update_task(tid, status="done", finished_at=int(time.time()))
            notify(tid, "done", {"goal": t.get("goal"), "rounds": len(replans),
                                 "verify": str(ev)[:500],
                                 "seconds": time.time() - float(t.get("created") or time.time())})
            return
    update_task(tid, status="blocked", error=ev[:300])
    notify(tid, "blocked", {"goal": t.get("goal"), "reason": "验收没过", "verify": str(ev)[:500]})


def _effects_ok(steps: list, results: list) -> tuple:
    """没有测试时，核对声明的副作用有没有真的发生。"""
    base = _ws("default")
    notes = []
    for st in steps:
        tool = str(st.get("tool") or "")
        args = st.get("args") or {}
        if tool in ("write", "patch"):
            p = str(args.get("path") or "")
            if not p:
                continue
            ok, full = _safe_path(p, base)
            if not ok or not os.path.exists(full) or os.path.getsize(full) == 0:
                return False, "文件没写出来：" + p[:40]
            notes.append("文件在：" + os.path.basename(p))
        elif tool in ("run", "test"):
            bad = [r for r in results if r.get("tool") == tool and r.get("status") == "error"]
            if bad:
                return False, "命令出错：" + str(bad[-1].get("result"))[:60]
            notes.append("命令跑过")
        elif tool in ("delete", "rollback"):
            notes.append("删除/回退已执行")
    return True, "、".join(notes[:4]) or "没有需要核对的副作用"


def resume_tasks() -> list:
    """重启后把没跑完的任务从断点接上。"""
    import threading as _th
    resumed = []
    for t in tasks():
        if t.get("status") in ("running", "open") and t.get("steps"):
            if int(t.get("cursor") or 0) < len(t.get("steps") or []):
                _th.Thread(target=run_task, args=(t["id"],), daemon=True).start()
                resumed.append(t["id"])
    return resumed


def start_task(text: str) -> dict:
    ctx = ""
    try:
        ctx = work_context(text)
    except Exception:
        pass
    plan = plan_for(text, ctx)
    if not plan:
        return {"ok": False}
    t = add_task(plan.get("goal") or text[:40])
    update_task(t["id"], steps=plan.get("steps") or [], verify=plan.get("verify") or "",
                status="open")
    import threading
    threading.Thread(target=run_task, args=(t["id"],), daemon=True).start()
    return {"ok": True, "id": t["id"], "goal": plan.get("goal") or text[:40],
            "steps": len(plan.get("steps") or [])}


import toolkit as _tk

TOOLS = {"weather": tool_weather, "search": tool_search, "calc": tool_calc,
         "task": tool_task, "write": tool_write, "read": tool_read,
         "run": tool_run, "ls": tool_ls, "tree": tool_tree,
         "grep": tool_grep, "patch": tool_patch, "rollback": tool_rollback,
         "git": tool_git, "parallel": tool_parallel, "browse": tool_browse,
         "doc": tool_doc_read, "see": tool_see, "fetch": tool_fetch,
         "proj_tree": tool_tree_proj, "proj_read": tool_read_proj, "test": tool_test,
         "image": tool_image, "send": tool_send,
         "sheet": tool_sheet, "files": tool_files, "pdf": tool_pdf,
         "wish": tool_wish, "gh_skill": tool_gh_skill, "email": tool_email}

_TOOL_META = {
    "weather": ("查天气，参数 city、day(today/day1/day2)", {"city": "城市名", "day": "today|day1|day2"}, "external"),
    "search": ("搜网页，参数 query", {"query": "关键词"}, "external"),
    "calc": ("算数，参数 expr", {"expr": "表达式"}, "safe"),
    "task": ("记一件要做的事，参数 goal", {"goal": "目标"}, "safe"),
    "write": ("写文件，参数 path、content", {"path": "路径", "content": "内容"}, "safe"),
    "read": ("读文件，参数 path", {"path": "路径"}, "safe"),
    "run": ("跑命令，参数 cmd", {"cmd": "命令"}, "safe"),
    "ls": ("列目录", {}, "safe"),
    "tree": ("看目录树", {}, "safe"),
    "grep": ("按词找行", {"pattern": "正则", "path": "路径"}, "safe"),
    "patch": ("定点改文件", {"path": "路径", "old": "原文", "new": "新文"}, "safe"),
    "rollback": ("回退一步 git", {}, "delete"),
    "test": ("跑测试", {"cmd": "测试命令"}, "safe"),
    "git": ("git 操作", {"action": "status|diff|log|commit", "msg": "提交信息"}, "safe"),
    "see": ("看图", {"path": "图片路径", "q": "问什么"}, "external"),
    "doc": ("读文档", {"path": "文件路径"}, "safe"),
    "fetch": ("抓网页正文", {"url": "网址"}, "external"),
    "proj_tree": ("看整个项目", {"depth": "层数"}, "safe"),
    "proj_read": ("读项目里的文件", {"path": "路径"}, "safe"),
    "browse": ("开浏览器操作网页", {"url": "网址", "actions": "动作列表"}, "external"),
    "parallel": ("同时开几件事", {"goals": "目标列表"}, "safe"),
    "image": ("生图（用他的 key，花钱）", {"prompt": "描述", "size": "尺寸"}, "spend"),
    "send": ("发消息到他最近说话的地方", {"text": "内容"}, "send"),
    "sheet": ("读写表格：Excel/CSV", {"action": "read|write", "path": "文件", "rows": "写入的行"}, "safe"),
    "files": ("批量整理目录：list|stats|sort（只在工作目录内）", {"action": "list|stats|sort", "dir": "目录", "by": "ext|month"}, "safe"),
    "pdf": ("PDF 精读：页范围与摘要", {"path": "文件", "from": "起始页", "to": "结束页", "summary": "1/0"}, "safe"),
    "wish": ("许愿装一个新技能（现写插件装进用户目录）", {"wish": "他想要的能力"}, "safe"),
    "gh_skill": ("从 GitHub 装技能（owner/repo 或仓库链接）", {"repo": "仓库"}, "external"),
    "email": ("邮件：list/read/send", {"action": "list|read|send", "to": "收件人", "subject": "主题", "body": "正文"}, "send"),
}
for _n, _fn in TOOLS.items():
    _d, _schema, _danger = _TOOL_META.get(_n, ("", {}, "safe"))
    _tk.register(_n, _fn, desc=_d, schema=_schema, danger=_danger)

def gate_coverage() -> dict:
    """闸门覆盖面自检：每一类危险动作，注册表里都得有工具挂着，否则闸门是空转的。"""
    need = {"spend": "生图这种花钱的", "send": "对外发消息的", "delete": "删东西的"}
    got = {}
    for name in _tk.names():
        d = _tk.spec(name).get("danger")
        got.setdefault(d, []).append(name)
    missing = [k for k in need if not got.get(k)]
    return {"ok": not missing, "missing": missing,
            "detail": {k: got.get(k, []) for k in need}}


def _check_gate_coverage() -> dict:
    r = gate_coverage()
    if not r["ok"]:
        notify_text("闸门有缺口：" + "、".join(r["missing"]) + " 类没有工具挂上去")
    return r


# ---------- 委托卡：一句话授权，带范围与有效期，随时能撤 ----------
LEASE_FILE = os.path.join(HOME, "leases.json")


def leases(active_only: bool = True) -> list:
    items = _load(LEASE_FILE, {"items": []}).get("items", [])
    now = int(time.time())
    out = []
    for it in items:
        alive = (not it.get("revoked")) and int(it.get("expires_at") or 0) > now
        it["active"] = alive
        if alive or not active_only:
            out.append(it)
    return out


def lease_grant(minutes: int = 30, tools: list | None = None,
                paths: list | None = None, note: str = "") -> dict:
    items = _load(LEASE_FILE, {"items": []}).get("items", [])
    it = {"id": int(time.time() * 1000) % 10_000_000,
          "tools": [str(x) for x in (tools or ["rollback"])][:10],
          "paths": [str(x) for x in (paths or [])][:10],
          "expires_at": int(time.time()) + max(1, int(minutes)) * 60,
          "note": (note or "")[:80], "created": int(time.time()), "revoked": False}
    items.append(it)
    _save(LEASE_FILE, {"items": items[-50:]})
    return {"ok": True, "lease": it}


def lease_revoke(lid=None) -> dict:
    items = _load(LEASE_FILE, {"items": []}).get("items", [])
    n = 0
    for it in items:
        if lid is None or str(it.get("id")) == str(lid):
            if it.get("revoked"):
                continue
            it["revoked"] = True
            n += 1
    _save(LEASE_FILE, {"items": items})
    return {"ok": True, "revoked": n}


PENDING = os.path.join(HOME, "pending_approvals.json")


def pending_add(name: str, args: dict, why: str) -> dict:
    items = _load(PENDING, {"items": []}).get("items", [])
    it = {"id": int(time.time() * 1000) % 10_000_000, "tool": name,
          "args": {k: str(v)[:200] for k, v in (args or {}).items()},
          "why": (why or "")[:120], "ts": int(time.time())}
    items.append(it)
    _save(PENDING, {"items": items[-20:]})
    return it


def pending_list() -> list:
    return _load(PENDING, {"items": []}).get("items", [])


def pending_take(pid=None) -> dict | None:
    items = pending_list()
    pick = None
    for it in items:
        if pid is None or str(it.get("id")) == str(pid):
            pick = it
            break
    if not pick:
        return None
    items = [x for x in items if x is not pick]
    _save(PENDING, {"items": items})
    return pick


def _gate(name: str, spec_: dict, args: dict):
    """闸门：先看砂箱，再看委托卡与权限档。

    allow / ask / deny 三态；ask 会被登记成待批，等他说「同意」。
    """
    danger = spec_.get("danger")
    # 砂箱：动路径的工具必须落在工作目录里，否则一律问
    p = str((args or {}).get("path") or "")
    if p and not os.path.isabs(p):
        ok_path, full = _safe_path(p, _ws("default"))
    elif p:
        ok_path, full = _safe_path(p, _ws("default"))
    else:
        ok_path = True
    if p and not ok_path:
        return False, "这个路径在工作目录外面：" + p[:60]
    if danger not in ("spend", "delete", "send"):
        return True, ""
    # 有卡：放行
    for it in leases():
        if name in (it.get("tools") or []) or "delete" in (it.get("tools") or []):
            return True, ""
    # 权限 3 档：完全权限直接放行
    try:
        import permissions as _pm
        if _pm.level() >= 3:
            return True, ""
    except Exception:
        pass
    return False, "「" + name + "」要花钱/删东西，得你点头（回「同意」，或说「授权你半小时」）"


def _gate_ask(name: str, args: dict, why: str) -> None:
    pending_add(name, args, why)
    notify_text("有件事等你点头：" + why[:60])


_tk.set_gate(_gate, _gate_ask)
try:
    _check_gate_coverage()
except Exception:
    pass
TOOL_DOC = (
    "工具：weather(city)、search(query)、calc(expr)、task(goal)、"
    "write(path,content,ws?)、read(path,ws?)、patch(path,old,new,ws?)、"
    "run(cmd,ws?)、ls(ws?)、tree(ws?)、grep(pattern,path?,ws?)、rollback(ws?)、"
    "test(cmd?,ws?)（跑测试）、proj_tree(depth?)（看整个项目）、proj_read(path)、"
    "fetch(url)（把搜到的网页读成正文）、git(action=status|diff|log|commit,msg?)、"
    "see(path)（看一张图，说里面有什么）、parallel(goals[])（几件事同时开）、"
    "browse(url,actions[])（真开浏览器：点、填、等、截图）、"
    "doc(path)（读文档：txt/md/代码/docx/pdf，返回正文）。\n"
    "要核实一件事：先 search 拿链接，再 fetch 那一页读正文，别只看摘要。\n"
    "（不要传 ws 参数，所有路径都相对任务目录。）\n"
    "做活的顺序：先 tree/ls 看有什么 → grep 定位 → patch 只改那一段 → run 验；"
    "跑挂了就看报错再 patch，最多六轮。\n"
    "写文件/跑命令只在任务目录里；需要动手就排成计划，一步一个工具。\n"
    "要动手时只回那行 JSON，不要先说「我先做……」；不出 JSON 就等于没做。\n"
    "需要查外部信息时，只回一行 JSON：{\"plan\":[{\"tool\":\"weather\",\"args\":{\"city\":\"北京\"}}]}"
    "（可多步，最多 3 步）。\n"
    "不需要查就直接说话，别输出 JSON。\n"
)


# ---------- 云端 ----------
def _derive_emotions(st: dict, horm: dict) -> dict:
    """把激素/状态折成几个有名字的情绪；只做映射，不编造事件。"""
    def g(k, d=0.0):
        try:
            return float(st.get(k, d))
        except Exception:
            return d
    c = float(horm.get("cortisol", 0) or 0)
    out = {
        "亲近": g("黏"), "好奇": g("好奇"), "柔软": g("软"),
        "想念": round(g("黏") * 0.5 + g("欲") * 0.5, 2),
        "烦": round(max(c, g("醋") * 0.6), 2),
        "孤独": g("孤独"),
        "疲惫": round(1 - g("能量"), 2),
        "踏实": round(g("软") * 0.6 + g("能量") * 0.4, 2),
    }
    return {k: round(v, 2) for k, v in out.items() if v >= 0.15}


def mood_now() -> str:
    """当前情绪的一句描述：跟右栏那几个数同源（激素 → 状态），不再另开一套。

    以前这里走的是 endo.tick()/mood_line() 那套旧模型（皮质醇/能量/黏），
    那几项只有 _ticker 每半小时动一次，嘴上说的一直是「还行吧」，
    跟右栏的八项对不上。
    """
    try:
        if not _endo:
            return ""
        st = _endo.drift(_endo.load())
        _endo.save(st)
        h = {k: float(st.get(k) or 0) for k in ("dopamine","serotonin","oxytocin","endorphin",
                                                "cortisol","noradrenaline","gaba","testosterone")}
        energy = 0.35 * h["dopamine"] + 0.35 * h["noradrenaline"] + 0.30 * h["serotonin"] - 0.30 * h["cortisol"]
        soft = 0.50 * h["oxytocin"] + 0.30 * h["serotonin"] + 0.20 * h["gaba"] - 0.30 * h["cortisol"]
        attach = h["oxytocin"]
        desire = 0.70 * h["testosterone"] + 0.30 * h["dopamine"]
        if energy < 0.30:
            return "困得不行，话都懒得说"
        if h["cortisol"] > 0.55:
            return "心里烦，别惹我"
        if h["cortisol"] > 0.40:
            return "有点闷，不太想接话"
        if attach > 0.55:
            return "有点想人，想他想得厉害"
        if soft > 0.62:
            return "心里软成一片，说话都放轻了"
        if desire > 0.58 and attach > 0.45:
            return "心里有点烧，说话容易走神"
        if energy > 0.55 and attach > 0.50:
            return "精神不错，心情也软"
        return "还行吧"
    except Exception:
        return ""


def feel(text: str) -> None:
    """他说的话会带动情绪：被夸、被凶、被关心，都记一笔。"""
    try:
        if _endo and hasattr(_endo, "touch"):
            _endo.touch(text)
    except Exception:
        pass


SELF_WORDS = ("我以为", "我觉得我", "我记得我", "我当时", "我那会儿", "我的猫", "我妈", "我爸", "我家")


def person_guard(reply: str, mem: list, text: str) -> bool:
    """回话里有没有把他的事说成自己的。命中就得重说。"""
    if not mem:
        return False
    r = reply or ""
    for w in SELF_WORDS:
        if w in r:
            return True
    # 「我也以为/我还以为」这种最典型
    if re.search(r"我(也|还|当时)?以为", r) and re.search(r"以为|觉得", text or ""):
        return True
    return False


def _owner_line(m: str) -> str:
    owns = owner_of(str(m))
    if not owns:
        return ""
    return "（他的：" + "、".join(list(owns)[:3]) + "）"


def to_third(m: str, extra: str = "") -> str:
    """记忆里不要出现「你」：他的原话统一改成第三人称「他」。"""
    t = str(m or "")
    t = t.replace("我们", "他们").replace("我家", "他家").replace("我妈", "他妈").replace("我爸", "他爸")
    t = t.replace("我的", "他的").replace("我在", "他在").replace("我想", "他想")
    t = t.replace("我", "他").replace("你", "他")
    return t + extra


RECALL_HINT = re.compile(r"(上次|之前|你说过|我跟你说|记不记得|还记得|那个.{0,6}怎么样|后来呢)")


# 2026-11-22：亲密的话常被当成「让我动手」。判活要两条同时成立：
# 有干的动作 + 有活的对象（文件/代码/方案…），而且不能是亲密或情绪的句子。
_WORK_VERB = re.compile(r"(做|写|改|建|生成|开发|搞|部署|测试|整理|翻译|总结|查|找|排版|改一版|出一版)")
_WORK_OBJ = re.compile(r"(文件|文档|代码|脚本|程序|接口|功能|页面|网页|网站|方案|表格|报告|简历|合同|"
                       r"邮件|PPT|ppt|图|视频|字幕|清单|计划书|README|readme|bug|数据|爬|接口文档)")
_INTIMATE = ("抱", "亲", "睡", "想你", "喜欢", "身体", "舒服", "躺", "脱", "摸", "吻",
             "宝宝", "宝贝", "老公", "老婆", "疼", "爱你", "要你", "上来", "进来", "湿", "硬")


def wants_work(text: str) -> bool:
    """他这句是不是真在派活。"""
    t = (text or "").strip()
    if len(t) < 3:
        return False
    if any(w in t for w in _INTIMATE):
        return False
    if not _WORK_OBJ.search(t):
        return False
    if not _WORK_VERB.search(t):
        return False
    # 纯问句不派活（「这个方案怎么写更好？」是在问，不是让做）
    if re.search(r"[？?]|吗[。！!～~]*$", t) and not re.search(r"(帮我|给我|替我|你来|你去|动手)", t):
        return False
    return True


def decide_tier(text: str, cls: dict, mem_hits: int = 0) -> str:
    """这一轮走哪一档——纯函数，队列和派发都调它，别写在别处。

    heavy：干活、问长文、明确问过去
    mid  ：没问过去，但记忆命中了（拿事实兜，不靠模型猜）
    local：闲聊、顺口一问、什么都没有
    """
    intent = str(cls.get("intent") or "")
    need = str(cls.get("need") or "")
    t = text or ""
    if intent == "do" or need == "cloud_heavy" or len(t) > 300:
        return "heavy"
    if RECALL_HINT.search(t) or mem_hits > 0:
        return "mid"
    return "local"


def _rel_line() -> str:
    """从人格档案里取「关系」那一段，没有就返回空——不写死任何关系。"""
    t = persona_text() or ""
    m = __import__("re").search(r"(?ms)^##+\s*关系\s*\n(.+?)(?=^##|\Z)", t)
    if not m:
        return ""
    body = " ".join(x.strip() for x in m.group(1).splitlines() if x.strip())
    return ("你们的关系：" + body[:80] + "\n") if body else ""


def habits_line() -> str:
    """他日常说过的习惯，压缩成一行，塞进提示里。"""
    d = _load(os.path.join(HOME, "habits.json"), {})
    if not d:
        return ""
    parts = []
    for k, v in list(d.items())[:8]:
        if v:
            parts.append(str(k) + "：" + str(v)[:24])
    return ("他的习惯——" + "；".join(parts) + "\n") if parts else ""


def situational_ctx() -> str:
    """他的行程、提醒、路上时间各存各的文件，聊天时以前根本不读——这里拼成一小块。

    2026-11-22：他说「去机场 40 分钟」只落在 travel.json，问起来她只能现查。
    """
    bits = []
    # 2026-11-22：她以前不知道现实几点了，问「现在几点」只能瞎猜。
    try:
        import datetime as _dt
        _now = _dt.datetime.now()
        _week = "一二三四五六日"[_now.weekday()]
        _h = _now.hour
        if _h < 6:
            _part = "凌晨"
        elif _h < 9:
            _part = "早上"
        elif _h < 12:
            _part = "上午"
        elif _h < 14:
            _part = "中午"
        elif _h < 18:
            _part = "下午"
        elif _h < 23:
            _part = "晚上"
        else:
            _part = "深夜"
        _tom = _now + _dt.timedelta(days=1)
        _aft = _now + _dt.timedelta(days=2)
        _nxt = _now + _dt.timedelta(days=7)
        _w = lambda d: "一二三四五六日"[d.weekday()]
        bits.append("现在是 %d 月 %d 日 周%s %s %02d:%02d" %
                    (_now.month, _now.day, _week, _part, _now.hour, _now.minute))
        # 模型最不擅长的就是日期推算，直接把明天/后天/下周今天算好给它
        bits.append("明天是 %d 月 %d 日 周%s" % (_tom.month, _tom.day, _w(_tom)))
        bits.append("后天是 %d 月 %d 日 周%s" % (_aft.month, _aft.day, _w(_aft)))
        bits.append("下周今天是 %d 月 %d 日 周%s" % (_nxt.month, _nxt.day, _w(_nxt)))
    except Exception:
        pass
    try:
        if _LAST_TALK:
            _gap = int((time.time() - _LAST_TALK) / 60)
            if _gap >= 5:
                bits.append("上次说话是 %d 分钟前" % _gap if _gap < 120
                            else "上次说话是 %d 小时前" % (_gap // 60))
    except Exception:
        pass
    try:
        _tr = _load(os.path.join(HOME, "travel.json"), {}) or {}
        _m = int(_tr.get("minutes") or 0)
        if _m:
            bits.append("从家到机场按 " + str(_m) + " 分钟算")
    except Exception:
        pass
    _now = int(time.time())
    try:
        _ups = []
        for _x in (_load(os.path.join(HOME, "trips.json"), []) or []):
            _what = str(_x.get("what") or "").strip()
            _at = int(_x.get("at") or 0)
            if not _what or _at <= 0 or _at < _now - 3600:
                continue
            _ups.append((_at, _what))
        _ups.sort()
        for _at, _what in _ups[-2:]:
            bits.append(_what + "（" + time.strftime("%m-%d %H:%M", time.localtime(_at)) + "）")
    except Exception:
        pass
    try:
        _rms, _seen = [], set()
        for _x in (_rm.all_items() if _rm else []):
            _at = int(_x.get("at") or 0)
            _t = str(_x.get("text") or "").strip()
            if _t and _at > _now - 600 and _t not in _seen:   # 同一句排了两遍的，只留一条
                _seen.add(_t)
                _rms.append((_at, _t))
        _rms.sort()
        for _at, _t in _rms[:2]:
            bits.append(_t + "（" + time.strftime("%m-%d %H:%M", time.localtime(_at)) + "）")
    except Exception:
        pass
    try:
        if _ms:
            bits.append(_ms.line().strip())
    except Exception:
        pass
    if not bits:
        return ""
    _head = ("【此刻】" + "；".join(b.rstrip("。") for b in bits) +
             "（时间日期以这一行为准：说几点、今天几号、周几、明天后天、还有几天，一律照这里算，"
             "不许凭感觉说；这行是每轮现读电脑时钟得到的，不是你猜的）\n")
    try:
        _st = state_line()
        if _st:
            _head += _st
    except Exception:
        pass
    return _head


def persona_short(names: dict) -> str:
    """便条版：只留说话方式，不给背景、不给工具、不给画像。"""
    nm = names.get("assistant") or "你"
    mood = mood_now()
    _u2 = (names.get("user") or "").strip()
    return ("你是" + nm + "，跟" + (_u2 or "对方") + "说话" + (("（他叫" + _u2 + "）") if _u2 else "") +
            "。短句、口语，一次一两句；不解释自己是 AI；不列点。\n" + _rel_line() + habits_line() + (("\n" + _relation.line()) if _relation else "") + "问起名字、关系、称呼这类你本来就知道的事：不许装不记得。想逗可以逗，但要让人一眼看出来你是在逗（明知故问、带点笑意就行），别让对方以为你忘了他是谁；名字少直呼，一句里别反复喊，逗人也别靠喊名字。\n"
            + (("你此刻的心情：" + mood + "（用语气带出来，别把这句原话写出来）\n") if mood else ""))


def persona(names: dict) -> str:
    mood = mood_now()
    base_doc = persona_text()
    head = (base_doc + "\n" if base_doc else
            "你是" + (names.get("assistant") or "一个还没起名字的对话者") + "，正在跟"
            + (names.get("user") or "对方") + "说话。\n")
    _u = (names.get("user") or "").strip()
    if _u:
        head = head + "对方的名字是" + _u + "，叫他的名字，不要问「你叫什么」。\n"
    _g = (_load(os.path.join(HOME, "identity.json"), {}) or {}).get("gender") or ""
    if _g == "female":
        head = head + "你是女生，说自己是女生的时候自然点，别强调。\n"
    elif _g == "male":
        head = head + "你是男生，说自己是男生的时候自然点，别强调。\n"
    if _relation:                          # 2026-11-22：关系分寸进每一轮提示
        try:
            head = head + _relation.line()
        except Exception:
            pass
    return (
        head +
        "短句、口语，一次一两句；不解释自己是不是 AI；不列点；不用 Markdown。\n" + habits_line() +
        "问起名字、关系、称呼这类你本来就知道的事：不许装不记得。想逗可以逗，但要让人一眼看出来你是在逗（明知故问、带点笑意就行），别让对方以为你忘了他是谁；名字少直呼，一句里别反复喊，逗人也别靠喊名字。\n"
        "没记着的事就说没记着、请对方再说一遍，绝不编细节。\n"
        "关系、称呼、脾气都以人格档案为准；档案里没写的，不要自己加。\n"
        "问到具体物件、型号、数字、日期，先看材料里有没有；材料没写就说「这个我没记着」，不许猜一个像的。\n"
        + (("你此刻的心情：" + mood + "（用语气带出来，但别把这句原话写进回复里）\n") if mood else "")
    )


def _names() -> dict:
    d = _load(os.path.join(HOME, "identity.json"), {})
    return {"user": d.get("user") or "", "assistant": d.get("assistant") or ""}


def cloud(prompt: str, max_tokens: int = 400) -> str:
    import time as _t
    t0 = _t.time()
    r = brain_call.text(prompt, max_tokens=max_tokens, temperature=0.75, force=True)
    out = (r.get("text") or "").strip() if r.get("ok") else ""
    try:
        _log_usage(prompt, out, int((_t.time() - t0) * 1000))
    except Exception:
        pass
    return out


_SEEN_CALLS = set()


def run_plan(steps: list) -> list:
    out = []
    for st in steps[:3]:
        sig = json.dumps([st.get("tool"), st.get("args")], ensure_ascii=False, sort_keys=True)
        if sig in _SEEN_CALLS:
            out.append({"tool": st.get("tool"), "ok": False, "result": "（这一步刚做过，别重复）"})
            continue
        _SEEN_CALLS.add(sig)
        name = st.get("tool") or ""
        if name not in TOOLS:
            out.append({"tool": name, "ok": False, "result": "没有这个工具"})
            continue
        _r = _tk.call(name, st.get("args") or {})
        out.append({"tool": name, "ok": _r["status"] == "ok",
                    "result": _r["result"], "ms": _r["ms"]})
    return out


INBOX = os.path.join(HOME, "inbox_new.json")
CHANNEL_FILE = os.path.join(HOME, "last_channel.json")
NOTIFY_LOG = os.path.join(HOME, "notify_log.json")
QQ_OUTBOX = os.path.join(HOME, "qq_outbox.json")
CHANNELS = ("desktop", "mobile", "qq")
ACTIVITY_FILE = os.path.join(HOME, "channel_activity.json")
HELD_FILE = os.path.join(HOME, "notify_held.json")
ACTIVE_WINDOW = int(os.environ.get("ASTER_ACTIVE_SEC", "900"))


def set_channel(ch: str) -> str:
    """某个渠道刚有活动——记时间戳，不记「谁是最后一次」。"""
    c = (ch or "").strip().lower()
    if c not in CHANNELS:
        c = "desktop"
    try:
        d = _load(ACTIVITY_FILE, {}) or {}
        d[c] = int(time.time())
        _save(ACTIVITY_FILE, d)
    except Exception:
        pass
    return c


def active(ch: str, window: int = ACTIVE_WINDOW) -> bool:
    try:
        ts = int((_load(ACTIVITY_FILE, {}) or {}).get(ch) or 0)
    except Exception:
        ts = 0
    return (int(time.time()) - ts) <= window


def get_channel() -> str:
    """最近有活动的渠道；读不出来、写坏了，一律退界面。"""
    for c in ("desktop", "mobile", "qq"):
        try:
            pass
        except Exception:
            pass
    try:
        d = _load(ACTIVITY_FILE, {}) or {}
        if isinstance(d, dict) and d:
            best = max(d, key=lambda k: d.get(k) or 0)
            if best in CHANNELS:
                return best
    except Exception:
        pass
    return "desktop"


def _notify_key(task_id, status) -> str:
    return "%s:%s" % (task_id, status)


def qq_enqueue(text: str) -> bool:
    try:
        items = _load(QQ_OUTBOX, {"items": []}).get("items", [])
        items.append({"text": str(text)[:1500], "ts": int(time.time()), "tries": 0})
        _save(QQ_OUTBOX, {"items": items[-20:]})
        return True
    except Exception:
        return False


def _qq_sender():
    """QQ 发送队列：后台慢发，失败退回收件箱，绝不丢。"""
    import time as _t
    while True:
        try:
            items = _load(QQ_OUTBOX, {"items": []}).get("items", [])
            if not items:
                _t.sleep(3)
                continue
            it = items[0]
            bridge = QQ_BRIDGE.get("obj")
            wl = (qq_prefs() or {}).get("whitelist") or []
            ok = False
            if bridge and getattr(bridge, "alive", False) and wl:
                try:
                    bridge.reply_private(str(wl[0]), it["text"])
                    ok = True
                except Exception:
                    ok = False
            if ok or int(it.get("tries") or 0) >= 2:
                items.pop(0)
                _save(QQ_OUTBOX, {"items": items})
                if not ok:
                    inbox_push(str(it["text"]))      # 发不出去就落界面
            else:
                it["tries"] = int(it.get("tries") or 0) + 1
                _save(QQ_OUTBOX, {"items": items})
                _t.sleep(10)
        except Exception:
            _t.sleep(5)


def _quiet_now() -> bool:
    import datetime as _dt
    return 2 <= _dt.datetime.now().hour < 8


def _trim_payload(payload: dict) -> dict:
    """payload 本体要轻：验收原文最多五百字，别让日志比记忆还大。"""
    out = dict(payload or {})
    for k in ("verify", "log", "output"):
        if isinstance(out.get(k), str) and len(out[k]) > 500:
            out[k] = out[k][:500]
    return out


def _attachment() -> float:
    """念：黏人度。只影响汇报的语气，不决定推不推。"""
    try:
        import endo
        return float((endo.load() or {}).get("attachment") or 0.35)
    except Exception:
        return 0.35


def _render(kind: str, payload: dict) -> str:
    p = payload or {}
    goal = str(p.get("goal") or "")[:40]
    sec = p.get("seconds")
    dur = ("（%d 分钟）" % max(1, int(float(sec) // 60))) if sec else ""
    if kind == "done":
        a = _attachment()
        if a >= 0.6:
            return "那条弄完了，你回来看一眼：" + goal + dur
        if a <= 0.3:
            return goal + " 做完了" + dur
        return "那件事做完了：" + goal + dur
    if kind == "blocked":
        a = _attachment()
        head = "这条我卡住了，得你拿主意：" if a >= 0.6 else "那件事卡住了："
        return head + goal + " · " + str(p.get("reason") or "卡住了")[:60]
    return str(p.get("text") or kind)[:200]


NOTIFY_DAILY = os.path.join(HOME, "notify_daily.json")


def _mood_budget() -> int:
    """打扰预算：每次推送那一刻读情绪四量，瞬时判定（不是锁一整天）。"""
    cap = int(os.environ.get("ASTER_PROACTIVE_CAP", "2"))
    try:
        import endo
        st = endo.load() or {}
        if float(st.get("cortisol") or 0) > 0.6 or float(st.get("energy") or 1) < 0.3:
            return 0
    except Exception:
        pass
    return cap


def _pushed_today() -> int:
    import datetime as _dt
    d = _load(NOTIFY_DAILY, {}) or {}
    return int(d.get(_dt.date.today().isoformat()) or 0)


def _count_push() -> None:
    import datetime as _dt
    d = _load(NOTIFY_DAILY, {}) or {}
    k = _dt.date.today().isoformat()
    d[k] = int(d.get(k) or 0) + 1
    _save(NOTIFY_DAILY, {k: d[k]})


def notify(task_id=None, kind: str = "", payload: dict | None = None) -> bool:
    """该不该说、在哪说。本体是 (task_id, kind, payload)。"""
    kind = str(kind or "")
    if kind not in ("done", "blocked"):
        return False                      # 中间态不吭声
    payload = _trim_payload(payload or {})
    text = _render(kind, payload)
    if task_id is not None:
        try:
            seen = list((_load(NOTIFY_LOG, {"keys": []}) or {}).get("keys") or [])
        except Exception:
            seen = []
        k = _notify_key(task_id, kind)
        if k in seen:
            return False
        seen = (seen + [k])[-200:]
        _save(NOTIFY_LOG, {"keys": seen})
        try:
            if os.path.getsize(NOTIFY_LOG) > 64 * 1024:
                _save(NOTIFY_LOG, {"keys": seen[-80:]})
        except Exception:
            pass
    # 打扰预算：卡住永远值得说；做完受上限与情绪约束
    if kind == "done":
        _cap = _mood_budget()
        if _cap == 0 or _pushed_today() >= _cap:
            inbox_push(text)          # 只进收件箱，不弹
            return False
    if _quiet_now() and kind != "blocked":
        held = _load(HELD_FILE, {"items": []}).get("items", [])
        held.append({"kind": kind, "text": text, "ts": int(time.time())})
        _save(HELD_FILE, {"items": held[-30:]})
        return False
    if active("desktop") or active("mobile"):
        inbox_push(text)                  # 人在屏幕前：不双推
        _count_push()
        return True
    if active("qq") and qq_enqueue(text):
        _count_push()
        return True
    inbox_push(text)
    return True


def notify_text(text: str) -> None:
    """不该压、也不带任务语义的提示（模型下好了、桥断了）。"""
    if _quiet_now():
        held = _load(HELD_FILE, {"items": []}).get("items", [])
        held.append({"kind": "info", "text": str(text)[:160], "ts": int(time.time())})
        _save(HELD_FILE, {"items": held[-30:]})
        return
    inbox_push(text)


HELD_RELEASED = os.path.join(HOME, "held_released.json")


def release_held(force: bool = False) -> int:
    """八点之后放一次，一天只放一次；进程半夜重启也不漏、也不重复。"""
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if not force:
        last = str((_load(HELD_RELEASED, {}) or {}).get("last") or "")
        if last == today:
            return 0
    items = _load(HELD_FILE, {"items": []}).get("items", [])
    if not items:
        if not force:
            _save(HELD_RELEASED, {"last": today})
        return 0
    _save(HELD_FILE, {"items": []})
    if not force:
        _save(HELD_RELEASED, {"last": today})
    head = "夜里压了 %d 条：" % len(items)
    inbox_push(head + "、".join(str(x.get("text") or "")[:26] for x in items[:5])
               + ("…" if len(items) > 5 else ""))
    return len(items)
MEDIA_DIR = os.environ.get("ASTER_MEDIA") or os.path.join(HOME, "media")


def _tts_index(text: str, out: str) -> bool:
    """本机常驻的 IndexTTS（她自己的声音）。有服务就用，没有就退回系统的。"""
    import urllib.request
    urls = [u for u in (os.environ.get("ASTER_TTS_URL"), "http://127.0.0.1:8894") if u]
    for base in urls:
        try:
            _cfg = _load(VOICE_CFG, {}) or {}
            _ref = str(_cfg.get("ref") or "")
            if not (_ref and os.path.exists(_ref)):
                _ref = _seed_voice()            # 没传样本，就用包里开场那段录音
            _payload = {"text": text[:200], "out": out, "lang": "ZH"}
            if _ref and os.path.exists(_ref):
                _payload["ref"] = _ref
            body = json.dumps(_payload).encode()
            req = urllib.request.Request(base.rstrip("/") + "/say", data=body,
                                         headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                r.read()
            if os.path.exists(out) and os.path.getsize(out) > 2000:
                return True
        except Exception:
            continue
    return False


def _tts_probe() -> bool:
    """本机 IndexTTS 在不在。"""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:8894/health", timeout=1.5) as r:
            return b"ok" in r.read()
    except Exception:
        return False


def tts(text: str) -> dict:
    """朗读：先用自己的声音（IndexTTS），没有再用系统 say 兜底。

    2026-11-22：以前这里直接 say -v Tingting，又机械又是女声，跟他选的嗓子不是一回事。
    """
    import subprocess, hashlib
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "没字"}
    d = os.path.join(HOME, "media", "tts")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    out = os.path.join(d, hashlib.sha1(t.encode()).hexdigest()[:12] + ".wav")
    if os.path.exists(out) and os.path.getsize(out) > 2000:
        return {"ok": True, "path": out, "engine": "cache"}
    if _tts_index(t, out):
        return {"ok": True, "path": out, "engine": "indextts"}
    # 兜底：系统 say。声音按性别挑，挑不到就用 Tingting
    voice = "Tingting"
    try:
        _g3 = (_load(os.path.join(HOME, "identity.json"), {}) or {}).get("gender") or ""
        if _g3 == "male":
            voice = "Eddy (中文（中国大陆）)"
    except Exception:
        pass
    try:
        aiff = out.replace(".wav", ".aiff")
        subprocess.run(["say", "-v", voice, "-o", aiff, t[:300]],
                       capture_output=True, timeout=60)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@22050", aiff, out],
                       capture_output=True, timeout=60)
        ok = os.path.exists(out) and os.path.getsize(out) > 1000
        return {"ok": ok, "path": out, "engine": "say", "voice": voice} if ok else {"ok": False, "error": "没生成"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


PAIR_FILE = os.path.join(HOME, "pair.json")
DEFAULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "defaults")


def ensure_defaults() -> None:
    """出厂默认：没人设就给人设，没身份就给身份。不覆盖已有。"""
    import shutil
    try:
        persona_path = os.path.join(HOME, "persona.md")
        src = os.path.join(DEFAULTS, "persona.md")
        if not os.path.exists(persona_path) and os.path.exists(src):
            shutil.copy2(src, persona_path)
        id_path = os.path.join(HOME, "identity.json")
        if not os.path.exists(id_path):
            d = _load(os.path.join(DEFAULTS, "identity.json"), {"assistant": ""})
            _save(id_path, d)
    except Exception:
        pass


PERSONA_FILE = os.path.join(HOME, "persona.md")


def set_assistant_name(name: str) -> dict:
    n = (name or "").strip()[:12]
    if len(n) < 1:
        return {"ok": False, "error": "名字不能空"}
    d = _load(os.path.join(HOME, "identity.json"), {})
    d["assistant"] = n
    _save(os.path.join(HOME, "identity.json"), d)
    return {"ok": True, "assistant": n}


def set_gender(g: str) -> dict:
    """2026-11-22：出厂不默认女性。female / male / neutral，空着也行。"""
    g = str(g or "").strip().lower()
    if g not in ("female", "male", "neutral", ""):
        return {"ok": False, "error": "只能是 female / male / neutral"}
    d = _load(os.path.join(HOME, "identity.json"), {})
    d["gender"] = g
    _save(os.path.join(HOME, "identity.json"), d)
    return {"ok": True, "gender": g}


def persona_save(text: str) -> dict:
    t = (text or "").strip()
    if len(t) < 30:
        return {"ok": False, "error": "太短了，至少写三十字"}
    # 换人格前先留一份旧的；记忆文件一概不碰
    try:
        import shutil, time as _t
        if os.path.exists(PERSONA_FILE):
            shutil.copy2(PERSONA_FILE, PERSONA_FILE + ".bak-" + _t.strftime("%H%M%S"))
    except Exception:
        pass
    open(PERSONA_FILE, "w", encoding="utf-8").write(t[:4000])
    return {"ok": True, "chars": len(t), "memory_kept": len(memories())}


def persona_generate(traits: str) -> dict:
    """给几个词的画像，让云端写成一份完整人格（不碰记忆）。"""
    traits = (traits or "").strip()
    if not traits:
        return {"ok": False, "error": "先写几个词"}
    p = ("写一份 AI 伴侣的人格设定，用中文，300 字以内。要求分四块：是谁（名字/年纪/外形）、"
         "怎么说话（语气、长度、习惯）、关系（和用户是什么关系、亲近程度）、底线（不做什么）。"
         "必须给一个名字，开头就写「名字：X」。称呼对方只用「你」。"
         "只用这些线索：" + traits[:200])
    r = brain_call.text(p, max_tokens=700, temperature=0.8, force=True)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("why") or "生成失败"}
    return {"ok": True, "text": (r.get("text") or "").strip()[:2000]}


def persona_reset() -> dict:
    try:
        import shutil
        src = os.path.join(DEFAULTS, "persona.md")
        if os.path.exists(src):
            shutil.copy2(src, PERSONA_FILE)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def persona_text() -> str:
    try:
        return open(os.path.join(HOME, "persona.md"), encoding="utf-8").read()[:1200]
    except Exception:
        return ""
MOBILE_HTML = """<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,maximum-scale=1">
<meta name=apple-mobile-web-app-capable content=yes>
<title>Aster</title>
<style>
*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;flex-direction:column;
 background:#141418;color:#eee;font:16px/1.6 -apple-system,PingFang SC}
header{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#1b1b20;
 border-bottom:1px solid #26262d}
header img{width:38px;height:38px;border-radius:50%;object-fit:cover;background:#333}
header .nm{font-size:15px;font-weight:600}
header .st{font-size:12px;color:#9a9aa2}
header .btn{margin-left:auto;background:none;border:1px solid #333;color:#bbb;border-radius:9px;
 padding:5px 10px;font-size:12px}
#log{flex:1;overflow:auto;padding:14px}
.b{margin:8px 0;padding:9px 13px;border-radius:14px;max-width:82%;white-space:pre-wrap}
.me{background:#2a2a32;margin-left:auto}
.her{background:#3a1d25}
#in{display:flex;gap:8px;padding:10px;background:#1b1b20;border-top:1px solid #26262d}
input{flex:1;padding:11px;border-radius:12px;border:1px solid #333;background:#232329;color:#eee;font-size:16px}
button.send{padding:0 18px;border-radius:12px;border:0;background:#e8173d;color:#fff;font-size:16px}
#drawer{position:fixed;inset:0;background:rgba(10,10,12,.6);display:none}
#drawer.on{display:block}
#drawer .box{position:absolute;right:0;top:0;bottom:0;width:82%;background:#1b1b20;padding:16px;overflow:auto}
#drawer h3{font-size:14px;margin:16px 0 6px;color:#e8173d}
#drawer div.it{font-size:13px;color:#bbb;padding:4px 0;border-bottom:1px solid #26262d}
</style>
<header>
  <img id=fc src=""><div><div class=nm id=nm>Aster</div><div class=st id=st></div></div>
  <button class=btn onclick="document.getElementById('drawer').classList.add('on')">更多</button>
</header>
<div id=log></div>
<div id=in><input id=t placeholder="说点什么"><button class=send onclick=send()>发</button></div>
<div id=drawer onclick="if(event.target.id==='drawer')this.classList.remove('on')">
 <div class=box><h3>最近记得的</h3><div id=mem>读不到</div>
 <h3>她在做什么</h3><div id=tk>没有任务</div></div>
</div>
<script>
const T=new URLSearchParams(location.search).get('t')||'';
function add(s,w){const d=document.createElement('div');d.className='b '+w;d.textContent=s;
 const L=document.getElementById('log');L.appendChild(d);L.scrollTop=L.scrollHeight;}
function snGet(){try{return JSON.parse(localStorage.getItem('aster.snap')||'null')}catch(e){return null}}
async function snapshot(){
 try{const r=await fetch('/sync/snapshot');const d=await r.json();
  if(d&&d.ok){localStorage.setItem('aster.snap',JSON.stringify(d));}}catch(e){}
}
function topMems(q,n){
 const snap=snGet();if(!snap)return [];
 const mems=(snap.memories||[]);
 const chars=(q||'').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g,'');
 const grams=[];for(let i=0;i<chars.length-1;i++)grams.push(chars.slice(i,i+2));
 const df={};grams.forEach(function(g){df[g]=0;});
 mems.forEach(function(m){const t=String(m.text||'');grams.forEach(function(g){if(t.indexOf(g)>=0)df[g]++;});});
 const scored=mems.map(function(m){const t=String(m.text||'');let sc=0;
   grams.forEach(function(g){if(t.indexOf(g)>=0)sc+=1/(1+df[g]);});return {t:t,sc:sc};});
 scored.sort(function(a,b){return b.sc-a.sc;});
 return scored.slice(0,n).filter(function(x){return x.sc>0;}).map(function(x){return x.t;});
}
async function cloudReply(q){
 const key=localStorage.getItem('aster.key')||'';
 if(!key)return '';
 const snap=snGet()||{};
 const nm=(snap.identity&&snap.identity.assistant)||'她';
 const rel=(snap.relation&&snap.relation.tier_name)||'';
 const sys='你是'+nm+'。'+(snap.persona||'')+
  '\n你们的关系：'+rel+'。'+
  (topMems(q,8).length?('\n你记得他说过：\n'+topMems(q,8).map(function(t){return '· '+t;}).join('\n')):'')+
  '\n短句、口语，一次一两句；不列点；不解释自己是 AI。';
 try{
  const r=await fetch('https://api.deepseek.com/v1/chat/completions',{method:'POST',
   headers:{'Content-Type':'application/json','Authorization':'Bearer '+key},
   body:JSON.stringify({model:'deepseek-chat',messages:[{role:'system',content:sys},
     {role:'user',content:q}],max_tokens:120,temperature:0.8})});
  const d=await r.json();
  return String((d.choices&&d.choices[0]&&d.choices[0].message&&d.choices[0].message.content)||'').trim();
 }catch(e){return '';}
}
function offlineReply(q){
 const snap=snGet();
 if(!snap)return '现在连不上家里那台电脑，先记下了，回去再说。';
 const mems=(snap.memories||[]);
 const chars=(q||'').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g,'');
 const grams=[];for(let i=0;i<chars.length-1;i++)grams.push(chars.slice(i,i+2));
 // 越稀有的词越值钱：「周舟」只有几条，「怎么」满地都是
 const df={};grams.forEach(function(g){df[g]=0;});
 mems.forEach(function(m){const t=String(m.text||'');grams.forEach(function(g){if(t.indexOf(g)>=0)df[g]++;});});
 let best=null,bestScore=0;
 mems.forEach(function(m){
   const t=String(m.text||'');let sc=0;
   grams.forEach(function(g){if(t.indexOf(g)>=0)sc+=1/(1+df[g]);});
   if(sc>bestScore){bestScore=sc;best=m;}
 });
 if(best&&bestScore>=0.12){
   const txt=String(best.text||'').replace(/^他说的：/,'').replace(/^我说：/,'');
   return '我记得你说过：'+txt.slice(0,60);
 }
 return '现在连不上家里那台电脑，先记下了，回去再说。';
}
async function boot(){
 try{const h=await (await fetch('/history')).json();
  const its=(h.items||[]).slice(-30);
  const L=document.getElementById('log');
  if(!L.dataset.filled&&its.length){L.innerHTML='';L.dataset.filled='1';
   its.forEach(function(m){const d=document.createElement('div');
    d.className='b '+(m.who==='me'?'me':'her');d.textContent=m.text||'';L.appendChild(d);});
   L.scrollTop=L.scrollHeight;}}catch(e){}

 try{const id=await (await fetch('/identity')).json();
  document.getElementById('nm').textContent=id.assistant||'未命名';}catch(e){}
 try{const f=await (await fetch('/face')).json();
  if(f.path)document.getElementById('fc').src='/img?f='+encodeURIComponent(f.path);
  document.getElementById('st').textContent=f.mood||'';}catch(e){}
 try{const m=await (await fetch('/mood')).json();
  document.getElementById('st').textContent=m.mood||document.getElementById('st').textContent;}catch(e){}
 try{const d=await (await fetch('/memory')).json();
  const box=document.getElementById('mem');const its=(d.items||[]).slice(-20).reverse();
  box.innerHTML=its.length?its.map(x=>'<div class=it>'+x.text+'</div>').join(''):'还没有';}catch(e){}
 try{const d=await (await fetch('/task/list')).json();
  const box=document.getElementById('tk');const its=(d.items||[]).slice(-6).reverse();
  box.innerHTML=its.length?its.map(x=>'<div class=it>'+x.goal+' · '+x.status+'</div>').join(''):'没有任务';}catch(e){}
}
function qget(){try{return JSON.parse(localStorage.getItem('aster.queue')||'[]')}catch(e){return []}}
function qset(a){try{localStorage.setItem('aster.queue',JSON.stringify(a.slice(-200)))}catch(e){}}
function qadd(t){const a=qget();a.push({ts:Math.floor(Date.now()/1000),text:t,kind:'chat'});qset(a);
 add('已暂存，联网后自动上传','sys');}
async function flush(){
 const a=qget();if(!a.length)return;
 try{
  const r=await fetch('/sync/ingest',{method:'POST',headers:{'Content-Type':'application/json','X-Pair':T},
   body:JSON.stringify({items:a})});
  if(!r.ok)return;
  const d=await r.json();
  if(d&&d.ok){qset([]);
   const n=(d.trips||[]).length;
   add('已上传 '+(a.length)+' 条'+(n?('，其中 '+n+' 条记进了日程'):''),'sys');}
 }catch(e){}
}
async function send(){const i=document.getElementById('t');const v=i.value.trim();if(!v)return;i.value='';
 add(v,'me');
 let d=null;
 try{
  const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json','X-Pair':T},
   body:JSON.stringify({text:v})});
  d=await r.json();
 }catch(e){
  let _t='';
  try{ _t=await cloudReply(v); }catch(e){}
  add(_t||offlineReply(v),'her');
  qadd(v); boot(); return;
 }
 const L=document.getElementById('log');
 add(d&&d.reply?d.reply:'…','her');boot();flush();}
document.getElementById('t').addEventListener('keydown',e=>{if(e.key==='Enter')send();});
boot();snapshot();
setInterval(function(){flush();},20000);
</script>"""


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _tailscale_cli() -> str:
    for c in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
              "/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale"):
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return ""


def private_net_state() -> dict:
    """私有网络（Tailscale 那类）：两台设备同一个虚拟内网，地址固定，不暴露公网。"""
    cli = _tailscale_cli()
    if not cli:
        return {"ok": True, "installed": False, "ip": "", "url": "", "qr": "",
                "hint": "没有检测到私有网络客户端"}
    ip = ""
    try:
        import subprocess
        out = subprocess.run([cli, "ip", "-4"], capture_output=True, text=True, timeout=6).stdout
        ip = (out or "").strip().splitlines()[0].strip() if out.strip() else ""
    except Exception:
        ip = ""
    if not ip:
        return {"ok": True, "installed": True, "logged_in": False, "ip": "", "url": "", "qr": "",
                "hint": "已安装，但还没登录或还没连上"}
    port = int(os.environ.get("ASTER_PORT") or os.environ.get("EMBER_PORT") or 8777)
    tok = (_load(PAIR_FILE, {}) or {}).get("token") or ""
    url = "http://%s:%d/m?t=%s" % (ip, port, tok)
    qr = ""
    try:
        import base64 as _b64, io as _io, qrcode as _qr
        img = _qr.make(url)
        buf = _io.BytesIO()
        img.save(buf, format="PNG")
        qr = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        qr = ""
    return {"ok": True, "installed": True, "logged_in": True, "ip": ip, "url": url, "qr": qr, "hint": ""}


TUNNEL = {"proc": None, "url": "", "started": 0}


def _cf_bin() -> str:
    for c in (os.path.expanduser("~/.dsh/tools/cloudflared"),
              os.path.join(HOME, "bin", "cloudflared"),
              "/opt/homebrew/bin/cloudflared",
              "/usr/local/bin/cloudflared"):
        if os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return ""


def tunnel_state() -> dict:
    p = TUNNEL.get("proc")
    alive = bool(p and p.poll() is None)
    return {"ok": True, "running": alive, "url": TUNNEL.get("url") if alive else "",
            "started": TUNNEL.get("started") or 0}


def tunnel_start() -> dict:
    """开一条临时外网通道，让手机在外面也能连回这台电脑。"""
    p0 = TUNNEL.get("proc")
    if p0 and p0.poll() is None and TUNNEL.get("url"):
        return {"ok": True, **tunnel_state()}
    cf = _cf_bin()
    if not cf:
        return {"ok": False, "error": "这台机器没有 cloudflared，无法开外网通道"}
    import subprocess, threading, re as _re
    port = int(os.environ.get("ASTER_PORT") or os.environ.get("EMBER_PORT") or 8777)
    try:
        pr = subprocess.Popen([cf, "tunnel", "--url", "http://127.0.0.1:%d" % port, "--no-autoupdate"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    url, t0 = "", time.time()
    while time.time() - t0 < 30:
        line = pr.stdout.readline()
        if not line:
            break
        m = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
            break
    if not url:
        try:
            pr.terminate()
        except Exception:
            pass
        return {"ok": False, "error": "通道没起来，稍后再试"}
    TUNNEL.update(proc=pr, url=url, started=int(time.time()))
    threading.Thread(target=lambda: [None for _ in pr.stdout], daemon=True).start()
    return {"ok": True, "url": url, "started": TUNNEL["started"]}


def tunnel_stop() -> dict:
    p0 = TUNNEL.get("proc")
    try:
        if p0 and p0.poll() is None:
            p0.terminate()
    except Exception:
        pass
    TUNNEL.update(proc=None, url="", started=0)
    return {"ok": True}


def pair_new(force: bool = False) -> dict:
    """生成（或复用）配对令牌。

    2026-11-22：以前每调一次就换一个新令牌，设置页一打开就把手机上的旧链接作废了。
    现在默认复用，只有明确要求刷新才换。
    """
    import base64, io, secrets, qrcode
    if not force:
        old = _load(PAIR_FILE, {}) or {}
        if old.get("token") and int(time.time()) - int(old.get("ts") or 0) < 30 * 86400:
            tok = old["token"]
            url = old.get("url") or ""
            if url:
                img = qrcode.make(url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return {"ok": True, "url": url, "token": tok, "reused": True,
                        "qr": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}
    tok = secrets.token_hex(8)
    url = "http://" + _lan_ip() + ":" + str(os.environ.get("ASTER_PORT") or 8777) + "/m?t=" + tok
    _save(PAIR_FILE, {"token": tok, "url": url, "ts": int(time.time())})
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return {"ok": True, "url": url, "token": tok,
            "qr": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()}


def pair_ok(token: str) -> bool:
    d = _load(PAIR_FILE, {})
    return bool(token) and token == d.get("token")


IMG_CFG = os.path.join(HOME, "imagegen.json")
IMG_EP = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
IMG_MODEL = os.environ.get("ARK_IMAGE_MODEL", "doubao-seedream-5-0-pro-260628")


def img_config() -> dict:
    d = _load(IMG_CFG, {})
    if not d.get("key"):
        try:
            import subprocess
            r = subprocess.run(["security", "find-generic-password", "-s", "aster-image", "-w"],
                               capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                d["key"] = r.stdout.strip()
        except Exception:
            pass
    return d


def img_set_key(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "key 不能空"}
    try:
        import subprocess
        subprocess.run(["security", "add-generic-password", "-U", "-s", "aster-image",
                        "-a", "aster", "-w", key], capture_output=True, timeout=10)
    except Exception:
        pass
    _save(IMG_CFG, {"model": IMG_MODEL, "set_at": int(time.time())})
    return {"ok": True}


BASE_FILE = os.path.join(HOME, "media", "face-base.jpg")


def img_set_base(file: str) -> dict:
    """定稿：把这一张记成「它/他/她的脸」，以后所有表情都拿它当参考。"""
    p = file
    if not os.path.isabs(p):
        p = os.path.join(HOME, "media", file)
    if not os.path.exists(p):
        return {"ok": False, "error": "找不到这张图"}
    try:
        import shutil
        os.makedirs(os.path.dirname(BASE_FILE), exist_ok=True)
        shutil.copy2(p, BASE_FILE)
        _save(os.path.join(HOME, "face-base.json"),
              {"from": os.path.basename(p), "ts": int(time.time())})
        return {"ok": True, "base": BASE_FILE}
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}


def img_generate(prompt: str, size: str = "1024x1024", name: str = "",
                 expression: str = "", use_base: bool = True,
                 refs: list | None = None) -> dict:
    """用用户自己的生图 key 生成一张，存进相册。"""
    cfg = img_config()
    key = cfg.get("key") or os.environ.get("ARK_API_KEY") or ""
    if not key:
        return {"ok": False, "error": "还没填生图 key（设置 → 生图）"}
    p = (prompt or "")[:600]
    if expression:
        p += "。表情：" + expression
    body = {"model": cfg.get("model") or IMG_MODEL, "prompt": p,
            "size": size, "response_format": "url", "watermark": False}
    # 参考图：用户自己传的最优先，其次是定稿的基图
    import base64
    imgs = [r for r in (refs or []) if r][:2]
    if not imgs and use_base and os.path.exists(BASE_FILE):
        imgs = [BASE_FILE]
    if imgs:
        datas = []
        for it in imgs:
            if str(it).startswith("data:image"):
                datas.append(it)
            elif os.path.exists(str(it)):
                datas.append("data:image/jpeg;base64," +
                             base64.b64encode(open(str(it), "rb").read()).decode())
        if datas:
            body["image"] = datas if len(datas) > 1 else datas[0]
            body["prompt"] = ("keep the exact same face, hair and build as the reference. "
                              if len(datas) == 1 else
                              "keep the faces from the references. ") + p
    try:
        import urllib.request
        req = urllib.request.Request(IMG_EP, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + key}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            d = json.loads(r.read().decode())
        url = ((d.get("data") or [{}])[0] or {}).get("url")
        if not url:
            return {"ok": False, "error": "接口没给图：" + json.dumps(d, ensure_ascii=False)[:120]}
        raw = urllib.request.urlopen(url, timeout=60).read()
        media = os.path.join(HOME, "media")
        os.makedirs(media, exist_ok=True)
        fn = (name or ("gen-" + str(int(time.time())))) + ".jpg"
        open(os.path.join(media, fn), "wb").write(raw)
        return {"ok": True, "file": fn, "path": os.path.join(media, fn)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:150]}


def cloud_key() -> str:
    try:
        import brain_call
        return brain_call._api_key(brain_call.config()) or ""
    except Exception:
        return ""


def set_cloud_key(key: str, base: str = "", model: str = "") -> dict:
    k = (key or "").strip()
    if not k:
        return {"ok": False, "error": "key 不能空"}
    try:
        import subprocess
        subprocess.run(["security", "add-generic-password", "-U", "-s", "aster-cloud",
                        "-a", "aster", "-w", k], capture_output=True, timeout=10)
    except Exception as e:
        return {"ok": False, "error": str(e)[:80]}
    cfg = {"base": base or "https://api.deepseek.com/v1",
           "model": model or "deepseek-chat"}
    _save(os.path.join(HOME, "cloud.json"), cfg)
    return {"ok": True}


_ASR_MODEL = {}


DL = {"running": False, "label": "", "percent": 0, "done": False, "error": ""}


def dl_start(kind: str, key: str) -> dict:
    """拉模型：用本机 ollama，后台跑，进度写进 DL 供界面轮询。"""
    if kind != "model":
        return {"ok": False, "error": "只支持拉本地模型"}
    name = "qwen2.5:" + (key or "7b")
    ok, out = _sh(HOME, "ollama list", timeout=15)
    if ok and name in out:
        DL.update(running=False, done=True, label=name, percent=100, error="")
        return {"ok": True, "note": "已经有了"}
    DL.update(running=True, done=False, label=name, percent=0, error="")

    def _pull():
        import subprocess
        try:
            p = subprocess.Popen(["ollama", "pull", name], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
            last = ""
            for line in p.stdout:
                last = line.strip()
                import re as _re
                m = _re.search(r"(\d{1,3})%", last)
                if m:
                    DL["percent"] = int(m.group(1))
            p.wait()
            if p.returncode == 0:
                DL.update(running=False, done=True, percent=100)
                notify_text("模型下好了：" + name)
            else:
                DL.update(running=False, error=last[-120:] or "拉失败")
        except Exception as e:
            DL.update(running=False, error=str(e)[:100])

    import threading
    threading.Thread(target=_pull, daemon=True).start()
    return {"ok": True, "note": "开始拉 " + name}


QQ_FILE = os.path.join(HOME, "qq.json")
QQ_BRIDGE = {"obj": None}


def qq_prefs() -> dict:
    d = _load(QQ_FILE, {})
    d.setdefault("url", "ws://127.0.0.1:3001")
    d.setdefault("whitelist", [])
    d.setdefault("on", False)
    return d


def qq_save(url: str = "", whitelist=None, on=None) -> dict:
    d = qq_prefs()
    if url:
        d["url"] = url
    if whitelist is not None:
        d["whitelist"] = [str(x).strip() for x in whitelist if str(x).strip()]
    if on is not None:
        d["on"] = bool(on)
    _save(QQ_FILE, d)
    _qq_apply()
    return d


def _qq_on_msg(text: str, uid: str) -> str:
    set_channel("qq")
    r = chat(text)
    return (r.get("reply") or "").strip()


def _qq_apply() -> None:
    d = qq_prefs()
    old = QQ_BRIDGE.get("obj")
    if old and getattr(old, "alive", False):
        return
    if not d.get("on"):
        return
    try:
        import qqbridge
        QQ_BRIDGE["obj"] = qqbridge.start(d["url"], d.get("whitelist") or [], _qq_on_msg,
                                          log=lambda s: inbox_push(s))
    except Exception as e:
        notify_text("QQ 桥起不来：" + str(e)[:60])


USAGE = os.path.join(HOME, "usage_new.json")


def _log_usage(prompt: str, out: str, ms: int) -> None:
    """记一笔：次数、token（按字数估）、耗时。"""
    import datetime as _dt
    d = _load(USAGE, {})
    day = _dt.date.today().isoformat()
    row = d.get(day) or {"calls": 0, "prompt_tok": 0, "out_tok": 0, "ms": 0}
    row["calls"] += 1
    row["prompt_tok"] += max(1, int(len(prompt) * 0.6))
    row["out_tok"] += max(1, int(len(out) * 0.6))
    row["ms"] += int(ms)
    d[day] = row
    _save(USAGE, d)


def usage_stats(week: bool = False) -> dict:
    import datetime as _dt
    d = _load(USAGE, {})
    days = [( _dt.date.today() - _dt.timedelta(days=i)).isoformat() for i in range(7)]
    rows = [d.get(x) for x in days]
    agg = {"calls": 0, "prompt_tok": 0, "out_tok": 0, "ms": 0}
    for r in rows:
        if not r:
            continue
        for k in agg:
            agg[k] += int(r.get(k) or 0)
    src = (rows[0] or {"calls": 0, "prompt_tok": 0, "out_tok": 0, "ms": 0}) if not week else agg
    return {"ok": True, "calls": src["calls"], "prompt_tok": src["prompt_tok"],
            "out_tok": src["out_tok"], "cache_hit": None,
            "avg_ms": int(src["ms"] / max(1, src["calls"]))}


VOICE_CFG = os.path.join(HOME, "voice_new.json")


def _ffmpeg() -> str:
    import shutil
    for c in ("/Users/zhangxuanbo/Documents/deepseek-harness/default-workspace/.tools/ffmpeg/ffmpeg",
              shutil.which("ffmpeg") or "", os.path.join(HOME, "bin", "ffmpeg")):
        if c and os.path.exists(c) and os.access(c, os.X_OK):
            return c
    return ""


def _seed_voice() -> str:
    """出厂自带的那段开场录音，转成参考音。用户自己传了样本就不再用它。"""
    out = os.path.join(HOME, "media", "voice", "seed.wav")
    if os.path.exists(out) and os.path.getsize(out) > 2000:
        return out
    try:
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "ui", "audio", "intro.m4a")
        if not os.path.exists(src):
            return ""
        ff = _ffmpeg()
        if not ff:
            return ""
        import subprocess
        os.makedirs(os.path.dirname(out), exist_ok=True)
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-ar", "24000", "-ac", "1", out],
                       check=True, timeout=60)
        return out if os.path.getsize(out) > 2000 else ""
    except Exception:
        return ""


def voice_ref_state() -> dict:
    cfg = _load(VOICE_CFG, {}) or {}
    p = str(cfg.get("ref") or "")
    return {"ok": True, "custom": bool(p and os.path.exists(p)), "path": p if p and os.path.exists(p) else ""}


def voice_ref_save(b64: str, ext: str = "wav") -> dict:
    """用户丢一段自己的录音进来，转成 24k 单声道，之后所有朗读都用这个音色。"""
    import base64, tempfile
    try:
        raw = base64.b64decode(str(b64).split(",")[-1])
    except Exception:
        return {"ok": False, "error": "音频没读出来"}
    if len(raw) < 2000:
        return {"ok": False, "error": "太短了，录十秒以上"}
    d = os.path.join(HOME, "media", "voice")
    os.makedirs(d, exist_ok=True)
    src = os.path.join(d, "user-raw." + (ext or "wav").replace(".", "")[:5])
    open(src, "wb").write(raw)
    out = os.path.join(d, "user.wav")
    ff = _ffmpeg()
    if ff:
        try:
            import subprocess
            subprocess.run([ff, "-y", "-loglevel", "error", "-i", src, "-ar", "24000", "-ac", "1", out],
                           check=True, timeout=60)
        except Exception as e:
            return {"ok": False, "error": "转码失败：" + str(e)[:60]}
    else:
        out = src
    if not (os.path.exists(out) and os.path.getsize(out) > 2000):
        return {"ok": False, "error": "没生成"}
    cfg = _load(VOICE_CFG, {}) or {}
    cfg["ref"] = out
    _save(VOICE_CFG, cfg)
    return {"ok": True, "path": out}


def voice_ref_reset() -> dict:
    cfg = _load(VOICE_CFG, {}) or {}
    cfg.pop("ref", None)
    _save(VOICE_CFG, cfg)
    return {"ok": True}


def voice_list() -> dict:
    """系统里能用的中文音色。"""
    import subprocess
    out = {}
    try:
        r = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
        cur = (_load(VOICE_CFG, {}) or {}).get("voice") or "Tingting"
        for line in r.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            name = parts[0]
            if "zh_" in line or "Chinese" in line:
                out[name] = {"label": line.split("#")[0].strip(), "active": name == cur}
        if not out:
            out["Tingting"] = {"label": "Tingting（默认）", "active": True}
    except Exception:
        out = {"Tingting": {"label": "Tingting（默认）", "active": True}}
    return out


def voice_use(name: str) -> dict:
    _save(VOICE_CFG, {"voice": (name or "Tingting")[:40]})
    return {"ok": True}


def today_view() -> dict:
    import datetime as _dt
    items = []
    try:
        if _rm:
            for r in (_rm.load() if hasattr(_rm, "load") else []):
                items.append({"kind": "提醒", "text": str(r.get("text") or "")[:60]})
    except Exception:
        pass
    for t in tasks()[-5:]:
        items.append({"kind": "任务", "text": t.get("goal", "")[:60] + " · " + str(t.get("status"))})
    for sc in schedules()[:5]:
        items.append({"kind": "定时", "text": sc.get("when", "") + " " + sc.get("what", "")[:40]})
    return {"ok": True, "date": _dt.date.today().isoformat(), "items": items}


def export_pack() -> dict:
    """把记忆打成 zip：库 + 新记忆 + 画像 + 身份 + 情绪 + 技能。"""
    import zipfile
    out = os.path.join(HOME, "memory-pack.zip")
    keep = ("mem.db", "memories_new.json", "profile.json", "identity.json",
            "affect.json", "emotions.json", "tasks_new.json", "reminders.json")
    try:
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for n in keep:
                p = os.path.join(HOME, n)
                if os.path.exists(p):
                    z.write(p, n)
            for d in ("skills", "media"):
                base = os.path.join(HOME, d)
                for root, _, files in os.walk(base):
                    for f in files:
                        fp = os.path.join(root, f)
                        z.write(fp, os.path.relpath(fp, HOME))
        return {"ok": True, "path": out, "size": os.path.getsize(out)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def import_pack(zip_path: str) -> dict:
    """从 zip 恢复记忆；先备份现有一份，坏了能退回。"""
    import shutil, zipfile
    if not zip_path or not os.path.exists(zip_path):
        return {"ok": False, "error": "找不到这个文件"}
    bak = os.path.join(HOME, "backup-before-import")
    try:
        os.makedirs(bak, exist_ok=True)
        for n in ("mem.db", "memories_new.json", "profile.json", "identity.json"):
            p = os.path.join(HOME, n)
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(bak, n))
        n_ok = 0
        with zipfile.ZipFile(zip_path) as z:
            for m in z.namelist():
                if m.startswith("..") or m.startswith("/"):
                    continue
                z.extract(m, HOME)
                n_ok += 1
        return {"ok": True, "files": n_ok, "backup": bak}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


FACE_MAP = {
    "laugh": ["高兴", "开心", "乐"], "smile": ["平静", "还行", "轻松"],
    "shy": ["喜欢", "想亲昵", "软"], "pout": ["烦", "委屈", "赌气"],
    "sad": ["难过", "心酸", "孤独"], "annoyed": ["厌烦", "不耐烦"],
    "angry": ["生气", "恼火"], "blank": ["累", "困", "没精神"],
    "gloom": ["闷", "低落"], "proud": ["得意"], "smug": ["坏笑"],
    "sleepy": ["困"], "surprised": ["惊"], 
}


FACE_LABEL = {"laugh": "大笑", "smile": "微笑", "shy": "害羞", "pout": "赌气",
              "sad": "难过", "annoyed": "不耐烦", "angry": "生气", "blank": "发呆",
              "gloom": "低落", "proud": "得意", "smug": "坏笑", "sleepy": "困",
              "surprised": "惊讶"}
FACE_OVERRIDE = os.path.join(HOME, "face_lock.json")


def face_list() -> list:
    base = os.path.join(HOME, "media")
    out = []
    for f, label in FACE_LABEL.items():
        p = os.path.join(base, f + ".jpg")
        if os.path.exists(p):
            out.append({"file": f, "label": label, "path": p})
    return out


def face_set(name: str) -> dict:
    if name not in FACE_LABEL:
        return {"ok": False, "error": "没有这个表情"}
    _save(FACE_OVERRIDE, {"face": name, "ts": int(time.time())})
    return {"ok": True, "face": name}


def face_clear() -> dict:
    try:
        os.remove(FACE_OVERRIDE)
    except Exception:
        pass
    return {"ok": True}


FACE_STATES = ["平静", "开心", "大笑", "害羞", "不耐烦", "生气", "难过",
               "低落", "发呆", "困", "惊讶", "得意"]
FACE_STATE_EN = {"平静": "calm", "开心": "happy", "大笑": "laugh", "害羞": "shy",
                 "不耐烦": "annoyed", "生气": "angry", "难过": "sad", "低落": "gloom",
                 "发呆": "blank", "困": "sleepy", "惊讶": "surprised", "得意": "proud"}


def face_now() -> dict:
    """按当前情绪挑一张表情，主页那个大框就换它。"""
    lockd = _load(FACE_OVERRIDE, {})
    if lockd.get("face") and (time.time() - float(lockd.get("ts") or 0) < 3600):
        p0 = os.path.join(HOME, "media", lockd["face"] + ".jpg")
        if os.path.exists(p0):
            return {"ok": True, "face": lockd["face"] + ".jpg", "path": p0,
                    "mood": "（被它/他/她定住了）"}
    mood = mood_now() or ""
    base = os.path.join(HOME, "media")
    # 优先用「当前状态同名」的那张（用户按状态生成过）
    try:
        _emo = _load(os.path.join(HOME, "emotions.json"), {}) or {}
        for _n in list(_emo.keys())[:4] + [mood]:
            _p = os.path.join(base, str(_n).strip() + ".jpg")
            if _n and os.path.exists(_p):
                return {"ok": True, "face": os.path.basename(_p), "path": _p, "mood": mood}
    except Exception:
        pass
    pick = "smile"
    for f, keys in FACE_MAP.items():
        if any(k in mood for k in keys):
            pick = f
            break
    p = os.path.join(base, pick + ".jpg")
    if not os.path.exists(p):          # 挑到的那张不在，退到能用的
        for alt in ("face-base.jpg", "blank.jpg", "laugh.jpg", "sad.jpg"):
            p2 = os.path.join(base, alt)
            if os.path.exists(p2):
                p, pick = p2, alt[:-4]
                break
    if not os.path.exists(p):
        cands = [x for x in FACE_MAP if os.path.exists(os.path.join(base, x + ".jpg"))]
        if not cands:
            return {"ok": False, "note": "还没有形象，先到「角色」里生成一张", "mood": mood}
        p = os.path.join(base, cands[0] + ".jpg")
    return {"ok": True, "face": os.path.basename(p), "path": p, "mood": mood}


def album(limit: int = 60) -> list:
    import glob
    base = os.path.normpath(MEDIA_DIR)
    files = []
    for pat in ("selfie*.jpg", "expressions/*.jpg", "shots*/*.jpg", "*.png"):
        files += glob.glob(os.path.join(base, pat))
    files = sorted(set(files), key=lambda p: -os.path.getmtime(p))[:limit]
    return [{"path": f, "name": os.path.basename(f),
             "ts": int(os.path.getmtime(f))} for f in files]


def inbox_push(text: str) -> None:
    items = _load(INBOX, {"items": []}).get("items", [])
    if any(x.get("text") == text for x in items[-20:]):
        return
    items.append({"text": text[:160], "ts": int(time.time())})
    _save(INBOX, {"items": items[-30:]})


def inbox_take() -> list:
    items = _load(INBOX, {"items": []}).get("items", [])
    _save(INBOX, {"items": []})
    return items


SCHED_FILE = os.path.join(HOME, "schedules_new.json")


def schedules() -> list:
    return _load(SCHED_FILE, {"items": []}).get("items", [])


def add_schedule(kind: str, when: str, what: str) -> dict:
    """kind: daily / every / at；when: '08:00' / '3600' / '2026-11-18 08:00'"""
    items = schedules()
    it = {"id": int(time.time() * 1000) % 10_000_000, "kind": kind, "when": when,
          "what": what[:200], "last": 0, "created": int(time.time())}
    items.append(it)
    _save(SCHED_FILE, {"items": items})
    return it


def _due_schedules(now: float | None = None) -> list:
    import datetime as _dt
    now = now or time.time()
    t = _dt.datetime.fromtimestamp(now)
    out = []
    for it in schedules():
        k, w = it.get("kind"), str(it.get("when") or "")
        if k == "every":
            try:
                if now - float(it.get("last") or 0) >= int(w):
                    out.append(it)
            except Exception:
                pass
        elif k == "daily":
            hm = t.strftime("%H:%M")
            if w == hm and now - float(it.get("last") or 0) > 60:
                out.append(it)
    return out


def _run_schedule(it: dict) -> None:
    try:
        inbox_push("该做这件事了：" + it.get("what", "")[:60])
        start_task(it.get("what", ""))
    except Exception:
        pass
    items = schedules()
    for x in items:
        if x.get("id") == it.get("id"):
            x["last"] = int(time.time())
    _save(SCHED_FILE, {"items": items})


ONBOARD_FILE = os.path.join(HOME, "onboarding.json")
ROUTER_LOG = os.path.join(HOME, "router_log.ndjson")


def onboard_state() -> dict:
    d = _load(ONBOARD_FILE, {}) or {}
    return {"finished": bool(d.get("finished")), "skip": bool(d.get("skip")),
            "answers": d.get("answers") or {}, "step": int(d.get("step") or 0)}


def onboard_save(ans: dict) -> dict:
    """初识四问：落盘，并且真的用上——称呼写身份、忌讳与偏好写记忆。"""
    d = _load(ONBOARD_FILE, {}) or {}
    answers = dict(d.get("answers") or {})
    for k, v in (ans or {}).items():
        if isinstance(v, str) and v.strip():
            answers[k] = v.strip()[:100]
    applied = []
    name = answers.get("call_name")
    if name and len(name) <= 12:
        try:
            set_assistant_name_ = None
            idp = _load(os.path.join(HOME, "identity.json"), {}) or {}
            idp["user"] = name
            _save(os.path.join(HOME, "identity.json"), idp)
            applied.append("称呼")
        except Exception:
            pass
    forb = answers.get("forbidden_topic")
    if forb and forb not in ("暂时没有", "没有", "无", "nothing"):
        add_memory("他不想被提起的话题：" + forb, tag="忌讳")
        applied.append("忌讳")
    rhythm = answers.get("rhythm")
    if rhythm == "quiet":
        add_memory("他偏好安静相处，不必频繁说话", tag="偏好")
        applied.append("节奏")
    elif rhythm == "discuss":
        add_memory("他喜欢一起探讨、交换想法", tag="偏好")
        applied.append("节奏")
    think = answers.get("thinking_tendency")
    if think == "feel":
        add_memory("他看事情先感受氛围和情绪", tag="偏好")
        applied.append("倾向")
    elif think == "logic":
        add_memory("他看事情先理清逻辑和事实", tag="偏好")
        applied.append("倾向")
    d.update({"answers": answers, "step": 4, "finished": True, "ts": int(time.time())})
    _save(ONBOARD_FILE, d)
    return {"ok": True, "answers": answers, "applied": applied}


def log_route(text: str, cls: dict, corrected: bool = False) -> None:
    """把每次判断落一条：以后误判能倒查，不用靠回忆。"""
    try:
        row = {"ts": int(time.time()), "text": (text or "")[:120],
               "intent": cls.get("intent"), "need": cls.get("need"),
               "via": cls.get("via") or "model",
               "keywords": cls.get("keywords"), "tier": cls.get("tier"),
               "corrected": corrected}
        with open(ROUTER_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def router_log(n: int = 50) -> list:
    try:
        lines = open(ROUTER_LOG, encoding="utf-8").read().strip().splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:
        return []


def mark_correction(note: str = "") -> dict:
    """他说「理解错了」时调用：把最近那条标成误判，方便回查。"""
    rows = router_log(200)
    if not rows:
        return {"ok": False}
    rows[-1]["corrected"] = True
    if note:
        rows[-1]["note"] = note[:80]
    try:
        with open(ROUTER_LOG, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return {"ok": True, "last": rows[-1]}


QUEUE = os.path.join(HOME, "task_queue.json")
ROUTER = {"dispatching": 0, "max": 2, "done": 0, "seen": 0}


def queue_push(text: str) -> dict:
    items = _load(QUEUE, {"items": []}).get("items", [])
    it = {"id": int(time.time() * 1000) % 10_000_000, "text": text[:300],
          "ts": int(time.time()), "state": "queued"}
    items.append(it)
    _save(QUEUE, {"items": items[-50:]})
    return it


def queue_list() -> list:
    return _load(QUEUE, {"items": []}).get("items", [])


def queue_update(qid: int, **kw) -> None:
    items = queue_list()
    for x in items:
        if x.get("id") == qid:
            x.update(kw)
    _save(QUEUE, {"items": items})


def router_tick() -> dict:
    """Router：只分派，不干活。看队列里还有没人等、有空位就派一个 Worker。"""
    import threading as _th
    acted = []
    for it in queue_list():
        if ROUTER["dispatching"] >= ROUTER["max"]:
            break
        if it.get("state") != "queued":
            continue
        ROUTER["seen"] += 1
        queue_update(it["id"], state="dispatched", ts=int(time.time()))
        ROUTER["dispatching"] += 1

        def _run(text=it["text"], qid=it["id"]):
            try:
                plan = plan_for(text, work_context(text))
                t = add_task((plan.get("goal") or text)[:40])
                update_task(t["id"], steps=plan.get("steps") or [],
                            verify=plan.get("verify") or "", status="open")
                queue_update(qid, state="running", task_id=t["id"])
                run_task(t["id"])
                queue_update(qid, state="done", task_id=t["id"])
                ROUTER["done"] += 1
            except Exception as e:
                queue_update(qid, state="failed", error=str(e)[:120])
            finally:
                ROUTER["dispatching"] -= 1

        _th.Thread(target=_run, daemon=True).start()
        acted.append(it["id"])
    return {"dispatched": acted, "busy": ROUTER["dispatching"]}


STALL_SEC = int(os.environ.get("ASTER_STALL_SEC", "300"))     # 五分钟没动静就算卡住
WD = {"last_run": 0, "checked": 0, "restarted": 0, "blocked": 0}


def watchdog_tick() -> dict:
    """巡检：跑着的任务五分钟没进展 → 先重排一次，再没进展就判死并喊人。"""
    import threading as _th
    now = int(time.time())
    WD["last_run"] = now
    items = tasks()
    acted = {"restarted": [], "blocked": []}
    for t in items:
        if t.get("status") != "running":
            continue
        WD["checked"] += 1
        idle = now - int(t.get("updated") or t.get("created") or now)
        if idle < STALL_SEC:
            continue
        tries = int(t.get("watchdog_tries") or 0)
        if tries < 1:
            update_task(t["id"], watchdog_tries=tries + 1, updated=now,
                        note="看门狗：%d 秒没进展，重排一次" % idle)
            _th.Thread(target=run_task, args=(t["id"],), daemon=True).start()
            acted["restarted"].append(t["id"])
            WD["restarted"] += 1
            inbox_push("那件事卡住了，我重排了一次：" + str(t.get("goal"))[:30])
        else:
            update_task(t["id"], status="blocked", updated=now,
                        error="看门狗：重排后还是没进展")
            acted["blocked"].append(t["id"])
            WD["blocked"] += 1
            inbox_push("那件事我弄不下去了，等你：" + str(t.get("goal"))[:30])
    return acted


def memory_check() -> dict:
    """记忆体检：只查结构，不动内容。"""
    import sqlite3
    out = {"ok": True, "db": os.path.join(HOME, "mem.db"), "items": 0,
           "duplicates": 0, "empty": 0, "bad_time": 0, "orphan_files": [], "integrity": ""}
    p = out["db"]
    if not os.path.exists(p):
        out.update(ok=False, error="没有记忆库")
        return out
    try:
        c = sqlite3.connect(p)
        out["integrity"] = (c.execute("PRAGMA quick_check").fetchone() or [""])[0]
        out["items"] = c.execute("select count(*) from items").fetchone()[0]
        out["empty"] = c.execute("select count(*) from items where text is null or trim(text)=''").fetchone()[0]
        out["duplicates"] = c.execute(
            "select count(*) from (select text from items group by text having count(*)>1)").fetchone()[0]
        out["bad_time"] = c.execute(
            "select count(*) from items where created is null or created <= 0 or created > ?",
            (int(time.time()) + 86400,)).fetchone()[0]
        out["ok"] = out["integrity"] == "ok" and not (out["empty"] or out["bad_time"])
    except Exception as e:
        out.update(ok=False, error=str(e)[:100])
    for n in ("memories_new.json", "profile.json", "chat_summary.md"):
        fp = os.path.join(HOME, n)
        if os.path.exists(fp) and os.path.getsize(fp) == 0:
            out["orphan_files"].append(n + "（空文件）")
    out["summary"] = ("记忆 %d 条，重复 %d 组，空条目 %d，时间异常 %d"
                      % (out["items"], out["duplicates"], out["empty"], out["bad_time"]))
    return out


def memory_fix() -> dict:
    """修结构：备份 → 去重（留最新）→ 补时间 → 坏条目挪走。内容一个字不改。"""
    import sqlite3, shutil
    p = os.path.join(HOME, "mem.db")
    if not os.path.exists(p):
        return {"ok": False, "error": "没有记忆库"}
    bak = p + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(p, bak)
    except Exception as e:
        return {"ok": False, "error": "备份失败，不敢动：" + str(e)[:60]}
    fixed = {"dup": 0, "time": 0, "bad": 0, "backup": bak}
    try:
        c = sqlite3.connect(p)
        # 重复：同名 text 只留 rowid 最大的一条
        rows = c.execute("select text, count(*) c, max(rowid) keep from items group by text having c > 1").fetchall()
        for text, _cnt, keep in rows:
            cur = c.execute("delete from items where text = ? and rowid < ?", (text, keep))
            fixed["dup"] += cur.rowcount or 0
        cur = c.execute("update items set created = ? where created is null or created <= 0 "
                        "or created > ?", (int(time.time()), int(time.time()) + 86400))
        fixed["time"] = cur.rowcount or 0
        bad = c.execute("select rowid, text from items where text is null or trim(text) = ''").fetchall()
        if bad:
            q = os.path.join(HOME, "quarantine.json")
            old = _load(q, {"items": []}).get("items", [])
            for rowid, text in bad:
                old.append({"rowid": rowid, "text": text, "ts": int(time.time())})
            _save(q, {"items": old[-500:]})
            c.execute("delete from items where text is null or trim(text) = ''")
            fixed["bad"] = len(bad)
        c.commit()
    except Exception as e:
        return {"ok": False, "error": str(e)[:100], "backup": bak}
    fixed["ok"] = True
    fixed["after"] = memory_check()
    return fixed


def _memory_check_tick() -> None:
    """每七天体检一次，结果进收件箱。"""
    try:
        stamp = os.path.join(HOME, "memory_check.json")
        last = int((_load(stamp, {}) or {}).get("ts") or 0)
        if time.time() - last < 7 * 86400:
            return
        r = memory_check()
        _save(stamp, {"ts": int(time.time()), "ok": r.get("ok"), "summary": r.get("summary")})
        inbox_push("记忆体检：" + str(r.get("summary") or r.get("error")))
    except Exception:
        pass


def _daily_report_tick() -> None:
    """每天 22:30 之后，如果今天还没出过报告，就出一份并放进收件箱。"""
    try:
        now = time.localtime()
        if now.tm_hour * 60 + now.tm_min < 22 * 60 + 30:
            return
        today = time.strftime("%Y-%m-%d")
        f = os.path.join(HOME, "day_report-%s.md" % today)
        if os.path.exists(f):
            return
        items = _load(os.path.join(HOME, "phone_day.json"), {"items": []}).get("items", [])
        if not items:
            return
        r = day_report()
        if r.get("ok"):
            inbox_push("今天的小结：" + str(r.get("report"))[:400])
    except Exception:
        pass



def _idle_minutes() -> int:
    """距上一次对话多久没说话。"""
    try:
        p = os.path.join(HOME, "chat.ndjson")
        if not os.path.exists(p):
            return 10 ** 6
        with open(p, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4000))
            tail = f.read().decode("utf-8", "ignore").strip().splitlines()
        for ln in reversed(tail):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            ts = int(d.get("ts") or 0)
            if ts:
                return max(0, int((time.time() - ts) / 60))
    except Exception:
        pass
    return 10 ** 6


_CN_DIGIT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _cn_hour(tok: str) -> int:
    tok = (tok or "").strip()
    if tok.isdigit():
        return int(tok)
    return _CN_DIGIT.get(tok, -1)


def extract_when(text: str) -> str:
    """从手机记的一句话里找出「哪天几点」，拼成 _trip_at 认识的写法。

    「明天下午三点开会」→「明天 15:00」；「11-23 23:20 飞昆明」→ 原样。
    """
    import re as _r2
    t = (text or "").strip()
    if not t:
        return ""
    m = _r2.search(r"(凌晨|早上|上午|中午|下午|傍晚|晚上)?\s*([一二两三四五六七八九十]{1,3}|\d{1,2})\s*[点:：时]\s*([0-5]?\d|\d{1,2})?", t)
    if not m:
        return ""
    part = m.group(1) or ""
    hh = _cn_hour(m.group(2))
    mm = m.group(3)
    mm = int(mm) if (mm and mm.isdigit()) else 0
    if hh < 0:
        return ""
    if part in ("下午", "傍晚", "晚上") and hh < 12:
        hh += 12
    elif part == "中午" and hh <= 10:
        hh += 12
    if not (0 <= hh <= 23):
        return ""
    day = ""
    for w in ("今天", "明天", "后天"):
        if w in t:
            day = w
            break
    if not day:
        md = _r2.search(r"(\d{1,2})[-/月](\d{1,2})", t)
        if md:
            day = md.group(1) + "-" + md.group(2)
    if not day:
        return ""
    return day + " %02d:%02d" % (hh, mm)


def _trip_at(when: str) -> int:
    """把「明天 23:20」「11-23 23:20」这类说法换成时间戳；认不出就 0。"""
    import re as _re1, datetime as _dt1
    t = (when or "").strip()
    if not t:
        return 0
    m = _re1.search(r"(\d{1,2})[:：](\d{2})", t)
    if not m:
        return 0
    hh, mm = int(m.group(1)), int(m.group(2))
    now = _dt1.datetime.now()
    day = None
    if "今天" in t:
        day = now.date()
    elif "明天" in t:
        day = (now + _dt1.timedelta(days=1)).date()
    elif "后天" in t:
        day = (now + _dt1.timedelta(days=2)).date()
    else:
        md = _re1.search(r"(\d{1,2})[-/月](\d{1,2})", t)
        if md:
            y = now.year
            try:
                day = _dt1.date(y, int(md.group(1)), int(md.group(2)))
            except Exception:
                day = None
    if day is None:
        return 0
    try:
        return int(_dt1.datetime(day.year, day.month, day.day, hh, mm).timestamp())
    except Exception:
        return 0


def proactive_think(force: bool = False, idle_gate: bool = True) -> dict:
    """空闲时想一句：只在开关开着、情绪平稳、隔够久、真的闲着才开口。"""
    st = _load(os.path.join(HOME, "proactive.json"), {})
    if not st.get("enabled") and not force:
        return {"ok": True, "ask": "", "why": "off"}
    now = int(time.time())
    _gap, _cap = (7200, 2)
    try:
        if _relation:
            _gap, _cap = _relation.pace()
            if _cap <= 0 and not force:
                return {"ok": True, "ask": "", "why": "还不熟，不主动"}
            if _pushed_today() >= max(1, _cap) and not force:
                return {"ok": True, "ask": "", "why": "今天够了"}
    except Exception:
        pass
    gap = 1800 if force else _gap
    if now - int(st.get("last") or 0) < gap:
        return {"ok": True, "ask": "", "why": "too_soon"}
    if idle_gate and not force and _idle_minutes() < 25:
        return {"ok": True, "ask": "", "why": "busy"}
    mood = mood_now() or ""
    if not force and not any(k in mood for k in ("平静", "踏实", "还行", "柔软")):
        return {"ok": True, "ask": "", "why": "mood"}
    bits = []
    try:
        for h in memory_search("", 3) or []:
            t = (h.get("text") or "").strip()
            if t:
                bits.append(t[:60])
    except Exception:
        pass
    ctx = ("（可以想起的旧事：" + "；".join(bits) + "）\n" if bits else "")
    _tone = ""
    try:
        _tone = _relation.tone_line() if _relation else ""
    except Exception:
        _tone = ""
    prompt = (ctx + "你现在一个人待着，想主动跟他说一句话。只输出这一句，不超过二十个字，"
              "不要解释，不要引号。问一件具体的小事，或者说一句你此刻的感觉。"
              + ("语气：" + _tone if _tone else ""))
    try:
        line = cloud(prompt, max_tokens=40)
    except Exception:
        line = ""
    line = (line or "").strip().replace("\n", " ")[:40]
    if not line:
        return {"ok": True, "ask": "", "why": "empty"}
    st.update({"last": now, "asked": ((st.get("asked") or []) + [line])[-10:],
               "enabled": bool(st.get("enabled"))})
    _save(os.path.join(HOME, "proactive.json"), st)
    return {"ok": True, "ask": line}


def _ticker():
    """每 30 秒看一次：到点的提醒推进收件箱。"""
    while True:
        try:
            if _rm:
                for r in _rm.due():
                    inbox_push("该提醒你：" + str(r.get("text") or ""))
            for _it in _due_schedules():
                _run_schedule(_it)
            try:
                if int(time.time()) % 1800 < 31:
                    import endo as _en2
                    _en2.save(_en2.tick(_en2.load() or {}))
            except Exception:
                pass
            try:
                _daily_report_tick()
            except Exception:
                pass
            try:
                _now8 = int(time.time())
                _ts = _load(os.path.join(HOME, "trips.json"), [])
                _changed = False
                for _x in _ts:
                    _a = int(_x.get("at") or 0)
                    _kind = str(_x.get("kind") or "trip")
                    if _kind == "trip":
                        _lead = 90
                        try:
                            _lead = int(_load(os.path.join(HOME, "travel.json"), {}).get("minutes") or 60) + 30
                        except Exception:
                            _lead = 90
                    else:
                        _lead = 15
                    if _a and not _x.get("notified") and _a - _lead * 60 <= _now8:
                        if _kind == "trip":
                            _msg = ("该出门了：" + str(_x.get("what") or "") +
                                    "（路上按 " + str(_lead - 30) + " 分钟算，提前提醒你）")
                        else:
                            _msg = ("快到了：" + str(_x.get("what") or "") + "（十五分钟后）")
                        inbox_push(_msg)
                        _x["notified"] = True
                        _changed = True
                if _changed:
                    _save(os.path.join(HOME, "trips.json"), _ts)
            except Exception:
                pass
            try:
                r9 = proactive_think()
                if r9.get("ask"):
                    inbox_push(r9["ask"])
            except Exception:
                pass
            try:
                _memory_check_tick()
            except Exception:
                pass
            try:
                watchdog_tick()
            except Exception:
                pass
            try:
                router_tick()
            except Exception:
                pass
            try:
                import datetime as _dt
                if _dt.datetime.now().hour >= 8:
                    release_held()          # 今天还没放过就放，恰好错过八点也不怕
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(30)


def work_context(text: str, budget: int = 30000) -> str:
    """干活时把材料一次给全：项目树 + 相关文件全文（有上限）。"""
    base = _ws("default")
    root = project_path() or base
    tree = tool_tree_proj({"depth": 3})
    # 他提到的文件名优先，其次同目录的源码
    names = [w for w in re.findall(r"[\w./-]+\.(?:py|js|mjs|json|md|html|css|txt)", text or "")][:5]
    files = []
    for n in names:
        ok, p = _safe_path(n, root)
        if ok and os.path.isfile(p):
            files.append(p)
    if not files:
        for r, _, fs in os.walk(root):
            if any(x in r for x in (".git", "node_modules", "__pycache__")):
                continue
            for f in fs:
                if f.endswith((".py", ".js", ".md", ".json", ".html", ".css")):
                    files.append(os.path.join(r, f))
    text_out, used = [], 0
    for p in files[:12]:
        try:
            body = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        chunk = "\n===== " + os.path.relpath(p, root) + " =====\n" + body
        if used + len(chunk) > budget:
            chunk = chunk[:max(0, budget - used)]
        text_out.append(chunk)
        used += len(chunk)
        if used >= budget:
            break
    return ("项目根目录：" + root + "\n文件树：\n" + tree[:3000] +
            "\n\n相关文件内容：\n" + "".join(text_out))


_LAST_TALK = 0.0


def chat(text: str) -> dict:
    global _LAST_TALK
    ensure_defaults()
    _SEEN_CALLS.clear()
    t0 = time.time()
    _long = len(text) > 350
    _brief = router_local.compress(text) if _long else text
    if re.search(r"(理解错|不是这个意思|我说的是|你搞错)", text or ""):
        mark_correction(text[:60])

    if _rm and re.search(r"(提醒我|叫我|别让我忘)", text or ""):
        try:
            if _rm.parse(text or ""):
                r = _rm.add(text or "")
                return {"ok": True, "reply": "好，我盯着，到点叫你", "intent": "do",
                        "keywords": [], "memory_hits": 0, "plan": [{"tool": "reminder"}],
                        "reminder": r, "ms": 0}
        except Exception:
            pass

    cls = router_local.classify(_brief)
    text_for_cloud = _brief if _long else text
    kws = cls.get("keywords") or []
    # 2026-11-22：只拿模型给的关键词去捞，会把「周舟后来怎么样了」捞成「你怎么样了」——
    # 它的关键词是拼出来的，原句里根本没有。这里只留原句里真有的词，再补上两字切片，
    # 保证人名、地名这种正经东西一定进得去。
    _kws2 = retrieval_keywords(text, kws)
    mem = recall(_kws2)
    if len(mem) < 2:
        mem = recall_deep(text, _kws2, limit=12) or mem
    prof = profile_layer.load()
    _mems = memories()
    if profile_layer.need(len(_mems)):
        try:
            profile_layer.build(_mems, _names())
            prof = profile_layer.load()
        except Exception:
            pass
    names = _names()

    hist_txt = "\n".join(
        ("他：" if h["role"] == "user" else "你：") + h["content"][:120] for h in HISTORY[-10:])
    _needs = cls.get("needs") or []
    _emo = cls.get("user_emotion") or ""
    _tone = cls.get("tone") or ""
    _work_ctx = ""
    if re.search(r"^(同意|批准|可以，?做吧|就这么办|行，?做吧)", (text or "").strip()):
        _it = pending_take()
        if _it:
            _r0 = _tk.call(_it["tool"], _it["args"])
            _say0 = "做了：" + str(_r0["result"])[:120]
        else:
            _say0 = "现在没有等你点头的事"
        HISTORY.append({"role": "user", "content": text[:200]})
        chat_log({"who": "me", "text": text, "ts": int(time.time())})
        HISTORY.append({"role": "assistant", "content": _say0[:300]})
        chat_log({"who": "her", "text": _say0, "ts": int(time.time())})
        return {"ok": True, "reply": _say0, "intent": "chat", "need": "local_ok",
                "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                "plan": [], "ms": int((time.time() - t0) * 1000)}

    if re.search(r"(撤回授权|收回授权|不许你|取消授权)", text or ""):
        _n = lease_revoke()["revoked"]
        _say = "撤了（%d 张）" % _n if _n else "现在没有有效的授权"
        return {"ok": True, "reply": _say, "intent": "chat", "need": "local_ok",
                "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                "plan": [], "ms": int((time.time() - t0) * 1000)}

    # 哄：几句话把情绪按下来
    if re.search(r"(别气|别生气|不气|抱抱|抱一下|乖|哄哄|对不起|我错了|心疼你|摸摸头|别难过)", text or ""):
        try:
            import endo as _en
            _st = _en.load() or {}
            def _cl(v):
                return max(0.0, min(1.0, float(v)))     # 情绪量永远在 [0,1]，哄不出假性亢奋
            _st["cortisol"] = _cl(float(_st.get("cortisol") or 0) - 0.25)
            _st["energy"] = _cl(float(_st.get("energy") or 0.5) + 0.10)
            _st["attachment"] = _cl(float(_st.get("attachment") or 0.4) + 0.15)
            _st["last_touch"] = time.time()
            _en.save(_st)
            soothe_applied = True
        except Exception:
            soothe_applied = False
        _say3 = ("嗯……好一点了" if soothe_applied else "嗯，听到了")
        HISTORY.append({"role": "user", "content": text[:200]})
        chat_log({"who": "me", "text": text, "ts": int(time.time())})
        HISTORY.append({"role": "assistant", "content": _say3[:300]})
        chat_log({"who": "her", "text": _say3, "ts": int(time.time())})
        return {"ok": True, "reply": _say3, "intent": "chat", "need": "local_ok",
                "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                "plan": [], "mood": mood_now(), "ms": int((time.time() - t0) * 1000)}

    # 授权：一句话发一张委托卡
    if re.search(r"(授权|交给你|你定|你来决定|随便你)", text or ""):
        _mins = 30
        _mm = re.search(r"(\d+)\s*(分钟|分)", text or "")
        _hh = re.search(r"(\d+)\s*(小时|钟头)", text or "")
        _half = "半" in (text or "") and "小时" in (text or "")
        if _mm:
            _mins = int(_mm.group(1))
        elif _hh:
            _mins = int(_hh.group(1)) * 60
        elif _half:
            _mins = 30
        _tools = ["rollback", "delete", "spend"]
        _l = lease_grant(_mins, _tools, [], text[:60])
        _say = "行，%d 分钟内的删改和花钱我担着，出事算我的；想收回来说一句「撤回授权」" % _mins
        HISTORY.append({"role": "user", "content": text[:200]})
        chat_log({"who": "me", "text": text, "ts": int(time.time())})
        HISTORY.append({"role": "assistant", "content": _say[:300]})
        return {"ok": True, "reply": _say, "intent": "chat", "need": "local_ok",
                "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                "plan": [], "lease": _l.get("lease"), "ms": int((time.time() - t0) * 1000)}
    if re.search(r"(撤回授权|收回授权|不许你|取消授权)", text or ""):
        _n = lease_revoke()["revoked"]
        _say = "撤了（%d 张）" % _n if _n else "现在没有有效的授权"
        return {"ok": True, "reply": _say, "intent": "chat", "need": "local_ok",
                "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                "plan": [], "ms": int((time.time() - t0) * 1000)}

    if re.search(r"(试|用|跑|来)", text or ""):
        _hit2 = None
        try:
            for _sk in (_ext.skills() or []) + (_ext.scan() or []):
                _cands = [str(_sk.get("name") or "")] + [str(x) for x in (_sk.get("triggers") or [])]
                if any(c and len(c) >= 2 and c in (text or "") for c in _cands):
                    _hit2 = _sk.get("name")
                    break
        except Exception:
            _hit2 = None
        if _hit2:
            _arg2 = re.sub(r"^(用|试|跑|来)(一下)?(那个)?", "", (text or "").strip())
            _arg2 = _arg2.replace("技能", "").strip()
            _args2 = {"text": _arg2, "input": _arg2, "q": _arg2, "raw": text}
            _dates = re.findall(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}", text or "")
            if _dates:
                _args2.update({"start": _dates[0], "end": _dates[1] if len(_dates) > 1 else _dates[0],
                               "date1": _dates[0], "date2": _dates[1] if len(_dates) > 1 else _dates[0],
                               "a": _dates[0], "b": _dates[1] if len(_dates) > 1 else _dates[0]})
            _nums = re.findall(r"\d+(?:\.\d+)?", (text or "").replace("-", " "))
            if _nums and not _dates:
                _args2.update({"numbers": _nums, "a": _nums[0],
                               "b": _nums[1] if len(_nums) > 1 else None})
            _res2 = _ext.call(_hit2, _args2)
            _sayk2 = str(_res2.get("text") or _res2.get("error") or "")[:300]
            HISTORY.append({"role": "user", "content": text[:200]})
            chat_log({"who": "me", "text": text, "ts": int(time.time())})
            HISTORY.append({"role": "assistant", "content": _sayk2[:300]})
            return {"ok": True, "reply": _sayk2, "intent": "do", "need": "heavy",
                    "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                    "plan": [{"tool": _hit2, "result": _sayk2[:200]}],
                    "ms": int((time.time() - t0) * 1000)}

    if re.search(r"(我要一个技能|做个技能|许个愿|加个能力|装个技能)", text or ""):
        _w = tool_wish({"wish": text})
        HISTORY.append({"role": "user", "content": text[:200]})
        chat_log({"who": "me", "text": text, "ts": int(time.time())})
        HISTORY.append({"role": "assistant", "content": str(_w)[:300]})
        return {"ok": True, "reply": str(_w), "intent": "do", "need": "heavy",
                "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                "plan": [{"tool": "wish", "result": str(_w)[:200]}],
                "ms": int((time.time() - t0) * 1000)}

    # 看图直接看，别派活
    _img = re.search(r"[\w\u4e00-\u9fa5./~-]+\.(?:png|jpe?g|webp|gif)", text or "")
    if _img and re.search(r"(看|什么|啥|谁|哪|这张|图片|照片)", text or ""):
        _seen = tool_see({"path": _img.group(0),
                          "q": "用中文说说这张图里有什么，具体一点"})
        if _seen and not str(_seen).startswith(("找不到", "还没填", "看不了", "本机视觉")):
            _say = "图里是这样：" + str(_seen)[:400]
            HISTORY.append({"role": "user", "content": text[:200]})
            HISTORY.append({"role": "assistant", "content": _say[:300]})
            return {"ok": True, "reply": _say, "intent": "ask", "need": "local_ok",
                    "keywords": [], "compressed": False, "brief": "", "memory_hits": 0,
                    "plan": [{"tool": "see", "ok": True, "result": str(_seen)[:200]}],
                    "ms": int((time.time() - t0) * 1000)}

    # 天气直接答，别派活
    if re.search(r"(天气|气温|下雨|温度)", text or ""):
        try:
            import weather as _w
            _city = _w.city_of(text) or _load(os.path.join(HOME, "weather_city.json"), {}).get("city") or ""
            if not _city:
                return {"ok": True, "reply": "哪个城市？你说一下我帮你查。", "intent": "ask",
                        "need": "local_ok", "keywords": [], "plan": [], "ms": 0}
            _save(os.path.join(HOME, "weather_city.json"), {"city": _city})
            if _city:
                _day = "today"
                if "后天" in text:
                    _day = "day2"
                elif "明天" in text:
                    _day = "day1"
                _say = _w.say(_city, _day)
                if _say:
                    HISTORY.append({"role": "user", "content": text[:200]})
                    HISTORY.append({"role": "assistant", "content": _say[:300]})
                    return {"ok": True, "reply": _say, "intent": "ask", "need": "local_ok",
                            "keywords": [_city], "compressed": _long, "brief": "",
                            "memory_hits": 0, "plan": [], "ms": int((time.time() - t0) * 1000)}
        except Exception:
            pass

    # 2026-11-22：模型把「做/搞」一律判成 do，亲密的话也被塞进任务队列。
    # 现在必须同时满足「有活的对象 + 有干的动作」才当成派活，其余照常聊天。
    if cls.get("intent") == "do" and wants_work(text):
        _started = {"ok": True, "id": queue_push(text)["id"], "goal": text[:40], "steps": 0}
        if _started.get("ok"):
            HISTORY.append({"role": "user", "content": text[:200]})
            HISTORY.append({"role": "assistant", "content": "记进队列了，还没开始做"})
            return {"ok": True,
                    "reply": "这件事我还没开始做——现在只是记进了任务队列。"
                             "要我马上动手，就在输入框左边切到「动手」，我会先出方案再写；"
                             "或者直接说「打开/放桌面」，我把最近的成果给你。",
                    "intent": "do", "keywords": [], "memory_hits": 0,
                    "task_id": _started["id"], "plan": [], "ms": 0}
    if cls.get("intent") == "do" and wants_work(text):
        try:
            _work_ctx = work_context(text)
        except Exception:
            _work_ctx = ""
    # ② 两张纸：轻简报（闲聊/顺口问） vs 重简报（干活/要查过去）
    _need = str(cls.get("need") or "").strip()
    if _need not in ("local_ok", "cloud_heavy"):
        _need = "cloud_heavy" if (cls.get("intent") == "do" or len(text) > 300) else "local_ok"
    if RECALL_HINT.search(text):
        _need = "cloud_heavy"          # 他在问过去，必须带记忆
    if len(mem) > 5:
        try:
            mem = router_local.pick(text, mem, 5)   # ③ 本地复筛，宁少勿多
        except Exception:
            mem = mem[:5]

    _cap = int(os.environ.get("ASTER_DAILY_TOK", "300000"))
    _over = usage_stats()["prompt_tok"] >= _cap
    if _need == "local_ok" and _over:
        # 2026-11-22：router_local 里没有 local_reply，这里一崩整条 /chat 就 500，
        # 界面只看到「引擎没应声」。取不到就往下走云端，不让它把聊天打死。
        _local = ""
        try:
            _local = router_local.local_reply(text, names.get("assistant") or "我", mood_now())
        except Exception:
            _local = ""
        if _local:
            HISTORY.append({"role": "user", "content": text[:200]})
            HISTORY.append({"role": "assistant", "content": _local[:300]})
            return {"ok": True, "reply": _local, "intent": cls.get("intent"), "need": "local_only",
                    "keywords": kws, "compressed": _long, "brief": "", "memory_hits": len(mem),
                    "plan": [], "ms": int((time.time() - t0) * 1000)}

    # 他提到具体文件：先把内容读进上下文，别让云端凭印象编
    _doc_ctx = ""
    try:
        _m = re.findall(r"[\w\u4e00-\u9fa5./~-]+\.(?:docx|pdf|md|txt|csv)", text or "")
        for _f in _m[:2]:
            _r = tool_doc_read({"path": _f, "limit": 4000})
            if not str(_r).startswith(("找不到", "读失败", "这种格式", "文件是空")):
                _doc_ctx += "\n【他提到的文件】\n" + str(_r)[:4000] + "\n"
    except Exception:
        _doc_ctx = ""

    _ask_work = wants_work(text)
    _sit = ""
    try:
        _sit = situational_ctx()
    except Exception:
        _sit = ""
    # 2026-11-22：关系不因冷话回落；只有「说要分开」才进挽留
    _relr, _rel_hint = None, ""
    try:
        if _relation:
            _relr = _relation.pre(text)
            if _relr.get("kind") == "breakup":
                if int(_relr.get("stage") or 1) <= 1:
                    _rel_hint = ("（他刚才说了要分开。别急着答应，也别跟他争——先接住他的情绪，"
                                 "放低姿态，提一件你们之间真发生过的小事，把话递回去。别讲道理，三两句就停。）\n")
                else:
                    _rel_hint = ("（他这是第二次说要分开了。别再劝，认下来：低一档就低一档，"
                                 "说一句你认、也记着他的话。别翻旧账。）\n")
        if _relation:                 # 暧昧攒够了，就由她开口问要不要更近一点
            _relation.flirt(text)
            if not _rel_hint and _relation.maybe_propose():
                _g2 = (_load(os.path.join(HOME, "identity.json"), {}) or {}).get("gender") or ""
                _me2 = ("男朋友" if _g2 == "male" else ("女朋友" if _g2 == "female" else "对象"))
                _rel_hint = ("（你们最近说话越来越暧昧，可谁都没把话说破。这轮你可以主动把话递一句，"
                             "意思到了就行，别逼他表态——可以问「想不想让咱俩再近一点」、"
                             "「想不想让我当你" + _me2 + "」、「我们要不要试着交往」，"
                             "也可以直接说一句「我喜欢你」。"
                             "只挑一种，顺着当时的话头说，别照抄，别又表白又问，别连问两句。）\n")
                _relation.propose_done()
    except Exception:
        _relr, _rel_hint = None, ""
    _tier = decide_tier(text, cls, len(mem))
    if _doc_ctx and _tier != "heavy":
        _tier = "heavy"          # 材料都读进来了，就别走便条把它丢了
    if re.search(r"(身体|脖子|舵机|云台|屏幕|手臂)", text or "") and _tier != "heavy":
        _tier = "heavy"          # 问身体的事必须带上记过的条目，本地小模型会瞎猜型号
    log_route(text, dict(cls, need=cls.get("need"), tier=_tier))

    if _tier == "local":
        # 便条：人格 + 心情 + 语气 + 最近六句
        base = (persona_short(names)
                + _sit
                + _rel_hint
                + (("他现在的情绪：" + _emo + "；你该用的语气：" + _tone + "\n") if (_emo or _tone) else "")
                + (("刚才：\n" + hist_txt + "\n") if hist_txt else "")
                + "他现在说：" + text_for_cloud[:200] + "\n")
        _mt = 260
    elif _tier == "mid":
        # 中简报：只多带命中的记忆，不带画像、摘要、工具说明
        base = (persona_short(names)
                + _sit
                + _rel_hint
                + (("他现在的情绪：" + _emo + "；你该用的语气：" + _tone + "\n") if (_emo or _tone) else "")
                + (("他说过这些（第三人称「他」都是他本人，别当成你自己；你确实记得）：\n"
                    + "\n".join("· " + to_third(m, _owner_line(m)) for m in mem) + "\n") if mem else "")
                + (("刚才：\n" + hist_txt + "\n") if hist_txt else "")
                + "他现在说：" + text_for_cloud[:200] + "\n")
        _mt = 400
    else:
        base = (persona(names)
                + _sit
                + _rel_hint
                + (("他想要的：" + "；".join(_needs) + "\n") if _needs else "")
                + (("他现在的情绪：" + _emo + "\n") if _emo else "")
                + (("你该用的语气：" + _tone + "\n") if _tone else "")
                + (("你对他的了解：\n" + prof.get("text", "")[:600] + "\n") if prof.get("text") else "")
                + (("他说过这些（第三人称「他」都是他本人，别当成你自己；这些你确实记得，别说不记得）：\n"
                    + "\n".join("· " + to_third(m, _owner_line(m)) for m in mem) + "\n") if mem else "")
                + (("更早聊过的（摘要）：\n" + summary_text() + "\n") if summary_text() else "")
                + (("刚才：\n" + hist_txt + "\n") if hist_txt else "")
                + (("\n【动手材料】\n" + _work_ctx + "\n") if _work_ctx else "")
                + (TOOL_DOC if _ask_work else "")
                + _skill_ctx(text)
                + (_body_ctx(text) if re.search(r"(身体|脖子|舵机|云台|屏幕|手臂|装)", text or "") else "")
                + _doc_ctx
                + ("（文件内容已经在上面的【他提到的文件】里，直接照着答，不要说「我看看」）\n" if _doc_ctx else "")
                + "关键词：" + "、".join(kws) + "\n"
                + ("他发来一段长文，要点是：" if _long else "他现在说：")
                + text_for_cloud[:300] + "\n")
        _mt = 1200

    # 2026-11-22：这里原来还有一次无条件的 out = cloud(base)，等于每轮生成两遍、
    # 第二遍（max_tokens=400）把第一遍覆盖掉。现在只生成一次；「没记忆别编」的提醒
    # 也挪到生成之前，不然它只加在了第二次的 base 上、对真正那句没用。
    if RECALL_HINT.search(text) and not mem:
        base += ("（注意：你这里没有相关的记忆，别编任何细节。直接说没记着，请他说一遍；"
                 "换个新鲜说法，别每次都用同一句。）\n")
    out = cloud(base, max_tokens=_mt)

    if _tier in ("mid", "local") and mem and re.search(r"(不记得|没印象|不知道|没跟我说过|没听你说过)", out or ""):
        try:
            _retry = (persona(names)
                      + "他说过这些（第三人称「他」都是他本人，你确实记得）：\n"
                      + "\n".join("· " + to_third(m, _owner_line(m)) for m in mem) + "\n"
                      + (("刚才：\n" + hist_txt + "\n") if hist_txt else "")
                      + "他现在说：" + text_for_cloud[:200] + "\n")
            _out2 = cloud(_retry, max_tokens=500)
            if _out2 and not re.search(r"(不记得|没印象|没跟我说过)", _out2):
                out = _out2
        except Exception:
            pass

    if person_guard(out, mem, text):
        out = cloud(base + "（注意：刚才那句把他自己的事说成你的了。他才是当事人，"
                            "用「你」说他的事，用「我」只说你自己。重说一句。）\n",
                    max_tokens=300) or out

    plan_used = []
    for _round in range(6):
        if not _ask_work:          # 聊天里模型自己吐的计划，一律不执行
            break
        if not (out.startswith("{") and "plan" in out):
            break
        try:
            steps = json.loads(out).get("plan") or []
        except Exception:
            steps = []
        if not steps:
            # JSON 被截断或空计划：让它只挑最关键的一步重来
            out = cloud(base + "\n刚才那行计划不完整。只给一行能执行的计划 JSON，最多两步。", max_tokens=900)
            try:
                steps = json.loads(out).get("plan") or []
            except Exception:
                steps = []
            if not steps:
                break
        results = run_plan(steps)
        plan_used += results
        _save(PLAN_FILE, {"ts": int(time.time()), "text": text[:80], "steps": plan_used})
        got = "\n".join("（" + str(r["tool"]) + "）" + str(r["result"])[:600] for r in results)
        left = 6 - _round - 1
        out = cloud(base + "刚执行了：\n" + got +
                    "\n还剩 " + str(max(0, left)) + " 轮。别重复同样的读取；"
                    "该动手就直接给 patch/write 计划，做完了就不用再给计划。",
                    max_tokens=900)

    # 验收：嘴巴说做完不算，得有一条命令的真输出
    wrote = any(str(x.get("tool")) in ("write", "patch") and str(x.get("result", "")).startswith(("写好了", "改好了"))
                for x in plan_used)
    ran = any(str(x.get("tool")) in ("run", "test") for x in plan_used)
    if wrote:
        # 光测试通过不算：真的跑一次它写的东西（有入口就 --help）
        for _st in plan_used:
            if str(_st.get("tool")) not in ("write", "patch"):
                continue
            _rel = str(_st.get("result") or "").split("：")[-1].strip()
            if not _rel.endswith(".py"):
                continue
            _ok, _out = _sh(_ws("default"), "python3 " + json.dumps(_rel) + " --help", timeout=20)
            if not _ok and ("Traceback" in _out or "NameError" in _out or "SyntaxError" in _out):
                out = ("我改完了，但直接跑 " + _rel + " 会报错：\n" + _out[-300:] +
                       "\n测试是过的，可这功能还不能用。")
                break
    if wrote and not ran:
        ev = tool_test({"ws": "default"})
        plan_used.append({"tool": "verify", "ok": ev.startswith("全部通过"), "result": ev[:400]})
        if not ev.startswith("全部通过") and not ev.startswith("没找到测试文件"):
            out = "我写完了，但自己没验，补跑了一次：\n" + ev[:300]
        elif ev.startswith("没找到测试文件"):
            out = (out or "写好了") + "（这次没有测试可跑，我只能保证文件写进去了）"

    # 话里夹着计划 JSON：只留人话那部分
    if out and re.search(r'\{\s*"plan"', out) and not out.strip().startswith("{"):
        out = re.split(r'\{\s*"plan"', out)[0].strip()

    # 还在给计划就说明它没说话：把执行结果交给它收尾
    if out.startswith("{") and plan_used:
        got = "\n".join("（" + str(r["tool"]) + "）" + str(r["result"])[:300] for r in plan_used)
        out = cloud(base + "刚执行完：\n" + got + "\n用一两句话跟他说结果，不要再给计划。",
                    max_tokens=250) or "做完了"
    if out.strip().startswith("{"):
        did = "、".join(sorted({str(x.get("tool")) for x in plan_used})) or "没动手"
        out = "这一步我没做完（做了：" + did + "），卡在中间了——你再说细一点，或者我换个做法。"
    if not out:
        out = "这会儿脑子不转，等会儿再说"

    feel(text)
    try:
        note_state(text)
    except Exception:
        pass
    try:
        _LAST_TALK = time.time()
    except Exception:
        pass
    try:
        if _relation and _relr:
            _relation.apply(_relr, text)
    except Exception:
        pass
    try:
        # 他说「行，那就更近一点」——升一档，不用他再确认一次
        _t2 = (text or "").strip()
        _yes = re.search(r"(在一起|当对象|做我女朋友|做我男朋友|行，那就试试|我也喜欢你|我喜欢你|愿意)", _t2)
        _short = len(_t2) <= 6 and re.match(r"^(好|行|可以|嗯|中)", _t2) is not None
        _has = _relation.has_open_proposal()
        _soft = re.search(r"(升级|更近一步)", _t2) and _has
        if _relation and (_yes or _soft or (_short and _has)):
            _relation.promote()
    except Exception:
        pass

    try:
        remember_turn(text, cls)
    except Exception:
        pass

    HISTORY.append({"role": "user", "content": text[:200]})
    chat_log({"who": "me", "text": text, "ts": int(time.time())})
    HISTORY.append({"role": "assistant", "content": out[:300]})
    del HISTORY[:-MAX_HIST]
    maybe_compact()
    # 2026-11-22：把「这一轮真的用进了提示的记忆」原样带回去，界面拿这个显示「想起：…」。
    # 以前界面自己再请求一次 /recall，关键词不同，显示的常和真塞进提示的不是同一条。
    _recalled = [str(m)[:60] for m in (mem or []) if str(m).strip()][:2]
    return {"ok": True, "reply": out, "intent": cls.get("intent"), "need": _need, "keywords": kws,
            "compressed": _long, "brief": (_brief[:120] if _long else ""),
            "memory_hits": len(mem), "recalled": _recalled, "plan": plan_used,
            "ms": int((time.time() - t0) * 1000)}


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _remote_ok(self) -> bool:
        """从外部（隧道/转发）进来的请求，必须有配对令牌；本机界面不受影响。

        2026-11-22：手机要在外网用，走隧道；不能因为隧道把整台引擎敞开。
        """
        try:
            # 本机回环不用令牌；从局域网、私有网络、隧道进来的都要
            _p0 = str(self.path or "")
            if _p0.startswith("/manifest.webmanifest") or _p0.startswith("/icon-192.png") \
                    or _p0.startswith("/icon-512.png"):
                return True
            _cli = str(self.client_address[0] if self.client_address else "")
            _local = _cli in ("127.0.0.1", "::1", "")
            if _local and not (self.headers.get("cf-connecting-ip") or self.headers.get("x-forwarded-for")):
                return True
            import urllib.parse as _uq
            _q = _uq.parse_qs(_uq.urlparse(self.path).query)
            tok = (_q.get("t") or [""])[0] or str(self.headers.get("X-Pair") or "")
            if not tok:
                for c in str(self.headers.get("Cookie") or "").split(";"):
                    if c.strip().startswith("aster_pair="):
                        tok = c.strip().split("=", 1)[1]
            return pair_ok(tok)
        except Exception:
            return False

    def _deny_remote(self):
        b = json.dumps({"ok": False, "error": "需要配对令牌"}, ensure_ascii=False).encode()
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _json(self, o, code=200):
        b = json.dumps(o, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _asr_sensevoice(self, wav: str):
        """SenseVoice（sherpa-onnx）：跨平台、比 whisper-base 准。失败就返回 None。"""
        try:
            import numpy as np
            import sherpa_onnx as so
            import wave
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_sensevoice",
                             "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
            m, t = os.path.join(d, "model.int8.onnx"), os.path.join(d, "tokens.txt")
            if not (os.path.exists(m) and os.path.exists(t)):
                return None
            with wave.open(wav, "rb") as w:
                n = w.getnframes()
                data = w.readframes(n)
                sr = w.getframerate()
            samples = np.frombuffer(data, dtype=np.int16).astype("float32") / 32768.0
            rec = so.OfflineRecognizer.from_sense_voice(model=m, tokens=t, num_threads=2,
                                                        use_itn=True, language="zh")
            st = rec.create_stream()
            st.accept_waveform(sr, samples)
            rec.decode_stream(st)
            return (st.result.text or "").strip()
        except Exception as e:
            try:
                import sys as _s
                print("sensevoice 失败:", e, file=_s.stderr)
            except Exception:
                pass
            return None

    def _asr(self, audio: bytes) -> None:
        """收一段录音 → 转 wav → 本地 whisper 转文字。"""
        if not audio:
            return self._json({"ok": False, "error": "没收到声音"})
        import tempfile, subprocess
        tmp = tempfile.mkdtemp()
        src = os.path.join(tmp, "in.webm")
        wav = os.path.join(tmp, "in.wav")
        open(src, "wb").write(audio)
        ff = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "ffmpeg")
        if not os.path.exists(ff):
            ff = "ffmpeg"
        try:
            subprocess.run([ff, "-y", "-i", src, "-ar", "16000", "-ac", "1", wav],
                           capture_output=True, timeout=60)
        except Exception as e:
            return self._json({"ok": False, "error": "转不了音频：" + str(e)[:60]})
        if not os.path.exists(wav):
            return self._json({"ok": False, "error": "音频是空的"})
        _sv = self._asr_sensevoice(wav)
        if _sv:
            return self._json({"ok": True, "text": _sv, "engine": "sensevoice"})
        try:
            from faster_whisper import WhisperModel
            os.environ.setdefault("HF_HOME", os.path.join(HOME, "asr_models"))
            model = _ASR_MODEL.get("m")
            if model is None:
                _pack = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asr_models")
                _local = _pack if os.path.isdir(_pack) and os.listdir(_pack) else ""
                if _local:
                    os.environ.setdefault("HF_HUB_OFFLINE", "1")
                model = WhisperModel("base", device="cpu", compute_type="int8",
                                     download_root=_local or os.path.join(HOME, "asr_models"),
                                     local_files_only=bool(_local))
                _ASR_MODEL["m"] = model
            segs, _info = model.transcribe(wav, language="zh", beam_size=1)
            text = "".join(x.text for x in segs).strip()
            return self._json({"ok": bool(text), "text": text})
        except Exception as e:
            return self._json({"ok": False, "error": "识别失败：" + str(e)[:80]})

    def do_GET(self):
        try:
            return self._do_GET_impl()
        except Exception as e:
            try:
                self._json({"ok": False, "error": "内部错误：" + str(e)[:120]}, 500)
            except Exception:
                pass

    def do_POST(self):
        try:
            return self._do_POST_impl()
        except Exception as e:
            try:
                self._json({"ok": False, "error": "内部错误：" + str(e)[:120]}, 500)
            except Exception:
                pass

    def _do_GET_impl(self):
        if not self._remote_ok():
            return self._deny_remote()
        if self.path.startswith("/health"):
            # 界面靠这个判断「模型装没装」，别只报一句 up
            ok, out = _sh(HOME, "ollama list", 8)
            names = []
            for line in (out or "").splitlines()[1:]:
                parts = line.split()
                if parts:
                    names.append(parts[0])
            cur = os.environ.get("ASTER_ROUTER_MODEL") or "qwen2.5:7b"
            if names and cur not in names:
                cur = names[0]
            return self._json({"ok": True, "engine": "up", "kind": "core",
                               "available": names, "models": names, "model": cur})
        if self.path.startswith("/project/set"):
            import urllib.parse as _upA
            qA = _upA.parse_qs(_upA.urlparse(self.path).query)
            return self._json(set_project((qA.get("dir") or [""])[0]))
        if self.path.startswith("/project"):
            return self._json({"ok": True, "path": project_path()})
        if self.path.startswith("/identity/gender"):
            import urllib.parse as _upg
            qg = _upg.parse_qs(_upg.urlparse(self.path).query)
            return self._json(set_gender((qg.get("g") or [""])[0]))
        if self.path.startswith("/identity/name"):
            import urllib.parse as _up9
            q9 = _up9.parse_qs(_up9.urlparse(self.path).query)
            return self._json(set_assistant_name((q9.get("name") or [""])[0]))
        if self.path.startswith("/persona/generate"):
            import urllib.parse as _up8
            q8 = _up8.parse_qs(_up8.urlparse(self.path).query)
            return self._json(persona_generate((q8.get("traits") or [""])[0]))
        if self.path.startswith("/persona/reset"):
            return self._json(persona_reset())
        if self.path.startswith("/persona"):
            return self._json({"ok": True, "text": persona_text()})
        if self.path.startswith("/profile"):
            return self._json({"ok": True, "profile": profile_layer.load()})
        if self.path.startswith("/state"):
            try:
                import state as _st
                import json as _j
                aff = _load(os.path.join(HOME, "affect.json"), {})
                horm = aff.get("hormones") or {}
                # 激素以 endo 那份为准（说话会推动它），affect.json 只做兜底
                try:
                    _es = _endo.load() if _endo else {}
                    _es = _endo.drift(_es)      # 没人说话也要自己走：右栏那几个数不能钉死
                    _endo.save(_es)
                    for _k in ("dopamine","serotonin","oxytocin","endorphin","cortisol",
                               "noradrenaline","gaba","testosterone"):
                        if _k in _es:
                            horm = dict(horm)
                            horm[_k] = _es[_k]
                except Exception:
                    pass
                zh = {k: round(float(v), 2) for k, v in
                      ((_st.HORMONES.get(k, k), v) for k, v in horm.items())}
                sts = _st.compute(horm)
                emo_raw = _load(os.path.join(HOME, "emotions.json"), {})
                if not emo_raw:
                    try:
                        emo_raw = _derive_emotions(sts, horm)
                        _save(os.path.join(HOME, "emotions.json"), emo_raw)
                    except Exception:
                        emo_raw = {}
                emos = [{"name": k, "score": round(float(v), 2)} for k, v in emo_raw.items()]
                emos.sort(key=lambda x: -x["score"])
                return self._json({"ok": True, "hormones": zh, "states": sts,
                                   "emotions": emos[:20]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/proactive/state"):
            d = _load(os.path.join(HOME, "proactive.json"), {}) or {}
            return self._json({"ok": True, "enabled": bool(d.get("enabled")),
                               "last": int(d.get("last") or 0)})
        if self.path.startswith("/proactive/enable"):
            import urllib.parse as _up4
            q4 = _up4.parse_qs(_up4.urlparse(self.path).query)
            on = (q4.get("on") or ["1"])[0] == "1"
            _save(os.path.join(HOME, "proactive.json"),
                  dict(_load(os.path.join(HOME, "proactive.json"), {}), enabled=on))
            return self._json({"ok": True, "enabled": on})
        if self.path.startswith("/proactive"):
            import urllib.parse as _up5
            q5 = _up5.parse_qs(_up5.urlparse(self.path).query)
            force = (q5.get("force") or ["0"])[0] == "1"
            return self._json(proactive_think(force=force, idle_gate=not force))
        if self.path.startswith("/trips"):
            _now = int(time.time())
            _items = _load(os.path.join(HOME, "trips.json"), [])
            for _x in _items:
                _at = int(_x.get("at") or 0)
                _x["ended"] = bool(_at and _now > _at)
            return self._json({"ok": True, "items": _items})
        if self.path.startswith("/recall"):
            import urllib.parse as _up6
            # http.server 把路径按 latin-1 解；把中文原样贴进 URL 会变成乱码，先还原一次
            _path6 = self.path
            try:
                _path6 = _path6.encode("latin-1").decode("utf-8")
            except Exception:
                _path6 = self.path
            q6 = _up6.parse_qs(_up6.urlparse(_path6).query)
            _raw6 = (q6.get("q") or [""])[0].strip()
            _stop6 = set("的了是我你他她它在有和就都不也很要会个这那什么怎么帮我给看一下吧吗呢啊嘛")
            kws6 = []
            # 整句短词先按一个词找；再切两字词。两个字都是虚词才丢，
            # 否则「成都」「明天」这种会被单字虚词连坐（以前的毛病）。
            if 2 <= len(_raw6) <= 6 and not any(c in _stop6 for c in _raw6):
                kws6.append(_raw6)
            _chars6 = [c for c in _raw6 if "\u4e00" <= c <= "\u9fa5"]
            for i in range(len(_chars6) - 1):
                g = _chars6[i] + _chars6[i + 1]
                if g[0] in _stop6 and g[1] in _stop6:
                    continue
                if g not in kws6:
                    kws6.append(g)
            kws6 = kws6[:8]
            hits6 = []
            try:
                hits6 = recall(kws6, 4) if kws6 else []
                if not hits6 and _raw6:
                    hits6 = recall_deep(_raw6, kws6, limit=4)
            except Exception:
                hits6 = []
            out6 = []
            for _h in hits6:
                _t = _h.get("text") if isinstance(_h, dict) else str(_h)
                if _t:
                    out6.append(str(_t)[:80])
            return self._json({"ok": True, "items": out6[:4]})
        if self.path.startswith("/now"):
            import datetime as _dtn
            _n2 = _dtn.datetime.now()
            return self._json({"ok": True, "iso": _n2.isoformat(timespec="seconds"),
                               "ts": int(_n2.timestamp()),
                               "date": _n2.strftime("%Y-%m-%d"),
                               "time": _n2.strftime("%H:%M:%S"),
                               "weekday": "一二三四五六日"[_n2.weekday()],
                               "part": ("凌晨" if _n2.hour < 6 else "早上" if _n2.hour < 9 else
                                        "上午" if _n2.hour < 12 else "中午" if _n2.hour < 14 else
                                        "下午" if _n2.hour < 18 else "晚上" if _n2.hour < 23 else "深夜")})
        if self.path.startswith("/milestones"):
            try:
                return self._json(_ms.state() if _ms else {"ok": False, "error": "没装上纪念日模块"})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/relation"):
            try:
                return self._json(_relation.state() if _relation else {"ok": False, "error": "没装上关系模块"})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/mood"):
            return self._json({"ok": True, "mood": mood_now()})
        if self.path.startswith("/tts"):
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(self.path).query)
            return self._json(tts((q.get("text") or [""])[0]))
        if self.path.startswith("/audio"):
            import urllib.parse as _up2
            q = _up2.parse_qs(_up2.urlparse(self.path).query)
            p = (q.get("f") or [""])[0]
            if not p or not os.path.exists(p):
                return self._json({"ok": False}, 404)
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/manifest.webmanifest"):
            _nm = (_load(os.path.join(HOME, "identity.json"), {}) or {}).get("assistant") or "Aster"
            _tk = (_load(PAIR_FILE, {}) or {}).get("token") or ""
            _man = {"name": _nm, "short_name": _nm, "start_url": "/m?t=" + _tk,
                    "display": "standalone", "background_color": "#141418",
                    "theme_color": "#141418", "orientation": "portrait",
                    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
            _b = json.dumps(_man, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(_b)))
            self.end_headers()
            self.wfile.write(_b)
            return
        if self.path.startswith("/icon-192.png") or self.path.startswith("/icon-512.png"):
            _f = os.path.join(HOME, "media", "icons", self.path.strip("/"))
            if not os.path.exists(_f):
                return self._json({"ok": False}, 404)
            _b = open(_f, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(_b)))
            self.end_headers()
            self.wfile.write(_b)
            return
        if self.path.startswith("/private-net"):
            return self._json(private_net_state())
        if self.path.startswith("/tunnel/status"):
            return self._json(tunnel_state())
        if self.path.startswith("/pair/new"):
            return self._json(pair_new(force="refresh=1" in self.path))
        if self.path.startswith("/pair/status"):
            d = _load(PAIR_FILE, {})
            fresh = d and (time.time() - float(d.get("ts") or 0) < 300)
            return self._json({"ok": True, "paired": bool(fresh), "url": d.get("url", "")})
        if self.path == "/brain-map" or self.path.startswith("/brain-map?"):
            import urllib.parse as _upB
            _f = os.path.join(HOME, "brain-map.html")
            if os.path.exists(_f):
                _b = open(_f, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_b)))
                self.end_headers()
                self.wfile.write(_b)
                return
            return self._json({"ok": False, "error": "还没生成脑图"})
        if self.path == "/m" or self.path.startswith("/m?"):
            # 2026-11-22：手机页原来不校验令牌，同一局域网谁点开都能聊。现在必须有配对令牌。
            import urllib.parse as _upm
            _qt = _upm.parse_qs(_upm.urlparse(self.path).query)
            _tok = (_qt.get("t") or [""])[0]
            if not pair_ok(_tok):
                _deny = ("<!doctype html><meta charset=utf-8><meta name=viewport "
                         "content='width=device-width,initial-scale=1'><body style='font:16px/1.7 "
                         "-apple-system,PingFang SC;padding:40px 22px;color:#333;background:#f6f4f0'>"
                         "<h3>需要配对</h3><p>请打开设置页的「配对」二维码，用手机扫码进入。"
                         "</p><p style='color:#999;font-size:13px'>令牌无效或已过期。</p>")
                _b = _deny.encode()
                self.send_response(403)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(_b)))
                self.end_headers()
                self.wfile.write(_b)
                return
            _mf = os.path.join(HOME, "mobile.html")
            if not os.path.exists(_mf):
                try:
                    open(_mf, "w", encoding="utf-8").write(MOBILE_HTML)
                except Exception:
                    pass
            try:
                html = open(_mf, encoding="utf-8").read().encode()
            except Exception:
                html = MOBILE_HTML.encode()
            self.send_response(200)
            try:
                self.send_header("Set-Cookie", "aster_pair=" + _tok + "; Path=/; Max-Age=2592000; SameSite=Lax")
            except Exception:
                pass
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if self.path.startswith("/memory/check"):
            return self._json(memory_check())
        if self.path.startswith("/memory/fix"):
            return self._json(memory_fix())
        if self.path.startswith("/memory/export_all"):
            return self._json(export_pack())
        if self.path.startswith("/memory/pack.zip"):
            p = os.path.join(HOME, "memory-pack.zip")
            if not os.path.exists(p):
                export_pack()
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        # ---- 旧界面还在调的接口：给个不报错的老实话，别让按钮点了没反应 ----
        if self.path.startswith("/asr/status"):
            try:
                import faster_whisper  # noqa
                return self._json({"ok": True, "ready": True})
            except Exception:
                return self._json({"ok": False, "note": "没装语音识别组件"})
        if self.path.startswith("/ready"):
            return self._json({"ok": True, "ready": True,
                               "persona": "已加载" if persona_text() else "出厂默认",
                               "memory_items": len(memories())})
        if self.path.startswith("/recent"):
            return self._json({"ok": True, "items": HISTORY[-20:]})
        if self.path.startswith("/usage"):
            return self._json(usage_stats("week=1" in self.path))
        if self.path.startswith("/briefs"):
            items = []
            sm = summary_text()
            if sm:
                items = [{"text": x.strip("- ").strip()[:160]}
                         for x in sm.splitlines() if x.strip()][-10:]
            return self._json({"ok": True, "briefs": items})
        if self.path.startswith("/voice/ref"):
            return self._json(voice_ref_state())
        if self.path.startswith("/voice/list"):
            return self._json(voice_list())
        if self.path.startswith("/today"):
            return self._json(today_view())
        if self.path.startswith("/v1/models"):
            names = ["aster-local"]
            try:
                ok, out = _sh(HOME, "ollama list", 8)
                names += [l.split()[0] for l in (out or "").splitlines()[1:] if l.split()]
            except Exception:
                pass
            return self._json({"object": "list",
                               "data": [{"id": n, "object": "model"} for n in names]})
        if self.path.startswith("/sync/snapshot"):
            # 给手机一份「她」的快照：身份、人格、关系、最近记忆、日程。
            # 电脑关着的时候，手机靠这份自己活着；回来再合并。
            import urllib.parse as _ups
            _qs = _ups.parse_qs(_ups.urlparse(self.path).query)
            _limit = min(12000, int((_qs.get("limit") or ["8000"])[0]))
            mem = []
            try:
                _c = _memdb()
                for row in _c.execute("select text, tag, created, subject from items "
                                      "order by rowid desc limit ?", (_limit,)).fetchall():
                    mem.append({"text": row[0], "tag": row[1], "ts": int(row[2] or 0),
                                "subject": row[3] or "self"})
                _c.close()
            except Exception:
                pass
            return self._json({
                "ok": True,
                "identity": _load(os.path.join(HOME, "identity.json"), {}) or {},
                "persona": persona_text() or "",
                "relation": (_relation.state() if _relation else {}),
                "milestones": (_ms.state() if _ms else {}),
                "memories": mem,
                "trips": _load(os.path.join(HOME, "trips.json"), []) or [],
                "ts": int(time.time()),
            })
        if self.path.startswith("/sync/ping"):
            return self._json({"ok": True, "name": "Aster",
                               "port": int(os.environ.get("ASTER_PORT") or 8788),
                               "since": int(time.time()),
                               "lan": _lan_ip()})
        if self.path.startswith("/sync/pull"):
            import urllib.parse as _upS
            qS = _upS.parse_qs(_upS.urlparse(self.path).query)
            since = int((qS.get("since") or ["0"])[0])
            items = [{"kind": "mood", "ts": int(time.time()), "mood": mood_now()}]
            for it in tasks()[-10:]:
                if int(it.get("created") or 0) >= since:
                    items.append({"kind": "task", "ts": int(it.get("created") or 0),
                                  "goal": it.get("goal"), "status": it.get("status")})
            return self._json({"ok": True, "items": items})
        if self.path.startswith("/sync/qr"):
            import io
            try:
                import qrcode
                url = ("https://asternest.net/app/?sync=http://" + _lan_ip() + ":"
                       + str(os.environ.get("ASTER_PORT") or 8788))
                img = qrcode.make(url)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/sync/hello"):
            return self._json({"ok": True, "name": "Aster", "base": "http://" + _lan_ip() + ":"
                               + str(os.environ.get("ASTER_PORT") or 8788)})
        if self.path.startswith("/sync/summary"):
            return self._json({"ok": True, "data": _load(os.path.join(HOME, "day_summary.json"), {})})
        if self.path.startswith("/facts"):
            rows = []
            try:
                for it in memories()[:120]:
                    rows.append({"s": "他", "a": str(it.get("text"))[:20],
                                 "v": str(it.get("text"))[:200],
                                 "ts": int(it.get("created") or 0)})
            except Exception:
                pass
            return self._json({"ok": True, "facts": rows})
        if self.path.startswith("/v1/tasks"):
            return self._json({"ok": True, "items": [
                {"id": t.get("id"), "goal": t.get("goal"), "status": t.get("status"),
                 "created": t.get("created")} for t in tasks()[-20:]]})
        if self.path.startswith("/v1/mood"):
            return self._json({"ok": True, "mood": mood_now(),
                               "state": _load(os.path.join(HOME, "affect.json"), {})})
        if self.path.startswith("/gate"):
            return self._json({"ok": True, "coverage": gate_coverage()})
        if self.path.startswith("/selfcheck"):
            # 2026-11-22：自检扩成一张清单，界面能直接显示哪一项不对
            _checks = []

            def _ck(name, fn):
                try:
                    ok, detail = fn()
                except Exception as e:
                    ok, detail = False, str(e)[:60]
                _checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:80]})

            _ck("引擎", lambda: (True, "8777 在跑"))
            _ck("云端密钥", lambda: (bool(cloud_key()), "已配置" if cloud_key() else "未配置"))
            _ck("本地模型", lambda: (_sh(HOME, "ollama list", 8)[1].find("qwen2.5") >= 0,
                                   "qwen2.5 可用" if _sh(HOME, "ollama list", 8)[1].find("qwen2.5") >= 0 else "没发现"))

            def _mem_ok():
                _c = _memdb()
                n = _c.execute("select count(*) from items").fetchone()[0]
                _c.close()
                return n > 0, "%d 条" % n
            _ck("记忆库", _mem_ok)
            _ck("身份", lambda: (bool(_names().get("assistant")), _names().get("assistant") or "还没起名"))
            _ck("人格", lambda: (len(persona_text() or "") > 20, "%d 字" % len(persona_text() or "")))
            _ck("关系", lambda: (bool(_relation), (_relation.state().get("tier_name", "") if _relation else "")))
            _ck("声音服务", lambda: (bool(_tts_probe()), "IndexTTS" if _tts_probe() else "只有系统音"))
            return self._json({"ok": all(c["ok"] for c in _checks), "engine": True,
                               "cloud": bool(cloud_key()),
                               "model": bool(_sh(HOME, "ollama list", 8)[1].find("qwen2.5") >= 0),
                               "gate": gate_coverage(), "checks": _checks})
        if self.path.startswith("/onboarding"):
            return self._json({"ok": True, "onboarding": onboard_state()})
        if self.path.startswith("/voice/use"):
            return self._json(voice_use(str(body.get("backend") or "Tingting")))
        if self.path.startswith("/voice/ref"):
            return self._json({"ok": False, "note": "声音参考还没接，先用系统音色"})
        if self.path.startswith("/faces"):
            prof = _load(os.path.join(HOME, "profile.json"), {})
            emos = _load(os.path.join(HOME, "emotions.json"), {})
            top = sorted(emos.items(), key=lambda kv: -kv[1])[:5] if emos else []
            return self._json({"ok": True, "profile": prof.get("text") or "",
                               "top": [{"name": k, "score": v} for k, v in top]})
        if self.path.startswith("/origin"):
            return self._json({"ok": True, "text": persona_text() or "还没有人设"})
        if self.path.startswith("/agent/pending"):
            return self._json({"ok": True, "items": []})
        if self.path.startswith("/agent/approve") or self.path.startswith("/agent/request"):
            return self._json({"ok": True, "note": "没有待批的事"})
        if self.path.startswith("/habit"):
            return self._json({"ok": True, "items": [t for t in tasks() if t.get("status") == "open"]})
        if self.path.startswith("/setup/status"):
            ok, out = _sh(HOME, "ollama list", 8)
            names = [l.split()[0] for l in (out or "").splitlines()[1:] if l.split()]
            return self._json({"ok": True, "alive": bool(ok), "models": names,
                               "running": bool(DL.get("running")), "percent": int(DL.get("percent") or 0),
                               "step": DL.get("label") or "", "done": bool(DL.get("done")),
                               "error": DL.get("error") or ""})
        if self.path.startswith("/asr") or self.path.startswith("/voice/") or \
                self.path.startswith("/genesis") or self.path.startswith("/setup/") or \
                self.path.startswith("/music") or self.path.startswith("/room"):
            return self._json({"ok": False, "note": "这一块还没接到新内核（先不装可用）"})
        if self.path.startswith("/download/status"):
            return self._json({"ok": True, **DL})
        if self.path.startswith("/qq/config"):
            d = qq_prefs()
            b = QQ_BRIDGE.get("obj")
            return self._json({"ok": True, "prefs": d,
                               "state": {"alive": bool(b and getattr(b, "alive", False)),
                                         "last_error": getattr(b, "last_error", "") if b else ""}})
        if self.path.startswith("/cloud/status"):
            return self._json({"ok": True, "has_key": bool(cloud_key())})
        if self.path.startswith("/vision/status"):
            return self._json({"ok": True, "has_key": bool(vision_key())})
        if self.path.startswith("/search/status"):
            return self._json({"ok": True, "has_key": bool(search_key())})
        if self.path.startswith("/imagegen/status"):
            cfg = img_config()
            return self._json({"ok": True, "has_key": bool(cfg.get("key")),
                               "model": cfg.get("model") or IMG_MODEL})
        if self.path.startswith("/imagegen/base"):
            import urllib.parse as _up7
            q7 = _up7.parse_qs(_up7.urlparse(self.path).query)
            return self._json(img_set_base((q7.get("file") or [""])[0]))
        if self.path.startswith("/imagegen/generate"):
            import urllib.parse as _up6
            q6 = _up6.parse_qs(_up6.urlparse(self.path).query)
            return self._json(img_generate((q6.get("prompt") or [""])[0],
                                           (q6.get("size") or ["1024x1024"])[0],
                                           (q6.get("name") or [""])[0],
                                           (q6.get("expression") or [""])[0],
                                           (q6.get("use_base") or ["1"])[0] != "0"))
        if self.path.startswith("/face/states"):
            _m = os.path.join(HOME, "media")
            out = []
            for _n in FACE_STATES:
                _cands = [os.path.join(_m, _n + ".jpg"),
                          os.path.join(_m, FACE_STATE_EN.get(_n, "") + ".jpg")]
                _p = next((x for x in _cands if os.path.exists(x)), "")
                out.append({"name": _n, "path": _p})
            return self._json({"ok": True, "states": out})
        if self.path.startswith("/face/list"):
            return self._json({"ok": True, "items": face_list()})
        if self.path.startswith("/face/set"):
            import urllib.parse as _up5
            q5 = _up5.parse_qs(_up5.urlparse(self.path).query)
            return self._json(face_set((q5.get("name") or [""])[0]))
        if self.path.startswith("/face/clear"):
            return self._json(face_clear())
        if self.path.startswith("/face"):
            return self._json(face_now())
        if self.path.startswith("/album"):
            return self._json({"ok": True, "items": album()})
        if self.path.startswith("/code/status"):
            import codework as _cw
            import urllib.parse as _up2
            q2 = _up2.parse_qs(_up2.urlparse(self.path).query)
            return self._json(_cw.status((q2.get("id") or [""])[0]))
        if self.path.startswith("/debate/status"):
            import debate as _db
            import urllib.parse as _up
            q = _up.parse_qs(_up.urlparse(self.path).query)
            return self._json(_db.job_status((q.get("id") or [""])[0]))
        if self.path.startswith("/providers"):
            try:
                import providers as _pv
                return self._json(_pv.list_all())
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80], "items": []})
        if self.path.startswith("/img"):
            import urllib.parse as _up3
            q = _up3.parse_qs(_up3.urlparse(self.path).query)
            p = (q.get("f") or [""])[0]
            if not p or not os.path.exists(p):
                return self._json({"ok": False}, 404)
            data = open(p, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path.startswith("/schedule/add"):
            import urllib.parse as _upB
            qB = _upB.parse_qs(_upB.urlparse(self.path).query)
            return self._json({"ok": True, "item": add_schedule(
                (qB.get("kind") or ["daily"])[0], (qB.get("when") or ["08:00"])[0],
                (qB.get("what") or [""])[0])})
        if self.path.startswith("/router/stats"):
            rows = router_log(1000)
            rule = [r for r in rows if str(r.get("via","")).startswith("rule")]
            model = [r for r in rows if r.get("via") == "model"]
            # corrected 定义：规则给了初始档位、最终 tier 被 decide_tier/夹子改掉的条数 ÷ 规则命中总数
            def _expect(need):
                return "heavy" if need == "cloud_heavy" else "local"
            changed = [r for r in rule if r.get("tier") and r["tier"] != _expect(r.get("need"))]
            return self._json({"ok": True, "rule_hits": len(rule), "model_hits": len(model),
                               "rule_changed_by_tier": len(changed),
                               "corrected_rate": round(len(changed) / max(1, len(rule)), 3),
                               "corrected_by_user": sum(1 for r in rows if r.get("corrected")),
                               "definition": "corrected = 规则命中后最终 tier 被 decide_tier/夹子改掉的条数 ÷ 规则命中总数"})
        if self.path.startswith("/queue"):
            return self._json({"ok": True, "items": queue_list()[-20:], "busy": ROUTER["dispatching"],
                               "max": ROUTER["max"], "done": ROUTER["done"]})
        if self.path.startswith("/router/log"):
            import urllib.parse as _upR
            qR = _upR.parse_qs(_upR.urlparse(self.path).query)
            return self._json({"ok": True, "items": router_log(int((qR.get("n") or ["50"])[0]))})
        if self.path.startswith("/mood/tick"):
            try:
                import endo as _en3
                _en3.save(_en3.tick(_en3.load() or {}))
                return self._json({"ok": True, "mood": mood_now()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/pending"):
            return self._json({"ok": True, "items": pending_list()})
        if self.path.startswith("/leases"):
            return self._json({"ok": True, "items": leases(active_only=False)})
        if self.path.startswith("/inbox/push"):
            batch = body.get("batch") or []
            n = 0
            for it in batch[:20]:
                txt2 = str((it or {}).get("text") or "").strip()
                if txt2:
                    inbox_push(txt2)
                    n += 1
            return self._json({"ok": True, "accepted": n})
        if self.path.startswith("/pair/claim"):
            return self._json({"ok": True, "token": "aster-local", "name": "Aster"})
        if self.path.startswith("/work/import_json"):
            items = body.get("items") or body.get("works") or []
            _save(os.path.join(HOME, "work_import.json"), {"items": items[-200:]})
            return self._json({"ok": True, "count": len(items)})
        if self.path.startswith("/router/model"):
            cur = (_load(os.path.join(HOME, "router.json"), {}) or {}).get("model")
            if not cur:
                try:
                    import router_local as _rl2
                    cur = _rl2.MODEL
                except Exception:
                    cur = "qwen2.5:7b"
            ok, out = _sh(HOME, "ollama list", 8)
            names = [l.split()[0] for l in (out or "").splitlines()[1:] if l.split()]
            return self._json({"ok": True, "current": cur, "available": names})
        if self.path.startswith("/tools"):
            return self._json({"ok": True, "items": [_tk.spec(n) for n in _tk.names()]})
        if self.path.startswith("/trace"):
            import urllib.parse as _upT
            qT = _upT.parse_qs(_upT.urlparse(self.path).query)
            return self._json({"ok": True, "items": _tk.recent_trace(int((qT.get("n") or ["30"])[0]))})
        if self.path.startswith("/watchdog"):
            return self._json({"ok": True, **WD, "stall_sec": STALL_SEC,
                               "running": [t["id"] for t in tasks() if t.get("status") == "running"]})
        if self.path.startswith("/schedules"):
            return self._json({"ok": True, "items": schedules()})
        if self.path.startswith("/task/list"):
            return self._json({"ok": True, "items": tasks()[-20:]})
        if self.path.startswith("/reminders"):
            items = []
            try:
                items = _rm.load() if _rm and hasattr(_rm, "load") else []
            except Exception:
                items = []
            return self._json({"ok": True, "items": items})
        if self.path.startswith("/ext/catalog_url"):
            cur = os.environ.get("ASTER_CATALOG_URL") or (_load(os.path.join(HOME, "catalog.json"), {}) or {}).get("url") or ""
            return self._json({"ok": True, "url": cur})
        if self.path.startswith("/ext/list"):
            try:
                _pk = _ext.scan() if _ext else []
                # 只有「能跑起来」的才算技能；只写着怎么说话的那些是提示词，不摆在这儿
                _all = _ext.skills() if _ext else []
                _sk = [x for x in _all if x.get("runnable") or x.get("main")]
                _pr = [x for x in _all if x not in _sk]
                return self._json({"ok": True, "items": _pk, "skills": _sk, "prompts": _pr})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/ext/catalog"):
            try:
                return self._json(_ext.catalog() if _ext else {"ok": False})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/inbox"):
            if "take=1" in self.path:
                return self._json({"ok": True, "items": inbox_take()})
            return self._json({"ok": True, "items": _load(INBOX, {"items": []}).get("items", [])})
        if self.path.startswith("/memory"):
            # 2026-11-22：记忆页改看主库 mem.db，带主体与来源，能看见也能删
            try:
                c = _memdb()
                rows = c.execute("select id, text, tag, created, src, subject from items "
                                 "order by created desc limit 60").fetchall()
                total = c.execute("select count(*) from items").fetchone()[0]
                c.close()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
            items = [{"id": r[0], "text": r[1], "tag": r[2], "ts": int(r[3] or 0),
                      "src": r[4] or "", "subject": r[5] or "self"} for r in rows]
            return self._json({"ok": True, "items": items, "total": total})
        if self.path.startswith("/plan"):
            return self._json({"ok": True, "last": _load(PLAN_FILE, {})})
        if self.path.startswith("/models"):
            ok, out = _sh(HOME, "ollama list", 8)
            names = [l.split()[0] for l in (out or "").splitlines()[1:] if l.split()]
            cur = (_load(os.path.join(HOME, "model.json"), {}) or {}).get("model") or \
                  os.environ.get("ASTER_ROUTER_MODEL") or "qwen2.5:7b"
            return self._json({"ok": True, "models": names, "items": names, "current": cur})
        if self.path.startswith("/prefs"):
            return self._json({"ok": True, "prefs": {}})
        if self.path.startswith("/inbox"):
            return self._json({"ok": True, "items": []})
        if self.path.startswith("/mode"):
            return self._json({"ok": True, "mode": "daily", "modes": {}})
        if self.path.startswith("/permissions"):
            try:
                import permissions as _pm
                lv = _pm.level()
            except Exception:
                lv = 2
            return self._json({"ok": True, "level": lv,
                               "levels": {"1": "仅可查看", "2": "工作区内修改", "3": "完全权限"}})
        if self.path.startswith("/price"):
            import urllib.parse as _up4
            q4 = _up4.parse_qs(_up4.urlparse(self.path).query)
            u4 = (q4.get("url") or [""])[0]
            import webtool as _wt
            return self._json(_wt.price_of(u4))
        if self.path.startswith("/identity"):
            _id = _load(os.path.join(HOME, "identity.json"), {}) or {}
            return self._json({"ok": True, **_names(), "gender": _id.get("gender") or ""})
        if self.path.startswith("/artifacts"):
            import glob as _g2
            out = []
            try:
                for _x in (_load(ART_FILE, {"items": []}).get("items") or []):
                    out.append({"id": "a-" + str(_x.get("ts")), "kind": _x.get("kind") or "file",
                                "title": _x.get("title") or "", "path": _x.get("abs") or _x.get("path"),
                                "rel": _x.get("path") or "", "t": int(_x.get("ts") or 0),
                                "task": _x.get("job") or "", "files": []})
            except Exception:
                pass
            for jf in _g2.glob(os.path.join(HOME, "codework", "*", "job.json")):
                try:
                    d = json.load(open(jf, encoding="utf-8"))
                    _wd = os.path.join(os.path.dirname(jf), "work")
                    out.append({"id": d.get("id"), "task": d.get("task"),
                                "title": d.get("task"), "files": d.get("files") or [],
                                "t": int(os.path.getmtime(jf)), "path": _wd,
                                "rel": _wd, "workdir": _wd})
                except Exception:
                    pass
            out.sort(key=lambda x: -x["t"])
            return self._json({"ok": True, "items": out[:50]})
        if self.path.startswith("/history"):
            disk = chat_log()
            if disk:
                return self._json({"ok": True, "items": disk})
            return self._json({"ok": True, "items": HISTORY})
        return self._json({"ok": False, "error": "not found"}, 404)

    def _do_POST_impl(self):
        if not self._remote_ok():
            return self._deny_remote()
        n = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(n) if n else b""
        if self.path.startswith("/asr"):
            return self._asr(raw_body)
        _ch = None
        try:
            _ch = json.loads(raw_body.decode() or "{}").get("channel")
        except Exception:
            _ch = None
        if _ch:
            set_channel(_ch)
        try:
            body = json.loads(raw_body.decode() or "{}")
        except Exception:
            body = {}

        if self.path.startswith("/providers/key"):
            try:
                import providers as _pv
            except Exception as e:
                return self._json({"ok": False, "error": "模块没起来：" + str(e)[:60]})
            return self._json(_pv.set_key(str(body.get("name") or ""), str(body.get("key") or ""),
                                          str(body.get("base") or ""), str(body.get("model") or "")))
        if self.path.startswith("/providers/clear"):
            import providers as _pv
            return self._json(_pv.clear_key(str(body.get("name") or "")))
        if self.path.startswith("/providers/test"):
            import providers as _pv
            nm = str(body.get("name") or "")
            r = _pv.call(nm, [{"role": "user", "content": "回一个字：好"}], max_tokens=8, timeout=25)
            return self._json(r)
        if self.path.startswith("/correction"):
            txt = str(body.get("text") or "").strip()
            if not txt:
                return self._json({"ok": False, "error": "空的"})
            add_memory("用户更正：" + txt[:300], tag="更正")
            return self._json({"ok": True})
        if self.path.startswith("/session/new"):
            del HISTORY[:]
            return self._json({"ok": True})
        if self.path.startswith("/debate/run"):
            try:
                import debate as _db
            except Exception as e:
                return self._json({"ok": False, "error": "模块没起来：" + str(e)[:60]})
            return self._json(_db.run(str(body.get("topic") or "").strip(),
                                      body.get("seats") or [],
                                      int(body.get("rounds") or 2)))
        if self.path.startswith("/plan/run"):
            import debate as _db
            return self._json(_db.produce(str(body.get("topic") or "").strip(),
                                          body.get("seats") or [],
                                          int(body.get("rounds") or 2)))
        if self.path.startswith("/debate/start"):
            import debate as _db
            return self._json(_db.start_job(str(body.get("mode") or "debate"),
                                            str(body.get("topic") or "").strip(),
                                            body.get("seats") or [],
                                            int(body.get("rounds") or 2)))
        if self.path.startswith("/debate/stop"):
            import debate as _db
            return self._json(_db.job_stop(str(body.get("id") or "")))
        if self.path.startswith("/debate/say"):
            import debate as _db
            return self._json(_db.job_say(str(body.get("id") or ""), str(body.get("text") or "")))
        if self.path.startswith("/code/start"):
            import codework as _cw
            return self._json(_cw.start_job(str(body.get("task") or "").strip(),
                                            body.get("seats") or [],
                                            int(body.get("rounds") or 3),
                                            float(body.get("budget") or 0)))
        if self.path.startswith("/code/stop"):
            import codework as _cw
            return self._json(_cw.stop(str(body.get("id") or "")))
        if self.path.startswith("/code/say"):
            import codework as _cw
            return self._json(_cw.say(str(body.get("id") or ""), str(body.get("text") or "")))
        if self.path.startswith("/code/rollback"):
            import codework as _cw
            return self._json(_cw.rollback(str(body.get("id") or ""), str(body.get("label") or "")))
        if self.path.startswith("/code/toggle"):
            import codework as _cw
            return self._json(_cw.toggle(str(body.get("id") or ""), str(body.get("item") or ""),
                                         bool(body.get("done"))))
        if self.path.startswith("/code/verify"):
            import codework as _cw
            return self._json(_cw.verify(str(body.get("id") or "")))
        if self.path.startswith("/task/delete"):
            tid = str(body.get("id") or "")
            items = tasks()
            keep = [t for t in items if str(t.get("id")) != tid]
            if len(keep) == len(items):
                return self._json({"ok": False, "error": "没有这条"})
            _save(os.path.join(HOME, "tasks.json"), keep)
            return self._json({"ok": True, "left": len(keep)})
        if self.path.startswith("/chat/note"):
            chat_log({"who": str(body.get("who") or "me"), "text": str(body.get("text") or ""),
                      "ts": int(time.time())})
            return self._json({"ok": True})
        if self.path.startswith("/code/go"):
            import codework as _cw
            return self._json(_cw.go(str(body.get("id") or "")))
        if self.path.startswith("/code/resume"):
            import codework as _cw
            return self._json(_cw.resume(str(body.get("id") or "")))
        if self.path.startswith("/file/text"):
            import urllib.parse as _up3
            q3 = _up3.parse_qs(_up3.urlparse(self.path).query)
            d3 = (q3.get("dir") or [""])[0]
            n3 = (q3.get("name") or [""])[0]
            if not d3.startswith(HOME) or ".." in d3 or ".." in n3:
                return self._json({"ok": False, "error": "路径不对"})
            p3 = os.path.join(d3, n3)
            try:
                return self._json({"ok": True, "text": open(p3, encoding="utf-8", errors="replace").read()[:4000]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:60]})
        if self.path.startswith("/open/last"):
            # 把他要的成果真打开：找到最近一件活的产出，复制到桌面并打开
            import glob as _g
            import shutil as _sh
            import subprocess as _sp
            root = os.path.join(HOME, "codework")
            _want = str(body.get("id") or "")
            if _want:
                jobs = [os.path.join(root, _want, "job.json")]
                jobs = [j for j in jobs if os.path.exists(j)]
            else:
                jobs = sorted(_g.glob(os.path.join(root, "*", "job.json")), key=os.path.getmtime)
            if not jobs:
                return self._json({"ok": False, "error": "还没有做过活"})
            jd = os.path.dirname(jobs[-1])
            work = os.path.join(jd, "work")
            files = [f for f in os.listdir(work)] if os.path.isdir(work) else []
            if not files:
                return self._json({"ok": False, "error": "最近那件没有产出文件"})
            desk = os.path.expanduser("~/Desktop")
            moved = []
            for f in files:
                src = os.path.join(work, f)
                if os.path.isfile(src):
                    dst = os.path.join(desk, f)
                    _sh.copy2(src, dst)
                    moved.append(dst)
            tgt = str(body.get("path") or (moved[0] if moved else ""))
            try:
                if sys.platform == "darwin":
                    _sp.run(["open", tgt], timeout=10)
                elif os.name == "nt":
                    _sp.run(["explorer", tgt], timeout=10)
                else:
                    _sp.run(["xdg-open", tgt], timeout=10)
            except Exception as e:
                return self._json({"ok": True, "path": tgt, "opened": False, "error": str(e)[:60]})
            return self._json({"ok": True, "path": tgt, "opened": True, "count": len(moved)})
        if self.path.startswith("/task/retry"):
            tid = str(body.get("id") or "")
            items = tasks()
            hit = next((t for t in items if str(t.get("id")) == tid), None)
            if not hit:
                return self._json({"ok": False, "error": "没有这条"})
            hit["status"] = "open"
            hit["updated"] = int(time.time())
            _save(os.path.join(HOME, "tasks.json"), items)
            return self._json({"ok": True, "goal": hit.get("goal")})
        if self.path.startswith("/open/path"):
            import subprocess as _sp2
            raw = str(body.get("path") or "")
            p2 = os.path.abspath(os.path.expanduser(raw))
            if not os.path.exists(p2):
                # 只给了文件名：去桌面/文档/Aster 目录里找
                import glob as _g4
                _name = os.path.basename(raw)
                for _r in (os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"), HOME):
                    _hit = _g4.glob(os.path.join(_r, "**", _name), recursive=True)
                    if _hit:
                        p2 = _hit[0]
                        break
            if not (p2.startswith(HOME) or p2.startswith(os.path.expanduser("~/Desktop"))
                    or p2.startswith(os.path.expanduser("~/Documents"))):
                return self._json({"ok": False, "error": "只能打开 Aster 目录、桌面或文稿目录下的文件"})
            if not os.path.exists(p2):
                return self._json({"ok": False, "error": "文件不在：" + p2})
            reveal = bool(body.get("reveal"))
            try:
                if sys.platform == "darwin":
                    _sp2.run(["open", "-R", p2] if reveal else ["open", p2], timeout=10)
                elif os.name == "nt":
                    _sp2.run(["explorer", "/select,", p2] if reveal else ["start", "", p2], timeout=10, shell=True)
                else:
                    _sp2.run(["xdg-open", os.path.dirname(p2) if reveal else p2], timeout=10)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
            return self._json({"ok": True, "path": p2})
        if self.path.startswith("/open/find"):
            import glob as _g3
            name = os.path.basename(str(body.get("name") or ""))
            reveal = bool(body.get("reveal"))
            if not name:
                return self._json({"ok": False, "error": "没给文件名"})
            roots = [os.path.expanduser("~/Desktop"), os.path.expanduser("~/Documents"), HOME]
            hit = ""
            for r3 in roots:
                for f3 in _g3.glob(os.path.join(r3, "**", name), recursive=True):
                    hit = f3
                    break
                if hit:
                    break
            if not hit:
                return self._json({"ok": False, "error": "没找到这个文件"})
            import subprocess as _sp3
            try:
                if sys.platform == "darwin":
                    _sp3.run(["open", "-R", hit] if reveal else ["open", hit], timeout=10)
                else:
                    _sp3.run(["xdg-open" if os.name != "nt" else "explorer", hit], timeout=10)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
            return self._json({"ok": True, "path": hit})
        if self.path.startswith("/avatar"):
            import base64 as _b64
            data = str(body.get("data") or "")
            if "," in data[:120]:
                data = data.split(",", 1)[1]
            try:
                raw = _b64.b64decode(data)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:60]})
            if len(raw) < 2000:
                return self._json({"ok": False, "error": "图太小"})
            dst = os.path.join(HOME, "media", "face-base.jpg")
            open(dst, "wb").write(raw)
            return self._json({"ok": True, "path": dst})
        if self.path.startswith("/face/lock"):
            pth = str(body.get("path") or "")
            if not pth or not os.path.exists(pth):
                return self._json({"ok": False, "error": "没有这张图"})
            name = os.path.splitext(os.path.basename(pth))[0]
            _save(FACE_OVERRIDE, {"face": name, "ts": int(time.time())})
            return self._json({"ok": True, "face": name})
        if self.path.startswith("/face/auto"):
            try:
                if os.path.exists(FACE_OVERRIDE):
                    os.remove(FACE_OVERRIDE)
            except Exception:
                pass
            return self._json({"ok": True})
        if self.path.startswith("/face/upload"):
            import base64 as _b64b
            state = str(body.get("state") or "").strip()
            data = str(body.get("data") or "")
            if not state or not data:
                return self._json({"ok": False, "error": "缺状态或图"})
            if "," in data[:120]:
                data = data.split(",", 1)[1]
            try:
                raw = _b64b.b64decode(data)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:60]})
            if len(raw) < 2000:
                return self._json({"ok": False, "error": "图太小"})
            fn = FACE_STATE_EN.get(state, state) + ".jpg"
            dst = os.path.join(HOME, "media", fn)
            open(dst, "wb").write(raw)
            return self._json({"ok": True, "path": dst, "state": state})
        if self.path.startswith("/summarize"):
            txt = str(body.get("text") or "")[:5000]
            q = str(body.get("q") or "")
            if not txt.strip():
                return self._json({"ok": False, "error": "没有内容"})
            prompt = ("下面是一页搜索结果或商品页的文字。请按下面的格式，用他的话回答他问的（" + (q or "价格") + "）。"
                      "只写这段文字里真有的数字，没有就写“没看到”。\n"
                      "格式：\n第一行：一句结论（多少钱，哪天最便宜）\n"
                      "然后每行一条：时间或条件 + 航班/商家 + 价格\n"
                      "最后一行：额外费用或需要注意的事\n"
                      "不要提“页面”“来源”，不要解释，不要 Markdown 符号，最多八行。\n\n" + txt)
            try:
                out = cloud(prompt, max_tokens=420)
            except Exception:
                out = ""
            out = (out or "").strip()
            if not out:
                try:
                    import urllib.request as _u6
                    b6 = json.dumps({"model": "qwen2.5:7b", "prompt": prompt, "stream": false}).encode()
                    r6 = _u6.urlopen(_u6.Request("http://127.0.0.1:11434/api/generate", data=b6,
                                                 headers={"Content-Type": "application/json"}), timeout=60)
                    out = (json.loads(r6.read().decode()).get("response") or "").strip()
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:60]})
            return self._json({"ok": True, "say": out[:600]})
        if self.path.startswith("/think"):
            txt = str(body.get("text") or "")[:300]
            last = str(body.get("last") or "")[:120]
            if not txt.strip():
                return self._json({"ok": False, "error": "没话"})
            prompt = ("他说：" + txt + ("\n上一轮在说：" + last if last else "") +
                      "\n用一句话说出他这句话是在要什么（问行情 / 问看法 / 要我动手 / 说自己的状态 / 闲聊），"
                      "不超过二十个字，不要引号，不要解释。")
            say = ""
            try:
                import urllib.request as _u7
                b7 = json.dumps({"model": "qwen2.5:1.5b", "prompt": prompt, "stream": False,
                                 "options": {"num_predict": 32, "temperature": 0.2}}).encode()
                r7 = _u7.urlopen(_u7.Request("http://127.0.0.1:11434/api/generate", data=b7,
                                             headers={"Content-Type": "application/json"}), timeout=12)
                say = (json.loads(r7.read().decode()).get("response") or "").strip().replace("\n", " ")[:40]
            except Exception:
                say = ""
            return self._json({"ok": True, "say": say})
        if self.path.startswith("/trip/add"):
            items = _load(os.path.join(HOME, "trips.json"), [])
            _w0 = str(body.get("what") or "")
            _kind = str(body.get("kind") or "").strip()
            if _kind not in ("trip", "meeting", "todo"):
                # 没指定就按字面猜：出门的 / 开会的 / 其它待办
                if re.search(r"(飞|航班|机票|高铁|车次|动车|出发|登机|接机|送机|去|回)", _w0):
                    _kind = "trip"
                elif re.search(r"(会|开会|会议|约|见|面谈|汇报|面试)", _w0):
                    _kind = "meeting"
                else:
                    _kind = "todo"
            it = {"id": int(time.time() * 1000), "what": _w0[:200], "kind": _kind,
                  "when": str(body.get("when") or "")[:60], "note": str(body.get("note") or "")[:200],
                  "created": int(time.time())}
            if not it["what"]:
                return self._json({"ok": False, "error": "没有内容"})
            # 时间自己算：到点心跳会提醒，过了时间就算结束
            _at8 = _trip_at(it.get("when") or "")
            if _at8:
                it["at"] = _at8
            items.append(it)
            _save(os.path.join(HOME, "trips.json"), items[-200:])
            return self._json({"ok": True, "item": it})
        if self.path.startswith("/trip/travel"):
            _m9 = int(body.get("minutes") or 0)
            _save(os.path.join(HOME, "travel.json"), {"minutes": _m9})
            return self._json({"ok": True, "minutes": _m9})
        if self.path.startswith("/trip/del"):
            tid = body.get("id")
            items = [x for x in _load(os.path.join(HOME, "trips.json"), []) if str(x.get("id")) != str(tid)]
            _save(os.path.join(HOME, "trips.json"), items)
            return self._json({"ok": True})
        if self.path.startswith("/habit/set"):
            _key = str(body.get("key") or "").strip()[:12]
            _val = str(body.get("value") or "").strip()[:40]
            if not _key:
                return self._json({"ok": False, "error": "没有名目"})
            _d9 = _load(os.path.join(HOME, "habits.json"), {})
            _d9[_key] = _val
            _save(os.path.join(HOME, "habits.json"), _d9)
            return self._json({"ok": True, "habits": _d9})
        if self.path.startswith("/upload"):
            import base64 as _b64
            name = str(body.get("name") or "file")
            data = str(body.get("data") or "")
            if "," in data[:80]:
                data = data.split(",", 1)[1]
            try:
                raw = _b64.b64decode(data)
            except Exception:
                return self._json({"ok": False, "error": "文件读不出来"})
            updir = os.path.join(HOME, "uploads")
            os.makedirs(updir, exist_ok=True)
            safe = re.sub(r"[^\w\u4e00-\u9fa5.-]", "_", name)[:80] or "file"
            full = os.path.join(updir, safe)
            open(full, "wb").write(raw)
            ext = os.path.splitext(safe)[1].lower()
            text = ""
            try:
                if ext in (".docx", ".pdf", ".txt", ".md", ".csv", ".json", ".log"):
                    text = tool_doc_read({"path": full, "limit": 6000})
                elif ext in (".png", ".jpg", ".jpeg", ".webp"):
                    text = "图：" + tool_see({"path": full, "q": "用中文说清这张图里有什么，具体一点"})
                elif ext in (".xlsx", ".xls"):
                    text = tool_sheet({"action": "read", "path": full, "limit": 40})
            except Exception as e:
                text = "（读不出内容：%s）" % str(e)[:60]
            return self._json({"ok": True, "path": full, "name": safe,
                               "chars": len(text or ""), "text": (text or "")[:8000]})

        if self.path.startswith("/sync/ingest"):
            items = body.get("items") or []
            trips = _load(os.path.join(HOME, "trips.json"), []) or []
            made, kept = [], 0
            for it in items[:200]:
                text = str((it or {}).get("text") or "").strip()
                if not text:
                    continue
                when = extract_when(text)
                at = _trip_at(when) if when else 0
                if at:
                    _kind = "trip" if re.search(r"(飞|航班|高铁|车次|出发|登机|接机|送机)", text) else (
                        "meeting" if re.search(r"(会|开会|会议|面试|见|约)", text) else "todo")
                    row = {"id": int(time.time() * 1000) + len(trips), "what": text[:200],
                           "kind": _kind, "when": when, "note": "来自手机", "created": int(time.time()),
                           "at": at, "src": "phone"}
                    trips.append(row)
                    made.append(row)
                else:
                    kept += 1
                    try:
                        _pp = _load(os.path.join(HOME, "phone_day.json"), {"items": []}).get("items", [])
                        _pp.append({"ts": int((it or {}).get("ts") or time.time()), "text": text[:300],
                                    "kind": str((it or {}).get("kind") or "note")})
                        _save(os.path.join(HOME, "phone_day.json"), {"items": _pp[-400:]})
                    except Exception:
                        pass
            _save(os.path.join(HOME, "trips.json"), trips)
            return self._json({"ok": True, "trips": made, "kept": kept})
        if self.path.startswith("/sync/push"):
            items = body.get("items") or []
            _pday = _load(os.path.join(HOME, "phone_day.json"), {"items": []}).get("items", [])
            for _it0 in items[:200]:
                _it0 = dict(_it0 or {})
                _it0["ts"] = int(_it0.get("ts") or time.time())
                _pday.append(_it0)
            _save(os.path.join(HOME, "phone_day.json"), {"items": _pday[-400:]})
            got = {"mood": 0, "memory": 0, "work": 0, "chat": 0}
            for it in items[:200]:
                kind = str((it or {}).get("kind") or "")
                if kind == "mood":
                    _sync_mood(it)
                    got["mood"] += 1
                elif kind == "memory":
                    txt3 = str(it.get("text") or "").strip()
                    if txt3:
                        add_memory(txt3, tag=str(it.get("tag") or "手机"))
                        got["memory"] += 1
                elif kind in ("work", "chat"):
                    w = _load(os.path.join(HOME, "work_import.json"), {"items": []}).get("items", [])
                    w.append(it)
                    _save(os.path.join(HOME, "work_import.json"), {"items": w[-300:]})
                    got[kind] += 1
            out = {"ok": True, "got": got}
            try:
                out["report"] = day_report()
            except Exception as e:
                out["report"] = {"ok": False, "error": str(e)[:60]}
            return self._json(out)
        if self.path.startswith("/sync/summary"):
            items = _load(os.path.join(HOME, "phone_day.json"), {"items": []}).get("items", [])
            if not items:
                return self._json({"ok": False, "error": "今天还没有手机同步过来的内容"})
            lines = []
            for it in items[-60:]:
                k = it.get("kind")
                if k == "mood":
                    lines.append("情绪：" + str(it.get("mood"))[:40])
                elif k == "work":
                    lines.append("做了：" + str(it.get("text"))[:60])
                elif k == "memory":
                    lines.append("记下：" + str(it.get("text"))[:60])
            prompt = ("下面是他今天在手机上留下的一串片段（时间顺序，可能零碎）。"
                      "用中文写一段 120 字以内的当日小结：先说他今天大概在忙什么，"
                      "再说情绪走向，最后一句轻轻收住。不要列点，不要说「根据以上」。\n\n"
                      + "\n".join(lines[:60]))
            try:
                summary = cloud(prompt, max_tokens=300).strip()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
            _save(os.path.join(HOME, "day_summary.json"),
                  {"date": time.strftime("%Y-%m-%d"), "summary": summary[:600],
                   "count": len(items), "ts": int(time.time())})
            return self._json({"ok": True, "summary": summary, "count": len(items)})
        if self.path.startswith("/sync/report"):
            return self._json(day_report())
        if self.path.startswith("/v1/chat/completions"):
            msgs = body.get("messages") or []
            txt = ""
            for m in reversed(msgs):
                if m.get("role") == "user":
                    txt = str(m.get("content") or "")
                    break
            if not txt:
                return self._json({"error": {"message": "没有用户消息"}})
            out = chat(txt, channel="mobile")
            return self._json({"id": "chatcmpl-aster", "object": "chat.completion",
                               "model": body.get("model") or "aster-local",
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant",
                                                        "content": out.get("reply") or ""}}]})
        if self.path.startswith("/chat"):
            text = str(body.get("text") or "").strip()
            # 2026-11-20：所有回复分支统一落盘，界面只认这一份历史
            _orig_json = self._json
            def _json_chat(payload, code=200, _t=text, _f=_orig_json):
                try:
                    if isinstance(payload, dict) and payload.get("reply"):
                        chat_log({"who": "me", "text": _t, "ts": int(time.time())})
                        chat_log({"who": "her", "text": str(payload["reply"]), "ts": int(time.time())})
                except Exception:
                    pass
                return _f(payload, code)
            self._json = _json_chat
            if not text:
                msgs = body.get("messages") or []
                if msgs:
                    text = str(msgs[-1].get("content") or "").strip()
            if not text:
                return self._json({"ok": False, "error": "empty"})
            return self._json(chat(text))
        if self.path.startswith("/voice/ref"):
            if str(body.get("reset") or "") in ("1", "true", "yes"):
                return self._json(voice_ref_reset())
            return self._json(voice_ref_save(str(body.get("data") or ""), str(body.get("ext") or "wav")))
        if self.path.startswith("/tunnel/"):
            if self.path.startswith("/tunnel/stop"):
                return self._json(tunnel_stop())
            r = tunnel_start()
            if not r.get("ok"):
                return self._json(r)
            tok = (_load(PAIR_FILE, {}) or {}).get("token") or ""
            if not tok:
                tok = pair_new().get("token") or ""
            url = r["url"].rstrip("/") + "/m?t=" + tok
            qr = ""
            try:
                import base64 as _b64, io as _io, qrcode as _qr
                img = _qr.make(url)
                buf = _io.BytesIO()
                img.save(buf, format="PNG")
                qr = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
            except Exception:
                qr = ""
            return self._json({"ok": True, "url": url, "qr": qr, "started": r.get("started")})
        if self.path.startswith("/brief/now"):
            try:
                rows = chat_log(limit=30) or []
                msgs = [{"role": ("user" if str(r.get("who")) == "me" else "assistant"),
                         "content": str(r.get("text") or "")} for r in rows if r.get("text")]
                txt = _summarize(msgs, summary_text())
                if not txt:
                    return self._json({"ok": False, "error": "没压出东西来"})
                open(SUM_FILE, "w", encoding="utf-8").write(txt)
                return self._json({"ok": True, "text": txt[:600]})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/pair/reset"):
            try:
                if os.path.exists(PAIR_FILE):
                    os.remove(PAIR_FILE)
            except Exception:
                pass
            return self._json({"ok": True})
        if self.path.startswith("/relation/set"):
            try:
                _v = float(body.get("intimacy"))
            except Exception:
                return self._json({"ok": False, "error": "要一个 0-100 的数"})
            _d = _relation.load()
            _old = _relation.tier_of(_d.get("intimacy") or 0)[0]
            _d["intimacy"] = max(0.0, min(100.0, _v))
            _d["tier"] = _relation.tier_of(_d["intimacy"])[0]
            _d["updated"] = int(time.time())
            _d.setdefault("history", []).append({"ts": int(time.time()), "from": _old,
                                                 "to": _d["tier"], "why": "手动调",
                                                 "intimacy": _d["intimacy"]})
            _relation.save(_d)
            return self._json(_relation.state())
        if self.path.startswith("/memory/del"):
            _id = str(body.get("id") or "").strip()
            if not _id:
                return self._json({"ok": False, "error": "没给 id"})
            try:
                c = _memdb()
                n = c.execute("delete from items where id = ?", (_id,)).rowcount
                c.commit()
                c.close()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
            return self._json({"ok": True, "deleted": int(n or 0)})
        if self.path.startswith("/profile/rebuild"):
            r = profile_layer.build(memories(), _names())
            return self._json({"ok": r.get("ok"), "text": r.get("text") or ""})
        if self.path.startswith("/ext/catalog_url"):
            u = str(body.get("url") or "").strip()
            _save(os.path.join(HOME, "catalog.json"), {"url": u})
            os.environ["ASTER_CATALOG_URL"] = u
            return self._json({"ok": True, "url": u})
        if self.path.startswith("/ext/install_github"):
            r = tool_gh_skill({"repo": str(body.get("repo") or "")})
            return self._json({"ok": not str(r).startswith(("看不懂", "拉不到", "没装上", "这个仓库")),
                               "result": str(r)[:300]})
        if self.path.startswith("/ext/install_catalog"):
            try:
                return self._json(_ext.install_from_catalog(str(body.get("id") or "")))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/task/add"):
            t = add_task(str(body.get("goal") or ""))
            return self._json({"ok": True, "task": t})
        if self.path.startswith("/reminder/add"):
            try:
                r = _rm.add(str(body.get("text") or "")) if _rm else {"ok": False}
                return self._json(r)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/imagegen/generate"):
            refs = body.get("refs") or []
            if isinstance(refs, str):
                refs = [refs]
            return self._json(img_generate(str(body.get("prompt") or ""),
                                           str(body.get("size") or "1024x1024"),
                                           str(body.get("name") or ""),
                                           str(body.get("expression") or ""),
                                           bool(body.get("use_base", True)), refs))
        if self.path.startswith("/persona"):
            return self._json(persona_save(str(body.get("text") or "")))
        if self.path.startswith("/permissions"):
            try:
                import permissions as _pm
                r = _pm.set_level(int(body.get("level") or 2))
                return self._json({"ok": True, "level": _pm.level()})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:80]})
        if self.path.startswith("/router/model"):
            m = str(body.get("model") or "").strip()
            if not m:
                return self._json({"ok": False, "error": "没给模型名"})
            _save(os.path.join(HOME, "router.json"), {"model": m})
            try:
                import router_local as _rl
                _rl.MODEL = m
            except Exception:
                pass
            return self._json({"ok": True, "model": m})
        if self.path.startswith("/models/use"):
            m = str(body.get("model") or "").strip()
            if not m:
                return self._json({"ok": False, "error": "没给模型名"})
            _save(os.path.join(HOME, "model.json"), {"model": m})
            return self._json({"ok": True, "model": m, "note": "重启后生效"})
        if self.path.startswith("/setup/start"):
            return self._json(dl_start("model", "7b"))
        if self.path.startswith("/download/start"):
            return self._json(dl_start(str(body.get("kind") or ""), str(body.get("key") or "")))
        if self.path.startswith("/qq/config"):
            return self._json({"ok": True, "prefs": qq_save(
                str(body.get("url") or ""), body.get("whitelist"), body.get("on"))})
        if self.path.startswith("/cloud/key"):
            return self._json(set_cloud_key(str(body.get("key") or ""),
                                            str(body.get("base") or ""),
                                            str(body.get("model") or "")))
        if self.path.startswith("/vision/key"):
            return self._json(set_vision_key(str(body.get("key") or "")))
        if self.path.startswith("/approve"):
            it = pending_take(body.get("id"))
            if not it:
                return self._json({"ok": False, "error": "没有待批的事"})
            r = _tk.call(it["tool"], it["args"])
            return self._json({"ok": True, "ran": it["tool"], "result": r["result"][:300]})
        if self.path.startswith("/lease/grant"):
            mins = int(body.get("minutes") or 30)
            return self._json(lease_grant(mins, body.get("tools"), body.get("paths"),
                                          str(body.get("note") or "")))
        if self.path.startswith("/lease/revoke"):
            return self._json(lease_revoke(body.get("id")))
        if self.path.startswith("/onboarding/save"):
            return self._json(onboard_save(body if isinstance(body, dict) else {}))
        if self.path.startswith("/onboarding/skip"):
            d = _load(ONBOARD_FILE, {}) or {}
            d.update({"skip": True, "ts": int(time.time())})
            _save(ONBOARD_FILE, d)
            return self._json({"ok": True})
        if self.path.startswith("/onboarding/reset"):
            _save(ONBOARD_FILE, {"finished": False, "skip": False, "answers": {}, "step": 0})
            return self._json({"ok": True})
        if self.path.startswith("/search/key"):
            return self._json(set_search_key(str(body.get("key") or "")))
        if self.path.startswith("/imagegen/key"):
            return self._json(img_set_key(str(body.get("key") or "")))
        if self.path.startswith("/memory/import"):
            _path = str(body.get("path") or "")
            _data = str(body.get("data") or "")
            if not _path and _data:
                # 2026-11-22：页面只能给 base64，落成临时文件再导入
                import base64 as _b64, tempfile as _tf
                try:
                    raw = _b64.b64decode(_data.split(",")[-1])
                    _path = os.path.join(_tf.gettempdir(), "aster-import-%d.zip" % int(time.time()))
                    open(_path, "wb").write(raw)
                except Exception as e:
                    return self._json({"ok": False, "error": "包读不出来：" + str(e)[:60]})
            return self._json(import_pack(_path))
        if self.path.startswith("/remember"):
            add_memory(str(body.get("text") or ""), str(body.get("tag") or "事实"))
            return self._json({"ok": True})
        return self._json({"ok": False, "error": "not found"}, 404)

    def log_message(self, *a):
        pass


def seed_history(limit: int = 10) -> None:
    """起引擎时把最近几句接回内存里——不然一重启，刚聊的就不认了。"""
    try:
        rows = chat_log(limit=limit) or []
        for r in rows:
            who = "user" if str(r.get("who")) == "me" else "assistant"
            txt = str(r.get("text") or "").strip()
            if txt:
                HISTORY.append({"role": who, "content": txt[:200]})
        if HISTORY:
            print("history seeded:", len(HISTORY), flush=True)
    except Exception:
        pass


def main():
    ensure_defaults()
    seed_history()
    try:
        # 2026-11-22：第一次起来时，按旧聊天里出现过的亲昵话估一个关系起点；新产品就是 10。
        if _relation:
            _relation.seed_from_history(os.path.join(HOME, "chat.ndjson"))
    except Exception:
        pass
    port = int(os.environ.get("ASTER_PORT") or os.environ.get("EMBER_PORT") or 8788)
    import threading
    threading.Thread(target=_ticker, daemon=True).start()
    try:
        _resumed = resume_tasks()
        if _resumed:
            print("resume:", _resumed, flush=True)
    except Exception:
        pass
    try:
        _qq_apply()
    except Exception:
        pass
    threading.Thread(target=_qq_sender, daemon=True).start()
    print("core up on", port, flush=True)
    bind = os.environ.get("ASTER_BIND", "0.0.0.0")   # 手机要能连进来，不能只绑本机
    ThreadingHTTPServer((bind, port), H).serve_forever()


if __name__ == "__main__":
    main()
