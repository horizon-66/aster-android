"""抓网页 + 无 key 搜索（免费档）。

fetch(url)  : 抓回来去掉脚本/样式，只留正文，转成可读段落
ddg(q)      : DuckDuckGo 的纯 HTML 结果页，解析标题/链接/摘要（无需 key）
bing_rss(q) : 必应 RSS，备用
"""
from __future__ import annotations
import html as _html
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(800 * 1024)
        enc = r.headers.get_content_charset() or "utf-8"
    return raw.decode(enc, "replace")


def fetch(url: str, limit: int = 4000) -> str:
    """抓一个页面，返回清理后的正文。"""
    if not url.startswith("http"):
        return ""
    try:
        doc = _get(url)
    except Exception as e:
        return "抓不到（" + str(e)[:60] + "）"
    doc = re.sub(r"(?is)<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", " ", doc)
    doc = re.sub(r"(?is)<!--.*?-->", " ", doc)
    # 优先取 article/main，没有就整页
    m = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", doc)
    body = m.group(2) if m else doc
    body = re.sub(r"(?is)<br\s*/?>", "\n", body)
    body = re.sub(r"(?is)</(p|div|h[1-6]|li|tr)>", "\n", body)
    body = re.sub(r"(?is)<[^>]+>", " ", body)
    body = _html.unescape(body)
    lines = [re.sub(r"[ \t\u3000]+", " ", x).strip() for x in body.splitlines()]
    lines = [x for x in lines if len(x) >= 8]
    out, seen = [], set()
    for x in lines:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    text = "\n".join(out)
    return text[:limit] or "页面里没抓到正文"


def baidu(q: str, n: int = 6) -> list:
    """百度，国内最稳；抓标题与摘要，链接多为跳转，只取标题供判断。"""
    try:
        doc = _get("https://www.baidu.com/s?wd=" + urllib.parse.quote(q))
    except Exception:
        return []
    rows = []
    for m in re.finditer(r"(?is)<h3[^>]*>(.*?)</h3>", doc):
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if title and len(title) > 3:
            rows.append({"title": title, "url": "", "snippet": ""})
        if len(rows) >= n:
            break
    return rows


def ddg(q: str, n: int = 6) -> list:
    """DuckDuckGo HTML 版，无需 key。"""
    try:
        doc = _get("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q))
    except Exception:
        return []
    rows = []
    for m in re.finditer(r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', doc):
        link, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2))
        if "uddg=" in link:
            try:
                link = urllib.parse.unquote(re.search(r"uddg=([^&]+)", link).group(1))
            except Exception:
                pass
        rows.append({"title": _html.unescape(title).strip(), "url": link, "snippet": ""})
        if len(rows) >= n:
            break
    snips = [re.sub(r"<[^>]+>", "", s) for s in
             re.findall(r'(?is)<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', doc)]
    for i, sn in enumerate(snips[:len(rows)]):
        rows[i]["snippet"] = _html.unescape(sn).strip()[:200]
    return rows


def bing_rss(q: str, n: int = 6) -> list:
    try:
        doc = _get("https://www.bing.com/search?format=rss&q=" + urllib.parse.quote(q))
    except Exception:
        return []
    rows = []
    for m in re.finditer(r"(?is)<item>(.*?)</item>", doc):
        blk = m.group(1)
        t = re.search(r"(?is)<title>(.*?)</title>", blk)
        l = re.search(r"(?is)<link>(.*?)</link>", blk)
        d = re.search(r"(?is)<description>(.*?)</description>", blk)
        if t and l:
            rows.append({"title": _html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip(),
                         "url": l.group(1).strip(),
                         "snippet": _html.unescape(re.sub(r"<[^>]+>", "", d.group(1))).strip()[:200] if d else ""})
        if len(rows) >= n:
            break
    return rows


def search(q: str, n: int = 6) -> str:
    rows = baidu(q, n) or bing_rss(q, n) or ddg(q, n)
    if not rows:
        return "没搜到"
    return "\n".join("- %s：%s（%s）" % (r["title"][:60], r["snippet"][:140], r["url"]) for r in rows)


def price_of(url: str) -> dict:
    """抓一个商品页，读标题与价格。读不到就明说，不编。"""
    import json as _j
    import re as _re
    import urllib.request as _u
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/122 Safari/537.36")
    try:
        req = _u.Request(url, headers={"User-Agent": ua, "Accept-Language": "zh-CN,zh;q=0.9"})
        with _u.urlopen(req, timeout=12) as r:
            raw = r.read()
            html = raw.decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": "打不开这个页面：" + str(e)[:60]}
    title = ""
    m = _re.search(r'(?is)<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html)
    if m:
        title = m.group(1).strip()
    if not title:
        m = _re.search(r"(?is)<title>(.*?)</title>", html)
        title = (m.group(1).strip() if m else "")[:80]
    prices = []
    for m in _re.finditer(r'"price"\s*:\s*"?([0-9.]+)', html):
        prices.append(m.group(1))
    for m in _re.finditer(r"¥\s*([0-9]+(?:\.[0-9]{1,2})?)", html):
        prices.append(m.group(1))
    for m in _re.finditer(r"(?is)<span[^>]*class=\"[^\"]*price[^\"]*\"[^>]*>\s*¥?\s*([0-9]+(?:\.[0-9]{1,2})?)", html):
        prices.append(m.group(1))
    clean = []
    for x in prices:
        try:
            v = float(x)
        except Exception:
            continue
        if 0.5 <= v <= 200000:
            clean.append(round(v, 2))
    clean = sorted(set(clean))[:5]
    if not clean:
        return {"ok": False, "title": title, "error": "页面里没找到价格（可能有反爬）"}
    return {"ok": True, "title": title, "prices": clean, "url": url}
