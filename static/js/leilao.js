'use strict';

const BRL = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL', maximumFractionDigits: 0 });
const BRL2 = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const NUM = new Intl.NumberFormat('pt-BR');

const state = {
  config: null,
  report: null,
  labels: {},
  conditions: [],
  makes: [],
  lots: [],
  whatsapp: null,
  scanning: false,
};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function money(value, strong = false) {
  if (value === null || value === undefined) return '<span class="money na">—</span>';
  const cls = strong ? 'money strong' : 'money';
  return `<span class="${cls}">${BRL.format(value)}</span>`;
}

function pct(value) {
  if (value === null || value === undefined) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
}

/* ------------------------------------------------------------------ scan */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${response.status}: ${text.slice(0, 300)}`);
  }
  return response.json();
}

async function loadStatus() {
  const data = await api('/api/leilao/status');
  state.config = data.config;
  state.report = data.last_report;
  state.labels = data.labels || {};
  state.conditions = (data.config.filters.conditions || []).slice();
  state.makes = (data.config.filters.makes || []).slice();

  fillCosts(data.config);
  fillWhatsapp(data.whatsapp);
  renderFilterOptions();
  renderSources();
  renderStats();
  renderScanLine();
}

function fillCosts(config) {
  const costs = config.costs || {};
  $('costFee').value = costs.auction_fee_percent ?? 10;
  $('costDespachante').value = costs.despachante_brl ?? 800;
  $('costTransfer').value = costs.transfer_brl ?? 350;
  $('costTow').value = costs.tow_brl ?? 400;
  $('costRepairs').value = costs.repairs_estimate_brl ?? 1500;
  $('costBuffer').value = costs.imprevistos_percent ?? 10;
  $('minBid').value = config.filters.min_bid_price_brl ?? 0;
  $('maxBid').value = config.filters.max_bid_price_brl ?? 300000;
  $('minMargin').value = config.valuation.min_margin_percent ?? 30;
  $('includeConserved').checked = !!(config.scraper_options || {}).superbid_include_conserved_fleet;
}

function renderFilterOptions() {
  const conditionBox = $('conditionFilter');
  conditionBox.innerHTML = state.conditions
    .map((condition) => `
      <label><input type="checkbox" class="condition" value="${esc(condition)}" checked> ${esc(condition)}</label>
    `)
    .join('');

  const makeBox = $('makeFilter');
  makeBox.innerHTML = state.makes
    .map((make) => `
      <label><input type="checkbox" class="make" value="${esc(make)}" checked> ${esc(make.replace(/^./, (c) => c.toUpperCase()))}</label>
    `)
    .join('');

  document.querySelectorAll('#conditionFilter input, #makeFilter input').forEach((input) => {
    input.addEventListener('change', loadLots);
  });
}

function statusMeta(status, note) {
  if (status === 'ok') return { cls: 'ok', label: 'Live' };
  if (status === 'limited') return { cls: 'warn', label: 'Limited' };
  if (status === 'blocked') return { cls: 'err', label: 'Blocked' };
  if (status === 'error') return { cls: 'err', label: 'Error' };
  if (status === 'empty') return { cls: 'warn', label: 'No matches' };
  return { cls: 'muted', label: note ? 'Blocked' : 'Not scanned' };
}

function renderSources() {
  const box = $('sources');
  const sites = ['copart', 'superbid', 'sodre_santoro', 'leilao_master'];
  const bySite = {};
  for (const source of (state.report?.sources || [])) bySite[source.site] = source;

  box.innerHTML = sites.map((site) => {
    const source = bySite[site];
    const label = source?.label || state.labels[site] || site;
    if (!source) {
      return `
        <div class="source">
          <div class="source-head">
            <span class="source-name">${esc(label)}</span>
            <span class="pill muted">not scanned</span>
          </div>
        </div>`;
    }
    const meta = statusMeta(source.status, source.note);
    const note = source.error || source.note || '';
    const counts = source.status === 'blocked' || source.status === 'error'
      ? ''
      : `<div class="source-counts">
           <span><b>${NUM.format(source.found)}</b> found</span>
           <span><b>${NUM.format(source.kept)}</b> kept</span>
           <span><b>${NUM.format(source.viable)}</b> viable</span>
         </div>`;
    return `
      <div class="source">
        <div class="source-head">
          <span class="source-name">${esc(label)}</span>
          <span class="pill ${meta.cls}">${meta.label}</span>
        </div>
        ${counts}
        ${note ? `<div class="source-note">${esc(note)}</div>` : ''}
        <div class="source-note">${source.seconds ? `${source.seconds}s` : ''}</div>
      </div>`;
  }).join('');
}

function renderStats() {
  const box = $('stats');
  if (!state.report) {
    box.innerHTML = '';
    return;
  }
  const report = state.report;
  const items = [
    ['Scraped', NUM.format(report.scraped ?? 0)],
    ['Matched filters', NUM.format(report.matched ?? 0)],
    ['Valued (FIPE)', NUM.format(report.valued ?? 0)],
    ['Profitable', NUM.format(report.viable ?? 0), 'ok'],
    ['Scan time', `${report.duration_seconds ?? 0}s`],
  ];
  box.innerHTML = items.map(([key, value, cls]) => `
    <div class="stat">
      <div class="k">${key}</div>
      <div class="v ${cls || ''}">${value}</div>
    </div>
  `).join('');
}

function renderScanLine() {
  const line = $('scanStatus');
  if (!state.report) {
    line.textContent = 'No scan yet — press “Search now”.';
    return;
  }
  const report = state.report;
  const finished = report.finished_at ? new Date(report.finished_at.replace(' ', 'T')) : null;
  const when = finished
    ? finished.toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'short' })
    : 'unknown';
  const blocked = (report.sources || []).filter((s) => s.status === 'blocked' || s.status === 'error');
  line.innerHTML = `Last scan <strong>${esc(when)}</strong> · <strong>${NUM.format(report.matched ?? 0)}</strong> lots matched · <strong>${NUM.format(report.viable ?? 0)}</strong> above target margin`
    + (blocked.length ? ` · ${blocked.length} source(s) unavailable` : '');
}

/* ------------------------------------------------------------------ lots */

function filtersQuery() {
  const params = new URLSearchParams();
  params.set('only_worth_it', $('onlyViable').checked ? 'true' : 'false');
  const minMargin = parseFloat($('minMargin').value);
  if (!Number.isNaN(minMargin)) params.set('min_margin', String(minMargin));
  const minBid = parseFloat($('minBid').value);
  if (!Number.isNaN(minBid) && minBid > 0) params.set('min_bid', String(minBid));
  const maxBid = parseFloat($('maxBid').value);
  if (!Number.isNaN(maxBid)) params.set('max_bid', String(maxBid));

  const conditions = [...document.querySelectorAll('#conditionFilter input:checked')].map((i) => i.value);
  if (conditions.length && conditions.length !== state.conditions.length) {
    params.set('conditions', conditions.join(','));
  }
  const makes = [...document.querySelectorAll('#makeFilter input:checked')].map((i) => i.value);
  if (makes.length && makes.length !== state.makes.length) {
    params.set('makes', makes.join(','));
  }
  return params;
}

async function loadLots() {
  const params = filtersQuery();
  const lots = await api(`/api/leilao/lots?${params.toString()}`);
  state.lots = lots;
  renderLots(lots);
  updateCsvLink(params);
}

function updateCsvLink(params) {
  const csvParams = new URLSearchParams();
  csvParams.set('only_worth_it', params.get('only_worth_it') || 'true');
  const minMargin = params.get('min_margin');
  if (minMargin) csvParams.set('min_margin', minMargin);
  const minBid = params.get('min_bid');
  if (minBid) csvParams.set('min_bid', minBid);
  const maxBid = params.get('max_bid');
  if (maxBid) csvParams.set('max_bid', maxBid);
  $('btnCsv').href = `/api/leilao/lots.csv?${csvParams.toString()}`;
}

function endLabel(value) {
  if (!value) return { text: 'available', soon: false };
  const end = new Date(value.replace(' ', 'T'));
  const diff = end.getTime() - Date.now();
  if (Number.isNaN(diff)) return { text: value, soon: false };
  if (diff <= 0) return { text: 'ended', soon: false };
  const minutes = Math.floor(diff / 60000);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);
  if (days >= 1) return { text: `in ${days}d ${hours % 24}h`, soon: days < 1 };
  if (hours >= 1) return { text: `in ${hours}h ${minutes % 60}m`, soon: true };
  return { text: `in ${minutes}m`, soon: true };
}

function renderLots(lots) {
  const body = $('lotsBody');
  const empty = $('empty');
  body.innerHTML = '';

  if (!lots.length) {
    empty.classList.remove('hidden');
    if (!state.report) {
      empty.innerHTML = '<b>Nothing scanned yet</b>Press “Search now” to run a live scan across the auction sources.';
    } else if ($('onlyViable').checked) {
      empty.innerHTML = '<b>No lots clear your margin threshold</b>Lower the minimum ROI, widen the bid range, or uncheck “Only profitable lots” to inspect everything that matched.';
    } else {
      empty.innerHTML = '<b>No live lots match these filters</b>Loosen the filters or run a new scan.';
    }
    return;
  }
  empty.classList.add('hidden');

  body.innerHTML = lots.map((lot) => {
    const roi = lot.margin_percent;
    const roiClass = roi === null || roi === undefined ? 'cool' : roi >= 60 ? 'hot' : roi >= 30 ? 'hot' : roi >= 0 ? 'cool' : 'neg';
    const end = endLabel(lot.end_date);
    const title = lot.title || [lot.make, lot.model].filter(Boolean).join(' ');
    const year = lot.year || lot.year_manufacture;
    const meta = [lot.make ? lot.make.toUpperCase() : '', year || '', lot.km ? `${NUM.format(lot.km)} km` : '']
      .filter(Boolean).join(' · ');
    const location = [lot.city, lot.state].filter(Boolean).join(', ') || '—';
    const sourceTag = lot.condition_source === 'source' ? '<span class="tag source">source</span>' : '';
    const valuation = lot.valuation_model
      ? `<div class="vehicle"><div class="meta">FIPE: ${esc(lot.valuation_model)} ${lot.valuation_year || ''}</div></div>`
      : '';
    const netClass = (lot.net_profit ?? 0) > 0 ? 'pos' : 'neg';
    return `
      <tr>
        <td>
          <div class="vehicle">
            <div class="title">${esc(title)}</div>
            <div class="meta">${esc(meta)}</div>
            ${valuation}
          </div>
        </td>
        <td>
          <div class="src-name">${esc(lot.site_label || lot.site)}</div>
          <div class="src-sub">${esc(lot.condition || '')}</div>
          ${sourceTag}
        </td>
        <td class="num">${money(lot.bid_price, true)}</td>
        <td class="num">${money(lot.fipe_price)}</td>
        <td class="num">${money(lot.total_costs)}</td>
        <td class="num">${lot.max_bid ? `<span class="money">${BRL2.format(lot.max_bid)}</span>` : '—'}</td>
        <td class="num">${lot.net_profit === null || lot.net_profit === undefined ? '—' : `<span class="money ${netClass}">${BRL2.format(lot.net_profit)}</span>`}</td>
        <td class="num">
          <span class="roi ${roiClass}">${pct(roi)}</span>
          ${lot.margin_percent > 60 ? '<span class="above">above target</span>' : ''}
        </td>
        <td><span class="ends ${end.soon ? 'soon' : ''}">${esc(end.text)}</span></td>
        <td><span class="ends">${esc(location)}</span></td>
        <td>${lot.url ? `<a class="open" href="${esc(lot.url)}" target="_blank" rel="noopener">Open ↗</a>` : ''}</td>
      </tr>`;
  }).join('');
}

/* ---------------------------------------------------------------- whatsapp */

const WA_SECRETS = { apikey: 'waApikey', meta_token: 'waMetaToken', twilio_auth_token: 'waTwilioToken' };
const WA_LABELS = { bridge: 'WhatsApp Web', callmebot: 'CallMeBot', meta: 'Meta Cloud API', twilio: 'Twilio' };

let bridgeTimer = null;

function stopBridgePolling() {
  if (bridgeTimer) {
    clearTimeout(bridgeTimer);
    bridgeTimer = null;
  }
}

function bridgeStateText(status) {
  if (!status.running) {
    if (status.installed === false) {
      return 'Bridge not installed. Run npm install inside agents/leilao/whatsapp_bridge.';
    }
    if (status.node === false) return 'Node.js was not found on PATH.';
    return 'Bridge is not running. Press Connect to start it.';
  }
  if (status.connected) {
    const number = status.me && status.me.number ? `+${status.me.number}` : 'your phone';
    return `Linked to ${number}. Messages arrive in “Message yourself”.`;
  }
  if (status.qr) return 'Scan the QR code: WhatsApp → Settings → Linked devices → Link a device.';
  if (status.pairing_code) return 'Pairing code ready — enter it on your phone under Linked devices.';
  return status.error || status.last_error || 'Starting the bridge…';
}

async function refreshBridge({ start = false } = {}) {
  stopBridgePolling();
  let status = {};
  try {
    if (start) {
      const data = await api('/api/leilao/whatsapp/bridge/start', { method: 'POST' });
      status = data.status || {};
      $('waBridgeState').textContent = data.ok === false
        ? (data.detail || 'Could not start the bridge.')
        : bridgeStateText(status);
    } else {
      status = await api('/api/leilao/whatsapp/bridge/status');
      $('waBridgeState').textContent = bridgeStateText(status);
    }
  } catch (error) {
    $('waBridgeState').textContent = `Bridge error: ${error.message}`;
  }

  const qr = status.qr;
  const img = $('waQr');
  if (qr && !status.connected) {
    img.src = qr;
    img.classList.remove('hidden');
  } else {
    img.classList.add('hidden');
    img.removeAttribute('src');
  }

  if (status.connected) {
    $('waPairCode').textContent = '';
  } else if (status.pairing_code) {
    $('waPairCode').textContent =
      `Code: ${status.pairing_code} — WhatsApp → Linked devices → Link with phone number.`;
  }

  if ($('waProvider').value === 'bridge') {
    bridgeTimer = setTimeout(() => refreshBridge(), status.connected ? 15000 : 3000);
  }
}

async function pairBridge() {
  const phone = $('waPairPhone').value.trim();
  const output = $('waPairCode');
  if (!phone) {
    output.textContent = 'Enter your number with the country code, e.g. +5511999999999.';
    return;
  }
  const button = $('btnWaPair');
  button.disabled = true;
  output.textContent = 'Requesting a pairing code…';
  try {
    const data = await api('/api/leilao/whatsapp/bridge/pair', {
      method: 'POST',
      body: JSON.stringify({ phone }),
    });
    if (data.ok && data.code) {
      output.textContent =
        `Code: ${data.code} — WhatsApp → Linked devices → Link with phone number.`;
      refreshBridge();
    } else {
      output.textContent = `Pairing failed: ${data.error || 'unknown error'}`;
    }
  } catch (error) {
    output.textContent = `Pairing failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function unlinkBridge() {
  const button = $('btnWaUnlink');
  button.disabled = true;
  try {
    await api('/api/leilao/whatsapp/bridge/logout', { method: 'POST' });
    $('waBridgeState').textContent = 'Unlinked. Starting a fresh session…';
    await refreshBridge({ start: true });
  } catch (error) {
    $('waBridgeState').textContent = `Unlink failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

function renderProviderFields(provider) {
  document.querySelectorAll('[data-provider]').forEach((el) => {
    el.classList.toggle('hidden', el.dataset.provider !== provider);
  });
  const isBridge = provider === 'bridge';
  $('waBridgePanel').classList.toggle('hidden', !isBridge);
  $('waCreds').classList.toggle('hidden', isBridge);
  if (isBridge) {
    refreshBridge({ start: true });
  } else {
    stopBridgePolling();
  }
}

function fillWhatsapp(info) {
  state.whatsapp = info || null;
  const settings = (info && info.settings) || {};
  const provider = settings.provider || 'bridge';

  $('waEnabled').checked = !!settings.enabled;
  $('waProvider').value = provider;
  $('waPhone').value = settings.phone || '';
  $('waMode').value = settings.mode || 'ending_today';
  $('waTime').value = settings.daily_time || '18:00';
  $('waBridgeRecipient').value = settings.bridge_recipient || '';
  $('waMax').value = settings.max_deals ?? 8;
  $('waMinBid').value = settings.min_bid ?? 0;
  $('waScanFirst').checked = settings.scan_before_digest !== false;
  $('waMetaPhoneId').value = settings.meta_phone_number_id || '';
  $('waTwilioSid').value = settings.twilio_account_sid || '';
  $('waTwilioFrom').value = settings.twilio_from || '';

  for (const [key, id] of Object.entries(WA_SECRETS)) {
    $(id).value = '';
    $(id).placeholder = settings[`has_${key}`] ? 'saved — leave blank to keep' : '';
  }

  renderProviderFields(provider);
  renderWhatsappStatus();
}

function renderWhatsappStatus() {
  const info = state.whatsapp || {};
  const settings = info.settings || {};
  let text = 'Not configured yet.';

  if (info.configured) {
    const label = WA_LABELS[settings.provider] || settings.provider;
    const sentToday = info.sent_today
      ? `Lots sent today: ${NUM.format(info.sent_today)}.`
      : 'No lots sent today yet.';
    text = settings.provider === 'bridge'
      ? `Delivery: ${label}. ${sentToday}`
      : `Ready via ${label}. ${sentToday}`;
    if (settings.enabled) text += ` Next send at ${settings.daily_time || '18:00'}.`;
  } else if ((info.missing || []).length) {
    text = `Missing: ${info.missing.join(', ')}.`;
  }

  const last = info.last;
  if (last && last.at) {
    const outcome = last.sent ? `sent ${last.deals ?? 0} deal(s)` : (last.skipped || last.error || 'not sent');
    text += ` Last attempt ${last.at}: ${outcome}.`;
  }
  $('waStatus').textContent = text;
}

function whatsappPayload() {
  const payload = {
    enabled: $('waEnabled').checked,
    provider: $('waProvider').value,
    phone: $('waPhone').value.trim(),
    mode: $('waMode').value,
    daily_time: $('waTime').value || '18:00',
    max_deals: parseInt($('waMax').value, 10) || 8,
    min_bid: parseFloat($('waMinBid').value) || 0,
    bridge_recipient: $('waBridgeRecipient').value.trim(),
    scan_before_digest: $('waScanFirst').checked,
    meta_phone_number_id: $('waMetaPhoneId').value.trim(),
    twilio_account_sid: $('waTwilioSid').value.trim(),
    twilio_from: $('waTwilioFrom').value.trim(),
  };
  for (const [key, id] of Object.entries(WA_SECRETS)) {
    const value = $(id).value.trim();
    if (value) payload[key] = value;
  }
  return payload;
}

async function saveWhatsapp({ quiet = false } = {}) {
  if ($('waProvider').value === 'bridge') {
    const recipient = $('waBridgeRecipient').value.trim();
    if (recipient && recipient.replace(/\D/g, '').length < 10) {
      $('waStatus').textContent =
        'Send to: enter the full number with country code, e.g. +5511999999999.';
      return false;
    }
  }
  await api('/api/leilao/config', {
    method: 'POST',
    body: JSON.stringify({ alerts: { whatsapp: whatsappPayload() } }),
  });
  await loadStatus();
  if (!quiet) $('waStatus').textContent = `Saved. ${$('waStatus').textContent}`;
  return true;
}

async function testWhatsapp() {
  const button = $('btnWaTest');
  button.disabled = true;
  try {
    if (!(await saveWhatsapp({ quiet: true }))) return;
    const result = await api('/api/leilao/whatsapp/test', { method: 'POST' });
    const target = result.recipient || result.phone || 'yourself';
    $('waStatus').textContent = result.sent
      ? `Test message sent to ${target} via ${result.provider}.`
      : `Test failed: ${result.detail || (result.missing || []).join(', ')}`;
  } catch (error) {
    $('waStatus').textContent = `Test failed: ${error.message}`;
  } finally {
    button.disabled = false;
  }
}

async function sendWhatsappDigest() {
  const button = $('btnWaDigest');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = 'Sending…';
  try {
    if (!(await saveWhatsapp({ quiet: true }))) return;
    const result = await api('/api/leilao/whatsapp/digest', {
      method: 'POST',
      body: JSON.stringify({ force: true }),
    });
    let message;
    if (result.sent) {
      const photos = result.images ? ` with ${result.images} photo(s)` : '';
      const target = result.recipient || 'yourself';
      message = `Sent ${result.deals} deal(s)${photos} in ${result.messages} message(s) to ${target} via ${result.provider}.`;
    } else {
      const detail = result.error || result.skipped || (result.results || []).map((r) => r.detail).join('; ');
      message = `Nothing sent: ${detail || 'no deals matched'}.`;
    }
    await loadStatus();
    $('waStatus').textContent = message;
  } catch (error) {
    $('waStatus').textContent = `Send failed: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ---------------------------------------------------------------- actions */

async function runSearch() {
  if (state.scanning) return;
  state.scanning = true;
  const button = $('btnSearch');
  button.disabled = true;
  button.textContent = 'Scanning…';
  $('scanSpinner').classList.remove('hidden');
  $('scanStatus').textContent = 'Contacting auction sources, filtering lots and querying FIPE…';

  try {
    const report = await api('/api/leilao/search', { method: 'POST' });
    state.report = report;
    renderSources();
    renderStats();
    renderScanLine();
    await loadLots();
  } catch (error) {
    $('scanStatus').textContent = `Scan failed: ${error.message}`;
  } finally {
    state.scanning = false;
    button.disabled = false;
    button.textContent = 'Search now';
    $('scanSpinner').classList.add('hidden');
  }
}

async function recalc() {
  const payload = {
    costs: {
      auction_fee_percent: parseFloat($('costFee').value) || 0,
      despachante_brl: parseFloat($('costDespachante').value) || 0,
      transfer_brl: parseFloat($('costTransfer').value) || 0,
      tow_brl: parseFloat($('costTow').value) || 0,
      repairs_estimate_brl: parseFloat($('costRepairs').value) || 0,
      imprevistos_percent: parseFloat($('costBuffer').value) || 0,
    },
    valuation: { min_margin_percent: parseFloat($('minMargin').value) || 0 },
    filters: {
      min_bid_price_brl: parseFloat($('minBid').value) || 0,
      max_bid_price_brl: parseFloat($('maxBid').value) || null,
    },
  };

  const button = $('btnRecalc');
  button.disabled = true;
  const original = button.textContent;
  button.textContent = 'Recalculating…';
  try {
    await api('/api/leilao/config', { method: 'POST', body: JSON.stringify(payload) });
    const summary = await api('/api/leilao/recalc', { method: 'POST' });
    const report = state.report || {};
    report.viable = summary.viable;
    report.valued = summary.valued;
    state.report = report;
    renderStats();
    await loadLots();
    $('scanStatus').textContent = `Recalculated ${NUM.format(summary.recalculated)} lots with the new costs — ${NUM.format(summary.viable)} clear the margin target.`;
  } catch (error) {
    $('scanStatus').textContent = `Recalculation failed: ${error.message}`;
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

/* ------------------------------------------------------------------- init */

function bind() {
  $('btnSearch').addEventListener('click', runSearch);
  $('btnRecalc').addEventListener('click', recalc);
  $('waProvider').addEventListener('change', (event) => renderProviderFields(event.target.value));
  $('btnWaSave').addEventListener('click', () => saveWhatsapp());
  $('btnWaTest').addEventListener('click', testWhatsapp);
  $('btnWaConnect').addEventListener('click', () => refreshBridge({ start: true }));
  $('btnWaUnlink').addEventListener('click', unlinkBridge);
  $('btnWaPair').addEventListener('click', pairBridge);
  $('btnWaDigest').addEventListener('click', sendWhatsappDigest);
  $('onlyViable').addEventListener('change', loadLots);
  $('minMargin').addEventListener('change', loadLots);
  $('minBid').addEventListener('change', loadLots);
  $('maxBid').addEventListener('change', loadLots);
  $('includeConserved').addEventListener('change', async (event) => {
    const enabled = event.target.checked;
    await api('/api/leilao/config', {
      method: 'POST',
      body: JSON.stringify({ scraper_options: { superbid_include_conserved_fleet: enabled } }),
    });
    $('scanStatus').textContent = enabled
      ? 'Conserved-fleet lots will be included on the next scan.'
      : 'Conserved-fleet lots will be skipped on the next scan.';
  });
}

(async function init() {
  bind();
  try {
    await loadStatus();
    await loadLots();
  } catch (error) {
    $('scanStatus').textContent = `Could not load scanner state: ${error.message}`;
  }
})();
