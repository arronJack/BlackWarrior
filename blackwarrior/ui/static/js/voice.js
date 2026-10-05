/* ============================================================
   voice.js — 语音能力（ASR 识别 + TTS 朗读）

   实现选择：走浏览器内置 Web Speech API，不引任何第三方依赖，也不要求
   Python 侧装 edge-tts。在 Electron（Chromium）里开箱可用。

   必须知道的边界（照实写，别让用户自己踩）：
     - **TTS（朗读）完全本地**，离线可用，浏览器/桌面壳里都能用；
     - **ASR（识别）在 Chromium 里走云端识别服务**，而 Electron 打包的
       Chromium **不含** 该服务的 API key —— 所以在桌面壳里 ASR 基本不可用，
       在 Chrome / Edge 浏览器里打开才可用。
       因此所有 ASR 调用都必须优雅降级：置灰按钮 + 说明原因，绝不静默失效。

   若要在桌面壳内做真语音输入，需要换本地 ASR（如 whisper.cpp / faster-whisper），
   那是另一条依赖较重的链路，当前版本不引入。
   ============================================================ */
(function (global) {
  'use strict';

  const BW = global.BW || (global.BW = {});

  const SR = global.SpeechRecognition || global.webkitSpeechRecognition || null;
  const SS = global.speechSynthesis || null;

  const V = {
    rec: null,
    listening: false,
    lang: 'zh-CN'
  };

  // ---------------------------------------------------------- 能力探测

  V.available = function () {
    return {
      asr: !!SR,
      tts: !!SS,
      asr_note: SR ? 'Chromium 云端识别（需联网）' : '当前环境不支持语音识别'
    };
  };

  // ---------------------------------------------------------- ASR

  /**
   * 开始识别。
   * opts: { onPartial(text), onFinal(text), onEnd(), onError(msg) }
   */
  V.startAsr = function (opts) {
    opts = opts || {};
    if (!SR) {
      if (opts.onError) opts.onError('当前环境不支持语音识别（需要 Chromium/Chrome/Edge）');
      return false;
    }
    if (V.listening) { return true; }

    const rec = new SR();
    rec.lang = opts.lang || V.lang;
    rec.continuous = true;       // 允许说长句
    rec.interimResults = true;   // 边说边出字，体验差别很大
    rec.maxAlternatives = 1;

    let finalText = '';

    rec.onresult = function (ev) {
      let interim = '';
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) { finalText += r[0].transcript; }
        else { interim += r[0].transcript; }
      }
      const live = (finalText + interim).trim();
      if (opts.onPartial) opts.onPartial(live);
    };

    rec.onerror = function (ev) {
      const msg = ({
        'not-allowed': '麦克风权限被拒绝',
        'no-speech': '没有检测到说话',
        'audio-capture': '找不到麦克风设备',
        'network': '识别服务不可达（该能力需要联网）'
      })[ev.error] || ('识别失败：' + ev.error);
      V.listening = false;
      if (opts.onError) opts.onError(msg);
    };

    rec.onend = function () {
      V.listening = false;
      const t = finalText.trim();
      if (t && opts.onFinal) opts.onFinal(t);
      else if (opts.onEnd) opts.onEnd();
      V.rec = null;
    };

    try {
      rec.start();
    } catch (e) {
      if (opts.onError) opts.onError('无法启动识别：' + e.message);
      return false;
    }
    V.rec = rec;
    V.listening = true;
    return true;
  };

  V.stopAsr = function () {
    if (V.rec) { try { V.rec.stop(); } catch (_) {} }
    V.listening = false;
  };

  // ---------------------------------------------------------- TTS

  function pickVoice(name) {
    if (!SS) { return null; }
    const voices = SS.getVoices() || [];
    if (!voices.length) { return null; }
    if (name) {
      const hit = voices.find(v => v.name === name || v.name.indexOf(name) >= 0);
      if (hit) { return hit; }
    }
    // 优先中文音色
    return voices.find(v => /zh|Chinese|中文/i.test(v.lang + ' ' + v.name)) || voices[0];
  }

  V.listVoices = function () {
    return SS ? (SS.getVoices() || []).map(v => ({ name: v.name, lang: v.lang })) : [];
  };

  /** 朗读；返回是否真的朗读了。 */
  V.speak = function (text, opts) {
    opts = opts || {};
    if (!SS || !text) { return false; }
    try { SS.cancel(); } catch (_) {}
    const u = new SpeechSynthesisUtterance(String(text));
    u.lang = opts.lang || V.lang;
    u.rate = opts.rate || 1.0;
    u.pitch = opts.pitch || 1.0;
    u.volume = opts.volume === undefined ? 1.0 : opts.volume;
    const v = pickVoice(opts.voice);
    if (v) { u.voice = v; }
    if (opts.onEnd) { u.onend = opts.onEnd; }
    if (opts.onError) { u.onerror = opts.onError; }
    SS.speak(u);
    return true;
  };

  V.stopSpeak = function () {
    if (SS) { try { SS.cancel(); } catch (_) {} }
  };

  V.speaking = function () {
    return !!(SS && SS.speaking);
  };

  // 部分浏览器首次 getVoices() 返回空，需等 voiceschanged
  if (SS && typeof SS.addEventListener === 'function') {
    SS.addEventListener('voiceschanged', () => { /* 触发一次内部刷新即可 */ });
  }

  BW.voice = V;

})(window);
