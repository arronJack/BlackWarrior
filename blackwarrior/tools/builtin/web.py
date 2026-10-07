"""联网工具：搜索 + 读网页。

用标准库实现（urllib），不引入 requests/bs4。正文抽取用轻量启发式规则：
优先取 ``<article>``/``<main>``，否则去掉脚本样式后按段落密度取正文。
效果不如专业解析库，但零依赖、离线可装。
"""

from __future__ import annotations

import html
import re
import urllib.parse
import urllib.request
from typing import Any, Dict, List

from ..registry import RISK_CAUTION, RISK_SAFE

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>",
                        re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")

#: 允许的协议白名单（防 file:// / gopher:// 之类的 SSRF 变体）
ALLOWED_SCHEMES = ("http", "https")


def _clean_text(raw: str, limit: int = 8000) -> str:
    text = _SCRIPT_RE.sub(" ", raw)
    text = _TAG_RE.sub("\n", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()[:limit]


def _fetch(url: str, timeout: int = 20, limit: int = 8000) -> Dict[str, Any]:
    scheme = urllib.parse.urlparse(url).scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        return {"ok": False, "error": f"不支持的协议：{scheme}"}
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            ctype = resp.headers.get("Content-Type", "")
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

    if "json" in ctype.lower():
        return {"ok": True, "url": url, "text": raw[:limit], "kind": "json"}
    return {"ok": True, "url": url, "text": _clean_text(raw, limit), "kind": "html"}


def register(reg: Any, ctx: Any) -> None:
    def web_search(query: str, max_results: int = 5) -> Dict[str, Any]:
        """搜索网页。多源依次回退，**默认走必应国内**。

        为什么不用 DuckDuckGo 当主力（2026-10-07 实测）：
        在国内网络环境下 ``lite.duckduckgo.com`` 会被代理挡掉，稳定报
        ``Tunnel connection failed: 502``——而"帮我搜一下今天的国内新闻"
        恰恰是最典型的用法。实测同一台机器：

            cn.bing.com       0.3s  ✅
            so.toutiao.com    1.6s  ✅（新闻最全）
            www.sogou.com     1.3s  ✅
            lite.duckduckgo   10s   ❌ 502
            www.baidu.com     0.2s  ⚠ 只返回 1.4KB 验证页（反爬）

        所以顺序是：必应国内 → 头条搜索 → 搜狗 → DuckDuckGo（留给
        能直连的境外网络）。任一源出结果就返回，并在 ``engine`` 里
        标明实际用的是哪个——搜索失败时必须能一眼看出是"网络不通"
        还是"被反爬了"。
        """
        q = str(query or "").strip()
        if not q:
            return {"ok": False, "error": "搜索词为空", "query": query}
        want = max(1, int(max_results or 5))
        errors: List[str] = []
        for engine, fetch in (("bing_cn", _search_bing_cn),
                              ("toutiao", _search_toutiao),
                              ("sogou", _search_sogou),
                              ("duckduckgo", _search_ddg)):
            try:
                results = fetch(q, want)
            except Exception as ex:
                errors.append(f"{engine}: {type(ex).__name__}: {ex}")
                continue
            if results:
                return {"ok": True, "query": q, "results": results,
                        "count": len(results), "engine": engine}
            errors.append(f"{engine}: 无结果")
        return {"ok": False, "error": "所有搜索源都没结果：" + "；".join(errors[:4]),
                "query": q, "engines_tried": errors}

    def web_read(url: str, limit: int = 8000) -> Dict[str, Any]:
        """读取网页正文。

        参数:
            url: 网页地址（仅 http/https）
            limit: 正文截断长度
        """
        got = _fetch(str(url or ""), limit=int(limit))
        if not got.get("ok"):
            return got
        return {"ok": True, "url": got["url"], "text": got["text"],
                "chars": len(got["text"])}

    def web_headlines(query: str) -> Dict[str, Any]:
        """搜索并只读标题（省 token 的搜索方式）。

        参数:
            query: 搜索关键词
        """
        res = web_search(query, max_results=5)
        if not res.get("ok"):
            return res
        return {"ok": True, "query": query,
                "headlines": [r.get("title", "") for r in res.get("results", [])]}

    def open_url(url: str) -> Dict[str, Any]:
        """用系统默认浏览器打开网址（用户能直接看到浏览器窗口）。"""
        import webbrowser

        u = str(url or "").strip()
        if not u:
            return {"ok": False, "error": "缺少 url 参数"}
        if not re.match(r"^https?://", u, re.I):
            u = "https://" + u
        try:
            opened = bool(webbrowser.open(u, new=1, autoraise=True))
        except Exception as ex:
            return {"ok": False, "url": u, "error": f"{type(ex).__name__}: {ex}"}
        # webbrowser.open 在无桌面环境可能返回 False——如实上报
        return {"ok": opened, "url": u,
                "note": "" if opened else "系统未能启动浏览器（无桌面环境或无默认浏览器）"}

    reg.register("web_search", web_search, risk=RISK_SAFE, category="web",
                 description="搜索网页并返回结果标题与链接")
    reg.register("web_read", web_read, risk=RISK_SAFE, category="web",
                 description="读取网页正文（自动去标签）")
    reg.register("web_headlines", web_headlines, risk=RISK_SAFE, category="web",
                 description="只取搜索结果的标题（省 token）")
    reg.register("open_url", open_url, risk=RISK_SAFE, category="web",
                 description="用系统默认浏览器打开网址（用户说『帮我打开某网站』时用这个，"
                             "不要只给链接让用户自己开）")


# ============================================================ 搜索源
# 每个源都只依赖标准库的 urllib + 正则，不引入 bs4/requests。
# 反爬与改版是常态，所以**每个源独立降级**：一个挂了换下一个，
# 并把失败原因如实带进返回值（"搜不到"必须能区分是网络还是反爬）。

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

_SKIP_HOSTS = ("bing.com", "sogou.com", "toutiao.com", "baidu.com",
               "duckduckgo.com", "microsoft.com", "msn.com")


def _http_get(url: str, timeout: int = 20) -> str:
    """带浏览器 UA 的 GET，返回 HTML 文本。失败抛异常交给上层降级。"""
    import ssl

    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    ctx = ssl.create_default_context()
    # 证书链不完整时仍然尝试——搜索页本身不是机密数据，
    # 但**绝不用不校验证书去访问要提交凭据的地址**。
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def _clean_title(raw: str) -> str:
    t = re.sub(r"<[^>]+>", "", raw or "")
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", t).strip()


def _ok_url(u: str) -> bool:
    if not u or not u.startswith("http"):
        return False
    return not any(h in u for h in _SKIP_HOSTS)


def _search_bing_cn(query: str, want: int) -> List[Dict[str, str]]:
    """必应国内。结果在 ``<li class="b_algo">`` 里，结构最规整。"""
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query) + "&setlang=zh-CN"
    html = _http_get(url)
    out: List[Dict[str, str]] = []
    for m in re.finditer(r'<li class="b_algo".*?</li>', html, re.S):
        blk = m.group(0)
        a = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                      blk, re.S)
        if not a:
            continue
        href, title = a.group(1), _clean_title(a.group(2))
        if not title or not _ok_url(href):
            continue
        snip = ""
        p = re.search(r'<p[^>]*>(.*?)</p>', blk, re.S)
        if p:
            snip = _clean_title(p.group(1))[:180]
        out.append({"title": title[:120], "url": href, "snippet": snip})
        if len(out) >= want:
            break
    return out


def _search_toutiao(query: str, want: int) -> List[Dict[str, str]]:
    """头条搜索。新闻类查询的结果质量最好。"""
    url = "https://so.toutiao.com/search?keyword=" + urllib.parse.quote(query)
    html = _http_get(url, timeout=25)
    out: List[Dict[str, str]] = []
    # 头条是前端渲染 + 内嵌 JSON，两种都试
    for m in re.finditer(r'"title":"([^"]{6,120})".*?"url":"(https?://[^"]+)"',
                         html, re.S):
        title = m.group(1).encode().decode("unicode_escape", "ignore")
        title = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9，。！？、：；（）《》\"'\- ]", "", title)
        href = m.group(2).replace("\\/", "/")
        if len(title) < 6 or not _ok_url(href):
            continue
        out.append({"title": title[:120], "url": href, "snippet": ""})
        if len(out) >= want:
            break
    return out


def _search_sogou(query: str, want: int) -> List[Dict[str, str]]:
    url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
    html = _http_get(url, timeout=25)
    out: List[Dict[str, str]] = []
    for m in re.finditer(r'<h3[^>]*class="vr-title"[^>]*>\s*<a[^>]+href="([^"]+)"'
                         r'[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), _clean_title(m.group(2))
        if not title or not _ok_url(href):
            continue
        out.append({"title": title[:120], "url": href, "snippet": ""})
        if len(out) >= want:
            break
    return out


def _search_ddg(query: str, want: int) -> List[Dict[str, str]]:
    """DuckDuckGo Lite。留给能直连的境外网络。"""
    url = "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query)
    html = _http_get(url)
    out: List[Dict[str, str]] = []
    for line in html.splitlines():
        line = line.strip()
        if line.startswith("http") and _ok_url(line):
            if out:
                out[-1]["url"] = line
        elif len(line) > 20 and out and not out[-1].get("url"):
            out[-1]["title"] = _clean_title(line)[:120]
    return [r for r in out if r.get("url")][:want]
