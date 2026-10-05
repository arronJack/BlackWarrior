/* ============================================================
   api.js — 黑武士 HTTP + SSE 客户端
   零依赖。所有请求走同源（桌面壳内就是本机内核服务）。
   ============================================================ */
(function (global) {
  'use strict';

  const BW = global.BW || (global.BW = {});

  // ---------------------------------------------------------- HTTP

  async function req(method, path, body, opts) {
    opts = opts || {};
    const init = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    if (opts.timeout) {
      init.signal = AbortSignal.timeout(opts.timeout);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch (err) {
      throw new Error('无法连接内核：' + (err && err.message ? err.message : err));
    }
    let data = null;
    const txt = await res.text();
    if (txt) {
      try { data = JSON.parse(txt); } catch (_) { data = txt; }
    }
    if (!res.ok) {
      const msg = (data && data.error) ? data.error : ('HTTP ' + res.status);
      throw new Error(msg);
    }
    return data;
  }

  const api = {
    get:    (p, o)        => req('GET', p, null, o),
    post:   (p, b, o)     => req('POST', p, b === undefined ? {} : b, o),
    patch:  (p, b, o)     => req('PATCH', p, b === undefined ? {} : b, o),
    del:    (p, o)        => req('DELETE', p, null, o),

    // ---- 基础
    health:   ()          => api.get('/api/healthz'),
    version:  ()          => api.get('/api/version'),
    status:   ()          => api.get('/api/status'),
    summary:  ()          => api.get('/api/summary'),

    // ---- 对话
    message:  (text)      => api.post('/api/message', { text: text, channel: 'ui' }),
    ask:      (text, t)   => api.post('/api/ask', { text: text, timeout: t || 120 },
                                      { timeout: (t || 120) * 1000 + 8000 }),
    history:  (n)         => api.get('/api/conversations', { timeout: 8000 })
                                .then(d => d && d.items ? d.items : []),

    // ---- 记忆
    memories: (limit, cat)=> api.get('/api/memories?limit=' + (limit || 100) +
                                     (cat ? '&category=' + encodeURIComponent(cat) : '')),
    addMemory:(m)         => api.post('/api/memories', m),
    editMemory:(id, f)    => api.patch('/api/memories/' + id, f),
    delMemory:(id)        => api.del('/api/memories/' + id),
    search:   (q, k)      => api.post('/api/memories/search', { query: q, k: k || 8 }),
    consolidate: ()       => api.post('/api/cognition/consolidate'),

    // ---- 认知
    cognition:()          => api.get('/api/cognition'),
    mood:     (n)         => api.get('/api/cognition/mood?n=' + (n || 80)),

    // ---- 工具 / 活动
    tools:    ()          => api.get('/api/tools'),
    actions:  ()          => api.get('/api/actions'),
    events:   (n)         => api.get('/api/events/recent?n=' + (n || 60)),
    bus:      ()          => api.get('/api/bus'),

    // ---- 设置
    settings: ()          => api.get('/api/settings'),
    save:     (patch)     => api.post('/api/settings', patch),
    providers:()          => api.get('/api/providers'),
    activate: (p)         => api.post('/api/activate', p),
    ping:     ()          => api.post('/api/llm/ping'),

    // ---- 管理
    start:    ()          => api.post('/api/admin/start'),
    stop:     ()          => api.post('/api/admin/stop'),
    resetMem: ()          => api.post('/api/admin/reset-memories'),
    resetConv:()          => api.post('/api/admin/reset-conversations')
  };

  // ---------------------------------------------------------- SSE

  /**
   * 内核发出的事件类型。
   * SSE 帧带 ``event: <type>``，浏览器只在 ``addEventListener(type)`` 上回调，
   * ``onmessage`` 收不到具名事件——所以这里必须穷举注册，漏一个就等于丢一类事件。
   */
  const EVENT_TYPES = [
    'message', 'core_started', 'core_stopped', 'loop_started', 'loop_stopped',
    'message_queued', 'turn_begin', 'turn_started', 'turn_end', 'turn_finished',
    'thinking', 'reply', 'reply_delta', 'tool_call', 'tool_result', 'tool_event',
    'prediction', 'cognition', 'consolidated', 'context_built', 'quota',
    'processing_preempted', 'tick_skipped', 'tools_reloaded', 'error',
    'activation_required', 'activation_required_cleared', 'reminder', 'memory'
  ];

  /**
   * 事件总线订阅。断线自动重连，并用 last_id 补齐遗漏事件。
   * onEvent(type, payload, raw)
   * onState('open' | 'closed' | 'error')
   */
  function EventStream(opts) {
    opts = opts || {};
    this.onEvent = opts.onEvent || function () {};
    this.onState = opts.onState || function () {};
    this.url = opts.url || '/events';
    this.es = null;
    this.lastId = 0;
    this.retry = 0;
    this.closed = false;
    this.timer = null;
  }

  EventStream.prototype._dispatch = function (data, fallbackType) {
    let obj = null;
    try { obj = JSON.parse(data); } catch (_) { obj = null; }
    if (!obj || typeof obj !== 'object') { obj = { type: fallbackType || 'raw', text: data }; }
    const id = Number(obj.id || 0);
    if (id) { this.lastId = id; }
    this.onEvent(obj.type || fallbackType || 'message', obj, obj);
  };

  EventStream.prototype.start = function () {
    if (this.es) { try { this.es.close(); } catch (_) {} }
    this.closed = false;
    let url = this.url;
    if (this.lastId) {
      url += (url.indexOf('?') < 0 ? '?' : '&') + 'last_id=' + this.lastId;
    }

    let es;
    try {
      es = new EventSource(url);
    } catch (err) {
      this.onState('error');
      this._schedule();
      return;
    }
    this.es = es;

    es.onopen = () => { this.retry = 0; this.onState('open'); };

    // 具名事件
    const self = this;
    EVENT_TYPES.forEach(function (t) {
      es.addEventListener(t, function (ev) { self._dispatch(ev.data, t); });
    });
    // 兜底：无名帧（event: message 或裸 data）
    es.onmessage = function (ev) { self._dispatch(ev.data, 'message'); };

    es.onerror = function () {
      self.onState('closed');
      try { es.close(); } catch (_) {}
      self.es = null;
      self._schedule();
    };
  };

  EventStream.prototype._schedule = function () {
    if (this.closed || this.timer) { return; }
    this.retry = Math.min(this.retry + 1, 10);
    const wait = Math.min(1000 * this.retry, 8000);
    this.timer = setTimeout(() => { this.timer = null; this.start(); }, wait);
  };

  EventStream.prototype.stop = function () {
    this.closed = true;
    if (this.timer) { clearTimeout(this.timer); this.timer = null; }
    if (this.es) { try { this.es.close(); } catch (_) {} this.es = null; }
  };

  // ---------------------------------------------------------- 工具

  const fmt = {
    time: (ts) => {
      if (!ts) { return '--:--'; }
      const d = new Date(ts * 1000);
      return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    },
    ago: (ts) => {
      if (!ts) { return '—'; }
      const s = Math.max(0, Date.now() / 1000 - ts);
      if (s < 60) { return Math.floor(s) + ' 秒前'; }
      if (s < 3600) { return Math.floor(s / 60) + ' 分钟前'; }
      if (s < 86400) { return Math.floor(s / 3600) + ' 小时前'; }
      return Math.floor(s / 86400) + ' 天前';
    },
    dur: (sec) => {
      sec = Math.max(0, Math.floor(sec || 0));
      const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
      if (h) { return h + 'h' + String(m).padStart(2, '0') + 'm'; }
      if (m) { return m + 'm' + String(s).padStart(2, '0') + 's'; }
      return s + 's';
    },
    num: (v, n) => Number(v || 0).toFixed(n === undefined ? 2 : n),
    esc: (s) => String(s === undefined || s === null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  };

  BW.api = api;
  BW.EventStream = EventStream;
  BW.fmt = fmt;

})(window);
