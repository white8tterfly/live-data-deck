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
                 files: '录制文件', analysis: '数据分析',
                 logs: '运行日志', settings: '设置' };

function showView(name) {
  $$('.nav-item').forEach((a) => a.classList.toggle('active', a.dataset.view === name));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${name}`));
  $('#pageTitle').textContent = TITLES[name];
  if (name === 'stream') { state.streamUnread = 0; updateBadge(); }
  if (name === 'rooms') renderRoomTable();
  if (name === 'dashboard') renderRoomGrid();
  if (name === 'files') loadFiles();
  if (name === 'analysis') initAnalysisView();
  if (name === 'logs') renderLogs(true);
  if (name === 'settings') { fillSettings(); loadCared(); }
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
    if (r.popularity_now) {
      meta.push(`人气 <b>${fmt.n(r.popularity_now)}</b>`
        + (r.popularity_peak ? `（峰 ${fmt.n(r.popularity_peak)}）` : ''));
    }
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
$$('#view-files .tabs .tab').forEach((t) => t.addEventListener('click', () => {
  $$('#view-files .tabs .tab').forEach((x) => x.classList.toggle('active', x === t));
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
    return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}"
              stroke="#222a3b" stroke-width="1"/>
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

async function openReport(kind, path) {
  reportCur = { kind, path };
  $('#reportMask').hidden = false;
  $('#reportBody').innerHTML = '<div class="muted">正在汇总本场数据…</div>';
  $('#reportGen').disabled = false;
  $('#reportGen').textContent = '生成 PNG 一页战报';
  try {
    const s = await api('/api/report/summary', { method: 'POST', body: { path, kind } });
    const dur = s.span_sec >= 3600
      ? `${(s.span_sec / 3600).toFixed(1)} 小时` : `${Math.round(s.span_sec / 60)} 分钟`;
    const row = (l, v, cls = '') => `<div class="dedup-stat ${cls}"><b>${v}</b>${l}</div>`;
    $('#reportBody').innerHTML = `
      <div style="font-weight:650;font-size:15px;margin-bottom:4px">
        ${fmt.esc(s.title || '（无标题）')}</div>
      <div class="muted" style="margin-bottom:14px">
        ${fmt.esc(s.anchor || s.vup)} · ${fmt.esc(s.date || '日期未知')} · 时长 ${dur}
        ${s.watched_show ? '· ' + fmt.esc(s.watched_show) : ''}</div>
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
        : '<div class="hint" style="margin:0;color:var(--warn)">该文件录制于人气采集上线前，战报中无人气曲线（其余内容正常）。</div>'}`;
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
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    window.open(url, '_blank');
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    btn.textContent = '已生成（新标签页打开）';
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
