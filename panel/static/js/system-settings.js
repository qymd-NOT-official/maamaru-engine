/**
 * System settings tab: bot model, QQ, Telegram, and broadcast configuration.
 */
(function() {
  'use strict';

  const app = window.Maamaru;
  if (!app) throw new Error('Maamaru core must be loaded first');

  const $ = app.dom.one;
  const escape = app.dom.escape;
  const groups = [{ label: '系统配置', items: [
    { key: 'ai', label: '机器人模型', icon: '✦', desc: 'QQ 和 Telegram 消息机器人使用的 API 和模型' },
    { key: 'qq', label: 'QQ', icon: '🗨︎', desc: 'OneBot 协议端：连接状态、消息 API 与管理页面' },
    { key: 'telegram', label: 'Telegram', icon: '✈', desc: 'Telegram Bot：Token + 白名单' },
    { key: 'broadcast', label: '播报', icon: '◉', desc: '脚本状态变化时通知哪些渠道' },
  ]}];

  let nav;
  let prev;
  let next;
  let detail;
  let selected = localStorage.getItem('maamaru_system') || 'ai';
  let loaded = false;
  let cache = { ai: {}, bot: {} };

  async function load() {
    const [ai, bot] = await Promise.all([
      app.api.json('/api/agent-config'),
      app.api.json('/api/bot-config'),
    ]);
    cache = { ai, bot };
    loaded = true;
  }

  function input(form, id, label, value, placeholder, type = 'text', className = '') {
    form.appendChild(app.components.input({ id, label, value, placeholder, type, className }));
  }

  function checkbox(form, id, label, checked) {
    form.appendChild(app.components.checkbox({ id, label, checked }));
  }

  async function save() {
    const message = $('#sys-save-msg');
    if (message) message.textContent = '正在保存…';
    let ok = false;
    let extra = '';
    try {
      if (selected === 'ai') {
        ok = (await app.api.post('/api/agent-config', {
          api_key: $('#sys-api-key').value.trim(),
          base_url: $('#sys-api-url').value.trim(),
          model: $('#sys-api-model').value.trim(),
        })).ok;
      } else {
        const body = {};
        const bot = cache.bot || {};
        if (selected === 'qq') {
          const enabled = $('#sys-qq-enabled').checked;
          body.enabled = enabled || (bot.platform === 'telegram' && bot.enabled);
          body.platform = enabled ? 'qq' : (bot.platform || 'qq');
          body.qq = {
            enabled,
            provider: $('#sys-qq-provider').value,
            snowluma_http: $('#sys-qq-api').value.trim(),
            snowluma_gui_http: $('#sys-qq-gui').value.trim(),
            admin_qq: $('#sys-qq-admin').value.trim(),
          };
        } else if (selected === 'telegram') {
          const enabled = $('#sys-tg-enabled').checked;
          body.enabled = enabled || !!bot.qq?.enabled;
          body.platform = enabled ? 'telegram' : (bot.qq?.enabled ? 'qq' : 'telegram');
          body.telegram = {
            token: $('#sys-tg-token').value.trim(),
            allowed_users: $('#sys-tg-users').value.trim(),
          };
        } else {
          body.broadcast = {
            qq: $('#sys-bc-qq').checked,
            ntfy: $('#sys-bc-ntfy').checked,
          };
        }
        const data = await app.api.post('/api/bot-config', body);
        ok = data.ok;
        if (data.tg_reload_msg) extra = data.tg_reload_msg;
        if (selected === 'qq' && data.qq_restart_required) {
          extra += (extra ? ' ' : '') + 'QQ 需重启まあ丸才生效。';
        }
      }
    } catch (_) {
      extra = '保存请求失败';
    }

    if (message) {
      message.textContent = ok
        ? '✓ 保存成功' + (extra ? ' — ' + extra : '')
        : '✗ 保存失败，检查后端连接';
    }
    app.feedback?.show(ok ? '系统设置已保存' : '系统设置保存失败', ok ? 'success' : 'error');
    if ($('#sys-api-key')) $('#sys-api-key').value = '';
    if ($('#sys-tg-token')) $('#sys-tg-token').value = '';
    if (ok && selected === 'qq') setTimeout(() => app.system.qq.refresh(), 200);
    setTimeout(() => { if (message) message.textContent = ''; }, 4000);
  }

  function renderNav() {
    if (!nav) return;
    const previousScrollLeft = nav.scrollLeft;
    nav.innerHTML = '';
    groups.forEach(groupInfo => {
      const group = document.createElement('div');
      group.className = 'config-group';
      group.innerHTML = `<div class="config-group-title">${groupInfo.label}</div>`;
      const list = document.createElement('div');
      list.className = 'config-items';
      groupInfo.items.forEach(info => {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'config-item' + (selected === info.key ? ' sel' : '');
        item.innerHTML = `<span class="ci-icon">${info.icon}</span><span class="ci-name">${info.label}</span>`;
        item.addEventListener('click', () => {
          selected = info.key;
          localStorage.setItem('maamaru_system', selected);
          renderNav();
          renderDetail();
        });
        list.appendChild(item);
      });
      group.appendChild(list);
      nav.appendChild(group);
    });
    requestAnimationFrame(() => {
      nav.scrollLeft = previousScrollLeft;
      const selectedItem = nav.querySelector('.config-item.sel');
      if (selectedItem) {
        const navRect = nav.getBoundingClientRect();
        const itemRect = selectedItem.getBoundingClientRect();
        if (itemRect.left < navRect.left) {
          nav.scrollLeft += itemRect.left - navRect.left;
        } else if (itemRect.right > navRect.right) {
          nav.scrollLeft += itemRect.right - navRect.right;
        }
      }
      updateCarousel();
    });
  }

  function updateCarousel() {
    if (!nav || !prev || !next) return;
    const maxScroll = Math.max(0, nav.scrollWidth - nav.clientWidth);
    prev.disabled = nav.scrollLeft <= 2;
    next.disabled = nav.scrollLeft >= maxScroll - 2;
  }

  function moveCarousel(direction) {
    const distance = Math.max(120, Math.round(nav.clientWidth * 0.72));
    nav.scrollBy({ left: direction * distance, behavior: 'smooth' });
  }

  function addHint(form, text) {
    const hint = document.createElement('div');
    hint.className = 'sys-hint';
    hint.textContent = text;
    form.appendChild(hint);
  }

  function renderDetail() {
    if (!detail) return;
    const item = groups.flatMap(group => group.items).find(info => info.key === selected);
    if (!item) {
      detail.innerHTML = '<div class="fd-empty">👈 左边选一项</div>';
      return;
    }
    const ai = cache.ai || {};
    const bot = cache.bot || {};
    bot.qq = bot.qq || {};
    bot.telegram = bot.telegram || {};
    bot.broadcast = bot.broadcast || {};

    const card = document.createElement('div');
    card.className = 'config-detail-card sys-card';
    card.innerHTML = `<div class="cd-head"><div class="cd-title"><span>${item.icon}</span> ${item.label}</div></div>`
      + `<div class="cd-desc">${escape(item.desc)}</div>`;
    const form = document.createElement('div');
    form.className = 'sys-form';

    if (item.key === 'ai') {
      const placeholder = ai.has_key ? `已配置（${ai.api_key_masked}），留空不改` : 'sk-...';
      input(form, 'sys-api-key', 'API Key', '', placeholder, 'password', 'pf-prompt');
      input(form, 'sys-api-url', 'API 地址', ai.base_url || '', 'https://api.openai.com/v1');
      input(form, 'sys-api-model', '模型', ai.model || '', 'gpt-4o-mini');
    } else if (item.key === 'qq') {
      const status = document.createElement('div');
      status.id = 'qq-status-box';
      status.className = 'qq-status-card checking';
      form.appendChild(status);
      const provider = document.createElement('div');
      provider.className = 'pf';
      provider.innerHTML = `<label>协议端类型</label><select id="sys-qq-provider">
        <option value="napcat" ${bot.qq.provider === 'napcat' ? 'selected' : ''}>NapCat（推荐）</option>
        <option value="snowluma" ${bot.qq.provider === 'snowluma' ? 'selected' : ''}>SnowLuma（实验性）</option>
        <option value="custom" ${bot.qq.provider === 'custom' ? 'selected' : ''}>其他 OneBot 实现</option>
      </select>`;
      form.appendChild(provider);
      checkbox(form, 'sys-qq-enabled', '启用 QQ 协议端', bot.qq.enabled);
      input(form, 'sys-qq-api', 'OneBot HTTP API（消息发送）', bot.qq.snowluma_http || '', 'http://127.0.0.1:3000');
      input(form, 'sys-qq-gui', '协议端管理页（WebUI）', bot.qq.snowluma_gui_http || '', 'http://127.0.0.1:6099');
      input(form, 'sys-qq-admin', '管理员 QQ', (bot.qq.admin_qq || []).join(', '), 'QQ号，逗号分隔（留空=不限制）');
      addHint(form, '启用只代表まあ丸会接收 QQ 消息，并不等于协议端已经安装。请在协议端中把 HTTP 上报地址设为 http://127.0.0.1:8080/onebot/webhook。QQ 配置修改后需重启まあ丸。');
      setTimeout(() => app.system.qq.refresh(), 0);
    } else if (item.key === 'telegram') {
      checkbox(form, 'sys-tg-enabled', '启用 Telegram Bot', bot.platform === 'telegram' && bot.enabled);
      input(form, 'sys-tg-token', 'Bot Token', '', bot.telegram.has_token ? `已配置（${bot.telegram.token_masked}），留空不改` : 'token', 'password');
      input(form, 'sys-tg-users', '允许的用户 ID', (bot.telegram.allowed_users || []).join(', '), '用户 ID，逗号分隔（留空=不限制）');
      addHint(form, 'Token 留空不改变，修改后 Telegram 自动热重启。');
    } else {
      checkbox(form, 'sys-bc-qq', 'QQ 渠道播报（由 QQ 协议端转发）', bot.broadcast.qq);
      checkbox(form, 'sys-bc-ntfy', 'ntfy 推送', bot.broadcast.ntfy);
      addHint(form, '脚本运行状态变化时自动通知对应渠道。');
    }

    const saveRow = document.createElement('div');
    saveRow.className = 'sys-save-row';
    saveRow.innerHTML = '<span id="sys-save-msg" class="sys-save-msg"></span>';
    const button = document.createElement('button');
    button.className = 's-run with-ui-icon ui-check';
    button.textContent = '保存到配置';
    button.addEventListener('click', save);
    saveRow.appendChild(button);
    form.appendChild(saveRow);
    card.appendChild(form);
    detail.replaceChildren(card);
    app.components.enhanceSelects?.(card);
  }

  async function render() {
    renderNav();
    if (!loaded) {
      try { await load(); } catch (_) {}
    }
    renderDetail();
  }

  function init(options) {
    nav = options.nav;
    prev = options.prev;
    next = options.next;
    detail = options.detail;
    prev?.addEventListener('click', () => moveCarousel(-1));
    next?.addEventListener('click', () => moveCarousel(1));
    nav?.addEventListener('scroll', updateCarousel, { passive: true });
    window.addEventListener('resize', updateCarousel, { passive: true });
    const tab = document.querySelector('[data-tab="system"]');
    tab?.addEventListener('click', () => setTimeout(render, 0));
    if (document.querySelector('.tab.active')?.dataset.tab === 'system') setTimeout(render, 0);
  }

  app.systemSettings = { init, render };
})();
