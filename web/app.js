/* 弹幕墙 Web UI —— 原生 JS，无构建步骤 */
'use strict';

/* 主题尽早兜底：与 index.html 内联引导脚本同逻辑（内联脚本被某些预览环境拦截时生效，
   正常浏览器中内联脚本已先执行，此处幂等） */
(function applyInitialTheme() {
  let saved = null;
  try { saved = localStorage.getItem('ldd-theme'); } catch (e) { /* 隐私模式忽略 */ }
  const theme = saved === 'light' || saved === 'dark'
    ? saved
    : (window.matchMedia && matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
  document.documentElement.dataset.theme = theme;
})();

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  view: 'dashboard',
  rooms: [], totals: {}, settings: {},
  recording: false, runningCount: 0, liveCount: 0, connectedCount: 0,
  videoActiveCount: 0, videoArmedCount: 0,
  stream: [],          // {kind, room_id, vup, text, uname, medal, ts, ...}
  streamPending: [],
  logs: [],
  logTs: new Set(), // 已入列日志指纹，去重 REST 历史与 WS 补发的重叠部分
  filesKind: 'records',
  files: [],
  videos: { sessions: [], orphans: [], dir: '', ffmpeg: null },
  streamUnread: 0,
  exportTimer: null, // 弹幕版导出进度轮询句柄
};

/* ---------------- 工具 ---------------- */
const fmt = {
  n: (v) => (Number(v) || 0).toLocaleString('zh-CN'),
  hms: (ts) => ts ? new Date(ts * 1000).toTimeString().slice(0, 8) : '--:--:--',
  clock: (ts) => ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—',
  dur: (sec) => {
    sec = Number(sec) || 0;
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
    return h ? `${h}小时${m}分` : m ? `${m}分钟` : `${sec}秒`;
  },
  size: (b) => {
    b = Number(b) || 0;
    if (b < 1024) return b + ' B';
    if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
    if (b < 1073741824) return (b / 1048576).toFixed(2) + ' MB';
    return (b / 1073741824).toFixed(2) + ' GB';
  },
  hm: (ts) => ts ? new Date(ts * 1000).toTimeString().slice(0, 5) : '--:--',
  esc: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
};

function toast(msg, kind = 'info', ms = 3200) {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $('#toastWrap').appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; }, ms - 300);
  setTimeout(() => el.remove(), ms);
}

/* ---------------- 日 / 夜主题 ---------------- */
function currentTheme() {
  return document.documentElement.dataset.theme === 'light' ? 'light' : 'dark';
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('ldd-theme', theme); } catch (e) { /* 隐私模式忽略 */ }
}
(function initThemeToggle() {
  const btn = $('#btnTheme');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const next = currentTheme() === 'light' ? 'dark' : 'light';
    setTheme(next);
    toast(next === 'light' ? '已切换到日间主题' : '已切换到夜间主题', 'ok', 1600);
  });
})();

/* ---------------- C01 全屏监控模式（#monitor，供 C03 复用） ---------------- */
function enterMonitor() {
  document.body.classList.add('monitor-mode');
  stopDsPreview();
  if (location.hash !== '#monitor') {
    history.replaceState(null, '', '#monitor');
  }
}
function exitMonitor() {
  document.body.classList.remove('monitor-mode');
  if (location.hash === '#monitor') {
    history.replaceState(null, '', location.pathname + location.search);
  }
}
(function initMonitorMode() {
  /* 浏览器前进 / 后退：hash 变化同步 monitor 态 */
  window.addEventListener('hashchange', () => {
    if (location.hash === '#monitor') enterMonitor();
    else exitMonitor();
  });
  /* ESC 退出（仅在 monitor 模式下响应，不与其他 ESC 监听冲突） */
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && document.body.classList.contains('monitor-mode')) {
      exitMonitor();
    }
  });
  /* 页面加载时若 hash 为 #monitor，自动进入 */
  if (location.hash === '#monitor') enterMonitor();
})();

/* ---------------- 侧栏收缩 ---------------- */
function toggleSidebar() {
  const collapsed = document.body.classList.toggle('sidebar-collapsed');
  localStorage.setItem('ldd-sidebar-collapsed', collapsed ? '1' : '0');
}
(function initSidebarToggle() {
  const btn = $('#btnSidebarToggle');
  if (btn) btn.addEventListener('click', toggleSidebar);
  /* 恢复上次状态 */
  if (localStorage.getItem('ldd-sidebar-collapsed') === '1') {
    document.body.classList.add('sidebar-collapsed');
  }
})();

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- 视图路由 ---------------- */
const TITLES = { dashboard: '仪表盘', stream: '实时弹幕', rooms: '房间管理',
                 files: '录制文件', analysis: '数据分析',
                 logs: '运行日志', settings: '设置' };

function showView(name) {
  state.view = name;
  $$('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  $('#pageTitle').textContent = TITLES[name];
  if (name === 'stream') { state.streamUnread = 0; updateBadge(); }
  if (name === 'rooms') renderRoomTable();
  if (name === 'dashboard') renderRoomGrid();
  if (name === 'files') {
    if (state.filesKind === 'videos') loadVideos(); else loadFiles();
  }
  if (name === 'analysis') initAnalysisView();
  if (name === 'logs') renderLogs(true);
  if (name === 'settings') { fillSettings(); loadCared(); loadTheme(); loadDs(); }
  else stopDsPreview();   // 离开设置页：停掉弹幕预览定时器，避免在不可见面板中空转
}

/* 导航统一入口：设置页有未保存修改时先弹确认 */
let pendingView = null;
function navTo(name) {
  if (state.view === 'settings' && name !== 'settings' && settingsDirty) {
    pendingView = name;
    $('#settingsGuardMask').hidden = false;
    return;
  }
  showView(name);
}
function closeGuard() { $('#settingsGuardMask').hidden = true; pendingView = null; }
$('#guardStay').addEventListener('click', closeGuard);
$('#guardDiscard').addEventListener('click', () => {
  fillSettings();               // 恢复为已保存的值
  const target = pendingView; closeGuard(); showView(target);
});
$('#guardSave').addEventListener('click', async () => {
  const target = pendingView;
  const ok = await saveSettings();
  if (ok) { closeGuard(); showView(target); }
});
$('#settingsGuardMask').addEventListener('click', (e) => {
  if (e.target.id === 'settingsGuardMask') closeGuard();
});
/* 刷新 / 关闭浏览器标签时的原生提醒 */
window.addEventListener('beforeunload', (e) => {
  if (state.view === 'settings' && settingsDirty) { e.preventDefault(); e.returnValue = ''; }
});

$$('.nav-item').forEach((a) => a.addEventListener('click', () => navTo(a.dataset.view)));
document.addEventListener('click', (e) => {
  const go = e.target.closest('[data-goto]');
  if (go) navTo(go.dataset.goto);
});

/* ---------------- WebSocket ---------------- */
let ws = null, wsBackoff = 1000;
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onopen = () => {
    wsBackoff = 1000;
    $('#wsDot').className = 'ws-dot on';
    $('#wsText').textContent = '实时已连接';
  };
  ws.onmessage = (ev) => {
    const evt = JSON.parse(ev.data);
    onEvent(evt.type, evt.data || {});
  };
  ws.onclose = () => {
    $('#wsDot').className = 'ws-dot off';
    $('#wsText').textContent = '已断开，重连中…';
    setTimeout(connectWS, Math.min(wsBackoff, 8000));
    wsBackoff *= 1.8;
  };
  ws.onerror = () => ws && ws.close();
}

function onEvent(type, data) {
  if (type === 'snapshot' || type === 'state') { applyState(data); return; }
  if (type === 'room_state') { upsertRoom(data); return; }
  if (type === 'danmaku' || type === 'moderation') {
    state.stream.push(data);
    if (state.stream.length > 500) state.stream.splice(0, state.stream.length - 500);
    state.streamPending.push(data);
    scheduleStreamFlush();
    if ($('#view-stream').classList.contains('active') === false) {
      state.streamUnread++; updateBadge();
    }
    return;
  }
  if (type === 'popularity') {
    const r = state.rooms.find((x) => x.room_id === data.room_id);
    if (r) {
      r.popularity_now = data.popularity;
      r.popularity_peak = data.peak;
      if ($('#view-dashboard').classList.contains('active')) renderRoomGrid();
    }
    return;
  }
  if (type === 'log') {
    // WS 建连会补发最近 100 条，与 init 时 REST 拉取的历史重叠：按指纹去重
    const fp = data.ts + '|' + (data.room_id || '') + '|' + data.msg;
    if (state.logTs.has(fp)) return;
    state.logTs.add(fp);
    state.logs.push(data);
    if (state.logs.length > 1000) {
      const old = state.logs.shift();
      state.logTs.delete(old.ts + '|' + (old.room_id || '') + '|' + old.msg);
    }
    if ($('#view-logs').classList.contains('active')) appendLogLine(data);
  }
  if (type === 'video') {
    const panel = $('#filesPanelVideos');
    if (data.status === 'disk_low') {
      toast(`磁盘剩余不足，房间 ${data.vup || data.room_id} 视频已自动停止（弹幕继续）`, 'warn', 6000);
    }
    if (data.status === 'ai_done') {
      toast(`🤖 ${data.vup || ''} 场次 ${data.session || ''} AI 自动解读完成`, 'ok', 5000);
      if (!$('#hlMask').hidden && hlCur.session === data.session) {
        loadAiResult().then(renderHighlights);
      }
    }
    if (panel && !panel.hidden && (data.status === 'session_start'
        || data.status === 'session_end' || data.status === 'disk_low'
        || data.status === 'ai_done')) {
      loadVideos();
    }
  }
}

function applyState(s) {
  state.rooms = s.rooms || [];
  state.totals = s.totals || {};
  state.settings = s.settings || {};
  state.recording = s.recording;
  state.runningCount = s.running_count;
  state.liveCount = s.live_count;
  state.connectedCount = s.connected_count;
  state.videoActiveCount = s.video_active_count || 0;
  state.videoArmedCount = s.video_armed_count || 0;
  renderStats(s);
  renderRoomPicker();
  if ($('#view-dashboard').classList.contains('active')) renderRoomGrid();
  if ($('#view-rooms').classList.contains('active')) renderRoomTable();
}

function upsertRoom(rt) {
  if (!rt || !rt.room_id) return;
  const idx = state.rooms.findIndex((r) => r.room_id === rt.room_id);
  const prev = idx >= 0 ? state.rooms[idx] : null;
  if (idx >= 0) state.rooms[idx] = rt; else state.rooms.push(rt);
  // 开播边沿提醒：该房间已订阅且相位从「非直播中」跳变为「直播中」
  // 必须有 prev：页面刚加载/刚添加房间时已在播的不补弹，避免刷新轰炸
  if (prev && prev.phase !== 'recording' && rt.phase === 'recording' && rt.notify) {
    fireLiveNotification(rt);
  }
  if ($('#view-dashboard').classList.contains('active')) renderRoomGrid();
  if ($('#view-rooms').classList.contains('active')) renderRoomTable();
}

/* ---------------- 开播系统通知 ---------------- */
async function ensureNotifyPermission() {
  if (!('Notification' in window)) {
    toast('当前浏览器不支持系统通知', 'warn', 4000);
    return false;
  }
  if (Notification.permission === 'granted') return true;
  if (Notification.permission === 'denied') {
    toast('通知权限已被拒绝：请点地址栏左侧锁形图标，在网站设置里允许通知后重试', 'warn', 6000);
    return false;
  }
  const res = await Notification.requestPermission();
  if (res !== 'granted') {
    toast('未授予通知权限，开播时不会弹窗提醒', 'warn', 4000);
    return false;
  }
  return true;
}

function fireLiveNotification(r) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  const name = r.anchor_name || r.vup || r.room_id;
  let n;
  try {
    n = new Notification(`🔔 ${name} 开播了`, {
      body: r.title || '直播间已开播，点击进入直播间',
      tag: `ldd-live-${r.room_id}`,
      renotify: true,
    });
  } catch { return; }
  n.onclick = () => {
    window.open(`https://live.bilibili.com/${r.room_id}`, '_blank', 'noopener');
    window.focus();
    n.close();
  };
  setTimeout(() => { try { n.close(); } catch { /* noop */ } }, 20000);
}

/* ---------------- 仪表盘 ---------------- */
const PHASE = {
  recording: ['recording', '直播中'],
  connecting: ['connecting', '连接中'],
  connected_idle: ['connecting', '已连接'],
  waiting: ['waiting', '等待开播'],
  stopped: ['stopped', '已停止'],
};
const PHASE_TITLES = {
  recording: '弹幕 WS 已连接，正在录制弹幕数据',
  connecting: '正在连接弹幕服务器',
  connected_idle: '弹幕已连接（主播未开播）',
  waiting: '弹幕任务运行中，等待开播',
  stopped: '弹幕任务未运行',
};
const QN_LABELS = { 10000: '原画', 400: '蓝光', 250: '超清', 150: '高清', 80: '流畅' };

function renderStats(s) {
  const flash = (el, val) => {
    el.textContent = fmt.n(val);
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  };
  flash($('#statRunning'), s.running_count);
  flash($('#statLive'), s.live_count);
  flash($('#statVideoRooms'), s.video_active_count || 0);
  flash($('#statConnected'), s.connected_count);
  flash($('#statDanmaku'), state.totals.danmaku);
  flash($('#statSC'), state.totals.super_chat);
  flash($('#statMod'), state.totals.moderation);
  flash($('#statRate'), s.rate_per_min);
  const pill = $('#globalPill');
  const videoN = s.video_active_count || 0;
  if (s.recording) {
    pill.classList.add('live');
    $('#globalPillText').textContent =
      `弹幕录制中 · ${s.live_count} 播 / ${s.running_count} 房`
      + (videoN ? ` · 🎥视频 ${videoN} 间` : '');
    pill.title = videoN
      ? `弹幕任务 ${s.running_count} 个运行中，其中 ${videoN} 个房间正在直录视频`
      : `弹幕任务 ${s.running_count} 个运行中（无房间在录视频）`;
  } else {
    pill.classList.remove('live');
    $('#globalPillText').textContent = '未在录制';
    pill.title = '';
  }
}

function roomCard(r) {
  const [cls, label] = PHASE[r.phase] || PHASE.stopped;
  const phaseTitle = PHASE_TITLES[r.phase] || '';
  const c = r.counters || {};

  const item = (k, v, span) => {
    const raw = String(v ?? '');
    return `<div class="rc-mi${span ? ' span2' : ''}"><span class="rc-k">${k}</span>`
      + `<span class="rc-v" title="${fmt.esc(raw)}">${fmt.esc(raw)}</span></div>`;
  };

  const grid = [];
  if (r.running) {
    grid.push(item('运行', fmt.dur(r.uptime_sec)));
    if (r.host_count) grid.push(item('节点', `${r.host_index}/${r.host_count}`));
    if (r.retries) grid.push(item('重连', r.retries));
    grid.push(item('弹幕速率', `${r.rate_per_min} 条/分`));
    if (r.popularity_now) {
      const popVal = fmt.n(r.popularity_now)
        + (r.popularity_peak ? `（峰 ${fmt.n(r.popularity_peak)}）` : '');
      grid.push(item('人气', popVal, true));
    }
  }
  if (r.current_file) {
    grid.push(`<div class="rc-mi span2"><span class="rc-k">弹幕文件</span>
      <button class="rc-filelink" data-openfile="${r.room_id}" type="button"
        title="点击在资源管理器中打开所在目录并选中该文件">
        <span class="rc-fname">${fmt.esc(r.current_file)}</span><span class="rc-fico">📂</span>
      </button></div>`);
  }

  let videoLine = '';
  if (r.video_active) {
    videoLine = `<div class="rc-video on">🎥 <b>视频录制中</b> · ${QN_LABELS[r.video_qn] || r.video_qn}
      <span class="rc-video-sub">${fmt.size(r.video_bytes)} / ${fmt.dur(r.video_sec)}${r.video_segments > 1 ? ` / ${r.video_segments} 段` : ''}</span></div>`;
  } else if (r.record_video) {
    const armText = !state.settings.video_enabled
      ? '全局开关未开，开启后立即起录'
      : (r.phase === 'recording' && r.live_status === 1
        ? '正在起录…' : '开播后自动起录');
    videoLine = `<div class="rc-video arm">🎥 视频待录 · ${armText}</div>`;
  } else {
    // 占位标签：未开视频的卡片保留同一行，与「视频录制中」卡片底部统计行对齐；
    // 只留一行短文案，第二行说明会把下方统计行顶下去导致同行卡片不对齐（用户反馈）
    videoLine = `<div class="rc-video off">🎥 <b>视频未录制</b></div>`;
  }

  const running = r.phase === 'recording';
  const showStats = running || c.danmaku || c.super_chat || c.gift
    || c.emoticon_skipped;

  // 未开播：单行紧凑卡
  if (!running) {
    const errIcon = r.last_error && !r.connected
      ? `<span class="rc-warn" title="${fmt.esc(r.last_error)}">⚠</span>` : '';
    const armIcon = r.record_video
      ? `<span class="rc-vflag${state.settings.video_enabled ? '' : ' off'}"
           title="🎥 视频待录${state.settings.video_enabled ? '，开播后自动起录' : '（全局开关未开）'}">🎥</span>`
      : '';
    const bellIcon = r.notify
      ? '<span class="rc-nflag" title="已订阅开播提醒（开播时弹系统通知）">🔔</span>' : '';
    const displayName = (r.anchor_name || r.vup) + (r.title ? ` · ${r.title}` : '');
    return `
  <div class="room-card compact ${cls}">
    <span class="badge ${cls}"${phaseTitle ? ` title="${phaseTitle}"` : ''}>${label}</span>
    <span class="rc-id">
      <span class="rc-name" title="${fmt.esc(displayName)}">${fmt.esc(r.anchor_name || r.vup)}</span>
      <span class="rc-room">${r.room_id}</span>
    </span>
    <span class="rc-acts">${errIcon}${armIcon}${bellIcon}
      <a class="rc-enter rc-ico-link" href="https://live.bilibili.com/${r.room_id}"
         target="_blank" rel="noopener" title="在 B 站新标签页打开该直播间">↗</a>
      ${r.running
        ? `<button class="btn btn-sm btn-danger" data-stop="${r.room_id}"
             title="停止该房间任务（弹幕与视频直录均停止）">停止</button>`
        : `<button class="btn btn-sm btn-primary" data-start="${r.room_id}"
             title="开始该房间的弹幕录制；视频按🎥勾选联动">开始</button>`}
    </span>
  </div>`;
  }

  const stats = showStats ? `
    <div class="rc-stats">
      <div class="rc-stat"><b>${fmt.n(c.danmaku)}</b>弹幕</div>
      <div class="rc-stat sc"><b>${fmt.n(c.super_chat)}</b>SC</div>
      <div class="rc-stat"><b>${fmt.n(c.gift)}</b>礼物</div>
      <div class="rc-stat"><b>${fmt.n(c.emoticon_skipped)}</b>纯表情过滤</div>
    </div>` : '';

  // 基于 room_id 生成负延迟，避免 WS 更新重建 DOM 时呼吸动画从头开始
  const recDelay = r.running
    ? `style="--rec-delay:${-((Number(r.room_id) % 240) / 100).toFixed(2)}s"`
    : '';

  return `
  <div class="room-card ${r.running ? 'recording' : ''}" data-room-id="${r.room_id}" ${recDelay}>
    <div class="rc-head">
      <span class="badge ${cls}"${phaseTitle ? ` title="${phaseTitle}"` : ''}>${label}</span>
      <span class="rc-name" title="${fmt.esc(r.anchor_name || r.vup)}">${fmt.esc(r.anchor_name || r.vup)}</span>
      <a class="rc-room" href="https://live.bilibili.com/${r.room_id}"
         target="_blank" rel="noopener" title="房间号（点击新标签打开直播间）">${r.room_id}</a>
    </div>
    <div class="rc-title" title="${fmt.esc(r.title)}">${fmt.esc(r.title || '—')}</div>
    ${grid.length ? `<div class="rc-grid">${grid.join('')}</div>` : ''}
    ${r.last_error && !r.connected ? `<div class="rc-err">⚠ ${fmt.esc(r.last_error)}</div>` : ''}
    ${videoLine}
    ${stats}
    ${r.running ? '<div class="rc-pulse" aria-hidden="true"></div>' : ''}
    <div class="rc-foot">
      <a class="rc-enter" href="https://live.bilibili.com/${r.room_id}"
         target="_blank" rel="noopener" title="在 B 站新标签页打开该直播间">进直播间 ↗</a>
      ${r.running
        ? `<button class="btn btn-sm btn-danger" data-stop="${r.room_id}"
             title="停止该房间任务（弹幕与视频直录均停止）">停止</button>`
        : `<button class="btn btn-sm btn-primary" data-start="${r.room_id}"
             title="开始该房间的弹幕录制；视频按🎥勾选联动">开始</button>`}
    </div>
  </div>`;
}

/* C01 脉冲条实例池与数据窗：按房间 id 复用，避免 WS 增量重渲染时重复创建 */
const pulseStrips = new Map();      // roomId → PulseStrip
const pulseHistory = new Map();     // roomId → number[]（弹幕计数滑动窗）
const PULSE_WINDOW = 60;

function syncRoomPulses() {
  const cards = $$('#roomGrid .room-card.recording[data-room-id]');
  const aliveIds = new Set();
  cards.forEach((card) => {
    const id = card.dataset.roomId;
    aliveIds.add(id);
    const room = state.rooms.find((r) => String(r.room_id) === String(id));
    if (!room) return;
    const val = Number(room.rate_per_min) || 0;
    let hist = pulseHistory.get(id) || [];
    hist.push(val);
    if (hist.length > PULSE_WINDOW) hist = hist.slice(-PULSE_WINDOW);
    pulseHistory.set(id, hist);
    /* 归一化到 [0,1]：除以历史最大值，至少 1 防除零 */
    const maxV = Math.max(1, ...hist);
    const norm = hist.map((v) => v / maxV);
    let strip = pulseStrips.get(id);
    const container = card.querySelector('.rc-pulse');
    if (!container) return;
    if (!strip || strip.container !== container) {
      if (strip) strip.destroy();
      strip = new PulseStrip(container, {
        data: norm, mode: 'static', color: 'var(--sig-recording)',
      });
      pulseStrips.set(id, strip);
    } else {
      strip.setData(norm);
    }
  });
  /* 销毁已离开 DOM 的实例 */
  for (const [id, strip] of pulseStrips) {
    if (!aliveIds.has(id)) { strip.destroy(); pulseStrips.delete(id); pulseHistory.delete(id); }
  }
}

/* 房间卡显示签名：影响卡片内容的关键字段拼接，用于增量 diff */
function roomSig(r) {
  const c = r.counters || {};
  return [
    r.phase, r.running, r.connected, r.live_status,
    r.anchor_name || r.vup, r.room_id, r.title,
    r.current_file, r.host_index, r.host_count, r.retries,
    r.rate_per_min, r.popularity_now, r.popularity_peak,
    r.uptime_sec, r.last_error, r.video_active, r.record_video, r.notify,
    c.danmaku, c.super_chat, c.gift, c.emoticon_skipped,
  ].join('|');
}

function renderRoomGrid() {
  const grid = $('#roomGrid');
  const empty = $('#dashboardEmpty');
  if (!state.rooms.length) { grid.innerHTML = ''; empty.hidden = false; syncRoomPulses(); return; }
  empty.hidden = true;

  // 直播中在前、未开播/未运行在后；组内保持原有顺序
  const live = state.rooms.filter((r) => r.phase === 'recording');
  const idle = state.rooms.filter((r) => r.phase !== 'recording');

  // 增量更新：按 room_id 复用已有卡片 DOM，签名相同则跳过重建
  // 避免 5s state 推送时全量重建导致脉冲条闪烁、呼吸动画打断
  const groups = [
    { key: 'live', label: '直播中', rooms: live },
    { key: 'idle', label: '未开播', rooms: idle },
  ].filter((g) => g.rooms.length);

  // 收集现有卡片，按 room_id 索引
  const existing = new Map();
  grid.querySelectorAll('.room-card[data-room-id]').forEach((el) => {
    existing.set(el.dataset.roomId, el);
  });

  const frag = document.createDocumentFragment();
  groups.forEach((g) => {
    const heading = document.createElement('div');
    heading.className = `room-group ${g.key}`;
    heading.dataset.group = g.key;
    const h = document.createElement('div');
    h.className = 'rg-head';
    h.innerHTML = `<i class="rg-dot ${g.key}"></i>${g.label}`
      + `<span class="rg-count">${g.rooms.length}</span>`;
    heading.appendChild(h);
    const body = document.createElement('div');
    body.className = 'room-grid';
    g.rooms.forEach((r) => {
      const sig = roomSig(r);
      let card = existing.get(String(r.room_id));
      if (card && card.dataset.sig === sig) {
        // 数据未变，直接复用
      } else {
        const tmp = document.createElement('div');
        tmp.innerHTML = roomCard(r);
        card = tmp.firstElementChild;
      }
      if (card) {
        card.dataset.sig = sig;
        body.appendChild(card);
      }
    });
    heading.appendChild(body);
    frag.appendChild(heading);
  });

  grid.innerHTML = '';
  grid.appendChild(frag);
  syncRoomPulses();
}

$('#roomGrid').addEventListener('click', async (e) => {
  const start = e.target.closest('[data-start]');
  const stop = e.target.closest('[data-stop]');
  const openFile = e.target.closest('[data-openfile]');
  try {
    if (openFile) {
      const res = await api(`/api/rooms/${openFile.dataset.openfile}/open-folder`,
        { method: 'POST' });
      if (res && res.path) toast(`已打开：${res.path}`, 'ok', 3600);
      return;
    }
    if (start) await api(`/api/rooms/${start.dataset.start}/start`, { method: 'POST' });
    if (stop) await api(`/api/rooms/${stop.dataset.stop}/stop`, { method: 'POST' });
  } catch (err) { toast(err.message, 'err'); }
});

/* ---------------- 房间管理 ---------------- */
function renderRoomTable() {
  const tbody = $('#roomTable');
  $('#roomCount').textContent = state.rooms.length ? `共 ${state.rooms.length} 间` : '';
  $('#roomsEmpty').hidden = state.rooms.length > 0;
  tbody.innerHTML = state.rooms.map((r) => {
    const [cls, label] = PHASE[r.phase] || PHASE.stopped;
    const phaseTitle = PHASE_TITLES[r.phase] || '';
    let vDot = '', vTitle;
    if (r.video_active) {
      vDot = '<i class="v-dot on" title="视频正在录制"></i>';
      vTitle = '该房间正在直录视频；取消勾选后本场结束即停止（弹幕不受影响）';
    } else if (r.record_video) {
      vDot = state.settings.video_enabled
        ? '<i class="v-dot armed" title="视频待录"></i>'
        : '<i class="v-dot blocked" title="全局视频开关未开"></i>';
      vTitle = state.settings.video_enabled
        ? '已勾选视频直录：正在直播立即起录，未开播则开播自动起录'
        : '已勾选，但设置页「视频直录全局总开关」未开启；开启后正在直播立即起录';
    } else {
      vTitle = '为该房间单独开启视频直录（全局总开关在设置页；弹幕录制与此无关）';
    }
    return `<tr data-room="${r.room_id}">
      <td><span class="badge ${cls}"${phaseTitle ? ` title="${phaseTitle}"` : ''}>${label}</span></td>
      <td class="mono"><a href="https://live.bilibili.com/${r.room_id}"
          target="_blank" rel="noopener">${r.room_id}</a></td>
      <td>${fmt.esc(r.vup)}${r.anchor_name && r.anchor_name !== r.vup
          ? `<div class="hint" style="margin:0">${fmt.esc(r.anchor_name)}</div>` : ''}</td>
      <td title="启用后启动服务/点开始时自动连接弹幕 WebSocket 录制弹幕数据">
        <input type="checkbox" ${r.enabled ? 'checked' : ''} data-enabled="${r.room_id}"></td>
      <td><span class="v-cell">${vDot}<input type="checkbox" ${r.record_video ? 'checked' : ''}
          data-video="${r.room_id}" title="${vTitle}"></span></td>
      <td title="该房间开播时弹浏览器系统通知（需本页面保持打开并允许通知权限）">
        <input type="checkbox" ${r.notify ? 'checked' : ''} data-notify="${r.room_id}"></td>
      <td>${fmt.n((r.counters || {}).danmaku)}</td>
      <td>${(() => {
        const n = roomOverrideCount(r);
        return `<button class="btn btn-sm cfg-btn ${n ? 'custom' : ''}" data-cfg="${r.room_id}"
          title="该房间专属录制设置（SC/礼物/房管存档、隐私打码、画质、AI 自动解读）">
          ⚙ 设置${n ? `<span class="cfg-badge">${n}</span>` : ''}</button>`;
      })()}</td>
      <td>
        ${r.running
          ? `<button class="btn btn-sm btn-danger" data-stop2="${r.room_id}"
               title="停止该房间的弹幕任务（视频直录一并停止）">停止</button>`
          : `<button class="btn btn-sm btn-primary" data-start2="${r.room_id}"
               title="开始该房间的弹幕录制；视频按左侧🎥勾选联动">开始</button>`}
        <a class="btn btn-sm" href="https://live.bilibili.com/${r.room_id}"
           target="_blank" rel="noopener" title="在 B 站新标签页打开该直播间">进直播间</a>
        <button class="btn btn-sm btn-danger" data-del="${r.room_id}">删除</button>
      </td>
    </tr>`;
  }).join('');
}

$('#btnAddRoom').addEventListener('click', async () => {
  const room_id = Number($('#addRoomId').value.trim());
  const vup = $('#addVup').value.trim();
  const enabled = $('#addEnabled').checked;
  const record_video = $('#addRecordVideo').checked;
  let notify = $('#addNotify').checked;
  if (!room_id || room_id <= 0) return toast('请填写正确的直播间号', 'warn');
  try {
    if (notify && !(await ensureNotifyPermission())) {
      $('#addNotify').checked = false;
      notify = false;
    }
    await api('/api/targets', {
      method: 'POST',
      body: { room_id, vup, enabled, record_video, notify },
    });
    $('#addRoomId').value = ''; $('#addVup').value = '';
    $('#addRecordVideo').checked = false;
    $('#addNotify').checked = false;
    toast(`房间 ${room_id} 已添加${enabled ? '，弹幕录制已开始' : ''}`
      + (record_video ? '；已勾选该房间视频直录（全局开关开启时生效）' : '')
      + (notify ? '；开播时将弹系统通知提醒' : ''), 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$('#roomTable').addEventListener('click', async (e) => {
  try {
    const s = e.target.closest('[data-start2]');
    const x = e.target.closest('[data-stop2]');
    const d = e.target.closest('[data-del]');
    const c = e.target.closest('[data-cfg]');
    if (c) { openRoomCfg(Number(c.dataset.cfg)); return; }
    if (s) await api(`/api/rooms/${s.dataset.start2}/start`, { method: 'POST' });
    if (x) await api(`/api/rooms/${x.dataset.stop2}/stop`, { method: 'POST' });
    if (d) {
      if (!confirm(`确认删除房间 ${d.dataset.del}？已录制的文件不会被删除。`)) return;
      await api(`/api/targets/${d.dataset.del}`, { method: 'DELETE' });
      toast('房间已删除', 'ok');
    }
  } catch (err) { toast(err.message, 'err'); }
});

$('#roomTable').addEventListener('change', async (e) => {
  const cb = e.target.closest('[data-enabled]');
  const vb = e.target.closest('[data-video]');
  const nb = e.target.closest('[data-notify]');
  try {
    if (cb) {
      await api(`/api/targets/${cb.dataset.enabled}`,
        { method: 'PUT', body: { enabled: cb.checked } });
      toast(cb.checked
        ? '已启用该房间的弹幕录制（启动服务/点全部开始时自动连接）'
        : '已停用该房间的弹幕录制（运行中的任务需手动停止）', 'ok');
    }
    if (vb) {
      await api(`/api/targets/${vb.dataset.video}`,
        { method: 'PUT', body: { record_video: vb.checked } });
      if (vb.checked) {
        if (!state.settings.video_enabled) {
          toast('已开启视频直录。还需在设置页打开全局视频开关：开启后正在直播立即起录，未开播则开播自动起录', 'ok', 4200);
        } else {
          const room = (state.rooms || []).find(
            (r) => String(r.room_id) === String(vb.dataset.video));
          toast(room && room.running && room.live_status === 1
            ? '已开启视频直录，正在直播将立即起录（约 2 秒内）'
            : '已开启视频直录，开播后自动起录', 'ok', 3600);
        }
      } else {
        toast('该房间视频直录已关闭（正在录制的场次结束后停止）', 'ok', 3000);
      }
    }
    if (nb) {
      // 勾选即请求系统通知权限；被拒/不支持则回退勾选，不调后端
      if (nb.checked && !(await ensureNotifyPermission())) {
        nb.checked = false;
        return;
      }
      await api(`/api/targets/${nb.dataset.notify}`,
        { method: 'PUT', body: { notify: nb.checked } });
      toast(nb.checked
        ? '🔔 该房间开播时将弹出系统通知（需保持本页面打开）'
        : '已关闭该房间的开播提醒', 'ok', 3200);
    }
  } catch (err) {
    toast(err.message, 'err');
    if (cb) cb.checked = !cb.checked;
    if (vb) vb.checked = !vb.checked;
    if (nb) nb.checked = !nb.checked;
  }
});

/* ---------------- 房间级录制设置 ---------------- */
// 画质档位与后端 VIDEO_QN_CHOICES 保持一致
const VIDEO_QN_OPTIONS = [
  [10000, '原画'], [400, '蓝光'], [250, '超清'], [150, '高清'], [80, '流畅'],
];
const QN_LABEL = Object.fromEntries(VIDEO_QN_OPTIONS);

// 布尔覆盖项的元数据（顺序即弹窗顺序；key 与后端 overrides 一致）
const ROOM_BOOL_GROUPS = [
  { title: '录制内容', items: [
    { key: 'record_sc', label: '录制 SC（醒目留言）',
      sub: '关闭后本房间的 SC 不入弹幕记录' },
    { key: 'record_gifts', label: '录制礼物事件',
      sub: '礼物信息密度低，全局默认关' },
    { key: 'record_moderation', label: '房管处置存档',
      sub: '房管拉黑/禁言某人时，自动保存该用户近 30 分钟发过的弹幕' },
  ] },
  { title: '弹幕内容处理', items: [
    { key: 'filter_pure_emoticon', label: '过滤纯大表情弹幕' },
    { key: 'anonymize', label: 'UID / 用户名匿名',
      sub: '文件里不保存真实 UID，用户名只保留姓（如 张***）',
      warnOff: '关闭后文件将保存真实 UID 与用户名' },
    { key: 'sanitize_text', label: '隐私内容自动打码',
      sub: '自动替换弹幕里的链接 / @用户 / QQ / UID / 手机号',
      warnOff: '关闭后这些隐私内容将原样保存' },
  ] },
  { title: 'AI', items: [
    { key: 'llm_auto_analyze', label: '场次结束自动解读',
      sub: '该房间视频场次正常下播后自动跑 LLM 战报' },
  ] },
];
const ROOM_BOOL_KEYS = ROOM_BOOL_GROUPS.flatMap((g) => g.items.map((i) => i.key));
const ROOM_OVR_KEYS = ROOM_BOOL_KEYS.concat(['video_qn']);

// 统计某房间已自定义的覆盖项数量（用于行内徽标）
function roomOverrideCount(r) {
  const ov = r && r.overrides;
  if (!ov) return 0;
  return ROOM_OVR_KEYS.reduce((n, k) => n + (ov[k] !== null && ov[k] !== undefined ? 1 : 0), 0);
}

const roomCfg = { roomId: null, draft: {} };

function openRoomCfg(roomId) {
  const r = state.rooms.find((x) => x.room_id === roomId);
  if (!r) return;
  roomCfg.roomId = roomId;
  roomCfg.draft = JSON.parse(JSON.stringify(r.overrides || {}));
  for (const k of ROOM_OVR_KEYS) if (roomCfg.draft[k] === undefined) roomCfg.draft[k] = null;
  $('#roomCfgTitle').textContent =
    `房间设置 · ${r.vup || roomId}（${roomId}）`;
  $('#roomCfgBody').innerHTML = renderRoomCfgBody(r);
  bindRoomCfgBody();
  $('#roomCfgMask').hidden = false;
}

function closeRoomCfg() { $('#roomCfgMask').hidden = true; }

function renderRoomCfgBody(r) {
  const s = state.settings || {};
  const esc = fmt.esc;
  const tri = (item) => {
    const key = item.key;
    const cur = roomCfg.draft[key]; // null=跟随全局, true/false=自定义
    const gv = !!s[key];
    const selCls = (val) => (cur === val ? 'sel' : '');
    return `
      <div class="cfg-row" data-bool-row="${key}">
        <div class="cfg-label">${esc(item.label)}
          ${item.sub ? `<span class="cfg-sub">${esc(item.sub)}</span>` : ''}</div>
        <div class="cfg-tri" data-tri="${key}">
          <button type="button" data-val="null" class="${cur === null ? 'sel' : ''}"
            title="使用设置页的全局开关值">跟随全局（当前：${gv ? '开' : '关'}）</button>
          <button type="button" data-val="true" class="${selCls(true)} ${cur === true ? 'on' : ''}">开</button>
          <button type="button" data-val="false" class="${selCls(false)} ${cur === false ? 'off' : ''}">关</button>
        </div>
        ${item.warnOff ? `<span class="cfg-warn ${cur === false ? 'show' : ''}">⚠ ${esc(item.warnOff)}</span>` : ''}
      </div>`;
  };
  const groups = ROOM_BOOL_GROUPS.map((g) => `
    <div class="cfg-group-title">${esc(g.title)}</div>
    ${g.items.map(tri).join('')}`).join('');
  // 画质
  const qnCur = roomCfg.draft.video_qn;
  const qnGv = s.video_qn;
  const qnOpts = VIDEO_QN_OPTIONS.map(([v, label]) =>
    `<option value="${v}" ${qnCur === v ? 'selected' : ''}>${label}（${v}）</option>`).join('');
  return `
    <div class="cfg-hint">
      每项默认「跟随全局」，使用设置页的全局开关值（全局开关也是<b>新房间的默认值</b>）；
      在此改成开/关即只对本房间生效。开关类改动即时生效（视频开关打开时正在直播最迟 2 秒起录），画质在下次取流 / 重连时生效。
      弹幕启用、视频直录、开播提醒仍在房间列表行内直接切换。
    </div>
    <div class="cfg-vup-row">
      <label>名称 / 保存目录</label>
      <input class="input" id="roomCfgVup" type="text"
             value="${esc(r.vup || '')}" placeholder="留空则使用 room_房间号">
    </div>
    <div class="cfg-sub" style="margin:-2px 0 0 187px;color:var(--text-3)">
      改名只影响此后新文件的保存目录，已录制的历史文件仍留在旧目录中
    </div>
    ${groups}
    <div class="cfg-group-title">视频画质</div>
    <div class="cfg-row">
      <div class="cfg-label">清晰度
        <span class="cfg-sub">仅影响本房间视频直录；原画通常需有效 SESSDATA</span></div>
      <select class="cfg-select" id="roomCfgQn">
        <option value="null" ${qnCur === null ? 'selected' : ''}>跟随全局（当前：${esc(QN_LABEL[qnGv] || qnGv)}）</option>
        ${qnOpts}
      </select>
    </div>`;
}

function bindRoomCfgBody() {
  // 三态分段按钮
  $('#roomCfgBody').querySelectorAll('[data-tri]').forEach((box) => {
    box.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-val]');
      if (!btn) return;
      const key = box.dataset.tri;
      const val = btn.dataset.val;
      roomCfg.draft[key] = val === 'null' ? null : val === 'true';
      // 重绘这一行，保证选中态与隐私警示同步
      const row = box.closest('[data-bool-row]');
      const item = ROOM_BOOL_GROUPS.flatMap((g) => g.items).find((i) => i.key === key);
      const tmp = document.createElement('div');
      tmp.innerHTML = (() => {
        const s = state.settings || {};
        const cur = roomCfg.draft[key];
        const gv = !!s[key];
        return `<div class="cfg-row" data-bool-row="${key}">
          <div class="cfg-label">${fmt.esc(item.label)}
            ${item.sub ? `<span class="cfg-sub">${fmt.esc(item.sub)}</span>` : ''}</div>
          <div class="cfg-tri" data-tri="${key}">
            <button type="button" data-val="null" class="${cur === null ? 'sel' : ''}">跟随全局（当前：${gv ? '开' : '关'}）</button>
            <button type="button" data-val="true" class="${cur === true ? 'sel on' : ''}">开</button>
            <button type="button" data-val="false" class="${cur === false ? 'sel off' : ''}">关</button>
          </div>
          ${item.warnOff ? `<span class="cfg-warn ${cur === false ? 'show' : ''}">⚠ ${fmt.esc(item.warnOff)}</span>` : ''}
        </div>`;
      })();
      row.replaceWith(tmp.firstElementChild);
      bindRoomCfgBody(); // 按钮是新 DOM，重新绑定（弹窗内行数少，开销可忽略）
    });
  });
}

async function saveRoomCfg(resetAll) {
  const overrides = resetAll
    ? Object.fromEntries(ROOM_OVR_KEYS.map((k) => [k, null]))
    : { ...roomCfg.draft };
  const qnRaw = resetAll ? 'null' : $('#roomCfgQn').value;
  overrides.video_qn = qnRaw === 'null' ? null : Number(qnRaw);
  const body = { overrides };
  if (!resetAll) {
    body.vup = $('#roomCfgVup').value.trim();
  }
  try {
    await api(`/api/targets/${roomCfg.roomId}`, { method: 'PUT', body });
    toast(resetAll ? '已恢复为全部跟随全局' : '房间设置已保存', 'ok');
    closeRoomCfg();
  } catch (err) { toast(err.message, 'err'); }
}

$('#roomCfgClose').addEventListener('click', closeRoomCfg);
$('#roomCfgCancel').addEventListener('click', closeRoomCfg);
$('#roomCfgSave').addEventListener('click', () => saveRoomCfg(false));
$('#roomCfgInheritAll').addEventListener('click', () => saveRoomCfg(true));
$('#roomCfgMask').addEventListener('click', (e) => {
  if (e.target.id === 'roomCfgMask') closeRoomCfg();
});

/* ---------------- B 站关注列表导入 ---------------- */
const followState = {
  items: [], loaded: false, loading: false, selected: new Set(),
  page: 0, total: 0, totalPage: 0, hasMore: false,
  loadingMore: false, moreError: '',
};

function followVisible() {
  const q = $('#followSearch').value.trim().toLowerCase();
  const liveOnly = $('#followLiveOnly').checked;
  const hideAdded = $('#followHideAdded').checked;
  return followState.items.filter((it) => {
    if (hideAdded && it.added) return false;
    if (liveOnly && it.live_status !== 1) return false;
    if (q && !`${it.uname} ${it.room_id} ${it.title}`.toLowerCase().includes(q)) {
      return false;
    }
    return true;
  });
}

function followBadge(it) {
  if (it.live_status === 1) return '<span class="badge recording">直播中</span>';
  if (it.live_status === 2) return '<span class="badge connecting">轮播中</span>';
  return '<span class="badge stopped">未开播</span>';
}

function syncFollowFoot(list) {
  const selectable = list.filter((i) => !i.added);
  const sel = followState.selected.size;
  $('#followSelInfo').textContent = sel
    ? `已选 ${sel} 间，加入后错峰启动弹幕录制（视频需逐房间勾选）` : '';
  $('#followConfirm').disabled = sel === 0;
  $('#followSelectLive').disabled =
    !selectable.some((i) => i.live_status === 1);
}

function renderFollow() {
  const box = $('#followContent');
  if (followState.loading) {
    box.innerHTML = '<div class="muted follow-hint">正在读取关注列表（第 1 页）…</div>';
    $('#followStats').textContent = '';
    renderFollowMore();
    syncFollowFoot([]);
    return;
  }
  if (!followState.loaded) {  // 错误提示已直接渲染在列表区
    renderFollowMore();
    return;
  }
  // 接口 live_count 字段实测恒为 0，直播中数仅统计当前已加载部分
  const liveLoaded = followState.items.filter((i) => i.live_status === 1).length;
  $('#followStats').textContent =
    `共 ${followState.total} 位 · 当前已显示中 ${liveLoaded} 位直播中`;
  if (!followState.items.length) {
    box.innerHTML = '<div class="muted follow-hint">这个账号的关注列表为空</div>';
    renderFollowMore();
    syncFollowFoot([]);
    return;
  }
  const list = followVisible();
  if (!list.length) {
    box.innerHTML = '<div class="muted follow-hint">没有符合筛选条件的主播（目标可能在尚未加载的页，可继续点「加载更多」）</div>';
    syncFollowFoot(list);
    renderFollowMore();
    return;
  }
  box.innerHTML = list.map((it) => {
    const face = it.face
      ? `<img class="follow-face" loading="lazy" src="${fmt.esc(it.face)}" alt=""
              onerror="this.remove()">`
      : '<span class="follow-face follow-face-ph">📺</span>';
    return `<label class="follow-item${it.added ? ' is-added' : ''}">
      <input type="checkbox" data-follow="${it.room_id}"
             ${followState.selected.has(it.room_id) ? 'checked' : ''}
             ${it.added ? 'disabled' : ''}>
      ${face}
      <span class="follow-main">
        <span class="follow-line1">
          <b>${fmt.esc(it.uname || `房间${it.room_id}`)}</b>
          ${followBadge(it)}
          <span class="muted mono" style="margin-left:0">${it.room_id}</span>
        </span>
        ${it.title ? `<span class="follow-title" title="${fmt.esc(it.title)}">${fmt.esc(it.title)}</span>` : ''}
      </span>
      ${it.added ? '<span class="tag">已添加</span>' : ''}
    </label>`;
  }).join('');
  syncFollowFoot(list);
  renderFollowMore();
}

function renderFollowMore() {
  const bar = $('#followMoreBar');
  if (!bar) return;
  if (!followState.loaded) { bar.hidden = true; return; }
  bar.hidden = false;
  const shown = followState.items.length;
  const prog = `已显示 ${shown} / 共 ${followState.total} 位`;
  if (followState.loadingMore) {
    bar.innerHTML = `<span class="muted">${prog} · 正在加载下一页…</span>`;
    return;
  }
  if (followState.moreError) {
    bar.innerHTML = `<span class="muted">${prog}</span>
      <span class="follow-more-err">${fmt.esc(followState.moreError)}</span>
      <button class="btn btn-sm" id="followMoreBtn" type="button">重试</button>`;
    return;
  }
  if (followState.hasMore) {
    bar.innerHTML = `<span class="muted">${prog}</span>
      <button class="btn btn-sm" id="followMoreBtn" type="button">加载更多（下一页 ${followState.page + 1}/${followState.totalPage}）</button>`;
    return;
  }
  bar.innerHTML = `<span class="muted">已全部加载（共 ${followState.total} 位）</span>`;
}

async function loadFollow() {
  followState.loading = true;
  followState.loaded = false;
  renderFollow();
  try {
    const r = await api('/api/followings?page=1');
    followState.items = r.items || [];
    followState.page = r.page || 1;
    followState.total = r.total || 0;
    followState.totalPage = r.total_page || 1;
    followState.hasMore = !!r.has_more;
    followState.moreError = '';
    followState.loaded = true;
    const ids = new Set(followState.items.map((i) => i.room_id));
    followState.selected = new Set(
      [...followState.selected].filter((id) => ids.has(id)));
  } catch (err) {
    followState.items = [];
    followState.loaded = false;
    $('#followContent').innerHTML =
      `<div class="follow-err"><div>❌ ${fmt.esc(err.message)}</div>
       <div class="hint">关注列表属于账号私有数据，需要有效登录态；弹幕录制不受影响。</div>
       <button class="btn btn-sm btn-primary" data-goto="settings"
               id="followGoSettings" type="button">去设置 SESSDATA</button></div>`;
    $('#followStats').textContent = '';
    syncFollowFoot([]);
  } finally {
    followState.loading = false;
    renderFollow();
  }
}

async function loadMoreFollow() {
  if (followState.loadingMore || followState.loading
      || !followState.hasMore) return;
  followState.loadingMore = true;
  followState.moreError = '';
  renderFollowMore();
  try {
    const next = followState.page + 1;
    const r = await api(`/api/followings?page=${next}`);
    const existing = new Set(followState.items.map((i) => i.room_id));
    (r.items || []).forEach((it) => {
      if (!existing.has(it.room_id)) {
        existing.add(it.room_id);
        followState.items.push(it);
      }
    });
    followState.page = r.page || next;
    followState.total = r.total || followState.total;
    followState.totalPage = r.total_page || followState.totalPage;
    followState.hasMore = !!r.has_more;
    renderFollow();
  } catch (err) {
    followState.moreError = err.message || '加载失败';
  } finally {
    followState.loadingMore = false;
    renderFollowMore();
  }
}

$('#btnImportFollowing').addEventListener('click', () => {
  $('#followMask').hidden = false;
  if (!followState.loaded && !followState.loading) loadFollow();
  else renderFollow();
});
$('#followClose').addEventListener('click', () => { $('#followMask').hidden = true; });
$('#followReload').addEventListener('click', () => { if (!followState.loading) loadFollow(); });
$('#followMoreBar').addEventListener('click', (e) => {
  if (e.target.closest('#followMoreBtn')) loadMoreFollow();
});
/* 滚动接近底部自动加载下一页（按钮兜底仍在） */
$('#followContent').addEventListener('scroll', () => {
  const el = $('#followContent');
  if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) loadMoreFollow();
}, { passive: true });
['followSearch', 'followLiveOnly', 'followHideAdded'].forEach((id) =>
  $('#' + id).addEventListener('input', renderFollow));

$('#followContent').addEventListener('change', (e) => {
  const cb = e.target.closest('[data-follow]');
  if (!cb) return;
  const id = Number(cb.dataset.follow);
  if (cb.checked) followState.selected.add(id);
  else followState.selected.delete(id);
  syncFollowFoot(followVisible());
});

$('#followContent').addEventListener('click', (e) => {
  if (e.target.closest('#followGoSettings')) $('#followMask').hidden = true;
});

$('#followSelectLive').addEventListener('click', () => {
  const liveIds = followVisible()
    .filter((i) => !i.added && i.live_status === 1)
    .map((i) => i.room_id);
  const allOn = liveIds.length > 0
    && liveIds.every((id) => followState.selected.has(id));
  liveIds.forEach((id) => allOn
    ? followState.selected.delete(id)
    : followState.selected.add(id));
  renderFollow();
});

$('#followConfirm').addEventListener('click', async () => {
  const chosen = followState.items
    .filter((i) => followState.selected.has(i.room_id) && !i.added);
  if (!chosen.length) return;
  const btn = $('#followConfirm');
  const oldText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '加入中…';
  try {
    const r = await api('/api/targets/bulk', {
      method: 'POST',
      body: {
        items: chosen.map((i) => ({ room_id: i.room_id, vup: i.uname || '' })),
        enabled: true,
      },
    });
    const addedIds = new Set(r.added.map((a) => a.room_id));
    followState.items.forEach((i) => { if (addedIds.has(i.room_id)) i.added = true; });
    followState.selected = new Set(
      [...followState.selected].filter((id) => !addedIds.has(id)));
    renderFollow();
    renderRoomTable();
    if (r.added.length) {
      toast(`已加入 ${r.added.length} 个房间，正在错峰开始弹幕录制`
        + `（视频默认不开，需在房间表逐房间勾选🎥）`
        + (r.skipped.length ? `，跳过已存在 ${r.skipped.length} 个` : ''), 'ok', 4600);
    } else {
      toast('所选房间均已在录制目标中', 'warn');
    }
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    btn.textContent = oldText;
  }
});

/* ---------------- 全局控制 ---------------- */
$('#btnStartAll').addEventListener('click', async () => {
  try {
    const r = await api('/api/start-all', { method: 'POST' });
    toast(`已启动 ${r.started.length} 个房间的弹幕录制（错峰连接中；视频按各房间🎥勾选联动）`, 'ok', 4200);
  } catch (err) { toast(err.message, 'err'); }
});
$('#btnStopAll').addEventListener('click', async () => {
  try {
    await api('/api/stop-all', { method: 'POST' });
    toast('已停止全部任务（弹幕录制与视频直录均停止）', 'ok');
  }
  catch (err) { toast(err.message, 'err'); }
});

/* ---------------- 实时弹幕 ---------------- */
/* 事件类型筛选：具体房间独立记忆；「全部房间」视图下的修改一次性同步到
   所有房间（key '' 同时作为未配置房间的全局兜底）；
   未配置过的房间默认四类全选，显式存空数组则表示该房间全部隐藏 */
const STREAM_KINDS = ['danmaku', 'super_chat', 'gift', 'moderation'];
let streamProfiles = {};
function loadStreamProfiles() {
  try { streamProfiles = JSON.parse(localStorage.getItem('ldd-stream-kinds') || '{}') || {}; }
  catch (e) { streamProfiles = {}; }
}
function saveStreamProfiles() {
  try { localStorage.setItem('ldd-stream-kinds', JSON.stringify(streamProfiles)); } catch (e) { /* 隐私模式忽略 */ }
}
loadStreamProfiles();

/* 房间多选：空集合 = 全部房间；持久化为 id 数组。
   首次加载兼容旧版单选键 ldd-stream-room。 */
const streamSel = new Set();
function loadStreamSelection() {
  try {
    const raw = localStorage.getItem('ldd-stream-rooms');
    if (raw) {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        arr.map(Number).filter((n) => n > 0).forEach((n) => streamSel.add(n));
      }
    } else {
      const old = localStorage.getItem('ldd-stream-room');
      if (old) {
        const n = Number(old);
        if (n > 0) streamSel.add(n);
        localStorage.removeItem('ldd-stream-room');
        saveStreamSelection();  // 立即落盘新键，避免未操作就再次刷新时丢失迁移结果
      }
    }
  } catch (e) { /* ignore */ }
}
function saveStreamSelection() {
  try {
    localStorage.setItem('ldd-stream-rooms', JSON.stringify([...streamSel]));
  } catch (e) { /* ignore */ }
}
loadStreamSelection();

function streamKindInputs() {
  return $$('#streamKindFilters input[type=checkbox]');
}
/* 当前选择对应的事件类型配置：
   全部房间 → 全局兜底；单房间 → 该房间记忆（无记忆则跟随全局）；
   多房间 → 取各房间配置交集，部分开启的类型以 indeterminate（半选）呈现 */
function streamKindState() {
  const ids = [...streamSel].map(String);
  const profileOf = (k) => {
    let s = streamProfiles[k];
    if (!Array.isArray(s) && Array.isArray(streamProfiles[''])) s = streamProfiles[''];
    return new Set(Array.isArray(s) ? s : STREAM_KINDS);
  };
  if (ids.length === 0) {
    return { on: profileOf(''), mixed: new Set() };
  }
  if (ids.length === 1) {
    return { on: profileOf(ids[0]), mixed: new Set() };
  }
  const sets = ids.map(profileOf);
  return {
    on: new Set(STREAM_KINDS.filter((k) => sets.every((s) => s.has(k)))),
    mixed: new Set(STREAM_KINDS.filter((k) =>
      !sets.every((s) => s.has(k)) && sets.some((s) => s.has(k)))),
  };
}
function applyStreamProfile() {
  const { on, mixed } = streamKindState();
  streamKindInputs().forEach((i) => {
    i.checked = on.has(i.value);
    i.indeterminate = mixed.has(i.value);
  });
}
function persistStreamProfile() {
  const kinds = streamKindInputs().filter((i) => i.checked).map((i) => i.value);
  const ids = [...streamSel].map(String);
  if (ids.length === 0) {
    // 「全部房间」视图下的勾选直接对所有房间一起生效（含全局兜底）
    streamProfiles[''] = kinds.slice();
    state.rooms.forEach((r) => { streamProfiles[String(r.room_id)] = kinds.slice(); });
  } else {
    // 单房间只改该房间；多房间则同步到选中的所有房间（「全部房间同步」的子集版）
    ids.forEach((k) => { streamProfiles[k] = kinds.slice(); });
  }
  saveStreamProfiles();
}
function updateStreamFilterHint() {
  const hint = $('#streamFilterHint');
  const group = $('#streamKindFilters');
  if (!hint || !group) return;
  const n = streamSel.size;
  if (n === 0) {
    hint.textContent = '当前筛选对全部房间一起生效';
    group.title = '事件类型筛选：在「全部房间」下修改将同步应用到所有房间';
  } else if (n === 1) {
    hint.textContent = '仅对该房间生效，各房间独立记忆';
    group.title = '事件类型筛选，仅作用于当前房间，不影响其他房间';
  } else {
    hint.textContent = `当前仅看选中的 ${n} 个房间；事件类型勾选会同时记忆到这 ${n} 个房间`;
    group.title = `事件类型筛选将同时应用到选中的 ${n} 个房间（半选表示各房间当前配置不一致，勾选后统一）`;
  }
}
/* 首屏同步恢复：初始房间状态是异步拉取的，在它返回前先用 localStorage 还原筛选，
   避免刷新瞬间闪现四个默认全选 */
applyStreamProfile();
updateStreamFilterHint();

/* ---- 房间多选下拉 ---- */
let roomListSig = '';
function renderRoomPicker() {
  const listEl = $('#streamRoomList');
  // 清理已删除房间的选择与筛选配置（房间列表尚未加载时不做清理，避免误删）
  const ids = new Set(state.rooms.map((r) => String(r.room_id)));
  let pruned = false;
  if (state.rooms.length) {
    for (const k of Object.keys(streamProfiles)) {
      if (k !== '' && !ids.has(k)) { delete streamProfiles[k]; pruned = true; }
    }
    for (const id of [...streamSel]) {
      if (!ids.has(String(id))) { streamSel.delete(id); pruned = true; }
    }
  }
  if (pruned) { saveStreamProfiles(); saveStreamSelection(); }

  // 房间成员/名称变化才重建列表；直播状态点在 syncPickerUI 里轻量更新
  const sig = state.rooms.map((r) => `${r.room_id}:${r.vup}`).join('|');
  if (sig !== roomListSig) {
    roomListSig = sig;
    listEl.innerHTML = state.rooms.length
      ? state.rooms.map((r) => `
        <label class="mselect-opt" data-id="${r.room_id}">
          <input type="checkbox" title="勾选后只看这些房间（可多选）">
          <span class="mselect-dot"></span>
          <span class="mselect-name">${fmt.esc(r.vup)}</span>
          <span class="mselect-rid">${r.room_id}</span>
        </label>`).join('')
      : '<div class="mselect-empty">暂无房间，请先在房间管理中添加</div>';
  }
  syncPickerUI();
  applyStreamProfile();
  updateStreamFilterHint();
}

function syncPickerUI() {
  // 各行勾选态 + 直播状态点
  $('#streamRoomList').querySelectorAll('.mselect-opt').forEach((row) => {
    const id = Number(row.dataset.id);
    const r = state.rooms.find((x) => x.room_id === id);
    row.querySelector('input').checked = streamSel.has(id);
    const dot = row.querySelector('.mselect-dot');
    const live = r && r.phase === 'recording';
    const busy = r && (r.phase === 'connecting' || r.phase === 'waiting');
    dot.className = 'mselect-dot' + (live ? ' live' : busy ? ' busy' : '');
    dot.title = r ? (PHASE_TITLES[r.phase] || '') : '';
  });
  // 「全部房间」行高亮
  $('#streamRoomAllRow').classList.toggle('sel', streamSel.size === 0);
  // 按钮摘要
  const label = $('#streamRoomLabel');
  const btn = $('#streamRoomBtn');
  btn.classList.toggle('has-sel', streamSel.size > 0);
  if (streamSel.size === 0) {
    label.textContent = '全部房间';
  } else if (streamSel.size === 1) {
    const id = [...streamSel][0];
    const r = state.rooms.find((x) => x.room_id === id);
    label.textContent = r ? `${r.vup}（${id}）` : `房间 ${id}`;
  } else {
    label.textContent = `已选 ${streamSel.size} 个房间`;
  }
  if (streamSel.size > 1) {
    const names = [...streamSel].map((id) => {
      const r = state.rooms.find((x) => x.room_id === id);
      return r ? r.vup : `房间${id}`;
    });
    btn.title = '当前查看：' + names.join('、');
  } else {
    btn.title = '选择要查看的直播间：不勾选任何房间 = 全部房间';
  }
}

function setRoomPickerOpen(open) {
  $('#streamRoomPanel').hidden = !open;
  $('#streamRoomBtn').classList.toggle('open', open);
  $('#streamRoomBtn').setAttribute('aria-expanded', String(open));
}

function onRoomPickChange() {
  saveStreamSelection();
  syncPickerUI();
  applyStreamProfile();   // 多房间交集 / 半选态
  updateStreamFilterHint();
  rerenderStream();
}

function streamFilters() {
  const kinds = new Set(streamKindInputs().filter((i) => i.checked).map((i) => i.value));
  return { kinds, rooms: streamSel.size ? new Set(streamSel) : null };
}

function streamRow(d) {
  const f = streamFilters();
  if (!f.kinds.has(d.kind)) return '';
  if (f.rooms && !f.rooms.has(d.room_id)) return '';
  const medal = d.medal && d.medal.name
    ? `<span class="dm-medal">${fmt.esc(d.medal.name)} Lv${d.medal.level}</span>` : '';
  if (d.kind === 'moderation') {
    return `<div class="dm-row moderation">
      <span class="dm-time">${fmt.hms(d.ts)}</span>
      <span class="dm-room">${fmt.esc(d.vup)}</span>
      <span class="dm-text">⚠ ${fmt.esc(d.event_name)}：已自动保存该用户近 30 分钟发过的弹幕（${d.suspect_count} 条），可在「录制文件 · 房管处置存档」查看</span>
    </div>`;
  }
  const user = d.uname ? `<span class="dm-user">${fmt.esc(d.uname)}</span>` : '';
  const price = d.kind === 'super_chat' ? `<span class="dm-price">¥${d.price}</span>` : '';
  return `<div class="dm-row ${d.kind}">
    <span class="dm-time">${fmt.hms(d.ts)}</span>
    <span class="dm-room">${fmt.esc(d.vup)}</span>
    ${user}${medal}${price}
    <span class="dm-text">${fmt.esc(d.text)}</span>
  </div>`;
}

let flushScheduled = false;
function scheduleStreamFlush() {
  if (flushScheduled || !$('#view-stream').classList.contains('active')) return;
  flushScheduled = true;
  requestAnimationFrame(() => {
    flushScheduled = false;
    flushStream();
  });
}

function flushStream() {
  if ($('#streamPause').checked) return;  // 暂停时等恢复后整表重建
  const list = $('#streamList');
  const hint = list.querySelector('.stream-hint');
  if (hint) hint.remove();
  const html = state.streamPending.splice(0).map(streamRow).filter(Boolean).join('');
  if (html) list.insertAdjacentHTML('beforeend', html);
  while (list.querySelectorAll('.dm-row').length > 500) {
    list.firstElementChild?.remove();
  }
  list.scrollTop = list.scrollHeight;
}

function rerenderStream() {
  const list = $('#streamList');
  const rows = state.stream.map(streamRow).filter(Boolean).slice(-500).join('');
  list.innerHTML = rows || '<div class="stream-hint">等待弹幕到达… 弹幕将实时显示在这里（仅保留最近 500 条）</div>';
  if (!$('#streamPause').checked) list.scrollTop = list.scrollHeight;
}

$('#view-stream').addEventListener('change', (e) => {
  if (e.target.id === 'streamPause') {
    if (!e.target.checked) rerenderStream();
  } else if (e.target.closest('#streamKindFilters')) {
    persistStreamProfile();
    rerenderStream();
  }
});
/* 房间多选下拉：开关面板 / 勾选 / 快捷操作（面板在多选过程中保持打开） */
$('#streamRoomBtn').addEventListener('click', () => {
  setRoomPickerOpen($('#streamRoomPanel').hidden);
});
$('#streamRoomAllRow').addEventListener('click', () => {
  if (streamSel.size !== 0) {
    streamSel.clear();
    onRoomPickChange();
  }
});
$('#streamRoomList').addEventListener('change', (e) => {
  const row = e.target.closest('.mselect-opt');
  if (!row) return;
  const id = Number(row.dataset.id);
  if (e.target.checked) streamSel.add(id); else streamSel.delete(id);
  onRoomPickChange();
});
$('#streamRoomAllBtn').addEventListener('click', () => {
  state.rooms.forEach((r) => streamSel.add(r.room_id));
  onRoomPickChange();
});
$('#streamRoomInvertBtn').addEventListener('click', () => {
  const next = new Set(state.rooms
    .map((r) => r.room_id)
    .filter((id) => !streamSel.has(id)));
  streamSel.clear();
  next.forEach((id) => streamSel.add(id));
  onRoomPickChange();
});
// 点击面板外部 / Esc 关闭
document.addEventListener('mousedown', (e) => {
  if (!$('#streamRoomPanel').hidden && !e.target.closest('#streamRoomPick')) {
    setRoomPickerOpen(false);
  }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#streamRoomPanel').hidden) setRoomPickerOpen(false);
});
$('#streamClear').addEventListener('click', () => {
  state.stream = []; state.streamPending = []; rerenderStream();
});
function updateBadge() {
  const b = $('#streamBadge');
  b.hidden = state.streamUnread === 0;
  b.textContent = state.streamUnread > 99 ? '99+' : state.streamUnread;
}

/* ---------------- 录制文件 ---------------- */
$$('#view-files .tabs .tab').forEach((t) => t.addEventListener('click', () => {
  $$('#view-files .tabs .tab').forEach((x) => x.classList.toggle('active', x === t));
  state.filesKind = t.dataset.kind;
  const videoMode = state.filesKind === 'videos';
  $('#filesPanelRecords').hidden = videoMode;
  $('#filesPanelVideos').hidden = !videoMode;
  if (videoMode) loadVideos(); else loadFiles();
}));

async function loadFiles() {
  try {
    state.files = await api('/api/files');
  } catch (err) { toast(err.message, 'err'); return; }
  const items = state.files[state.filesKind] || [];
  const empty = $('#filesEmpty');
  empty.hidden = items.length > 0;
  empty.querySelector('p').textContent = state.filesKind === 'weak'
    ? '暂无房管处置存档文件（房管在直播中拉黑或禁言观众后，才会自动保存该观众发过的弹幕）'
    : '暂无弹幕记录文件（真实开播后才会生成并保存）';
  $('#filesTable').innerHTML = items.map((f) => `
    <tr>
      <td>${fmt.esc(f.vup)}</td>
      <td class="mono">${fmt.esc(f.name)}</td>
      <td>${fmt.size(f.size)}</td>
      <td>${f.age_hour < 1 ? '刚刚' : f.age_hour < 24
        ? `${f.age_hour.toFixed(1)} 小时前` : `${(f.age_hour / 24).toFixed(1)} 天前`}</td>
      <td>
        <button class="btn btn-sm" data-preview="${fmt.esc(f.path)}">预览</button>
        ${state.filesKind === 'records'
          ? `<button class="btn btn-sm btn-primary" data-report="${fmt.esc(f.path)}">战报</button>` : ''}
        ${state.filesKind === 'records' && !f.deduped
          ? `<button class="btn btn-sm op-dedup" data-dedup="${fmt.esc(f.path)}">去重</button>`
          : f.deduped ? '<span class="row-state-tag deduped op-dedup">已去重</span>' : ''}
        <a class="btn btn-sm" href="/api/files/download?kind=${state.filesKind}&path=${encodeURIComponent(f.path)}">下载</a>
      </td>
    </tr>`).join('');
}

$('#filesTable').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-preview]');
  if (!btn) return;
  const path = btn.dataset.preview;
  $('#previewTitle').textContent = path;
  $('#previewDownload').href =
    `/api/files/download?kind=${state.filesKind}&path=${encodeURIComponent(path)}`;
  $('#previewBody').textContent = '加载中…';
  $('#previewMask').hidden = false;
  try {
    const r = await api(`/api/files/preview?kind=${state.filesKind}&path=${encodeURIComponent(path)}&limit=200`);
    const pretty = r.lines.map((line) => {
      try { return JSON.stringify(JSON.parse(line), null, 2); }
      catch (_) { return line; }
    }).join('\n');
    $('#previewBody').textContent =
      (r.truncated ? '（仅展示最后 200 行）\n\n' : '') + pretty;
    $('#previewBody').scrollTop = 0;
  } catch (err) { $('#previewBody').textContent = '加载失败：' + err.message; }
});
$('#previewClose').addEventListener('click', () => { $('#previewMask').hidden = true; });
$('#previewMask').addEventListener('click', (e) => {
  if (e.target.id === 'previewMask') $('#previewMask').hidden = true;
});

/* ---------------- 视频场次 ---------------- */
function videoDl(vup, file) {
  return `/api/videos/download?vup=${encodeURIComponent(vup)}&file=${encodeURIComponent(file)}`;
}

async function loadVideos() {
  let data;
  try {
    data = await api('/api/videos');
  } catch (err) { toast(err.message, 'err'); return; }
  state.videos = data;
  $('#videoDirHint').innerHTML = data.dir
    ? `目录：<button type="button" class="pathlink" data-openvideodir
         title="点击在资源管理器中打开此目录">${fmt.esc(data.dir)}</button>`
    : '';
  const ff = data.ffmpeg;
  $('#videoFfmpegPill').innerHTML = ff
    ? `<span class="tag ok">ffmpeg ${fmt.esc(ff.version || '已就绪')}</span>`
    : '<span class="tag" title="秒切功能需要 ffmpeg">未检测到 ffmpeg（仍可复制跳转命令）</span>';
  const ss = data.sessions || [];
  $('#videosEmpty').hidden = ss.length > 0;
  $('#videosTable').innerHTML = ss.map(videoRow).join('');
  // 有导出任务在跑时轮询刷新进度，结束自动停
  const exporting = ss.some((s) => s.exporting);
  if (exporting && !state.exportTimer) {
    state.exportTimer = setInterval(() => {
      if (!$('#filesPanelVideos').hidden) loadVideos();
    }, 2000);
  } else if (!exporting && state.exportTimer) {
    clearInterval(state.exportTimer);
    state.exportTimer = null;
  }
  const orphans = data.orphans || [];
  $('#videoOrphans').hidden = orphans.length === 0;
  $('#videoOrphansTable').innerHTML = orphans.map((o) => `
    <tr>
      <td>${fmt.esc(o.vup)}</td>
      <td class="mono">${fmt.esc(o.file)}</td>
      <td>${fmt.size(o.size)}</td>
      <td>
        <a class="btn btn-sm" href="${videoDl(o.vup, o.file)}">下载</a>
        <button class="btn btn-sm btn-danger" data-odel="${fmt.esc(o.vup)}"
                data-ofile="${fmt.esc(o.file)}">删除</button>
      </td>
    </tr>`).join('');
}

function videoRow(s) {
  const badges = [];
  if (s.ongoing) badges.push('<span class="badge recording">视频录制中</span>');
  badges.push(`<span class="tag">${fmt.esc(s.qn_label || (QN_LABELS[s.qn] || s.qn))}</span>`);
  if (s.codec) badges.push(`<span class="tag">${fmt.esc(s.codec)}</span>`);
  if (s.segment_count > 1) badges.push(`<span class="tag">${s.segment_count} 段</span>`);
  if (s.ass) badges.push('<span class="tag ok" title="本场已生成 ASS 弹幕字幕文件：与视频同名放在一起，播放器加载后可随时开关显示">弹幕字幕</span>');
  if (s.ai) badges.push(
    `<span class="tag ai-badge" title="${fmt.esc(s.ai.summary || '已生成 AI 解读')}">🤖 AI ${fmt.esc(s.ai.mode || '')} · ${s.ai.picks || 0} 条</span>`);
  if (s.recovered) badges.push('<span class="tag" style="color:var(--warn)" title="录制进程异常退出（崩溃 / 被强制结束 / 断电），场次记录没来得及正常收尾。视频文件本身完好，程序重启后已按文件名自动找回并归回本场，仅结束时间为估算值">中断恢复</span>');
  if (s.end_reason === 'disk') badges.push('<span class="tag" style="color:var(--danger-hi)">磁盘止录</span>');
  return `<tr>
    <td>${fmt.esc(s.vup)}<div class="hint" style="margin:0">${fmt.esc(s.anchor_name || '')}</div></td>
    <td>
      <div class="mono" style="font-size:12.5px">${fmt.esc(s.base)}</div>
      <div class="hint" style="margin:2px 0 0" title="${fmt.esc(s.title || '')}">${fmt.esc(s.title || '—')}</div>
      <div style="margin-top:3px">${badges.join(' ')}</div>
    </td>
    <td>${fmt.size(s.bytes)}</td>
    <td>${fmt.dur(s.duration_sec)}</td>
    <td>${s.start_clock ? fmt.esc(s.start_clock) : '—'}</td>
    <td>
      <div class="v-actions">
      <button class="btn btn-sm btn-primary" data-hl="${fmt.esc(s.base)}"
              data-vup="${fmt.esc(s.vup)}">高能时间轴</button>
      ${s.exporting
        ? `<button class="btn btn-sm" disabled
              title="正在后台烧录弹幕（当前：${fmt.esc(s.exporting.current || '')}）">
              导出中 ${s.exporting.progress || 0}%</button>`
        : s.ongoing ? '' : `<button class="btn btn-sm" data-export="${fmt.esc(s.base)}"
              data-vup="${fmt.esc(s.vup)}"
              title="开始制作：后台把弹幕永久烧录进画面，生成 .danmaku.mp4 新文件（需几分钟，完成后本行出现「下载弹幕版」；原视频不改动；已导出过会重新生成覆盖）">导出弹幕版</button>`}
      ${s.ass ? `<a class="btn btn-sm" href="${videoDl(s.vup, s.ass)}"
           title="下载 ASS 弹幕字幕文件（ASS 是一种通用字幕格式）：与视频同名放在一起，用 VLC / PotPlayer / mpv 播放时可加载、随时开关显示，弹幕不会烧进画面">下载弹幕文件</a>`
        : ''}
      ${s.ongoing || !s.jsonl_exists ? '' : `<button class="btn btn-sm" data-assregen="${fmt.esc(s.base)}"
              data-vup="${fmt.esc(s.vup)}"
              title="按当前「设置 → 弹幕样式」从本场弹幕记录重新生成 ASS 字幕（已有文件会覆盖）">${s.ass ? '重新生成字幕' : '生成弹幕字幕'}</button>`}
      ${(s.exports || []).map((f) => `
        <a class="btn btn-sm" href="${videoDl(s.vup, f.file)}"
           title="下载烧录完成的成品视频（弹幕已压进画面，任何播放器可直接播放、可直接分享）：${fmt.esc(f.file)}（${fmt.size(f.size)}）">下载弹幕版</a>`).join('')}
      ${s.ongoing ? '' : `<button class="btn btn-sm btn-danger" data-vdel="${fmt.esc(s.base)}"
              data-vup="${fmt.esc(s.vup)}">删除</button>`}
      </div>
    </td>
  </tr>`;
}

$('#btnVideoReload').addEventListener('click', loadVideos);

$('#videoDirHint').addEventListener('click', async (e) => {
  if (!e.target.closest('[data-openvideodir]')) return;
  try {
    const res = await api('/api/videos/open-folder', { method: 'POST' });
    if (res && res.path) toast(`已打开：${res.path}`, 'ok', 3600);
  } catch (err) { toast(err.message, 'err'); }
});

$('#videosTable').addEventListener('click', (e) => {
  const hl = e.target.closest('[data-hl]');
  const del = e.target.closest('[data-vdel]');
  const exp = e.target.closest('[data-export]');
  const ass = e.target.closest('[data-assregen]');
  if (hl) { openHighlights(hl.dataset.vup, hl.dataset.hl); return; }
  if (exp) {
    api('/api/videos/export-sub', {
      method: 'POST',
      body: { vup: exp.dataset.vup, session: exp.dataset.export },
    })
      .then((r) => {
        toast(`已开始后台烧录弹幕（${r.segments} 个分段），完成后本行出现「下载弹幕版」`, 'ok', 5000);
        loadVideos();
      })
      .catch((err) => toast(err.message, 'err', 6000));
    return;
  }
  if (ass) {
    const old = ass.textContent;
    ass.disabled = true;
    ass.textContent = '生成中…';
    api('/api/videos/regenerate-ass', {
      method: 'POST',
      body: { vup: ass.dataset.vup, session: ass.dataset.assregen },
    })
      .then((r) => {
        toast(`字幕已重新生成：${r.ass}`, 'ok');
        loadVideos();
      })
      .catch((err) => {
        toast(err.message, 'err', 6000);
        ass.disabled = false;
        ass.textContent = old;
      });
    return;
  }
  if (del && confirm(`确认删除场次 ${del.dataset.vdel} 的全部视频/字幕/切片？\n该操作不可恢复，弹幕 jsonl 不受影响。`)) {
    api(`/api/videos?vup=${encodeURIComponent(del.dataset.vup)}&session=${encodeURIComponent(del.dataset.vdel)}`,
      { method: 'DELETE' })
      .then(() => { toast('场次已删除', 'ok'); loadVideos(); })
      .catch((err) => toast(err.message, 'err'));
  }
});

$('#videoOrphansTable').addEventListener('click', (e) => {
  const b = e.target.closest('[data-odel]');
  if (!b) return;
  if (!confirm(`确认删除散片 ${b.dataset.ofile}？`)) return;
  api(`/api/videos/file?vup=${encodeURIComponent(b.dataset.odel)}&file=${encodeURIComponent(b.dataset.ofile)}`,
    { method: 'DELETE' })
    .then(() => { toast('散片已删除', 'ok'); loadVideos(); })
    .catch((err) => toast(err.message, 'err'));
});

/* 视频录制中的场次：面板可见时每 5s 刷新大小/时长 */
setInterval(() => {
  const panel = $('#filesPanelVideos');
  if (!panel || panel.hidden) return;
  if ((state.videos.sessions || []).some((s) => s.ongoing)) loadVideos();
}, 5000);

/* ---------------- 高能时间轴 ---------------- */
const hlCur = { vup: '', session: '', data: null, ai: null };

async function openHighlights(vup, session) {
  hlCur.vup = vup;
  hlCur.session = session;
  hlCur.data = null;
  hlCur.ai = null;
  $('#hlTitle').textContent = `高能时间轴 · ${vup} · ${session}`;
  // 弹窗模式默认跟随全局设置
  const lm = (state.settings && state.settings.llm_mode === 'C') ? 'C' : 'A';
  $('#hlAiMode').value = lm;
  $('#hlMask').hidden = false;
  await loadHighlights();
  await loadAiResult();
  renderHighlights();
}

async function loadAiResult() {
  try {
    const q = `vup=${encodeURIComponent(hlCur.vup)}&session=${encodeURIComponent(hlCur.session)}`;
    hlCur.ai = await api(`/api/videos/ai-result?${q}`);
    if (hlCur.ai && hlCur.ai.mode) $('#hlAiMode').value = hlCur.ai.mode;
    return true;
  } catch (_) {
    hlCur.ai = null;  // 404 = 还没解读过，静默
    return false;
  }
}

async function runAi(force) {
  if (!hlCur.data) { toast('弹幕数据尚未加载完成', 'warn'); return; }
  const btn = $('#hlAiRun');
  btn.disabled = true;
  const oldText = btn.textContent;
  btn.textContent = force ? 'AI 重新分析中…' : 'AI 分析中…（C 档约 1–2 分钟）';
  try {
    hlCur.ai = await api('/api/videos/ai-analyze', {
      method: 'POST',
      body: {
        vup: hlCur.vup,
        session: hlCur.session,
        mode: $('#hlAiMode').value,
        force,
        window_sec: Number($('#hlWindow').value) || 60,
        top_n: Number($('#hlTopN').value) || 8,
      },
    });
    renderHighlights();
    toast(force ? 'AI 重新分析完成' : '🤖 AI 解读完成', 'ok');
    loadVideos();
  } catch (err) {
    toast('AI 解读失败：' + err.message, 'err', 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = oldText;
  }
}

async function loadHighlights() {
  const win = $('#hlWindow').value;
  const top = $('#hlTopN').value;
  $('#hlBody').innerHTML = '<div class="muted">正在扫描弹幕/SC/人气数据…</div>';
  try {
    const q = `vup=${encodeURIComponent(hlCur.vup)}&session=${encodeURIComponent(hlCur.session)}`
      + `&window_sec=${win}&top_n=${top}`;
    hlCur.data = await api(`/api/videos/highlights?${q}`);
    renderHighlights();
  } catch (err) {
    $('#hlBody').innerHTML = `<div style="color:var(--danger)">分析失败：${fmt.esc(err.message)}</div>`;
  }
}

function hlTimeline(d) {
  const tl = d.timeline || [];
  if (!tl.length) return '';
  const maxDm = Math.max(1, ...tl.map((b) => b.dm));
  const winSet = new Set();
  (d.windows || []).forEach((w) => {
    for (let t = w.start_ts; t < w.end_ts; t += d.window_sec) {
      winSet.add(Math.floor(t / d.window_sec) * d.window_sec);
    }
  });
  const start0 = tl[0].ts;
  const span = Math.max(1, tl[tl.length - 1].ts + d.window_sec - start0);
  const bars = tl.map((b) => {
    const h = Math.max(2, Math.round(b.dm / maxDm * 100));
    const hot = winSet.has(b.ts);
    const sc = b.sc > 0;
    return `<div class="hl-bar ${hot ? 'hot' : ''} ${sc ? 'has-sc' : ''}"
      style="height:${h}%"
      title="${fmtClock(b.ts)} 弹幕 ${b.dm}${b.sc ? ' · SC ' + b.sc : ''}${b.slope ? ' · 人气+' + fmt.n(b.slope) : ''}"></div>`;
  }).join('');
  return `<div class="hl-timeline"><div class="hl-bars">${bars}</div>
    <div class="hl-axis"><span>${fmtClock(start0)}</span>
    <span>${fmtClock(start0 + span)}</span></div>
    <div class="hint" style="margin:4px 0 0">
    柱高=该窗口弹幕密度；金色柱含 SC；红色描边=入选高能窗口（共 ${tl.length} 个 ${d.window_sec}s 窗口）</div></div>`;
}

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function aiPickForStart(startTs) {
  const ai = hlCur.ai;
  if (!ai) return null;
  const target = Math.floor(Number(startTs));
  return (ai.picks || []).find(
    (p) => Math.abs(Math.floor(Number(p.start_ts)) - target) <= 1) || null;
}

function aiPickBlock(p) {
  if (!p) return '';
  return `<div class="ai-pick">
    <div class="ai-pick-title">🤖 ${fmt.esc(p.title)}</div>
    <div class="ai-pick-tags">
      <span class="hl-tag ai">${fmt.esc(p.category || '其他')}</span>
      ${p.cover ? `<span class="ai-cover">封面大字：<b>${fmt.esc(p.cover)}</b></span>` : ''}
      <span class="muted">建议切点：起点前 ${p.pad_before}s · 终点后 ${p.pad_after}s</span>
    </div>
    <div class="ai-reason">${fmt.esc(p.reason)}</div>
    ${p.intro ? `<div class="ai-intro">🎬 口播开场：${fmt.esc(p.intro)}</div>` : ''}
  </div>`;
}

function aiCutButton(p, seek, cutId, resId, label) {
  const ff = hlCur.data.ffmpeg && hlCur.data.ffmpeg.available;
  if (!ff || !seek || !seek.part) return '';
  const ss = Math.max(0, Number((seek.start_sec - (p ? p.pad_before : 0)).toFixed(1)));
  const to = Number((seek.end_sec + (p ? p.pad_after : 0)).toFixed(1));
  return `<button class="btn btn-sm btn-primary" data-cut="${cutId}"
      data-resid="${resId}"
      data-vup="${fmt.esc(hlCur.vup)}" data-session="${fmt.esc(hlCur.session)}"
      data-part="${fmt.esc(seek.part)}" data-ss="${ss}" data-to="${to}">${label}</button>
    <span class="hl-cut-result" id="${resId}"></span>`;
}

function aiSummaryBox() {
  const ai = hlCur.ai;
  if (!ai) return '';
  const when = ai.ts
    ? new Date(ai.ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '';
  const rank = (ai.picks || []).map((p, i) =>
    `<li><b>${i + 1}. ${fmt.esc(p.title)}</b>`
    + `<span class="muted">${fmtClock(p.start_ts)} · ${fmt.esc(p.category || '')}`
    + `${p.source === 'ai' ? ' · AI 补漏' : ''}</span></li>`).join('');
  return `<div class="ai-box">
    <div class="ai-box-head">
      <b>🤖 AI 解读结果</b>
      <span class="hl-tag ai">${ai.mode === 'C' ? 'C 深度策划' : 'A 轻量解读'}</span>
      <span class="muted">${fmt.esc(ai.model || '')} · ${when}</span>
      <button class="btn btn-sm" id="hlAiRerun" type="button">重新分析（会再次计费）</button>
    </div>
    <div class="ai-summary">整场总结：${fmt.esc(ai.summary || '')}</div>
    ${rank ? `<ol class="ai-rank">${rank}</ol>` : ''}
  </div>`;
}

function aiExtraCards() {
  const ai = hlCur.ai;
  const wins = (ai && ai.extra_windows) || [];
  if (!wins.length) return '';
  const cards = wins.map((w, j) => {
    const seek = w.seek || {};
    const cmds = seek.commands || {};
    const pick = aiPickForStart(w.start_ts);
    const phrases = (w.top_phrases || []).map((p) =>
      `<span class="hl-phrase">${fmt.esc(p.text)} <b>×${p.count}</b></span>`).join('');
    const samples = (w.samples || []).map((s) =>
      `<div class="ps-line"><span class="ps-time">${fmt.hm(s.ts)}</span>`
      + `<b>${fmt.esc(s.uname || '匿名')}：</b>${fmt.esc(s.text)}</div>`).join('');
    return `<div class="hl-card ai-extra">
      <div class="hl-head">
        <span class="hl-rank ai">AI 补漏</span>
        <span class="hl-clock">${fmtClock(w.start_ts)}</span>
        <span class="hl-tag ai">5 分钟段</span>
        <span class="muted">弹幕 ${fmt.n(w.danmaku)}${w.super_chat ? ` · SC ${w.super_chat}` : ''}
          · ${w.users} 人</span>
        <span class="muted" style="margin-left:auto">${fmt.esc(seek.part || '—')}
          ${seek.part ? `· 偏移 ${Math.round(seek.start_sec)}s` : ''}</span>
      </div>
      ${w.ai_why ? `<div class="ai-reason">🔍 入选理由：${fmt.esc(w.ai_why)}</div>` : ''}
      ${aiPickBlock(pick)}
      ${phrases ? `<div class="hl-phrases">${phrases}</div>` : ''}
      ${samples ? `<details class="hl-samples"><summary>代表弹幕（${w.samples.length}）</summary>${samples}</details>` : ''}
      <div class="hl-actions">
        <button class="btn btn-sm" data-cmd="${encodeURIComponent(cmds.vlc || '')}">复制 VLC 跳转</button>
        <button class="btn btn-sm" data-cmd="${encodeURIComponent(cmds.ffplay || '')}">复制 ffplay 跳转</button>
        ${aiCutButton(pick, seek, `x${j}`, `hlCutx${j}`,
          pick ? '按 AI 建议切点秒切' : 'ffmpeg 秒切这 5 分钟')}
      </div>
    </div>`;
  }).join('');
  return `<div class="panel-title" style="font-size:14px;margin:14px 0 8px">
      🤖 AI 补漏窗口（规则密度信号未覆盖，共 ${wins.length} 个）</div>${cards}`;
}

function renderHighlights() {
  const d = hlCur.data;
  const ff = d.ffmpeg && d.ffmpeg.available;
  const t = d.totals || {};
  const head = `
    <div class="hl-summary">
      <div class="dedup-stat"><b>${fmt.n(t.danmaku)}</b>弹幕</div>
      <div class="dedup-stat"><b>${fmt.n(t.super_chat)}</b>SC</div>
      <div class="dedup-stat"><b>${fmt.n(t.max_dm_per_window)}</b>单窗峰值弹幕</div>
      <div class="dedup-stat"><b>${d.window_sec}s</b>窗口 / Top ${(d.windows || []).length}</div>
    </div>
    ${ff ? '' : '<div class="warn-box" style="margin:8px 0">未检测到 ffmpeg：无法在页面内「秒切」，但仍可复制 VLC/ffplay 跳转命令，或自行安装 ffmpeg 并加入 PATH 后刷新。</div>'}
    ${d.jsonl_exists ? '' : '<div class="warn-box" style="margin:8px 0">未找到同日弹幕 jsonl，高能检测无数据可用（视频本身不受影响）。</div>'}
    ${d.note ? `<div class="muted">${fmt.esc(d.note)}</div>` : ''}
    ${aiSummaryBox()}
    ${hlTimeline(d)}`;
  const cards = (d.windows || []).map((w, i) => {
    const seek = w.seek || {};
    const cmds = seek.commands || {};
    const pick = aiPickForStart(w.start_ts);
    const phrases = (w.top_phrases || []).map((p) =>
      `<span class="hl-phrase">${fmt.esc(p.text)} <b>×${p.count}</b></span>`).join('');
    const samples = (w.samples || []).map((s) =>
      `<div class="ps-line"><span class="ps-time">${fmt.hm(s.ts)}</span>`
      + `<b>${fmt.esc(s.uname || '匿名')}：</b>${fmt.esc(s.text)}</div>`).join('');
    const tags = w.tags.map((x) => `<span class="hl-tag">${fmt.esc(x)}</span>`).join('');
    const cutLabel = seek.cross_part ? '秒切（本段部分，窗口跨分段）' : 'ffmpeg 无损秒切这段';
    return `<div class="hl-card">
      <div class="hl-head">
        <span class="hl-rank">#${i + 1}</span>
        <span class="hl-clock">${fmtClock(w.start_ts)}</span>
        <span class="hl-score">${w.score} 分</span>
        ${tags}
        <span class="muted">弹幕 ${fmt.n(w.danmaku)}${w.super_chat ? ` · SC ${w.super_chat} / ¥${fmt.n(w.sc_amount)}` : ''}
          ${w.pop_slope ? ` · 人气 +${fmt.n(w.pop_slope)}` : ''} · ${w.users} 人</span>
        <span class="muted" style="margin-left:auto">${fmt.esc(seek.part || '—')}
          ${seek.part ? `· 偏移 ${Math.round(seek.start_sec)}s` : ''}
          ${seek.cross_part ? '· ⚠跨分段' : ''}</span>
      </div>
      ${aiPickBlock(pick)}
      ${phrases ? `<div class="hl-phrases">${phrases}</div>` : ''}
      ${samples ? `<details class="hl-samples"><summary>代表弹幕（${w.samples.length}）</summary>${samples}</details>` : ''}
      <div class="hl-actions">
        <button class="btn btn-sm" data-cmd="${encodeURIComponent(cmds.vlc || '')}">复制 VLC 跳转</button>
        <button class="btn btn-sm" data-cmd="${encodeURIComponent(cmds.ffplay || '')}">复制 ffplay 跳转</button>
        ${ff && seek.part
          ? `<button class="btn btn-sm" data-cut="${i}"
              data-vup="${fmt.esc(hlCur.vup)}" data-session="${fmt.esc(hlCur.session)}"
              data-part="${fmt.esc(seek.part)}" data-ss="${seek.start_sec}" data-to="${seek.end_sec}">
              ${cutLabel}</button>
            <span class="hl-cut-result" id="hlCut${i}"></span>`
          : ''}
        ${aiCutButton(pick, seek, i, `hlAiCut${i}`, '🤖 按 AI 建议切点秒切')}
      </div>
      <details class="hl-cmdbox"><summary>命令行详情</summary>
        <label>VLC（本机终端执行）<input class="input mono" readonly value="${fmt.esc(cmds.vlc || '')}"></label>
        <label>ffplay<input class="input mono" readonly value="${fmt.esc(cmds.ffplay || '')}"></label>
        ${cmds.ffmpeg_copy ? `<label>ffmpeg 手动秒切<input class="input mono" readonly value="${fmt.esc(cmds.ffmpeg_copy)}"></label>` : ''}
        <div class="hint">${fmt.esc(cmds.tip || '')}</div>
      </details>
    </div>`;
  }).join('');

  const segRows = (() => {
    // 场次分段信息来自列表接口；用当前已知 session 再拉一次列表太重，改为分析结果不含分段，
    // 分段下载通过下方刷新列表完成。这里展示窗口所在 part 的下载入口。
    const parts = [...new Set((d.windows || []).map((w) => w.seek && w.seek.part).filter(Boolean))];
    return parts.map((f) => `<a class="btn btn-sm" href="${videoDl(hlCur.vup, f)}">下载 ${fmt.esc(f)}</a>`).join(' ');
  })();

  $('#hlBody').innerHTML = head
    + `<div class="panel-title" style="font-size:14px;margin:12px 0 8px">高能窗口</div>`
    + (cards || '<div class="muted">未检测到显著高能窗口，可尝试调大窗口或降低 Top 门槛；C 档 AI 也可能补到规则漏掉的片段。</div>')
    + aiExtraCards()
    + `<div class="hl-files">
        <div class="panel-title" style="font-size:13px">相关文件</div>
        <div class="hint">高能窗口所在分段：${segRows || '无'}</div>
        <div class="hint">完整分段、弹幕字幕（ASS 文件）与已切片段请在视频列表行内下载；「导出弹幕版」烧录按钮也在列表页。</div>
      </div>`;
}

$('#hlClose').addEventListener('click', () => { $('#hlMask').hidden = true; });
$('#hlMask').addEventListener('click', (e) => {
  if (e.target.id === 'hlMask') $('#hlMask').hidden = true;
});
$('#hlReanalyze').addEventListener('click', loadHighlights);
$('#hlAiRun').addEventListener('click', () => runAi(false));
$('#hlRegenAss').addEventListener('click', async () => {
  try {
    const r = await api('/api/videos/regenerate-ass', {
      method: 'POST',
      body: { vup: hlCur.vup, session: hlCur.session },
    });
    toast(`字幕已重新生成：${r.ass}`, 'ok');
    loadVideos();
  } catch (err) { toast(err.message, 'err'); }
});

$('#hlBody').addEventListener('click', async (e) => {
  if (e.target.closest('#hlAiRerun')) {
    if (confirm('重新分析会再次调用模型并产生费用，确定继续？')) runAi(true);
    return;
  }
  const copyBtn = e.target.closest('[data-cmd]');
  const cutBtn = e.target.closest('[data-cut]');
  if (copyBtn) {
    const cmd = decodeURIComponent(copyBtn.dataset.cmd || '');
    if (!cmd) return toast('命令不可用', 'warn');
    try {
      await navigator.clipboard.writeText(cmd);
      toast('命令已复制，在本机 PowerShell 中粘贴执行即可跳转到高能时刻', 'ok', 3600);
    } catch (_) { toast('复制失败，请手动展开命令行详情复制', 'warn'); }
    return;
  }
  if (cutBtn) {
    const box = $('#' + (cutBtn.dataset.resid || 'hlCut' + cutBtn.dataset.cut));
    cutBtn.disabled = true;
    const old = cutBtn.textContent;
    cutBtn.textContent = '秒切中…';
    box.textContent = '';
    try {
      const r = await api('/api/videos/cut', {
        method: 'POST',
        body: {
          vup: cutBtn.dataset.vup,
          session: cutBtn.dataset.session,
          part: cutBtn.dataset.part,
          start_sec: Number(cutBtn.dataset.ss),
          end_sec: Number(cutBtn.dataset.to),
        },
      });
      box.innerHTML = `✅ 已生成 <a href="${videoDl(cutBtn.dataset.vup, r.file)}" target="_blank">${fmt.esc(r.file)}</a>（${fmt.size(r.size)}，流拷贝无损）`;
      toast('秒切完成', 'ok');
      loadVideos();
    } catch (err) {
      box.textContent = '❌ ' + err.message;
      cutBtn.disabled = false;
      cutBtn.textContent = old;
    }
  }
});

/* ---------------- 日志 ---------------- */
function logLevels() {
  return new Set($$('.terminal-bar input:checked').map((i) => i.dataset.level));
}
function logLineHtml(d) {
  const lv = d.level === 'warning' ? 'warning' : d.level;
  return `<div class="log-line log-${lv}"><span class="log-time">${fmt.hms(d.ts)}</span> ${fmt.esc(d.msg)}</div>`;
}
function renderLogs(rebuild = false) {
  const levels = logLevels();
  const term = $('#logTerminal');
  if (rebuild) {
    term.innerHTML = state.logs.filter((l) => levels.has(l.level)).map(logLineHtml).join('');
    term.scrollTop = term.scrollHeight;
  }
}
function appendLogLine(d) {
  if (!logLevels().has(d.level)) return;
  const term = $('#logTerminal');
  term.insertAdjacentHTML('beforeend', logLineHtml(d));
  while (term.querySelectorAll('.log-line').length > 1000) term.firstElementChild?.remove();
  term.scrollTop = term.scrollHeight;
}
$('.terminal-bar').addEventListener('change', () => renderLogs(true));
$('#logsClear').addEventListener('click', () => { $('#logTerminal').innerHTML = ''; });
$('#logDirHint').addEventListener('click', async () => {
  try {
    const res = await api('/api/logs/open-folder', { method: 'POST' });
    if (res && res.path) toast(`已打开：${res.path}`, 'ok', 3600);
  } catch (err) { toast(err.message, 'err'); }
});

/* 设置页三处「生效路径」：点击在文件管理器中打开当前已生效的目录。
   打开的是服务端已保存配置派生的目录（输入框中未保存的改动不会改变打开位置）；
   路径全部由服务端派生，接口不接受前端传参，防穿越。 */
const EFF_PATH_API = {
  data: '/api/data/open-folder',
  logs: '/api/logs/open-folder',
  video: '/api/videos/open-folder',
};
document.addEventListener('click', async (e) => {
  const el = e.target.closest('.eff-path');
  if (!el) return;
  const url = EFF_PATH_API[el.dataset.open];
  if (!url) return;
  try {
    const res = await api(url, { method: 'POST' });
    if (res && res.path) toast(`已打开：${res.path}`, 'ok', 3600);
  } catch (err) { toast(err.message, 'err'); }
});

/* ---------------- 设置 ---------------- */
/* ------- 设置页子标签：切换 + 记忆 + 保存方式提示 ------- */
const SETTINGS_TABS = ['base', 'video', 'ds', 'ai', 'cared', 'theme'];
const SETTINGS_TAB_HINT = {
  base: '本页修改统一保存：有改动时底部出现保存条，离开页面会提醒',
  video: '本页修改统一保存：有改动时底部出现保存条，离开页面会提醒',
  ds: '弹幕样式在本页独立即时保存，与上方的统一保存无关',
  ai: '本页修改统一保存：有改动时底部出现保存条，离开页面会提醒',
  cared: '关心名单即时保存，与上方的统一保存无关',
  theme: '战报样式在本页独立保存，与上方的统一保存无关',
};
/* 弹幕样式实时预览的运行状态（var 声明避免本 IIFE 执行时的 TDZ 问题） */
var dsTimer = null, dsLanes = [], dsLanesN = 0, dsBoxW = 0, dsBoxH = 0;
function switchSettingsTab(tab) {
  if (!SETTINGS_TABS.includes(tab)) tab = 'base';
  $$('#settingsTabs .st-tab').forEach((b) =>
    b.classList.toggle('active', b.dataset.tab === tab));
  $$('.settings-tabpanel').forEach((p) =>
    p.classList.toggle('active', p.dataset.panel === tab));
  const hint = $('#settingsTopHint');
  if (hint) hint.textContent = SETTINGS_TAB_HINT[tab];
  if (typeof startDsPreview === 'function' && typeof stopDsPreview === 'function') {
    if (tab === 'ds') startDsPreview(); else stopDsPreview();
  }
  try { localStorage.setItem('ldd-settings-tab', tab); } catch (e) { /* ignore */ }
}
$$('#settingsTabs .st-tab').forEach((b) =>
  b.addEventListener('click', () => switchSettingsTab(b.dataset.tab)));
(function restoreSettingsTab() {
  let tab = 'base';
  try { tab = localStorage.getItem('ldd-settings-tab') || 'base'; } catch (e) { /* ignore */ }
  /* 标签恢复失败不得中断其后的脚本求值（历史 TDZ 教训） */
  try { switchSettingsTab(tab); } catch (e) { /* ignore */ }
})();

function fillSettings() {
  const s = state.settings;
  $('#sessTag').textContent = s.sessdata_set ? '已配置' : '未配置';
  $('#sessTag').className = 'tag' + (s.sessdata_set ? ' ok' : '');
  $('#setSess').value = '';
  $('#setSess').placeholder = s.sessdata_set
    ? '已配置，留空保存则保持不变' : '粘贴 SESSDATA 值；游客模式可留空但稳定性较差';
  $('#setSalt').value = s.anon_salt || '';
  $('#setDataDir').value = s.data_dir || '';
  $('#dataDirEff').textContent = s.data_dir_effective || '—';
  $('#setLogDir').value = s.log_dir || '';
  updateLogDirEff();
  $('#swSc').checked = s.record_sc !== false;
  $('#swGifts').checked = !!s.record_gifts;
  $('#swEmoticon').checked = s.filter_pure_emoticon !== false;
  $('#swModeration').checked = s.record_moderation !== false;
  $('#swAnon').checked = s.anonymize !== false;
  $('#swSanitize').checked = s.sanitize_text !== false;
  $('#swAutoStart').checked = s.auto_start !== false;
  $('#setPoll').value = s.offline_poll_sec || 45;
  $('#setIdle').value = s.idle_stop_minutes ?? 0;
  $('#swVideoEnabled').checked = !!s.video_enabled;
  const qnSel = $('#setVideoQn');
  const qnVal = String(s.video_qn ?? 10000);
  qnSel.value = [...qnSel.options].some((o) => o.value === qnVal) ? qnVal : '10000';
  $('#setVideoMinFree').value = s.video_min_free_gb ?? 5;
  $('#setVideoDir').value = s.video_dir || '';
  updateVideoDirEff();
  // AI 高能解读
  $('#swLlmAuto').checked = !!s.llm_auto_analyze;
  $('#setLlmBase').value = s.llm_base_url || '';
  $('#setLlmKey').value = '';
  $('#setLlmModel').value = s.llm_model || '';
  const lm = s.llm_mode === 'C' ? 'C' : 'A';
  const lmRadio = document.querySelector(`input[name="llmMode"][value="${lm}"]`);
  if (lmRadio) lmRadio.checked = true;
  $('#llmKeyTag').textContent = s.llm_key_set ? '已保存' : '未配置';
  $('#llmKeyTag').className = 'tag' + (s.llm_key_set ? ' ok' : '');
  $('#setLlmKey').placeholder = s.llm_key_set
    ? '已保存 Key，留空保存则保持不变' : '填写 API Key（仅存本机服务端，不回显）';
  $('#llmTestResult').textContent = '';
  setSettingsDirty(false);
}

/* ------- 统一保存：脏状态 + 底部保存条 + 离开拦截 ------- */
let settingsDirty = false;
function setSettingsDirty(v) {
  settingsDirty = !!v;
  const bar = $('#settingsDirtyBar');
  if (bar) bar.hidden = !settingsDirty;
  if (!settingsDirty) $('#saveResult').textContent = '';
}

const SETTINGS_FIELDS = ['#setSess', '#setSalt', '#setDataDir', '#setLogDir',
  '#swSc', '#swGifts', '#swEmoticon', '#swModeration', '#swAnon', '#swSanitize', '#swAutoStart',
  '#setPoll', '#setIdle',
  '#swVideoEnabled', '#setVideoQn', '#setVideoMinFree', '#setVideoDir',
  '#swLlmAuto', '#setLlmBase', '#setLlmKey', '#setLlmModel'];
SETTINGS_FIELDS.forEach((sel) => {
  const el = $(sel);
  if (el) { el.addEventListener('change', () => setSettingsDirty(true));
            el.addEventListener('input', () => setSettingsDirty(true)); }
});
document.querySelectorAll('input[name="llmMode"]').forEach((el) =>
  el.addEventListener('change', () => setSettingsDirty(true)));

/* 收集表单为设置补丁（密码类留空=不修改） */
function collectSettingsPatch() {
  const sess = $('#setSess').value.trim();
  const llmKey = $('#setLlmKey').value.trim();
  const llmMode = (document.querySelector('input[name="llmMode"]:checked') || {}).value || 'A';
  const patch = {
    anon_salt: $('#setSalt').value.trim(),
    data_dir: $('#setDataDir').value.trim(),
    log_dir: $('#setLogDir').value.trim(),
    record_sc: $('#swSc').checked,
    record_gifts: $('#swGifts').checked,
    filter_pure_emoticon: $('#swEmoticon').checked,
    record_moderation: $('#swModeration').checked,
    anonymize: $('#swAnon').checked,
    sanitize_text: $('#swSanitize').checked,
    auto_start: $('#swAutoStart').checked,
    offline_poll_sec: Number($('#setPoll').value) || 45,
    idle_stop_minutes: Number($('#setIdle').value) || 0,
    video_enabled: $('#swVideoEnabled').checked,
    video_qn: Number($('#setVideoQn').value) || 10000,
    video_min_free_gb: Number($('#setVideoMinFree').value) || 5,
    video_dir: $('#setVideoDir').value.trim(),
    llm_auto_analyze: $('#swLlmAuto').checked,
    llm_base_url: $('#setLlmBase').value.trim(),
    llm_model: $('#setLlmModel').value.trim(),
    llm_mode: llmMode,
  };
  if (sess) patch.sessdata = sess;
  if (llmKey) patch.llm_api_key = llmKey;
  return patch;
}

async function saveSettings() {
  $('#saveResult').textContent = '保存中…';
  try {
    const saved = await api('/api/settings', { method: 'PUT', body: collectSettingsPatch() });
    state.settings = saved || state.settings;
    fillSettings();          // 用服务端最终值重置表单 + 清除脏状态
    updateVideoDirEff();
    setSettingsDirty(false);
    toast('设置已保存（部分开关对新启动的任务生效）', 'ok');
    return true;
  } catch (err) {
    $('#saveResult').textContent = '❌ ' + err.message;
    toast(err.message, 'err');
    return false;
  }
}
$('#btnSaveSettings').addEventListener('click', saveSettings);
$('#btnDiscardSettings').addEventListener('click', () => {
  fillSettings();
  toast('已放弃未保存的修改', 'ok');
});

$('#toggleSess').addEventListener('click', () => {
  const i = $('#setSess');
  const show = i.type === 'password';
  i.type = show ? 'text' : 'password';
  $('#toggleSess').textContent = show ? '显示' : '隐藏';
});

/* ------- SESSDATA：粘贴整段 Cookie 自动提取 ------- */
function extractSessdata(text) {
  const m = /(?:^|;\s*|^cookie:\s*)SESSDATA=([^;"'\s]+)/i.exec(text || '');
  if (!m) return '';
  try { return decodeURIComponent(m[1]); } catch (_) { return m[1]; }
}
$('#setSess').addEventListener('paste', (e) => {
  const text = (e.clipboardData || window.clipboardData).getData('text') || '';
  if (!/SESSDATA=/i.test(text)) return; // 粘贴的就是单个值，走默认行为
  const sess = extractSessdata(text);
  if (!sess) return;
  e.preventDefault();
  const inp = $('#setSess');
  inp.value = sess;
  inp.type = 'text';
  $('#toggleSess').textContent = '隐藏';
  setSettingsDirty(true);
  toast('已从整段 Cookie 中自动提取 SESSDATA，记得点「保存设置」', 'ok');
});

/* ------- SESSDATA：B 站 App 扫码获取（后端代理，无需 F12）------- */
const qrLogin = { key: '', timer: null, dead: false };

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function qrSetStatus(html, cls) {
  const el = $('#qrLoginStatus');
  el.className = 'qr-status' + (cls ? ' ' + cls : '');
  el.innerHTML = html;
}
function qrStop() {
  qrLogin.dead = true;
  clearTimeout(qrLogin.timer);
}
function qrClose() {
  $('#qrLoginMask').hidden = true;
  qrStop();
}
async function qrStart() {
  clearTimeout(qrLogin.timer);
  qrLogin.key = '';
  qrLogin.dead = false;
  $('#qrLoginMask').hidden = false;
  $('#qrLoginImg').hidden = true;
  $('#qrLoginRefresh').hidden = true;
  const loading = $('#qrLoginLoading');
  loading.hidden = false;
  loading.textContent = '正在向 B 站申请二维码…';
  qrSetStatus('请使用 <b>哔哩哔哩手机 App</b> 扫码（左上角菜单 → 扫一扫）', '');
  try {
    const r = await api('/api/bili-qrcode/generate', { method: 'POST' });
    qrLogin.key = r.qrcode_key;
    const img = $('#qrLoginImg');
    img.src = r.qr_image;
    loading.hidden = true;
    img.hidden = false;
    qrPollLoop();
  } catch (err) {
    loading.textContent = '二维码加载失败';
    qrSetStatus('申请二维码失败：' + escapeHtml(err.message)
      + '<br>可重试，或改用下方「方式二」手动复制', 'err');
    $('#qrLoginRefresh').hidden = false;
  }
}
function qrPollLoop() {
  qrLogin.timer = setTimeout(async () => {
    if (qrLogin.dead || $('#qrLoginMask').hidden) return;
    let r;
    try {
      r = await api('/api/bili-qrcode/poll?qrcode_key='
        + encodeURIComponent(qrLogin.key));
    } catch (err) {
      qrSetStatus('查询扫码状态失败，2 秒后重试…', 'warn');
      qrLogin.timer = setTimeout(qrPollLoop, 2000);
      return;
    }
    if (r.status === 'success') {
      qrStop();
      qrSetStatus('✅ 扫码成功，SESSDATA 已自动填入', 'ok');
      const inp = $('#setSess');
      inp.value = r.sessdata;
      inp.type = 'text';
      $('#toggleSess').textContent = '隐藏';
      setSettingsDirty(true);
      setTimeout(qrClose, 1000);
      toast('SESSDATA 已通过扫码填入，请点底部「保存设置」', 'ok');
      return;
    }
    if (r.status === 'scanned') {
      qrSetStatus('✅ 已扫码，请在 <b>手机上点「确认登录」</b>', 'warn');
    } else if (r.status === 'expired') {
      qrStop();
      qrSetStatus('二维码已过期，请点下方按钮刷新', 'err');
      $('#qrLoginRefresh').hidden = false;
      return;
    }
    qrPollLoop();
  }, 1800);
}
$('#btnQrLogin').addEventListener('click', qrStart);
$('#qrLoginClose').addEventListener('click', qrClose);
$('#qrLoginRefresh').addEventListener('click', qrStart);
$('#qrLoginMask').addEventListener('click', (e) => {
  if (e.target.id === 'qrLoginMask') qrClose();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#qrLoginMask').hidden) qrClose();
});

/* 目录输入：实时预览生效路径（视频目录留空=数据目录/video 子目录） */
function updateVideoDirEff() {
  const custom = $('#setVideoDir').value.trim().replace(/^"|"$/g, '');
  const dataBase = $('#setDataDir').value.trim().replace(/^"|"$/g, '')
    || state.settings.data_dir_effective || '—';
  $('#videoDirEff').textContent = custom
    || `${dataBase}\\${state.settings.video_subdir || 'video'}`;
}
$('#setVideoDir').addEventListener('input', updateVideoDirEff);
/* 日志目录留空=数据目录/logs；与数据目录输入联动预览 */
function updateLogDirEff() {
  const custom = $('#setLogDir').value.trim().replace(/^"|"$/g, '');
  const dataBase = $('#setDataDir').value.trim().replace(/^"|"$/g, '')
    || state.settings.data_dir_effective || '—';
  $('#logDirEff').textContent = custom || `${dataBase}\\logs`;
}
$('#setLogDir').addEventListener('input', updateLogDirEff);
$('#setDataDir').addEventListener('input', () => {
  $('#dataDirEff').textContent = $('#setDataDir').value.trim().replace(/^"|"$/g, '')
    || state.settings.data_dir_effective || '—';
  updateVideoDirEff();
  updateLogDirEff();
});

$('#btnTestCookie').addEventListener('click', async () => {
  const sess = $('#setSess').value.trim();
  $('#cookieResult').textContent = '检测中…';
  try {
    const r = await api('/api/settings/test-cookie',
      { method: 'POST', body: { sessdata: sess || null } });
    if (r.ok) {
      $('#cookieResult').innerHTML =
        `✅ 登录有效：<b>${fmt.esc(r.uname)}</b>（UID ${r.mid}）`;
    } else {
      $('#cookieResult').textContent = '❌ ' + r.message;
    }
  } catch (err) { $('#cookieResult').textContent = '❌ ' + err.message; }
});

$('#toggleLlmKey').addEventListener('click', () => {
  const i = $('#setLlmKey');
  const show = i.type === 'password';
  i.type = show ? 'text' : 'password';
  $('#toggleLlmKey').textContent = show ? '隐藏' : '显示';
});

$('#llmPresets').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-base]');
  if (!b) return;
  $('#setLlmBase').value = b.dataset.base;
  $('#setLlmModel').value = b.dataset.model;
  $('#llmTestResult').textContent = `已填充 ${b.textContent.trim()} 模板，本地 Ollama 的 Key 可随意填（如 ollama）`;
  setSettingsDirty(true);
});

$('#btnTestLlm').addEventListener('click', async () => {
  const base = $('#setLlmBase').value.trim();
  const model = $('#setLlmModel').value.trim();
  const key = $('#setLlmKey').value.trim();
  const out = $('#llmTestResult');
  if (!base || !model) {
    out.textContent = '❌ 请先填写（或点模板填充）Base URL 与模型名';
    return;
  }
  out.textContent = '连接中…';
  try {
    const body = { base_url: base, model };
    if (key) body.api_key = key;   // 留空则后端用已保存的 Key
    const r = await api('/api/settings/test-llm', { method: 'POST', body });
    if (r.ok) {
      out.innerHTML = `✅ 连通（${r.latency_ms}ms）模型回复：${fmt.esc(r.reply || '正常')}`;
    } else {
      out.textContent = '❌ ' + (r.message || '连接失败');
    }
  } catch (err) { out.textContent = '❌ ' + err.message; }
});

/* ---------------- 关心用户名单 ---------------- */
async function loadCared() {
  let list = [];
  try { list = (await api('/api/cared-users')).cared_users || []; }
  catch (err) { /* 静默，设置页其它内容不受影响 */ }
  const tb = $('#caredTable');
  $('#caredEmpty').hidden = list.length > 0;
  tb.innerHTML = list.map((u) => `
    <tr>
      <td class="mono">${fmt.esc(u.key)}</td>
      <td>${fmt.esc(u.note || '')}</td>
      <td>${u.added_at ? new Date(u.added_at * 1000).toLocaleString('zh-CN', { hour12: false }) : '—'}</td>
      <td><button class="btn btn-sm btn-danger" data-cared-del="${fmt.esc(u.key)}">删除</button></td>
    </tr>`).join('');
  return new Set(list.map((u) => u.key));
}

$('#btnAddCared').addEventListener('click', async () => {
  const key = $('#caredKey').value.trim();
  const note = $('#caredNote').value.trim();
  if (!key) return toast('请填写 UID 或匿名 ID', 'warn');
  try {
    await api('/api/cared-users', { method: 'POST', body: { key, note } });
    $('#caredKey').value = ''; $('#caredNote').value = '';
    toast('已加入关心名单', 'ok');
    loadCared();
  } catch (err) { toast(err.message, 'err'); }
});

$('#caredTable').addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-cared-del]');
  if (!btn) return;
  try {
    await api(`/api/cared-users/${encodeURIComponent(btn.dataset.caredDel)}`, { method: 'DELETE' });
    toast('已移出关心名单', 'ok');
    loadCared();
  } catch (err) { toast(err.message, 'err'); }
});

/* ---------------- 战报样式 ---------------- */
let thData = null;

async function loadTheme() {
  try { thData = await api('/api/report-theme'); }
  catch (err) { return; }  // 加载失败不阻断设置页其它功能
  fillTheme();
}

function normHex(v) {
  let s = String(v).trim();
  if (/^#[0-9a-fA-F]{3}$/.test(s)) {
    s = '#' + s[1] + s[1] + s[2] + s[2] + s[3] + s[3];
  }
  return s.toUpperCase();
}

function validHex(v) {
  return /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(String(v).trim());
}

function thOriMeta() {
  const key = $('#thOrientation').value || 'portrait';
  return thData.meta.orientations.find((o) => o.key === key)
    || thData.meta.orientations[0];
}

/* 按当前画布方向重建「画布宽度 / 指标卡每行个数」下拉。
   adoptDefault=true（用户主动切换方向）：直接选该方向推荐默认值；
   false（加载/保存后回填）：旧配置若不属于该方向推荐值，追加「（当前值）」
   选项保留，不静默改动用户排版 */
function rebuildThSizeOptions(adoptDefault) {
  const o = thOriMeta();
  const wSel = $('#thWidth');
  const cSel = $('#thCardCols');
  const curW = Number(wSel.value || thData.theme.width);
  const curC = Number(cSel.value || thData.theme.card_cols);
  wSel.innerHTML = o.widths.map((w) => `<option value="${w}">${w} px</option>`).join('');
  cSel.innerHTML = o.card_cols.map((n) =>
    `<option value="${n}">每行 ${n} 个</option>`).join('');
  if (adoptDefault) {
    wSel.value = String(o.default_width);
    cSel.value = String(o.default_card_cols);
  } else {
    if (!o.widths.includes(curW)) {
      wSel.insertAdjacentHTML('afterbegin',
        `<option value="${curW}">${curW} px（当前值）</option>`);
    }
    wSel.value = String(curW);
    if (!o.card_cols.includes(curC)) {
      cSel.insertAdjacentHTML('afterbegin',
        `<option value="${curC}">每行 ${curC} 个（当前值）</option>`);
    }
    cSel.value = String(curC);
  }
}

$('#thOrientation').addEventListener('change', () => {
  rebuildThSizeOptions(true);
  toast('已切换画布方向，宽度与每行卡片数换为该方向推荐值，可先「生成预览」查看', 'ok');
});

function fillTheme() {
  const { theme, meta } = thData;
  $('#thColors').innerHTML = meta.colors.map((c) => `
    <div class="th-color">
      <input type="color" data-th-color="${c.key}" value="${(theme[c.key] || '#000000').toLowerCase()}">
      <input type="text" class="input th-hex" data-th-hex="${c.key}"
             value="${theme[c.key] || ''}" maxlength="7" spellcheck="false">
      <span class="th-color-name">${c.label}</span>
    </div>`).join('');
  $('#thOrientation').innerHTML = meta.orientations.map((o) =>
    `<option value="${o.key}">${o.label}</option>`).join('');
  $('#thOrientation').value = theme.orientation || 'portrait';
  rebuildThSizeOptions(false);
  // font_scale=1.0 经 JSON 往返会变成数字 1，需按数值匹配 option
  const fsSel = $('#thFontScale');
  const fsOpt = Array.from(fsSel.options)
    .find((o) => Number(o.value) === Number(theme.font_scale));
  fsSel.value = fsOpt ? fsOpt.value : '1.0';
  $('#thFontRegular').value = theme.font_regular || '';
  $('#thFontBold').value = theme.font_bold || '';
  $('#thBrand').value = theme.brand || '';
  $('#thFooter').value = theme.footer_note || '';
  $('#thShowCover').checked = theme.show_cover !== false;
  $('#thShowTopTexts').checked = theme.show_top_texts !== false;
  $('#thShowTopSc').checked = theme.show_top_sc !== false;
  $('#thShowTopMedals').checked = theme.show_top_medals !== false;
  $('#thShowFooter').checked = theme.show_footer !== false;
  $('#thCards').innerHTML = meta.cards.map((c) => `
    <label class="th-switch">
      <input type="checkbox" data-th-card="${c.key}"
             ${theme['card_' + c.key] !== false ? 'checked' : ''}>
      <span>${c.label}</span>
    </label>`).join('');
  renderThSections();
  $('#thResult').textContent = '';
}

function renderThSections() {
  const labels = Object.fromEntries(
    thData.meta.sections.map((s) => [s.key, s.label]));
  const arr = thData.theme.sections;
  $('#thSections').innerHTML = arr.map((key, i) => `
    <div class="th-sec-row">
      <label class="th-switch">
        <input type="checkbox" data-th-sec="${key}"
               ${thData.theme['show_' + key] !== false ? 'checked' : ''}>
        <span>${labels[key] || key}</span>
      </label>
      <span class="th-order">
        <button class="btn btn-sm" type="button" data-th-up="${key}"
                ${i === 0 ? 'disabled' : ''}>↑</button>
        <button class="btn btn-sm" type="button" data-th-down="${key}"
                ${i === arr.length - 1 ? 'disabled' : ''}>↓</button>
      </span>
    </div>`).join('');
}

function syncSectionChecks() {
  $$('#thSections [data-th-sec]').forEach((cb) => {
    thData.theme['show_' + cb.dataset.thSec] = cb.checked;
  });
}

function collectTheme() {
  const t = { ...thData.theme };
  syncSectionChecks();
  $$('#thColors [data-th-color]').forEach((inp) => {
    t[inp.dataset.thColor] = normHex(inp.value);
  });
  t.font_regular = $('#thFontRegular').value.trim();
  t.font_bold = $('#thFontBold').value.trim();
  t.font_scale = Number($('#thFontScale').value);
  t.orientation = $('#thOrientation').value || 'portrait';
  t.width = Number($('#thWidth').value);
  t.card_cols = Number($('#thCardCols').value);
  t.brand = $('#thBrand').value.trim();
  t.footer_note = $('#thFooter').value.trim();
  t.show_cover = $('#thShowCover').checked;
  t.show_top_texts = $('#thShowTopTexts').checked;
  t.show_top_sc = $('#thShowTopSc').checked;
  t.show_top_medals = $('#thShowTopMedals').checked;
  t.show_footer = $('#thShowFooter').checked;
  $$('#thCards [data-th-card]').forEach((cb) => {
    t['card_' + cb.dataset.thCard] = cb.checked;
  });
  return t;
}

// 取色器 ↔ 十六进制文本双向联动
$('#thColors').addEventListener('input', (e) => {
  if (!e.target.matches('[data-th-color]')) return;
  const key = e.target.dataset.thColor;
  const hex = $(`#thColors [data-th-hex="${key}"]`);
  hex.value = e.target.value.toUpperCase();
  hex.classList.remove('bad');
});
$('#thColors').addEventListener('change', (e) => {
  if (!e.target.matches('[data-th-hex]')) return;
  const inp = e.target;
  if (validHex(inp.value)) {
    inp.value = normHex(inp.value);
    $(`#thColors [data-th-color="${inp.dataset.thHex}"]`).value =
      inp.value.toLowerCase();
    inp.classList.remove('bad');
  } else {
    inp.classList.add('bad');
  }
});

// 区块上移 / 下移
$('#thSections').addEventListener('click', (e) => {
  const up = e.target.closest('[data-th-up]');
  const dn = e.target.closest('[data-th-down]');
  if (!up && !dn) return;
  syncSectionChecks();
  const key = up ? up.dataset.thUp : dn.dataset.thDown;
  const arr = thData.theme.sections;
  const i = arr.indexOf(key);
  const j = up ? i - 1 : i + 1;
  if (i < 0 || j < 0 || j >= arr.length) return;
  [arr[i], arr[j]] = [arr[j], arr[i]];
  renderThSections();
});

// 配色预设（仅覆盖 12 个颜色，字体/排版/开关保持当前编辑态）
$('.th-toolbar').addEventListener('click', (e) => {
  const b = e.target.closest('[data-th-preset]');
  if (!b || !thData) return;
  const preset = thData.presets[b.dataset.thPreset];
  if (!preset) return;
  $$('#thColors [data-th-color]').forEach((inp) => {
    inp.value = normHex(preset[inp.dataset.thColor]).toLowerCase();
  });
  $$('#thColors [data-th-hex]').forEach((inp) => {
    inp.value = normHex(preset[inp.dataset.thHex]);
    inp.classList.remove('bad');
  });
  toast(`已套用${b.dataset.thPreset === 'light' ? '浅色' : '深色'}配色，确认后点击「保存样式」`, 'ok');
});

$('#btnThSave').addEventListener('click', async () => {
  if ($$('#thColors .th-hex.bad').length) {
    return toast('存在不合法的颜色值，应为 #RRGGBB 格式', 'err');
  }
  $('#thResult').textContent = '保存中…';
  try {
    const r = await api('/api/report-theme',
      { method: 'PUT', body: collectTheme() });
    thData.theme = r.theme;
    fillTheme();
    $('#thResult').textContent = '✅ 已保存，重新生成战报即按新样式渲染';
    toast('战报样式已保存', 'ok');
  } catch (err) {
    $('#thResult').textContent = '❌ ' + err.message;
    toast(err.message, 'err');
  }
});

$('#btnThReset').addEventListener('click', async () => {
  try {
    const r = await api('/api/report-theme', { method: 'DELETE' });
    thData.theme = r.theme;
    fillTheme();
    $('#thResult').textContent = '已恢复并保存为默认样式';
    toast('已恢复默认战报样式', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

async function genThPreview() {
  if ($$('#thColors .th-hex.bad').length) {
    return toast('存在不合法的颜色值，应为 #RRGGBB 格式', 'err');
  }
  const btns = [$('#btnThPreview'), $('#btnThPreviewAgain')];
  btns.forEach((b) => { b.disabled = true; b.textContent = '绘制中…'; });
  try {
    const res = await fetch('/api/report-theme/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectTheme()),
    });
    if (!res.ok) {
      let msg = res.statusText;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      throw new Error(msg);
    }
    const url = URL.createObjectURL(await res.blob());
    const img = $('#thPreviewImg');
    if (img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
    img.src = url;
    const box = $('#thPreviewBox');
    box.hidden = false;
    // 高亮闪烁一次，明确提示预览已更新
    box.classList.remove('th-flash');
    void box.offsetWidth;
    box.classList.add('th-flash');
    box.addEventListener('animationend', () => box.classList.remove('th-flash'), { once: true });
    // 预览框在长面板最底部，自动平滑滚入视口，避免用户没往下拖而发现不了
    requestAnimationFrame(() => {
      box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  } catch (err) { toast(err.message, 'err'); }
  finally {
    $('#btnThPreview').textContent = '生成预览';
    $('#btnThPreviewAgain').textContent = '重新生成';
    btns.forEach((b) => { b.disabled = false; });
  }
}
$('#btnThPreview').addEventListener('click', genThPreview);
$('#btnThPreviewAgain').addEventListener('click', genThPreview);
$('#btnThPreviewClose').addEventListener('click', () => {
  $('#thPreviewBox').hidden = true;
});

/* 预览图点击放大：战报为竖屏长图，面板宽度内文字偏小，遮罩内可上下滚动查看 */
const thZoomMask = $('#thZoomMask');
function closeThZoom() { thZoomMask.hidden = true; }
$('#thPreviewImg').addEventListener('click', () => {
  if (!$('#thPreviewImg').src) return;
  $('#thZoomImg').src = $('#thPreviewImg').src;
  $('#thZoomScroll').scrollTop = 0;
  thZoomMask.hidden = false;
});
$('#thZoomClose').addEventListener('click', closeThZoom);
thZoomMask.addEventListener('click', (e) => {
  if (e.target === thZoomMask || e.target === $('#thZoomScroll')) closeThZoom();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !thZoomMask.hidden) closeThZoom();
});

/* ---------------- 弹幕样式（ASS 观感配置） ---------------- */
/* 必须用 var：恢复上次设置子标签的 IIFE（本文件中段）可能在本行求值前
   同步调用 startDsPreview()，let/const 会触发 TDZ 并中断整个脚本求值 */
var dsData = null;
const DS_CROSS = { slow: 11, normal: 9, fast: 7 };
const DS_SAMPLES = ['哈哈哈哈哈哈', '?', '6666', '主播好强', '这条弹幕很长很长很长很长啊',
  '绷', '233333', '来晚了', '前排围观', 'nice', 'awsl', '下次一定', '关注了关注了',
  '冲鸭', 'good game', '绝绝子', '笑死我了', '这波操作可以'];
const DS_COLORS = ['#ff5c7c', '#30d7ff', '#42d392', '#f6c34b', '#ff8a00',
  '#ff69b4', '#8fc0ff'];

/* ds 面板是否处于激活态：供 loadDs 判断是否需要补启预览定时器 */
function dsPanelActive() {
  const p = document.querySelector('.settings-tabpanel[data-panel="ds"]');
  return !!(p && p.classList.contains('active'));
}

async function loadDs() {
  if (!dsData) {   // 保存/恢复后本地即最新，整轮页面只拉一次
    try { dsData = await api('/api/danmaku-style'); }
    catch (err) { return; }
    fillDs();
    applyDsStatic();
  }
  /* 子标签被记忆为 ds 时，脚本加载期的 restoreSettingsTab 会在 dsData 就绪前调用
     startDsPreview() 并直接返回；此处补启，否则滚动弹幕的定时器永远不会启动 */
  if (dsPanelActive()) startDsPreview();
}

function dsFillSelect(sel, items, val) {
  sel.innerHTML = items.map((o) =>
    `<option value="${o.key}">${fmt.esc(o.label)}</option>`).join('');
  sel.value = String(val);
}

function fillDs() {
  const { style, meta } = dsData;
  $$('input[name="dsCanvasMode"]').forEach((r) => {
    r.checked = r.value === style.canvas_mode;
  });
  $('#dsManualBox').classList.toggle('disabled', style.canvas_mode !== 'manual');
  $('#dsCanvasW').value = style.canvas_w;
  $('#dsCanvasH').value = style.canvas_h;
  $('#dsFontFamily').value = style.font_family;
  dsFillSelect($('#dsFontScale'), meta.font_scales, style.font_scale);
  dsFillSelect($('#dsOutline'), meta.outlines, style.outline);
  dsFillSelect($('#dsSpeed'), meta.speeds, style.speed);
  dsFillSelect($('#dsArea'), meta.areas, style.area_ratio);
  dsFillSelect($('#dsOpacity'), meta.opacities, style.opacity);
  $('#dsBold').checked = !!style.bold;
  $('#dsShowColor').checked = !!style.show_color;
  $('#dsFixedMode').checked = !!style.fixed_mode;
  $('#dsScCard').checked = !!style.sc_card;
}

function collectDs() {
  const modeEl = $$('input[name="dsCanvasMode"]').find((r) => r.checked);
  const w = parseInt($('#dsCanvasW').value, 10);
  const h = parseInt($('#dsCanvasH').value, 10);
  return {
    canvas_mode: modeEl ? modeEl.value : 'auto',
    canvas_w: Number.isFinite(w) ? w : 1920,
    canvas_h: Number.isFinite(h) ? h : 1080,
    font_family: $('#dsFontFamily').value.trim() || 'Microsoft YaHei',
    font_scale: Number($('#dsFontScale').value),
    outline: Number($('#dsOutline').value),
    speed: $('#dsSpeed').value,
    area_ratio: Number($('#dsArea').value),
    opacity: Number($('#dsOpacity').value),
    bold: $('#dsBold').checked,
    show_color: $('#dsShowColor').checked,
    fixed_mode: $('#dsFixedMode').checked,
    sc_card: $('#dsScCard').checked,
  };
}

$('#btnDsSave').addEventListener('click', async () => {
  $('#dsResult').textContent = '保存中…';
  try {
    const r = await api('/api/danmaku-style', { method: 'PUT', body: collectDs() });
    dsData.style = r.style;
    fillDs();
    applyDsStatic();
    $('#dsResult').textContent = '已保存，之后生成 / 重新生成的字幕立即采用';
    toast('弹幕样式已保存', 'ok');
  } catch (err) {
    $('#dsResult').textContent = '保存失败：' + err.message;
    toast(err.message, 'err');
  }
});

$('#btnDsReset').addEventListener('click', async () => {
  try {
    const r = await api('/api/danmaku-style', { method: 'DELETE' });
    dsData.style = r.style;
    fillDs();
    applyDsStatic();
    $('#dsResult').textContent = '已恢复并保存为默认样式';
    toast('已恢复默认弹幕样式', 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

/* 「预览弹幕」：预览常驻在表单下方，点击后平滑滚入视口并高亮一次，避免用户手动拖到底部 */
$('#btnDsPreviewJump').addEventListener('click', () => {
  const box = document.querySelector('.ds-preview-col');
  if (!box) return;
  box.classList.remove('ds-flash');
  void box.offsetWidth;
  box.classList.add('ds-flash');
  box.addEventListener('animationend', () => box.classList.remove('ds-flash'), { once: true });
  requestAnimationFrame(() => box.scrollIntoView({ behavior: 'smooth', block: 'start' }));
});

/* ------- 实时预览：与后端同参数的迷你弹幕模拟 ------- */
/* 预览画布比例跟随画布设置：自动 16:9，手动取手填宽高比（含竖屏） */
function dsApplyRatio(st) {
  let rw = 16, rh = 9;
  if (st.canvas_mode === 'manual' && st.canvas_w >= 320 && st.canvas_h >= 320) {
    rw = st.canvas_w; rh = st.canvas_h;
  }
  const box = $('#dsPreview');
  box.style.setProperty('--ds-rw', String(rw));
  box.style.setProperty('--ds-rh', String(rh));
}

function dsGeom(st) {
  const rect = $('#dsPreview').getBoundingClientRect();
  if (rect.width < 10 || rect.height < 10) return null;
  const W = rect.width, H = rect.height;
  const S = Math.max(12, H / 1080 * 39 * Number(st.font_scale));
  const LH = S * 1.25;
  const margin = Math.max(3, H * 0.02);
  const n = Math.max(1, Math.floor((H * Number(st.area_ratio) - margin) / LH));
  const v = W / (DS_CROSS[st.speed] || 9);   // px/s
  return { W, H, S, LH, margin, n, v };
}

function dsTextShadow(outline, S) {
  const p = Math.max(0.8, Number(outline) * S / 22);
  const dirs = [[-1, 0], [1, 0], [0, -1], [0, 1], [-1, -1], [1, -1], [-1, 1], [1, 1]];
  return dirs.map(([dx, dy]) => `${dx * p}px ${dy * p}px 0 #000`).join(',');
}

function dsTick() {
  const st = collectDs();
  const g = dsGeom(st);
  if (!g) return;
  /* 预览框尺寸（含手动画布改比例/窗口缩放）变化：旧像素坐标的在途弹幕作废，车道重建 */
  if (g.n !== dsLanesN || g.W !== dsBoxW || g.H !== dsBoxH) {
    dsLanesN = g.n; dsBoxW = g.W; dsBoxH = g.H;
    dsLanes = new Array(g.n).fill(null);
    $$('#dsPreview .ds-dm').forEach((el) => el.remove());
  }
  const box = $('#dsPreview');
  const t = performance.now() / 1000;
  const gap = g.S * 0.25;
  const laneFirst = st.fixed_mode ? 1 : 0;
  const laneLast = st.fixed_mode ? g.n - 2 : g.n - 1;
  const spawnOne = () => {
    if (!dsTimer) return;   // 已离开面板：丢弃排队中的补发
    if (laneFirst > laneLast) return;
    let lane = -1;
    for (let i = laneFirst; i <= laneLast; i++) {
      const L = dsLanes[i];
      const dt = L ? g.v * (t - L.t) : 0;
      if (!L || dt >= g.W || dt >= L.w + gap) { lane = i; break; }
    }
    if (lane < 0) return;   // 车道全忙：模拟后端丢弃
    const text = DS_SAMPLES[Math.floor(Math.random() * DS_SAMPLES.length)];
    const el = document.createElement('span');
    el.className = 'ds-dm';
    el.textContent = text;
    el.style.top = `${g.margin + lane * g.LH}px`;
    el.style.left = `${g.W}px`;
    el.style.fontFamily = `"${st.font_family.replace(/["\\]/g, '')}", "Microsoft YaHei", sans-serif`;
    el.style.fontSize = `${g.S}px`;
    el.style.fontWeight = st.bold ? '700' : '400';
    el.style.color = (st.show_color && Math.random() > 0.35)
      ? DS_COLORS[Math.floor(Math.random() * DS_COLORS.length)] : '#fff';
    el.style.textShadow = dsTextShadow(st.outline, g.S);
    box.appendChild(el);
    const w = el.offsetWidth;
    dsLanes[lane] = { t, w };
    const dur = (g.W + w) / g.v * 1000;
    const anim = el.animate(
      [{ left: `${g.W}px` }, { left: `${-w}px` }],
      { duration: dur, easing: 'linear', fill: 'forwards' });
    anim.onfinish = () => el.remove();
  };
  spawnOne();
  if (Math.random() < 0.4) setTimeout(spawnOne, 240);
}

function applyDsStatic() {
  if (!dsData) return;
  const st = collectDs();
  const box = $('#dsPreview');
  dsApplyRatio(st);   // 必须先写比例变量，下面 dsGeom 读 rect 才能拿到新尺寸
  const g = dsGeom(st);
  box.style.setProperty('--ds-area-h', `${Number(st.area_ratio) * 100}%`);
  box.style.setProperty('--ds-op', String(st.opacity));
  if (g) {
    box.style.setProperty('--ds-fs', `${g.S}px`);
    box.style.setProperty('--ds-fw', st.bold ? '700' : '400');
  }
  const sh = g ? dsTextShadow(st.outline, g.S) : '0 1px 1px #000';
  const fam = `"${st.font_family.replace(/["\\]/g, '')}", "Microsoft YaHei", sans-serif`;
  const top = $('#dsFixedTop'), bot = $('#dsFixedBottom');
  top.hidden = !st.fixed_mode;
  bot.hidden = !st.fixed_mode;
  if (g) {
    top.style.top = `${g.margin}px`;
    bot.style.bottom = `${g.margin}px`;
  }
  [top, bot].forEach((el) => {
    el.style.fontFamily = fam;
    el.style.textShadow = sh;
  });
  top.style.color = st.show_color ? '#ffd700' : '#fff';
  bot.style.color = st.show_color ? '#39ff7a' : '#fff';
  // SC：卡片 / 金色底行二选一（¥100 档：深绿卡 + 亮绿强调，与后端等级色一致）
  const card = $('#dsScSample'), plain = $('#dsScPlain');
  card.hidden = !st.sc_card;
  plain.hidden = st.sc_card;
  if (g) {
    const hs = Math.max(10, g.S * 0.72), bs = Math.max(11, g.S * 0.92);
    $('#dsScSample .ds-sc-head').style.fontSize = `${hs}px`;
    $('#dsScSample .ds-sc-body').style.fontSize = `${bs}px`;
    plain.style.bottom = st.fixed_mode ? `${g.margin + g.LH + 8}px` : '8px';
  }
  plain.style.fontFamily = fam;
  plain.style.textShadow = sh;
}

function startDsPreview() {
  if (!dsData) return;
  applyDsStatic();
  if (dsTimer) return;
  dsTick();
  dsTimer = setInterval(dsTick, 650);
}
function stopDsPreview() {
  if (dsTimer) { clearInterval(dsTimer); dsTimer = null; }
  $$('#dsPreview .ds-dm').forEach((el) => el.remove());
}

/* 面板内任意改动：手动画布框启停 + 即时预览（独立保存，不触发统一保存条） */
function onDsChange(e) {
  if (e.target.name === 'dsCanvasMode') {
    const manual = $$('input[name="dsCanvasMode"]').find((r) => r.checked);
    $('#dsManualBox').classList.toggle('disabled',
      !(manual && manual.value === 'manual'));
  }
  applyDsStatic();
}
const dsPanel = document.querySelector('.settings-tabpanel[data-panel="ds"]');
dsPanel.addEventListener('input', onDsChange);
dsPanel.addEventListener('change', onDsChange);
/* 窗口缩放：预览尺寸由 vh/宽度公式决定，静止示例字号需同步重算（在途弹幕由 dsTick 重建） */
var dsResizeQueued = false;
window.addEventListener('resize', () => {
  if (!dsTimer || dsResizeQueued) return;
  dsResizeQueued = true;
  requestAnimationFrame(() => { dsResizeQueued = false; applyDsStatic(); });
});

/* ---------------- 数据分析 ---------------- */
const ana = {
  files: null,
  kind: 'records',
  sel: { records: new Set(), weak: new Set() },
  report: null,
  caredSet: new Set(),
};

function shortName(name) {
  return name.replace(/^live_/, '').replace(/\.jsonl$/, '');
}

async function initAnalysisView() {
  $('#anaTabs').dataset.ready = '1';
  await loadAnaFiles();
}

async function loadAnaFiles() {
  try {
    ana.files = await api('/api/files');
  } catch (err) { toast(err.message, 'err'); return; }
  renderFileTree();
}

function renderFileTree() {
  const items = (ana.files && ana.files[ana.kind]) || [];
  const groups = {};
  items.forEach((f) => (groups[f.vup] = groups[f.vup] || []).push(f));
  const sel = ana.sel[ana.kind];
  const tree = $('#anaFileTree');
  tree.innerHTML = Object.entries(groups).map(([vup, fs]) => `
    <div class="ft-group">
      <div class="ft-group-title"><span class="ft-caret">▼</span>
        ${fmt.esc(vup)} <span class="muted">${fs.length}</span></div>
      <div class="ft-items">
        ${fs.map((f) => `
          <label class="ft-item">
            <input type="checkbox" data-path="${fmt.esc(f.path)}" ${sel.has(f.path) ? 'checked' : ''}>
            <span class="mono">${shortName(f.name)}</span>
            ${f.deduped ? '<span class="ft-tag dedup">去重</span>' : ''}
          </label>`).join('')}
      </div>
    </div>`).join('')
    || '<div class="empty soft" style="padding:24px"><p>暂无文件</p></div>';
  updateSelCount();
}

function updateSelCount() {
  $('#anaSelCount').textContent = `已选 ${ana.sel[ana.kind].size}`;
}

$('#anaFileTree').addEventListener('change', (e) => {
  const cb = e.target.closest('input[data-path]');
  if (!cb) return;
  const set = ana.sel[ana.kind];
  cb.checked ? set.add(cb.dataset.path) : set.delete(cb.dataset.path);
  updateSelCount();
});
$('#anaFileTree').addEventListener('click', (e) => {
  const t = e.target.closest('.ft-group-title');
  if (t) t.parentElement.classList.toggle('collapsed');
});
$$('#anaTabs .tab').forEach((t) => t.addEventListener('click', () => {
  $$('#anaTabs .tab').forEach((x) => x.classList.toggle('active', x === t));
  ana.kind = t.dataset.kind;
  renderFileTree();
}));
$('#anaSelectAll').addEventListener('click', () => {
  const set = ana.sel[ana.kind];
  ((ana.files && ana.files[ana.kind]) || []).forEach((f) => set.add(f.path));
  renderFileTree();
});
$('#anaClear').addEventListener('click', () => {
  ana.sel[ana.kind].clear();
  renderFileTree();
});

/* ---- 纯 SVG 柱状图 ---- */
function vBars(data, opts = {}) {
  if (!data.length) return '<div class="muted">暂无数据</div>';
  const W = 760, H = 178, padL = 38, padR = 6, padT = 18, padB = 26;
  const max = Math.max(1, ...data.map((d) => d.value));
  const innerW = W - padL - padR;
  const n = data.length;
  const slot = innerW / n;
  const bw = Math.max(2, slot * 0.7);
  const step = Math.ceil(n / 12);
  const grid = [0.25, 0.5, 0.75, 1].map((p) => {
    const y = padT + (1 - p) * (H - padT - padB);
    return `<line class="grid" x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"/>
            <text x="${padL - 6}" y="${y + 3}" text-anchor="end">${Math.round(max * p)}</text>`;
  }).join('');
  const bars = data.map((d, i) => {
    const h = Math.max(0, d.value / max) * (H - padT - padB);
    const x = padL + i * slot + (slot - bw) / 2;
    const y = H - padB - h;
    const cls = 'bar' + (d.hot ? ' hot' : opts.primary ? ' pri' : '');
    const showLabel = n <= 12 || i % step === 0 || i === n - 1;
    return `<g>
      <title>${fmt.esc(d.title || d.label)}：${fmt.n(d.value)} 条</title>
      <rect class="${cls}" x="${x.toFixed(1)}" y="${y.toFixed(1)}"
            width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"/>
      ${n <= 25 && d.value > 0
        ? `<text class="vlabel" x="${(x + bw / 2).toFixed(1)}" y="${(y - 4).toFixed(1)}"
                 text-anchor="middle">${d.value}</text>` : ''}
      ${showLabel ? `<text x="${(x + bw / 2).toFixed(1)}" y="${H - padB + 14}"
                 text-anchor="middle">${fmt.esc(d.label)}</text>` : ''}
    </g>`;
  }).join('');
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
            ${grid}${bars}</svg>`;
}

function hRows(items, opts = {}) {
  if (!items.length) return '<div class="muted">暂无数据</div>';
  const max = Math.max(1, ...items.map((i) => i.value));
  return items.map((i) => `
    <div class="rank-row">
      <span class="rank-name" title="${fmt.esc(i.label)}">${fmt.esc(i.label)}</span>
      <span class="rank-track"><span class="rank-fill ${opts.gold ? 'gold' : ''}"
        style="width:${(i.value / max * 100).toFixed(1)}%"></span></span>
      <span class="rank-val">${fmt.n(i.value)}${i.suffix || ''}</span>
    </div>`).join('');
}

function card(v, l, cls = '') {
  return `<div class="ana-card ${cls}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
}

async function btnAnalyzeClick() {
  const paths = [...ana.sel[ana.kind]];
  if (!paths.length) return toast('请先勾选至少一个文件', 'warn');
  const rep = $('#analysisReport');
  rep.innerHTML = '<div class="empty soft"><p>分析中…</p></div>';
  try {
    ana.report = await api('/api/analysis', { method: 'POST', body: { paths, kind: ana.kind } });
    ana.caredSet = await loadCared();
    renderReport(ana.report);
  } catch (err) {
    rep.innerHTML = `<div class="empty soft"><p>分析失败：${fmt.esc(err.message)}</p></div>`;
  }
}
$('#btnAnalyze').addEventListener('click', btnAnalyzeClick);

function renderReport(d) {
  const t = d.totals;
  const spanTxt = t.span_sec >= 3600
    ? `${(t.span_sec / 3600).toFixed(1)} 小时` : `${Math.round(t.span_sec / 60)} 分钟`;
  const timeRange = t.start
    ? `${fmt.clock(t.start).slice(5)} ~ ${fmt.clock(t.end).slice(5)}` : '—';

  const timeline = d.timeline.bars.map((b, i) => ({
    label: b.label, value: b.value, hot: i === d.timeline.peak_index,
    title: b.label,
  }));
  const hourly = d.hourly.map((v, h) => ({ label: `${h}时`, value: v }));
  const lens = d.length_buckets.map((b) => ({ label: b.label, value: b.value }));
  const perFile = d.per_file_bars.map((b) => ({ label: b.label, value: b.value }));
  const medals = d.top_medals.map((m) => ({ label: m.name, value: m.count }));
  const modes = d.modes.map((m) => ({ label: m.label, value: m.value }));

  $('#analysisReport').innerHTML = `
    <div class="section-head" style="display:flex;align-items:center;margin-bottom:14px">
      <h2 style="font-size:15px">分析结果 · ${d.file_count} 个文件</h2>
      <span class="muted" style="margin-left:10px;font-size:12px">${fmt.esc(timeRange)}</span>
      <button class="btn btn-sm" id="btnExportReport" style="margin-left:auto">导出报告 JSON</button>
    </div>
    <div class="ana-cards">
      ${card(fmt.n(t.danmaku), '弹幕总数', 'accent')}
      ${card(fmt.n(t.users), '独立用户')}
      ${card(fmt.n(t.super_chat), 'SC 条数')}
      ${card('¥' + fmt.n(t.sc_amount), 'SC 总金额', 'gold')}
      ${card('¥' + fmt.n(t.revenue ?? t.sc_amount), '总营收(SC+金礼物)', 'gold')}
      ${card(fmt.n(t.gift), '礼物事件')}
      ${card(fmt.n(t.peak_per_min), '峰值 条/分')}
      ${card(t.avg_per_min, '平均 条/分')}
      ${card(spanTxt, '时间跨度')}
    </div>
    <div class="chart-grid">
      <div class="chart-card span-2">
        <h3>弹幕时间轴 <span class="muted">红色 = 弹幕峰值时段（高能时刻）</span></h3>
        ${vBars(timeline)}
      </div>
      <div class="chart-card">
        <h3>24 小时分布</h3>${vBars(hourly, { primary: true })}
      </div>
      <div class="chart-card">
        <h3>弹幕长度分布</h3>${vBars(lens)}
      </div>
      <div class="chart-card span-2">
        <h3>各文件弹幕量对比</h3>${hRows(perFile)}
      </div>
      <div class="chart-card span-2">
        <h3>高频弹幕 Top 20 <span class="muted">刷屏口号 / 梗识别</span></h3>
        ${d.top_texts.length ? d.top_texts.map((x) => `
          <div class="top-text-row">
            <span class="tt-text" title="${fmt.esc(x.text)}">${fmt.esc(x.text)}</span>
            <button class="tt-copy" data-copy="${fmt.esc(x.text)}">复制</button>
            <span class="tt-count">×${x.count}</span>
          </div>`).join('') : '<div class="muted">暂无数据</div>'}
      </div>
      <div class="chart-card">
        <h3>活跃用户 Top 20 <span class="muted">可加关心</span></h3>
        ${d.top_users.length ? d.top_users.map((u) => {
          const maxU = d.top_users[0].count;
          const anonymous = u.uid.startsWith('名:');
          const cared = ana.caredSet.has(u.uid);
          return `<div class="rank-row">
            <span class="rank-name" title="${fmt.esc(u.uid)}">${fmt.esc(u.uname || '匿名')}</span>
            <span class="rank-track"><span class="rank-fill"
              style="width:${(u.count / maxU * 100).toFixed(1)}%"></span></span>
            <span class="rank-val">${fmt.n(u.count)}</span>
            ${anonymous ? '' : `<button class="rank-btn ${cared ? 'done' : ''}"
              data-addcared="${fmt.esc(u.uid)}" ${cared ? 'disabled' : ''}>
              ${cared ? '已关心' : '⭐ 关心'}</button>`}
          </div>`;
        }).join('') : '<div class="muted">暂无数据</div>'}
      </div>
      <div class="chart-card">
        <h3>SC 贡献榜 Top 10</h3>
        ${d.sc_users.length ? d.sc_users.map((u) => `
          <div class="rank-row">
            <span class="rank-name">${fmt.esc(u.uname || '匿名')}</span>
            <span class="rank-track"><span class="rank-fill gold"
              style="width:${(u.amount / Math.max(1, d.sc_users[0].amount) * 100).toFixed(1)}%"></span></span>
            <span class="rank-val">¥${fmt.n(u.amount)}</span>
          </div>`).join('') : '<div class="muted">选中文件中没有 SC</div>'}
      </div>
      <div class="chart-card">
        <h3>粉丝牌分布 Top 10</h3>${hRows(medals)}
      </div>
      <div class="chart-card">
        <h3>弹幕模式分布</h3>${hRows(modes)}
      </div>
      <div class="chart-card span-2">
        <h3>高能时刻 Top 5 <span class="muted">点击展开：该分钟高频弹幕 Top 20 / 高频 SC（按内容去重）</span></h3>
        ${d.peak_minutes.length ? d.peak_minutes.map((p, i) => `
          <div class="peak-row">
            <div class="peak-head" data-peak="${i}">
              <span>▶</span>
              <span class="peak-count">${p.count}</span> 条/分
              <span>${fmt.esc(p.label)}</span>
              <span class="muted" style="margin-left:auto">${p.users} 位用户</span>
              ${p.sc_count ? `<span class="peak-sc-chip">🎫 SC ×${p.sc_count}${p.sc_amount ? ' ¥' + fmt.n(p.sc_amount) : ''}</span>` : ''}
            </div>
            <div class="peak-samples" hidden>
              <div class="peak-subhead">高频弹幕 Top ${p.top_texts.length} <span class="muted">按内容去重 · 只看大家在刷什么</span></div>
              ${p.top_texts.length ? p.top_texts.map((x) => `
                <div class="top-text-row">
                  <span class="tt-text" title="${fmt.esc(x.text)}">${fmt.esc(x.text)}</span>
                  <button class="tt-copy" data-copy="${fmt.esc(x.text)}">复制</button>
                  <span class="tt-count">×${x.count}</span>
                </div>`).join('') : '<div class="muted">该分钟没有文字弹幕</div>'}
              ${p.top_sc.length ? `
              <div class="peak-subhead">高频 SC <span class="muted">付费留言 · 合计 ¥${fmt.n(p.sc_amount)}</span></div>
              ${p.top_sc.map((x) => `
                <div class="top-text-row">
                  <span class="tt-price">¥${fmt.n(x.price)}</span>
                  <span class="tt-text" title="${fmt.esc(x.text)}">${fmt.esc(x.text)}</span>
                  <button class="tt-copy" data-copy="${fmt.esc(x.text)}">复制</button>
                  <span class="tt-count">×${x.count}</span>
                </div>`).join('')}` : ''}
            </div>
          </div>`).join('') : '<div class="muted">暂无数据</div>'}
      </div>
    </div>`;
}

$('#analysisReport').addEventListener('click', async (e) => {
  const peak = e.target.closest('[data-peak]');
  if (peak) {
    const box = peak.nextElementSibling;
    box.hidden = !box.hidden;
    peak.querySelector('span').textContent = box.hidden ? '▶' : '▼';
    return;
  }
  const copyBtn = e.target.closest('[data-copy]');
  if (copyBtn) {
    try { await navigator.clipboard.writeText(copyBtn.dataset.copy); toast('已复制文案', 'ok', 1500); }
    catch (_) { toast('复制失败，请手动选择', 'warn'); }
    return;
  }
  const caredBtn = e.target.closest('[data-addcared]');
  if (caredBtn) {
    const uid = caredBtn.dataset.addcared;
    const row = caredBtn.closest('.rank-row');
    const uname = row.querySelector('.rank-name').textContent.trim();
    try {
      await api('/api/cared-users', { method: 'POST', body: { key: uid, note: `分析页添加：${uname}` } });
      caredBtn.textContent = '已关心';
      caredBtn.classList.add('done');
      caredBtn.disabled = true;
      ana.caredSet.add(uid);
      toast(`已将 ${uname} 加入关心名单`, 'ok');
    } catch (err) { toast(err.message, 'err'); }
    return;
  }
  if (e.target.closest('#btnExportReport') && ana.report) {
    const blob = new Blob([JSON.stringify(ana.report, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `danmaku_report_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }
});

/* ---- 批量去重 ---- */
$('#btnBatchDedup').addEventListener('click', async () => {
  if (ana.kind !== 'records') return toast('房管处置存档文件不需要去重', 'warn');
  const all = (ana.files && ana.files.records) || [];
  const paths = [...ana.sel.records].filter((p) =>
    !all.find((f) => f.path === p && f.deduped));
  if (!paths.length) return toast('请勾选待去重的原始文件（.dedup.jsonl 自动跳过）', 'warn');
  const w = Math.max(0, Number($('#anaDedupWindow').value));
  if (!confirm(`将对 ${paths.length} 个文件执行去重（窗口 ${w} 秒，0=整文件）。\n输出 .dedup.jsonl，原文件不会被修改，是否继续？`)) return;
  const btn = $('#btnBatchDedup');
  btn.disabled = true; btn.textContent = '去重中…';
  try {
    const r = await api('/api/dedup/run-batch',
      { method: 'POST', body: { paths, kind: 'records', window_sec: w } });
    toast(`批量去重完成：${r.results.length} 个文件，共去除 ${fmt.n(r.total_removed)} 条重复`, 'ok', 5000);
    await loadAnaFiles();
    if ($('#view-files').classList.contains('active')) loadFiles();
  } catch (err) { toast(err.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '批量去重'; }
});

/* ---------------- 后处理去重弹窗 ---------------- */
let dedupCur = null;

async function openDedup(kind, path, displayName) {
  dedupCur = { kind, path };
  $('#dedupTitle').textContent = '后处理去重 · ' + displayName;
  $('#dedupBody').className = 'modal-body nonmono';
  $('#dedupBody').innerHTML = '<div class="muted">正在分析重复情况…</div>';
  $('#dedupConfirm').disabled = false;
  $('#dedupMask').hidden = false;
  await dedupPreview();
}

async function dedupPreview() {
  const w = Math.max(0, Number($('#dedupWindow').value) || 0);
  $('#dedupBody').innerHTML = '<div class="muted">正在分析…</div>';
  try {
    const r = await api('/api/dedup/preview',
      { method: 'POST', body: { path: dedupCur.path, kind: dedupCur.kind, window_sec: w } });
    $('#dedupBody').innerHTML = `
      <div class="dedup-stats">
        <div class="dedup-stat"><b>${fmt.n(r.danmaku)}</b>弹幕总数</div>
        <div class="dedup-stat ok"><b>${fmt.n(r.kept)}</b>保留</div>
        <div class="dedup-stat warn"><b>${fmt.n(r.removed)}</b>去除重复</div>
        <div class="dedup-stat"><b>${(r.removed_rate * 100).toFixed(2)}%</b>重复率</div>
        <div class="dedup-stat"><b>${fmt.n(r.cared_count)}</b>关心用户豁免</div>
        <div class="dedup-stat"><b>${fmt.n(r.non_danmaku)}</b>SC/礼物/头保留</div>
      </div>
      <div class="hint" style="margin:0 0 10px">同用户相同文案在 ${r.window_sec === 0 ? '整个文件' : r.window_sec + ' 秒窗口'} 内重复时仅保留首条。</div>
      <div style="font-weight:650;margin-bottom:6px">刷屏样例 Top ${r.top_spam.length}</div>
      ${r.top_spam.length ? r.top_spam.map((s) => `
        <div class="spam-row"><span class="spam-count">×${s.count}</span>
          <span style="color:var(--text-3)">${fmt.esc(s.uname || '匿名')}：</span>
          <span>${fmt.esc(s.text)}</span></div>`).join('')
        : '<div class="muted">没有检测到重复弹幕 🎉</div>'}`;
  } catch (err) {
    $('#dedupBody').innerHTML = '<div style="color:var(--danger)">预览失败：' + fmt.esc(err.message) + '</div>';
  }
}

$('#dedupClose').addEventListener('click', () => { $('#dedupMask').hidden = true; });
$('#dedupMask').addEventListener('click', (e) => {
  if (e.target.id === 'dedupMask') $('#dedupMask').hidden = true;
});
$('#dedupRePreview').addEventListener('click', dedupPreview);
$('#dedupConfirm').addEventListener('click', async () => {
  const w = Math.max(0, Number($('#dedupWindow').value) || 0);
  const btn = $('#dedupConfirm');
  btn.disabled = true; btn.textContent = '去重中…';
  try {
    const r = await api('/api/dedup/run',
      { method: 'POST', body: { path: dedupCur.path, kind: dedupCur.kind, window_sec: w } });
    $('#dedupBody').innerHTML = `
      <div style="color:var(--ok);font-weight:650;margin-bottom:10px">✅ 去重完成，原文件未修改</div>
      <div class="dedup-stats">
        <div class="dedup-stat"><b>${fmt.n(r.danmaku)}</b>弹幕</div>
        <div class="dedup-stat ok"><b>${fmt.n(r.kept)}</b>保留</div>
        <div class="dedup-stat warn"><b>${fmt.n(r.removed)}</b>去除</div>
        <div class="dedup-stat"><b>${fmt.n(r.cared_count)}</b>关心豁免</div>
      </div>
      <div class="hint" style="margin:0">已保存为：<code>${fmt.esc(r.output.split(/[\\\\/]/).pop())}</code>（与原文件同目录）</div>`;
    btn.textContent = '已完成';
    toast(`去重完成：去除 ${r.removed} 条`, 'ok');
    if (ana.files) loadAnaFiles();
    if ($('#view-files').classList.contains('active')) loadFiles();
  } catch (err) {
    btn.disabled = false; btn.textContent = '执行去重（另存 .dedup.jsonl）';
    toast(err.message, 'err');
  }
});

$('#filesTable').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-dedup]');
  if (!btn) return;
  openDedup(state.filesKind, btn.dataset.dedup, btn.dataset.dedup.split(/[\\/]/).pop());
});

/* ---------------- 单场直播战报 ---------------- */
let reportCur = null;
let reportMeta = null;
let reportBlobUrl = null;

function resetReportImage() {
  if (reportBlobUrl) { URL.revokeObjectURL(reportBlobUrl); reportBlobUrl = null; }
  $('#reportImgWrap').hidden = true;
  $('#reportImg').removeAttribute('src');
  $('#reportDownload').hidden = true;
  $('#reportOpenTab').hidden = true;
  const gen = $('#reportGen');
  gen.hidden = false;
  gen.disabled = false;
  gen.textContent = '生成 PNG 一页战报';
}

function safeFileName(s) {
  return String(s ?? '').replace(/[\\/:*?"<>|]/g, '_').trim() || '战报';
}

async function openReport(kind, path) {
  reportCur = { kind, path };
  reportMeta = null;
  resetReportImage();
  $('#reportMask').hidden = false;
  $('#reportBody').innerHTML = '<div class="muted">正在汇总本场数据…</div>';
  try {
    const s = await api('/api/report/summary', { method: 'POST', body: { path, kind } });
    reportMeta = s;
    const dur = s.span_sec >= 3600
      ? `${(s.span_sec / 3600).toFixed(1)} 小时` : `${Math.round(s.span_sec / 60)} 分钟`;
    const row = (l, v, cls = '') => `<div class="dedup-stat ${cls}"><b>${v}</b>${l}</div>`;
    $('#reportBody').innerHTML = `
      <div style="font-weight:650;font-size:15px;margin-bottom:4px">
        ${fmt.esc(s.title || '（无标题）')}</div>
      <div class="muted" style="margin-bottom:14px">
        ${fmt.esc(s.anchor || s.vup)} · ${fmt.esc(s.date || '日期未知')} · 时长 ${dur}
        ${(s.watched_show || s.watched_recorded) ? '· 累计看过 ' + fmt.esc(s.watched_show || s.watched_recorded) : ''}</div>
      <div class="dedup-stats">
        ${row('弹幕总数', fmt.n(s.danmaku))}
        ${row('独立观众', fmt.n(s.users))}
        ${row('峰值人气', s.has_pop ? fmt.n(s.pop_peak) : '无数据', s.has_pop ? 'warn' : '')}
        ${row('平均人气', s.has_pop ? fmt.n(s.pop_avg) : '无数据')}
        ${row('总营收', '¥' + fmt.n(s.revenue), 'ok')}
        ${row('SC', s.sc_count + ' / ¥' + fmt.n(s.sc_amount))}
        ${row('金礼物', '¥' + fmt.n(s.gift_amount))}
        ${row('弹幕峰值', fmt.n(s.dm_peak.count) + ' 条/分')}
      </div>
      ${s.has_pop ? `<div class="hint" style="margin:0">人气曲线共 ${fmt.n(s.pop_points)} 个采样点（25 秒粒度），将绘制在战报图中。</div>`
        : '<div class="hint" style="margin:0;color:var(--warn)">峰值/平均人气与曲线：该文件录制于人气采集上线前，历史数据无法回填（其余内容正常）。</div>'}
      ${(s.watched_show || s.watched_recorded) ? ''
        : '<div class="hint" style="margin:0">「累计看过」由新版录制器在直播中随人气采样保存；本场录制时该能力尚未生效，无法补取。</div>'}`;
  } catch (err) {
    $('#reportBody').innerHTML = `<div style="color:var(--danger)">汇总失败：${fmt.esc(err.message)}</div>`;
  }
}

$('#reportClose').addEventListener('click', () => { $('#reportMask').hidden = true; });
$('#reportMask').addEventListener('click', (e) => {
  if (e.target.id === 'reportMask') $('#reportMask').hidden = true;
});
$('#reportGen').addEventListener('click', async () => {
  if (!reportCur) return;
  const btn = $('#reportGen');
  btn.disabled = true; btn.textContent = '绘制中…';
  try {
    const res = await fetch('/api/report/image', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reportCur),
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const url = URL.createObjectURL(await res.blob());
    if (reportBlobUrl) URL.revokeObjectURL(reportBlobUrl);
    reportBlobUrl = url;
    $('#reportImg').src = url;
    $('#reportImgWrap').hidden = false;
    const m = reportMeta || {};
    $('#reportDownload').href = url;
    $('#reportDownload').download =
      `战报_${safeFileName(m.anchor || m.vup)}_${safeFileName(m.date)}.png`;
    $('#reportOpenTab').href = url;
    $('#reportDownload').hidden = false;
    $('#reportOpenTab').hidden = false;
    btn.disabled = false;
    btn.textContent = '重新生成';
    toast('战报已生成，可直接预览或下载 PNG', 'ok', 2400);
    requestAnimationFrame(() =>
      $('#reportImgWrap').scrollIntoView({ behavior: 'smooth', block: 'nearest' }));
  } catch (err) {
    btn.disabled = false; btn.textContent = '生成 PNG 一页战报';
    toast(err.message, 'err');
  }
});

$('#filesTable').addEventListener('click', (e) => {
  const btn = e.target.closest('[data-report]');
  if (!btn) return;
  openReport(state.filesKind, btn.dataset.report);
});

/* ---------------- 启动 ---------------- */
(async function init() {
  try {
    const s = await api('/api/state');
    applyState(s);
    // 补发历史日志
    const r = await api('/api/logs?limit=300');
    state.logs = r.logs || [];
    state.logTs = new Set(state.logs.map(
      (l) => l.ts + '|' + (l.room_id || '') + '|' + l.msg));
    $('#logDirHint').textContent = r.dir || '—';
  } catch (err) {
    toast('初始状态加载失败：' + err.message, 'err', 6000);
  }
  connectWS();
})();
