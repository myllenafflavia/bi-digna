// Coleta o desempenho das contas de anúncio do Meta (Ads Insights API) e grava data/meta.json.
// Roda no GitHub Actions. O token vem do segredo META_ACCESS_TOKEN: vai só no cabeçalho
// Authorization, nunca em URL, arquivo, commit ou log.
import fs from 'node:fs/promises';
import path from 'node:path';

const TOKEN = process.env.META_ACCESS_TOKEN || '';
const VERSION = process.env.META_API_VERSION || 'v26.0';
const BASE = (process.env.META_BASE || 'https://graph.facebook.com').replace(/\/$/, '') + '/' + VERSION;
const ALLOWED = (process.env.META_AD_ACCOUNTS || '').split(',').map(s => s.trim().replace(/^act_/, '')).filter(Boolean);
const START = process.env.META_START || '2025-01-01';
const REPROC = Number(process.env.META_REPROCESS_DAYS || 7);
const PAUSE = Number(process.env.META_PAUSE_MS || 300);
const OUT = process.env.OUT_DIR || 'data';
const STORE = path.join(OUT, 'meta');
const WARN_DAYS = 15;
const MAX_MS = Number(process.env.META_MAX_MINUTES || 35) * 60e3; // tempo máximo da carga histórica por rodada
const T_START = Date.now();

const sleep = ms => new Promise(r => setTimeout(r, ms));
const redact = s => String(s).split(TOKEN || '\u0000').join('***').replace(/access_token=[^&\s"]+/gi, 'access_token=***');
const log = (...a) => console.log(new Date().toISOString(), ...a.map(redact));

// tipos de ação guardados (código curto)
const ACT = { 'lead': 'ld', 'onsite_conversion.lead_grouped': 'lg', 'offsite_conversion.fb_pixel_lead': 'pl', 'onsite_web_lead': 'wl',
  'onsite_conversion.messaging_conversation_started_7d': 'mc', 'landing_page_view': 'lp', 'link_click': 'lk' };
const FIELDS_AD = 'account_id,account_name,account_currency,campaign_id,campaign_name,adset_id,adset_name,ad_id,ad_name,date_start,date_stop,spend,impressions,reach,frequency,clicks,inline_link_clicks,ctr,cpc,cpm,actions,cost_per_action_type';
const LEVEL_ID = { account: 'account_id', campaign: 'campaign_id', adset: 'adset_id', ad: 'ad_id' };

class ApiError extends Error { constructor(m, code, sub, kind) { super(m); this.code = code; this.sub = sub; this.kind = kind; } }

let usageWait = 0;
function readUsage(h) {
  try {
    const bu = h.get('x-business-use-case-usage');
    if (bu) for (const arr of Object.values(JSON.parse(bu))) for (const u of arr) {
      const pct = Math.max(u.call_count || 0, u.total_cputime || 0, u.total_time || 0);
      if (u.estimated_time_to_regain_access > 0) usageWait = Math.max(usageWait, Math.min(15, u.estimated_time_to_regain_access) * 60e3);
      else if (pct >= 85) usageWait = Math.max(usageWait, 60e3);
      else if (pct >= 70) usageWait = Math.max(usageWait, 10e3);
    }
    const au = h.get('x-ad-account-usage');
    if (au) { const p = JSON.parse(au).acc_id_util_pct || 0; if (p >= 85) usageWait = Math.max(usageWait, 60e3); }
  } catch { /* cabeçalho em formato inesperado: segue */ }
}

async function call(p, params = {}) {
  const u = new URL(BASE + p);
  for (const [k, v] of Object.entries(params)) if (v != null) u.searchParams.set(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
  for (let a = 0; a < 7; a++) {
    if (usageWait) { log('perto do limite da API, aguardando', Math.round(usageWait / 1000), 's'); await sleep(usageWait); usageWait = 0; }
    await sleep(PAUSE);
    let r, j;
    try { r = await fetch(u, { headers: { authorization: 'Bearer ' + TOKEN, accept: 'application/json' } }); j = await r.json(); }
    catch (e) { log('falha de rede em', p, 'tentativa', a + 1); await sleep(4000 * (a + 1)); continue; }
    readUsage(r.headers);
    if (r.ok && !j.error) return j;
    const e = j.error || {}; const code = e.code, sub = e.error_subcode;
    const msg = redact(`${p}: ${e.message || 'HTTP ' + r.status} (código ${code}${sub ? '/' + sub : ''})`);
    if (code === 190 || code === 102 || code === 463 || code === 467) throw new ApiError(msg, code, sub, 'token');
    if (code === 10 || code === 200 || (code >= 270 && code <= 299)) throw new ApiError(msg, code, sub, 'permission');
    if ([4, 17, 32, 613].includes(code) || (code >= 80000 && code <= 80014)) { log('limite da API:', msg); await sleep(Math.min(15 * 60e3, 60e3 * (a + 1))); continue; }
    if (code === 1 || code === 2 || r.status >= 500) {
      if (/reduce the amount of data|too much data/i.test(e.message || '') || sub === 1487534) throw new ApiError(msg, code, sub, 'too_much');
      log('erro temporário:', msg); await sleep(5000 * (a + 1)); continue;
    }
    throw new ApiError(msg, code, sub, 'other');
  }
  throw new ApiError(redact(`${p}: sem resposta após várias tentativas`), 0, 0, 'retry');
}

async function all(p, params) {
  const out = []; let after = null;
  for (let n = 0; n < 2000; n++) {
    const j = await call(p, { ...params, after });
    out.push(...(j.data || []));
    after = j.paging && j.paging.next && j.paging.cursors && j.paging.cursors.after;
    if (!after) return out;
  }
  return out;
}

// datas no fuso da conta
const day = 864e5;
const isoD = t => new Date(t).toISOString().slice(0, 10);
const parseD = s => Date.parse(s + 'T00:00:00Z');
const addD = (s, n) => isoD(parseD(s) + n * day);
const todayIn = off => isoD(Date.now() + off * 3600e3);
const monthStart = s => s.slice(0, 8) + '01';
const monthEnd = s => { const d = new Date(parseD(monthStart(s))); return isoD(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)); };
const minD = (a, b) => a < b ? a : b, maxD = (a, b) => a > b ? a : b;

async function insights(acct, level, since, until, fields, increment) {
  const params = { level, fields, time_range: { since, until }, limit: 500, use_unified_attribution_setting: true };
  if (increment) params.time_increment = 1;
  try { return await all(`/act_${acct}/insights`, params); }
  catch (e) {
    if (e.kind === 'too_much' && since < until) { // divide o intervalo e tenta de novo
      const mid = addD(since, Math.floor((parseD(until) - parseD(since)) / day / 2));
      return [...await insights(acct, level, since, mid, fields, increment), ...await insights(acct, level, addD(mid, 1), until, fields, increment)];
    }
    throw e;
  }
}

const num = v => v == null || v === '' ? 0 : Number(v);
function actions(r) { const o = {}; for (const a of r.actions || []) { const k = ACT[a.action_type]; if (k) o[k] = num(a.value); } return o; }

// busca dias [since, until] e substitui esses dias no armazenamento (sem duplicar)
async function fetchDays(st, acct, since, until) {
  const rows = await insights(acct, 'ad', since, until, FIELDS_AD, true);
  const lv = {};
  for (const level of ['account', 'campaign', 'adset']) lv[level] = await insights(acct, level, since, until, `${LEVEL_ID[level]},date_start,reach,impressions`, true);
  // só altera o armazenamento depois que tudo do intervalo chegou
  for (let d = since; d <= until; d = addD(d, 1)) { delete st.days[d]; for (const level in lv) if (st.lvl[level]) delete st.lvl[level][d]; }
  for (const r of rows) {
    st.names.c[r.campaign_id] = r.campaign_name; st.names.s[r.adset_id] = [r.adset_name, r.campaign_id]; st.names.a[r.ad_id] = [r.ad_name, r.adset_id];
    (st.days[r.date_start] ||= []).push([r.ad_id, Math.round(num(r.spend) * 100), num(r.impressions), num(r.reach), num(r.clicks), num(r.inline_link_clicks), actions(r)]);
  }
  for (const [level, arr] of Object.entries(lv)) { st.lvl[level] ||= {}; for (const r of arr) ((st.lvl[level][r.date_start] ||= {})[r[LEVEL_ID[level]]] = [num(r.reach), num(r.impressions)]); }
  return rows.length;
}

async function fetchPeriod(st, acct, key, since, until) {
  const levels = {};
  for (const level of ['account', 'campaign', 'adset', 'ad']) {
    const arr = await insights(acct, level, since, until, `${LEVEL_ID[level]},reach,impressions`, false);
    levels[level] = Object.fromEntries(arr.map(r => [r[LEVEL_ID[level]], [num(r.reach), num(r.impressions)]]));
  }
  st.periods[key] = { since, until, at: Date.now(), levels };
}

async function readJson(f, d) { try { return JSON.parse(await fs.readFile(f, 'utf8')); } catch { return d; } }
async function writeJson(f, o) { const t = f + '.tmp'; await fs.writeFile(t, JSON.stringify(o)); await fs.rename(t, f); }

async function syncAccount(acct, info) {
  const file = path.join(STORE, acct + '.json');
  const st = await readJson(file, null) || { days: {}, lvl: {}, names: { c: {}, s: {}, a: {} }, periods: {}, done: [] };
  const off = Number(info.timezone_offset_hours_utc || 0);
  const today = todayIn(off), winStart = addD(today, -(REPROC - 1));
  // carga histórica por mês, salvando a cada mês concluído
  for (let m = monthStart(START); m < winStart; m = addD(monthEnd(m), 1)) {
    const key = m.slice(0, 7); if (st.done.includes(key)) continue;
    if (Date.now() - T_START > MAX_MS) { st.partial = true; log(info.name, 'histórico continua na próxima rodada (a partir de', key + ')'); break; }
    const s = maxD(maxD(m, START), st.fetchedUntil ? addD(st.fetchedUntil, 1) : START), u = minD(monthEnd(m), addD(winStart, -1));
    if (s > u) { if (u === monthEnd(m)) st.done.push(key); continue; }
    const n = await fetchDays(st, acct, s, u);
    if (u === monthEnd(m)) st.done.push(key);
    st.updated_at = Date.now(); await writeJson(file, st);
    log(info.name, 'histórico', key, n, 'linhas');
  }
  // últimos dias: reprocessa por causa da atribuição
  const n = await fetchDays(st, acct, winStart, today);
  st.fetchedUntil = today; // dias que saírem da janela já foram buscados em definitivo
  log(info.name, 'últimos', REPROC, 'dias:', n, 'linhas');
  st.updated_at = Date.now(); await writeJson(file, st);
  // alcance por período (não é somável: consultado já no nível e intervalo exibidos)
  const wk = addD(today, -((new Date(parseD(today)).getUTCDay() + 6) % 7));
  const want = { w: [wk, today], '7d': [addD(today, -6), today], '30d': [addD(today, -29), today], '90d': [addD(today, -89), today], ytd: [today.slice(0, 4) + '-01-01', today] };
  for (let m = monthStart(maxD(START, addD(today, -400))); m <= today; m = addD(monthEnd(m), 1)) {
    const k = 'm:' + m.slice(0, 7), u = minD(monthEnd(m), today), old = st.periods[k];
    if (!old || old.until !== u || u >= addD(winStart, -1)) want[k] = [maxD(m, START), u];
  }
  for (const [k, [s, u]] of Object.entries(want)) {
    try { await fetchPeriod(st, acct, k, s, u); }
    catch (e) { if (e.kind === 'token') throw e; log(info.name, 'alcance', k, 'não atualizado:', e.message); }
  }
  st.updated_at = Date.now(); await writeJson(file, st);
  return st;
}

async function tokenCheck() {
  const t = { checked_at: Date.now() };
  try {
    const j = await call('/debug_token', { input_token: TOKEN });
    const d = j.data || {};
    Object.assign(t, { valid: !!d.is_valid, type: d.type || null, app_id: d.app_id || null, app_name: d.application || null, user_id: d.user_id || null, expires_at: d.expires_at ? d.expires_at * 1000 : 0, data_access_expires_at: d.data_access_expires_at ? d.data_access_expires_at * 1000 : 0, ads_read: (d.scopes || []).includes('ads_read'), scopes_known: !!d.scopes });
  } catch (e) {
    if (e.kind === 'token') return { ...t, valid: false, error: e.message };
    const me = await call('/me', { fields: 'id' }).catch(x => ({ error: x }));
    Object.assign(t, { valid: !me.error, note: 'Não foi possível ler a validade (debug_token): ' + e.message });
  }
  const exp = [t.expires_at, t.data_access_expires_at].filter(x => x > 0);
  t.next_expiry = exp.length ? Math.min(...exp) : 0;
  return t;
}

async function main() {
  await fs.mkdir(STORE, { recursive: true });
  const statusFile = path.join(OUT, 'meta-status.json');
  const prev = await readJson(statusFile, {});
  const status = { at: Date.now(), version: VERSION, allowed: ALLOWED, accounts: {}, ok: true };
  if (!TOKEN) throw new Error('Segredo META_ACCESS_TOKEN não configurado no repositório.');
  if (!ALLOWED.length) throw new Error('Lista de contas permitidas (META_AD_ACCOUNTS) vazia.');
  status.token = await tokenCheck();
  if (!status.token.valid) throw new Error('Token do Meta inválido ou expirado. ' + (status.token.error || ''));
  if (status.token.scopes_known && !status.token.ads_read) log('::warning::O token não tem a permissão ads_read.');
  if (status.token.next_expiry) {
    const days = Math.floor((status.token.next_expiry - Date.now()) / day);
    status.token.days_left = days;
    if (days <= WARN_DAYS) console.log(`::warning::O token do Meta expira em ${days} dias. Gere um novo e atualize o segredo META_ACCESS_TOKEN.`);
  }
  const bundle = { generated_at: Date.now(), version: VERSION, accounts: {} };
  for (const acct of ALLOWED) {
    const s = { id: acct, ok: false };
    try {
      const info = await call(`/act_${acct}`, { fields: 'name,account_id,currency,timezone_name,timezone_offset_hours_utc,account_status' });
      Object.assign(s, { name: info.name, currency: info.currency, timezone: info.timezone_name, tz_offset: info.timezone_offset_hours_utc, account_status: info.account_status });
      const st = await syncAccount(acct, info);
      s.ok = true; s.updated_at = st.updated_at;
    } catch (e) {
      if (e.kind === 'token') { status.ok = false; status.error = e.message; s.error = e.message; status.accounts[acct] = s; break; }
      if (e.kind === 'permission') { s.pending = true; s.error = 'Acesso pendente: o token não tem permissão nesta conta.'; status.accounts[acct] = s; log('conta', acct, 'sem acesso ainda (acesso pendente)'); continue; }
      s.error = e.message; status.ok = false; log('conta', acct, 'falhou:', e.message, '- mantendo os dados anteriores');
      const old = (prev.accounts || {})[acct]; if (old) s.updated_at = old.updated_at;
    }
    status.accounts[acct] = s;
  }
  // pacote único para o painel, com o que houver de cada conta (inclusive de rodadas anteriores)
  for (const acct of ALLOWED) {
    const st = await readJson(path.join(STORE, acct + '.json'), null); if (!st) continue;
    const s = status.accounts[acct] || (prev.accounts || {})[acct] || { id: acct };
    bundle.accounts[acct] = { info: s, days: st.days, lvl: st.lvl, names: st.names, periods: st.periods };
  }
  const failed0 = Object.values(status.accounts).filter(a => !a.ok);
  if (failed0.length) { // diagnóstico: quem é o token e quais contas ele enxerga
    try {
      const me = await call('/me', { fields: 'id,name' });
      const acc = await all('/me/adaccounts', { fields: 'account_id,name,account_status', limit: 200 });
      status.diagnostic = { user: me.name || me.id, visible_accounts: acc.map(a => ({ id: a.account_id, name: a.name })), allowed_visible: ALLOWED.filter(id => acc.some(a => a.account_id === id)) };
      log('diagnóstico: token de', status.diagnostic.user, '| app', status.token.app_name || status.token.app_id, '| contas visíveis:', acc.length, '| permitidas visíveis:', status.diagnostic.allowed_visible.length);
    } catch (e) { status.diagnostic = { error: e.message }; log('diagnóstico falhou:', e.message); }
  }
  await writeJson(path.join(OUT, 'meta.json'), bundle);
  await writeJson(statusFile, status);
  const fail = Object.values(status.accounts).filter(a => !a.ok && !a.pending);
  const pend = Object.values(status.accounts).filter(a => a.pending);
  if (pend.length) console.log(`::warning::${pend.length} conta(s) com acesso pendente: ${pend.map(a => a.id).join(', ')}`);

  if (fail.length) { console.log(`::error::${fail.length} conta(s) sem atualização: ${fail.map(a => a.id + ' (' + (a.error || '') + ')').join('; ')}`); process.exitCode = 1; }
  log('fim:', Object.values(status.accounts).filter(a => a.ok).length, 'de', ALLOWED.length, 'contas atualizadas');
}

main().catch(async e => {
  console.log('::error::' + redact(e.message));
  try { const f = path.join(OUT, 'meta-status.json'); const prev = await readJson(f, {}); await writeJson(f, { ...prev, ok: false, failed_at: Date.now(), error: redact(e.message) }); } catch {}
  process.exit(1);
});
