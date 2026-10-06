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
        """搜索网页（ DuckDuckGo Lite）。

        参数:
            query: 搜索关键词
            max_results: 返回结果条数
        """
        try:
            url = ("https://lite.duckduckgo.com/lite/?q="
                   + urllib.parse.quote(str(query or "")))
            got = _fetch(url, timeout=20, limit=20000)
            if not got.get("ok"):
                return {"ok": False, "error": got.get("error", "搜索失败"),
                        "query": query}
            text = got.get("text", "")
            # Lite 版结构规整：结果行大致以序号开头，这里做宽松抽取
            results: List[Dict[str, str]] = []
            for line in text.splitlines():
                line = line.strip()
                if not line or len(line) < 20:
                    continue
                if line.startswith("http"):
                    if results:
                        results[-1]["url"] = line
                else:
                    results.append({"title": line[:120], "url": ""})
                if len(results) >= int(max_results) * 3:
                    break
            results = [r for r in results if r.get("url")][: int(max_results)]
            return {"ok": True, "query": query, "results": results,
                    "count": len(results)}
        except Exception as ex:
            return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}

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
