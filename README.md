# 黑武士 BlackWarrior

> **带真实认知内核的桌面 AI Agent** —— 持续运行 · 记住你 · 会思考 · 有情绪 · 会成长
>
> Electron 桌面壳 + **PASM V2 十九层认知底座**，离线也能跑。

<div align="center">

![version](https://img.shields.io/badge/version-0.3.0-37e6ff)
![python](https://img.shields.io/badge/python-3.9%2B-8b5cff)
![electron](https://img.shields.io/badge/electron-33-9feaf9)
![license](https://img.shields.io/badge/license-MIT-3bffa5)
![deps](https://img.shields.io/badge/%E5%BC%BA%E5%88%B6%E4%BE%9D%E8%B5%96-0-critical)

**Gitee** · https://gitee.com/arronzheng/BlackWarrior
**GitHub** · https://github.com/arronJack/BlackWarrior
**GitCode** · https://gitcode.com/arronzheng/BlackWarrior

</div>

---

## 目录

- [这是什么](#这是什么)
- [核心设计：认知进内核，不是提示词](#核心设计认知进内核不是提示词)
- [功能说明](#功能说明)
- [安装](#安装)
- [使用教程](#使用教程)
- [配置项](#配置项)
- [HTTP API](#http-api)
- [架构](#架构)
- [安全模型](#安全模型)
- [开发与验证](#开发与验证)
- [Roadmap](#roadmap)

---

## 这是什么

黑武士是一个**长期驻留在你电脑里**的 AI 智能体，不是"打开-聊天-关闭"的窗口：

| 特征 | 说明 |
|---|---|
| **持续运行** | 有心跳、有作息、能被消息抢占的常驻主循环；空闲时自己思考 |
| **真的有内核** | 内置 **PASM V2 十九层认知底座**，未接入任何大模型也能运行、记忆、校验、成长 |
| **记住你** | 遗忘曲线 + 睡眠巩固 + FTS5 中文全文 + 联想线索 + 结构化用户画像 |
| **会思考** | 世界模型预测、丘脑门控、元认知冲突监测、双系统快慢切换 |
| **有情绪** | 杏仁核显著性 + 四通道神经调质（多巴胺/血清素/去甲肾上腺素/乙酰胆碱） |
| **会成长** | 失衡告警 → 参数建议 → 灰度应用；evalkit 成长日记 |
| **桌面同源** | Electron 壳 + 暗黑科技风 UI，托盘常驻、关窗不退出 |

---

## 核心设计：认知进内核，不是提示词

多数桌面 Agent 的"智能"全部来自外部大模型：自主行为 = 空闲时拿一句提示词去问模型，
记忆 = 数据库检索，**它不知道自己会错**，断网即哑火。

黑武士把认知做成**本地就成立的器官**。每轮对话，除了给模型拼提示词，还会做四件事：

```
用户输入
   │
   ├─→ ① 世界模型：预测这一步，事后算预测误差 RPE ──→ 决定心跳快慢与好奇心
   ├─→ ② PASM V2 observe：文本→8维象量，喂给十九层底座 ──→ 情绪/门控/躯体标记
   ├─→ ③ 上下文装配（ACI）：认知状态 + 画像 + 预取 + V2 认知 一起注入提示词
   └─→ ④ 输出前校验：V2 三角校验四层 + 内核级工具安全闸
```

**上下文里真正多了什么**（这是纯 LLM 方案拿不到的）：

```
[认知状态] 情绪 +0.13（-1 消极 ~ +1 积极）｜预测误差 0.00｜内核档位 pasm2
  当前焦点：记忆机制、认知内核
  对下一轮的预期：置信度 0.42｜好奇心 0.31
[用户画像] 姓名：小志（置信 0.9，依据「我叫小志」）
[预取缓存] 以下信息已预先准备好：- https://wttr.in/Beijing：Sunny 18°C
[PASM V2 认知] 情绪 效价 0.20 / 唤醒 0.37｜世界模型 预测落空（可能出乎意料）
               ｜躯体标记 0.19（直觉倾向，供参考不必盲从）
```

### 十九层架构

| 层 | 模块 | 作用 |
|---|---|---|
| 层 -2 | 安全层 | 价值锚定·边界约束·欺骗检测·伦理审查·人类优先·资源上限（12 把锁） |
| 层 -1 | 脑干调质 | 觉醒·疲劳·多巴胺/血清素/去甲肾上腺素/乙酰胆碱四通道 |
| 层 0.5 | 丘脑中继 | 门控筛选 + **新颖度直通**（防漏掉新事物） |
| 层 1 | 工作记忆 | 前额叶-顶叶（容量 7±2）+ 前瞻意图 |
| 层 2 | 记忆体 | 模式分离·模式完成·系统巩固·再巩固·自传体记忆 |
| 层 2.5 | 小脑 | 时序·协调·误差微调 |
| 层 3 | 世界模型 | 上下文预测器 + **反事实推理**（`what_if`） |
| 层 4 | 情绪 | 杏仁核显著性 + 边缘系统价值 + 情绪记忆绑定 |
| 层 4.5 | 岛叶 | 内感受 → 躯体标记（"gut feeling" 偏置） |
| 层 5 | 人格 | 慢时间尺度特质 EMA（探索/稳定/敏感/可塑） |
| 层 6 | 元认知 | **ACC 冲突/错误监测** + 双系统快慢门控（System2 惊讶触发） |
| 层 6.5 | 网络动态 | DMN 自发思考 + 小脑 + 突显网络 |
| 层 7 | 符号层 | 真符号注册表 + 符号化梯度 + **三角校验** |
| 层 7.5 | 符号映射 | SLH 单向桥 + 内生↔外部符号多对多映射 + 四路路由 |
| 层 8 | 执行输出 | 动作选择·工具调用 |
| 规划器 | CEM + MPC | 量子退火决策 |
| 认知皮层 | 对外执行 | 经安全层的工具调用与输出校验 |
| narrator | 语言输出 | 经 LLM 桥的语言生成（**无 LLM 时退化为结构化诚实申报**） |
| — | 成长回路 | 失衡告警 → 参数建议 → 灰度应用 |
| — | evalkit | 成长日记 GrowthTrace + 十四项失衡监测 |

### 档位与降级（永不隐藏）

| 档位 | 条件 | 表现 |
|---|---|---|
| `pasm2` | 装了 `numpy` + `pasm-agent` | 十九层真实活跃度、安全层闸门、反事实推理、成长闭环 |
| `pasm1` | 无 pasm2，有 `pasm-skills` | 记忆 / 情绪 / 焦点 / 塑形（内核六项） |
| `builtin` | 都没有 | 内置等价实现，功能完整、语义检索较弱 |

V1 引擎内部再分 `bionic / core / light` 三档（有无 torch 情绪系统）。
**任何一档缺失都在 `/status` 与「心智」页写明原因**，动作类端点返回 503 而非 500。

---

## 功能说明

### 对话

- 异步优先级队列 + SSE 增量渲染，思考条实时显示当前阶段与工具调用
- 同步问答接口（CLI / 脚本 / 单轮调用）
- 会话历史回放，刷新页面不"失忆"
- 自主 TICK 的产出单独渲染成低打扰样式，不混进正常对话

### 记忆

| 能力 | 说明 |
|---|---|
| 结构化记忆库 | 标题/简述/标签/类目/显著度/保留度，UI 可浏览编辑 |
| 遗忘曲线 | 艾宾浩斯衰减，重要度与复习次数延长半衰期 |
| 睡眠巩固 | 相似记忆蒸馏合并，可手动触发 |
| FTS5 中文全文 | trigram 分词，支持中文子串检索（≥3 字；2 字由 LIKE 兜底） |
| 联想线索 | 记忆间显式建边，表达"苹果→手机"这类不含关键词的关系 |
| 记忆审计 | create/update/delete 全量账本，before → after 可追责 |
| 用户画像 | 8 维度结构化画像，启发式 + LLM 双抽取，带证据与置信度 |

### 认知与心智

- 十九层实时活跃度矩阵（数值 + 人类可读说明）
- 安全层：12 把锁状态、白名单规模、检查/拦截计数、连续拒绝、锁死状态
- 认知计数：步数 / 象量实体数 / 真符号数 / 记忆图边数 / 嵌入语义标注
- 失衡告警列表（内核自检出的"不对劲"）
- 睡眠巩固、成长复盘按钮
- 元认知冲突监测、双系统快慢切换可视化

### 工具（33 个）

| 类别 | 工具 |
|---|---|
| 文件系统 | 读/写/列目录/搜索（锁定沙箱，拒绝 `..` 逃逸） |
| Shell | 命令执行（黑名单即拒 + 超时上限 + 风险分级） |
| Web | 联网搜索、网页阅读 |
| 记忆 | 写入/召回/更新/删除/巩固/线索/审计 |
| 系统 | 提醒、心跳自调节、时间、状态、环境、工具列表、反馈塑形 |
| 画像 | `set_profile` / `get_profile` / `get_panel` / `add_prefetch` / `list_prefetch` |
| 自发现 | `find_tool`（按关键词检索工具，命中与否都写审计日志）、`system_probe`（CPU/内存/磁盘/电池） |

工具执行是**双闸门**：应用层 policy（配置/风险/频率/黑名单）之后，
还有 PASM V2 安全层（白名单/限流/提示注入筛查/连续拒绝锁死升级）。

### 信息面板与预取

- 天气（`wttr.in`，需配 `weather_city`）、人物卡（内置常识 + 从记忆扩充）
- 热点面板：需搜索能力，未配置时返回"可扩展"占位，**不伪造榜单**
- 预取缓存：登记 URL + TTL，心跳刷新到期项，有效期内注入上下文

### 语音

| 能力 | 实现 | 可用性 |
|---|---|---|
| 朗读回复（TTS） | `speechSynthesis` | **完全本地、离线可用** |
| 语音输入（ASR） | `SpeechRecognition` + 云端转写 | Chromium 走云端；**桌面壳内置的 Chromium 不带该服务 API key，壳内基本不可用**（按钮自动置灰并说明原因），在 Chrome / Edge 打开可用 |

### 桌面壳

Electron：托盘常驻、关窗不退出、单实例、启动进度、上下文隔离桥。

---

## 安装

### 前置要求

- **Python 3.9+**（推荐 3.12+）
- **Node.js 18+**（仅桌面壳需要）
- 操作系统：Windows / macOS / Linux

### 方式一：源码安装（推荐）

```bash
git clone https://gitee.com/arronzheng/BlackWarrior.git
cd BlackWarrior
pip install -e .
```

### 方式二：桌面壳

```bash
cd BlackWarrior/electron
npm install
npm start
```

### 可选增强（按需装，不装也能跑）

| 装什么 | 得到什么 | 不装会怎样 |
|---|---|---|
| `numpy` + `pasm-agent` | **PASM V2 十九层底座**：安全层闸门、元认知、反事实推理、成长闭环 | 退回内核 V1 档，「心智」页标注原因 |
| `pasm-skills` | 内核 V1 档（`bionic`/`core`）：语义检索 + 情绪系统 | 退回 `builtin` 档，字面+字符级检索 |
| `torch` | 情绪系统（valence × arousal → mood） | 情绪退化为单一效价值 |
| `psutil` | `system_probe` 能读内存/电池 | 内存字段为空并标注"未装 psutil" |

```bash
pip install numpy
pip install pasm-agent          # PASM 引擎（含 pasm2 底座）
pip install pasm-skills         # 认知基座（可选）
pip install psutil              # 本机资源探测（可选）
```

验证装没装成功：

```bash
blackwarrior selftest           # 末尾会打印当前档位与降级原因
```

---

## 使用教程

### 1. 启动

```bash
blackwarrior serve              # 浏览器打开 http://127.0.0.1:3721
blackwarrior serve --port 8080  # 换端口
blackwarrior serve --host 0.0.0.0 --allow-lan   # 局域网（必须同时设 api_token）
```

### 2. 首次接入模型（可选）

黑武士**不接模型也能用**——本地认知内核会接管回复、记忆照常累积。
在 UI「接入大脑」弹窗里填任意 OpenAI 兼容的 Key 即解锁完整推理：

| Provider | 说明 |
|---|---|
| `deepseek` / `qwen` / `kimi` / `glm` / `moonshot` / `openai` / `azure` | 需 API Key |
| `ollama` | 本地模型，可离线 |
| `mock` | 本地假模型，用于演示与自测 |

也可以命令行写入：

```bash
curl -X POST http://127.0.0.1:3721/api/activate \
  -H "Content-Type: application/json" \
  -d '{"provider":"deepseek","api_key":"sk-xxx","model":"deepseek-chat"}'
```

### 3. 界面导览

| 视图 | 做什么 |
|---|---|
| **对话** | 聊天、看思考流与工具调用、语音输入输出 |
| **记忆** | 力导向记忆图谱（按保留度着色）、语义检索、一键睡眠巩固 |
| **认知** | 情绪曲线、预测误差仪表、好奇心、焦点栈、内核快照 |
| **心智** | 十九层活跃度、安全层、认知计数、失衡告警、睡眠/成长按钮 |
| **全景** | 用户画像卡片、信息面板、预取缓存登记 |
| **活动** | 工具注册表、行动日志、实时事件流 |
| **设置** | 供应商、人格、心跳、能力开关、语音、密钥脱敏回显 |

### 4. 常用操作

**让它记住你**

```bash
curl -X POST http://127.0.0.1:3721/api/memories \
  -H "Content-Type: application/json" \
  -d '{"title":"我叫小志","brief":"用户的名字","category":"身份","salience":5}'
```

**查记忆（含中文全文检索）**

```bash
curl -X POST http://127.0.0.1:3721/api/memories/search \
  -H "Content-Type: application/json" -d '{"query":"认知","k":5}'
```

**设置提醒**

```bash
curl -X POST http://127.0.0.1:3721/api/reminders \
  -H "Content-Type: application/json" -d '{"title":"喝水","minutes":30}'
```

**手动触发睡眠巩固**（含十四项失衡自检）

```bash
curl -X POST http://127.0.0.1:3721/api/mind/sleep -H "Content-Type: application/json" -d '{}'
```

**看十九层实时状态**

```bash
curl http://127.0.0.1:3721/api/mind | python -m json.tool
```

**登记预取 URL**（心跳自动刷新并注入上下文）

```bash
curl -X POST http://127.0.0.1:3721/api/prefetch \
  -H "Content-Type: application/json" \
  -d '{"url":"https://wttr.in/Beijing?format=3","ttl":3600}'
```

**在设置页配天气**（不配则诚实降级，不伪造）

```json
{ "weather_enabled": true, "weather_city": "Beijing" }
```

### 5. 命令行

```bash
blackwarrior serve            # 启动服务
blackwarrior ask "你还记得我叫什么吗"   # 单轮问答（CLI）
blackwarrior selftest         # 包级自检
blackwarrior --version        # 版本与协议
```

---

## 配置项

配置文件落在 `<data_root>/config.json`，支持四级覆盖：
**代码默认 < 配置文件 < 环境变量 < 运行时调用**。

### 数据位置

| 环境 | `data_root` |
|---|---|
| 开发态（仓库内运行） | `./data/` |
| 安装态 | `%LOCALAPPDATA%/BlackWarrior`（Windows） |

### 关键配置

| 键 | 默认 | 说明 |
|---|---|---|
| `provider` / `model` / `api_key` / `base_url` | deepseek / - / - / - | 模型接入 |
| `temperature` / `max_tokens` / `timeout` | 0.7 / 4096 / 120 | 生成参数 |
| `heartbeat_enabled` | `true` | 启用心跳（空闲自主思考） |
| `tick_interval` | 60.0 | 空闲心跳秒数 |
| `task_tick_interval` | 30.0 | 有任务时的心跳 |
| `awakening_interval` / `awakening_ticks` | 10.0 / 3 | 唤醒期密集心跳 |
| `watchdog_seconds` | 300 | 单回合看门狗超时 |
| `cognition_enabled` | `true` | 认知内核总开关 |
| `consolidate_every` | 20 | 每 N 次心跳做一次记忆巩固 |
| `recall_k` | 6 | 每轮召回记忆条数 |
| `auto_observe` | `true` | 自动把对话写入情景记忆 |
| `tools_enabled` | `true` | 工具系统总开关 |
| `allow_dangerous_tools` | `false` | 允许危险工具（删除/写文件） |
| `shell_enabled` / `web_enabled` | `true` | 关闭后对应工具**根本不注册** |
| `pasm2_enabled` | `true` | PASM V2 底座开关 |
| `pasm2_profile` | `full` | `minimal` / `standard` / `full` / `brainwide` |
| `pasm2_tool_gate` | `true` | V2 安全层作为工具闸门 |
| `pasm2_verify_claims` | `false` | 输出前对结论性断言做逻辑层校验 |
| `weather_enabled` / `weather_city` | `false` / - | 天气面板 |
| `hotspot_enabled` | `false` | 热点面板 |
| `prefetch_enabled` | `false` | 预取心跳刷新 |
| `host` / `port` | 127.0.0.1 / 3721 | 服务监听 |
| `allow_lan` / `api_token` | `false` / - | 局域网模式（开就必须设 token） |
| `voice_enabled` / `tts_enabled` / `asr_enabled` | `false` | 语音 |

### 环境变量

`BLACKWARRIOR_PROVIDER` / `BLACKWARRIOR_MODEL` / `BLACKWARRIOR_API_KEY` /
`BLACKWARRIOR_BASE_URL` / `BLACKWARRIOR_HOST` / `BLACKWARRIOR_PORT` /
`BLACKWARRIOR_ALLOW_LAN` / `BLACKWARRIOR_API_TOKEN` / `BLACKWARRIOR_AGENT_NAME` /
`LLM_PROVIDER` / `DEEPSEEK_API_KEY` / `MINIMAX_API_KEY` / `OPENAI_API_KEY`

---

## HTTP API

服务默认只监听 `127.0.0.1`。完整路由见 `blackwarrior/server/routes.py`（60+ 条）。

### 基础

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/healthz` | 健康检查 |
| GET | `/api/version` | 版本与协议 |
| GET | `/api/status` | 完整状态（含 `cognition` / `affect` / `panorama` / `pasm2`） |
| GET | `/api/summary` | 精简状态（高频轮询） |

### 对话

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/message` | 异步发消息（入主循环） |
| POST | `/api/ask` | 同步问答（等结果） |
| GET | `/api/conversations` | 会话历史 |
| GET | `/events` | SSE 事件流 |

### 记忆 / 提醒 / 工具

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/memories` | 列表 / 新建 |
| PATCH/DELETE | `/api/memories/{id}` | 更新 / 删除 |
| POST | `/api/memories/search` | 检索（FTS5 + 语义） |
| GET | `/api/actions` | 行动日志 |
| GET/POST | `/api/reminders` | 提醒 |
| DELETE | `/api/reminders/{id}` | 取消提醒 |
| GET | `/api/tools` | 工具注册表 |

### 认知与心智

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/cognition` | 内核快照 + 情绪世界模型 |
| POST | `/api/cognition/consolidate` | 手动记忆巩固 |
| GET | `/api/cognition/mood?n=60` | 情绪曲线 |
| GET | `/api/mind` | 心智全景（十九层 + 安全层 + 成长） |
| GET | `/api/mind/layers` | 只取十九层（轻量轮询） |
| POST | `/api/mind/observe` | 手动喂一步认知 |
| POST | `/api/mind/sleep` | 睡眠巩固 + 失衡告警 |
| POST | `/api/mind/growth` | 成长复盘（参数建议） |
| POST | `/api/mind/whatif` | 反事实推演 |
| POST | `/api/mind/verify` | 三角校验（声明 / 工具调用） |
| POST | `/api/mind/tell` | 符号化注入 |
| POST | `/api/mind/gate-reset` | 复位安全层锁死 |

### 全景 / 面板 / 预取

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/api/profile` | 用户画像 |
| GET | `/api/panels` · `/api/panels/{kind}` | 信息面板 |
| GET/POST/DELETE | `/api/prefetch` | 预取缓存 |

### 语音 / 设置 / 运维

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/voice/status` | 语音能力状态（前端据此置灰） |
| POST | `/api/voice/transcribe` | 上传音频转写（未配置返回 503） |
| POST | `/api/voice/speak` | 云端合成（不可用时前端回退本地） |
| GET/POST | `/api/settings` | 配置（脱敏 / 更新） |
| GET | `/api/providers` | 供应商列表 |
| POST | `/api/activate` | 写入 provider/key/model |
| POST | `/api/llm/ping` | 测试连通 |
| POST | `/api/admin/start` · `/api/admin/stop` | 主循环启停 |
| POST | `/api/admin/reset-memories` · `/reset-conversations` | 重置数据 |

---

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
│  server/   HTTP + SSE 事件流（60+ 路由）                   │
│  runtime/  Continuum 主循环 + panels 面板 + prefetch 预取   │
│  llm/      多模型网关 + 工具循环 + V2 安全闸门              │
│  context/  上下文装配（认知/画像/预取/V2 四层注入）          │
│  tools/    33 个工具 · 三级风险 · 双闸门                    │
│  brain/    PASM V1 内核 + V2 十九层桥接 + 画像              │
│  voice/    语音服务门面（未配置显式降级）                    │
│  core.py   WarriorCore 总装                               │
│  db/       SQLite（WAL）：对话/记忆/全文索引/线索/审计      │
└───────────────────────┬──────────────────────────────────┘
                        │ 可选依赖（缺失即降级，永不隐藏）
┌───────────────────────▼──────────────────────────────────┐
│  PASM V2 认知底座 pasm2/                                  │
│  十九层 + 安全层（12 锁）+ 成长回路 + evalkit 成长日记       │
└──────────────────────────────────────────────────────────┘
```

```
BlackWarrior/
├── blackwarrior/          Python 内核包
│   ├── brain/  llm/  runtime/  server/  tools/  context/  db/  voice/
│   └── ui/static/         BlackWarrior UI（零构建）
├── electron/              桌面壳（main / preload / 图标）
├── scripts/make_icon.py   图标生成（纯标准库）
├── tests/test_server.py   端到端测试
└── docs/                  架构文档
```

---

## 安全模型

- 默认只监听 `127.0.0.1`；开局域网模式后所有敏感路由强制 Token；
- 工具分 `safe / caution / danger` 三级，策略层可按类别开关 + 频率节流；
- **双闸门**：应用层 policy 之外，PASM V2 安全层在内核侧再拦一道
  （白名单 / 每分钟限流 / 提示注入筛查 / 连续拒绝锁死升级），模型绕不过；
- 文件工具锁定沙箱目录，拒绝 `..` 逃逸；Shell 有黑名单即拒 + 超时上限；
- 配置回显一律脱敏（`key/token/secret/password`）；
- 密钥只落本机 `data/config.json`，不进版本库。

---

## 开发与验证

```bash
blackwarrior selftest        # 包级自检（41 项，纯本地不依赖网络与模型）
python tests/test_server.py  # 端到端：HTTP/SSE/UI + 前端静态一致性
```

端到端测试覆盖：REST 主干、SSE 事件流、前端 DOM id 一致性（`$('id')` 必须存在于 HTML）、
工具注册数量、密钥脱敏、用户画像读写与非法维度拒绝、面板降级、预取增删、
十九层心智端点（有 V2 时验真实能力，无 V2 时验 503 降级）。

---

## Roadmap

- [x] **v0.1**：PASM 认知内核 + Electron 壳 + 五视图 + 工具/记忆/提醒/语音
- [x] **v0.2**：结构化用户画像、FTS5 中文全文、信息面板、预取缓存、「全景」视图
- [x] **v0.3**：接入 **PASM V2 十九层认知底座**（安全层闸门 / 元认知 ACC /
      反事实推理 / 成长闭环 / 失衡监测）+「心智」视图 + 工具自发现 +
      本机资源感知 + 记忆线索 + 记忆审计
- [ ] v0.4：本地语义嵌入（替换哈希象量）、桌面壳内本地 ASR、渠道接入
- [ ] v0.5：技能市场（pasm-skills 生态互通）、多智能体协同
- [ ] v1.0：安装包全平台产物（NSIS / DMG / AppImage）+ 增量更新

规划中（尚未实现，不做承诺）：本地媒体处理（音乐库/视频面板）、
社交渠道、Scene Protocol 与 Agent 驱动界面、便携模式。

---

## License

MIT © 2026 arronzheng
