/* ============================================================
   viz.js — 黑武士 Canvas 可视化
   深空星野 / 情绪曲线 / 预测误差仪表 / 记忆图谱（力导向）/ 保留度直方图 / 心跳
   全部原生 Canvas 2D，不引第三方库。
   ============================================================ */
(function (global) {
  'use strict';

  const BW = global.BW || (global.BW = {});
  const CY = '#37e6ff', VI = '#8b5cff', RD = '#ff3b5c', GR = '#3bffa5', AM = '#ffc44d';

  function hiDpi(cv) {
    const dpr = global.devicePixelRatio || 1;
    const r = cv.getBoundingClientRect();
    const w = Math.max(1, Math.round(r.width)), h = Math.max(1, Math.round(r.height));
    if (cv.width !== w * dpr || cv.height !== h * dpr) {
      cv.width = w * dpr; cv.height = h * dpr;
    }
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx: ctx, w: w, h: h };
  }

  // ============================================================ 星野背景
  function Starfield(canvas, count) {
    this.cv = canvas;
    this.n = count || 130;
    this.stars = [];
    this.t = 0;
    this.resize();
    global.addEventListener('resize', () => this.resize());
  }
  Starfield.prototype.resize = function () {
    const d = hiDpi(this.cv);
    this.w = d.w; this.h = d.h;
    this.stars = [];
    for (let i = 0; i < this.n; i++) {
      this.stars.push({
        x: Math.random() * this.w, y: Math.random() * this.h,
        z: 0.3 + Math.random() * 0.9,
        r: 0.4 + Math.random() * 1.3,
        p: Math.random() * Math.PI * 2
      });
    }
  };
  Starfield.prototype.frame = function () {
    const d = hiDpi(this.cv), c = d.ctx, w = d.w, h = d.h;
    this.w = w; this.h = h; this.t += 0.008;
    c.clearRect(0, 0, w, h);
    // 深空渐变
    const g = c.createRadialGradient(w * 0.5, h * 0.12, 0, w * 0.5, h * 0.12, Math.max(w, h) * 0.9);
    g.addColorStop(0, 'rgba(18,28,54,.85)');
    g.addColorStop(0.55, 'rgba(8,12,24,.9)');
    g.addColorStop(1, 'rgba(3,4,9,1)');
    c.fillStyle = g; c.fillRect(0, 0, w, h);

    for (let i = 0; i < this.stars.length; i++) {
      const s = this.stars[i];
      const a = 0.25 + 0.6 * Math.abs(Math.sin(this.t * s.z + s.p));
      c.globalAlpha = a * s.z;
      c.fillStyle = i % 7 === 0 ? VI : (i % 11 === 0 ? CY : '#cfe2ff');
      c.beginPath(); c.arc(s.x, s.y, s.r * s.z, 0, 6.2832); c.fill();
      s.y += s.z * 0.06;
      if (s.y > h) { s.y = -2; s.x = Math.random() * w; }
    }
    c.globalAlpha = 1;
  };
  Starfield.prototype.run = function () {
    const loop = () => { this.frame(); global.requestAnimationFrame(loop); };
    loop();
  };

  // ============================================================ 情绪曲线
  function drawMood(canvas, curve) {
    const d = hiDpi(canvas), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    const pad = { l: 30, r: 10, t: 12, b: 18 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;

    // 网格 + 零线
    c.strokeStyle = 'rgba(120,180,255,.10)'; c.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + ih * i / 4;
      c.beginPath(); c.moveTo(pad.l, y); c.lineTo(w - pad.r, y); c.stroke();
    }
    c.strokeStyle = 'rgba(120,180,255,.28)';
    c.setLineDash([3, 3]);
    const y0 = pad.t + ih / 2;
    c.beginPath(); c.moveTo(pad.l, y0); c.lineTo(w - pad.r, y0); c.stroke();
    c.setLineDash([]);

    c.fillStyle = 'rgba(109,124,153,.85)';
    c.font = '10px ui-monospace, monospace';
    c.textAlign = 'right';
    c.fillText('+1', pad.l - 6, pad.t + 9);
    c.fillText(' 0', pad.l - 6, y0 + 3);
    c.fillText('-1', pad.l - 6, pad.t + ih + 3);

    const pts = (curve || []).map(p => Number(p.mood || 0));
    if (!pts.length) {
      c.fillStyle = 'rgba(109,124,153,.7)'; c.textAlign = 'center';
      c.fillText('等待采样…', w / 2, h / 2);
      return;
    }
    const n = pts.length;
    const X = i => pad.l + (n === 1 ? iw / 2 : iw * i / (n - 1));
    const Y = v => pad.t + ih * (1 - (Math.max(-1, Math.min(1, v)) + 1) / 2);

    // 面积
    const grad = c.createLinearGradient(0, pad.t, 0, pad.t + ih);
    grad.addColorStop(0, 'rgba(59,255,165,.22)');
    grad.addColorStop(0.5, 'rgba(55,230,255,.10)');
    grad.addColorStop(1, 'rgba(255,59,92,.20)');
    c.beginPath();
    c.moveTo(X(0), Y(pts[0]));
    for (let i = 1; i < n; i++) { c.lineTo(X(i), Y(pts[i])); }
    c.lineTo(X(n - 1), pad.t + ih); c.lineTo(X(0), pad.t + ih); c.closePath();
    c.fillStyle = grad; c.fill();

    // 线
    c.beginPath();
    c.moveTo(X(0), Y(pts[0]));
    for (let i = 1; i < n; i++) { c.lineTo(X(i), Y(pts[i])); }
    c.strokeStyle = CY; c.lineWidth = 1.8;
    c.shadowColor = 'rgba(55,230,255,.7)'; c.shadowBlur = 8;
    c.stroke();
    c.shadowBlur = 0;

    // 末点
    const lx = X(n - 1), ly = Y(pts[n - 1]);
    c.beginPath(); c.arc(lx, ly, 3.2, 0, 6.2832);
    c.fillStyle = pts[n - 1] >= 0 ? GR : RD; c.fill();
    c.beginPath(); c.arc(lx, ly, 6.5, 0, 6.2832);
    c.strokeStyle = pts[n - 1] >= 0 ? 'rgba(59,255,165,.4)' : 'rgba(255,59,92,.4)';
    c.lineWidth = 1; c.stroke();
  }

  // ============================================================ 预测误差仪表
  function drawGauge(canvas, value, avg) {
    const d = hiDpi(canvas), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h * 0.86, R = Math.min(w * 0.42, h * 0.78);
    const A0 = Math.PI * 1.0, A1 = Math.PI * 2.0;   // 半圆

    // 底环
    c.lineWidth = 9; c.lineCap = 'round';
    c.strokeStyle = 'rgba(120,180,255,.12)';
    c.beginPath(); c.arc(cx, cy, R, A0, A1); c.stroke();

    // 分段刻度
    const segs = [[0, .33, GR], [.33, .66, AM], [.66, 1, RD]];
    segs.forEach(s => {
      c.strokeStyle = s[2] + '33';
      c.beginPath(); c.arc(cx, cy, R, A0 + (A1 - A0) * s[0], A0 + (A1 - A0) * s[1]); c.stroke();
    });

    const v = Math.max(0, Math.min(1, Number(value || 0)));
    const ang = A0 + (A1 - A0) * v;
    const col = v < .33 ? GR : (v < .66 ? AM : RD);
    c.strokeStyle = col; c.shadowColor = col; c.shadowBlur = 12;
    c.beginPath(); c.arc(cx, cy, R, A0, ang); c.stroke();
    c.shadowBlur = 0;

    // 指针
    c.strokeStyle = '#e9f2ff'; c.lineWidth = 2;
    c.beginPath();
    c.moveTo(cx + Math.cos(ang) * (R - 16), cy + Math.sin(ang) * (R - 16));
    c.lineTo(cx + Math.cos(ang) * (R + 3), cy + Math.sin(ang) * (R + 3));
    c.stroke();

    // 均值标记
    if (avg !== undefined && avg !== null) {
      const a2 = A0 + (A1 - A0) * Math.max(0, Math.min(1, Number(avg)));
      c.strokeStyle = 'rgba(139,92,255,.85)'; c.lineWidth = 1.4;
      c.beginPath();
      c.moveTo(cx + Math.cos(a2) * (R - 13), cy + Math.sin(a2) * (R - 13));
      c.lineTo(cx + Math.cos(a2) * (R + 9), cy + Math.sin(a2) * (R + 9));
      c.stroke();
    }
    c.beginPath(); c.arc(cx, cy, 3.5, 0, 6.2832); c.fillStyle = '#e9f2ff'; c.fill();
  }

  // ============================================================ 认知雷达
  /**
   * 六维认知雷达。
   * 这个图只有"真有认知内核"才画得出来——同类项目没有情绪、没有预测误差、
   * 没有焦点栈，六根轴里有一半是空的。
   * dims: [{label, value(0..1), color}]
   */
  function drawRadar(canvas, dims) {
    const d = hiDpi(canvas), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2 + 2;
    const R = Math.min(w, h) * 0.36;
    const n = (dims || []).length;
    if (!n) { return; }

    // 底网
    for (let ring = 1; ring <= 4; ring++) {
      const rr = R * ring / 4;
      c.beginPath();
      for (let i = 0; i <= n; i++) {
        const a = (i / n) * Math.PI * 2 - Math.PI / 2;
        const x = cx + Math.cos(a) * rr, y = cy + Math.sin(a) * rr;
        if (i === 0) { c.moveTo(x, y); } else { c.lineTo(x, y); }
      }
      c.closePath();
      c.strokeStyle = ring === 4 ? 'rgba(120,180,255,.28)' : 'rgba(120,180,255,.10)';
      c.lineWidth = 1; c.stroke();
    }
    // 轴线 + 标签
    c.font = '9.5px ui-monospace, monospace';
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 - Math.PI / 2;
      c.strokeStyle = 'rgba(120,180,255,.12)';
      c.beginPath();
      c.moveTo(cx, cy); c.lineTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
      c.stroke();
      const lx = cx + Math.cos(a) * (R + 15), ly = cy + Math.sin(a) * (R + 15);
      c.fillStyle = 'rgba(141,158,186,.9)';
      c.textAlign = Math.abs(Math.cos(a)) < 0.25 ? 'center' : (Math.cos(a) > 0 ? 'left' : 'right');
      c.textBaseline = 'middle';
      c.fillText(dims[i].label, lx, ly);
    }

    // 数据面
    c.beginPath();
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 - Math.PI / 2;
      const v = Math.max(0.02, Math.min(1, Number(dims[i].value || 0)));
      const x = cx + Math.cos(a) * R * v, y = cy + Math.sin(a) * R * v;
      if (i === 0) { c.moveTo(x, y); } else { c.lineTo(x, y); }
    }
    c.closePath();
    const g = c.createRadialGradient(cx, cy, 0, cx, cy, R);
    g.addColorStop(0, 'rgba(55,230,255,.42)');
    g.addColorStop(1, 'rgba(139,92,255,.22)');
    c.fillStyle = g; c.fill();
    c.strokeStyle = CY; c.lineWidth = 1.6;
    c.shadowColor = 'rgba(55,230,255,.55)'; c.shadowBlur = 8;
    c.stroke(); c.shadowBlur = 0;

    // 顶点
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2 - Math.PI / 2;
      const v = Math.max(0.02, Math.min(1, Number(dims[i].value || 0)));
      c.beginPath();
      c.arc(cx + Math.cos(a) * R * v, cy + Math.sin(a) * R * v, 2.6, 0, 6.2832);
      c.fillStyle = dims[i].color || CY; c.fill();
    }
  }

  // ============================================================ 记忆保留环
  function drawRing(canvas, value, label, sub) {
    const d = hiDpi(canvas), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const R = Math.min(w, h) * 0.38;
    const v = Math.max(0, Math.min(1, Number(value || 0)));

    c.lineWidth = 10; c.lineCap = 'round';
    c.strokeStyle = 'rgba(120,180,255,.12)';
    c.beginPath(); c.arc(cx, cy, R, 0, 6.2832); c.stroke();

    const g = c.createLinearGradient(cx - R, cy - R, cx + R, cy + R);
    g.addColorStop(0, CY); g.addColorStop(1, VI);
    c.strokeStyle = g;
    c.shadowColor = 'rgba(55,230,255,.5)'; c.shadowBlur = 10;
    c.beginPath();
    c.arc(cx, cy, R, -Math.PI / 2, -Math.PI / 2 + 6.2832 * v);
    c.stroke(); c.shadowBlur = 0;

    c.textAlign = 'center'; c.textBaseline = 'middle';
    c.fillStyle = '#e6eefc';
    c.font = '600 20px ui-monospace, monospace';
    c.fillText(label || '', cx, cy - 4);
    if (sub) {
      c.fillStyle = 'rgba(109,124,153,.9)';
      c.font = '10px ui-monospace, monospace';
      c.fillText(sub, cx, cy + 15);
    }
  }

  // ============================================================ 记忆图谱（力导向）
  function MemoryGraph(canvas) {
    this.cv = canvas;
    this.nodes = [];
    this.links = [];
    this.raf = null;
  }
  MemoryGraph.prototype.setData = function (items) {
    const prev = {};
    this.nodes.forEach(n => { prev[n.id] = n; });
    this.nodes = (items || []).slice(0, 90).map((m, i) => {
      const id = String(m.id);
      const old = prev[id];
      const a = (i / Math.max(1, (items || []).length)) * Math.PI * 2;
      return {
        id: id, title: String(m.title || ''), cat: String(m.category || '日常'),
        rt: Number(m.retention || 0), sal: Number(m.salience || 1),
        x: old ? old.x : 0, y: old ? old.y : 0,
        vx: old ? old.vx : 0, vy: old ? old.vy : 0,
        seed: a, r: 3 + Math.min(6, Number(m.salience || 1) * 1.1)
      };
    });
    // 同类别互连（构成"语义簇"）
    const byCat = {};
    this.nodes.forEach((n, i) => {
      (byCat[n.cat] = byCat[n.cat] || []).push(i);
    });
    this.links = [];
    Object.keys(byCat).forEach(k => {
      const arr = byCat[k];
      for (let i = 0; i + 1 < arr.length; i++) {
        this.links.push([arr[i], arr[i + 1]]);
      }
      if (arr.length > 2) { this.links.push([arr[0], arr[arr.length - 1]]); }
    });
    if (!this.raf) { this._loop(); }
  };
  MemoryGraph.prototype._color = function (rt) {
    return rt > 0.66 ? CY : (rt > 0.33 ? VI : '#41506b');
  };
  MemoryGraph.prototype._loop = function () {
    const self = this;
    const step = function () {
      self._tick();
      self.raf = global.requestAnimationFrame(step);
    };
    this.raf = global.requestAnimationFrame(step);
  };
  MemoryGraph.prototype._tick = function () {
    const d = hiDpi(this.cv), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2;
    const N = this.nodes.length;
    if (!N) {
      c.fillStyle = 'rgba(109,124,153,.65)';
      c.font = '11px ui-monospace, monospace'; c.textAlign = 'center';
      c.fillText('暂无记忆节点', w / 2, h / 2);
      return;
    }
    const scale = Math.min(w, h) * 0.32;

    // 力：向心 + 斥力 + 弹簧
    for (let i = 0; i < N; i++) {
      const a = this.nodes[i];
      const tx = cx + Math.cos(a.seed) * scale * (0.45 + 0.55 * a.rt);
      const ty = cy + Math.sin(a.seed) * scale * (0.45 + 0.55 * a.rt);
      a.vx += (tx - a.x) * 0.012;
      a.vy += (ty - a.y) * 0.012;
      for (let j = i + 1; j < N; j++) {
        const b = this.nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) { d2 = 1; dx = 0.6; dy = 0.4; }
        if (d2 < 3000) {
          const f = 220 / d2;
          a.vx -= dx * f; a.vy -= dy * f;
          b.vx += dx * f; b.vy += dy * f;
        }
      }
    }
    for (let k = 0; k < this.links.length; k++) {
      const a = this.nodes[this.links[k][0]], b = this.nodes[this.links[k][1]];
      if (!a || !b) { continue; }
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (dist - 42) * 0.006;
      const ux = dx / dist * f, uy = dy / dist * f;
      a.vx += ux; a.vy += uy; b.vx -= ux; b.vy -= uy;
    }
    for (let i = 0; i < N; i++) {
      const a = this.nodes[i];
      a.vx *= 0.86; a.vy *= 0.86;
      a.x += a.vx; a.y += a.vy;
      a.x = Math.max(10, Math.min(w - 10, a.x));
      a.y = Math.max(10, Math.min(h - 10, a.y));
    }

    // 连线
    c.lineWidth = 1;
    for (let k = 0; k < this.links.length; k++) {
      const a = this.nodes[this.links[k][0]], b = this.nodes[this.links[k][1]];
      if (!a || !b) { continue; }
      c.strokeStyle = 'rgba(120,180,255,.14)';
      c.beginPath(); c.moveTo(a.x, a.y); c.lineTo(b.x, b.y); c.stroke();
    }
    // 节点
    for (let i = 0; i < N; i++) {
      const a = this.nodes[i];
      const col = this._color(a.rt);
      c.beginPath(); c.arc(a.x, a.y, a.r, 0, 6.2832);
      c.fillStyle = col;
      c.shadowColor = col; c.shadowBlur = a.rt > 0.66 ? 9 : 3;
      c.fill(); c.shadowBlur = 0;
      if (a.r > 5) {
        c.fillStyle = 'rgba(216,226,242,.62)';
        c.font = '9px ui-monospace, monospace'; c.textAlign = 'center';
        const t = a.title.length > 9 ? a.title.slice(0, 9) + '…' : a.title;
        c.fillText(t, a.x, a.y + a.r + 9);
      }
    }
  };

  // ============================================================ 保留度直方图
  function drawRetention(canvas, items) {
    const d = hiDpi(canvas), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    const bins = [0, 0, 0, 0, 0];
    (items || []).forEach(m => {
      const r = Math.max(0, Math.min(1, Number(m.retention === undefined ? 1 : m.retention)));
      bins[Math.min(4, Math.floor(r * 5))]++;
    });
    const max = Math.max(1, ...bins);
    const pad = { l: 4, r: 4, t: 8, b: 18 };
    const iw = w - pad.l - pad.r, ih = h - pad.t - pad.b;
    const bw = iw / 5;
    const cols = ['#41506b', '#4c6ea8', VI, CY, GR];
    const labels = ['0-20', '20-40', '40-60', '60-80', '80-100'];
    for (let i = 0; i < 5; i++) {
      const bh = ih * (bins[i] / max);
      const x = pad.l + bw * i + 3, y = pad.t + ih - bh;
      const g = c.createLinearGradient(0, y, 0, pad.t + ih);
      g.addColorStop(0, cols[i]); g.addColorStop(1, cols[i] + '22');
      c.fillStyle = g;
      c.fillRect(x, y, bw - 6, bh);
      c.fillStyle = 'rgba(109,124,153,.8)';
      c.font = '9px ui-monospace, monospace'; c.textAlign = 'center';
      c.fillText(String(bins[i]), x + (bw - 6) / 2, y - 3);
      c.fillStyle = 'rgba(77,91,118,.9)';
      c.fillText(labels[i], x + (bw - 6) / 2, h - 5);
    }
  }

  // ============================================================ 心跳波形
  function BeatLine(canvas) {
    this.cv = canvas;
    this.buf = new Array(120).fill(0);
    this.t = 0;
  }
  BeatLine.prototype.push = function (v) {
    this.buf.push(Number(v || 0));
    if (this.buf.length > 120) { this.buf.shift(); }
  };
  BeatLine.prototype.frame = function () {
    const d = hiDpi(this.cv), c = d.ctx, w = d.w, h = d.h;
    c.clearRect(0, 0, w, h);
    this.t += 0.06;
    const mid = h * 0.55;
    c.strokeStyle = 'rgba(120,180,255,.10)';
    c.beginPath(); c.moveTo(0, mid); c.lineTo(w, mid); c.stroke();

    c.beginPath();
    for (let i = 0; i < this.buf.length; i++) {
      const x = w * i / (this.buf.length - 1);
      const amp = this.buf[i] * h * 0.34;
      const y = mid - amp * Math.sin(i * 0.35 - this.t) - (amp > 0 ? amp * 0.5 : 0);
      if (i === 0) { c.moveTo(x, y); } else { c.lineTo(x, y); }
    }
    c.strokeStyle = CY; c.lineWidth = 1.4;
    c.shadowColor = 'rgba(55,230,255,.6)'; c.shadowBlur = 6;
    c.stroke(); c.shadowBlur = 0;
  };

  BW.viz = {
    Starfield: Starfield,
    MemoryGraph: MemoryGraph,
    BeatLine: BeatLine,
    drawMood: drawMood,
    drawGauge: drawGauge,
    drawRetention: drawRetention,
    drawRadar: drawRadar,
    drawRing: drawRing
  };

})(window);
