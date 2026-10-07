# 黑武士 BlackWarrior — 完整功能说明

> 对应版本 **v0.8.9**。本文所有工具名与端点都是从运行时注册表实读的，
> 不是手写宣传稿（生成时间 2026-10-07）。
>
> 复现命令：
> ```bash
> python -c "from blackwarrior.server.routes import build_routes; print(len(build_routes()))"
> ```

---

## 目录

- [一句话定位](#一句话定位)
- [能力总览](#能力总览)
- [贾维斯模式：唤醒与授权](#贾维斯模式唤醒与授权)
- [1. 文件与真实世界访问（9）](#1-文件与真实世界访问9)
- [2. 系统与环境（23）](#2-系统与环境23)
- [3. 记忆（9）](#3-记忆9)
- [4. 任务（11）](#4-任务11)
- [5. 联网（4）](#5-联网4)
- [6. 媒体库（6）](#6-媒体库6)
- [7. Shell（2）](#7-shell2)
- [8. MCP 外部工具生态](#8-mcp-外部工具生态)
- [9. 消息渠道：飞书 / 企微](#9-消息渠道飞书--企微)
- [10. 开机自启与桌面壳](#10-开机自启与桌面壳)
- [11. 环境自检与一键修复](#11-环境自检与一键修复)
- [HTTP 接口一览（82）](#http-接口一览82)
- [安全模型](#安全模型)
- [已知边界与不做什么](#已知边界与不做什么)

---

## 一句话定位

> **它不是聊天框，是一个长期住在你电脑里、会记事、有情绪、有安全边界的认知智能体——
> 你可以喊它名字让它开口说话，也可以一句话让它干活。**

---

## 能力总览

| 维度 | 事实 |
|---|---|
| 内置工具 | **64 个**（七大类，见下） |
| HTTP 端点 | **82 个** |
| 认知底座 | PASM V2 十九层（`engine=pasm2` / `tier=full`），可降级 |
| 语音识别 | faster-whisper 本地，约 1.0 秒/句 |
| 语音合成 | piper 本地，**0.17~0.41 秒**/句 |
| 唤醒 | 「黑武士」，同音容错，静音时零上传 |
| 授权 | 11 个高危动作需人工点击确认，双闸门 |
| 数据 | SQLite（FTS5 中文全文）+ 结构化画像 + 预取缓存 |

---

## 贾维斯模式：唤醒与授权

### 交互循环

```
静默（一条静止直线）
  │ 喊「黑武士」/ 点击托盘
  ▼
待命（极轻微呼吸）
  │ 说话（本地 VAD 断句：静音 750ms 视为说完）
  ▼
聆听（波纹跟随你的真实音量）
  ▼
思考（波纹内缩、缓慢脉动）
  ├─ 低危工具 → 直接执行
  └─ 高危工具 → 面板弹出确认卡，等人点一下
  ▼
回应（波纹随它出声的音量起伏）→ 自动回到待命
```

### 分级授权：为什么语音不能直接放行

你最初的想法是「涉及安全时说授权就授权」。**我没有照做**，因为它有一个真实绕过漏洞：

> 唤醒词能被**音箱、电视、微信语音条、录音重放**骗过。
> 你音箱正在播"黑武士，把桌面删了"，它就真听见了——等于把钥匙挂在门上。

所以定成两级（你确认采纳）：

| 等级 | 涵盖工具 | 语音行为 |
|---|---|---|
| **低危** | `web_search` `web_read` `read_file` `list_dir` `get_time` `list_tasks` `autostart_status` `find_command` 等 | **直接执行**，不打断 |
| **高危** | `delete_file` `write_file` `run_command` `grant_access` `revoke_access` `open_app` `set_reminder` `setup_autostart` `cancel_autostart` `memory_write` `memory_consolidate` **+ 全部 MCP 工具** | 语音只**提出请求**，面板显示"要执行什么 + 为什么拦"，**人点一下**才执行 |

即使点了确认，`policy` 与 PASM V2 **两道闸门仍会再审一次**——实测 `delete_file`
即使通过贾维斯层，仍被内层拒绝（危险工具需显式 `allow_dangerous`）。纵深防御是有意的。

### 三个刻意的工程取舍

1. **静音时一个字节都不上传。** 只做本地能量门控；有人说话才切片送去识别，
   识别完只留文本、音频立刻丢。
2. **唤醒按「同音组」容错**，不是死记变体列表。实测 ASR 把「黑武士」稳定听成
   「黑午市」，枚举列表覆盖不住。容忍线：`黑午市`(3/3同音)放行 /
   `黑土匪`(1/2)拒绝 / 单字`黑`拒绝。
3. **「我做完了/没做完」不由模型说。** 实测本地小模型会谎报——命令是"把桌面
   文件删了，顺便告诉我几点"，它汇报「文件已删除」，而文件还在等确认；
   加防幻觉指令**完全无效**。所以这类措辞只用我们自己从真实工具结果生成的摘要。

### 实测数据（本机，无独立显卡，无任何云端 Key）

| 环节 | 实测 |
|---|---|
| ASR 模型加载 | 4.0 s（常驻，每句边际成本仅推理） |
| ASR 单句识别 | 0.96~1.03 s |
| TTS 单句合成 | 0.16~0.41 s |
| 唤醒判定 | <1 ms |
| 搜索+回复（本地 7B） | ~15 s |
| 搜索+回复（DeepSeek 云端） | **~6 s** |

---

## 1. 文件与真实世界访问（9）

| 工具 | 用途 |
|---|---|
| `read_file` | 读取文件（相对路径→沙箱；绝对路径需在允许区） |
| `write_file` | 写入文件，自动建父目录 ⚠️高危 |
| `list_dir` | 列目录 |
| `file_info` | 文件/目录元信息 |
| `delete_file` | 删除文件或空目录 ⚠️高危（danger 档） |
| `grant_access` | 授权读写真实目录 ⚠️高危 |
| `revoke_access` | 撤销目录授权 ⚠️高危 |
| `list_workspaces` | 列出当前能读写的全部目录 |
| `text_stats` | 文本行数/字符/最长行 |

**默认可读范围** = 沙箱 ∪ **你授权的目录** ∪（开启整盘权限时）全盘。
授权目录内的写入不再需要危险授权——这是v0.8.0「住进电脑」的基础。

> `full_fs_access` 与 `allowed_paths` **刻意不开放给通用设置口**，只能走
> `/api/access/grant`（带独立审计）。否则任何能打这个接口的脚本都能给自己开整盘读写。

---

## 2. 系统与环境（23）

| 分类 | 工具 |
|---|---|
| 状态查询 | `get_time` `get_status` `environment` `system_probe` `dev_env` `get_panel` `get_profile` |
| 软件与网络 | `list_software` `diagnose_network` `check_port` `which` `find_command` |
| 命令执行 | `run_command` ⚠️高危 · `open_app` ⚠️高危 · `open_url` |
| 提醒与心跳 | `set_reminder` ⚠️高危 · `list_reminders` · `set_tick_interval` |
| 开机自启 | `setup_autostart` ⚠️ · `cancel_autostart` ⚠️ · `autostart_status` |
| 预取与后台 | `list_prefetch` `add_prefetch` `push_background` |

`get_panel` 支持 `weather`（天气）与 `hotspot`（热榜），
网络失败时**诚实降级，不伪造数据**。

---

## 3. 记忆（9）

| 工具 | 用途 |
|---|---|
| `memory_write` | 写入长期记忆 ⚠️高危 |
| `memory_recall` | 检索（语义 + 保留度 + 重要度融合） |
| `memory_list` | 最近记忆 |
| `memory_stats` | 记忆系统状态 |
| `memory_consolidate` | 巩固：相似记忆蒸馏成要点 ⚠️高危 |
| `memory_audit` | 记忆审计账本（谁在何时写了/改了什么） |
| `link_clue` / `list_clues` | 联想线索 |
| `remember_feedback` | 接收反馈以塑形行为 |

记忆会**记住、会遗忘、会巩固**。存储：SQLite + FTS5 中文全文检索。

---

## 4. 任务（11）

`task_create` `list_tasks` `task_current` `task_step_done` `task_step_failed`
`task_complete` `task_pause` `task_resume` `task_skip_step` `task_abandon`
`find_tool`

多步骤任务**持久化，重启可续**。`find_tool` 是自发现入口——工具多时先查再用。

---

## 5. 联网（4）

| 工具 | 说明 |
|---|---|
| `web_search` | 多源回退：必应国内 → 头条 → 搜狗 → DuckDuckGo |
| `web_read` | 读网页正文（自动去标签） |
| `web_headlines` | 只取搜索结果标题（省 token） |
| `open_url` | 系统默认浏览器打开 |

`web_search` 返回值带 `engine` 字段标明**实际用了哪个源**——
区分"网络不通"与"被反爬"，否则用户只会看到一句"搜索失败"。

> 实测：国内网络下 DuckDuckGo 被代理挡死（502），必应国内 0.3s / 头条 1.6s /
> 搜狗 1.3s 都通。v0.8.4 起默认走必应国内。

---

## 6. 媒体库（6）

`media_stats` `add_media_root` `remove_media_root` `scan_media` `list_media`
`probe_media`

登记媒体目录后可按audio/video/image 分类与关键词过滤；
`probe_media` 读单个文件的标题/艺术家/时长/尺寸（未解析字段为 null）。

---

## 7. Shell（2）

`run_command`（danger 档）· `which`

均受安全策略 + 超时约束 + PASM V2 闸门，**贾维斯层也会拦**。

---

## 8. MCP 外部工具生态

配置一行 `mcp_servers` 即可挂载外部 MCP 服务器的工具。

| 特性 | 说明 |
|---|---|
| 传输 | stdio + Streamable HTTP 双支持 |
| 依赖 | **纯标准库**，零强制依赖 |
| 分帧 | 同时兼容「按行」与 `Content-Length`（换实现就连不上的坑提前堵了） |
| 工具命名 | `mcp__<服务器>__<工具>`，超长截断 + 哈希去重 |
| 风险映射 | 只有 `readOnlyHint` 才降为 safe，否则 caution |
| 沙箱 | **MCP 不放宽文件沙箱** |
| 连接 | 后台线程（写错 command 不卡开机） |
| 热重载 | `POST /api/mcp/reload` |
| 授权 | **一律高危**——外部服务器今天返回只读、明天可能改 |

---

## 9. 消息渠道：飞书 / 企微

| 渠道 | 类型 | 能力 |
|---|---|---|
| 飞书 | `feishu_bot` | 群机器人，**仅发** |
| 飞书 | `feishu_app` | 自建应用，**能收能对话** |
| 企微 | `wecom_app` | 自建应用，能收能回，加解密 + token 缓存 |

> **关键区别**：群机器人只能发，自建应用才能对话。要"在飞书里使唤它"必须用自建应用。

端点 `GET/POST /api/channel/{feishu,wecom}`，实现 URL 验证、签名校验、
AES-256-CBC 解密、消息解析。**回复自动发给"刚才那个人"**
（多窗口/群聊不会发错）。

配置步骤见 [USAGE-0.7.0.md](USAGE-0.7.0.md)。

---

## 10. 开机自启与桌面壳

| 能力 | 说明 |
|---|---|
| 单实例锁 | 已在跑时双击图标直接把面板亮出来 |
| 托盘常驻 | 关窗不退出；单击=唤起/收起贾维斯面板 |
| 托盘菜单 | 贾维斯面板 / 主界面 / 环境自检 / 重启内核 / 渲染模式 / 开机自启 / 数据目录 / 退出 |
| 开机自启 | **自启的是桌面壳**（含界面），不是裸内核 |
| 端口探测 | 启动前主动探测，被占用则顺延（`skipped_ports`）——治"改了没生效" |
| 麦克风权限 | 显式放行（Electron 默认拒绝且失败得很安静） |
| 渲染模式 | 自动 / 软件（兼容无独显），**能自愈**：启动未完成下次自动切软件 |

> Windows 上 `SO_REUSEADDR` **允许抢占已绑定端口**，旧实现的"端口占用就顺延"
> 形同虚设，新实例请求全被旧进程吃掉。v0.7.2 起的主动探测治的就是这个。

---

## 11. 环境自检与一键修复

打开 `/doctor.html`（或托盘 → 环境自检与修复）。10 项体检：

| 项 | 缺了会怎样 |
|---|---|
| Python 解释器 | 完全起不来 |
| 黑武士内核 | 无法导入 |
| numpy | 认知引擎**降级**到 builtin/light，记不住长期记忆 |
| PASM V2 十九层 | engine 退回 builtin，认知只剩一层 |
| 思考引擎 | 无法思考，只能当记事本 |
| 本地语音识别环境 | 无法听懂语音（文字对话正常） |
| 语音识别模型 | 首次识别时自动下载 |
| 语音合成模型 | 回落 edge-tts 云端，延迟 3.5 秒 |
| 磁盘空间 | 语音组件约需 600 MB |
| 配置文件 | 读取异常 |

每项显示 **缺什么 / 缺了会怎样 / 装它多大**，点一下才安装，带进度。

> 两条写进代码的原则：**绝不自动安装**（能自己往机器上装东西，它和恶意软件
> 的区别只剩一个用户勾选）；**如实分级**（把"语音不可用"说成"一切正常"比报错更糟）。

---

## HTTP 接口一览（82）

```
健康与状态
  GET    /healthz  /status  /version  /api/bus  /api/events/recent

对话
  POST   /api/ask  /api/send  /api/stream

记忆
  GET    /api/memories  /api/memories/{id}
  POST   /api/memories（写/巩固/审计/线索）
  DELETE /api/memories/{id}

任务
  GET    /api/tasks  /api/tasks/{id}
  POST   /api/tasks（创建/步骤/暂停/恢复/…）

面板与工具
  GET    /api/tools  /api/settings  /api/providers
  GET    /api/status（面板）  /api/cognition/*  /api/mind/*

贾维斯
  GET    /api/jarvis/state
  POST   /api/jarvis/transcribe  /utterance  /confirm
  GET    /api/jarvis/audio

环境自检
  GET    /api/doctor  /api/doctor/jobs
  POST   /api/doctor/fix

MCP 生态
  GET    /api/mcp  /api/mcp/tools
  POST   /api/mcp/reload

真实世界访问权
  GET    /api/access
  POST   /api/access/grant  /api/access/revoke

消息渠道
  GET/POST /api/channel/feishu  /api/channel/wecom

激活与连通性
  POST   /api/activate  /api/llm/ping
```

完整清单：`python -m blackwarrior routes`

---

## 安全模型

1. **两道闸门**：应用层 policy（配置/风险/频率）+ PASM V2 安全层
   （白名单 / 限流 / 注入检测 / 伦理审查）。**模型绕不过**。
2. **危险动作需人工确认**：见[分级授权](#分级授权为什么语音不能直接放行)。
3. **访问权不走通用设置口**：只能 `/api/access/grant`，带独立审计。
4. **配置有备份保护**：写前 `fsync` + 回读校验；主文件坏了自动从 `.bak` 恢复
   （蓝屏曾让 API Key 随 2001 字节全 NUL 的配置一起丢失）。
5. **桌面壳上下文隔离**：渲染进程拿不到 Node，preload 只暴露白名单方法。
6. **说谎防护**：涉及"我做完了"的措辞不经模型生成。
7. **音频端点只读 data 目录**：用 `os.path.commonpath` 限定
   （`startswith` 会被 `C:\data_evil` 这类同前缀目录绕过）。

---

## 已知边界与不做什么

诚实地列出来，免得被误以为是 bug：

| 事项 | 现状 |
|---|---|
| 安装包不含 Python | 内置会带来 +500 MB。探测环境变量 → 内置运行时 → 常见路径 → PATH |
| 不能自动装 Python | doctor 只**检测并说明**，不做引导安装 |
| GPU 依赖 | 无。软件渲染零损失，ASR/TTS 跑在独立 Python 进程里，不受影响 |
| `delete_file` 默认不可用 | 危险工具需显式 `allow_dangerous`——**双闸门的第二道是有意的** |
| 本地小模型会重复内容 | 上下文注入的时间等信息偶尔被照抄；换云端模型明显改善 |
| 天气依赖境外服务 | wttr.in 主站 + 镜像双源；失败时人话报错并自动恢复 |
| MCP 工具一律高危 | 外部服务器的行为不可预判，保守取值 |
| 语音识别模型首次要下载 | 150 MB，走 hf-mirror；装完即缓存 |