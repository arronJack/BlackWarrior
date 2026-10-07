/**
 * 黑武士 BlackWarrior — Electron 主进程
 *
 * 职责：
 *   1. 拉起 Python 内核（blackwarrior serve），解析 [BW-READY] 拿真实端口
 *   2. 单实例锁 / 托盘 / 关闭最小化到托盘
 *   3. 窗口加载内核自带的 UI（同源，天然无 CORS）
 *   4. 退出时干净地终止内核（Windows 下杀进程树）
 *
 * 设计原则：壳只做壳的事，所有智能都在 Python 内核里。
 */

const { app, BrowserWindow, Tray, Menu, ipcMain, shell, nativeImage, dialog } = require('electron');
const { spawn, execFile } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

// ---------------------------------------------------------- 常量

const IS_DEV = !app.isPackaged;
const PROJECT_ROOT = IS_DEV ? path.resolve(__dirname, '..') : path.resolve(__dirname, '..', '..');
const READY_PREFIX = '[BW-READY]';
const HEALTH_TIMEOUT_MS = 90 * 1000;   // 首次冷启动给足时间

// ★必须在 app ready 之前调用，否则不生效。
// 这台机器没有独立显卡，Electron 的 GPU 进程起不来，会连续报
//   GPU process exited unexpectedly: exit_code=1
// 然后直接 FATAL: GPU process isn't usable. Goodbye. ——**整个应用起不来**。
// 本项目只画 canvas 波形，不吃 GPU，禁用硬件加速零损失。
// （无独显 / 虚拟机 / 远程桌面会话的机器全都会踩这个，必须无条件关。）
try { app.disableHardwareAcceleration(); } catch (_) {}

let win = null;
let jarvis = null;         // 贾维斯面板（无边框置顶小窗）
let tray = null;
let kernel = null;          // Python 子进程
let kernelUrl = null;
let kernelPort = 0;
let quitting = false;
let bootLog = [];

// 面板状态：托盘图标靠它变色，用户一眼知道它在不在听
const JARVIS_W = 900;
const JARVIS_H = 560;

// ---------------------------------------------------------- 基础工具

function log(...args) {
  const line = args.map(a => (typeof a === 'string' ? a : JSON.stringify(a))).join(' ');
  bootLog.push(line);
  if (bootLog.length > 400) bootLog.splice(0, bootLog.length - 400);
  if (!IS_DEV) console.log(line);
}

function httpGet(url, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const t = setTimeout(() => { if (!done) { done = true; resolve(null); } }, timeoutMs || 4000);
    try {
      const req = http.get(url, (res) => {
        clearTimeout(t);
        if (done) { try { res.resume(); } catch (_) {} return; }
        done = true;
        resolve(res.statusCode === 200);
        res.resume();
      });
      req.on('error', () => { clearTimeout(t); if (!done) { done = true; resolve(null); } });
    } catch (_) {
      clearTimeout(t); if (!done) { done = true; resolve(null); }
    }
  });
}

/** Windows 下可靠地杀掉 Python 进程树；POSIX 直接 kill。 */
function killKernel() {
  if (!kernel || kernel.killed || kernel.exitCode !== null) return;
  const pid = kernel.pid;
  if (process.platform === 'win32' && pid) {
    try {
      execFile('taskkill', ['/pid', String(pid), '/T', '/F'], () => {});
    } catch (_) { /* 尽力而为 */ }
  }
  try { kernel.kill(); } catch (_) {}
}

// ---------------------------------------------------------- Python 内核定位

/**
 * 依次尝试：环境变量 → 打包内置运行时 → Windows 常见安装位置 → PATH。
 * 每个候选用「能否 import blackwarrior」验证，验不过直接跳过——
 * 否则用户机器上装了一堆 Python 却找不到可用解释器时表现会非常诡异。
 *
 * ★为什么要加"Windows 常见安装位置"：打包产物里目前**没有**内置
 * Python（那要额外打 ~500MB），所以安装后的 exe 完全依赖用户机器上
 * 已经装好的 Python。只探 PATH 是不够的——很多人（包括本机）装的
 * Python 在 C:\Python312 这种非默认位置，PATH 里根本没有。
 * 找不到时宁可明确报错，也别让用户对着"内核启动失败"猜。
 */
async function resolvePython() {
  const candidates = [];
  if (process.env.BLACKWARRIOR_PYTHON) candidates.push(process.env.BLACKWARRIOR_PYTHON);
  if (app.isPackaged) {
    const bundled = path.join(process.resourcesPath, 'python', process.platform === 'win32' ? 'python.exe' : 'bin/python3');
    if (fs.existsSync(bundled)) candidates.push(bundled);
  } else {
    // 开发态：先试本机已知的可用解释器，避免装了一堆环境却挑错
    candidates.push('C:/Python312/python.exe', 'C:/Python311/python.exe',
                    'C:/Python313/python.exe');
  }
  candidates.push(process.platform === 'win32' ? 'python' : 'python3', 'python');

  const probe = [
    '-c',
    'import blackwarrior, sys;'
    + 'print(blackwarrior.__file__); print(getattr(blackwarrior, "__version__", "?"))'
  ];

  for (const exe of candidates) {
    const ok = await new Promise((resolve) => {
      execFile(exe, probe, { timeout: 20000, cwd: PROJECT_ROOT }, (err, stdout) => {
        if (err) { resolve(null); return; }
        const out = String(stdout || '').trim().split(/\r?\n/);
        resolve({ exe, file: out[0] || '', version: out[1] || '?' });
      });
    });
    if (ok) { log(`[python] 使用 ${ok.exe} (v${ok.version})`); return ok; }
    log(`[python] 候选不可用: ${exe}`);
  }
  return null;
}

/**
 * 决定怎么把内核拉起来。
 *
 * 打包态有两种可能的落点，**必须真的区分开**：
 *   1. `resources/app/` 里带着源码（目录或 zipapp）→ 需要把该目录加进 PYTHONPATH；
 *   2. 解释器是内置的、blackwarrior 已装进 site-packages → 直接 `-m` 即可。
 *
 * 之前这里两个分支返回一模一样的参数，注释还写着"退而求其次用 zipapp 直跑"，
 * 实际上什么都没做——真到了"包里没带源码"的机器上，只会看到一个
 * `No module named blackwarrior` 的原始报错，用户完全不知道该怎么办。
 */
function pythonArgs() {
  if (app.isPackaged) {
    const appdir = path.join(process.resourcesPath, 'app');
    if (fs.existsSync(path.join(appdir, 'blackwarrior'))) {
      // 源码在 resources/app → 显式告知解释器去哪找包
      return { args: ['-m', 'blackwarrior.cli'], extraPath: appdir };
    }
    // 没带源码：只能靠解释器里已安装的包。启动前先验一次，
    // 免得等到 spawn 失败才报一句让人摸不着头脑的模块错误。
    return { args: ['-m', 'blackwarrior.cli'], extraPath: null,
             warn: '安装包内未找到 resources/app 源码，'
                 + '将依赖系统 Python 中已安装的 blackwarrior 包。' };
  }
  return { args: ['-m', 'blackwarrior.cli'], extraPath: PROJECT_ROOT, warn: null };
}

// ---------------------------------------------------------- 启动内核

async function startKernel() {
  const py = await resolvePython();
  if (!py) {
    throw new Error(
      '未找到可用的 Python（需要 3.10+ 且已安装 blackwarrior 包）。\n\n'
      + '本安装包**没有内置 Python**（内置会带来约 500MB 体积），\n'
      + '所以需要你机器上已装好 Python。可任选一种方式解决：\n'
      + '  1) 设置环境变量 BLACKWARRIOR_PYTHON 指向解释器完整路径，\n'
      + '     例如 C:\\Python312\\python.exe（装在非默认位置时最省事）\n'
      + '  2) 确认该解释器能 import blackwarrior（pip install 本项目）');
  }

  const spec = pythonArgs();
  if (spec.warn) { log('[kernel] ' + spec.warn); }
  const args = spec.args.concat(['serve', '--host', '127.0.0.1']);
  log('[kernel] 启动:', py.exe, args.join(' '));

  // 打包态源码不在 site-packages 时，靠 PYTHONPATH 把它指到 resources/app；
  // 已有 PYTHONPATH 时**追加**而不是覆盖（用户可能自己设了别的路径）。
  const envPath = spec.extraPath
    ? (process.env.PYTHONPATH
        ? process.env.PYTHONPATH + ';' + spec.extraPath
        : spec.extraPath)
    : process.env.PYTHONPATH;

  kernel = spawn(py.exe, args, {
    cwd: IS_DEV ? PROJECT_ROOT : process.resourcesPath,
    env: Object.assign({}, process.env, {
      PYTHONUNBUFFERED: '1',
      PYTHONIOENCODING: 'utf-8',
      PYTHONPATH: envPath,
      BW_SHELL: 'electron'
    }),
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe']
  });

  kernel.stdout.on('data', (buf) => {
    const text = buf.toString('utf8');
    log(text.trim());
    const m = text.split(/\r?\n/).find(l => l.startsWith(READY_PREFIX));
    if (m) {
      try {
        const info = JSON.parse(m.slice(READY_PREFIX.length).trim());
        kernelUrl = info.url;
        kernelPort = info.port;
        log('[kernel] 就绪：' + kernelUrl + ' 档位=' + info.tier + '/' + info.engine);
      } catch (e) { log('[kernel] READY 行解析失败', String(e)); }
    }
  });
  kernel.stderr.on('data', (buf) => log('[py-err]', buf.toString('utf8').trim()));
  kernel.on('exit', (code, signal) => {
    log('[kernel] 退出 code=' + code + ' signal=' + signal);
    kernel = null; kernelUrl = null;
    if (win && !win.isDestroyed() && !quitting) {
      win.webContents.send('bw:kernel-gone', { code, signal });
    }
  });

  // 等待健康检查（READY 行 + healthz 双保险）
  const deadline = Date.now() + HEALTH_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (kernelUrl) {
      const ok = await httpGet(kernelUrl + '/api/healthz', 2500);
      if (ok) { return kernelUrl; }
    } else {
      await new Promise(r => setTimeout(r, 500));
    }
  }
  throw new Error('内核健康检查超时（' + HEALTH_TIMEOUT_MS / 1000 + 's），详见日志。');
}

// ---------------------------------------------------------- 窗口

function createWindow() {
  win = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 940,
    minHeight: 620,
    show: false,
    backgroundColor: '#04050a',
    icon: path.join(__dirname, 'assets', 'icon.png'),
    autoHideMenuBar: true,
    title: '黑武士 BlackWarrior',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,        // 铁律：上下文隔离，渲染进程碰不到 Node
      nodeIntegration: false,
      sandbox: true,
      spellcheck: false
    }
  });

  win.once('ready-to-show', () => {
    win.show();
    if (IS_DEV) win.webContents.openDevTools({ mode: 'detach' });
  });

  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);        // 外链一律交给系统浏览器
    return { action: 'deny' };
  });

  win.on('close', (e) => {
    // 关闭=最小化到托盘（托盘菜单里有真退出）；这是常驻 Agent 的惯例
    if (!quitting && app.trayAvailable !== false) {
      e.preventDefault();
      win.hide();
    }
  });

  return win;
}

function loadApp() {
  const fallback = 'file://' + path.join(__dirname, 'pages', 'nokernel.html');
  if (kernelUrl) {
    win.loadURL(kernelUrl).catch(() => win.loadURL(fallback));
  } else {
    win.loadURL(fallback);
  }
}

// ---------------------------------------------------------- 贾维斯面板

/**
 * 贾维斯面板：无边框、置顶、可穿透点击的小窗。
 *
 * 为什么单独开一个窗口而不是塞进主界面：
 * 主界面是1280x800 的工作台，而这个面板的定位是**常驻在屏幕边缘、
 * 随时能被喊醒**。把它做成独立小窗才能做到「不打断你正在做的事」。
 *
 * 三个刻意的取舍：
 *   1. `transparent: true` + `frame: false` —— 无边框窗口在 Windows 上
 *      必须配transparent 才是真透明；只设frame:false 会得到一个黑框。
 *   2. `resizable: false` + 固定尺寸 —— 面板不该被拖成别的形状，
 *      波纹的构图是定死的。
 *   3. **不默认置顶**。置顶窗口会盖住所有东西，包括你正在打的字。
 *      做成「点托盘才置顶，几秒后自动降级」的临时置顶（flashOnTop）。
 */
function createJarvisWindow() {
  if (jarvis && !jarvis.isDestroyed()) return jarvis;

  jarvis = new BrowserWindow({
    width: JARVIS_W,
    height: JARVIS_H,
    show: false,
    frame: false,
    transparent: true,
    backgroundColor: '#00000000',
    resizable: false,
    maximizable: false,
    minimizable: false,
    skipTaskbar: true,
    alwaysOnTop: false,
    hasShadow: false,
    icon: path.join(__dirname, 'assets', 'icon.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });

  // 面板页面自己会拉满窗口并把背景设成透明色；这里只保证不弹外部链接
  jarvis.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  // 点面板空白处穿透到下面的窗口（贾维斯不该挡住你干活）
  jarvis.setIgnoreMouseEvents(true, { forward: true });

  jarvis.on('closed', () => { jarvis = null; updateTray(); });

  return jarvis;
}

function loadJarvis() {
  if (!jarvis || jarvis.isDestroyed()) createJarvisWindow();
  const target = kernelUrl
    ? kernelUrl + '/jarvis.html'
    : 'file://' + path.join(__dirname, 'pages', 'nokernel.html');
  jarvis.loadURL(target).catch((e) => log('[jarvis] 加载失败', String(e)));
  return jarvis;
}

/** 显示面板并短暂置顶——让用户看见它，但别长期压住别的窗口。 */
function showJarvis(flashMs) {
  if (!jarvis || jarvis.isDestroyed()) loadJarvis();
  jarvis.showInactive();          // 不抢当前窗口的焦点
  jarvis.setAlwaysOnTop(true, 'screen-saver');
  jarvis.moveTop();
  setTimeout(() => {
    if (jarvis && !jarvis.isDestroyed()) jarvis.setAlwaysOnTop(false);
  }, flashMs || 6000);
  updateTray();
}

function toggleJarvis() {
  if (jarvis && !jarvis.isDestroyed() && jarvis.isVisible()) {
    jarvis.hide();
  } else {
    showJarvis();
  }
  updateTray();
}

// ---------------------------------------------------------- 托盘

/**
 * 托盘图标跟随内核/面板状态变化——用户不用点开就知道它在不在。
 *
 * 亮/暗两态用**内联 SVG 现生成**，不额外放二进制资源：
 * 多一个 png 就多一份要维护、容易忘的资产，而这里要的只是"一个点
 * 变亮"。SVG 只有几百字节，Electron 的 nativeImage 也能直接吃
 * data URL。SVG 不可用时退回原png（见下面的 catch）。
 */
function updateTray() {
  if (!tray || tray.isDestroyed()) return;
  const panelOn = !!(jarvis && !jarvis.isDestroyed() && jarvis.isVisible());
  const basePath = path.join(__dirname, 'assets', 'tray.png');
  try {
    const dot = panelOn ? '#39ffb0' : '#4a5a68';
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32">`
      + `<circle cx="16" cy="16" r="13" fill="none" stroke="${dot}" stroke-width="2"/>`
      + `<circle cx="16" cy="16" r="6" fill="${dot}"/></svg>`;
    const img = nativeImage.createFromDataURL(
      'data:image/svg+xml;base64,' + Buffer.from(svg).toString('base64'));
    tray.setImage(img.isEmpty() ? nativeImage.createFromPath(basePath) : img);
  } catch (_) {
    try { tray.setImage(nativeImage.createFromPath(basePath)); } catch (_) {}
  }
  tray.setToolTip(panelOn
    ? '黑武士 · 贾维斯面板已打开（点此隐藏）'
    : '黑武士 · 点击唤起贾维斯面板');
}

function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: '贾维斯面板', click: () => toggleJarvis() },
    { label: '主界面', click: () => { if (win) { win.show(); win.focus(); } } },
    { type: 'separator' },
    { label: '重启内核', click: async () => {
        killKernel();
        await new Promise(r => setTimeout(r, 1200));
        await startKernel();
        if (win) win.loadURL(kernelUrl);
        if (jarvis && !jarvis.isDestroyed()) loadJarvis();
      } },
    { label: '开机自启（登录后自动运行）',
      type: 'checkbox',
      checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => setAutoLaunch(item.checked) },
    { label: '打开数据目录', click: () => {
        const cfg = path.join(app.getPath('appData'), 'BlackWarrior');
        if (fs.existsSync(cfg)) shell.openPath(cfg);
      } },
    { type: 'separator' },
    { label: '退出', click: () => { quitting = true; app.quit(); } }
  ]);
}

/**
 * 开机自启由 **Electron 自己**注册（app.setLoginItemSettings），
 * 而不是内核去写 .vbs。
 *
 * 之前内核的 setup_autostart 写vbs 拉起的是 `python -m blackwarrior serve`
 * ——只起内核，**没有界面**。开机后用户看到的是"服务在跑但什么都没有"，
 * 还得自己找 exe 打开。桌面 Agent 的开机自启必须把**壳**一起拉起来。
 */
function setAutoLaunch(enable) {
  try {
    app.setLoginItemSettings({
      openAtLogin: !!enable,
      // 开发态（electron .）注册的是 electron.exe，必须带上项目路径参数，
      // 否则开机后 Electron 启动器找不到 main.cjs 直接退出。
      args: IS_DEV ? [PROJECT_ROOT] : []
    });
    log('[autostart] 已' + (enable ? '开启' : '关闭'));
    return true;
  } catch (e) {
    log('[autostart] 设置失败', String(e));
    return false;
  }
}

function createTray() {
  try {
    const icon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray.png'));
    tray = new Tray(icon);
    tray.setContextMenu(buildTrayMenu());
    tray.on('click', () => toggleJarvis());
    tray.on('double-click', () => { if (win) { win.show(); win.focus(); } });
    app.trayAvailable = true;
    updateTray();
  } catch (e) {
    log('[tray] 创建失败', String(e));
    app.trayAvailable = false;
  }
}

// ---------------------------------------------------------- IPC

ipcMain.handle('bw:status', () => ({
  kernelUrl, kernelPort, running: !!kernel,
  log: bootLog.slice(-120),
  platform: process.platform,
  version: app.getVersion(),
  jarvisOpen: !!(jarvis && !jarvis.isDestroyed() && jarvis.isVisible()),
  autoLaunch: (() => { try { return app.getLoginItemSettings().openAtLogin; } catch (_) { return false; } })()
}));

// —— 面板控制（渲染进程只能调这几个白名单方法）——
ipcMain.handle('bw:jarvis-show', () => {
  if (quitting) return { ok: false, reason: '正在退出' };
  try { showJarvis(8000); return { ok: true, kernelUrl }; }
  catch (e) { return { ok: false, reason: String(e && e.message || e) }; }
});
ipcMain.handle('bw:jarvis-hide', () => {
  if (jarvis && !jarvis.isDestroyed()) jarvis.hide();
  updateTray();
  return { ok: true };
});
ipcMain.handle('bw:jarvis-toggle', () => { toggleJarvis(); return { ok: true }; });
ipcMain.handle('bw:set-auto-launch', (_e, enable) => ({ ok: setAutoLaunch(!!enable) }));

ipcMain.handle('bw:open-external', (_e, url) => {
  if (/^https?:\/\//.test(String(url))) shell.openExternal(String(url));
});
ipcMain.handle('bw:reveal-data', () => {
  const base = app.getPath('appData');
  const dir = path.join(base, 'BlackWarrior');
  try { fs.mkdirSync(dir, { recursive: true }); } catch (_) {}
  shell.openPath(dir);
});
ipcMain.handle('bw:relaunch', () => { app.relaunch(); app.exit(0); });

// ---------------------------------------------------------- 生命周期

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 已经在跑就把面板亮出来——开机自启后用户双击图标，第一反应
    // 就是"唤起来看看"，而不是只看主界面。
    showJarvis(8000);
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });

  app.whenReady().then(async () => {
    // ★麦克风权限：Electron 默认**拒绝** media 权限，而且失败得很安静——
    // getUserMedia 直接 reject，页面只表现为"点了开启麦克风没反应"，
    // 面板永远停在待命。这个坑不显式处理一定会遇到。
    const ses = require('electron').session;
    ses.defaultSession.setPermissionRequestHandler((wc, permission, cb) => {
      // 只放行麦克风；其它一律拒绝（不需要摄像头/通知/定位）
      cb(permission === 'media' || permission === 'audioCapture');
    });
    ses.defaultSession.setPermissionCheckHandler((wc, permission) => {
      return permission === 'media' || permission === 'audioCapture';
    });

    createWindow();
    createTray();

    // 开机自启触发的启动：只起托盘+面板，不弹主界面窗口。
    // 理由是开机时用户多半在做别的事，弹一个 1280x800 抢焦点很粗暴。
    const silent = process.argv.includes('--autostart');
    if (!silent) { win.show(); }

    try {
      await startKernel();
      loadApp();
      // 面板在内核就绪后再建：它要加载 kernelUrl + /jarvis.html
      loadJarvis();
      if (!silent) showJarvis(6000);
    } catch (err) {
      log('[boot] 失败:', String(err && err.message || err));
      dialog.showMessageBox(win, {
        type: 'error',
        title: '内核启动失败',
        message: String(err && err.message || err),
        buttons: ['重试', '退出']
      }).then(({ response }) => {
        if (response === 0) { app.relaunch(); app.exit(1); }
        else app.exit(1);
      });
    }

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) { createWindow(); loadApp(); }
      else if (win) { win.show(); }
    });
  });

  app.on('before-quit', () => { quitting = true; });
  app.on('will-quit', () => { killKernel(); });
  app.on('window-all-closed', (e) => {
    // 常驻：不退出，交给托盘。面板窗口（skipTaskbar）不算"用户要关的窗口"。
  });
}
