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
  rooms: [], totals: {}, settings: {},
  recording: false, runningCount: 0, liveCount: 0, connectedCount: 0,
  stream: [],          // {kind, room_id, vup, text, uname, medal, ts, ...}
  streamPending: [],
  logs: [],
  filesKind: 'records',
  files: [],
  videos: { sessions: [], orphans: [], dir: '', ffmpeg: null },
  streamUnread: 0,
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
  if (name === 'settings') { fillSettings(); loadCared(); loadTheme(); }
}

$$('.nav-item').forEach((a) => a.addEventListener('click', () => showView(a.dataset.view)));
document.addEventListener('click', (e) => {
  const go = e.target.closest('[data-goto]');
  if (go) showView(go.dataset.goto);
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
    state.logs.push(data);
    if (state.logs.length > 1000) state.logs.shift();
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
  renderStats(s);
  renderRoomSelect();
  if ($('#view-dashboard').classList.contains('active')) renderRoomGrid();
  if ($('#view-rooms').classList.contains('active')) renderRoomTable();
}

function upsertRoom(rt) {
  if (!rt || !rt.room_id) return;
  const idx = state.rooms.findIndex((r) => r.room_id === rt.room_id);
  if (idx >= 0) state.rooms[idx] = rt; else state.rooms.push(rt);
  if ($('#view-dashboard').classList.contains('active')) renderRoomGrid();
  if ($('#view-rooms').classList.contains('active')) renderRoomTable();
}

/* ---------------- 仪表盘 ---------------- */
const PHASE = {
  recording: ['recording', '直播中'],
  connecting: ['connecting', '连接中'],
  connected_idle: ['connecting', '已连接'],
  waiting: ['waiting', '等待开播'],
  stopped: ['stopped', '已停止'],
};
const QN_LABELS = { 10000: '原画', 400: '蓝光', 250: '超清', 150: '高清', 80: '流畅' };

function renderStats(s) {
  $('#statRunning').textContent = fmt.n(s.running_count);
  $('#statLive').textContent = fmt.n(s.live_count);
  $('#statConnected').textContent = fmt.n(s.connected_count);
  $('#statDanmaku').textContent = fmt.n(state.totals.danmaku);
  $('#statSC').textContent = fmt.n(state.totals.super_chat);
  $('#statMod').textContent = fmt.n(state.totals.moderation);
  $('#statRate').textContent = fmt.n(s.rate_per_min);
  const pill = $('#globalPill');
  if (s.recording) {
    pill.classList.add('live');
    $('#globalPillText').textContent =
      `录制中 · ${s.live_count} 播 / ${s.running_count} 房`;
  } else {
    pill.classList.remove('live');
    $('#globalPillText').textContent = '未在录制';
  }
}

function roomCard(r) {
  const [cls, label] = PHASE[r.phase] || PHASE.stopped;
  const c = r.counters || {};
  const meta = [];
  if (r.running) {
    meta.push(`运行 <b>${fmt.dur(r.uptime_sec)}</b>`);
    if (r.host_count) meta.push(`节点 <b>${r.host_index}/${r.host_count}</b>`);
    if (r.retries) meta.push(`重连 <b>${r.retries}</b>`);
    meta.push(`<b>${r.rate_per_min}</b> 条/分`);
    if (r.popularity_now) {
      meta.push(`人气 <b>${fmt.n(r.popularity_now)}</b>`
        + (r.popularity_peak ? `（峰 ${fmt.n(r.popularity_peak)}）` : ''));
    }
  }
  if (r.current_file) meta.push(`文件 <b>${fmt.esc(r.current_file)}</b>`);
  if (r.video_active) {
    meta.push(`🎥 <b>${QN_LABELS[r.video_qn] || r.video_qn}</b> ${fmt.size(r.video_bytes)}`
      + ` / ${fmt.dur(r.video_sec)}` + (r.video_segments > 1 ? ` / ${r.video_segments}段` : ''));
  } else if (r.record_video) {
    meta.push('🎥 视频待录');
  }
  const running = r.phase === 'recording';
  return `
  <div class="room-card ${running ? 'recording' : ''}">
    <div class="rc-head">
      <span class="badge ${cls}">${label}</span>
      <span class="rc-name" title="${fmt.esc(r.anchor_name || r.vup)}">${fmt.esc(r.anchor_name || r.vup)}</span>
      <span class="rc-room">${r.room_id}</span>
      <span class="rc-actions">
        <a class="btn btn-sm" href="https://live.bilibili.com/${r.room_id}"
           target="_blank" rel="noopener" title="在 B 站新标签页打开该直播间">进直播间</a>
        ${r.running
          ? `<button class="btn btn-sm btn-danger" data-stop="${r.room_id}">停止</button>`
          : `<button class="btn btn-sm btn-primary" data-start="${r.room_id}">开始</button>`}
      </span>
    </div>
    <div class="rc-title" title="${fmt.esc(r.title)}">${fmt.esc(r.title || '—')}</div>
    <div class="rc-meta">${meta.join(' · ') || '<span>未运行</span>'}</div>
    ${r.last_error && !r.connected ? `<div class="rc-meta" style="color:var(--danger-hi)">⚠ ${fmt.esc(r.last_error)}</div>` : ''}
    <div class="rc-stats">
      <div class="rc-stat"><b>${fmt.n(c.danmaku)}</b>弹幕</div>
      <div class="rc-stat sc"><b>${fmt.n(c.super_chat)}</b>SC</div>
      <div class="rc-stat"><b>${fmt.n(c.gift)}</b>礼物</div>
      <div class="rc-stat"><b>${fmt.n(c.emoticon_skipped)}</b>纯表情过滤</div>
    </div>
  </div>`;
}

function renderRoomGrid() {
  const grid = $('#roomGrid');
  const empty = $('#dashboardEmpty');
  if (!state.rooms.length) { grid.innerHTML = ''; empty.hidden = false; return; }
  empty.hidden = true;
  grid.innerHTML = state.rooms.map(roomCard).join('');
}

$('#roomGrid').addEventListener('click', async (e) => {
  const start = e.target.closest('[data-start]');
  const stop = e.target.closest('[data-stop]');
  try {
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
    return `<tr data-room="${r.room_id}">
      <td><span class="badge ${cls}">${label}</span></td>
      <td class="mono"><a href="https://live.bilibili.com/${r.room_id}"
          target="_blank" rel="noopener">${r.room_id}</a></td>
      <td>${fmt.esc(r.vup)}${r.anchor_name && r.anchor_name !== r.vup
          ? `<div class="hint" style="margin:0">${fmt.esc(r.anchor_name)}</div>` : ''}</td>
      <td><input type="checkbox" ${r.enabled ? 'checked' : ''} data-enabled="${r.room_id}"></td>
      <td><input type="checkbox" ${r.record_video ? 'checked' : ''}
          data-video="${r.room_id}"
          title="为该房间单独开启视频直录（全局开关在设置页）"></td>
      <td>${fmt.n((r.counters || {}).danmaku)}</td>
      <td>
        ${r.running
          ? `<button class="btn btn-sm btn-danger" data-stop2="${r.room_id}">停止</button>`
          : `<button class="btn btn-sm btn-primary" data-start2="${r.room_id}">开始</button>`}
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
  if (!room_id || room_id <= 0) return toast('请填写正确的直播间号', 'warn');
  try {
    await api('/api/targets', {
      method: 'POST',
      body: { room_id, vup, enabled, record_video },
    });
    $('#addRoomId').value = ''; $('#addVup').value = '';
    $('#addRecordVideo').checked = false;
    toast(`房间 ${room_id} 已添加${enabled ? '并开始录制' : ''}`
      + (record_video ? '（含视频直录）' : ''), 'ok');
  } catch (err) { toast(err.message, 'err'); }
});

$('#roomTable').addEventListener('click', async (e) => {
  try {
    const s = e.target.closest('[data-start2]');
    const x = e.target.closest('[data-stop2]');
    const d = e.target.closest('[data-del]');
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
  try {
    if (cb) {
      await api(`/api/targets/${cb.dataset.enabled}`,
        { method: 'PUT', body: { enabled: cb.checked } });
      toast(cb.checked ? '已启用' : '已停用（运行中的任务需手动停止）', 'ok');
    }
    if (vb) {
      await api(`/api/targets/${vb.dataset.video}`,
        { method: 'PUT', body: { record_video: vb.checked } });
      if (vb.checked) {
        toast('该房间已开启视频直录（全局开关开启时，下一场开播生效）', 'ok', 3600);
      } else {
        toast('该房间视频直录已关闭（正在录制的场次结束后停止）', 'ok', 3000);
      }
    }
  } catch (err) {
    toast(err.message, 'err');
    if (cb) cb.checked = !cb.checked;
    if (vb) vb.checked = !vb.checked;
  }
});

/* ---------------- B 站关注列表导入 ---------------- */
const followState = { items: [], loaded: false, loading: false, selected: new Set() };

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
  $('#followSelInfo').textContent = sel ? `已选 ${sel} 间，加入后错峰启动录制` : '';
  $('#followConfirm').disabled = sel === 0;
  $('#followSelectLive').disabled =
    !selectable.some((i) => i.live_status === 1);
}

function renderFollow() {
  const box = $('#followContent');
  if (followState.loading) {
    box.innerHTML = '<div class="muted follow-hint">正在读取关注列表（关注较多时需翻几页）…</div>';
    $('#followStats').textContent = '';
    syncFollowFoot([]);
    return;
  }
  if (!followState.loaded) return;  // 错误提示已直接渲染在列表区
  const liveCount = followState.items.filter((i) => i.live_status === 1).length;
  $('#followStats').textContent =
    `共 ${followState.items.length} 位 · 直播中 ${liveCount}`;
  if (!followState.items.length) {
    box.innerHTML = '<div class="muted follow-hint">这个账号的关注列表为空</div>';
    syncFollowFoot([]);
    return;
  }
  const list = followVisible();
  if (!list.length) {
    box.innerHTML = '<div class="muted follow-hint">没有符合筛选条件的主播</div>';
    syncFollowFoot([]);
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
}

async function loadFollow() {
  followState.loading = true;
  followState.loaded = false;
  renderFollow();
  try {
    const r = await api('/api/followings');
    followState.items = r.items || [];
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

$('#btnImportFollowing').addEventListener('click', () => {
  $('#followMask').hidden = false;
  if (!followState.loaded && !followState.loading) loadFollow();
  else renderFollow();
});
$('#followClose').addEventListener('click', () => { $('#followMask').hidden = true; });
$('#followReload').addEventListener('click', () => { if (!followState.loading) loadFollow(); });
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
      toast(`已加入 ${r.added.length} 个房间，正在错峰连接录制`
        + (r.skipped.length ? `，跳过已存在 ${r.skipped.length} 个` : ''), 'ok', 4200);
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
    toast(`已启动 ${r.started.length} 个房间（错峰连接中）`, 'ok');
  } catch (err) { toast(err.message, 'err'); }
});
$('#btnStopAll').addEventListener('click', async () => {
  try { await api('/api/stop-all', { method: 'POST' }); toast('已全部停止', 'ok'); }
  catch (err) { toast(err.message, 'err'); }
});

/* ---------------- 实时弹幕 ---------------- */
function renderRoomSelect() {
  const sel = $('#streamRoom');
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部房间</option>' +
    state.rooms.map((r) =>
      `<option value="${r.room_id}">${fmt.esc(r.vup)}（${r.room_id}）</option>`).join('');
  sel.value = cur;
}

function streamFilters() {
  const kinds = new Set($$('.toolbar .chip-check input:checked').map((i) => i.value));
  const room = $('#streamRoom').value;
  return { kinds, room: room ? Number(room) : 0 };
}

function streamRow(d) {
  const f = streamFilters();
  if (!f.kinds.has(d.kind)) return '';
  if (f.room && d.room_id !== f.room) return '';
  const medal = d.medal && d.medal.name
    ? `<span class="dm-medal">${fmt.esc(d.medal.name)} Lv${d.medal.level}</span>` : '';
  if (d.kind === 'moderation') {
    return `<div class="dm-row moderation">
      <span class="dm-time">${fmt.hms(d.ts)}</span>
      <span class="dm-room">${fmt.esc(d.vup)}</span>
      <span class="dm-text">⚠ ${fmt.esc(d.event_name)}事件，回捞 ${d.suspect_count} 条疑似弹幕（已存弱标签）</span>
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
  if (e.target.id === 'streamPause' && !e.target.checked) rerenderStream();
  else if (e.target.matches('.chip-check input, .chip-check input')) rerenderStream();
});
$('#streamRoom').addEventListener('change', rerenderStream);
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
  $('#filesEmpty').hidden = items.length > 0;
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
          ? `<button class="btn btn-sm" data-dedup="${fmt.esc(f.path)}">去重</button>`
          : f.deduped ? '<span class="ft-tag dedup" style="margin:0">已去重</span>' : ''}
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
  $('#videoDirHint').textContent = data.dir ? `目录：${data.dir}` : '';
  const ff = data.ffmpeg;
  $('#videoFfmpegPill').innerHTML = ff
    ? `<span class="tag ok">ffmpeg ${fmt.esc(ff.version || '已就绪')}</span>`
    : '<span class="tag" title="秒切功能需要 ffmpeg">未检测到 ffmpeg（仍可复制跳转命令）</span>';
  const ss = data.sessions || [];
  $('#videosEmpty').hidden = ss.length > 0;
  $('#videosTable').innerHTML = ss.map(videoRow).join('');
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
  if (s.ongoing) badges.push('<span class="badge recording">录制中</span>');
  badges.push(`<span class="tag">${fmt.esc(s.qn_label || (QN_LABELS[s.qn] || s.qn))}</span>`);
  if (s.codec) badges.push(`<span class="tag">${fmt.esc(s.codec)}</span>`);
  if (s.segment_count > 1) badges.push(`<span class="tag">${s.segment_count} 段</span>`);
  if (s.ass) badges.push('<span class="tag ok">ASS 字幕</span>');
  if (s.ai) badges.push(
    `<span class="tag ai-badge" title="${fmt.esc(s.ai.summary || '已生成 AI 解读')}">🤖 AI ${fmt.esc(s.ai.mode || '')} · ${s.ai.picks || 0} 条</span>`);
  if (s.recovered) badges.push('<span class="tag" style="color:var(--warn)" title="进程异常退出，分段为启动列表时自动认回">异常认回</span>');
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
      <button class="btn btn-sm btn-primary" data-hl="${fmt.esc(s.base)}"
              data-vup="${fmt.esc(s.vup)}">高能时间轴</button>
      ${s.ass ? `<a class="btn btn-sm" href="${videoDl(s.vup, s.ass)}">ASS</a>` : ''}
      ${s.ongoing ? '' : `<button class="btn btn-sm btn-danger" data-vdel="${fmt.esc(s.base)}"
              data-vup="${fmt.esc(s.vup)}">删除</button>`}
    </td>
  </tr>`;
}

$('#btnVideoReload').addEventListener('click', loadVideos);

$('#videosTable').addEventListener('click', (e) => {
  const hl = e.target.closest('[data-hl]');
  const del = e.target.closest('[data-vdel]');
  if (hl) { openHighlights(hl.dataset.vup, hl.dataset.hl); return; }
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

/* 录制中的场次：面板可见时每 5s 刷新大小/时长 */
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
        <div class="hint">完整分段、ASS 字幕与已切片段请在视频列表行内下载；删除场次也在列表页操作。</div>
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

/* ---------------- 设置 ---------------- */
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
  $('#videoDirEff').textContent = (s.data_dir_effective || '—')
    + '\\' + (s.video_subdir || 'video');
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
  $('#saveResult').textContent = '';
}

$('#toggleSess').addEventListener('click', () => {
  const i = $('#setSess');
  const show = i.type === 'password';
  i.type = show ? 'text' : 'password';
  $('#toggleSess').textContent = show ? '隐藏' : '显示';
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

$('#btnSaveSettings').addEventListener('click', async () => {
  const sess = $('#setSess').value.trim();
  const llmKey = $('#setLlmKey').value.trim();
  const llmMode = (document.querySelector('input[name="llmMode"]:checked') || {}).value || 'A';
  const patch = {
    anon_salt: $('#setSalt').value.trim(),
    data_dir: $('#setDataDir').value.trim(),
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
    llm_auto_analyze: $('#swLlmAuto').checked,
    llm_base_url: $('#setLlmBase').value.trim(),
    llm_model: $('#setLlmModel').value.trim(),
    llm_mode: llmMode,
  };
  if (sess) patch.sessdata = sess;
  if (llmKey) patch.llm_api_key = llmKey;
  $('#saveResult').textContent = '保存中…';
  try {
    await api('/api/settings', { method: 'PUT', body: patch });
    $('#saveResult').textContent = '✅ 已保存（新任务即时生效；运行中的任务按原配置继续）';
    toast('设置已保存', 'ok');
  } catch (err) {
    $('#saveResult').textContent = '❌ ' + err.message;
    toast(err.message, 'err');
  }
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

function fillTheme() {
  const { theme, meta } = thData;
  $('#thColors').innerHTML = meta.colors.map((c) => `
    <div class="th-color">
      <input type="color" data-th-color="${c.key}" value="${(theme[c.key] || '#000000').toLowerCase()}">
      <input type="text" class="input th-hex" data-th-hex="${c.key}"
             value="${theme[c.key] || ''}" maxlength="7" spellcheck="false">
      <span class="th-color-name">${c.label}</span>
    </div>`).join('');
  $('#thWidth').innerHTML =
    meta.widths.map((w) => `<option value="${w}">${w} px</option>`).join('');
  $('#thCardCols').innerHTML =
    meta.card_cols.map((n) => `<option value="${n}">每行 ${n} 个</option>`).join('');
  // font_scale=1.0 经 JSON 往返会变成数字 1，需按数值匹配 option
  const fsSel = $('#thFontScale');
  const fsOpt = Array.from(fsSel.options)
    .find((o) => Number(o.value) === Number(theme.font_scale));
  fsSel.value = fsOpt ? fsOpt.value : '1.0';
  $('#thWidth').value = String(theme.width);
  $('#thCardCols').value = String(theme.card_cols);
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

$('#btnThPreview').addEventListener('click', async () => {
  if ($$('#thColors .th-hex.bad').length) {
    return toast('存在不合法的颜色值，应为 #RRGGBB 格式', 'err');
  }
  const btn = $('#btnThPreview');
  btn.disabled = true; btn.textContent = '绘制中…';
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
    $('#thPreviewBox').hidden = false;
  } catch (err) { toast(err.message, 'err'); }
  finally { btn.disabled = false; btn.textContent = '生成预览'; }
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
        <h3>高能时刻 Top 5 <span class="muted">点击展开该分钟弹幕样例（最多 50 条）</span></h3>
        ${d.peak_minutes.length ? d.peak_minutes.map((p, i) => `
          <div class="peak-row">
            <div class="peak-head" data-peak="${i}">
              <span>▶</span>
              <span class="peak-count">${p.count}</span> 条/分
              <span>${fmt.esc(p.label)}</span>
              <span class="muted" style="margin-left:auto">${p.users} 位用户</span>
            </div>
            <div class="peak-samples" hidden>${p.samples.map((s) => `
              <div class="ps-line"><span class="ps-time">${fmt.hms(s.ts)}</span>
                <b>${fmt.esc(s.uname || '匿名')}：</b>${fmt.esc(s.text)}</div>`).join('')}
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
  if (ana.kind !== 'records') return toast('弱标签文件不需要去重', 'warn');
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
  } catch (err) {
    toast('初始状态加载失败：' + err.message, 'err', 6000);
  }
  connectWS();
})();
