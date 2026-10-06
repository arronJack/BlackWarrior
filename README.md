# 黑武士 BlackWarrior

> **带真实认知内核的桌面 AI Agent** —— 持续运行 · 记住你 · 会思考 · 有情绪
>
> Electron 炫酷桌面壳 + Python PASM 认知内核，离线也能跑。

<div align="center">

![version](https://img.shields.io/badge/version-0.2.0-37e6ff)
![python](https://img.shields.io/badge/python-3.10%2B-8b5cff)
![electron](https://img.shields.io/badge/electron-33-9feaf9)
![license](https://img.shields.io/badge/license-MIT-3bffa5)
![deps](https://img.shields.io/badge/%E5%BC%BA%E5%88%B6%E4%BE%9D%E8%B5%96-0-critical)

**Gitee** · https://gitee.com/arronzheng/BlackWarrior
**GitHub** · https://github.com/arronJack/BlackWarrior

</div>

---

## 这是什么

黑武士是一个**长期驻留在你电脑里**的 AI 智能体：

- **持续运行**：不是"打开-聊天-关闭"的窗口，而是有心跳、有作息、能被消息抢占的常驻主循环；
- **真的有内核**：内置 PASM 认知内核（语义检索 / 遗忘曲线 / 记忆巩固 / 焦点栈 / 情绪动力学 / 预测误差），**未接入任何大模型也能运行和记忆**；
- **桌面同源体验**：Electron 壳 + 暗黑科技风 UI，托盘常驻、关窗不退出。

## 与同类项目的差异（为什么重复造轮子）

| 能力 | 典型 Electron 桌面 Agent（如白马AI） | 黑武士 |
|---|---|---|
| "智能"来源 | 完全依赖外部 LLM（脑子是租的） | **PASM 认知内核 + LLM 增强**，离线降级不哑火 |
| 记忆 | 对话历史拼接 / 外挂向量库 | **侧车认知索引 + SQLite 结构化副本**，带遗忘曲线保留度 |
| 情绪 | 无 | **情绪动力学**（valence×arousal→mood），并注入提示词 |
| 世界模型 | 无 | **预测误差**（自由能近似）驱动心跳快慢与好奇心 |
| 焦点 | 无 | **带衰减的话题焦点栈** |
| 档位诚实度 | 常把降级伪装成正常 | **永不隐藏降级**：`bionic/core/light/none` 在状态页明示 |
| 自主性 | 空闲轮询 | 8 级心跳决策 + 预测误差缩放 + 提醒独立计时源 |
| 安全 | — | 工具风险三级 + 路径沙箱 + Shell 黑名单 + 局域网强制 Token |
| 用户画像 | 结构化 profile（角色/领域/专长/偏好…） | **v0.2 补齐**：结构化画像 + 启发式/LLM 双抽取 + 置信度与依据，注入上下文 |
| 中文检索 | FTS5 trigram 全文 | **v0.2 补齐**：同款 FTS5 trigram 兜底轻量档中文子串检索 |
| 信息面板 | 天气/热点/人物卡常驻预喂 | **v0.2 补齐**：天气/热点/人物卡面板（未配置诚实降级，不伪造） |
| 预取缓存 | 周期 URL 预热注入上下文 | **v0.2 补齐**：可登记 URL、心跳刷新、注入上下文 |
| 媒体/社交 | 有较长工具面 | **诚实差距（规划中）**：本地媒体处理、社交渠道、Scene Protocol 暂未覆盖 |

黑武士的定位是"参考而非复刻"：借鉴其持续运行架构模式，内核用 Python 重写，
补上它们没有的认知层。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  Electron 壳（main.cjs / preload.cjs）                    │
│  拉起内核 · 托盘 · 单实例 · 上下文隔离桥                    │
└───────────────────────┬──────────────────────────────────┘
                        │ http://127.0.0.1:<port>
┌───────────────────────▼──────────────────────────────────┐
│  Python 内核 blackwarrior/                                │
│                                                          │
│  server/   HTTP + SSE 事件流（30+ 路由）                   │
│  runtime/  Continuum 主循环（优先级抢占/看门狗/心跳）        │
│  llm/      12 家 OpenAI 兼容供应商网关 + 工具循环           │
│  context/  上下文装配（★注入认知状态）                      │
│  tools/    风险分级工具注册表 + 策略层                      │
│  brain/    ★PASM 认知内核 / 记忆服务 / 情绪世界模型         │
│  core.py   WarriorCore 总装                               │
│  db/       SQLite（WAL）：对话/记忆/行动/提醒/信号          │
└──────────────────────────────────────────────────────────┘
```

## 快速开始

### 1) 安装内核

```bash
git clone https://gitee.com/arronzheng/BlackWarrior.git
cd BlackWarrior
pip install -e .
# 可选：接入 PASM 真内核（否则用内置等价实现，功能完整、档位标为 light）
pip install -e ".[pasm]"
```

### 2) 启动

```bash
blackwarrior serve           # 浏览器访问 http://127.0.0.1:3721
# 或桌面壳
cd electron && npm install && npm start
```

### 3) 激活（可选）

未接入模型时黑武士**照样运行**：本地内核接管回复、记忆照常累积。
在 UI「接入大脑」弹窗里填入任意 OpenAI 兼容 Key（DeepSeek / Qwen / Kimi / GLM / Ollama…）即解锁完整推理。

### 4) 验证

```bash
blackwarrior selftest        # 包级自检（26 项，纯本地不依赖网络与模型）
python tests/test_server.py  # HTTP/SSE/UI 端到端 + 前端静态一致性（47 项）
```

## UI 一览

- **对话流**：异步队列 + SSE 增量渲染，思考条实时显示当前阶段与工具调用；
- **记忆库**：力导向记忆图谱（按保留度着色）、语义检索、一键睡眠巩固；
- **认知内核**（独有）：情绪曲线、预测误差仪表、好奇心、焦点栈、内核快照 JSON；
- **活动**：工具注册表（风险着色）、行动日志、实时事件流；
- **全景**（v0.2）：用户画像卡片、信息面板（天气/热点/人物）、预取缓存登记与管理；
- **设置**：12 家供应商、人格、心跳、能力开关、语音开关，密钥脱敏回显。

## 语音

走浏览器内置 Web Speech API，**零额外依赖**：

| 能力 | 实现 | 可用性 |
|---|---|---|
| 朗读回复（TTS） | `speechSynthesis` | **完全本地、离线可用**，浏览器与桌面壳均支持 |
| 语音输入（ASR） | `SpeechRecognition` | Chromium 走云端识别；**Electron 内置的 Chromium 不带该服务 API key，桌面壳里基本不可用**（按钮自动置灰），在 Chrome / Edge 打开可用 |

要在桌面壳内做真语音输入，需换本地 ASR（whisper.cpp / faster-whisper 之类），
当前版本不引入——能力缺失时一律显式降级并说明原因，绝不静默失效。

## 安全模型

- 默认只监听 `127.0.0.1`；开局域网模式后所有敏感路由强制 Token；
- 工具分 `safe / caution / danger` 三级，策略层可按类别开关 + 频率节流；
- 文件工具锁定沙箱目录，拒绝 `..` 逃逸；Shell 有黑名单即拒 + 超时上限；
- 配置回显一律脱敏（`key/token/secret/password`）。

## 目录

```
BlackWarrior/
├── blackwarrior/          Python 内核包
│   ├── brain/  llm/  runtime/  server/  tools/  context/  db/
│   └── ui/static/         DarkWarrior UI（零构建）
├── electron/              桌面壳（main / preload / 图标）
├── scripts/make_icon.py   图标生成（纯标准库）
├── tests/test_server.py   端到端测试
└── docs/                  架构文档
```

## Roadmap

- [x] **v0.2（已完成）**：补齐与白马 AI 的差距——结构化用户画像、FTS5 中文全文检索、
      信息面板（天气/热点/人物）、预取缓存；新增「全景」视图与 7 个 REST 端点。
- [ ] v0.3：桌面壳内本地 ASR（whisper.cpp）、微信/钉钉渠道接入、技能市场（pasm-skills 互通）
- [ ] v1.0：安装包全平台产物（NSIS / DMG / AppImage）+ 增量更新

## License

MIT © 2026 arronzheng
