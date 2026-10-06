/* ============================================================
   voice.js — 语音能力

   为什么不用浏览器自带的 SpeechRecognition：
     Electron 打包的 Chromium **不带** 云端识别服务的 API key，
     桌面壳里该 API 基本不可用。所以主通道走——

       麦克风 → MediaRecorder → 上传内核 → OpenAI 兼容 /audio/transcriptions

   三条通道按可用性自动选择，任何一条不可用都显式降级并说明原因：
     1. 云端转写（需配 asr_enabled + Key）—— 桌面壳内可用，主通道
     2. 浏览器 SpeechRecognition —— 仅在 Chrome/Edge 打开时可用，免配置
     3. 都不可用 → 麦克风置灰 + 说明

   朗读：浏览器本地合成（离线可用）为主，配了云端 TTS 时用云端（音质更好）。
   打断（barge-in）：一开始说话就停止朗读，避免自己盖自己。
   ============================================================ */
(function (global) {
  'use strict';

  const BW = global.BW || (global.BW = {});
  const api = BW.api;

  const SR = global.SpeechRecognition || global.webkitSpeechRecognition || null;
  const SS = global.speechSynthesis || null;

  const V = {
    lang: 'zh-CN',
    recording: false,
    _stream: null,
    _rec: null,
    _actx: null,
    _analyser: null,
    _freq: null,
    _chunks: [],
    _orb: null,
    _orbRaf: 0,
    _level: 0,
    srRec: null,
    srListening: false
  };

  // ---------------------------------------------------------- 能力探测

  V.available = function () {
    return {
      mic: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
      recorder: typeof global.MediaRecorder !== 'undefined',
      browser_asr: !!SR,
      browser_tts: !!SS,
      // 云端通道能不能用由 /api/voice/status 决定，见 refreshStatus()
      cloud: V._cloud || { asr_ready: false, asr_reason: '未查询' }
    };
  };

  /** 拉一次后端语音状态，前端据此决定按钮可用性。 */
  V.refreshStatus = function () {
    return api.get('/api/voice/status').then(st => {
      V._cloud = st || {};
      return V._cloud;
    }).catch(() => {
      V._cloud = { asr_ready: false, asr_reason: '内核未连接' };
      return V._cloud;
    });
  };

  /** 返回 { mode, why } —— mode: cloud | browser | none */
  V.pickAsrMode = function () {
    const a = V.available();
    if (a.cloud && a.cloud.asr_ready && a.recorder && a.mic) {
      return { mode: 'cloud', why: '云端转写（需联网）' };
    }
    if (a.browser_asr) {
      return { mode: 'browser', why: '浏览器识别（Chrome/Edge 内可用，免配置）' };
    }
    return {
      mode: 'none',
      why: (a.cloud && a.cloud.asr_reason) ||
           '缺少麦克风或录音能力，且当前环境不支持浏览器识别'
    };
  };

  // ---------------------------------------------------------- 麦克风录制

  /**
   * 开始录音。
   * opts: { onLevel(0..1), onError(msg) }
   */
  V.startRecording = function (opts) {
    opts = opts || {};
    if (V.recording) { return Promise.resolve(true); }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      if (opts.onError) opts.onError('当前环境没有麦克风能力');
      return Promise.resolve(false);
    }
    V.stopSpeak();     // barge-in：开口即停朗读

    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true }
    }).then(stream => {
      V._stream = stream;
      V._chunks = [];

      // 音量分析（驱动语音球）
      try {
        const AC = global.AudioContext || global.webkitAudioContext;
        V._actx = new AC();
        const src = V._actx.createMediaStreamSource(stream);
        V._analyser = V._actx.createAnalyser();
        V._analyser.fftSize = 256;
        V._analyser.smoothingTimeConstant = 0.75;
        src.connect(V._analyser);
        V._freq = new Uint8Array(V._analyser.frequencyBinCount);
      } catch (_) { V._analyser = null; }

      let mime = '';
      if (typeof global.MediaRecorder !== 'undefined') {
        const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4',
                            'audio/ogg;codecs=opus'];
        for (const m of candidates) {
          if (global.MediaRecorder.isTypeSupported &&
              global.MediaRecorder.isTypeSupported(m)) { mime = m; break; }
        }
        V._rec = new global.MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
        V._mime = mime || 'audio/webm';
        V._rec.ondataavailable = (e) => { if (e.data && e.data.size) V._chunks.push(e.data); };
        V._rec.start();
      } else {
        V._rec = null;
      }

      V.recording = true;
      V._tickLevel(opts.onLevel);
      return true;
    }).catch(err => {
      const msg = ({
        NotAllowedError: '麦克风权限被拒绝（Electron 需在系统设置里授权）',
        NotFoundError: '找不到麦克风设备',
        NotReadableError: '麦克风被其他程序占用'
      })[err.name] || ('无法打开麦克风：' + err.message);
      if (opts.onError) opts.onError(msg);
      return false;
    });
  };

  V._tickLevel = function (cb) {
    const step = () => {
      if (!V.recording) { V._level = 0; return; }
      let lv = 0;
      if (V._analyser && V._freq) {
        V._analyser.getByteFrequencyData(V._freq);
        let sum = 0;
        // 低频段对语音更敏感，取前 3/4 就好
        const n = Math.floor(V._freq.length * 0.75);
        for (let i = 0; i < n; i++) { sum += V._freq[i]; }
        lv = Math.min(1, (sum / Math.max(1, n)) / 110);
      }
      V._level = lv;
      if (cb) cb(lv);
      V._levelRaf = global.requestAnimationFrame(step);
    };
    step();
  };

  /** 结束录音，返回 Blob（没有录到则 null）。 */
  V.stopRecording = function () {
    if (!V.recording) { return Promise.resolve(null); }
    V.recording = false;
    if (V._levelRaf) { global.cancelAnimationFrame(V._levelRaf); }

    const rec = V._rec;
    const done = new Promise(resolve => {
      if (!rec) { resolve(null); return; }
      rec.onstop = () => {
        const blob = V._chunks.length
          ? new Blob(V._chunks, { type: V._mime || 'audio/webm' }) : null;
        resolve(blob);
      };
      try { rec.stop(); } catch (_) { resolve(null); }
    });

    // 释放设备，否则托盘常驻会一直占着麦克风指示灯
    done.then(() => {
      if (V._stream) { V._stream.getTracks().forEach(t => t.stop()); V._stream = null; }
      if (V._actx) { try { V._actx.close(); } catch (_) {} V._actx = null; }
      V._analyser = null; V._rec = null; V._chunks = [];
    });
    return done;
  };

  /** 上传音频转写。 */
  V.transcribe = function (blob) {
    if (!blob) { return Promise.reject(new Error('没有录到音频')); }
    return fetch('/api/voice/transcribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream',
                 'X-Audio-Mime': blob.type || 'audio/webm' },
      body: blob
    }).then(async r => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const e = new Error(data.error || ('转写失败 ' + r.status));
        e.code = data.code || '';
        throw e;
      }
      return data;
    });
  };

  // ---------------------------------------------------------- 浏览器识别（备用通道）

  V.startBrowserAsr = function (opts) {
    opts = opts || {};
    if (!SR) {
      if (opts.onError) opts.onError('当前环境不支持浏览器语音识别');
      return false;
    }
    V.stopSpeak();
    const rec = new SR();
    rec.lang = V.lang;
    rec.continuous = true;
    rec.interimResults = true;
    rec.maxAlternatives = 1;
    let finalText = '';
    rec.onresult = (ev) => {
      let interim = '';
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const r = ev.results[i];
        if (r.isFinal) { finalText += r[0].transcript; } else { interim += r[0].transcript; }
      }
      if (opts.onPartial) opts.onPartial((finalText + interim).trim());
    };
    rec.onerror = (ev) => {
      const msg = ({ 'not-allowed': '麦克风权限被拒绝', 'no-speech': '没听到说话',
                     'audio-capture': '找不到麦克风', 'network': '识别服务不可达（需联网）'
                   })[ev.error] || ('识别失败：' + ev.error);
      V.srListening = false;
      if (opts.onError) opts.onError(msg);
    };
    rec.onend = () => {
      V.srListening = false;
      const t = finalText.trim();
      if (t && opts.onFinal) opts.onFinal(t); else if (opts.onEnd) opts.onEnd();
      V.srRec = null;
    };
    try { rec.start(); } catch (e) {
      if (opts.onError) opts.onError('无法启动识别：' + e.message);
      return false;
    }
    V.srRec = rec; V.srListening = true;
    return true;
  };

  V.stopBrowserAsr = function () {
    if (V.srRec) { try { V.srRec.stop(); } catch (_) {} }
    V.srListening = false;
  };

  // ---------------------------------------------------------- 朗读

  function pickVoice(name) {
    if (!SS) { return null; }
    const voices = SS.getVoices() || [];
    if (!voices.length) { return null; }
    if (name) {
      const hit = voices.find(v => v.name === name || v.name.indexOf(name) >= 0);
      if (hit) { return hit; }
    }
    return voices.find(v => /zh|Chinese|中文/i.test(v.lang + ' ' + v.name)) || voices[0];
  }

  V.listVoices = function () {
    return SS ? (SS.getVoices() || []).map(v => ({ name: v.name, lang: v.lang })) : [];
  };

  /** 朗读：云端优先，失败自动回退浏览器本地。 */
  V.speak = function (text, opts) {
    opts = opts || {};
    if (!text) { return Promise.resolve(false); }
    const cloudOn = !!(V._cloud && V._cloud.tts_enabled) && opts.allowCloud !== false;

    if (cloudOn) {
      return api.post('/api/voice/speak', { text: String(text).slice(0, 2000),
                                            voice: opts.voice || '' })
        .then(() => true)          // 云端合成由后端流式返回，前端另行播放
        .catch(() => V._speakLocal(text, opts));
    }
    return Promise.resolve(V._speakLocal(text, opts));
  };

  V._speakLocal = function (text, opts) {
    if (!SS) { return false; }
    try { SS.cancel(); } catch (_) {}
    const u = new SpeechSynthesisUtterance(String(text));
    u.lang = opts.lang || V.lang;
    u.rate = opts.rate || 1.0;
    u.pitch = opts.pitch || 1.0;
    const v = pickVoice(opts.voice);
    if (v) { u.voice = v; }
    if (opts.onEnd) { u.onend = opts.onEnd; }
    SS.speak(u);
    return true;
  };

  V.stopSpeak = function () {
    if (SS) { try { SS.cancel(); } catch (_) {} }
  };

  V.speaking = function () { return !!(SS && SS.speaking); };

  // ---------------------------------------------------------- 语音球（Voice Orb）

  /**
   * 绑定 canvas 并启动渲染循环。音量驱动球体呼吸 + 环形粒子，
   * 灵感来自同类桌面 Agent 的语音球，但这里用**真实频谱**驱动，不是假动画。
   */
  V.bindOrb = function (canvas) {
    if (!canvas) { return; }
    V._orb = canvas;
    const draw = () => {
      V._orbRaf = global.requestAnimationFrame(draw);
      const dpr = global.devicePixelRatio || 1;
      const r = canvas.getBoundingClientRect();
      const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
      if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
        canvas.width = w * dpr; canvas.height = h * dpr;
      }
      const c = canvas.getContext('2d');
      c.setTransform(dpr, 0, 0, dpr, 0, 0);
      c.clearRect(0, 0, w, h);

      const cx = w / 2, cy = h / 2;
      const base = Math.min(w, h) * 0.22;
      const t = Date.now() / 1000;

      // 频谱
      let spectrum = null;
      if (V._analyser && V._freq) {
        V._analyser.getByteFrequencyData(V._freq);
        spectrum = V._freq;
      }
      const level = V.recording ? V._level : 0;

      // 外层光晕
      const glow = c.createRadialGradient(cx, cy, base * 0.4, cx, cy, base * 2.6);
      glow.addColorStop(0, 'rgba(55,230,255,' + (0.20 + level * 0.35) + ')');
      glow.addColorStop(0.5, 'rgba(139,92,255,' + (0.10 + level * 0.22) + ')');
      glow.addColorStop(1, 'rgba(4,5,10,0)');
      c.fillStyle = glow;
      c.beginPath(); c.arc(cx, cy, base * 2.6, 0, 6.2832); c.fill();

      // 频谱环：每根条对应一个频点
      const bars = 72;
      for (let i = 0; i < bars; i++) {
        const a = (i / bars) * 6.2832 - Math.PI / 2;
        let amp = 0.12;
        if (spectrum) {
          const idx = Math.floor(i / bars * spectrum.length * 0.75);
          amp = 0.10 + (spectrum[idx] / 255) * 0.95;
        } else {
          amp = 0.12 + Math.abs(Math.sin(t * 1.6 + i * 0.28)) * 0.10;
        }
        const r0 = base * 1.16;
        const r1 = r0 + base * 0.55 * amp;
        const col = i % 3 === 0 ? 'rgba(139,92,255,.85)' : 'rgba(55,230,255,.85)';
        c.strokeStyle = col;
        c.lineWidth = 2;
        c.lineCap = 'round';
        c.beginPath();
        c.moveTo(cx + Math.cos(a) * r0, cy + Math.sin(a) * r0);
        c.lineTo(cx + Math.cos(a) * r1, cy + Math.sin(a) * r1);
        c.stroke();
      }

      // 核心球：随音量呼吸
      const pulse = base * (1 + level * 0.42 + Math.sin(t * 2.2) * 0.03);
      const core = c.createRadialGradient(cx, cy, 0, cx, cy, pulse);
      core.addColorStop(0, 'rgba(255,255,255,' + (0.85 + level * 0.15) + ')');
      core.addColorStop(0.35, 'rgba(55,230,255,.9)');
      core.addColorStop(1, 'rgba(139,92,255,.15)');
      c.fillStyle = core;
      c.beginPath(); c.arc(cx, cy, pulse, 0, 6.2832); c.fill();

      // 内环（剑痕）
      c.strokeStyle = 'rgba(255,59,92,.75)';
      c.lineWidth = 2.4; c.lineCap = 'round';
      c.beginPath();
      c.moveTo(cx - pulse * 0.62, cy);
      c.lineTo(cx + pulse * 0.62, cy);
      c.stroke();
    };
    draw();
  };

  V.unbindOrb = function () {
    if (V._orbRaf) { global.cancelAnimationFrame(V._orbRaf); V._orbRaf = 0; }
    V._orb = null;
  };

  BW.voice = V;

})(window);
