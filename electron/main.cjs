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

let win = null;
let tray = null;
let kernel = null;          // Python 子进程
let kernelUrl = null;
let kernelPort = 0;
let quitting = false;
let bootLog = [];

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
 * 依次尝试：环境变量 → 打包内置运行时 → PATH 上的 python。
 * 每个候选用「能否 import blackwarrior」验证，验不过直接跳过——
 * 否则用户机器上装了一堆 Python 却找不到可用解释器时表现会非常诡异。
 */
async function resolvePython() {
  const candidates = [];
  if (process.env.BLACKWARRIOR_PYTHON) candidates.push(process.env.BLACKWARRIOR_PYTHON);
  if (app.isPackaged) {
    const bundled = path.join(process.resourcesPath, 'python', process.platform === 'win32' ? 'python.exe' : 'bin/python3');
    if (fs.existsSync(bundled)) candidates.push(bundled);
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
      '未找到可用的 Python（需要 3.10+ 且已安装 blackwarrior 包）。\n' +
      '可设置环境变量 BLACKWARRIOR_PYTHON 指向解释器路径。');
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

// ---------------------------------------------------------- 托盘

function createTray() {
  try {
    const icon = nativeImage.createFromPath(path.join(__dirname, 'assets', 'tray.png'));
    tray = new Tray(icon);
    const menu = Menu.buildFromTemplate([
      { label: '打开黑武士', click: () => { if (win) { win.show(); win.focus(); } } },
      { type: 'separator' },
      { label: '重启内核', click: async () => {
          killKernel();
          await new Promise(r => setTimeout(r, 1200));
          await startKernel();
          if (win) win.loadURL(kernelUrl);
        } },
      { label: '打开数据目录', click: () => {
          const cfg = path.join(app.getPath('appData'), 'BlackWarrior');
          if (fs.existsSync(cfg)) shell.openPath(cfg);
        } },
      { type: 'separator' },
      { label: '退出', click: () => { quitting = true; app.quit(); } }
    ]);
    tray.setToolTip('黑武士 BlackWarrior');
    tray.setContextMenu(menu);
    tray.on('double-click', () => { if (win) { win.show(); win.focus(); } });
    app.trayAvailable = true;
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
  version: app.getVersion()
}));
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
    if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
  });

  app.whenReady().then(async () => {
    createWindow();
    createTray();

    try {
      await startKernel();
      loadApp();
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
    // 常驻：不退出，交给托盘
  });
}
