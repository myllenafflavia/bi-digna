// Sincroniza o RD Station CRM (API v1) com data/bi.json e registra as movimentações de etapa.
// Roda no GitHub Actions. O token vem do segredo RD_CRM_TOKEN e nunca é gravado em arquivo.
// Dados pessoais de contatos (nome, e-mail, telefone) e o nome do negócio NÃO são gravados:
// o contato vira um código irreversível, usado só para contar pessoas únicas.
import { createHmac } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';

const TOKEN = process.env.RD_CRM_TOKEN;
const BASE = (process.env.RD_BASE || 'https://crm.rdstation.com/api/v1').replace(/\/$/, '');
const START = process.env.RD_START || '2018-01-01';
const SALT = process.env.RD_HASH_SALT || TOKEN || '';
const OUT = process.env.OUT_DIR || 'data';
const PAUSE = Number(process.env.RD_PAUSE_MS || 400);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(new Date().toISOString(), ...a);

async function get(p, params = {}) {
  const u = new URL(BASE + p);
  u.searchParams.set('token', TOKEN);
  for (const [k, v] of Object.entries(params)) if (v != null) u.searchParams.set(k, String(v));
  for (let a = 0; a < 7; a++) {
    await sleep(PAUSE);
    let r;
    try { r = await fetch(u, { headers: { accept: 'application/json' } }); }
    catch (e) { log('rede falhou em', p, '- tentativa', a + 1); await sleep(3000 * (a + 1)); continue; }
    if (r.status === 429 || r.status >= 500) { log('HTTP', r.status, 'em', p, '- aguardando'); await sleep(5000 * (a + 1)); continue; }
    if (!r.ok) { const t = (await r.text()).slice(0, 300); throw new Error(`${p} respondeu HTTP ${r.status}: ${t}`); }
    return r.json();
  }
  throw new Error(`${p}: sem resposta após várias tentativas`);
}
const list = (r, key) => Array.isArray(r) ? r : (r && (r[key] || r.data || r.items)) || [];
const idOf = o => o && (o._id || o.id || null);
const iso = t => new Date(t).toISOString().slice(0, 19);

async function pipelines() {
  const ps = list(await get('/deal_pipelines', { limit: 200 }), 'deal_pipelines');
  const out = [];
  for (const p of ps) {
    let st = p.deal_stages && p.deal_stages.length ? p.deal_stages : list(await get('/deal_stages', { deal_pipeline_id: idOf(p), limit: 12 }), 'deal_stages');
    st = [...st].sort((a, b) => (a.order ?? 0) - (b.order ?? 0));
    out.push({ id: idOf(p), name: p.name, stages: st.map(s => ({ id: idOf(s), name: s.name })) });
  }
  return out;
}

async function dealsWindow(pid, ws, we, depth = 0) {
  const all = [];
  let page = 1, next = null;
  for (;;) {
    const params = { deal_pipeline_id: pid, limit: 200, created_at_period: true, start_date: iso(ws), end_date: iso(we) };
    if (next) params.next_page = next; else params.page = page;
    const r = await get('/deals', params);
    const arr = list(r, 'deals');
    all.push(...arr);
    if (r && r.next_page) { next = r.next_page; continue; }
    if (r && r.has_more && arr.length) {
      page++;
      if (page > 45 && depth < 8) { // perto do teto de paginação: divide a janela
        const mid = ws + (we - ws) / 2;
        return [...await dealsWindow(pid, ws, mid, depth + 1), ...await dealsWindow(pid, mid, we, depth + 1)];
      }
      continue;
    }
    return all;
  }
}

function contactKey(d) {
  const c = (d.contacts || [])[0];
  if (!c) return null;
  const e = (c.emails || [])[0];
  const ph = (c.phones || [])[0];
  const k = (e && (e.email || e)) || (ph && String(ph.phone || ph).replace(/\D/g, '')) || (c.name || '').trim().toLowerCase();
  if (!k) return null;
  return createHmac('sha256', SALT).update(String(k).toLowerCase()).digest('hex').slice(0, 12);
}
function customs(d) {
  let city = null, utm = null, obs = '';
  for (const f of d.deal_custom_fields || []) {
    const label = String((f.custom_field && (f.custom_field.label || f.custom_field.name)) || f.label || f.name || '');
    const v = Array.isArray(f.value) ? f.value.join(', ') : f.value;
    if (v == null || v === '') continue;
    if (/observa|origem da venda|como chegou|como conheceu/i.test(label)) obs = (obs ? obs + ' ' : '') + String(v);
    if (/utm.?campaign|campanha.?utm/i.test(label)) utm = String(v).slice(0, 120);
    else if (/cidade|praç|praca|munic/i.test(label)) city = String(v).slice(0, 60);
  }
  return { city, utm, obs };
}

// Classifica o texto de anotações/observação: de onde veio a venda.
// Só a classificação e o termo encontrado são gravados; o texto nunca é salvo.
const norm = t => String(t || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
const NEG = /\b(nao|n)\s+(veio|foi|e|era)\s+(de|do|da|por|pelo|pela)?\s*(anuncio|trafego|midia|campanha|facebook|instagram|meta|google)/;
const RULES = [
  ['m', /facebook|instagram|\binsta\b|\bfb\b|meta ads|\bmeta\b|lead ?ads?|formulario do (face|insta)|anuncio do (face|insta)|\bface\b/],
  ['g', /google|adwords|youtube|pesquisa paga/],
  ['p', /trafego pago|midia paga|anuncio|campanha paga|patrocinad|impulsionad|\bads\b|lead pago|\bpago\b/],
  ['n', /indicacao|indicado|prospeccao|prospectad|organico|cliente antigo|recompra|carteira|parceir|presencial|na loja|balcao|porta a porta|walk ?in/],
];
function classify(texts) {
  const found = {};
  for (const t of texts) {
    const x = norm(t); if (!x.trim()) continue;
    const neg = x.match(NEG); if (neg) { found.n ||= neg[0].slice(0, 40); continue; }
    for (const [c, re] of RULES) { const m = x.match(re); if (m && !found[c]) found[c] = m[0].slice(0, 40); }
  }
  for (const c of ['m', 'g', 'p', 'n']) if (found[c]) return [c, found[c]];
  return ['', ''];
}
async function readJson(f, dflt) { try { return JSON.parse(await fs.readFile(f, 'utf8')); } catch { return dflt; } }
async function writeJson(f, obj) { const tmp = f + '.tmp'; await fs.writeFile(tmp, JSON.stringify(obj)); await fs.rename(tmp, f); }

async function main() {
  if (!TOKEN) throw new Error('Segredo RD_CRM_TOKEN não configurado no repositório.');
  await fs.mkdir(OUT, { recursive: true });
  const now = Date.now();
  log('lendo funis');
  const pls = await pipelines();
  log(pls.length, 'funis');

  const raw = new Map();
  for (const p of pls) {
    let t = Date.parse(START + 'T00:00:00Z'); let n = 0;
    while (t < now) {
      const e = Math.min(now + 864e5, Date.UTC(new Date(t).getUTCFullYear(), new Date(t).getUTCMonth() + 3, 1));
      for (const d of await dealsWindow(p.id, t, e)) { raw.set(idOf(d), { d, pid: p.id }); n++; }
      t = e;
    }
    log(p.name, n, 'negócios');
  }
  if (!raw.size) throw new Error('A API não devolveu negócios. Confira o token e o acesso aos funis.');

  const dict = { src: [], camp: [], user: [], reason: [], city: [], utm: [] };
  const ix = {}; for (const k in dict) ix[k] = new Map();
  const code = (k, v) => { if (v == null || v === '') return -1; v = String(v); if (!ix[k].has(v)) { ix[k].set(v, dict[k].length); dict[k].push(v); } return ix[k].get(v); };
  const pIndex = new Map(pls.map((p, i) => [p.id, i]));
  const sIndex = new Map(); pls.forEach((p, i) => p.stages.forEach((s, j) => { sIndex.set(s.id, [i, j]); }));

  const histFile = path.join(OUT, 'history.json');
  const H = await readJson(histFile, null) || { start: now, lastRun: null, last: {}, ev: {} };
  const first = !H.lastRun;
  const deals = [];
  const obsText = new Map();
  const seen = {};
  for (const [id, { d, pid }] of raw) {
    const sid = idOf(d.deal_stage) || d.deal_stage_id;
    let pi = pIndex.get(pid), si = 0;
    if (sid && sIndex.has(sid)) [pi, si] = sIndex.get(sid);
    else if (d.deal_stage && d.deal_stage.name) { const j = pls[pi].stages.findIndex(s => s.name === d.deal_stage.name); if (j >= 0) si = j; }
    const st = d.win === true ? 'w' : d.win === false ? 'l' : d.hold ? 'p' : 'o';
    const c = Date.parse(d.created_at) / 1000 | 0;
    const cl = d.closed_at ? (Date.parse(d.closed_at) / 1000 | 0) : 0;
    const cu = customs(d);
    obsText.set(id, cu.obs);
    deals.push([id, pi, si, st, c, cl, code('src', d.deal_source && d.deal_source.name), code('camp', d.campaign && d.campaign.name), code('user', d.user && (d.user.name || d.user.nickname)), code('reason', st === 'l' && d.deal_lost_reason ? d.deal_lost_reason.name : null), contactKey(d), code('city', cu.city), code('utm', cu.utm), Math.round(Number(d.amount_total) || 0)]);
    // histórico: registra entrada em etapa e mudanças entre sincronizações
    const pidS = pls[pi].id, sidS = pls[pi].stages[si] && pls[pi].stages[si].id;
    const prev = H.last[id];
    const upd = d.updated_at ? Date.parse(d.updated_at) : now;
    const when = Math.round(Math.min(now, H.lastRun && upd > H.lastRun ? upd : now) / 1000);
    if (!prev) {
      if (!first && c * 1000 >= H.start) H.ev[id] = [[pidS, sidS, c]];
    } else if (prev[0] !== pidS || prev[1] !== sidS) {
      if (!H.ev[id]) H.ev[id] = [[prev[0], prev[1], 0]];
      H.ev[id].push([pidS, sidS, when]);
    } else if ((prev[2] === 'w' || prev[2] === 'l') && (st === 'o' || st === 'p')) {
      if (!H.ev[id]) H.ev[id] = [[prev[0], prev[1], 0]];
      H.ev[id].push([pidS, sidS, when, 'r']);
    }
    seen[id] = [pidS, sidS, st];
  }
  H.last = seen; H.lastRun = now;

  // origem informada pela equipe nas anotações das vendas (funis de venda)
  const notesFile = path.join(OUT, 'notes.json');
  const NC = await readJson(notesFile, {});
  const saleFunnels = new Set(pls.filter(p => /^vendas?\b/i.test(p.name.trim())).map(p => p.id));
  let fetched = 0; const ND = { vendas: 0, com_anotacao: 0, sem_anotacao: 0, classificadas: 0, campos_personalizados: {} };
  for (const { d } of raw.values()) for (const f of d.deal_custom_fields || []) { const l = String((f.custom_field && (f.custom_field.label || f.custom_field.name)) || f.label || '').trim(); if (l && f.value != null && f.value !== '') ND.campos_personalizados[l] = (ND.campos_personalizados[l] || 0) + 1; }
  for (const r of deals) {
    const [id, pi, , st] = r; if (st !== 'w' || !saleFunnels.has(pls[pi].id)) continue;
    const d = raw.get(id).d; const upd = d.updated_at || '';
    if (!NC[id] || NC[id].u !== upd || NC[id].t === undefined) {
      const texts = [obsText.get(id) || ''];
      try {
        for (let page = 1; page <= 5; page++) {
          const j = await get('/activities', { deal_id: id, limit: 200, page });
          const arr = list(j, 'activities'); texts.push(...arr.map(a => a.text || a.description || ''));
          if (!(j && j.has_more) || arr.length < 200) break;
        }
        const [c, k] = classify(texts); NC[id] = { c, k, u: upd, t: texts.filter(x => String(x).trim()).length }; fetched++;
      } catch (e) { log('anotações de', id, 'não lidas:', e.message); }
    }
    r.push(NC[id] ? NC[id].c : '', NC[id] ? NC[id].k : '');
    if (NC[id]) { ND.vendas++; if (NC[id].t > 0) ND.com_anotacao++; else if (NC[id].t === 0) ND.sem_anotacao++; if (NC[id].c) ND.classificadas++; }
  }
  for (const r of deals) if (r.length < 16) r.push('', '');
  await writeJson(notesFile, NC);
  log('anotações de vendas lidas nesta rodada:', fetched);
  const hist = {};
  for (const [id, evs] of Object.entries(H.ev)) {
    hist[id] = evs.map(e => { const pi = pIndex.get(e[0]); const si = pi == null ? -1 : pls[pi].stages.findIndex(s => s.id === e[1]); return [pi ?? -1, si, e[2], e[3] || 0]; });
  }
  await writeJson(histFile, H);
  await writeJson(path.join(OUT, 'bi.json'), {
    meta: { generated_at: Math.round(now / 1000), track_start: Math.round(H.start / 1000), deals: deals.length, source: 'RD Station CRM API v1' },
    pipelines: pls, dict, deals, hist,
  });
  await writeJson(path.join(OUT, 'status.json'), { ok: true, at: Math.round(now / 1000), deals: deals.length, message: 'Sincronização concluída', anotacoes: ND });
  log('ok:', deals.length, 'negócios gravados');
}

main().catch(async e => {
  console.error(e.message);
  try { await fs.mkdir(OUT, { recursive: true }); const prev = await readJson(path.join(OUT, 'status.json'), {}); await writeJson(path.join(OUT, 'status.json'), { ...prev, ok: false, failed_at: Math.round(Date.now() / 1000), message: String(e.message).replace(TOKEN || '###', '***').slice(0, 300) }); } catch {}
  process.exit(1);
});
