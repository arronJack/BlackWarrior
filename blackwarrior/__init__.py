"""黑武士 BlackWarrior —— 带真实认知内核的持续运行桌面 AI Agent。

分层（依赖单方向，无环）::

    electron/            桌面壳（窗口 / 托盘 / 单实例）
        │  HTTP + SSE
        ▼
    server/              本地服务（REST / SSE / 安全策略）
        │
        ▼
    runtime/             持续运行主循环（优先级调度 / 抢占 / 看门狗）
        │
        ├── llm/         多模型网关 + 工具调用回合
        ├── tools/       工具注册表 / 策略 / 审计
        ├── context/     上下文组装（ACI 式注入）
        ▼
    brain/               认知内核（PASM：记忆 / 遗忘 / 巩固 / 焦点 / 情绪）
        │
        ▼
    pasm-skills / pasm-framework   PASM 生态（可选依赖）

设计信条
--------
1. **内核是真的**：自主行为与记忆不靠"再问一次 LLM"，认知层离线也能工作。
2. **永不隐藏降级**：PASM 缺失时明确标注档位，不假装用了真内核。
3. **零强制依赖**：标准库即可跑，PASM 与第三方库都是可选增强。
"""

from __future__ import annotations

from .version import CODENAME, PROTOCOL, __version__

__all__ = ["__version__", "CODENAME", "PROTOCOL", "selftest"]


def selftest() -> bool:
    """包级自检：不依赖网络与模型，纯本地可跑。"""
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(("  v " if cond else "  x ") + msg)
        if not cond:
            ok = False

    print("-" * 58)
    print(f"黑武士 {CODENAME} v{__version__} 自检")
    print("-" * 58)

    # 0. 包元数据版本必须与代码一致（漂移过一次，pip show 显示假版本）
    try:
        import re as _re
        from pathlib import Path as _P

        _pkg = _P(__file__).resolve().parents[1] / "pyproject.toml"
        if _pkg.is_file():
            _txt = _pkg.read_text(encoding="utf-8")
            try:
                import tomllib
                _pv = str((tomllib.loads(_txt).get("project") or {})
                          .get("version") or "")
            except ImportError:
                # Python 3.9/3.10 没有 tomllib，退回正则（够用：只取 project.version）
                _m = _re.search(r'^\s*version\s*=\s*"([^"]+)"',
                                _txt, _re.M)
                _pv = _m.group(1) if _m else ""
            check(_pv == __version__,
                  f"包元数据版本与代码一致（{__version__}）")
    except Exception as ex:
        print(f"  ! 无法校验 pyproject 版本：{type(ex).__name__}")

    # 1. 路径层
    from . import paths
    root = paths.data_root()
    check(root.exists(), f"数据根可用：{root}")
    check(paths.within_sandbox(paths.sandbox_dir() / "a.txt"), "沙箱边界判定正确")
    check(paths.safe_join_sandbox("../escape") is None, "沙箱拒绝 .. 逃逸")

    # 2. 配置层
    from .config import Config, mask_secrets
    cfg = Config(data={"api_key": "sk-1234567890abcdef"})
    check(cfg.get("api_key") == "sk-1234567890abcdef", "配置读取正确")
    masked = mask_secrets({"api_key": "sk-1234567890abcdef"})
    check("1234567890" not in str(masked["api_key"]), "密钥导出已脱敏")
    check(cfg.is_activated() is True, "有 key 时判定为已激活")

    # 3. 事件总线
    from .events import EventBus
    bus = EventBus(history=5)
    q = bus.subscribe()
    bus.publish("probe", x=1)
    got = q.get_nowait()
    check(got["type"] == "probe", "事件总线可发布订阅")
    check(len(bus.recent(10)) == 1, "事件历史可回看")

    # 4. 数据库
    from .db.store import Store
    import json
    import tempfile, os
    tmp = tempfile.mkdtemp(prefix="bw-selftest-")
    st = Store(path=os.path.join(tmp, "t.db"))
    mid = st.add_memory("测试记忆", "内容", tags=["测试"], salience=3)
    st.add_message("user", "你好")
    check(mid > 0, "记忆写入成功")
    check(len(st.search_memories("测试")) >= 1, "记忆检索成功")
    check(st.count_memories() == 1, "记忆计数正确")
    st.close()

    # 5. 认知内核
    from .brain.kernel import build_kernel, HAS_PASM
    kern = build_kernel(cfg)
    kern.observe("自检事件", "内核可用", tags=["自检"], salience=2)
    hits = kern.recall("内核", k=3)
    check(bool(hits), f"认知召回成功（tier={kern.tier}, engine={kern.engine}）")
    check(kern.tier in ("bionic", "core", "light", "none"), "档位标注合法")
    if not HAS_PASM:
        print("  ! 未安装 PASM 基座，当前为内置降级档（功能完整，语义检索较弱）")

    # 6. 记忆服务
    from .brain.memory import MemoryService
    st2 = Store(path=os.path.join(tmp, "t2.db"))
    ms = MemoryService(kern, st2)
    ms.remember("服务层记忆", "来自 MemoryService", tags=["自检"], salience=3)
    check(len(ms.recall("服务层", k=3)) >= 1, "记忆服务召回成功")
    check("服务层" in ms.to_prompt("服务层", k=3), "记忆可渲染进提示词")
    st2.close()

    # 7. 情绪 / 世界模型
    from .brain.affect import AffectTracker
    at = AffectTracker(kern)
    rep = at.observe_input("今天天气不错")
    check("error" in rep, "世界模型产出预测误差")
    rep2 = at.observe_input("帮我把这个文件删掉")
    check(rep2["error"] != rep["error"], "预测误差随输入变化")
    check(0.6 <= at.suggest_tick_scale() <= 1.6, "心跳缩放系数在合理区间")

    # 8. 用户画像 / 信息面板 / 预取缓存（v0.2）
    from .brain.profile import UserProfile
    up = UserProfile(st, cfg)
    check(up.set("name", "小志", evidence="我叫小志", confidence=0.9),
          "画像写入成功")
    check((up.get("name") or {}).get("value") == "小志", "画像读取成功")
    check("小志" in up.to_prompt(), "画像可渲染进提示词")
    from .runtime.panels import PanelManager
    pm = PanelManager(cfg, st)
    w = pm.get("weather")
    check(w.get("available") is False and ("note" in w or "error" in w),
          "面板未配置时诚实降级（不伪造天气）")
    from .runtime.prefetch import PrefetchCache
    pf = PrefetchCache(cfg, st)
    pf.add("https://example.com/x", content="测试内容", ttl=3600)
    check(len(pf.serve()) >= 1, "预取缓存可服务")
    pf.clear()
    check(len(pf.serve()) == 0, "预取缓存可清空")

    # 9. PASM V2 十九层认知底座（v0.3）
    from .brain.pasm2_bridge import LAYERS, build_bridge
    check(len(LAYERS) == 19, f"十九层定义完整（{len(LAYERS)} 层）")
    b2 = build_bridge("full", tool_allowlist=["get_time", "read_file"])
    z1 = b2.encode("黑武士")
    z2 = b2.encode("黑武士")
    check(len(z1) == 8 and z1 == z2, "象量编码确定且 8 维（纯标准库）")
    check(b2.encode("黑武士") != b2.encode("PASM"), "不同文本产出不同象量")
    if b2.available:
        d = b2.observe("自检：我叫小志")
        check(bool(d) and d.get("step") == 1, f"V2 感知可用（{b2.reason}）")
        stt = b2.status()
        check(len(stt.get("layers") or []) == 19, "V2 状态含十九层活跃度")
        check(b2.verify_tool("get_time", {}).get("pass") is True,
              "V2 安全层放行白名单内工具")
        check(b2.verify_tool("format_disk", {}).get("pass") is False,
              "V2 安全层拒绝白名单外工具（内核级闸门）")
        check(isinstance(b2.growth_review(), dict), "V2 成长复盘可调用")
        check(json.dumps(b2.status(), default=str) is not None, "V2 状态可序列化")
    else:
        check(not b2.observe("x"), "V2 缺席时 observe 安静返回空")
        check(not b2.verify_tool("x", {}), "V2 缺席时校验安静返回空")
        check(len(b2.status().get("layers") or []) == 19,
              "V2 缺席时仍列十九层（标注未接入，不假装有）")
        # v0.4 语义嵌入：哈希必须无语义，LSA 必须能判出真语义
        from .brain.embedding import HashEmbedding, LsaEmbedding
        _h = HashEmbedding()
        check(_h.similarity("苹果", "苹果") > 0.99 and
              not getattr(_h, "semantic", False),
              "哈希后端：确定性但无语义（如实标注）")
        _l = LsaEmbedding()
        for _d in ("苹果 水果 香蕉 都常见", "我喜欢吃苹果", "水果店有苹果香蕉",
                   "汽车 轮胎 发动机 保养", "汽车轮胎该保养了",
                   "孩子的作业考试要复习", "复习备考做作业"):
            _l.feed(_d)
        _l.rebuild()
        check(len(_l.encode_pasm("苹果")) == 8,
              "LSA 给 PASM 的投影恒为 8 维")
        check(b2.embedding_status().get("backend") in ("hash", "lsa", "onnx"),
              f"嵌入后端可申报（{b2.embedding_status().get('backend')}）")
        if getattr(_l, "semantic", False):
            _rel = _l.similarity("汽车", "轮胎")
            _unrel = _l.similarity("汽车", "香蕉")
            check(_rel > _unrel,
                  f"LSA 语义生效（相关 {_rel:+.3f} > 无关 {_unrel:+.3f}，"
                  f"置信 {_l.confidence()}）")
        else:
            # 没 numpy 时 LSA 做不了 SVD，必须**如实退哈希**而不是假装有语义。
            # 这正是"永不隐藏降级"要验的那条：降级了要说降级了。
            check(_l.semantic is False,
                  f"LSA 无 numpy 时诚实退化为无语义（{_l.status().get('reason')}）")
            print("  ! 装 numpy 可启用本地语义嵌入（LSA）")

        print(f"  ! {b2.reason}")
        print("  ! 装 numpy + 从源码装 PASM 主仓（--no-deps）可启用真实十九层底座"
              "，见 README「装 PASM V2 十九层底座」")

    # 10. 文档里的安装命令必须指向真实存在的包
    # 踩过坑：README 写 `pip install pasm-agent`，而那个包名从未发布到 PyPI
    #（PASM 主仓的 name 就是它，但没发过），用户装完直接报
    # `No matching distribution found`。文档里的包名必须可核验。
    # （V2 底座后来以 `pasm2` 之名正式上架，这条守卫就是那次踩坑的产物。）
    try:
        from pathlib import Path as _P2

        _readme = _P2(__file__).resolve().parents[1] / "README.md"
        if _readme.is_file():
            _txt = _readme.read_text(encoding="utf-8")
            _hits = []
            for _ln in _txt.splitlines():
                if "pip install" not in _ln:
                    continue
                # 说明性引用（"…会报 No matching distribution found"）不算
                if "No matching" in _ln or "从未发布" in _ln:
                    continue
                _code = _ln.replace("`", "")
                for _b in ("pasm-agent", "pasm_agent"):
                    if _b in _code:
                        _hits.append(_ln.strip()[:60])
            check(not _hits,
                  "README 未推荐不存在的包名"
                  + ("" if not _hits else f"：{_hits}"))
    except Exception as ex:
        print(f"  ! 无法校验 README 安装命令：{type(ex).__name__}")

    # 10. 主循环可反复启停（2026-10-06 用户报「停止后开启不了」）
    # 这类"一次性守卫"型 bug 只有真实使用才暴露，测试必须钉住。
    # ★ 放在所有分支之外 —— 之前插在 V2 分支里，无 V2 时整段不执行。
    import time as _t
    from .core import WarriorCore as _WC
    _c = _WC(auto_start=False)
    try:
        _c.start(); _t.sleep(0.35)
        check(_c.is_running(), "主循环可启动")
        _c.stop(); _t.sleep(0.25)
        check(not _c.is_running(), "主循环可停止")
        _c.start(); _t.sleep(0.35)
        check(_c.is_running(), "★停止后可再次启动")
        _c.stop(); _t.sleep(0.2)
        _c.start(); _t.sleep(0.35)
        check(_c.is_running(), "★可第三次启动（幂等，非一次性）")
        _c.stop()
    finally:
        try:
            _c.close()
        except Exception:
            pass

    # 8.6 任务续跑（v0.6）
    from .runtime.tasks import selftest as _task_self
    check(_task_self(), "任务续跑引擎（建/推进/受阻/恢复/收尾需证据/重启转暂停）")
    from .tools.builtin import tasks as _ttools
    from .tools.builtin import sysinfo as _stools
    from .tools.registry import ToolRegistry as _TRCls
    _tr = _TRCls()
    class _TC:
        pass
    _tc = _TC()
    _tc.store = st
    _tc.config = cfg
    from .runtime.tasks import TaskEngine as _TE
    _tc.tasks = _TE(st, cfg)
    _tc.push_background = lambda text, source="system": {"ok": True}
    check(bool(_ttools.register(_tr, _tc)), "任务工具已注册")
    _sr = _TRCls()
    _tc2 = _TC()
    _tc2.store = st
    _tc2.config = cfg
    check(bool(_stools.register(_sr, _tc2)), "资源诊断工具已注册")

    # 9. 本地媒体库（v0.5）
    # 媒体库是"零依赖可用"能力的代表：实测本机 mutagen/Pillow 全无，
    # 所以内置自己解析文件头。这组断言保证它不会因缺依赖而整体消失。
    from .media.library import MediaLibrary as _ML
    from .media.library import selftest as _ml_self
    check(_ml_self(), "媒体库内置解析（MP3/FLAC/WAV/PNG + 增量扫描）")
    _mlib = _ML(st, cfg)
    check(_mlib.add_root("/definitely/not/exist")["ok"] is False,
          "媒体目录不存在时如实拒绝（不假装登记成功）")
    _mstat = _mlib.stats()
    check(_mstat.get("available") is True,
          "媒体库状态可申报（缺 mutagen/Pillow 也不会整体消失）")
    check(_mlib.to_prompt() == "",
          "媒体库为空时不注入上下文（不占 token）")

    print("-" * 58)
    print("自检结果：" + ("通过" if ok else "失败"))
    return ok
