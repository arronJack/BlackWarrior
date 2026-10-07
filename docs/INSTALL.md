# 黑武士 BlackWarrior — 安装与排障

> 对应版本 **v0.8.9**。本文覆盖三种安装方式、语音组件的可选安装、
> 以及实测踩过的每一个坑。

---

## 目录

- [系统要求](#系统要求)
- [方式一：安装包（普通用户）](#方式一安装包普通用户)
- [方式二：源码运行（开发者）](#方式二源码运行开发者)
- [方式三：桌面壳开发态](#方式三桌面壳开发态)
- [语音组件（可选）](#语音组件可选)
- [配置思考引擎](#配置思考引擎)
- [第一次启动会看到什么](#第一次启动会看到什么)
- [排障手册](#排障手册)
- [打包（开发者）](#打包开发者)
- [卸载](#卸载)

---

## 系统要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10+ / macOS 11+ / Linux | 桌面壳 Electron 33 |
| Python | 3.9+ | **安装包不含**，需自带 |
| 内存 | 4 GB（建议 8 GB） | 本地语音栈约需 1 GB |
| 磁盘 | 约 700 MB 可用 | 安装包 82 MB + 语音组件 ~600 MB |
| 显卡 | **不需要** | 无独显也能跑（自动软件渲染） |
| 网络 | 可选 | 离线也能用（本地 ollama + 本地语音） |

---

## 方式一：安装包（普通用户）

1. 拿到 `BlackWarrior Setup <版本>.exe`（约 82 MB）；
2. 双击安装，**可自选安装目录**，会创建桌面快捷方式；
3. 首次启动：Electron 会拉起 Python 内核，然后**自动做环境自检**——
   缺什么会列成清单，你可以逐项点安装；
4. 之后托盘常驻，右键托盘图标 →「贾维斯面板」。

### ⚠️ 关键前提：安装包不含 Python

这是最常见的卡点。内置 Python 会让安装包从 82 MB 涨到约 500 MB，
所以我们没做。因此**需要你机器上已装好 Python**。

探测顺序：

```
1. 环境变量 BLACKWARRIOR_PYTHON
2. 安装包内置运行时（如果你自己往 resources/python 放了）
3. C:\Python312\ / C:\Python311\ / C:\Python313\
4. PATH上的 python / python3
```

每个候选都会用「能否 `import blackwarrior`」验证，验不过直接跳过——
所以装了多个 Python 也不会挑错。

### 如果 Python 装在非默认位置

```cmd
setx BLACKWARRIOR_PYTHON "C:\Python312\python.exe"
```

（`setx` 只对**新**开的进程生效，重启应用即可。）

### 如果该解释器还 import 不了 blackwarrior

在**那个解释器**的环境里装一次：

```cmd
C:\Python312\python.exe -m pip install -e D:\path\to\BlackWarrior
```

---

## 方式二：源码运行（开发者）

```bash
git clone https://gitee.com/arronzheng/BlackWarrior.git
cd BlackWarrior

# 1) 本体
python -m pip install -e .

# 2) 认知底座（强烈建议）：给十九层装上 numpy + PASM V2
python -m pip install numpy
cd ../PASM && python -m pip install --no-deps -e . && cd ../BlackWarrior

# 3) 自检（应该全绿）
python -m blackwarrior selftest

# 4) 启动
python -m blackwarrior serve
```

启动后会打印：

```
[BW-READY] {"url": "http://127.0.0.1:3721", "port": 3721,
            "tier": "full", "engine": "pasm2"}
```

`tier: full` + `engine: pasm2` 才是认知底座真的挂上了。
若显示 `builtin` / `light`，说明 numpy 或 PASM V2 没装——回看第 2 步。

---

## 方式三：桌面壳开发态

```bash
cd electron
npm install                       # 首次约 8 分钟（要下 Electron 二进制）
# 国内已配好镜像（.npmrc），无需额外设置

# 告诉壳去哪个 Python 找内核
export BLACKWARRIOR_PYTHON="C:/Python312/python.exe"   # Windows 用 set
npx electron .
```

Windows 上：

```cmd
set BLACKWARRIOR_PYTHON=C:\Python312\python.exe
npx electron .
```

---

## 语音组件（可选）

装上才能语音对话。不装**文字对话一切正常**。

打开 `/doctor.html`（托盘 → 环境自检与修复）点一下即可，
或手动装：

```bash
cd BlackWarrior
python -m venv .venv-asr

# Windows
./.venv-asr/Scripts/pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
    faster-whisper edge-tts "av<15"
# macOS / Linux
./.venv-asr/bin/pip install faster-whisper edge-tts "av<15"
```

### ⚠️ 两个必须知道的坑

1. **`av` 必须钉 `<15`。** PyAV 19.x 移除了 `metadata_errors` 参数，
   faster-whisper 1.2.1 会直接
   `TypeError: open() got an unexpected keyword argument 'metadata_errors'`。

2. **模型下载要国内镜像。** 直连 HuggingFace 会卡到超时。环境里设：

```bash
export HF_ENDPOINT=https://hf-mirror.com       # Windows: set HF_ENDPOINT=...
```

### piper 中文音色（强烈建议）

不装会回落 edge-tts 云端，**延迟 3.5 秒**；装上只要**0.17 秒**且完全离线。

```bash
mkdir -p ~/.local/share/piper
# base URL
https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx
# 配置（注意：必须带请求头，默认 urllib UA 会被拒 403）
https://hf-mirror.com/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json
```

或者直接用 `/doctor.html` 点「下载 piper 中文音色」（63 MB）。

### 各组件落点与体积

| 组件 | 位置 | 体积 |
|---|---|---|
| faster-whisper 环境 | `<项目>/.venv-asr` | ~260 MB |
| whisper base 模型 | `~/.cache/huggingface/hub` | ~150 MB |
| piper 中文音色 | `~/.local/share/piper` | 63 MB |

---

## 配置思考引擎

打开设置页，或调接口：

```bash
curl -X POST http://127.0.0.1:3721/api/activate \
  -H "Content-Type: application/json" \
  -d '{"provider":"deepseek","api_key":"sk-...","model":"deepseek-chat"}'
```

| provider | base_url（自动） | 特点 |
|---|---|---|
| `deepseek` | `https://api.deepseek.com/v1` | 快、便宜、工具调用稳 |
| `openai` | `https://api.openai.com/v1` | 生态好 |
| `qwen` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 国内直连 |
| `ollama` | `http://127.0.0.1:11434/v1` | **完全离线**，首启加载 68s |
| `custom` | 你自己填 | 走代理/中转 |

### ⚠️ 切换 provider 时不要手动清 base_url

v0.8.9 起`base_url` **带归属标记**（`base_url_provider`），换 provider 时旧的
地址自动失效。

> 这个 bug 的实际表现：切到 deepseek 后报 `404 model not found`，
> **换别的模型也一样报 model 错**——因为根因跟模型名毫无关系：
> `base_url` 还指着本地 ollama，deepseek 的请求被打到 ollama。

想自查当前实际端点：

```bash
curl -s http://127.0.0.1:3721/api/status | python -c "
import json,sys; l=json.load(sys.stdin)['llm']
print('provider  :', l['provider'])
print('model     :', l['model'])
print('请求打到  :', l['endpoint'])       # ★看这个，不是看 model
print('地址归属  :', l.get('base_url_provider') or '(未设置)')
print('旧地址失效:', l.get('stale_base_url'))"
```

> `endpoint` 是排查这类问题的**唯一可靠依据**——它就是请求实际发出去的地址。
> 配置写着 deepseek、`endpoint` 却指向本地 ollama，就是配错了。
> `stale_base_url: true` 表示你填过一个属于别的 provider 的地址、已被自动忽略。

---

## 第一次启动会看到什么

| 场景 | 表现 |
|---|---|
| 一切正常 | 主界面 + 贾维斯面板（波纹静止直线）+ 托盘图标 |
| 有block 级缺失 | 自动弹出**环境自检与修复**窗口 |
| 只有 warn 级缺失 | 静默，但托盘里随时可打开体检页 |
| 内核起不来 | 弹错误框，**文案里直接给出三条可操作步骤**（不再是一句干巴巴的报错） |
| 无独显 | 自动软件渲染；波纹动画照样流畅 |

---

## 排障手册

### 「未找到可用的 Python」

按顺序查：

```bash
python -c "import blackwarrior; print(blackwarrior.__file__)"
python -c "import sys; print(sys.executable)"
```

- 第一条报错 → 那个解释器里 `pip install -e <项目路径>`
- 装了多个 Python → 设 `BLACKWARRIOR_PYTHON` 指向能用的那个

### 「改了配置没生效」

**先查版本**：

```bash
curl -s http://127.0.0.1:3721/api/version
```

如果显示的版本比源码旧，多半是**旧进程还在跑**：

```bash
netstat -ano | grep 127.0.0.1:372     # Windows
```

> Windows 的 `SO_REUSEADDR` **允许抢占已绑定端口**，旧实例可能仍在吃掉请求。
> v0.7.2 起启动会主动探测端口并顺延，看 `/api/status` 的 `skipped_ports`。
> 养成习惯：改配置前先确认只有一个实例在跑——
> 两个实例共享同一 `data_root` 会**互相覆盖 config.json**。

### 模型报404 / model 错

```bash
curl -s http://127.0.0.1:3721/api/status | python -c "
import json,sys; l=json.load(sys.stdin)['llm']
print('provider  :', l['provider'])
print('model     :', l['model'])
print('请求打到  :', l['endpoint'])       # ★看这个，不是看 model
print('旧地址失效:', l.get('stale_base_url'))"
```

**`endpoint` 不是你预期的地址** → 就是 base_url 归属问题（v0.8.9 已修，
若复现请把当前 `provider` / `model` / `base_url` / `base_url_provider` 报给我们）。

### 天气面板报「暂时不可用」

v0.8.9 起是主站 + 镜像双源、15 秒超时、人话错误提示，**会自己恢复，不用重启**。
若持续失败，多半是网络问题或城市名拼错。

### Electron 报 `GPU process isn't usable. Goodbye.`

无独显 / 虚拟机 / 远程桌面的机器会踩这个。

**自愈机制**：v0.8.8 起，若启动没到 ready 就退出，会自动写下偏好，
**下次启动自动用软件渲染**。也可以手动切：托盘 →「渲染模式」。

> 不影响语音：ASR/TTS 跑在独立 Python 进程里（faster-whisper CPU int8 /
> piper ONNX），压根不经过 Electron 的 GPU。

### 语音没反应

按顺序排查：

1. **麦克风权限**——桌面壳已自动放行，但系统层可能被拒。
   在浏览器打开 `/jarvis.html`，点「开启麦克风」看有没有反应。
2. **组件是否就绪**：

```bash
curl -s http://127.0.0.1:3721/api/jarvis/state | python -c "
import json,sys; d=json.load(sys.stdin); print('ASR',d['asr']); print('TTS',d['tts'])"
```

`ready: false` → 去 `/doctor.html` 点安装。

3. **唤醒没匹配上**——看事件流里的 `jarvis_heard` / `jarvis_wake`，
   `mode` 字段会告诉你 ASR 到底听成了什么。

### 本地模型特别慢

首次启动模型加载要 **68 秒**（权重加载），之后正常。v0.7.1 起启动会自动预热，
看 `/api/status` 的 `llm.warmup_ms`。

日常对话慢（12~15 秒/轮）是本地 7B 的正常水平，换云端约 6 秒。

### 配置被写坏了

v0.7.2 起配置有三重保护：写前 `fsync` + 回读校验、自动备份 `.bak`、
启动时主文件坏了自动从备份恢复。

若真的损坏过，看 `config.json` 旁边有没有 `config.json.bak`。

> 历史事故：蓝屏打断写入，`config.json` 变成 2001 字节全 NUL，API Key 一起丢失。
> 所以升级到 ≥ v0.7.2 很重要。

---

## 打包（开发者）

```bash
cd electron
npm install
npx electron-builder --win --x64
# 产物：electron/release/BlackWarrior Setup <版本>.exe
```

国内镜像已配在 `electron/.npmrc`，无需手动带环境变量。

### ⚠️ 三个打包必踩的坑

1. **winCodeSign 解压需要符号链接特权。** 未开 Windows 开发者模式时报
   `Cannot create symbolic link : 客户端没有所需的特权`。
   且 electron-builder **每次重试都换新的随机缓存目录名**，手动预解压绕不过去。
   → 已在 `package.json` 设 `win.signAndEditExecutable: false` 跳过 rcedit。
   代价：exe 内部图标/版本元数据用 Electron 默认值
   （安装包与桌面快捷方式仍有图标）。

2. **NSIS 图标必须是真 `.ico`。** 配 `.png` 会报
   `Error while loading icon ...: invalid icon file`。
   用 Pillow 生成多尺寸 ico：

   ```python
   from PIL import Image
   im = Image.open('assets/icon.png').convert('RGBA')
   im.save('assets/icon.ico', format='ICO',
           sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
   ```

3. **`nsis.shortcutIcon` 在 electron-builder 25.1.8 不存在**，写了会直接校验失败。
   合法的是 `installerIcon` / `uninstallerIcon`。

---

## 卸载

1. 托盘 →「退出」；
2. 开始菜单 → BlackWarrior → 卸载（或跑安装包选卸载）；
3. 数据留在 `~/.local/share/BlackWarrior/`（或 `%APPDATA%\BlackWarrior\`）。
   想彻底清除就删这个目录——**API Key 也在里面**，删了要重填。

语音组件体积较大，如需清理：

| 删什么 | 路径 | 省多少 |
|---|---|---|
| 语音识别环境 | `<项目>/.venv-asr` | ~260 MB |
| 识别模型 | `~/.cache/huggingface/hub` | ~150 MB |
| 合成音色 | `~/.local/share/piper` | 63 MB |