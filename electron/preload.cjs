/**
 * 黑武士 preload —— 上下文隔离下的受控桥。
 *
 * 铁律：
 *   - contextBridge 只暴露**白名单方法**，绝不暴露 ipcRenderer 原始对象
 *   - 渲染进程拿不到 Node，是防 XSS 的最后一道墙
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('bwDesktop', {
  platform: process.platform,
  isDesktop: true,

  status: () => ipcRenderer.invoke('bw:status'),
  openExternal: (url) => ipcRenderer.invoke('bw:open-external', url),
  revealData: () => ipcRenderer.invoke('bw:reveal-data'),
  relaunch: () => ipcRenderer.invoke('bw:relaunch'),

  // —— 贾维斯面板控制（v0.8.7）——
  // 页面里要判断"我是在桌面壳里还是普通浏览器里"：
  // 浏览器里没有 bwDesktop，就退回点击麦克风的手动模式。
  jarvisShow: () => ipcRenderer.invoke('bw:jarvis-show'),
  jarvisHide: () => ipcRenderer.invoke('bw:jarvis-hide'),
  jarvisToggle: () => ipcRenderer.invoke('bw:jarvis-toggle'),
  setAutoLaunch: (enable) => ipcRenderer.invoke('bw:set-auto-launch', !!enable),

  // 内核异常退出通知
  onKernelGone: (fn) => {
    if (typeof fn !== 'function') return;
    ipcRenderer.on('bw:kernel-gone', (_e, info) => fn(info));
  }
});
