/* 信号仪表台脉冲条组件（PulseStrip）与直播心电图组件（EcgStrip）。
   零依赖 Canvas 2D：读 CSS 信号色变量、DPR 适配、容器 resize 重绘、主题切换重绘，
   prefers-reduced-motion 时降级为静态图，另挂 sr-only 文本摘要。
   PulseStrip 静态模式只画一次，实时模式走 rAF 持续刷新数据窗。
   对外暴露 window.PulseStrip 与 window.EcgStrip。 */
(function () {
  'use strict';

  /* 读 CSS 变量（带缓存：主题切换时清缓存） */
  var varCache = {};
  function cssVar(name, el) {
    if (varCache[name] != null) return varCache[name];
    el = el || document.documentElement;
    var val = getComputedStyle(el).getPropertyValue(name).trim();
    varCache[name] = val || '';
    return val;
  }
  function clearVarCache() { varCache = {}; }

  /* 主题切换监听（全局唯一，所有实例共享） */
  var themeObserver = null;
  var instances = [];
  function ensureThemeObserver() {
    if (themeObserver) return;
    themeObserver = new MutationObserver(function () {
      clearVarCache();
      instances.forEach(function (s) { s._onThemeChange(); });
    });
    themeObserver.observe(document.documentElement, {
      attributes: true, attributeFilter: ['data-theme'],
    });
  }

  /* 实例化脉冲条：把 canvas 与 sr-only 摘要挂到 container，注册 resize 监听；
     realtime 且未开 reduce-motion 时启动 rAF 循环，static 模式只画一次 */
  function PulseStrip(container, opts) {
    if (!container) throw new Error('PulseStrip: container required');
    opts = opts || {};
    this.container = container;
    this.data = Array.isArray(opts.data) ? opts.data.slice() : [];
    this.events = Array.isArray(opts.events) ? opts.events.slice() : [];
    this.mode = opts.mode === 'realtime' ? 'realtime' : 'static';
    this.colorVar = opts.color || 'var(--sig-level)';
    this.windowSeconds = opts.windowSeconds || 60;
    this.srLabel = opts.srLabel || ('近 ' + this.windowSeconds + ' 秒信号');
    this._reduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    /* 建 canvas */
    this.canvas = document.createElement('canvas');
    this.canvas.style.display = 'block';
    this.canvas.style.width = '100%';
    this.canvas.style.height = '100%';
    container.innerHTML = '';
    container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');

    /* sr-only 摘要 */
    this.sr = document.createElement('span');
    this.sr.className = 'sr-only';
    container.appendChild(this.sr);

    this._rafId = null;
    this._resizeRaf = null;

    ensureThemeObserver();
    instances.push(this);

    this._setupResize();
    this.redraw();
    if (this.mode === 'realtime' && !this._reduced) this._startLoop();
  }

  PulseStrip.prototype._setupResize = function () {
    var self = this;
    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(function () { self._scheduleRedraw(); });
      this._ro.observe(this.container);
    } else {
      window.addEventListener('resize', this._onResize = function () { self._scheduleRedraw(); });
    }
  };

  PulseStrip.prototype._scheduleRedraw = function () {
    var self = this;
    if (this._resizeRaf) return;
    this._resizeRaf = requestAnimationFrame(function () {
      self._resizeRaf = null;
      self.redraw();
    });
  };

  PulseStrip.prototype._onThemeChange = function () {
    this.redraw();
  };

  /* 解析颜色：支持 var(--xxx) 或直接色值 */
  PulseStrip.prototype._resolveColor = function (expr) {
    if (expr && expr.indexOf('var(') === 0) {
      var name = expr.slice(4, -1).trim();
      return cssVar(name) || '#39d59b';
    }
    return expr || '#39d59b';
  };

  PulseStrip.prototype._applyDPR = function () {
    var rect = this.container.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.floor(rect.width));
    var h = Math.max(1, Math.floor(rect.height));
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.scale(dpr, dpr);
    return { w: w, h: h };
  };

  /* 全量重绘：按容器尺寸重设 DPR，画网格横线、基线、面积+折线波形、末端发光点与事件竖线 */
  PulseStrip.prototype.redraw = function () {
    var g = this._applyDPR();
    var ctx = this.ctx;
    var data = this.data;
    var color = this._resolveColor(this.colorVar);

    ctx.clearRect(0, 0, g.w, g.h);

    if (!data.length) {
      this._updateSr();
      return;
    }

    /* 画网格横线（3 等分，仪表刻度感） */
    ctx.strokeStyle = cssVar('--border-soft') || 'rgba(255,255,255,.1)';
    ctx.lineWidth = 1;
    for (var gx = 1; gx < 3; gx++) {
      var gy = Math.round(g.h * gx / 3) + 0.5;
      ctx.beginPath();
      ctx.moveTo(0, gy);
      ctx.lineTo(g.w, gy);
      ctx.stroke();
    }

    /* 画基线 */
    ctx.beginPath();
    ctx.moveTo(0, g.h - 0.5);
    ctx.lineTo(g.w, g.h - 0.5);
    ctx.stroke();

    /* 画波形（面积 + 折线） */
    var n = data.length;
    var stepX = g.w / Math.max(1, n - 1);
    var maxV = 0;
    for (var i = 0; i < n; i++) if (data[i] > maxV) maxV = data[i];
    if (maxV <= 0) maxV = 1;

    ctx.beginPath();
    ctx.moveTo(0, g.h);
    for (var j = 0; j < n; j++) {
      var x = j * stepX;
      var y = g.h - (data[j] / maxV) * (g.h - 4) - 2;
      if (j === 0) ctx.lineTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.lineTo(g.w, g.h);
    ctx.closePath();
    ctx.fillStyle = hexToRgba(color, 0.18);
    ctx.fill();

    ctx.beginPath();
    for (var k = 0; k < n; k++) {
      var xx = k * stepX;
      var yy = g.h - (data[k] / maxV) * (g.h - 4) - 2;
      if (k === 0) ctx.moveTo(xx, yy); else ctx.lineTo(xx, yy);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    /* 末端发光圆点（实时感） */
    var lastX = (n - 1) * stepX;
    var lastY = g.h - (data[n - 1] / maxV) * (g.h - 4) - 2;
    ctx.beginPath();
    ctx.arc(lastX, lastY, 3.5, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.shadowColor = color;
    ctx.shadowBlur = 8;
    ctx.fill();
    ctx.shadowBlur = 0;

    /* 事件标记（竖向尖峰） */
    for (var e = 0; e < this.events.length; e++) {
      var ev = this.events[e];
      var ex = (ev.at / this.windowSeconds) * g.w;
      if (ex < 0 || ex > g.w) continue;
      ctx.strokeStyle = cssVar('--sig-peak') || '#f6b93b';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(ex, 0);
      ctx.lineTo(ex, g.h);
      ctx.stroke();
    }

    this._updateSr(maxV);
  };

  /* 更新 sr-only 文本：写入峰值与事件标记数；maxV 缺省时自行扫描 data */
  PulseStrip.prototype._updateSr = function (maxV) {
    if (maxV == null) {
      var data = this.data;
      maxV = 0;
      for (var i = 0; i < data.length; i++) if (data[i] > maxV) maxV = data[i];
    }
    var peak = Math.round(maxV);
    this.sr.textContent = this.srLabel +
      '：峰值 ' + peak + (this.events.length ? '，含 ' + this.events.length + ' 个事件标记' : '');
  };

  /* 替换数据数组并立即重绘（拷贝一份，避免外部改动影响内部状态） */
  PulseStrip.prototype.setData = function (arr) {
    this.data = Array.isArray(arr) ? arr.slice() : [];
    this.redraw();
  };

  /* 启动 rAF 循环持续重绘（实时模式专用） */
  PulseStrip.prototype._startLoop = function () {
    var self = this;
    function tick() {
      self.redraw();
      self._rafId = requestAnimationFrame(tick);
    }
    this._rafId = requestAnimationFrame(tick);
  };

  /* 销毁实例：停 rAF、解绑 resize、从全局实例表移除，并摘掉 canvas 与 sr-only */
  PulseStrip.prototype.destroy = function () {
    if (this._rafId) cancelAnimationFrame(this._rafId);
    if (this._ro) this._ro.disconnect();
    else if (this._onResize) window.removeEventListener('resize', this._onResize);
    var idx = instances.indexOf(this);
    if (idx >= 0) instances.splice(idx, 1);
    if (this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas);
    if (this.sr.parentNode) this.sr.parentNode.removeChild(this.sr);
  };

  /* #rrggbb / #rgb → rgba() */
  function hexToRgba(hex, a) {
    if (!hex || hex[0] !== '#') return 'rgba(57,213,155,' + a + ')';
    var h = hex.slice(1);
    if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
    var r = parseInt(h.slice(0, 2), 16);
    var g = parseInt(h.slice(2, 4), 16);
    var b = parseInt(h.slice(4, 6), 16);
    return 'rgba(' + r + ',' + g + ',' + b + ',' + a + ')';
  }

  /* 直播心电图（EcgStrip）：把一场直播的多层信号叠在同一时间轴上。
     由底向上依次是：人气半透明面积 → 弹幕折线/面积主轨迹 → 金礼物粒子 →
     SC 竖向尖峰（高度按金额对数）→ 高能窗口高亮。
     交互：滚轮以光标为锚点缩放、拖拽平移、悬停十字线 + tooltip、单击回调 onSeek(point)。
     颜色全部读 CSS 变量随主题切换；静态绘制不跑 rAF，reduce-motion 天然为静态图；旁挂 sr-only 摘要。 */
  function EcgStrip(container, opts) {
    if (!container) throw new Error('EcgStrip: container required');
    opts = opts || {};
    this.container = container;
    this.series = [];
    this.segments = [];
    this.windows = [];
    this.totals = null;
    this.groupSec = 60;
    this.onSeek = opts.onSeek || null;
    this.windowsVar = opts.windows || [];   // 可选：外部注入高亮窗口
    this._reduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    container.classList.add('ecg-wrap');
    container.innerHTML = '';
    this.canvas = document.createElement('canvas');
    this.canvas.style.display = 'block';
    this.canvas.style.width = '100%';
    this.canvas.style.height = '100%';
    this.canvas.style.touchAction = 'none';
    if (this.onSeek) this.canvas.style.cursor = 'crosshair';
    container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext('2d');

    this.tip = document.createElement('div');
    this.tip.className = 'ecg-tip';
    this.tip.hidden = true;
    container.appendChild(this.tip);

    this.sr = document.createElement('span');
    this.sr.className = 'sr-only';
    container.appendChild(this.sr);

    this.view = { t0: 0, t1: 1 };
    this.session = null;
    this._hoverIdx = -1;
    this._drag = null;

    ensureThemeObserver();
    instances.push(this);
    this._bindPointer();
    this._setupResize();
    this.redraw();
  }

  EcgStrip.prototype._setupResize = function () {
    var self = this;
    if (typeof ResizeObserver !== 'undefined') {
      this._ro = new ResizeObserver(function () { self._scheduleRedraw(); });
      this._ro.observe(this.container);
    } else {
      window.addEventListener('resize', this._onResize = function () { self._scheduleRedraw(); });
    }
  };

  EcgStrip.prototype._scheduleRedraw = function () {
    var self = this;
    if (this._resizeRaf) return;
    this._resizeRaf = requestAnimationFrame(function () {
      self._resizeRaf = null;
      self.redraw();
    });
  };

  EcgStrip.prototype._onThemeChange = function () { this.redraw(); };

  EcgStrip.prototype._applyDPR = function () {
    var rect = this.container.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.floor(rect.width));
    var h = Math.max(1, Math.floor(rect.height));
    this.canvas.width = w * dpr;
    this.canvas.height = h * dpr;
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.ctx.setTransform(1, 0, 0, 1, 0, 0);
    this.ctx.scale(dpr, dpr);
    return { w: w, h: h };
  };

  EcgStrip.prototype._resolve = function (name) {
    return cssVar(name) || '#888888';
  };

  /* 载入一整场数据：payload 含 series/segments/windows/totals/group_sec；
     session 取首尾时间戳定界，view 重置为整场，并清悬停与 tooltip */
  EcgStrip.prototype.setData = function (payload) {
    payload = payload || {};
    this.series = Array.isArray(payload.series) ? payload.series : [];
    this.segments = payload.segments || [];
    this.windows = (payload.windows || this.windowsVar || []).slice();
    this.totals = payload.totals || null;
    this.groupSec = payload.group_sec || payload.bucket_sec || 60;
    var n = this.series.length;
    if (n) {
      var t0 = this.series[0].ts;
      var t1 = this.series[n - 1].ts + this.groupSec;
      if (t1 <= t0) t1 = t0 + this.groupSec;
      this.session = { t0: t0, t1: t1 };
      this.view = { t0: t0, t1: t1 };
    } else {
      this.session = null;
    }
    this._hoverIdx = -1;
    this.tip.hidden = true;
    this.redraw();
  };

  /* 视图复位到整场范围（双击画布或切换场次时调用） */
  EcgStrip.prototype.resetView = function () {
    if (this.session) this.view = { t0: this.session.t0, t1: this.session.t1 };
    this.redraw();
  };

  EcgStrip.prototype._visRange = function () {
    var s = this.series, n = s.length, step = this.groupSec || 60;
    var a = Math.floor((this.view.t0 - s[0].ts) / step);
    var b = Math.ceil((this.view.t1 - s[0].ts) / step);
    if (a < 0) a = 0;
    if (b > n) b = n;
    if (b <= a) b = Math.min(n, a + 1);
    return [a, b];
  };

  EcgStrip.prototype._tsAt = function (mx, w) {
    return this.view.t0 + (mx / w) * (this.view.t1 - this.view.t0);
  };

  EcgStrip.prototype._xOf = function (ts, padL, plotW) {
    var span = Math.max(1e-6, this.view.t1 - this.view.t0);
    return padL + (ts - this.view.t0) / span * plotW;
  };

  EcgStrip.prototype._nearestIdx = function (tc) {
    var s = this.series;
    if (!s.length) return -1;
    var step = this.groupSec || 60;
    var idx = Math.round((tc - s[0].ts) / step);
    if (idx < 0) idx = 0;
    if (idx >= s.length) idx = s.length - 1;
    return idx;
  };

  EcgStrip.prototype._clampView = function () {
    var S = this.session;
    if (!S) return;
    var sessionSpan = S.t1 - S.t0;
    var span = this.view.t1 - this.view.t0;
    if (span >= sessionSpan) { this.view.t0 = S.t0; this.view.t1 = S.t1; return; }
    if (this.view.t0 < S.t0) { this.view.t0 = S.t0; this.view.t1 = S.t0 + span; }
    if (this.view.t1 > S.t1) { this.view.t1 = S.t1; this.view.t0 = S.t1 - span; }
  };

  /* 滚轮缩放：以光标所在时刻为锚点，缩放后夹在最小跨度与整场范围之间 */
  EcgStrip.prototype._zoom = function (e) {
    if (!this.series.length) return;
    var rect = this.canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    var span = this.view.t1 - this.view.t0;
    var anchor = Math.min(1, Math.max(0, mx / Math.max(1, rect.width)));
    var tc = this.view.t0 + anchor * span;
    var s = e.deltaY < 0 ? 0.8 : 1.25;
    var ns = span * s;
    var minSpan = Math.max((this.groupSec || 60) * 4, 60);
    if (ns < minSpan) ns = minSpan;
    var sessionSpan = this.session ? this.session.t1 - this.session.t0 : ns;
    if (ns > sessionSpan) ns = sessionSpan;
    this.view.t0 = tc - anchor * ns;
    this.view.t1 = this.view.t0 + ns;
    this._clampView();
    this.redraw();
  };

  EcgStrip.prototype._bindPointer = function () {
    var self = this, cv = this.canvas;
    cv.addEventListener('wheel', function (e) { e.preventDefault(); self._zoom(e); },
      { passive: false });
    cv.addEventListener('pointerdown', function (e) {
      try { cv.setPointerCapture(e.pointerId); } catch (_) { /* noop */ }
      self._drag = { x: e.clientX, moved: 0, view: { t0: self.view.t0, t1: self.view.t1 } };
      self._hoverIdx = -1;
      self.tip.hidden = true;
    });
    cv.addEventListener('pointermove', function (e) { self._onMove(e); });
    cv.addEventListener('pointerup', function (e) { self._onUp(e); });
    cv.addEventListener('dblclick', function (e) { e.preventDefault(); self.resetView(); });
    cv.addEventListener('pointerleave', function () {
      self.tip.hidden = true; self._hoverIdx = -1; self.redraw();
    });
  };

  /* 指针移动：拖拽中（位移 >4px）平移视图，否则标出最近数据点并弹 tooltip */
  EcgStrip.prototype._onMove = function (e) {
    var rect = this.canvas.getBoundingClientRect();
    var mx = e.clientX - rect.left;
    if (this._drag) {
      var dx = e.clientX - this._drag.x;
      if (Math.abs(dx) > this._drag.moved) this._drag.moved = Math.abs(dx);
      if (this._drag.moved > 4) {
        var span = this._drag.view.t1 - this._drag.view.t0;
        var dts = -dx / Math.max(1, rect.width) * span;
        this.view.t0 = this._drag.view.t0 + dts;
        this.view.t1 = this._drag.view.t1 + dts;
        this._clampView();
        this.redraw();
      }
      return;
    }
    var idx = this._nearestIdx(this._tsAt(mx, Math.max(1, rect.width)));
    this._hoverIdx = idx;
    this._showTip(e, idx, rect);
    this.redraw();
  };

  EcgStrip.prototype._onUp = function (e) {
    if (!this._drag) return;
    var moved = this._drag.moved;
    this._drag = null;
    if (moved > 4 || !this.onSeek) return;
    var rect = this.canvas.getBoundingClientRect();
    var idx = this._nearestIdx(this._tsAt(e.clientX - rect.left, Math.max(1, rect.width)));
    if (idx >= 0) this.onSeek(this.series[idx]);
  };

  /* 拼 tooltip 内容，并按指针位置左右避让防超出画布（idx 无效则隐藏） */
  EcgStrip.prototype._showTip = function (e, idx, rect) {
    if (idx < 0 || !this.series[idx]) { this.tip.hidden = true; return; }
    var p = this.series[idx];
    var span = p.span || 1;
    var html = '<div class="ecg-tip-t">' + _clock(p.ts) + '</div>';
    html += '<div>弹幕 ' + p.dm + (span > 1 ? '（峰）· ' + p.dm_sum + '（共）' : '') + '</div>';
    html += p.sc
      ? '<div>SC ' + p.sc + ' 条 · ¥' + _n(p.sc_amount) + '</div>'
      : '<div class="ecg-tip-off">本时段无 SC</div>';
    if (p.gift) html += '<div>金礼物 ' + p.gift + ' 件 · ¥' + _n(p.gift_amount) + '</div>';
    html += p.pop ? '<div>人气 ' + _n(p.pop) + '</div>'
      : '<div class="ecg-tip-off">无人气采样</div>';
    this.tip.innerHTML = html;
    this.tip.hidden = false;
    var tw = this.tip.offsetWidth || 150;
    var left = (e.clientX - rect.left) + 12;
    if (left + tw > rect.width) left = (e.clientX - rect.left) - tw - 12;
    this.tip.style.left = Math.max(0, left) + 'px';
  };

  EcgStrip.prototype._updateSr = function () {
    var t = this.totals;
    if (!t) { this.sr.textContent = ''; return; }
    this.sr.textContent = '本场弹幕峰值 ' + t.dm_peak + ' 条/分，SC ' + t.super_chat
      + ' 条合计 ¥' + _n(t.sc_amount) + '，金礼物 ' + t.gift + ' 件'
      + (t.pop_peak ? '，人气峰值 ' + _n(t.pop_peak) : '');
  };

  /* 全量重绘当前视图：高亮窗口区段、人气底面积、弹幕主轨迹、
     礼物粒子、SC 尖峰、悬停十字线，最后写首/中/尾时间刻度 */
  EcgStrip.prototype.redraw = function () {
    var g = this._applyDPR();
    var ctx = this.ctx;
    ctx.clearRect(0, 0, g.w, g.h);
    this._updateSr();
    var s = this.series, n = s.length;
    if (!n) return;

    var w = g.w, h = g.h;
    var padT = 10, padB = 18, padL = 4, padR = 4;
    var plotW = Math.max(1, w - padL - padR);
    var plotH = Math.max(1, h - padT - padB);
    var baseY = padT + plotH;
    var self = this;
    var xOf = function (ts) { return self._xOf(ts, padL, plotW); };

    /* 网格 + 基线 */
    ctx.strokeStyle = cssVar('--border-soft') || 'rgba(255,255,255,.1)';
    ctx.lineWidth = 1;
    for (var gi = 1; gi <= 2; gi++) {
      var gy = Math.round(padT + plotH * gi / 2) + 0.5;
      ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(padL + plotW, gy); ctx.stroke();
    }
    ctx.beginPath();
    ctx.moveTo(padL, baseY + 0.5); ctx.lineTo(padL + plotW, baseY + 0.5); ctx.stroke();

    /* 高能窗口高亮区段 */
    var recCol = this._resolve('--sig-recording');
    for (var wi = 0; wi < this.windows.length; wi++) {
      var win = this.windows[wi];
      if (!win || win.start_ts == null) continue;
      var wx0 = xOf(win.start_ts), wx1 = xOf(win.end_ts == null ? win.start_ts + 60 : win.end_ts);
      if (wx1 < padL || wx0 > padL + plotW) continue;
      var rx0 = Math.max(padL, wx0), rx1 = Math.min(padL + plotW, wx1);
      ctx.fillStyle = hexToRgba(recCol, 0.12);
      ctx.fillRect(rx0, padT, rx1 - rx0, plotH);
    }

    var range = this._visRange();
    var a = range[0], b = range[1];

    /* 全局极值（缩放时尺度稳定） */
    var dmMax = 1, popMax = 1, scMax = 1, giftMax = 1;
    for (var k = 0; k < n; k++) {
      if (s[k].dm > dmMax) dmMax = s[k].dm;
      if (s[k].pop > popMax) popMax = s[k].pop;
      if (s[k].sc_amount_max > scMax) scMax = s[k].sc_amount_max;
      if (s[k].gift_amount > giftMax) giftMax = s[k].gift_amount;
    }

    /* 人气底面积 */
    var popCol = this._resolve('--sig-armed');
    ctx.beginPath();
    ctx.moveTo(xOf(s[a].ts), baseY);
    for (var pi = a; pi < b; pi++) {
      ctx.lineTo(xOf(s[pi].ts), baseY - (s[pi].pop / popMax) * plotH * 0.45);
    }
    ctx.lineTo(xOf(s[b - 1].ts), baseY);
    ctx.closePath();
    ctx.fillStyle = hexToRgba(popCol, 0.12);
    ctx.fill();

    /* 弹幕主轨迹：面积 + 折线 */
    var col = this._resolve('--sig-level');
    ctx.beginPath();
    ctx.moveTo(xOf(s[a].ts), baseY);
    for (var di = a; di < b; di++) {
      ctx.lineTo(xOf(s[di].ts), baseY - (s[di].dm / dmMax) * plotH * 0.85);
    }
    ctx.lineTo(xOf(s[b - 1].ts), baseY);
    ctx.closePath();
    ctx.fillStyle = hexToRgba(col, 0.16);
    ctx.fill();

    ctx.beginPath();
    for (var li = a; li < b; li++) {
      var lx = xOf(s[li].ts), ly = baseY - (s[li].dm / dmMax) * plotH * 0.85;
      if (li === a) ctx.moveTo(lx, ly); else ctx.lineTo(lx, ly);
    }
    ctx.strokeStyle = col; ctx.lineWidth = 1.6; ctx.stroke();

    /* 金礼物粒子 */
    var giftCol = this._resolve('--sig-blocked');
    for (var gp = a; gp < b; gp++) {
      if (!s[gp].gift) continue;
      var gx = xOf(s[gp].ts);
      var ratio = s[gp].gift_amount > 0
        ? Math.log(1 + s[gp].gift_amount) / Math.log(1 + giftMax) : 0;
      ctx.beginPath(); ctx.arc(gx, baseY - ratio * plotH * 0.6 - 2, 2.2, 0, Math.PI * 2);
      ctx.fillStyle = giftCol; ctx.fill();
    }

    /* SC 竖向尖峰（高度=金额对数缩放） */
    var scCol = this._resolve('--sig-peak');
    ctx.strokeStyle = scCol; ctx.lineWidth = 2;
    for (var si = a; si < b; si++) {
      var amt = s[si].sc_amount_max || s[si].sc_amount;
      if (!amt) continue;
      var sx = xOf(s[si].ts);
      var shh = Math.log(1 + amt) / Math.log(1 + scMax) * plotH * 0.92;
      ctx.beginPath(); ctx.moveTo(sx, baseY); ctx.lineTo(sx, baseY - shh); ctx.stroke();
      ctx.beginPath(); ctx.arc(sx, baseY - shh, 1.6, 0, Math.PI * 2);
      ctx.fillStyle = scCol; ctx.fill();
    }

    /* 悬停十字线 */
    if (this._hoverIdx >= a && this._hoverIdx < b) {
      var hx = xOf(s[this._hoverIdx].ts + (this.groupSec || 60) / 2);
      ctx.save();
      ctx.strokeStyle = cssVar('--text-3') || 'rgba(128,128,128,.5)';
      ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hx, padT); ctx.lineTo(hx, baseY); ctx.stroke();
      ctx.restore();
    }

    /* 时间轴首尾刻度（随缩放/平移更新为当前视图范围） */
    var axisCol = cssVar('--text-3') || 'rgba(128,128,128,.7)';
    ctx.fillStyle = axisCol;
    ctx.font = '10px system-ui, -apple-system, "Segoe UI", sans-serif';
    ctx.textBaseline = 'alphabetic';
    var midT = (this.view.t0 + this.view.t1) / 2;
    ctx.textAlign = 'left';
    ctx.fillText(_axis(this.view.t0, midT), padL, h - 4);
    ctx.textAlign = 'right';
    ctx.fillText(_axis(this.view.t1, midT), padL + plotW, h - 4);
    ctx.textAlign = 'center';
    ctx.fillText(_axis(midT, midT), padL + plotW / 2, h - 4);
  };

  /* 销毁实例：停待执行的 rAF、解绑 resize、从全局实例表移除，并摘掉 canvas/tooltip/sr-only */
  EcgStrip.prototype.destroy = function () {
    if (this._resizeRaf) cancelAnimationFrame(this._resizeRaf);
    if (this._ro) this._ro.disconnect();
    else if (this._onResize) window.removeEventListener('resize', this._onResize);
    var idx = instances.indexOf(this);
    if (idx >= 0) instances.splice(idx, 1);
    if (this.canvas.parentNode) this.canvas.parentNode.removeChild(this.canvas);
    if (this.tip.parentNode) this.tip.parentNode.removeChild(this.tip);
    if (this.sr.parentNode) this.sr.parentNode.removeChild(this.sr);
  };

  /* 保留一位小数并按千分位显示 */
  function _n(v) {
    var n = Math.round(Number(v) * 10) / 10;
    return n.toLocaleString('zh-CN');
  }
  /* 秒级时间戳 → HH:MM */
  function _clock(ts) {
    var d = new Date(Number(ts) * 1000);
    var p = function (x) { return (x < 10 ? '0' : '') + x; };
    return p(d.getHours()) + ':' + p(d.getMinutes());
  }
  /* 轴刻度：与视图中心同日只显示 HH:MM，跨日补 MM-DD */
  function _axis(ts, midTs) {
    var d = new Date(Number(ts) * 1000);
    var m = new Date(Number(midTs) * 1000);
    var p = function (x) { return (x < 10 ? '0' : '') + x; };
    var hhmm = p(d.getHours()) + ':' + p(d.getMinutes());
    return (d.getDate() === m.getDate() && d.getMonth() === m.getMonth())
      ? hhmm : p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + hhmm;
  }

  window.PulseStrip = PulseStrip;
  window.EcgStrip = EcgStrip;
})();
