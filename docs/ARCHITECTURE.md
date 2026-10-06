# 架构与设计决策

本文记录黑武士的关键设计取舍，以及"为什么这么做"的理由。

## 1. 总体分层

```
Electron 壳（表现层，零业务逻辑）
   │  spawn + [BW-READY] 行解析（端口自动顺延）
   ▼
WarriorCore（总装）
   ├── Config       四级覆盖：代码默认 < data/config.json < 环境变量 < 运行时
   ├── Paths        打包态走 APPDATA，开发态走仓库 data/
   ├── Store        SQLite WAL（单写多读，线程安全门面）
   ├── CognitiveKernel（★大脑）  MemoryService（侧车×副本）  AffectTracker（世界模型）
   ├── ToolRegistry + ToolPolicy（能力与策略分离）
   ├── LLMGateway   12 家 OpenAI 兼容供应商，标准库 urllib + 逐行 SSE
   ├── TurnRunner   工具循环（上限 4 轮）+ 持久化 + 离线渲染
   ├── Continuum    持续运行主循环（优先级队列 + 心跳 + 看门狗）
   └── WarriorServer  ThreadingHTTPServer + SSE 事件流
```

**壳与内核的唯一耦合点**是 HTTP/SSE 与 `[BW-READY]` 输出行。
Electron 崩了内核还在跑；内核挂了壳能弹窗降级——两边可独立重启。

## 2. 为什么内核用 Python 而不是 Node

- PASM 生态（pasm-skills / pasm-framework）是 Python 资产，直接 `import`；
- 认知层有大量数值计算（余弦、指数衰减、聚类），numpy 生态成熟；
- Electron 只做壳，天然规避"前端塞进全部智能"的失控路径。

代价是进程间通信，用本机 HTTP + SSE 解决（同源，无 CORS 负担）。

## 3. 认知内核：档位与诚实

档位是**两条独立的轴**，不要混：

**轴一 · 内核引擎（V1）** —— `pasm-skills` 提供的认知引擎：

| 档位 | 含义 | 何时出现 |
|---|---|---|
| `bionic` | PASM 真内核 + 情绪动力学全开 | 装了 `pasm` extra 且有 torch |
| `core` | PASM 真内核，情绪简化 | pasm 可用但部分能力缺失 |
| `light` | 内置等价实现（纯标准库） | 默认，零依赖可跑 |
| `none` | 认知关闭 | 配置显式关闭 |

**轴二 · V2 十九层底座** —— `pasm2` 提供的认知器官：

| 状态 | 含义 | 何时出现 |
|---|---|---|
| `pasm2` | 十九层全开 + 安全层闸门 + 成长闭环 | 装了 `pasm2`（见下） |
| `pasm1` | 只有 V1 引擎 | 装了 `pasm` extra |
| `builtin` | 纯内置实现 | 都没装 |

> **V2 底座装法**：`pasm2` 已发布到 PyPI，直接装即可（只需 numpy，不拖 torch）：
>
> ```bash
> pip install pasm2
> ```
>
> 从源码装最新版 Alpha 用 `pip install --no-deps git+https://gitee.com/arronzheng/PASM.git`
> （`--no-deps` 是为了不让主仓的 torch 依赖被拖进来 —— V2 底座只需 numpy）。
>
> `--no-deps` 是为了跳过主仓声明的 `torch>=2.0`（数 GB）——V2 底座本身不 import torch。

**铁律：任何降级必须在 `/api/status` 与 UI 明示**（`tier` + `engine` + `reason` +
`pasm2.reason` + `embedding.backend`），绝不把内置兜底伪装成真内核——
这是黑武士最核心的价值观。

## 4. 持续运行主循环（Continuum）

```
消息(100) > 提醒(80) > 后台任务(50) > 系统(10)
```

- 高优先级可抢占低优先级轮次（`abort` Event + 看门狗 join）；
- 空闲时按 8 级决策决定下一次心跳：用户消息 0s → 后台 0s → 心跳关 →
  限流退避 → L2 自定义节奏 → 唤醒时段 → 任务 30s → 空闲间隔；
- 空闲间隔再乘 **tick_scale**（AffectTracker 建议：预测误差低 ×1.6 慢，
  误差高 ×0.6 快）——环境可预测时省 token，出现新鲜事时更勤快；
- 提醒是独立计时源（不受心跳休眠影响，MAX_SLEEP 分段唤醒）。

## 5. 事件流（SSE）的三条实践

1. `: ping` 保活（15s），否则代理/浏览器会掐掉"空闲"连接；
2. 客户端断开必须能感知，写出即抛错退出，避免死连接堆积；
3. 帧必须带 `id:`，浏览器重连回传 `Last-Event-ID`，服务端补发遗漏——
   少这一行，断线期间发生的事就永久丢了。

前端注意：SSE 帧带 `event: <type>`，浏览器只在 `addEventListener(type)` 上回调，
`onmessage` 收不到具名事件，客户端必须穷举注册类型表。

## 6. 记忆：侧车 + 副本双写

- **侧车**（`data/cognition/`，由 PASM 管理）：向量索引、复习计数、巩固状态；
- **副本**（SQLite `memories` 表）：给人看、给 UI 用（浏览/编辑/删除）；
- `memories.key` 关联两侧。读路径优先侧车（带 score/semantic/retention 三分数），
  未命中退回 SQLite 字面检索——保证 light 档也有可用的回想。

自主 TICK 的产出写入独立低重要度类别 `自省`，绝不混入 `对话`，
否则会出现"用户问我是谁，召回落点是 TICK 提示词原文"的污染事故。

## 7. 安全

- 默认 `127.0.0.1`；`allow_lan` 打开后敏感路由强制 Bearer Token；
- API 层拒绝修改 `allow_lan / api_token`（防止网页脚本放大权限）；
- 文件工具全部走 `safe_join_sandbox()`（拒绝 `..`）；
- Shell：黑名单即拒（format / rm -rf / shutdown…）+ 120s 超时 + 工作目录锁定；
- 工具按 `safe/caution/danger` 分级，danger 默认需确认（策略层可调）。

## 8. 已知坑（都是踩过的）

| 坑 | 症状 | 修法 |
|---|---|---|
| `emit(type, {...})` 与 `emit(type, **kw)` 两种写法并存 | `TypeError: takes 1 positional argument` | `emit(type_, payload=None, **kw)` 兼容 |
| `self.engine = self.engine or "builtin"` | 初始值 `"none"` 是真值，档位永远显示 none | `if not self.engine or self.engine == "none"` |
| MSYS 的 `/h/...` 路径塞进 `sys.path` | `ModuleNotFoundError: pasm_skills` | 用 Windows 原生盘符路径 |
| `start(background=False)` 只 bind 不 accept | 服务"活着但永不响应" | `serve_forever` 放后台线程，主线程收口 Ctrl+C |
| SSE 帧缺 `id:` | 断线重连永远从 0 补发 | `_frame()` 输出 `id:` 行 |
| subprocess 输出 GBK | `UnicodeDecodeError` | `PYTHONIOENCODING=utf-8` + `errors="replace"` |

## 9. 打包

- 图标：`scripts/make_icon.py`（纯 zlib+struct 手写 PNG，零第三方依赖）；
- 桌面壳：`cd electron && npm run dist:win`（NSIS + portable），
  `extraResources` 把 `blackwarrior/` 源码带进 `resources/app/`；
- 打包态解释器查找顺序：`BLACKWARRIOR_PYTHON` 环境变量 →
  `resources/python/`（内嵌运行时，可选）→ PATH 上的 `python`，
  每个候选都要通过 `import blackwarrior` 验证。
