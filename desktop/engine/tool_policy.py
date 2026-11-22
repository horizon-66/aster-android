#!/usr/bin/env python3
"""命令白名单：按「命令 + 参数模式」判，不认命令名。

原则：
- 黑名单优先于白名单，命中即拒（参数怎么拼都不放）
- 不在白名单里的命令一律拒（默认 deny）
- 真执行还要另外两把锁：全局开关 + 每次人工确认（见 tool_loop）

对外：
  check(cmd) -> (allowed: bool, level: str, reason: str)
  idem_key(cmd, cwd) -> str
"""
from __future__ import annotations
import hashlib, os, re, shlex

# 黑名单：正则匹配整条命令，命中即拒
BLACK = [
    (r"\brm\b", "删除类命令一律不在白名单"),
    (r"\bmkfs\b|\bdd\b", "磁盘级操作"),
    (r"\bsudo\b|\bsu\b", "提权"),
    (r"\bcurl\b|\bwget\b|\bnc\b|\bssh\b|\bscp\b", "外联/传输"),
    (r">\s*/", "写系统路径"),
    (r"\bchmod\s+777\b|\bchown\b", "权限改动"),
    (r"\bshutdown\b|\breboot\b|\bkillall\b", "系统动作"),
    (r"\.\./", "相对路径越界"),
    (r"\s-(exec|execdir|delete|ok|okdir)\b", "find 的 -exec/-delete 会绕过白名单"),
    (r"/etc/|/System/|/Library/", "系统目录"),
    (r"\$\(|`", "命令替换"),
]

# 白名单：命令名 + 允许的参数形态（参数逐个校验：不允许出现 -r/-f 组合、不允许写操作）
WHITE = [
    ("mkdir", r"^mkdir(\s+-p)?(\s+[\w./~-]+)+$"),
    ("touch", r"^touch(\s+[\w./~-]+)+$"),
    ("echo",  r"^echo\s+[^|;&<>]{1,200}$"),
    ("pwd",   r"^pwd$"),
    ("which", r"^which\s+[\w./-]+$"),
    ("python3-mod", r"^python3\s+-m\s+(pytest|unittest)(\s+[\w./=-]+)*$"),
    ("python3-version", r"^python3\s+--version$"),
    # 只允许跑任务目录里的 .py，不许 -c、不许绝对路径、不许 ../
    ("python", r"^python\s+(?!/|-c)(?!.*\.\.)(?!/)[\w./-]+\.py(\s+[\w./=-]+)*$"),
    ("python3", r"^python3\s+(?!/|-c)(?!.*\.\.)(?!/)[\w./-]+\.py(\s+[\w./=-]+)*$"),
    ("ls",    r"^ls(\s+[-aAlhtr1]+)?(\s+[\w./~-]+)*$"),
    ("pwd",   r"^pwd$"),
    ("cat",   r"^cat(\s+[\w./~-]+)+$"),
    ("head",  r"^head(\s+-\d+)?(\s+[\w./~-]+)+$"),
    ("tail",  r"^tail(\s+-\d+)?(\s+[\w./~-]+)+$"),
    ("wc",    r"^wc(\s+-l)?(\s+[\w./~-]+)+$"),
    ("grep",  r"^grep(\s+-[ri]+)?\s+[\w\u4e00-\u9fa5\"'|-]+(\s+[\w./~-]+)+$"),
    ("find",  r"""^find\s+[\w./~-]+(\s+-(name|type)\s+("[^"]{1,40}"|'[^']{1,40}'|[\w.*?-]+))?(\s+-maxdepth\s+\d+)?$"""),
    ("git",   r"^git\s+(status|log|diff|show|branch)(\s+[\w./~-]+)*$"),
    # python3 / node 只认「登记过哈希的脚本」，脚本一改就重新要确认
    ("python3", r"^python3\s+[\w./~-]+\.py(\s+[\w./~-]+)*$"),
    ("node",  r"^node\s+[\w./~-]+\.(mjs|js)(\s+[\w./~-]+)*$"),
]



# 脚本白名单：路径 + sha256。没有登记、或哈希对不上，一律拒。
REGISTRY_NAME = "tool-allow-scripts.json"


def _registry_paths() -> list[str]:
    base = os.environ.get("ASTER_WORKSPACE") or os.path.expanduser("~/Documents/Aster")
    return [os.path.join(base, "scripts", REGISTRY_NAME),
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "scripts", REGISTRY_NAME)]


def _registry() -> dict:
    import json
    for p in _registry_paths():
        try:
            if os.path.isfile(p):
                return json.load(open(p, encoding="utf-8")) or {}
        except Exception:
            continue
    return {}


def _script_ok(cmd: str) -> tuple[bool, str]:
    """python3/node 的脚本必须登记过且哈希一致。"""
    import hashlib
    parts = shlex.split(cmd)
    if len(parts) < 2:
        return False, "没有脚本路径"
    script = parts[1]
    path = script if os.path.isabs(script) else os.path.join(
        os.environ.get("ASTER_WORKSPACE") or os.path.expanduser("~/Documents/Aster"), script)
    path = os.path.realpath(path)
    reg = _registry()
    base = os.environ.get("ASTER_WORKSPACE") or os.path.expanduser("~/Documents/Aster")
    rel = os.path.relpath(path, os.path.realpath(base))
    if rel.startswith(".."):
        return False, "脚本不在工作区"
    want = reg.get(rel) or reg.get(script)
    if not want:
        return False, "脚本没登记过，拒绝执行"
    try:
        h = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    except Exception:
        return False, "脚本读不了"
    if h != want:
        return False, "脚本内容变过，需要重新确认"
    return True, "已登记脚本"


def check(cmd: str) -> tuple[bool, str, str]:
    c = (cmd or "").strip()
    # 允许「&&」串起来的白名单命令，但每一段都得单独过
    if " && " in c:
        for part in c.split(" && "):
            ok, lv, why = check(part.strip())
            if not ok:
                return False, lv, why
        return True, "ok", "白名单（组合命令）"
    if not c:
        return False, "deny", "空命令"
    if len(c) > 400:
        return False, "deny", "命令过长"
    for pat, why in BLACK:
        if re.search(pat, c):
            return False, "deny", "黑名单：" + why
    for name, pat in WHITE:
        if re.match(pat, c):
            if name in ("python3", "python", "node"):
                ok, why = _script_ok(c)
                if not ok:
                    return False, "deny", "拒绝：" + why
                return True, "ask", "已登记脚本（执行前仍需确认）"
            if name == "find" and re.search(r"\s-(exec|execdir|delete|ok|okdir)\b", c):
                return False, "deny", "拒绝：find 的危险参数"
            return True, "ask", "白名单命中：%s（执行前仍需确认）" % name
    return False, "deny", "不在白名单"


def idem_key(cmd: str, cwd: str = "") -> str:
    """同一命令在同一目录重复执行，回灌只记一次。"""
    raw = (cmd or "").strip() + "\x00" + (cwd or os.getcwd())
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
