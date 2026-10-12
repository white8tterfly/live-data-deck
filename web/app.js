/* 弹幕墙 Web UI —— 原生 JS，无构建步骤 */
'use strict';

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
    return (b / 1048576).toFixed(2) + ' MB';
  },
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
                 files: '录制文件', logs: '运行日志', settings: '设置' };

function showView(name) {
  $$('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  $('#pageTitle').textContent = TITLES[name];
  if (name === 'stream') { state.streamUnread = 0; updateBadge(); }
  if (name === 'rooms') renderRoomTable();
  if (name === 'dashboard') renderRoomGrid();
  if (name === 'files') loadFiles();
  if (name === 'logs') renderLogs(true);
  if (name === 'settings') fillSettings();
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
  if (type === 'log') {
    state.logs.push(data);
    if (state.logs.length > 1000) state.logs.shift();
    if ($('#view-logs').classList.contains('active')) appendLogLine(data);
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
  }
  if (r.current_file) meta.push(`文件 <b>${fmt.esc(r.current_file)}</b>`);
  const running = r.phase === 'recording';
  return `
  <div class="room-card ${running ? 'recording' : ''}">
    <div class="rc-head">
      <span class="badge ${cls}">${label}</span>
      <span class="rc-name" title="${fmt.esc(r.anchor_name || r.vup)}">${fmt.esc(r.anchor_name || r.vup)}</span>
      <span class="rc-room">${r.room_id}</span>
      <span class="rc-actions">
        ${r.running
          ? `<button class="btn btn-sm btn-danger" data-stop="${r.room_id}">停止</button>`
          : `<button class="btn btn-sm btn-primary" data-start="${r.room_id}">开始</button>`}
      </span>
    </div>
    <div class="rc-title" title="${fmt.esc(r.title)}">${fmt.esc(r.title || '—')}</div>
    <div class="rc-meta">${meta.join(' · ') || '<span>未运行</span>'}</div>
    ${r.last_error && !r.connected ? `<div class="rc-meta" style="color:#ff9aa5">⚠ ${fmt.esc(r.last_error)}</div>` : ''}
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
      <td>${fmt.n((r.counters || {}).danmaku)}</td>
      <td>
        ${r.running
          ? `<button class="btn btn-sm btn-danger" data-stop2="${r.room_id}">停止</button>`
          : `<button class="btn btn-sm btn-primary" data-start2="${r.room_id}">开始</button>`}
        <button class="btn btn-sm btn-danger" data-del="${r.room_id}">删除</button>
      </td>
    </tr>`;
  }).join('');
}

$('#btnAddRoom').addEventListener('click', async () => {
  const room_id = Number($('#addRoomId').value.trim());
  const vup = $('#addVup').value.trim();
  const enabled = $('#addEnabled').checked;
  if (!room_id || room_id <= 0) return toast('请填写正确的直播间号', 'warn');
  try {
    await api('/api/targets', { method: 'POST', body: { room_id, vup, enabled } });
    $('#addRoomId').value = ''; $('#addVup').value = '';
    toast(`房间 ${room_id} 已添加${enabled ? '并开始录制' : ''}`, 'ok');
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
  if (!cb) return;
  try {
    await api(`/api/targets/${cb.dataset.enabled}`,
      { method: 'PUT', body: { enabled: cb.checked } });
    toast(cb.checked ? '已启用' : '已停用（运行中的任务需手动停止）', 'ok');
  } catch (err) { toast(err.message, 'err'); cb.checked = !cb.checked; }
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
$$('.tab').forEach((t) => t.addEventListener('click', () => {
  $$('.tab').forEach((x) => x.classList.toggle('active', x === t));
  state.filesKind = t.dataset.kind;
  loadFiles();
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
        <button class="btn btn-sm" data-preview='${JSON.stringify(f.path)}'>预览</button>
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

$('#btnSaveSettings').addEventListener('click', async () => {
  const sess = $('#setSess').value.trim();
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
  };
  if (sess) patch.sessdata = sess;
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
