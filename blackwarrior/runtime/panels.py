"""信息面板 —— 让黑武士主动把"环境信息"准备好（天气 / 热点 / 人物卡）。

运行时预喂面板（天气 / 热点 / 人物卡）是这类桌面 Agent 的标配能力。
这里是一套**可降级**的面板系统：

- 天气：``wttr.in`` 取实时天气（无网络/未开 → 明确标注不可用，不假装有）；
- 热点：需要搜索 key 才能拉实时榜单，缺配置时返回"可扩展"占位（诚实降级）；
- 人物卡：内置少量常识人物 + 支持用户自定义（存进画像/记忆），被问"某人是谁"时给出。

所有面板都带 ``available`` 标志，UI 与上下文注入都能据此决定是否展示，
沿用黑武士"永不隐藏降级"的设计信条。
"""

from __future__ import annotations

import time
import urllib.request
from typing import Any, Dict, List, Optional

#: 内置常识人物卡（小体量，覆盖常见追问；用户自定义的可覆盖/扩充）。
_BUILTIN_PERSONS: Dict[str, Dict[str, str]] = {
    "爱因斯坦": {
        "identity": "阿尔伯特·爱因斯坦（1879–1955）",
        "intro": "理论物理学家，相对论提出者，1921 年诺贝尔物理学奖。",
        "tags": "物理,相对论,诺奖",
    },
    "马斯克": {
        "identity": "Elon Musk（1971–）",
        "intro": "企业家，特斯拉、SpaceX、xAI 等公司创始人。",
        "tags": "商业,特斯拉,SpaceX",
    },
    "李白": {
        "identity": "李白（701–762）",
        "intro": "唐代浪漫主义诗人，被誉「诗仙」。",
        "tags": "文学,唐诗,诗人",
    },
}

PANEL_KINDS = ("weather", "hotspot", "person")

#: wttr.in ``weatherDesc`` 英文 → 中文映射（v0.6.7）。
#: wttr.in 的 lang=zh 参数经常不生效（返回仍是英文），这里做兜底翻译。
#: 只收录常见词，未命中的走 _zh_desc 模糊规则，再未命中就保留英文原文。
_WTTR_ZH: Dict[str, str] = {
    "sunny": "晴", "clear": "晴", "sunny intervals": "晴间多云",
    "partly cloudy": "局部多云", "cloudy": "多云", "overcast": "阴",
    "light cloud": "少云", "mist": "薄雾", "fog": "雾", "freezing fog": "冻雾",
    "smoky haze": "烟霾", "haze": "霾", "smoke": "烟霾",
    "patchy rain nearby": "周边零星降雨",
    "patchy rain possible": "可能有小雨", "light rain": "小雨",
    "moderate rain": "中雨", "heavy rain": "大雨",
    "light rain shower": "小阵雨", "patchy light rain": "零星小雨",
    "moderate or heavy rain shower": "大阵雨",
    "light drizzle": "毛毛雨", "patchy light drizzle": "零星毛毛雨",
    "freezing drizzle": "冻毛毛雨", "patchy freezing drizzle possible": "可能有冻毛毛雨",
    "light freezing rain": "小冻雨",
    "thundery outbreaks possible": "可能有雷雨", "thunderstorm": "雷暴",
    "patchy light rain with thunder": "小雨伴雷", "moderate or heavy rain with thunder": "中到大雨伴雷",
    "patchy light snow with thunder": "小雪伴雷",
    "light snow": "小雪", "patchy light snow": "零星小雪", "moderate snow": "中雪",
    "heavy snow": "大雪", "light snow showers": "小阵雪", "blowing snow": "吹雪",
    "blizzard": "暴风雪", "patchy snow possible": "可能有小雪",
    "light sleet": "小雨夹雪", "light sleet showers": "小雨夹雪阵",
    "moderate or heavy sleet": "中到大雨夹雪", "patchy sleet possible": "可能有雨夹雪",
    "hail": "冰雹", "light hail": "小冰雹", "ice pellets": "冰粒",
}


def _zh_desc(en: str) -> str:
    """英文天气描述 → 中文（精确映射 → 模糊规则 → 原样返回）。"""
    s = str(en or "").strip()
    if not s:
        return s
    low = s.lower()
    hit = _WTTR_ZH.get(low)
    if hit:
        return hit
    # 模糊规则：顺序敏感（雷雨 > 雨夹雪 > 雪 > 阵雨 > 雨 > 毛毛雨）
    if "thunder" in low:
        return "雷雨"
    if "sleet" in low:
        return "雨夹雪"
    if "snow" in low:
        return "雪"
    if "drizzle" in low:
        return "毛毛雨"
    if "shower" in low:
        return "阵雨"
    if "rain" in low:
        return "雨"
    if "hail" in low:
        return "冰雹"
    if "sunny" in low or "clear" in low:
        return "晴"
    if "overcast" in low:
        return "阴"
    if "cloud" in low:
        return "多云"
    if "fog" in low:
        return "雾"
    if "mist" in low:
        return "薄雾"
    if "haze" in low or "smoke" in low:
        return "霾"
    return s


#: 热点数据源（60s API，GitHub 开源项目，免 key）。
#: 支持自部署：hotspot_base 换成自己的部署地址即可。
_HOT_SOURCES = {
    "weibo": ("微博热搜", "weibo"),
    "zhihu": ("知乎热榜", "zhihu"),
    "bili": ("B站热搜", "bili"),
    "douyin": ("抖音热点", "douyin"),
    "news": ("每日60秒读懂世界", "60s"),
}


class PanelManager:
    """面板管理器：拉取/缓存/降级。"""

    def __init__(self, config: Any, store: Any = None) -> None:
        self.config = config
        self.store = store
        self._cache: Dict[str, Dict[str, Any]] = {}

    # ------- 对外入口 ---------------------------------------------

    def get(self, kind: str, *, force: bool = False) -> Dict[str, Any]:
        kind = str(kind or "").lower()
        if kind not in PANEL_KINDS:
            return {"kind": kind, "available": False, "error": "未知面板类型"}
        cached = self._cache.get(kind)
        now = time.time()
        if not force and cached and now - cached.get("ts", 0) < 600:
            return cached["payload"]
        payload = self._fetch(kind)
        self._cache[kind] = {"ts": now, "payload": payload}
        return payload

    def pre_feed(self) -> str:
        """供上下文装配使用的简短预喂文本（只注入可用且有价值的）。"""
        bits: List[str] = []
        if bool(self.config.get("weather_enabled", False)):
            w = self.get("weather")
            if w.get("available") and w.get("summary"):
                bits.append(f"[环境] 当前天气：{w['summary']}")
        return "\n".join(bits)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for k in PANEL_KINDS:
            if k == "weather" and not bool(self.config.get("weather_enabled", False)):
                continue
            out[k] = self.get(k)
        return out

    # ------- 各面板实现 -------------------------------------------

    def _fetch(self, kind: str) -> Dict[str, Any]:
        if kind == "weather":
            return self._fetch_weather()
        if kind == "hotspot":
            return self._fetch_hotspot()
        if kind == "person":
            return self._fetch_person_index()
        return {"kind": kind, "available": False}

    def _fetch_weather(self) -> Dict[str, Any]:
        city = str(self.config.get("weather_city", "") or "").strip()
        if not city:
            return {"kind": "weather", "available": False,
                    "note": "未配置 weather_city，无法获取天气"}
        try:
            url = f"https://wttr.in/{city}?format=j1&lang=zh"
            req = urllib.request.Request(url, headers={"User-Agent": "BlackWarrior/0.2"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                import json
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            cur = (data.get("current_condition") or [{}])[0]
            desc = (cur.get("lang_zh") or [{}])
            txt = desc[0].get("value") if desc else cur.get("weatherDesc", [{}])[0].get("value", "")
            if not txt:
                txt = cur.get("weatherDesc", [{}])[0].get("value", "")
            # wttr.in 的 lang=zh 经常不生效（返回英文），中文映射兜底
            zh = _zh_desc(txt)
            temp = cur.get("temp_C", "—")
            feels = cur.get("FeelsLikeC", "—")
            area = (data.get("nearest_area") or [{}])[0]
            name = area.get("areaName", [{}])[0].get("value", city)
            return {
                "kind": "weather", "available": True,
                "city": name, "summary": f"{zh} {temp}°C（体感 {feels}°C）",
                "temp_c": temp, "feels_c": feels, "desc": zh, "desc_raw": txt,
            }
        except Exception as ex:
            return {"kind": "weather", "available": False,
                    "error": f"{type(ex).__name__}: {ex}"}

    def _fetch_hotspot(self) -> Dict[str, Any]:
        """热点面板：60s API 实时榜单（免 key，可自部署）。

        数据源 ``60s.viki.moe``（GitHub 开源项目 60s 的公共部署），
        支持微博/知乎/B站/抖音热榜与每日新闻；``hotspot_base`` 可换成
        自部署地址。网络失败时诚实降级——不伪造榜单。
        """
        if not bool(self.config.get("hotspot_enabled", False)):
            return {"kind": "hotspot", "available": False,
                    "note": "热点面板未开启（config hotspot_enabled）"}
        src_key = str(self.config.get("hotspot_source", "weibo") or "weibo").lower()
        if src_key not in _HOT_SOURCES:
            src_key = "weibo"
        label, path = _HOT_SOURCES[src_key]
        base = str(self.config.get("hotspot_base", "") or "https://60s.viki.moe").rstrip("/")
        try:
            import json
            url = f"{base}/v2/{path}"
            req = urllib.request.Request(url, headers={"User-Agent": "BlackWarrior/0.2"})
            with urllib.request.urlopen(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
            payload = data.get("data") if isinstance(data, dict) else None
            items: List[Dict[str, Any]] = []
            if src_key == "news":
                # /v2/60s：data.news 是 15 条新闻字符串
                for s in (payload or {}).get("news") or []:
                    t = str(s).strip()
                    if t:
                        items.append({"title": t})
                updated = (payload or {}).get("date", "")
            elif isinstance(payload, list):
                for it in payload[:15]:
                    t = str(it.get("title") or "").strip()
                    if not t:
                        continue
                    items.append({
                        "title": t,
                        "hot_value": it.get("hot_value"),
                        "link": it.get("link") or "",
                        "detail": (it.get("detail") or "")[:120] if it.get("detail") else "",
                    })
                updated = time.strftime("%Y-%m-%d %H:%M")
            else:
                payload = None
            if not items:
                return {"kind": "hotspot", "available": False,
                        "note": f"{label}接口返回空（源：{base}）"}
            return {"kind": "hotspot", "available": True, "source": label,
                    "source_key": src_key, "base": base, "updated": updated,
                    "count": len(items), "items": items}
        except Exception as ex:
            return {"kind": "hotspot", "available": False, "source": label,
                    "error": f"{type(ex).__name__}: {ex}",
                    "note": f"热点源拉取失败（可配 hotspot_base 换自部署）"}

    def _fetch_person_index(self) -> Dict[str, Any]:
        """人物卡索引（内置常识 + 用户自定义）。"""
        persons = dict(_BUILTIN_PERSONS)
        # 用户自定义：从记忆里捞"人物"类目
        try:
            rows = self.store.list_memories(limit=200, category="人物") if self.store else []
            for r in rows:
                nm = str(r.get("title") or "").replace("人物：", "").strip()
                if nm:
                    persons[nm] = {
                        "identity": nm,
                        "intro": str(r.get("brief") or ""),
                        "tags": ",".join(r.get("tags") or []),
                        "custom": True,
                    }
        except Exception:
            pass
        return {"kind": "person", "available": True, "count": len(persons),
                "items": persons}

    def person_card(self, name: str) -> Optional[Dict[str, str]]:
        idx = self._fetch_person_index()
        return idx.get("items", {}).get(str(name or "").strip())
