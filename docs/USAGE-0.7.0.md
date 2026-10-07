# v0.7.0 使用指南：让黑武士住进你的电脑

> 一句话：这一版把黑武士从"聊天窗口里的助手"变成**能碰你真实文件、能接外部工���生态、
> 能在微信/飞书里随时使唤的常驻智能体**。

三件事各自独立，可单独用也可全开。

---

## 一、放开文件沙箱：让黑武士真能读写你的文件

### 问题
以前黑武士只能读写自己沙箱（`data/sandbox`）里的东西。你说"帮我整理桌面的季报"，
它只能干瞪眼——因为桌面在允许区之外。

### 解法：授权目录（推荐）
```
对话直接说：
  黑武士，把 C:\Users\志哥\Desktop 和 D:\工作 授权给你
```
它会调用 `grant_access` 工具，把目录写进配置并立即生效。

也可以手动在 `data/config.json` 里加：
```json
{
  "allowed_paths": [
    "C:\\Users\\你的用户名\\Desktop",
    "C:\\Users\\你的用户名\\Documents",
    "D:\\工作"
  ]
}
```

**授权后**，这些目录里的读写**不再需要危险授权**（这是 v0.7.0 最大的行为变化）。
想收紧就 `revoke_access`。

### 相关工具
| 工具 | 作用 |
|---|---|
| `grant_access(path, reason)` | 授权一个真实目录 |
| `revoke_access(path)` | 撤销授权 |
| `list_workspaces()` | 看我现在能碰哪些目录 |

### 整盘模式（⚠️ 谨慎）
```json
{ "full_fs_access": true }
```
放开整台机器，不再有任何路径边界。**只在完全可信的独占环境开**。
这个开关**故意不开放给 HTTP 设置接口**（`/api/settings` 会忽略它），
只能用配置文件或直接编辑，避免网页脚本偷偷给自己提权。

### 相关接口
- `GET  /api/access` —— 当前允许区
- `POST /api/access/grant`  `{"path": "..."}`
- `POST /api/access/revoke` `{"path": "..."}`

---

## 二、MCP：接入外部工具生态

### 问题
黑武士自带 60 多个工具，但生态里还有成千上万个工具以 **MCP 服务器**形式存在
（文件系统、GitHub、浏览器、数据库、Figma、Playwright…）。不接 MCP 就用不上。

### 接一个 MCP 服务器
在 `data/config.json` 里加：

```json
{
  "mcp_enabled": true,
  "mcp_servers": [
    {
      "name": "files",
      "transport": "stdio",
      "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\你\\Documents"],
      "timeout": 60
    },
    {
      "name": "myremote",
      "transport": "http",
      "url": "http://127.0.0.1:8931/mcp",
      "headers": { "Authorization": "Bearer xxx" }
    }
  ]
}
```

### 两种传输
| 传输 | 怎么用 | 备注 |
|---|---|---|
| `stdio` | 起一个子进程，用 JSON-RPC 说话 | 最常见。分帧同时兼容"按行"和 `Content-Length` 两种实现 |
| `http` | POST JSON-RPC（Streamable HTTP / SSE 响应） | 连远程 MCP 服务 |

### 行为约定
- **启动不阻塞**：连接在后台线程做，`command` 写错也**不会卡住开机**。
- **命名**：`mcp__<服务器>__<工具>`，超长自动截断加哈希去重，绝不撞内置工具名。
- **风险分级**：服务器声明 `readOnlyHint` 才降为 `safe`，否则 `caution`；
  可以在服务器条目里用 `"risk": "safe|caution|danger"` 强制指定。
- **沙箱依然生效**：MCP 只是多了工具，路径边界仍由策略层统一裁决。
- **永不被筛掉**：工具选择的关键词启发式**不会**过滤 MCP 工具。
- 配好了不用重启进程：`POST /api/mcp/reload` 重连。

### 相关接口
- `GET  /api/mcp` —— 每个服务器的连接状态与报错原因
- `GET  /api/mcp/tools` —— 已挂载的 MCP 工具清单
- `POST /api/mcp/reload` —— 重连（同步返回结果，不玩薛定谔）

---

## 三、飞书 / 企业微信：在 IM 里直接使唤黑武士

### 先分清两种机器人（关键）
| 类型 | 能发 | 能收 | 配置 |
|---|---|---|---|
| **群机器人**（`feishu_bot` / `wecom_bot`） | ✅ | ❌ | 贴一个 webhook 地址即可 |
| **自建应用**（`feishu_app` / `wecom_app`） | ✅ | ✅ | 要 app_id + app_secret |

**想在 IM 里"对话"必须用自建应用**——群机器人收不到消息。

### 方式 A：只收不发（配 webhook 中转，无需公网 IP）
适合"我发消息给某个中转服务，它喂给黑武士"：
```json
{ "channels": [ { "name": "mybridge", "token": "自己定一个",
    "poll_url": "http://127.0.0.1:xxxx/inbox" } ] }
```

### 方式 B：飞书自建应用（完整双向）
1. 飞书开放平台建「企业自建应用」，拿到 `App ID` / `App Secret`
2. 事件订阅里把请求地址填成你的回调（需要公网 HTTPS，可用内网穿透）：
   ```
   https://你的域名/api/channel/feishu
   ```
   订阅事件 `im.message.receive_v1`，校验方式选 token 或加密传输
3. 配置：
```json
{ "channels": [ {
    "name": "feishu", "type": "feishu_app", "enabled": true,
    "app_id": "cli_xxx", "app_secret": "xxx",
    "verification_token": "", "encrypt_key": "",
    "token": "自己定一个内部口令"
} ] }
```

### 方式 C：企业微信自建应用
1. 企业微信后台建自建应用，拿 `corpid` / `corpsecret`
2. 「接收消息」服务器 URL 填 `https://你的域名/api/channel/wecom`，
   填 Token 与 EncodingAESKey（对应配置里的 `token` / `encrypt_key`）
3. 配置：
```json
{ "channels": [ {
    "name": "wecom", "type": "wecom_app", "enabled": true,
    "app_id": "corpid", "app_secret": "corpsecret",
    "token": "回调Token", "encrypt_key": "EncodingAESKey",
    "receive_id": ""   // 留空 = @all
} ] }
```

### 回复发给谁
入站消息里解析出的发件人（`chat_id` / `open_id` / `FromUserName`）会被记为
该渠道的**当前回复目标**——所以多窗口、私聊、群里都不会发错人。

### 路径
```
GET  /api/channel/feishu   ← URL 验证（后台"保存"时会打这个）
POST /api/channel/feishu   ← 事件推送
GET  /api/channel/wecom    ← URL 验证
POST /api/channel/wecom    ← 消息（XML + AES）
```

### 安全
- 签名校验失败、时间戳过期、密文解不开 —— **一律如实拒绝并返回原因**，绝不"猜着解析"。
- 内部仍走 `ChannelBridge.inbound` 的令牌闸门。
- 密文解密需要 `cryptography` 包；没装会**明确报错**而不是假装成功：
  `pip install cryptography`

---

## 四、开机自启（"一直在"）

```
黑武士，设置开机自启
```
它会在 Windows 启动文件夹写一个 `.vbs`，以**隐藏窗口**启动 `blackwarrior serve`
（不会开机弹黑框）。

- `setup_autostart()` / `cancel_autostart()` / `autostart_status()`
- 想彻底取消：删掉
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BlackWarrior_autostart.vbs`

---

## 五、顺手修掉的一个坑：端口抢占

Windows 的 `SO_REUSEADDR` 允许**抢占**已被占用的端口。结果是：新实例"启动成功"，
但所有 API 请求都被**先启动的旧进程**吃掉——界面能开，改的代码却没生效。

v0.7.0 起启动会**主动探测**端口上有没有活着的服务，有人就往后顺延，
并把跳过的端口记在 `/api/version` 之外的状态里（`skipped_ports`）。

> 如果你遇到"改了没生效"，先看 `blackwarrior status` 报的端口号，
> 以及是不是忘了关掉旧实例。

---

## 自检覆盖

`blackwarrior selftest` 新增：

- 渠道入站（飞书/企微签名、加解密、消息解析、未配置如实 404）
- MCP 客户端（真起一个 stdio 服务器跑握手/注册/调用/token 缓存）
- 访问权（授权前拦、授权后放行、full_fs 开与关、越界绝对路径被拒）
- 开机自启脚本内容生成
- MCP 工具在关键词筛选下**永不被剔除**（回归守卫）
