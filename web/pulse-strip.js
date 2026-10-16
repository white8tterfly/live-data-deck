/* PulseStrip —— 信号仪表台脉冲条母组件（C01）
 * 零依赖 Canvas 2D，自动读 CSS 信号色变量、DPR 适配、容器 resize 重绘、
 * 主题切换自动重绘、prefers-reduced-motion 降级、sr-only 摘要。
 * 静态模式只画一次；实时模式走 rAF 维护数据窗。
 */
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

  function PulseStrip(container, opts) {
    if (!container) throw new Error('PulseStrip: container required');
    opts = opts || {};
    this.container = container;
    this.data = Array.isArray(opts.data) ? opts.data.slice() : [];
    this.events = Array.isArray(opts.events) ? opts.events.slice() : [];
    this.mode = opts.mode === 'realtime' ? 'realtime' : 'static';
    this.colorVar = opts.color || 'var(--sig-level)';
    this.windowSeconds = opts.windowSeconds || 60;
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

  PulseStrip.prototype._updateSr = function (maxV) {
    if (maxV == null) {
      var data = this.data;
      maxV = 0;
      for (var i = 0; i < data.length; i++) if (data[i] > maxV) maxV = data[i];
    }
    var peak = Math.round(maxV);
    this.sr.textContent = '近 ' + this.windowSeconds +
      ' 秒信号：峰值 ' + peak + (this.events.length ? '，含 ' + this.events.length + ' 个事件标记' : '');
  };

  PulseStrip.prototype.setData = function (arr) {
    this.data = Array.isArray(arr) ? arr.slice() : [];
    this.redraw();
  };

  PulseStrip.prototype._startLoop = function () {
    var self = this;
    function tick() {
      self.redraw();
      self._rafId = requestAnimationFrame(tick);
    }
    this._rafId = requestAnimationFrame(tick);
  };

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

  window.PulseStrip = PulseStrip;
})();
