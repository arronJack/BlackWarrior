/* ============================================================
   app.js — 黑武士主控（渲染进程）
   - 启动序列 → 主界面
   - SSE 实时事件驱动（思考流 / 增量回复 / 工具活动）
   - 五个视图：对话 / 记忆 / 认知 / 活动 / 设置
   - 未接入模型时弹出「接入大脑」引导
   ============================================================ */
(function (global) {
  'use strict';

  const BW = global.BW;
  const api = BW.api, fmt = BW.fmt, viz = BW.viz;
  const $ = (id) => document.getElementById(id);

  const S = {
    view: 'chat',
    running: false,
    activated: false,
    status: null,
    settings: null,
    cog: null,
    memories: [],
    pending: null,      // {turn_id, el, text, resolve}
    feed: [],
    evtFeed: [],
    beatTimer: null,
    graph: null,
    beat: null,
    star: null,
    toolPills: {},
    voice: { tts: false, asr: false, recording: false }
  };

  // ============================================================ 启动序列
  const BOOT_LINES = [
    '初始化数据根目录 …',
    '挂载 SQLite（WAL）…',
    '探测 PASM 认知内核 …',
    '构建工具注册表 …',
    '启动持续运行主循环 …',
    '绑定 HTTP / SSE 服务 …'
  ];

  function boot() {
    const log = $('bootLog'), bar = $('bootBar');
    let i = 0;
    const step = () => {
      if (i < BOOT_LINES.length) {
        const line = BOOT_LINES[i];
        log.innerHTML += '<div>&gt; ' + line + ' <b>OK</b></div>';
        log.scrollTop = log.scrollHeight;
        bar.style.width = Math.round((i + 1) / BOOT_LINES.length * 100) + '%';
        i++;
        setTimeout(step, 130 + Math.random() * 110);
        return;
      }
      setTimeout(finish, 260);
    };
    const finish = () => {
      log.innerHTML += '<div>&gt; 黑武士已就位。</div>';
      $('boot').classList.add('done');
      $('app').classList.remove('hidden');
      setTimeout(() => { $('boot').style.display = 'none'; }, 700);
      start();
    };
    step();
  }

  // ============================================================ 主入口
  function start() {
    S.star = new viz.Starfield($('stars'), 130);
    S.star.run();
    S.graph = new viz.MemoryGraph($('memGraph'));
    S.beat = new viz.BeatLine($('beat'));

    bindNav();
    bindChat();
    bindMemory();
    bindSettings();
    bindActivate();
    bindPower();
    bindVoice();

    // 先拿配置（语音开关来自它），再连事件流
    api.settings().then(cfg => {
      S.settings = cfg;
      applySpeechConfig(cfg);
    }).catch(() => {});

    connectSSE();
    loadHistory();      // 刷新页面不该"失忆"
    refresh();
    setInterval(refresh, 5000);
    setInterval(() => { S.beat.frame(); }, 60);
    global.addEventListener('resize', () => redrawAll());
  }

  function redrawAll() {
    if (!S.cog) { return; }
    viz.drawMood($('moodChart'), (S.cog.affect || {}).mood_curve || []);
    viz.drawGauge($('errGauge'), (S.cog.affect || {}).prediction_error || 0,
                  (S.cog.affect || {}).avg_prediction_error);
    viz.drawRetention($('retentionChart'), S.memories);
  }

  // ============================================================ 导航
  function bindNav() {
    document.querySelectorAll('.nav-btn[data-view]').forEach(btn => {
      btn.addEventListener('click', () => {
        const v = btn.getAttribute('data-view');
        document.querySelectorAll('.nav-btn[data-view]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        document.querySelectorAll('.view').forEach(s => s.classList.remove('active'));
        $('view-' + v).classList.add('active');
        S.view = v;
        if (v === 'memory') { loadMemories(); }
        if (v === 'cognition') { loadCognition(); }
        if (v === 'activity') { loadActivity(); }
        if (v === 'settings') { loadSettings(); }
      });
    });
  }

  // ============================================================ SSE
  function connectSSE() {
    S.stream = new BW.EventStream({
      onState: (st) => {
        const chip = $('chipLink');
        chip.classList.toggle('on', st === 'open');
        chip.classList.toggle('off', st !== 'open');
        chip.innerHTML = '<i></i>' + (st === 'open' ? '链路' : '重连中');
      },
      onEvent: onEvent
    });
    S.stream.start();
  }

  function onEvent(type, p) {
    p = p || {};
    // 事件流面板
    pushEvt(type, p);

    switch (type) {
      case 'turn_begin':
      case 'turn_started':
        if (p.turn_id && S.pending && p.turn_id === S.pending.turn_id) { /* 自己的轮次 */ }
        setThinking('处理中：' + (p.label || '推理'));
        break;

      case 'thinking':
        pushFeed(p.text || (p.payload && p.payload.text) || '思考…', 'cog');
        if (p.text) { setThinking(String(p.text).slice(0, 90)); }
        break;

      case 'prediction':
        pushFeed('预测误差 ' + fmt.num(p.prediction_error || p.error) +
                 ' · 好奇心 ' + fmt.num(p.curiosity), 'cog');
        break;

      case 'tool_call':
        addToolPill(p.name, 'run');
        pushFeed('调用工具 ' + p.name, 'tool');
        break;

      case 'tool_result':
        setToolPill(p.name, p.ok === false ? 'err' : 'ok');
        pushFeed('工具 ' + p.name + ' → ' +
                 (p.ok === false ? '失败' : '完成') + ' ' + fmt.num((p.duration_ms || 0) / 1000, 2) + 's',
                 p.ok === false ? 'err' : 'tool');
        break;

      case 'reply_delta':
        if (S.pending && (!p.turn_id || p.turn_id === S.pending.turn_id)) {
          S.pending.text += (p.text || '');
          renderBubble(S.pending.el, S.pending.text, true);
          scrollDown();
        }
        break;

      case 'reply':
        if (S.pending && (!p.turn_id || p.turn_id === S.pending.turn_id)) {
          finishPending(p.text || S.pending.text, p.offline);
        } else if (!S.pending) {
          // 自主 TICK 或外部渠道产生的回复
          addMsg('agent', p.text || '', p.offline ? '自省' : '');
        }
        break;

      case 'turn_end':
      case 'turn_finished':
        if (S.pending) { finishPending(S.pending.text, false); }
        setThinking('待机');
        clearToolPills();
        break;

      case 'tick_skipped':
        setThinking('待机（自主 TICK 跳过）');
        break;

      case 'consolidated':
        toast('记忆巩固完成');
        loadMemories();
        break;

      case 'tools_reloaded':
        if (S.view === 'activity') { loadActivity(); }
        break;

      case 'cognition':
        loadCognition();
        break;

      case 'error':
        pushFeed('错误：' + (p.message || p.error || ''), 'err');
        break;

      case 'activation_required':
        S.activated = false;
        break;

      case 'activation_required_cleared':
        S.activated = true;
        break;
    }
  }

  // ============================================================ 对话
  // ============================================================ 历史会话
  function loadHistory() {
    api.history(50).then(items => {
      const box = $('stream');
      box.innerHTML = '';
      (items || []).forEach(m => {
        const role = m.role === 'user' ? 'user' : (m.role === 'system' ? 'system' : 'agent');
        // 自主 TICK 的产出单独渲染成低打扰样式，不混在对话里
        if (String(m.content || '').indexOf('[自主 TICK]') === 0) {
          const wrap = document.createElement('div');
          wrap.className = 'msg tick';
          const b = document.createElement('div');
          b.className = 'bubble';
          b.textContent = String(m.content).replace('[自主 TICK]', '自省 ·');
          wrap.appendChild(b);
          box.appendChild(wrap);
          return;
        }
        addMsg(role, m.content || '', '');
      });
      if (!(items || []).length) {
        const wrap = document.createElement('div');
        wrap.className = 'msg system';
        const b = document.createElement('div');
        b.className = 'bubble';
        b.textContent = '黑武士已就位。它会在空闲时自己思考，也会记住你说过的话。';
        wrap.appendChild(b);
        box.appendChild(wrap);
      }
      scrollDown();
    }).catch(() => {});
  }

  function bindChat() {
    const form = $('composer'), input = $('input');
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(168, input.scrollHeight) + 'px';
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
    });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const text = input.value.trim();
      if (!text) { return; }
      input.value = ''; input.style.height = 'auto';
      send(text);
    });
    $('btnClear').addEventListener('click', () => { $('stream').innerHTML = ''; });
  }

  function send(text) {
    addMsg('user', text);
    scrollDown();

    const el = addMsg('agent', '', '');
    S.pending = { turn_id: null, el: el, text: '', started: Date.now(), done: false };
    setThinking('排队中…');
    renderBubble(el, '', true);

    api.message(text).then(r => {
      if (r && r.message && r.message.turn_id) { S.pending.turn_id = r.message.turn_id; }
      setThinking('处理中…');
      // 兜底：SSE 若异常，15s 后仍未收到 turn_end 就走同步接口取结果
      setTimeout(() => {
        if (!S.pending || S.pending.done || S.pending.text) { return; }
        fallbackAsk(text, el);
      }, 15000);
    }).catch(err => {
      finishPending('（发送失败）' + err.message, true);
    });
  }

  function fallbackAsk(text, el) {
    api.ask(text, 120).then(r => {
      if (!S.pending || S.pending.done) { return; }
      finishPending((r && r.reply) || '（无回复）', false);
    }).catch(err => {
      if (!S.pending || S.pending.done) { return; }
      finishPending('（内核无响应）' + err.message, true);
    });
  }

  function finishPending(text, offline) {
    if (!S.pending || S.pending.done) { return; }
    S.pending.done = true;
    renderBubble(S.pending.el, text || '（空回复）', false);
    if (offline) {
      const tag = S.pending.el.querySelector('.tag');
      if (tag) { tag.textContent = '本地认知内核'; }
    }
    S.pending = null;
    setThinking('待机');
    clearToolPills();
    scrollDown();
    speakReply(text);
  }

  function addMsg(role, text, tag) {
    const wrap = document.createElement('div');
    wrap.className = 'msg ' + (role === 'user' ? 'user' : (role === 'system' ? 'system' : 'agent'));
    const av = document.createElement('div');
    av.className = 'av';
    av.textContent = role === 'user' ? '你' : 'BW';
    const b = document.createElement('div');
    b.className = 'bubble';
    if (tag) { const t = document.createElement('div'); t.className = 'tag'; t.textContent = tag; b.appendChild(t); }
    const body = document.createElement('span');
    body.className = 'body';
    body.textContent = text || '';
    b.appendChild(body);
    wrap.appendChild(av); wrap.appendChild(b);
    $('stream').appendChild(wrap);
    scrollDown();
    return b;
  }

  function renderBubble(bubble, text, streaming) {
    if (!bubble) { return; }
    let body = bubble.querySelector('.body');
    if (!body) {
      body = document.createElement('span');
      body.className = 'body';
      bubble.appendChild(body);
    }
    body.textContent = text || '';
    let cur = bubble.querySelector('.cursor');
    if (streaming) {
      if (!cur) { cur = document.createElement('i'); cur.className = 'cursor'; bubble.appendChild(cur); }
    } else if (cur) { cur.remove(); }
  }

  function scrollDown() {
    const s = $('stream');
    s.scrollTop = s.scrollHeight;
  }

  function setThinking(txt) {
    $('thinkText').textContent = txt || '待机';
    $('thinkbar').querySelector('.pulse').classList.toggle('on', !!txt && txt !== '待机');
  }

  function addToolPill(name, state) {
    S.toolPills[name] = state;
    paintToolPills();
  }
  function setToolPill(name, state) {
    if (!(name in S.toolPills)) { S.toolPills[name] = state; }
    else { S.toolPills[name] = state; }
    paintToolPills();
  }
  function clearToolPills() {
    setTimeout(() => { S.toolPills = {}; paintToolPills(); }, 2500);
  }
  function paintToolPills() {
    const box = $('thinkTools');
    box.innerHTML = '';
    Object.keys(S.toolPills).forEach(n => {
      const s = document.createElement('span');
      s.className = 'tpill ' + S.toolPills[n];
      s.textContent = n;
      box.appendChild(s);
    });
    // HUD 工具活动
    const ta = $('toolAct');
    ta.innerHTML = '';
    Object.keys(S.toolPills).slice(-6).forEach(n => {
      const d = document.createElement('div');
      d.className = 'ta-item ' + (S.toolPills[n] === 'err' ? 'bad' : 'ok');
      d.innerHTML = '<span>' + fmt.esc(n) + '</span><b>' +
        (S.toolPills[n] === 'run' ? '···' : (S.toolPills[n] === 'err' ? 'FAIL' : 'OK')) + '</b>';
      ta.appendChild(d);
    });
  }

  function pushFeed(text, kind) {
    const box = $('thinkFeed');
    const d = document.createElement('div');
    d.className = 'tf-item ' + (kind || '');
    d.textContent = text;
    box.appendChild(d);
    while (box.children.length > 60) { box.removeChild(box.firstChild); }
    box.scrollTop = box.scrollHeight;
  }

  function pushEvt(type, p) {
    const box = $('evtList');
    if (!box) { return; }
    const d = document.createElement('div');
    d.className = 'evt-item';
    const t = document.createElement('span'); t.className = 'ty'; t.textContent = type;
    const l = document.createElement('span'); l.className = 'pl';
    let brief = '';
    try {
      brief = JSON.stringify(p).slice(0, 120);
    } catch (_) { brief = ''; }
    l.textContent = brief;
    d.appendChild(t); d.appendChild(l);
    box.insertBefore(d, box.firstChild);
    while (box.children.length > 80) { box.removeChild(box.lastChild); }
  }

  // ============================================================ 数据刷新
  function refresh() {
    api.status().then(st => {
      S.status = st;
      S.running = !!(st && st.running);
      S.activated = !!(st && st.activated);

      $('hRun').textContent = S.running ? '运行中' : '已停止';
      $('hRun').className = S.running ? 'on' : 'off';
      $('hQueue').textContent = ((st.loop || {}).queue_size || 0);
      $('hMem').textContent = ((st.memory || {}).count || 0);
      $('hUptime').textContent = fmt.dur(st.uptime || 0);

      const nx = (st.loop || {}).next_tick_in;
      $('hNext').textContent = (nx === undefined || nx === null) ? '—' : fmt.dur(nx);

      $('chipTier').textContent = '档位 ' + (((st.cognition || {}).tier) || '—');
      $('chipTier2').textContent = '档位 ' + (((st.cognition || {}).tier) || '—');
      $('chipEngine').textContent = '引擎 ' + (((st.cognition || {}).engine) || '—');
      const modelName = (S.settings && S.settings.model) || '';
      $('chipModel').textContent = '模型 ' +
        (S.activated ? (modelName || '已连接') : '未接入');
      $('chatSub').textContent = S.running
        ? ('持续运行 · ' + (((st.cognition || {}).tier) || '') + ' 内核 · 工具 ' + (((st.tools || {}).count) || 0))
        : '主循环已停止';

      const btn = $('btnPower');
      btn.classList.toggle('on', S.running);
      btn.classList.toggle('off', !S.running);

      S.beat.push(S.running ? 0.5 + Math.random() * 0.5 : 0.08);

      if (!S.activated && !sessionStorage.getItem('bw_skip_activate')) {
        showActivate();
      }
      if (S.view === 'cognition') { loadCognition(); }
    }).catch(() => {
      $('hRun').textContent = '离线'; $('hRun').className = 'off';
    });

    if (S.view === 'memory') { loadMemories(); }
  }

  // ============================================================ 记忆页
  function bindMemory() {
    $('btnConsolidate').addEventListener('click', () => {
      api.consolidate().then(r => {
        const rep = r && r.report ? r.report : {};
        toast('巩固完成：' + (rep.merged || rep.clusters || 0) + ' 组');
        loadMemories();
      }).catch(e => toast(e.message, true));
    });
    $('btnMemClear').addEventListener('click', () => {
      if (!confirm('确认清空全部结构化记忆？认知内核侧车索引不受影响。')) { return; }
      api.resetMem().then(() => { toast('已清空'); loadMemories(); })
        .catch(e => toast(e.message, true));
    });
    let t = null;
    $('memSearch').addEventListener('input', (e) => {
      clearTimeout(t);
      const q = e.target.value.trim();
      t = setTimeout(() => { q ? searchMem(q) : loadMemories(); }, 280);
    });
  }

  function loadMemories() {
    api.memories(120).then(r => {
      S.memories = (r && r.items) ? r.items : [];
      renderMemories(S.memories);
      S.graph.setData(S.memories);
      viz.drawRetention($('retentionChart'), S.memories);
    }).catch(() => {});
  }

  function searchMem(q) {
    api.search(q, 20).then(r => {
      const items = (r && r.items) ? r.items : [];
      renderMemories(items, true);
    }).catch(() => {});
  }

  function renderMemories(items, searched) {
    const box = $('memList');
    box.innerHTML = '';
    if (!items.length) {
      box.innerHTML = '<div class="empty">' +
        (searched ? '没有命中相关记忆' : '记忆库为空——和黑武士聊聊，它会记住你。') + '</div>';
      return;
    }
    items.forEach(m => {
      const rt = Number(m.retention === undefined ? 1 : m.retention);
      const card = document.createElement('div');
      card.className = 'mem-card';
      const left = document.createElement('div');
      left.innerHTML =
        '<div class="t">' + fmt.esc(m.title) + '</div>' +
        (m.brief ? '<div class="b">' + fmt.esc(m.brief) + '</div>' : '') +
        '<div class="m"><span class="cat">' + fmt.esc(m.category || '日常') + '</span>' +
        '<span>显著度 ' + (m.salience || 1) + '</span>' +
        '<span>命中 ' + (m.hits || 0) + '</span>' +
        '<span>' + fmt.ago(m.ts) + '</span></div>';
      const right = document.createElement('div');
      right.className = 'rt';
      right.innerHTML =
        '<span class="rtbar"><i style="width:' + Math.round(rt * 100) + '%"></i></span>' +
        '<span class="rtnum">' + Math.round(rt * 100) + '%</span>';
      const del = document.createElement('button');
      del.className = 'delbtn';
      del.title = '删除';
      del.innerHTML = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';
      del.addEventListener('click', () => {
        api.delMemory(m.id).then(() => { toast('已删除'); loadMemories(); })
          .catch(e => toast(e.message, true));
      });
      right.appendChild(del);
      card.appendChild(left); card.appendChild(right);
      box.appendChild(card);
    });
  }

  // ============================================================ 认知页
  function loadCognition() {
    api.cognition().then(r => {
      S.cog = r || {};
      const cog = S.cog.cognition || {}, aff = S.cog.affect || {};

      const mood = Number(aff.mood || cog.mood || 0);
      $('moodNow').textContent = (mood >= 0 ? '+' : '') + fmt.num(mood);
      $('moodLabel').textContent = mood > 0.25 ? '积极' : (mood < -0.25 ? '低落' : '平稳');

      viz.drawMood($('moodChart'), aff.mood_curve || []);
      viz.drawGauge($('errGauge'), aff.prediction_error || 0, aff.avg_prediction_error);
      $('errVal').textContent = fmt.num(aff.prediction_error || 0);
      $('errAvg').textContent = fmt.num(aff.avg_prediction_error || 0);
      $('curVal').textContent = fmt.num(aff.curiosity || 0);
      const ts = (cog.tick_scale !== undefined ? cog.tick_scale : null);
      $('tickScale').textContent = (ts === null ? '—' : fmt.num(ts) + '×');

      // 情绪分解条
      const bars = $('moodBars');
      const rows = [
        ['情绪值', mood, '#37e6ff'],
        ['预测误差', Number(aff.prediction_error || 0), '#ffc44d'],
        ['好奇心', Number(aff.curiosity || 0), '#8b5cff']
      ];
      bars.innerHTML = '';
      rows.forEach(r2 => {
        const pct = Math.max(0, Math.min(1, Math.abs(r2[1]))) * 100;
        const d = document.createElement('div');
        d.className = 'bar-row';
        d.innerHTML = '<span>' + r2[0] + '</span>' +
          '<span class="bar-track"><i style="width:' + pct + '%;background:' + r2[2] + '"></i></span>' +
          '<b>' + fmt.num(r2[1]) + '</b>';
        bars.appendChild(d);
      });

      // 焦点栈
      const fl = $('focusList');
      fl.innerHTML = '';
      const focus = cog.focus || [];
      if (!focus.length) {
        fl.innerHTML = '<div class="empty">焦点栈为空</div>';
      } else {
        focus.slice().reverse().slice(0, 12).forEach(f => {
          const d = document.createElement('div');
          d.className = 'focus-item';
          const topic = typeof f === 'string' ? f : (f.topic || f.text || JSON.stringify(f));
          const w = typeof f === 'object' && f.weight !== undefined ? fmt.num(f.weight) : '';
          d.innerHTML = '<span>' + fmt.esc(String(topic)) + '</span><span class="w">' + w + '</span>';
          fl.appendChild(d);
        });
      }

      $('cogJson').textContent = JSON.stringify(S.cog, null, 2);
    }).catch(() => {});
  }

  // ============================================================ 活动页
  function loadActivity() {
    api.tools().then(r => {
      const box = $('toolList');
      box.innerHTML = '';
      const items = (r && r.items) ? r.items : [];
      if (!items.length) { box.innerHTML = '<div class="empty">未注册工具</div>'; return; }
      items.forEach(t => {
        const d = document.createElement('div');
        d.className = 'tool-item ' + (t.risk || 'safe');
        d.innerHTML = '<div class="n">' + fmt.esc(t.name) + '</div>' +
                      '<div class="d">' + fmt.esc(t.description || '') + '</div>';
        box.appendChild(d);
      });
    }).catch(() => {});

    api.actions().then(r => {
      const box = $('actList');
      box.innerHTML = '';
      const items = (r && r.items) ? r.items : [];
      if (!items.length) { box.innerHTML = '<div class="empty">暂无行动记录</div>'; return; }
      items.forEach(a => {
        const d = document.createElement('div');
        d.className = 'act-item' + (a.ok ? '' : ' bad');
        d.innerHTML = '<span class="nm">' + fmt.esc(a.tool) + '</span>' +
                      '<span>' + fmt.esc(a.summary || '') + '</span>';
        box.appendChild(d);
      });
    }).catch(() => {});

    api.events(60).then(r => {
      const box = $('evtList');
      box.innerHTML = '';
      ((r && r.items) ? r.items : []).slice().reverse().forEach(e => {
        pushEvt(e.type, e);
      });
    }).catch(() => {});
  }

  // ============================================================ 设置页
  function bindSettings() {
    $('btnSave').addEventListener('click', saveSettings);
    $('btnPing').addEventListener('click', () => {
      api.ping().then(r => {
        toast(r && r.ok ? ('连通 · ' + (r.model || '')) : ('失败：' + ((r && r.error) || '未知')));
      }).catch(e => toast(e.message, true));
    });
    $('btnReloadTools').addEventListener('click', () => {
      api.tools().then(() => { toast('工具已重载'); loadActivity(); });
    });
  }

  let PROVIDERS = [];
  /**
   * 填充供应商下拉。
   *
   * 字段名必须跟后端 `Provider.as_dict()` 对齐：那里是 **key**，不是 id。
   * 这里如果写成 `p.id`，value 会退化成中文名（如 "DeepSeek"），
   * 保存后 provider 查表失败 —— 表现是"填了 Key 却一直连不上"，极难排查。
   */
  function fillProviders(sel, current) {
    sel.innerHTML = '';
    PROVIDERS.forEach(p => {
      const o = document.createElement('option');
      o.value = p.key;
      o.textContent = (p.name || p.key) + (p.requires_key === false ? '（可离线）' : '');
      sel.appendChild(o);
    });
    if (current) { sel.value = current; }
  }

  function loadSettings() {
    Promise.all([api.settings(), api.providers()]).then(([cfg, pv]) => {
      PROVIDERS = (pv && pv.items) ? pv.items : [];
      fillProviders($('setProvider'), cfg.provider);
      fillProviders($('actProvider'), cfg.provider);

      // 选中模型的建议：切换供应商时给个默认值，省得用户去查文档
      $('setProvider').onchange = () => {
        const p = PROVIDERS.find(x => x.key === $('setProvider').value);
        if (p && p.default_model) { $('setModel').value = p.default_model; }
      };

      $('setModel').value = cfg.model || '';
      $('setKey').value = '';
      $('setBase').value = cfg.base_url || '';
      $('setName').value = cfg.agent_name || '黑武士';

      // agent_persona 是对象；UI 只暴露最有用的 tone 字段，回填时保留其余字段
      const persona = cfg.agent_persona || {};
      $('setPersona').value = persona.tone || '';

      $('setTick').value = cfg.tick_interval || 60;
      $('setAwakeInt').value = cfg.awakening_interval || 10;
      $('setHeart').checked = cfg.heartbeat_enabled !== false;

      $('capTools').checked = cfg.tools_enabled !== false;
      $('capDanger').checked = cfg.allow_dangerous_tools === true;
      $('capShell').checked = cfg.shell_enabled !== false;
      $('capWeb').checked = cfg.web_enabled !== false;
      $('capCog').checked = cfg.cognition_enabled !== false;

      $('capVoice').checked = cfg.voice_enabled === true;
      $('capTts').checked = cfg.tts_enabled === true;
      $('capAsr').checked = cfg.asr_enabled === true;
      $('setVoice').value = cfg.tts_voice || '';

      $('setUrl').textContent = location.origin;
      $('setDataDir').textContent = (S.status && S.status.data_dir) || '—';
      paintSpeechState();
    }).catch(() => {});
  }

  function saveSettings() {
    // agent_persona 是对象，只覆盖 tone，其余字段原样保留
    const persona = (S.settings && S.settings.agent_persona) || {};
    persona.tone = $('setPersona').value.trim();

    const patch = {
      provider: $('setProvider').value,
      model: $('setModel').value.trim(),
      base_url: $('setBase').value.trim(),
      agent_name: $('setName').value.trim() || '黑武士',
      agent_persona: persona,
      tick_interval: Number($('setTick').value) || 60,
      awakening_interval: Number($('setAwakeInt').value) || 10,
      heartbeat_enabled: $('setHeart').checked,
      tools_enabled: $('capTools').checked,
      allow_dangerous_tools: $('capDanger').checked,
      shell_enabled: $('capShell').checked,
      web_enabled: $('capWeb').checked,
      cognition_enabled: $('capCog').checked,
      voice_enabled: $('capVoice').checked,
      tts_enabled: $('capTts').checked,
      asr_enabled: $('capAsr').checked,
      tts_voice: $('setVoice').value.trim()
    };
    // 空 model 表示"用该供应商默认模型"，不写空串进配置
    if (!patch.model) { delete patch.model; }
    const k = $('setKey').value.trim();
    if (k) { patch.api_key = k; }

    api.save(patch).then((cfg) => {
      S.settings = cfg || S.settings;
      applySpeechConfig(cfg || patch);
      toast('已保存（心跳/内核类改动重启内核后生效）');
      refresh();
    }).catch(e => toast(e.message, true));
  }

  // ============================================================ 激活
  function bindActivate() {
    $('actSkip').addEventListener('click', () => {
      sessionStorage.setItem('bw_skip_activate', '1');
      $('activate').classList.add('hidden');
    });
    $('actGo').addEventListener('click', () => {
      const msg = $('actMsg');
      msg.className = 'act-msg';
      msg.textContent = '连接中…';
      api.activate({
        provider: $('actProvider').value,
        api_key: $('actKey').value.trim(),
        model: $('actModel').value.trim()
      }).then(r => {
        const ping = r && r.ping ? r.ping : {};
        if (r && r.activated && ping.ok) {
          msg.className = 'act-msg ok';
          msg.textContent = '已接入：' + (ping.model || '');
          setTimeout(() => { $('activate').classList.add('hidden'); refresh(); }, 700);
        } else {
          msg.className = 'act-msg err';
          msg.textContent = '已保存但连通失败：' + ((ping && ping.error) || '请检查 Key / 模型名');
        }
      }).catch(e => {
        msg.className = 'act-msg err';
        msg.textContent = '失败：' + e.message;
      });
    });
  }

  function showActivate() {
    const m = $('activate');
    if (!m.classList.contains('hidden')) { return; }
    if (!PROVIDERS.length) {
      api.providers().then(pv => {
        PROVIDERS = (pv && pv.items) ? pv.items : [];
        fillProviders($('actProvider'), (S.settings || {}).provider);
      }).catch(() => {});
    }
    m.classList.remove('hidden');
  }

  // ============================================================ 语音
  /**
   * 语音链路：ASR 走 Chromium 云端识别（需联网），TTS 走本地合成（离线可用）。
   * 任一环节不可用时必须**显式降级**（置灰 + 说明），不能静默失效。
   */
  function bindVoice() {
    const mic = $('btnMic'), speak = $('btnSpeak');
    const cap = BW.voice.available();

    if (!cap.asr) {
      mic.disabled = true;
      mic.title = cap.asr_note;
    }
    if (!cap.tts) {
      speak.classList.add('off');
      speak.title = '当前环境不支持语音合成';
    }

    mic.addEventListener('click', () => {
      if (S.voice.recording) {
        BW.voice.stopAsr();
        setRecording(false);
        return;
      }
      if (!BW.voice.available().asr) { toast(cap.asr_note, true); return; }
      const input = $('input');
      const before = input.value;
      setRecording(true);
      BW.voice.startAsr({
        onPartial: (t) => { input.value = before ? before + t : t; },
        onFinal: (t) => {
          setRecording(false);
          input.value = before ? (before + t).trim() : t;
          if (input.value.trim()) { $('composer').requestSubmit(); }
        },
        onEnd: () => setRecording(false),
        onError: (msg) => { setRecording(false); toast(msg, true); }
      });
    });

    speak.addEventListener('click', () => {
      const next = !S.voice.tts;
      S.voice.tts = next;
      paintSpeakBtn();
      if (!next) { BW.voice.stopSpeak(); }
      // 顺手把偏好写回配置，下次启动保持
      api.save({ tts_enabled: next }).then(cfg => { S.settings = cfg || S.settings; })
        .catch(() => {});
    });
  }

  function setRecording(on) {
    S.voice.recording = !!on;
    const mic = $('btnMic');
    mic.classList.toggle('rec', !!on);
    mic.title = on ? '正在聆听…点击结束' : '语音输入（浏览器语音识别）';
    if (!on) { return; }
  }

  function applySpeechConfig(cfg) {
    cfg = cfg || {};
    const canAsr = BW.voice.available().asr;
    const canTts = BW.voice.available().tts;
    S.voice.tts = !!cfg.tts_enabled && canTts;
    S.voice.asr = !!cfg.asr_enabled && canAsr;
    if (!canAsr) { $('btnMic').disabled = true; }
    paintSpeakBtn();
  }

  function paintSpeakBtn() {
    const b = $('btnSpeak');
    if (!b) { return; }
    b.classList.toggle('on', !!S.voice.tts);
    b.classList.toggle('off', !S.voice.tts && !BW.voice.available().tts);
  }

  function paintSpeechState() {
    const cap = BW.voice.available();
    const voices = BW.voice.listVoices();
    $('asrState').textContent = cap.asr ? '可用（联网）' : '不可用';
    $('ttsState').textContent = cap.tts
      ? ('可用 · ' + (voices.length ? voices.length + ' 个音色' : '系统默认')) : '不可用';
  }

  /** 回复落定时朗读（受 tts 开关控制）。 */
  function speakReply(text) {
    if (!S.voice.tts || !text) { return; }
    const v = (S.settings || {}).tts_voice || '';
    // 去掉 Markdown 花括号与过长的自省文本，避免念一堆符号
    const clean = String(text)
      .replace(/```[\s\S]*?```/g, '（代码块）')
      .replace(/[`*_#>|]/g, '')
      .slice(0, 400);
    BW.voice.speak(clean, { voice: v, lang: 'zh-CN' });
  }

  // ============================================================ 主循环开关
  function bindPower() {
    $('btnPower').addEventListener('click', () => {
      const fn = S.running ? api.stop() : api.start();
      fn.then(() => { toast(S.running ? '主循环已停止' : '主循环已启动'); refresh(); })
        .catch(e => toast(e.message, true));
    });
  }

  // ============================================================ Toast
  let toastTimer = null;
  function toast(text, isErr) {
    const t = $('toast');
    t.textContent = text;
    t.className = 'toast show' + (isErr ? ' err' : ' ok');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = 'toast' + (isErr ? ' err' : ' ok'); }, 2600);
  }

  // ============================================================ Go
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }

})(window);
