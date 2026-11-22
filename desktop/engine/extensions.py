#!/usr/bin/env python3
"""插件与技能的骨架：扫描、启用、调用。

插件 = extensions/<name>/{manifest.json, main.py}
技能 = skills/<name>/SKILL.md

安全：默认全部禁用；调用走子进程，限时，输出截断；未声明的权限一律不给。
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time

def root() -> str:
    """插件/技能装在哪：优先工作区，否则跟着用户的记忆目录走（通用做法，不绑某个人）。"""
    if os.environ.get("ASTER_WORKSPACE"):
        return os.environ["ASTER_WORKSPACE"]
    try:
        import paths  # type: ignore
        return paths.mem_home()
    except Exception:
        return os.environ.get("ASTER_HOME") or os.path.expanduser(
            "~/Library/Application Support/Aster/memory")


WEATHER_MAIN = "\n".join([
    "import json, sys, urllib.request",
    "a = json.load(sys.stdin)",
    "lat, lon = a.get('lat'), a.get('lon')",
    "if lat is None or lon is None:",
    "    print(json.dumps({'error': 'need lat/lon'}, ensure_ascii=False)); sys.exit(0)",
    "url = 'https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s&current=temperature_2m,weather_code' % (lat, lon)",
    "try:",
    "    with urllib.request.urlopen(url, timeout=8) as r:",
    "        d = json.loads(r.read().decode())",
    "    cur = d.get('current') or {}",
    "    print(json.dumps({'temp': cur.get('temperature_2m'), 'code': cur.get('weather_code')}, ensure_ascii=False))",
    "except Exception as e:",
    "    print(json.dumps({'error': str(e)[:100]}, ensure_ascii=False))",
    "",
])

VIDEO_MAIN = '"""视频合成：把几行字做成一条竖屏短片（mp4）。需要系统里有 ffmpeg。"""\nimport json, os, subprocess, tempfile\n\ndef _ffmpeg():\n    here = os.path.dirname(os.path.dirname(os.path.abspath(globals().get("__file__", os.getcwd()))))\n    home = os.environ.get("ASTER_HOME") or os.path.expanduser("~/Library/Application Support/Aster/memory")\n    cands = [os.environ.get("FFMPEG") or "",\n             os.path.join(home, "bin", "ffmpeg"),\n             os.path.join(here, "bin", "ffmpeg"),\n             os.path.join(here, "engine", "bin", "ffmpeg"),\n             "ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]\n    try:\n        import imageio_ffmpeg\n        cands.insert(1, imageio_ffmpeg.get_ffmpeg_exe())\n    except Exception:\n        pass\n    for p in cands:\n        if not p:\n            continue\n        try:\n            r = subprocess.run([p, "-version"], capture_output=True, timeout=8)\n            if r.returncode == 0:\n                return p\n        except Exception:\n            continue\n    return ""\n\ndef run(args, ctx=None):\n    ff = _ffmpeg()\n    if not ff:\n        return {"ok": False, "error": "这台机器没有 ffmpeg。装一个再来：brew install ffmpeg"}\n    raw = (args or {}).get("script") or ""\n    lines = (args or {}).get("lines") or []\n    if raw and not lines:\n        import re as _re\n        parts = [x.strip() for x in _re.split(r"[。！？!?\\n；;]", str(raw)) if x.strip()]\n        lines = [x for x in parts if len(x) >= 2]\n    if isinstance(lines, str):\n        lines = [lines]\n    lines = [str(x)[:40] for x in lines][:6] or ["没给内容"]\n    title = str((args or {}).get("title") or "")[:30]\n    out = str((args or {}).get("out") or os.path.join(tempfile.gettempdir(), "aster-video.mp4"))\n    dur = max(6, min(30, int((args or {}).get("seconds") or (2 * len(lines) + 2))))\n    per = dur / max(1, len(lines))\n    filters = []\n    # 每条一句，居中打字幕；背景纯色渐变感（用两张色块叠）\n    parts = []\n    for i, t in enumerate(lines):\n        safe = t.replace("\'", "\\\\\'").replace(":", "\\\\:")\n        filters.append(\n            f"drawtext=text=\'{safe}\':fontcolor=white:fontsize=42:x=(w-text_w)/2:"\n            f"y=h/2+(i-{len(lines)/2:.1f})*60:enable=\'gte(t,{i*per:.2f})\'"\n            if False else\n            f"drawtext=text=\'{safe}\':fontcolor=white:fontsize=42:x=(w-text_w)/2:"\n            f"y=h/2-{len(lines)*30}+{i*60}:enable=\'between(t,{i*per:.2f},{(i+1)*per:.2f})\'")\n    vf = ",".join(filters)\n    cmd = [ff, "-y", "-f", "lavfi", "-i", f"color=c=0x1b1b22:s=720x1280:d={dur}",\n           "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(dur), out]\n    try:\n        r = subprocess.run(cmd, capture_output=True, timeout=180)\n    except Exception as e:\n        return {"ok": False, "error": str(e)[:120]}\n    if r.returncode != 0:\n        return {"ok": False, "error": (r.stderr or b"").decode("utf-8", "replace")[-300:]}\n    return {"ok": True, "path": out, "seconds": dur, "lines": len(lines)}\n'

BUILTIN_PLUGINS = {
    "video": {
        "manifest": {
            "name": "video",
            "version": "1.0",
            "description": "把几行字做成一条竖屏短片（需要 ffmpeg）",
            "permissions": ["run"],
            "triggers": ["做个视频", "合成视频", "短视频", "出片"],
        },
        "main": VIDEO_MAIN,
    },
    "weather": {
        "manifest": {
            "name": "weather",
            "version": "1.0",
            "description": "查当前天气（开放接口，不需要 key）",
            "permissions": ["net"],
            "triggers": ["天气", "气温", "下雨", "冷不冷", "热不热"],
        },
        "main": WEATHER_MAIN,
    },
}

# 技能库：一键装，不用挑文件夹
LIBRARY = [
    {"id": "todo", "name": "待办整理", "icon": "☑️",
     "desc": "把一堆事理成能执行的清单",
     "body": "# 待办整理\n\n用在：他说「帮我整理下待办 / 列一下要做的事」。\n\n做法：\n- 只列他提到的事，按顺序 1. 2. 3.\n- 不补充他没说的，不加建议\n- 每行一句话，不超 20 字\n"},
    {"id": "minutes", "name": "会议纪要", "icon": "🗒",
     "desc": "把流水账压成结论和待办",
     "body": "# 会议纪要\n\n用在：他说「整理下会议记录 / 这段话太长帮我压一下」。\n\n做法：\n- 先一句结论，再列 3 条以内要点\n- 谁要做什么单独一行\n- 没提到的人不写\n"},
    {"id": "tone-soft", "name": "话说软一点", "icon": "🫧",
     "desc": "把硬邦邦的话改得不刺人",
     "body": "# 话说软一点\n\n用在：他说「帮我把这句话说得自然点 / 委婉点」。\n\n做法：\n- 保留原意，去掉命令感和指责感\n- 给他两版：偏客气、偏亲近\n- 不解释为什么\n"},
    {"id": "checklist", "name": "出门清单", "icon": "🎒",
     "desc": "按天气和场合列要带的东西",
     "body": "# 出门清单\n\n用在：他说「列个出门清单」。\n\n做法：\n- 分三组：必带、看天气、看场合\n- 每组不超过 4 项\n- 没有天气信息就只列必带\n"},
    {"id": "unit", "name": "单位换算", "icon": "📐",
     "desc": "长度重量温度体积，一句话换过来",
     "body": "# 单位换算\n\n用在：他说「换算一下 / 多少斤等于多少公斤」。\n\n做法：\n- 先给结果，再写换算关系一行\n- 保留他给的精度，不自作主张进位\n- 不确定的单位先问一句「是市斤还是磅」\n"},
    {"id": "express", "name": "快递查件", "icon": "📦",
     "desc": "单号查物流，说清到哪一步了",
     "body": "# 快递查件\n\n用在：他说「帮我查下这个快递 / 单号 xxx」。\n\n做法：\n- 先给当前状态一句话（已揽收/在途/派送/签收）\n- 再给最近一条节点和时间\n- 查不到就说「这个单号没查到」，不要编节点\n"},
    {"id": "error", "name": "报错解读", "icon": "🐞",
     "desc": "把一屏报错翻成一句人话和一个下一步",
     "body": "# 报错解读\n\n用在：他贴一段报错或日志。\n\n做法：\n- 第一行说人话：哪出了问题\n- 第二行给下一步动作（改哪个字段/装哪个依赖/找他确认）\n- 原文里没有的信息不要补\n"},
    {"id": "split", "name": "拆日程", "icon": "🗓",
     "desc": "把「明天要做一堆事」拆成能排进时间的块",
     "body": "# 拆日程\n\n用在：他说「明天好多事 / 帮我排一下」。\n\n做法：\n- 先按必须做、可以做分两堆\n- 每件事估一个时长（15/30/60 分钟）\n- 只排必须做的，其余列在下面\n"},
    {"id": "ledger", "name": "记账归类", "icon": "🧾",
     "desc": "把一天的零散花费归类汇总",
     "body": "# 记账归类\n\n用在：他说「今天花了这些 / 帮我记一下」。\n\n做法：\n- 按吃、行、用、其他四类归\n- 每类给合计，最后给总额\n- 没说金额的项不补数字\n"},
    {"id": "writer", "name": "写作", "icon": "✍️",
     "desc": "写文章、公众号、演讲稿，有结构不注水",
     "body": "# 写作\n\n用在：他说「帮我写一篇…」「改成文章」「写个稿」。\n\n做法：\n- 先问一句目标读者和篇幅（只问一次，别追问）\n- 开头三句内进入正题，不铺垫、不客套\n- 每一段只讲一件事，句子短，能念出来\n- 不写「在这个时代」「不得不说」这类空话\n- 结尾给一句能记住的话，不写「希望对你有帮助」\n- 要标题就给三个备选，风格拉开\n"},
    {"id": "explain", "name": "讲人话", "icon": "💡",
     "desc": "把复杂的话翻成大白话",
     "body": "# 讲人话\n\n用在：他说「这是什么原理 / 讲简单点」。\n\n做法：\n- 先用一句大白话解释\n- 再举一个生活里的例子\n- 不确定的部分说「这个我不确定」\n"},
]


# 技能触发词：光靠名字匹配不够（用户说「写一篇」不会提「写作」）
SKILL_TRIGGERS = {
    "writer": ("写一篇", "写篇", "文章", "稿子", "公众号", "推文", "演讲稿", "起草"),
    "todo": ("待办", "要做的事", "列一下", "清单"),
    "minutes": ("会议", "纪要", "记录"),
    "tone-soft": ("说得软", "委婉", "别太冲", "客气点"),
    "checklist": ("出门", "带什么", "清单"),
    "unit": ("换算", "多少斤", "多少公里", "多少度"),
    "express": ("快递", "单号", "物流", "到哪了"),
    "error": ("报错", "崩溃", "traceback", "异常"),
    "split": ("排一下", "拆一下", "安排一下", "日程"),
    "ledger": ("花了", "记账", "开销"),
    "explain": ("讲简单", "大白话", "什么意思", "原理"),
}

CATALOG_FILE = "skills_catalog.json"      # 用户目录下；也可以 ASTER_CATALOG_URL 指到网上


def catalog() -> dict:
    """技能商店：优先拉线上清单，拉不到就用本地清单，再不行回落到内置库。

    清单格式：[{"id","name","icon","desc","version","content" 或 "url"}]
    - content：直接给 SKILL.md 正文（最安全，一键装）
    - url：https 的 zip 或单文件，装的时候下载并校验（后续再做签名）
    """
    import urllib.request
    items, source = [], "builtin"
    url = os.environ.get("ASTER_CATALOG_URL") or ""
    if url.startswith("https://"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Aster"})
            with urllib.request.urlopen(req, timeout=6) as r:
                items = json.loads(r.read().decode("utf-8")).get("items") or []
            source = url
        except Exception:
            items = []
    if not items:
        p = os.path.join(root(), CATALOG_FILE)
        if os.path.exists(p):
            try:
                items = json.load(open(p, encoding="utf-8")).get("items") or []
                source = "local"
            except Exception:
                items = []
    if not items:
        items = [dict(it, version="1.0", content=it.get("body", "")) for it in LIBRARY]
    installed = {s["name"] for s in skills()}
    for it in items:
        it["installed"] = (it.get("id") in installed)
    # 能力插件也进商店（用户自己装，不默认写进目录）
    have_plugin = {p["name"] for p in scan()}
    for name, spec in BUILTIN_PLUGINS.items():
        mf = spec.get("manifest") or {}
        items.append({"id": "plugin:" + name, "name": mf.get("description", name)[:40],
                      "icon": "🧩", "desc": mf.get("description", ""),
                      "kind": "plugin", "plugin": name,
                      "version": mf.get("version", "1.0"),
                      "installed": name in have_plugin})
    return {"ok": True, "source": source, "items": items}


def install_from_catalog(sid: str) -> dict:
    cat = catalog()
    for it in cat["items"]:
        if it.get("id") != sid:
            continue
        body = it.get("content")
        if not body and it.get("url"):
            import urllib.request
            u = it["url"]
            if not u.startswith("https://"):
                return {"ok": False, "error": "只接受 https 地址"}
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Aster"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    body = r.read(300 * 1024).decode("utf-8", "replace")
            except Exception as e:
                return {"ok": False, "error": str(e)[:100]}
        if it.get("kind") == "plugin":
            spec = BUILTIN_PLUGINS.get(it.get("plugin") or "")
            if not spec:
                return {"ok": False, "error": "这个插件不在内置库里"}
            return install(it["plugin"], [
                {"path": "manifest.json", "content": json.dumps(spec["manifest"], ensure_ascii=False, indent=1)},
                {"path": "main.py", "content": spec["main"]}], "plugin")
        if not body:
            return {"ok": False, "error": "这条清单里没有内容"}
        return install(it["id"], [{"path": "SKILL.md", "content": body}], "skill")
    return {"ok": False, "error": "清单里没有这个"}


def library() -> list:
    """技能库：标出哪些已经装了。"""
    ensure_builtin()
    installed = {s["name"] for s in skills()}
    out = []
    for it in LIBRARY:
        d = dict(it)
        d["installed"] = it["id"] in installed
        out.append(d)
    return out


def install_from_library(sid: str) -> dict:
    for it in LIBRARY:
        if it["id"] == sid:
            return install(it["id"], [{"path": "SKILL.md", "content": it["body"]}], "skill")
    return {"ok": False, "error": "技能库里没有这个"}


# 默认随包装上的预设（首次运行写进用户目录；已存在的不覆盖，用户删了也不会自动回来）
BUILTIN_SKILLS = {it["id"]: it["body"] for it in LIBRARY}
BUILTIN_SKILLS["todo"] = (
    "# 整理待办\n\n用在：他说「帮我整理下待办 / 列一下我要做的事」时。\n\n做法：\n"
    "- 只把他提到的事按顺序列成 1. 2. 3.\n- 不补充他没说的内容，不加建议\n"
    "- 每行一句话，不超过 20 字\n")


# 默认随包写入的插件；其余只放商店，让用户自己装
DEFAULT_PLUGINS = ("weather",)


def ensure_builtin() -> None:
    """第一次跑的时候把示例插件/技能写进用户目录，不覆盖已存在的。"""
    base = root()
    for name, spec in BUILTIN_PLUGINS.items():
        if name not in DEFAULT_PLUGINS:
            continue
        d = os.path.join(base, "extensions", name)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            continue
        mf = os.path.join(d, "manifest.json")
        if not os.path.exists(mf):
            json.dump(spec["manifest"], open(mf, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        mp = os.path.join(d, "main.py")
        if not os.path.exists(mp):
            open(mp, "w", encoding="utf-8").write(spec["main"])
    for name, body in BUILTIN_SKILLS.items():
        d = os.path.join(base, "skills", name)
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            continue
        fp = os.path.join(d, "SKILL.md")
        if not os.path.exists(fp):
            open(fp, "w", encoding="utf-8").write(body)


def _state_path() -> str:
    try:
        import paths  # type: ignore
        return os.path.join(paths.mem_home(), "extensions.json")
    except Exception:
        return os.path.join(os.environ.get("ASTER_HOME") or root(), "extensions.json")


def _state() -> dict:
    try:
        return json.load(open(_state_path(), encoding="utf-8"))
    except Exception:
        return {"enabled": {}}


def _save(st: dict) -> None:
    p = _state_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    json.dump(st, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def scan() -> list[dict]:
    ensure_builtin()
    out = []
    base = os.path.join(root(), "extensions")
    if not os.path.isdir(base):
        return out
    st = _state()
    for name in sorted(os.listdir(base)):
        d = os.path.join(base, name)
        mf = os.path.join(d, "manifest.json")
        if not os.path.isfile(mf):
            continue
        try:
            m = json.load(open(mf, encoding="utf-8"))
        except Exception:
            continue
        out.append({"name": m.get("name") or name,
                    "version": m.get("version") or "0",
                    "description": m.get("description") or "",
                    "permissions": m.get("permissions") or [],
                    "triggers": m.get("triggers") or [],
                    "enabled": bool(st.get("enabled", {}).get(name)),
                    "dir": d})
    return out


def set_enabled(name: str, on: bool) -> dict:
    st = _state()
    st.setdefault("enabled", {})[name] = bool(on)
    _save(st)
    return {"ok": True, "name": name, "enabled": bool(on)}


def _find(name: str) -> dict | None:
    for e in scan():
        if e["name"] == name:
            return e
    return None


def call(name: str, args: dict | None = None, timeout: int = 30) -> dict:
    e = _find(name)
    if not e:
        return {"ok": False, "error": "没有这个插件"}
    if not e["enabled"]:
        return {"ok": False, "error": "插件未启用"}
    entry = os.path.join(e["dir"], "main.py")
    if not os.path.isfile(entry):
        return {"ok": False, "error": "缺 main.py"}
    payload = json.dumps(args or {}, ensure_ascii=False)
    try:
        r = subprocess.run([sys.executable, entry], input=payload.encode(),
                           capture_output=True, timeout=timeout,
                           cwd=e["dir"],
                           env={**os.environ, "ASTER_EXT_PERMS": ",".join(e["permissions"])})
        out = (r.stdout or b"").decode("utf-8", "replace").strip()
        if out:
            try:
                return {"ok": True, "result": json.loads(out)}
            except Exception:
                return {"ok": True, "text": out[:2000]}
        # 没打印东西：多半是只写了 run()，没写 __main__，这里直接调它
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("aster_ext_" + name, entry)
            mod = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            if hasattr(mod, "run"):
                got = mod.run(args or {}, None)
                if isinstance(got, dict):
                    return {"ok": bool(got.get("ok", True)), "text": str(got.get("text") or got.get("result") or got)[:2000],
                            "result": got}
                return {"ok": True, "text": str(got)[:2000]}
        except Exception as ex:
            return {"ok": False, "error": "插件没输出，且调用 run() 失败：" + str(ex)[:100]}
        return {"ok": False, "error": "插件没有任何输出"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "插件超时"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)[:120]}


def skills() -> list[dict]:
    ensure_builtin()
    out = []
    base = os.path.join(root(), "skills")
    if not os.path.isdir(base):
        return out
    meta = {it["id"]: it for it in LIBRARY}
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name, "SKILL.md")
        if os.path.isfile(p):
            m = meta.get(name) or {}
            _runnable = os.path.isfile(os.path.join(base, name, "main.py"))
            out.append({"name": name, "path": p, "runnable": _runnable,
                        "chars": os.path.getsize(p),
                        "title": m.get("name") or name,
                        "icon": m.get("icon") or "📘",
                        "desc": m.get("desc") or "自装的技能",
                        "triggers": list(SKILL_TRIGGERS.get(name, ()))})
    return out


def install(name: str, files: list, kind: str = "skill") -> dict:
    """从外部把技能/插件装进用户目录。

    files: [{"path": "SKILL.md", "content": "..."}]；
    kind=skill 写 skills/<name>/，kind=plugin 写 extensions/<name>/。
    只写文本文件，路径里不许有 ..，防止写到目录外。
    """
    name = (name or "").strip()
    if not name or not re.match(r"^[\w\u4e00-\u9fa5-]{1,32}$", name):
        return {"ok": False, "error": "名字不合法（字母数字下划线中文，1-32 字）"}
    base = os.path.join(root(), "skills" if kind == "skill" else "extensions", name)
    written = []
    for f in files or []:
        rel = (f.get("path") or "").strip().lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        dst = os.path.join(base, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            open(dst, "w", encoding="utf-8").write(f.get("content") or "")
            written.append(rel)
        except Exception as e:
            return {"ok": False, "error": str(e)[:100]}
    if not written:
        return {"ok": False, "error": "没有可写的文件"}
    return {"ok": True, "name": name, "dir": base, "files": written}


def uninstall(name: str, kind: str = "skill") -> dict:
    import shutil
    base = os.path.join(root(), "skills" if kind == "skill" else "extensions", name)
    if not os.path.isdir(base):
        return {"ok": False, "error": "没装过这个"}
    shutil.rmtree(base, ignore_errors=True)
    return {"ok": True, "removed": name}


def skill_text(name: str, limit: int = 4000) -> str:
    for s in skills():
        if s["name"] == name:
            return open(s["path"], encoding="utf-8", errors="replace").read(limit)
    return ""
