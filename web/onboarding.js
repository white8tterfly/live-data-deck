/* 新手引导：空态跳转 + 半透明灰蒙版分步教程，原生 JS 无构建。
   教程期间向后端申请一份演示数据，让「录制文件」「数据分析」有内容可看，结束时清除。
   对外暴露 window.LDDOnboarding（start / maybeAutoStart / notifyRoomAdded / isDone / reset）。 */
'use strict';
(function () {
  const DONE_KEY = 'ldd-onboarding-done';
  const DEMO_URL = '/api/onboarding/demo';
  const $ = (s, r = document) => r.querySelector(s);

  /* 教程步骤：view=所在页面，target=高亮选择器；targets=多个选择器（挖孔取并集），
     用于一步同时框住相邻的两块界面（如「添加直播间」要连关注导入一起框）；
     fallback=目标不可见时的回退选择器，
     tab=设置页子标签（进入该步时切换，使目标面板可见），
     filesTab=录制文件页签（records / weak / videos，进入该步时切换），
     enter=进入该步时主动触发的动作（hl=打开高能弹窗，ana=跑一次演示分析）。
     正文 text 为 HTML：用 <ol><li> 分条，重点词用 .tour-kw 着色、.tour-chip 加底、<b> 提亮 */
  const STEPS = [
    { view: 'dashboard', target: '#view-dashboard .stat-grid',
      title: '实时数据总览',
      text: '<ol>'
        + '<li><span class="tour-chip">在录房间（弹幕）</span>：正在录弹幕的房间数，下一行标出其中几间在直播、几间在录视频。</li>'
        + '<li><span class="tour-chip">WS 连接</span>：已连上的弹幕服务器数量，掉线会<span class="tour-kw">自动重连</span>。</li>'
        + '<li><span class="tour-chip">本次弹幕</span>：累计弹幕条数，后面附带 SC 与房管处置数。</li>'
        + '<li><span class="tour-chip">实时速率</span>：全部房间合计<span class="tour-kw">每分钟</span>产生多少条弹幕。</li>'
        + '</ol>' },
    { view: 'dashboard', target: '#roomGrid', fallback: '#dashboardEmpty',
      title: '房间卡片',
      text: '<ol>'
        + '<li>每个直播间一张卡片，顶部是<span class="tour-kw">开播状态圆点</span>与主播名。</li>'
        + '<li>中间滚动显示<span class="tour-kw">最近几条弹幕</span>，底部列出弹幕数、SC、礼物。</li>'
        + '<li>房间开播会<span class="tour-kw">自动开始</span>弹幕录制；卡片上也能单独开始 / 停止，或打开它的录制文件夹。</li>'
        + '</ol>' },
    { view: 'dashboard', target: '.topbar-right', fallback: '#globalPill',
      title: '全局操作与主题',
      text: '<ol>'
        + '<li><span class="tour-chip">● 全部开始</span>：一键连接所有「弹幕启用」的房间开始录弹幕。</li>'
        + '<li><span class="tour-chip">■ 全部停止</span>：一键断开全部房间。</li>'
        + '<li>中间<span class="tour-chip">胶囊</span>显示整体录制状态。</li>'
        + '<li>最右<span class="tour-chip">日 / 夜按钮</span>切换亮色与暗色主题，设置会自动记住。</li>'
        + '</ol>' },
    { view: 'stream', target: '#view-stream .toolbar', fallback: '#streamList',
      title: '实时弹幕 · 按房间与事件筛选、暂停',
      text: '<ol>'
        + '<li>左侧下拉可<span class="tour-kw">只看某几个房间</span>；一个都不勾 = 全部房间。</li>'
        + '<li><span class="tour-chip">弹幕 / SC / 礼物 / 房管处置</span>四个勾选框按事件类型筛选，点<span class="tour-chip">全选 / 反选</span>批量调整。</li>'
        + '<li><span class="tour-chip">暂停滚动</span>冻结画面方便细看。</li>'
        + '<li><span class="tour-chip">清空</span>只清屏，<span class="tour-kw">不删文件</span>。</li>'
        + '</ol>' },
    { view: 'stream', target: '#streamList',
      title: '实时弹幕 · 内容区',
      text: '<ol>'
        + '<li>弹幕实时滚动区：普通弹幕、SC、礼物、房管处置用<span class="tour-kw">不同颜色</span>区分。</li>'
        + '<li>每条都带<span class="tour-kw">发送时间与用户名</span>。</li>'
        + '<li>为省内存只保留<span class="tour-chip">最近 500 条</span>，滚动时旧消息自动淡出。</li>'
        + '</ol>' },
    { view: 'rooms', targets: ['#view-rooms .add-row', '#btnImportFollowing'], fallback: '#btnImportFollowing',
      title: '添加直播间',
      text: '<ol>'
        + '<li><span class="tour-chip">直播间号</span>填网址里那串数字：<span class="tour-chip">live.bilibili.com/12345</span> 就填 <b>12345</b>。</li>'
        + '<li><span class="tour-chip">保存目录名</span>可留空自动命名；勾选<span class="tour-chip">弹幕启用</span>后点<span class="tour-kw">添加房间</span>，即开始录弹幕。</li>'
        + '<li>也可以点上面的<span class="tour-chip">从 B 站关注列表一键导入</span>：读取你关注的主播、看谁正在开播，勾选后<span class="tour-kw">批量加入</span>。</li>'
        + '</ol>' },
    { view: 'rooms', target: '#view-rooms .table-scroll', fallback: '#roomTable',
      title: '录制目标管理',
      text: '<ol>'
        + '<li>所有已添加房间都在这张表里，可逐间<span class="tour-kw">开始 / 停止</span>。</li>'
        + '<li>开关<span class="tour-chip">弹幕启用</span>、<span class="tour-chip">视频</span>、<span class="tour-chip">开播提醒</span>。</li>'
        + '<li>点<span class="tour-chip">⚙ 设置</span>为该房间单独调整 SC / 礼物、隐私打码、画质、AI 等（不设则跟<span class="tour-kw">全局</span>）。</li>'
        + '<li><span class="tour-chip">删除</span>只移除房间，<span class="tour-kw">不删已录文件</span>。</li>'
        + '</ol>' },
    { view: 'files', filesTab: 'records', target: '#view-files .tabs', fallback: '#filesTable',
      title: '录制文件 · 三个页签',
      text: '<ol>'
        + '<li><span class="tour-chip">弹幕记录</span>：每场一份弹幕数据，可预览 / 下载，并能生成单场 PNG 战报。</li>'
        + '<li><span class="tour-chip">房管处置存档</span>：房管拉黑 / 禁言时，自动留存该观众此前发过的弹幕。</li>'
        + '<li><span class="tour-chip">视频录制</span>：录下的视频场次、高能时间轴与字幕。</li>'
        + '<li>下面三步逐个页签细看；教程期间会出现<span class="tour-kw">演示文件</span>，结束后自动清除。</li>'
        + '</ol>' },
    { view: 'files', filesTab: 'records', target: '#filesPanelRecords', fallback: '#view-files .tabs',
      title: '录制文件 · 弹幕记录',
      text: '<ol>'
        + '<li>每行是一份<span class="tour-kw">弹幕记录文件</span>：可<span class="tour-chip">预览</span>内容、<span class="tour-chip">下载</span>保存。</li>'
        + '<li>点<span class="tour-chip">战报</span>按这一场生成 PNG 一页战报，含弹幕榜、SC 榜、营收与高能片段。</li>'
        + '<li>教程期间的演示文件会在结束时<span class="tour-kw">自动清除</span>。</li>'
        + '</ol>' },
    { view: 'files', filesTab: 'weak', target: '#view-files .tabs .tab[data-kind="weak"]', fallback: '#view-files .tabs',
      title: '录制文件 · 房管处置存档',
      text: '<ol>'
        + '<li>存的是另一类文件：房管在直播中<span class="tour-kw">拉黑 / 禁言</span>某观众后，程序把该观众此前发过的弹幕单独存一份。</li>'
        + '<li>用途：方便回看<span class="tour-kw">处理依据</span>。</li>'
        + '<li>平时这里通常是<span class="tour-kw">空的</span>，只有真发生过房管操作才有文件。</li>'
        + '</ol>' },
    { view: 'files', filesTab: 'videos', target: '#videosTable', fallback: '#filesPanelVideos',
      title: '录制文件 · 视频录制',
      text: '<ol>'
        + '<li>按场次列出录下的视频：每行显示主播、场次名、大小、时长与开播时间。</li>'
        + '<li><span class="tour-chip">高能时间轴</span>查看精彩片段；<span class="tour-chip">导出弹幕版</span>把弹幕烧进画面生成新视频。</li>'
        + '<li><span class="tour-chip">下载弹幕文件</span>拿 ASS 字幕；也可<span class="tour-chip">删除</span>整场。</li>'
        + '<li>页面下方还可能列出<span class="tour-chip">散片</span>——录制中途异常中断留下的视频残段，没用可删。</li>'
        + '</ol>' },
    { view: 'files', filesTab: 'videos', enter: 'hl', target: '#hlMask .modal', fallback: '#hlBody',
      title: '高能时间轴',
      text: '<ol>'
        + '<li>它把一场直播里<span class="tour-kw">弹幕突然变密</span>的时间点标出来，当作可能的高能时刻。</li>'
        + '<li>下方可调<span class="tour-chip">窗口秒数</span>与<span class="tour-chip">Top 条数</span>后<span class="tour-chip">重新分析</span>。</li>'
        + '<li>可选 <b>A 轻量 / C 深度</b>档让 AI 解读本场，或重新生成弹幕字幕。</li>'
        + '<li>右上角<span class="tour-chip">关闭</span>收起窗口。</li>'
        + '</ol>' },
    { view: 'analysis', target: '#view-analysis .analysis-side', fallback: '#analysisReport',
      title: '数据分析 · 选择文件',
      text: '<ol>'
        + '<li>左侧勾选一个或多个录制文件（可<span class="tour-kw">跨主播多选</span>），点<span class="tour-chip">开始分析</span>即出结果。</li>'
        + '<li><span class="tour-chip">后处理去重</span>按窗口秒数清理刷屏重复弹幕，结果另存为 <b>.dedup</b> 文件，<span class="tour-kw">不改动原始数据</span>。</li>'
        + '</ol>' },
    { view: 'analysis', enter: 'ana', target: '#analysisReport', fallback: '#view-analysis .analysis-side',
      title: '数据分析 · 分析结果',
      text: '<ol>'
        + '<li>勾选左侧文件后点<span class="tour-chip">开始分析</span>，右边这块显示统计结果。</li>'
        + '<li>指标卡：总弹幕数、参与人数、营收等。</li>'
        + '<li>图表：弹幕时间轴、高频弹幕、活跃用户排行。</li>'
        + '<li>分析<span class="tour-kw">只在本地做统计</span>，不改动文件、也不联网。</li>'
        + '</ol>' },
    { view: 'logs', target: '#view-logs .terminal', fallback: '#view-logs .terminal-bar',
      title: '运行日志',
      text: '<ol>'
        + '<li>程序的运行记录，可按级别筛选：<span class="tour-chip">INFO</span> 常规信息、<span class="tour-chip">WARN</span> 提醒、<span class="tour-chip">ERROR</span> 出错。</li>'
        + '<li>取消勾选即<span class="tour-kw">只看某一类</span>。</li>'
        + '<li><span class="tour-chip">清空显示</span>只清这里的内容，<span class="tour-kw">不删磁盘上的日志文件</span>。</li>'
        + '<li>点上方的<span class="tour-chip">日志目录路径</span>可在文件管理器里打开日志文件夹。</li>'
        + '</ol>' },
    { view: 'settings', tab: 'base', target: '#settingsTabs .st-tab[data-tab="base"]', fallback: '#settingsTabs',
      title: '设置 · 基础与策略',
      text: '<ol>'
        + '<li>填登录凭证（SESSDATA，可选）、数据保存目录。</li>'
        + '<li>设全局录制策略：是否录 SC / 礼物、是否过滤纯表情、是否匿名、是否隐私打码等。</li>'
        + '<li>这里的开关也是<span class="tour-kw">新房间的默认值</span>，可在房间设置里逐间覆盖。</li>'
        + '<li>本页顶部入口条还能<span class="tour-kw">随时重新播放</span>本教程。</li>'
        + '</ol>' },
    { view: 'settings', tab: 'video', target: '#settingsTabs .st-tab[data-tab="video"]', fallback: '#settingsTabs',
      title: '设置 · 视频直录',
      text: '<ol>'
        + '<li>视频直录是<span class="tour-kw">附加功能、默认关闭</span>。</li>'
        + '<li>打开全局总开关后，勾选了<span class="tour-chip">视频</span>的房间开播才会额外直录直播流。</li>'
        + '<li>这里还能设默认画质、磁盘剩余空间下限与视频保存目录。</li>'
        + '<li>只打算录弹幕的话，这一页<span class="tour-kw">完全不用管</span>。</li>'
        + '</ol>' },
    { view: 'settings', tab: 'ds', target: '#settingsTabs .st-tab[data-tab="ds"]', fallback: '#settingsTabs',
      title: '设置 · 弹幕样式',
      text: '<ol>'
        + '<li>调旁挂 <b>ASS 字幕</b>的外观：字体、字号、滚动速度、透明度以及 SC 卡片样式。</li>'
        + '<li>右侧有<span class="tour-chip">实时预览</span>，改完点本页保存按钮生效。</li>'
        + '<li>生成的 ASS 文件随视频一起用播放器加载，弹幕<span class="tour-kw">不会烧进画面</span>。</li>'
        + '</ol>' },
    { view: 'settings', tab: 'ai', target: '#settingsTabs .st-tab[data-tab="ai"]', fallback: '#settingsTabs',
      title: '设置 · AI 解读',
      text: '<ol>'
        + '<li>把高能片段交给 <b>OpenAI 兼容接口</b>生成标题与文案。</li>'
        + '<li>可一键套用 <b>DeepSeek / 通义千问 / Kimi / 本地 Ollama</b> 模板，填好 API 地址、Key 与模型名即可。</li>'
        + '<li>不填也<span class="tour-kw">不影响录制</span>。</li>'
        + '<li>分析<span class="tour-kw">只外发弹幕文本</span>，不会上传视频或 Cookie。</li>'
        + '</ol>' },
    { view: 'settings', tab: 'cared', target: '#settingsTabs .st-tab[data-tab="cared"]', fallback: '#settingsTabs',
      title: '设置 · 关心名单',
      text: '<ol>'
        + '<li>记录你特别关注的人（如常互动的观众或房管）。</li>'
        + '<li>加进来之后，后处理去重时这些用户的弹幕会<span class="tour-kw">全部保留</span>，分析时也会单独统计。</li>'
        + '<li>名单<span class="tour-kw">即时保存</span>，不用点底部的统一保存按钮。</li>'
        + '</ol>' },
    { view: 'settings', tab: 'theme', target: '#settingsTabs .st-tab[data-tab="theme"]', fallback: '#settingsTabs',
      title: '设置 · 战报样式',
      text: '<ol>'
        + '<li>调 PNG 战报的配色、字体、区块与封面文字。</li>'
        + '<li>可先生成<span class="tour-chip">预览</span>看看效果。</li>'
        + '<li>本页<span class="tour-kw">独立保存</span>，改完点页内的保存按钮即可。</li>'
        + '</ol>' },
  ];

  let overlay = null, hole = null, tip = null;
  let cur = 0, active = false, savedTab = null;
  let hlOpened = false;      // 引导是否打开过高能时间轴弹窗，结束时据此关闭
  let demoAnaPath = null;    // 演示分析勾选的文件路径，结束时据此还原勾选

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  /* 教程完成标记的读写（localStorage 不可用时静默降级） */
  function isDone() { try { return localStorage.getItem(DONE_KEY) === '1'; } catch (e) { return false; } }
  function markDone() { try { localStorage.setItem(DONE_KEY, '1'); } catch (e) { /* 隐私模式忽略 */ } }
  function reset() { try { localStorage.removeItem(DONE_KEY); } catch (e) { /* 忽略 */ } }

  /* 空态引导卡片的显隐（元素在 index.html 里） */
  function guideCard() { return $('#onboardGuideCard'); }
  function showGuideCard() { const c = guideCard(); if (c) c.hidden = false; }
  function hideGuideCard() { const c = guideCard(); if (c) c.hidden = true; }

  /* 仅在目标页未激活时切换，避免无谓重渲染 */
  function switchView(name) {
    const view = name && document.getElementById('view-' + name);
    if (!view || view.classList.contains('active')) return;
    if (typeof window.showView === 'function') window.showView(name);
    else if (typeof window.navTo === 'function') window.navTo(name);
  }

  /* 设置页步骤：切到指定子标签，使目标面板可见；优先复用 app.js 的 switchSettingsTab */
  function switchTab(name) {
    if (!name) return;
    if (typeof window.switchSettingsTab === 'function') { window.switchSettingsTab(name); return; }
    const btn = document.querySelector('#settingsTabs .st-tab[data-tab="' + name + '"]');
    if (btn) btn.click();
  }

  /* 录制文件页签切换（data-kind：records / weak / videos），点对应按钮复用 app.js 的处理 */
  function switchFilesTab(kind) {
    if (!kind) return;
    const btn = document.querySelector('#view-files .tabs .tab[data-kind="' + kind + '"]');
    if (btn && !btn.classList.contains('active')) btn.click();
  }

  /* 进入高能时间轴步骤：等视频场次表出现后打开弹窗（演示数据写入到列表刷新之间有延迟，轮询几次） */
  function openTourHighlights() {
    const hooks = window.LDDTourHooks;
    if (!hooks || typeof hooks.openHighlights !== 'function' || hlOpened) return;
    const tryOpen = () => {
      const btn = document.querySelector('#videosTable [data-hl]');
      if (!btn || !btn.dataset.vup) return false;
      try { hooks.openHighlights(btn.dataset.vup, btn.dataset.hl); hlOpened = true; }
      catch (e) { return false; }
      return true;
    };
    if (tryOpen()) return;
    let n = 0;
    const t = setInterval(() => {
      n += 1;
      if (tryOpen() || n >= 12 || !active) clearInterval(t);
    }, 250);
  }

  /* 关闭高能时间轴弹窗（引导打开过才关），离开该步或结束教程时调用 */
  function closeTourHighlights() {
    if (!hlOpened) return;
    hlOpened = false;
    const hooks = window.LDDTourHooks;
    if (hooks && typeof hooks.closeHighlights === 'function') {
      try { hooks.closeHighlights(); return; } catch (e) { /* 兜底走直接操作 DOM */ }
    }
    const mask = $('#hlMask');
    if (mask) mask.hidden = true;
  }

  /* 进入分析结果步骤：让 app.js 勾选第一个文件并跑一次演示分析，记下勾选项供结束时还原 */
  function demoAnalysis() {
    const hooks = window.LDDTourHooks;
    if (!hooks || typeof hooks.runDemoAnalysis !== 'function' || demoAnaPath) return;
    Promise.resolve()
      .then(() => hooks.runDemoAnalysis())
      .then((path) => { if (path) demoAnaPath = path; })
      .catch(() => { /* 分析失败只当普通步骤，不打断教程 */ });
  }

  /* 还原演示分析勾选的文件：取消勾选并同步分析页的选择集合 */
  function restoreDemoAnalysis() {
    if (!demoAnaPath) return;
    const path = demoAnaPath;
    demoAnaPath = null;
    document.querySelectorAll('#anaFileTree input[data-path]').forEach((cb) => {
      if (cb.dataset.path === path && cb.checked) {
        cb.checked = false;
        cb.dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
  }

  /* 元素是否真实可见（有尺寸且未被 display / visibility 隐藏） */
  function visible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 && r.height < 1) return false;
    const cs = getComputedStyle(el);
    return cs.display !== 'none' && cs.visibility !== 'hidden';
  }
  /* 取步骤目标元素集合：targets 数组逐个匹配，只收真实可见的；
     一个都没命中时回退到 fallback。返回可能为空数组（此时不挖孔、气泡居中） */
  function targetEls(step) {
    const sel = step.targets || (step.target ? [step.target] : []);
    const els = [];
    sel.forEach((s) => {
      const el = document.querySelector(s);
      if (visible(el)) els.push(el);
    });
    if (!els.length && step.fallback) {
      const el = document.querySelector(step.fallback);
      if (visible(el)) els.push(el);
    }
    return els;
  }

  /* 多个目标元素的合并外接矩形，用于把挖孔框成一块（相邻元素连起来整块高亮） */
  function unionRect(els) {
    let l = Infinity, t = Infinity, r = -Infinity, b = -Infinity;
    els.forEach((el) => {
      const q = el.getBoundingClientRect();
      l = Math.min(l, q.left); t = Math.min(t, q.top);
      r = Math.max(r, q.right); b = Math.max(b, q.bottom);
    });
    return { left: l, top: t, right: r, bottom: b, width: r - l, height: b - t };
  }

  /* 首次开始时构建遮罩、挖孔与气泡 DOM，并绑定跳过 / 上一步 / 下一步按钮 */
  function build() {
    overlay = document.createElement('div');
    overlay.className = 'tour-overlay';
    overlay.hidden = true;
    hole = document.createElement('div');
    hole.className = 'tour-hole';
    tip = document.createElement('div');
    tip.className = 'tour-tip';
    tip.innerHTML = ''
      + '<div class="tour-tip-head">'
      +   '<span class="tour-tip-step"></span>'
      +   '<button type="button" class="tour-skip">跳过教程</button>'
      + '</div>'
      + '<div class="tour-tip-title"></div>'
      + '<div class="tour-tip-text"></div>'
      + '<div class="tour-tip-foot">'
      +   '<button type="button" class="btn btn-sm tour-prev">上一步</button>'
      +   '<span class="tour-tip-spacer"></span>'
      +   '<button type="button" class="btn btn-sm btn-primary tour-next">下一步</button>'
      + '</div>';
    overlay.appendChild(hole);
    overlay.appendChild(tip);
    document.body.appendChild(overlay);
    tip.querySelector('.tour-skip').addEventListener('click', () => end(true));
    tip.querySelector('.tour-prev').addEventListener('click', () => go(cur - 1));
    tip.querySelector('.tour-next').addEventListener('click', () => go(cur + 1));
  }

  /* 把当前步骤的标题与正文写进气泡，并刷新步数进度与按钮状态 */
  function render() {
    const step = STEPS[cur];
    tip.querySelector('.tour-tip-step').textContent = '第 ' + (cur + 1) + ' / ' + STEPS.length + ' 步';
    tip.querySelector('.tour-tip-title').textContent = step.title;
    /* 正文为 HTML（分条列表 + 关键词高亮），内容全部由本文件静态定义，无外部输入 */
    tip.querySelector('.tour-tip-text').innerHTML = step.text;
    tip.querySelector('.tour-prev').style.visibility = cur === 0 ? 'hidden' : 'visible';
    tip.querySelector('.tour-next').textContent = cur === STEPS.length - 1 ? '完成' : '下一步';
  }

  /* 对齐挖孔到目标，并把气泡放到目标旁（下方→上方→右侧→左侧依次尝试） */
  function layout() {
    if (!active) return;
    const step = STEPS[cur];
    const els = targetEls(step);
    const gap = 12, pad = 6;
    const vw = window.innerWidth, vh = window.innerHeight;
    const maxX = () => Math.max(8, vw - tip.offsetWidth - 8);
    const maxY = () => Math.max(8, vh - tip.offsetHeight - 8);

    let r = null;
    if (els.length) {
      /* 多目标取并集，把相邻元素框成一整块 */
      r = unionRect(els);
      hole.style.left = (r.left - pad) + 'px';
      hole.style.top = (r.top - pad) + 'px';
      hole.style.width = (r.width + pad * 2) + 'px';
      hole.style.height = (r.height + pad * 2) + 'px';
      hole.style.display = '';
    } else {
      hole.style.display = 'none';
    }

    tip.style.visibility = 'hidden';
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x, y;
    if (!r) {
      x = clamp((vw - tw) / 2, 8, maxX());
      y = clamp((vh - th) / 2, 8, maxY());
    } else if (r.bottom + gap + th <= vh) {
      y = r.bottom + gap;
      x = clamp(r.left + r.width / 2 - tw / 2, 8, maxX());
    } else if (r.top - gap - th >= 0) {
      y = r.top - gap - th;
      x = clamp(r.left + r.width / 2 - tw / 2, 8, maxX());
    } else if (r.right + gap + tw <= vw) {
      x = r.right + gap;
      y = clamp(r.top, 8, maxY());
    } else if (r.left - gap - tw >= 0) {
      x = r.left - gap - tw;
      y = clamp(r.top, 8, maxY());
    } else {
      x = clamp(r.left, 8, maxX());
      y = clamp(vh - th - gap, 8, maxY());
    }
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
    tip.style.visibility = 'visible';
  }

  /* 跳到第 n 步：切换所在页面、渲染气泡，下一帧把目标滚入视口并重新布局 */
  function go(n) {
    if (!active) return;
    if (n >= STEPS.length) { end(true); return; }
    if (n < 0) n = 0;
    cur = n;
    const step = STEPS[cur];
    closeTourHighlights();      // 换步先收掉上一波可能开着的高能弹窗
    switchView(step.view);
    switchTab(step.tab);
    switchFilesTab(step.filesTab);
    if (step.enter === 'hl') openTourHighlights();
    else if (step.enter === 'ana') demoAnalysis();
    render();
    requestAnimationFrame(() => {
      const els = targetEls(STEPS[cur]);
      if (els.length) els[0].scrollIntoView({ block: 'nearest', inline: 'nearest' });
      requestAnimationFrame(layout);
    });
  }

  /* resize / 滚动时重新对齐挖孔与气泡；Esc 结束教程并记为已完成 */
  function onReflow() { if (active) requestAnimationFrame(layout); }
  function onKey(e) { if (active && e.key === 'Escape') { e.preventDefault(); end(true); } }

  /* 演示数据 */
  /* 让 app.js 注入的刷新回调去重载「录制文件」「数据分析」列表（页面没加载过时它自行跳过） */
  function refreshLists() {
    if (typeof window.LDDRefreshLists !== 'function') return;
    try { window.LDDRefreshLists(); } catch (e) { /* 刷新失败不影响教程 */ }
  }
  /* 教程开始：请求写入演示数据，成功后刷新列表；接口不可用等异常一律忽略 */
  function demoOn() {
    fetch(DEMO_URL, { method: 'POST' }).catch(() => {}).then(refreshLists);
  }
  /* 教程结束：请求清除演示数据，随后刷新列表 */
  function demoOff() {
    fetch(DEMO_URL, { method: 'DELETE' }).catch(() => {}).then(refreshLists);
  }

  /* 开始教程：opts.force 可忽略已完成标记；构建 DOM、挂监听并进入第 0 步 */
  function start(opts) {
    if (active) return;
    if (!(opts && opts.force) && isDone()) return;
    if (!overlay) build();
    if (!overlay) return;
    active = true;
    cur = 0;
    hlOpened = false;
    demoAnaPath = null;
    /* 记住设置页当前子标签，教程结束后恢复，避免教程改动了用户的记忆 */
    savedTab = null;
    try { savedTab = localStorage.getItem('ldd-settings-tab'); } catch (e) { /* 忽略 */ }
    overlay.hidden = false;
    window.addEventListener('resize', onReflow);
    window.addEventListener('scroll', onReflow, true);
    document.addEventListener('keydown', onKey, true);
    go(0);
    demoOn();
  }

  /* 结束教程：mark 为真时写完成标记，隐藏遮罩并解绑全部监听，还原引导期间的 Demo 动作 */
  function end(mark) {
    if (!active) return;
    active = false;
    if (mark) markDone();
    closeTourHighlights();
    restoreDemoAnalysis();
    if (overlay) overlay.hidden = true;
    window.removeEventListener('resize', onReflow);
    window.removeEventListener('scroll', onReflow, true);
    document.removeEventListener('keydown', onKey, true);
    if (savedTab) switchTab(savedTab);
    demoOff();
  }

  /* 启动时调用：房间为空 → 跳转房间管理并展示引导卡；否则不打扰 */
  function maybeAutoStart(opts) {
    const rooms = (opts && opts.rooms) || [];
    if (!rooms.length) {
      showGuideCard();
      if (typeof window.navTo === 'function') window.navTo('rooms');
    } else {
      hideGuideCard();
    }
  }

  /* 成功添加房间后调用：首次（未完成过教程）自动开始分步教程 */
  function notifyRoomAdded() {
    hideGuideCard();
    if (active || isDone()) return;
    start();
  }

  window.LDDOnboarding = { start, maybeAutoStart, notifyRoomAdded, isDone, reset };
})();
