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

    print(f"黑武士 {CODENAME} v{__version__} 自检")
    print("-" * 58)

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

    print("-" * 58)
    print("自检结果：" + ("通过" if ok else "失败"))
    return ok
