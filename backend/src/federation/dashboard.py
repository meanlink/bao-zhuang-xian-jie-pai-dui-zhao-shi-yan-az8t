"""中心简报页面：原生 HTML/JS，无构建步骤，且不出现任何原始节拍。"""
from __future__ import annotations

FEDERATION_JS = r"""
const $ = (s, el=document) => el.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtTime = ts => ts ? new Date(ts*1000).toLocaleString('zh-CN', {hour12:false}) : '—';
// 相对基址：直连为 ''，经 nginx /federation/ 反代时为 '/federation'，始终指向中心自身
const BASE = location.pathname.replace(/\/$/, '');

async function api(path, options={}) {
  const r = await fetch(BASE + path, {headers:{'Content-Type':'application/json'}, ...options});
  const data = await r.json().catch(() => ({detail:'响应解析失败'}));
  if (!r.ok) throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
  return data;
}

function badge(status) {
  const map = {collecting:['待收集','badge-collecting'], sealed:['已封算','badge-sealed'],
               received:['已到','badge-ok'], missing:['缺站','badge-missing']};
  const [text, cls] = map[status] || [status,'badge'];
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

function trialCard(t) {
  const stations = t.stations.map(s => `
    <tr>
      <td>${esc(s.station_name)}<small>${esc(s.station_id)}</small></td>
      <td><code>${esc(s.expected_version)}</code></td>
      <td>${badge(s.status)}${s.received_version ? `<small>实收版本 ${esc(s.received_version)}</small>` : ''}</td>
      <td>${s.totals ? `A ${s.totals.A} 件 / B ${s.totals.B} 件` : '—'}</td>
      <td>${fmtTime(s.received_at)}</td>
    </tr>`).join('');
  const conflicts = (t.conflicts||[]).map(c =>
    `<li><code>${esc(c.station_id)}</code> · ${esc(c.reason)} · ${fmtTime(c.occurred_at)}</li>`).join('')
    || '<li class="muted">无冲突</li>';
  const result = t.status === 'sealed' && t.result ? `
    <section class="result">
      <h3>封算结论（合并样本，中心未见原始节拍）</h3>
      <p class="summary">${esc(t.result.summary)}</p>
      <div class="metrics">
        ${t.result.metrics.map(m => `<div><span>${esc(m.label)}</span><strong>${esc(m.value)}</strong><small>${esc(m.unit||'')}</small></div>`).join('')}
      </div>
      <table class="mini"><thead><tr><th>组</th><th>合并件数</th><th>均值</th><th>标准差</th></tr></thead>
        <tbody>${t.result.table.rows.map(r => `<tr><td>${esc(r.arm)}</td><td>${esc(r.n)}</td><td>${esc(r.mean)}</td><td>${esc(r.sd)}</td></tr>`).join('')}</tbody></table>
      <details><summary>计算口径</summary><p class="muted">${esc(t.result.method)}</p></details>
    </section>` : `
    <div class="seal-bar">
      <p>进度 ${t.received_count}/${t.station_count}；缺站：${t.missing.length ? esc(t.missing.join('、')) : '无'}</p>
      <button data-seal="${t.id}" ${t.missing.length ? 'disabled' : ''}>封算</button>
    </div>`;
  return `<article class="trial" data-id="${t.id}">
    <header><div><h2>${esc(t.name)}</h2><small>试验 #${t.id} · 置信度 ${esc(t.confidence*100)}% · 建 ${fmtTime(t.created_at)}</small></div>${badge(t.status)}</header>
    <table class="stations"><thead><tr><th>站点</th><th>固定样本版本</th><th>状态</th><th>汇总件数（仅件数/均值/离差）</th><th>接收时间</th></tr></thead><tbody>${stations}</tbody></table>
    <section class="conflicts"><h4>冲突与拒收</h4><ul>${conflicts}</ul></section>
    ${result}
  </article>`;
}

async function refresh() {
  const list = await api('/api/federation/trials');
  const root = $('#trials');
  if (!list.length) { root.innerHTML = '<p class="empty">尚无联合试验。由中心管理员建立试验并固定站点名单与样本版本。</p>'; return; }
  root.innerHTML = list.map(trialCard).join('');
  root.querySelectorAll('[data-seal]').forEach(btn => btn.addEventListener('click', async e => {
    e.target.disabled = true;
    try { await api(`/api/federation/trials/${e.target.dataset.seal}/seal`, {method:'POST'}); }
    catch (err) { alert(err.message); }
    refresh();
  }));
}

async function createTrial(e) {
  e.preventDefault();
  const roster = $('#roster').value.split('\n').map(line => line.trim()).filter(Boolean)
    .map(line => { const [station_id, station_name, sample_version] = line.split(',').map(x => x && x.trim()); return {station_id, station_name: station_name || station_id, sample_version}; });
  try {
    await api('/api/federation/trials', {method:'POST', body: JSON.stringify({
      name: $('#name').value, confidence: Number($('#confidence').value), roster})});
    e.target.reset(); refresh();
  } catch (err) { alert(err.message); }
}

document.addEventListener('DOMContentLoaded', () => {
  $('#create-form').addEventListener('submit', createTrial);
  $('#refresh').addEventListener('click', refresh);
  refresh(); setInterval(refresh, 5000);
});
"""


def render_index():
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>联合试验 · 联邦节拍对照中心</title>
<style>
:root{--bg:#f4f2ec;--ink:#1f2421;--muted:#6b7268;--line:#d9d4c7;--accent:#2f6f57;--warn:#a8541f;--card:#fffef9}
*{box-sizing:border-box}body{margin:0;font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
header.top{padding:22px 28px;border-bottom:1px solid var(--line);background:var(--card);display:flex;justify-content:space-between;align-items:center}
header.top h1{margin:0;font-size:18px}header.top p{margin:4px 0 0;color:var(--muted);font-size:12px}
.wrap{max-width:1080px;margin:24px auto;padding:0 20px;display:grid;gap:20px}
form.create,.trial{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px}
form.create h3,.trial h2{margin:0 0 10px;font-size:15px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}
label{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:4px}
input,textarea,select{border:1px solid var(--line);border-radius:6px;padding:8px;font:inherit;background:#fff;min-width:160px}
textarea{width:100%;min-height:80px;font-family:ui-monospace,Menlo,monospace}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:9px 16px;font:inherit;cursor:pointer}
button:disabled{opacity:.45;cursor:not-allowed}.trial header{display:flex;justify-content:space-between;align-items:start;gap:10px}
.trial h2{font-size:16px}.trial small,.muted{color:var(--muted);font-size:12px}
.badge{font-size:12px;padding:3px 9px;border-radius:999px;white-space:nowrap}.badge-collecting{background:#f3e6c8;color:#7a5410}
.badge-sealed{background:#dcece4;color:var(--accent)}.badge-ok{background:#dcece4;color:var(--accent)}.badge-missing{background:#f6ddd2;color:var(--warn)}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}td small{display:block;color:var(--muted);margin-top:2px}code{background:#f0ede4;padding:1px 6px;border-radius:4px}
.conflicts{margin-top:12px}.conflicts h4{margin:0 0 6px;font-size:13px}.conflicts ul{margin:0;padding-left:18px}
.seal-bar{display:flex;justify-content:space-between;align-items:center;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}
.result{margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}.result .summary{font-weight:600}
.metrics{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}.metrics div{background:#f7f5ee;border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}
.metrics span{display:block;font-size:12px;color:var(--muted)}.metrics strong{font-size:18px}.metrics small{color:var(--muted)}
table.mini{max-width:520px}details{margin-top:8px}.empty{color:var(--muted);padding:30px;text-align:center}
.privacy{font-size:12px;color:var(--muted);border:1px dashed var(--line);border-radius:8px;padding:10px;background:#faf8f2}
</style></head>
<body>
<header class="top"><div><h1>联合试验 · 联邦节拍对照中心</h1>
<p>两地车间不交换原始节拍，仅交换各站件数、均值与平方离差和；中心与本页面均不出现原始片段。</p></div>
<button id="refresh" class="ghost" style="background:#eee;color:#333">刷新 ↻</button></header>
<div class="wrap">
 <p class="privacy">隐私边界：原始片段仅保存在各站点本地库。中心持久化的只有试验名册、各站样本版本与 A/B 两组的 n / mean / ss；封算结果来自合并样本，无法还原任何单件节拍。</p>
 <form class="create" id="create-form"><h3>建立联合试验（固定站点名单与样本版本）</h3>
   <div class="row">
     <label>试验名称<input id="name" required placeholder="如：两车间封装动作 11 月联合试验"></label>
     <label>置信度<select id="confidence"><option value="0.95">95%</option><option value="0.9">90%</option><option value="0.99">99%</option></select></label>
   </div>
   <p class="muted" style="margin:10px 0 4px">每行一个站点：station_id, 站点名称, 样本版本（建立后不可变）</p>
   <textarea id="roster" required placeholder="YC-NORTH, 榆川北车间, sv-2026-11a&#10;YC-SOUTH, 榆川南车间, sv-2026-11b"></textarea>
   <div style="margin-top:10px"><button type="submit">建立试验</button></div>
 </form>
 <div id="trials"><p class="empty">加载中…</p></div>
</div><script src="static/federation.js"></script></body></html>"""
