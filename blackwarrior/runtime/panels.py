"""信息面板 —— 让黑武士主动把"环境信息"准备好（对标白马 AI 的 Hotspot/天气/人物卡）。

白马 AI 有天气、热点、世界杯、人物卡片等运行时预喂面板。黑武士此前完全没有这类能力。
这里补上一套**可降级**的面板系统：

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
            temp = cur.get("temp_C", "—")
            feels = cur.get("FeelsLikeC", "—")
            area = (data.get("nearest_area") or [{}])[0]
            name = area.get("areaName", [{}])[0].get("value", city)
            return {
                "kind": "weather", "available": True,
                "city": name, "summary": f"{txt} {temp}°C（体感 {feels}°C）",
                "temp_c": temp, "feels_c": feels, "desc": txt,
            }
        except Exception as ex:
            return {"kind": "weather", "available": False,
                    "error": f"{type(ex).__name__}: {ex}"}

    def _fetch_hotspot(self) -> Dict[str, Any]:
        """热点面板：实时榜单需要搜索 key / 第三方接口。

        黑武士不内置任何热点 API 密钥，缺配置时诚实返回"可扩展"占位，
        而不是伪造榜单——这是与白马 AI 的诚实差距标注，已在 README 说明。
        """
        if not bool(self.config.get("hotspot_enabled", False)):
            return {"kind": "hotspot", "available": False,
                    "note": "热点面板需要配置搜索 key，当前未开启"}
        return {"kind": "hotspot", "available": False,
                "note": "热点依赖搜索能力，配置 web_search 后可在后续版本接入"}

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
