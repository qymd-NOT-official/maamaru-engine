/**
 * まあ丸 近侍面板 — 前端逻辑
 * 2026-07-30 布局重构（按鹿目圆草图）：
 *   概览 = 左常用功能列表 + 中选中功能区/日志 + 右统计表
 *   配置 = 双栏卡片，参数内嵌卡片直接调，运行按钮小小的
 */

(function() {
  'use strict';

  // ── DOM refs ──
  const frontend = window.Maamaru;
  const $ = frontend?.dom.one || ((s) => document.querySelector(s));
  const $$ = frontend?.dom.all || ((s) => document.querySelectorAll(s));

  const els = {
    tabs:       $$('.tab'),
    panels:     $$('.tab-panel'),
    scriptGrid: $('#script-grid'),
    configNav:  $('#config-nav'),
    configSelect: $('#config-select'),
    configDetail: $('#config-detail'),
    commonExpPanel: $('#exp-common-panel'),
    schedPanel: $('#sched-panel'),
    schedDock:  $('#sched-dock'),
    funcList:   $('#func-list'),
    funcPrev:   $('#func-prev'),
    funcNext:   $('#func-next'),
    funcDetail: $('#func-detail'),
    taskIndicator: $('#task-indicator'),
    stopAll:    $('#btn-stop-all'),
    statusDot:  $('#status-dot'),
    statusText: $('#status-text'),
    settingsBtn: null,
    themeBtn:   $('#btn-theme'),
    systemNav:  $('#system-nav'),
    systemPrev: $('#system-prev'),
    systemNext: $('#system-next'),
    systemDetail: $('#system-detail'),
    sysStatus:  $('#sys-status'),
  };

  // ── State ──
  let isRunning = false;
  let currentScript = null;
  let scriptMeta = {};   // name -> {label, desc, params}
  // 概览左侧选中的功能（默认日课，记住上次选择）
  let selectedScript = localStorage.getItem('maamaru_selected') || 'daily';
  // 参数表单：创建一次常驻内存，配置页渲染时挂进卡片（DOM 搬家，状态不丢）
  const paramForms = {};
  // 全局名单配置缓存（repair_blacklist / dismantle_whitelist）

  // 配置页左侧分组（按企业名单草图）
  const CONFIG_GROUPS = [
    { label: '日常配置', items: [
      { key: 'daily', label: '一键日课' },
      { key: 'repair', label: '手入' },
    ]},
    { label: '活动配置', items: [
      { key: 'raid', label: '联队战' },
      { key: 'pumpkin', label: '南瓜大作战' },
    ]},
    { label: '出阵配置', items: [
      { key: 'sortie', label: '出阵' },
      { key: 'yosari', label: '异去' },
      { key: 'osaka', label: '大阪城挖地' },
      { key: 'sakura', label: '刷花' },
      { key: 'practice', label: '演练' },
    ]},
    { label: '后勤配置', items: [
      { key: 'expedition', label: '远征' },
      { key: '_expedition_schedule', target: 'expedition', label: '自动排班', panel: 'schedule' },
      { key: 'dispatch', label: '派遣远征' },
      { key: 'forge', label: '锻刀' },
      { key: 'sugar', label: '炼糖' },
      { key: 'snapshot', label: '库存快照' },
    ]},
    { label: '名单设置', items: [
      { key: '_repair_blacklist', label: '手入黑名单', listKey: 'repair_blacklist',
        desc: '修复列表里看到这几把刀就跳过，适合碰瓷队带伤上班' },
      { key: '_dismantle_whitelist', label: '刀解白名单', listKey: 'dismantle_whitelist',
        desc: '刀解只解这里面的刀，留空不解' },
    ]},
  ];

  function resolveAlias(key) {
    for (const g of CONFIG_GROUPS) {
      for (const it of g.items) {
        if (it.key === key) return it.target || it.key;
      }
    }
    return key;
  }

  function findConfigItem(key) {
    for (const g of CONFIG_GROUPS) {
      for (const it of g.items) {
        if (it.key === key) return it;
      }
    }
    return null;
  }

  const taskRunner = frontend.taskRunner.create({
    els,
    resolveAlias,
    getMeta: () => scriptMeta,
    getState: () => ({ running: isRunning, current: currentScript }),
    setState: (running, current) => {
      isRunning = running;
      currentScript = current;
    },
    render: renderScripts,
    onStarted: realName => {
      recordScriptUse(realName);
      renderFuncList();
    },
    onFinished: () => frontend.dashboard?.load(),
  });

  // ── 主题切换：由注册表驱动，新皮肤不需改页面逻辑 ──
  function applyTheme(themeId) {
    const theme = frontend.theme.apply(themeId);
    const nextTheme = frontend.theme.themes[
      (frontend.theme.themes.findIndex(item => item.id === theme.id) + 1) % frontend.theme.themes.length
    ];
    els.themeBtn.textContent = '';
    els.themeBtn.title = `切换到${nextTheme.label}主题`;
    els.themeBtn.setAttribute('aria-label', els.themeBtn.title);
    // 同步到服务器（合并式存储，不会冲掉脚本参数记忆）
    fetch('/api/saved-settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: theme.id }),
    }).catch(() => {});
  }
  els.themeBtn.addEventListener('click', () => {
    const themes = frontend.theme.themes;
    const currentIndex = themes.findIndex(theme => theme.id === frontend.theme.current());
    applyTheme(themes[(currentIndex + 1) % themes.length].id);
  });
  applyTheme(localStorage.getItem('maamaru_theme') || 'washi');

  // ── Tab switching ──
  els.tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      els.tabs.forEach(t => t.classList.remove('active'));
      els.panels.forEach(p => p.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('panel-' + tab.dataset.tab).classList.add('active');
    });
  });

  // ── Scripts: load list ──
  async function loadScripts() {
    try {
      const r = await fetch('/api/scripts');
      const data = await r.json();
      isRunning = data.running;
      currentScript = data.current;
      scriptMeta = data.scripts;
      if (!scriptMeta[resolveAlias(selectedScript)]) selectedScript = 'daily';
      renderScripts();
      taskRunner.updateStatus(data.running, data.current);
    } catch(e) {
      els.scriptGrid.innerHTML = '<p style="color:var(--red)">无法连接面板后端，确认 panel 已启动。</p>';
    }
  }

  // ── 参数记忆：每张卡的上次选项存 localStorage，重启不丢 ──
  function savedParams(scriptName) {
    try {
      const params = JSON.parse(localStorage.getItem('maamaru_params_' + scriptName) || '{}');
      // 出阵数量统一为 runs；第一次打开新版时接住旧页面保存的不同字段。
      if (params.runs == null) {
        const legacyKey = scriptName === 'raid' ? 'rounds'
          : scriptName === 'pumpkin' ? 'max_skips'
          : (scriptName === 'sortie' || scriptName === 'yosari') ? 'loops'
          : null;
        if (legacyKey && params[legacyKey] != null) params.runs = params[legacyKey];
      }
      return params;
    } catch(e) { return {}; }
  }

  // ── 刀剑候选列表（名单选择器共用）──
  // ── 参数表单渲染 ──
  function renderParamField(scriptName, field) {
    return frontend.paramForm.renderField(field, savedParams(scriptName)[field.key]);
  }

  // ── 条件显隐（visibleWhen {key, is|not}）──
  function updateCardVisibility(card) {
    frontend.paramForm.updateVisibility(card);
  }

  function collectParams(card) {
    return frontend.paramForm.collect(card);
  }

  // ── 参数表单：创建一次常驻 paramForms，渲染配置卡片时挂进去 ──
  function ensureParamForms() {
    Object.entries(scriptMeta).forEach(([key, info]) => {
      if (paramForms[key]) return;
      const fields = info.params || [];
      if (!fields.length) return;
      const form = document.createElement('div');
      form.className = 'param-form';
      form.dataset.script = key;
      fields.forEach(f => form.appendChild(renderParamField(key, f)));
      // 条件显隐：控制器变了就重算，先算一遍初始状态
      form.querySelectorAll('select[data-param-key]').forEach(sel =>
        sel.addEventListener('change', () => updateCardVisibility(form)));
      updateCardVisibility(form);
      // 概览选中区的参数摘要跟着表单一起变
      form.addEventListener('change', () => {
        if (key === resolveAlias(selectedScript)) renderFuncDetail();
      });
      form.addEventListener('click', (e) => {
        if ((e.target.closest('.pill') || e.target.closest('[data-act]'))
            && key === resolveAlias(selectedScript)) renderFuncDetail();
      });
      paramForms[key] = form;
    });
  }

  function paramsOf(key) {
    return collectParams(paramForms[resolveAlias(key)]);
  }

  // ── 概览左栏：常用功能列表 ──
  const FUNC_ICONS = {
    daily: '🌅', raid: '⚔️', pumpkin: '🎃', sortie: '🗡', yosari: '🏮', osaka: '⛏️', sakura: '🌸',
    practice: '🥊', expedition: '🏕', dispatch: '⛺', forge: '🔥',
    sugar: '🍬', repair: '🛠', snapshot: '📦',
  };

  const FUNC_ICON_IMAGES = {
    daily: '/static/img/ui/daily.png',
    raid: '/static/img/ui/raid.png',
    pumpkin: '/static/img/ui/pumpkin.png',
    sortie: '/static/img/ui/sortie.png',
    yosari: '/static/img/ui/yosari.png',
    osaka: '/static/img/ui/digging.png',
    sakura: '/static/img/ui/sakura.png',
    practice: '/static/img/ui/practice.png',
    expedition: '/static/img/ui/expedition.png',
    dispatch: '/static/img/ui/expedition.png',
    forge: '/static/img/ui/forge.png',
    sugar: '/static/img/ui/sugar.png',
    repair: '/static/img/ui/repair-tools.png',
    snapshot: '/static/img/ui/snapshot.png',
    _dismantle_whitelist: '/static/img/ui/dismantle.png',
  };

  const FUNC_USAGE_KEY = 'maamaru_func_usage_v1';
  const DEFAULT_FREQUENT_ORDER = [
    'daily', 'sortie', 'yosari', 'osaka', 'expedition', 'repair', 'forge', 'pumpkin',
    'raid', 'sugar', 'sakura', 'practice', 'snapshot', 'dispatch',
  ];

  function funcIconMarkup(key) {
    const src = FUNC_ICON_IMAGES[key];
    if (src) return `<img class="task-pixel-icon" src="${src}" alt="">`;
    return FUNC_ICONS[key] || '🧰';
  }

  function scriptOrder() {
    return ['daily', ...Object.keys(scriptMeta).filter(k => k !== 'daily')]
      .filter(k => scriptMeta[k]);
  }

  function readScriptUsage() {
    try {
      const value = JSON.parse(localStorage.getItem(FUNC_USAGE_KEY) || '{}');
      return value && typeof value === 'object' ? value : {};
    } catch (_) {
      return {};
    }
  }

  function recordScriptUse(key) {
    try {
      const realKey = resolveAlias(key);
      const usage = readScriptUsage();
      usage[realKey] = (Number(usage[realKey]) || 0) + 1;
      localStorage.setItem(FUNC_USAGE_KEY, JSON.stringify(usage));
    } catch (_) {}
  }

  function rankedScriptOrder() {
    const usage = readScriptUsage();
    const fallbackIndex = new Map(DEFAULT_FREQUENT_ORDER.map((key, index) => [key, index]));
    const ranked = scriptOrder().sort((a, b) => {
      const countDiff = (Number(usage[b]) || 0) - (Number(usage[a]) || 0);
      if (countDiff) return countDiff;
      return (fallbackIndex.get(a) ?? 999) - (fallbackIndex.get(b) ?? 999);
    });
    return ranked;
  }

  function selectOverviewScript(key) {
    selectedScript = key;
    localStorage.setItem('maamaru_selected', key);
    renderFuncList();
    renderFuncDetail();
    frontend.dashboard?.preview(resolveAlias(key), scriptMeta[resolveAlias(key)]?.label || '');
  }

  function updateFuncCarousel() {
    if (!els.funcList || !els.funcPrev || !els.funcNext) return;
    const maxScroll = Math.max(0, els.funcList.scrollWidth - els.funcList.clientWidth);
    els.funcPrev.disabled = els.funcList.scrollLeft <= 2;
    els.funcNext.disabled = els.funcList.scrollLeft >= maxScroll - 2;
  }

  function moveFuncCarousel(direction) {
    const distance = Math.max(120, Math.round(els.funcList.clientWidth * 0.72));
    els.funcList.scrollBy({ left: direction * distance, behavior: 'smooth' });
  }

  els.funcPrev?.addEventListener('click', () => moveFuncCarousel(-1));
  els.funcNext?.addEventListener('click', () => moveFuncCarousel(1));
  els.funcList?.addEventListener('scroll', updateFuncCarousel, { passive: true });
  window.addEventListener('resize', updateFuncCarousel, { passive: true });

  function renderFuncList() {
    const selReal = resolveAlias(selectedScript);
    const previousScrollLeft = els.funcList.scrollLeft;
    els.funcList.innerHTML = '';
    rankedScriptOrder().forEach(k => {
      const info = scriptMeta[k];
      const running = isRunning && currentScript === k;
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'func-item' + (k === selReal ? ' sel' : '')
                     + (running ? ' running' : '');
      item.innerHTML = `<span class="fi-icon">${funcIconMarkup(k)}</span>`
        + `<span class="fi-name">${escHtml(info.label)}</span>`;
      item.addEventListener('click', () => selectOverviewScript(k));
      els.funcList.appendChild(item);
    });
    requestAnimationFrame(() => {
      els.funcList.scrollLeft = previousScrollLeft;
      updateFuncCarousel();
    });
  }

  // ── 概览中间：选中功能区（开始任务 / 强制关闭）──
  function renderFuncDetail() {
    const realKey = resolveAlias(selectedScript);
    const info = scriptMeta[realKey];
    if (!info) {
      els.funcDetail.innerHTML = '<div class="fd-empty">👈 左边点一个功能</div>';
      return;
    }
    const running = isRunning && currentScript === realKey;
    const busy = isRunning && !running;

    frontend.taskCard.renderOverview(els.funcDetail, {
      key: realKey,
      info,
      icon: funcIconMarkup(realKey),
      running,
      busy,
      params: paramsOf(selectedScript),
      onRun: () => taskRunner.run(selectedScript, paramsOf(selectedScript)),
      onStop: taskRunner.stop,
      onConfig: () => {
        document.querySelector('[data-tab="control"]').click();
        setTimeout(() => els.configDetail && els.configDetail.scrollTo({ top: 0, behavior: 'smooth' }), 0);
      },
    });
  }

  // ── 配置页：左侧分类导航 ──
  function renderConfigNav() {
    if (!els.configNav) return;
    els.configNav.innerHTML = '';
    if (els.configSelect) els.configSelect.innerHTML = '';
    CONFIG_GROUPS.forEach(g => {
      const items = g.items.filter(it => it.listKey || scriptMeta[resolveAlias(it.key)]);
      if (!items.length) return;
      if (els.configSelect) {
        const optionGroup = document.createElement('optgroup');
        optionGroup.label = g.label;
        items.forEach(it => {
          const option = document.createElement('option');
          option.value = it.key;
          option.textContent = `${it.label}${isRunning && currentScript === resolveAlias(it.key) ? ' · 运行中' : ''}`;
          option.selected = selectedScript === it.key;
          optionGroup.appendChild(option);
        });
        els.configSelect.appendChild(optionGroup);
      }
      const group = document.createElement('div');
      group.className = 'config-group';
      const title = document.createElement('div');
      title.className = 'config-group-title';
      title.textContent = g.label;
      group.appendChild(title);
      const list = document.createElement('div');
      list.className = 'config-items';
      items.forEach(it => {
        const real = resolveAlias(it.key);
        const running = isRunning && currentScript === real;
        const icon = it.listKey ? '📋' : (it.panel === 'schedule' ? '⏰' : funcIconMarkup(real));
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'config-item' + (selectedScript === it.key ? ' sel' : '')
                       + (running ? ' running' : '');
        item.innerHTML = `<span class="ci-icon">${icon}</span>`
          + `<span class="ci-name">${escHtml(it.label)}</span>`;
        item.addEventListener('click', () => {
          selectedScript = it.key;
          localStorage.setItem('maamaru_selected', selectedScript);
          renderConfigNav();
          renderConfigDetail();
          renderFuncList();
          renderFuncDetail();
        });
        list.appendChild(item);
      });
      group.appendChild(list);
      els.configNav.appendChild(group);
    });
  }

  els.configSelect?.addEventListener('change', event => {
    selectedScript = event.target.value;
    localStorage.setItem('maamaru_selected', selectedScript);
    renderConfigNav();
    renderConfigDetail();
    renderFuncList();
    renderFuncDetail();
  });

  // ── 配置页：右侧选中项配置 ──
  function renderConfigDetail() {
    if (!els.configDetail) return;
    const item = findConfigItem(selectedScript);
    els.configDetail.innerHTML = '';

    // 远征的手动安排与自动排班先各自收回隐藏坞。
    [els.commonExpPanel, els.schedPanel].forEach(panel => {
      if (panel && panel.parentElement === els.configDetail) els.schedDock.appendChild(panel);
    });

    // 自动排班是独立配置页，不提供“立即运行”按钮。
    if (item?.panel === 'schedule') {
      els.configDetail.appendChild(els.schedPanel);
      return;
    }

    if (item?.listKey) {
      frontend.listEditor.render(item, els.configDetail);
      return;
    }

    const key = resolveAlias(selectedScript);
    const info = scriptMeta[key];
    if (!info) {
      els.configDetail.innerHTML = '<div class="fd-empty">👈 左边选一项配置</div>';
      return;
    }

    const card = frontend.taskCard.createConfig({
      key,
      label: item ? item.label : info.label,
      info,
      icon: funcIconMarkup(key),
      running: isRunning && currentScript === key,
      busy: isRunning,
      form: paramForms[key],
      showSave: key !== 'expedition',
      onSave: saveBtn => saveSettings(saveBtn),
      onRun: () => taskRunner.run(selectedScript, paramsOf(selectedScript)),
    });
    els.configDetail.appendChild(card);
    finishConfigDetail(card, item, key);
  }

  function finishConfigDetail(card, item, key) {
    // 别名项：自动滚动并高亮对应字段（如南瓜监视名单）
    if (item && item.scrollTo) {
      const fieldEl = card.querySelector(`[data-param-key="${item.scrollTo}"]`);
      if (fieldEl) {
        const wrap = fieldEl.closest('.pf');
        if (wrap) {
          wrap.classList.add('cd-highlight');
          setTimeout(() => wrap.classList.remove('cd-highlight'), 1400);
          wrap.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
      }
    }

    // 手动远征页只展示“本次运行要派什么”，自动排班在独立入口中。
    if (key === 'expedition' && els.commonExpPanel) {
      els.configDetail.appendChild(els.commonExpPanel);
    }
  }

  // ── 渲染：配置页 + 概览功能列表 + 选中区 ──
  function renderScripts() {
    ensureParamForms();
    renderConfigNav();
    renderConfigDetail();
    renderFuncList();
    renderFuncDetail();
  }

  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  // ── 设置持久化：服务器端保存/加载 ──

  async function loadSavedSettings() {
    try {
      const r = await fetch('/api/saved-settings');
      const data = await r.json();
      const params = data.params || {};
      // 把服务器端保存的设置合并进 localStorage，renderParamField 会自动应用
      Object.entries(params).forEach(([name, vals]) => {
        if (vals && typeof vals === 'object') {
          localStorage.setItem('maamaru_params_' + name, JSON.stringify(vals));
        }
      });
      // 主题也是服务器说了算（客户端/手机/浏览器 localStorage 各玩各的，统一拉齐）
      if (frontend.theme.get(data.theme)) {
        applyTheme(data.theme);
      }
    } catch(e) {
      // 首加载失败无所谓，至少 localStorage 里的还在
    }
  }

  async function saveSettings(sourceButton = null) {
    // 从常驻参数表单收集当前参数
    const allParams = {};
    Object.values(paramForms).forEach(form => {
      const name = form.dataset.script;
      if (!name) return;
      allParams[name] = collectParams(form);
    });
    const payload = {params: allParams};
    try {
      const r = await fetch('/api/saved-settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      // 也写 localStorage 做缓存
      Object.entries(allParams).forEach(([k, v]) => {
        localStorage.setItem('maamaru_params_' + k, JSON.stringify(v));
      });
      if (data.ok) {
        frontend.feedback?.show('配置已保存', 'success');
        if (sourceButton) {
          const oldText = sourceButton.textContent;
          sourceButton.textContent = '✓ 已保存';
          setTimeout(() => { sourceButton.textContent = oldText; }, 2000);
        }
      }
    } catch(e) {
      frontend.feedback?.show('保存失败，面板后端没有响应', 'error');
    }
  }

  // ── Init ──
  async function init() {
    frontend.systemSettings?.init({
      nav: els.systemNav,
      prev: els.systemPrev,
      next: els.systemNext,
      detail: els.systemDetail,
    });
    // 先从服务器加载保存的设置（会在 render 之前写入 localStorage）
    await loadSavedSettings();
    await frontend.logViewer?.init();
    await loadScripts();
    frontend.expeditionSchedule?.load();
    frontend.dashboard?.init({ isRunning: () => isRunning });
    setInterval(taskRunner.poll, 3000);
  }

  init();
})();
