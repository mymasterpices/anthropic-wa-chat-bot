"""Live inbox: watch the bot's WhatsApp conversations in a browser.

Cloud API conversations do NOT appear in the WhatsApp Business app or Meta
Business Suite, so this page is how RKJ reads what the bot is saying.

Open:  https://<your-app>/admin/inbox?token=<ADMIN_TOKEN>
Auto-refreshes every 10 seconds. Read-only.
"""

PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RKJ Bot — Live Inbox</title>
<style>
:root{color-scheme:light}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;background:#f0f2f5;height:100vh;display:flex;flex-direction:column}
header{background:#075E54;color:#fff;padding:11px 16px;display:flex;align-items:center;gap:12px;flex-shrink:0}
header h1{font-size:16px;font-weight:600}
header .live{font-size:11px;background:rgba(255,255,255,.18);padding:3px 9px;border-radius:10px}
header .dot{display:inline-block;width:7px;height:7px;border-radius:50%;background:#5efc82;margin-right:5px;animation:p 2s infinite}
@keyframes p{50%{opacity:.35}}
#wrap{flex:1;display:flex;min-height:0}
#list{width:330px;background:#fff;border-right:1px solid #e0e0e0;overflow-y:auto;flex-shrink:0}
.conv{padding:11px 14px;border-bottom:1px solid #f0f0f0;cursor:pointer}
.conv:hover{background:#f7f9fa}
.conv.sel{background:#e7f3ef}
.conv .top{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.conv .ph{font-weight:600;font-size:14px}
.conv .tm{font-size:11px;color:#8696a0;white-space:nowrap}
.conv .pv{font-size:12.5px;color:#667781;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.badge{font-size:10px;padding:2px 7px;border-radius:9px;margin-top:5px;display:inline-block}
.hot{background:#FFEBEE;color:#B71C1C}
#pane{flex:1;display:flex;flex-direction:column;min-width:0}
#head{background:#f0f2f5;border-bottom:1px solid #e0e0e0;padding:10px 16px;font-weight:600;font-size:14px;flex-shrink:0}
#msgs{flex:1;overflow-y:auto;padding:16px;background:#ECE5DD}
.row{display:flex;margin-bottom:7px}
.row.user{justify-content:flex-end}
.b{max-width:72%;padding:7px 10px 6px;border-radius:8px;font-size:14px;line-height:1.42;white-space:pre-wrap;word-wrap:break-word;box-shadow:0 1px 1px rgba(0,0,0,.07)}
.assistant .b{background:#fff;border-top-left-radius:0}
.user .b{background:#DCF8C6;border-top-right-radius:0}
.t{font-size:10px;color:#8696a0;text-align:right;margin-top:3px}
.cards{font-size:11px;color:#0b7a5f;margin-top:5px;padding-top:4px;border-top:1px dashed #cfe0d9}
.esc{text-align:center;margin:11px 0}
.esc span{background:#FFEBEE;color:#B71C1C;font-size:12px;padding:5px 12px;border-radius:11px;display:inline-block;max-width:85%}
.empty{color:#8696a0;text-align:center;padding:60px 20px;font-size:14px}
</style></head><body>
<header><h1>RK Jewellers — Bot Inbox</h1>
<span class="live"><span class="dot"></span>live · refreshes every 10s</span>
<span class="live" id="cnt"></span></header>
<div id="wrap">
  <div id="list"><div class="empty">Loading…</div></div>
  <div id="pane">
    <div id="head">Select a conversation</div>
    <div id="msgs"><div class="empty">Pick a customer on the left to read the full chat.</div></div>
  </div>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
let sel = null, lastSig = '';
const fmt = ts => { const d = new Date(ts*1000);
  return d.toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}); };
const esc = s => { const d=document.createElement('div'); d.textContent=s||''; return d.innerHTML; };

async function api(path){
  const r = await fetch(path + (path.includes('?')?'&':'?') + 'token=' + encodeURIComponent(TOKEN));
  if(r.status === 403){ document.body.innerHTML = '<p style="padding:40px;font:15px sans-serif">Wrong or missing token. Open this page as <code>/admin/inbox?token=YOUR_ADMIN_TOKEN</code></p>'; throw new Error('403'); }
  return r.json();
}
async function loadList(){
  const rows = await api('/admin/api/conversations');
  document.getElementById('cnt').textContent = rows.length + ' customers';
  const sig = JSON.stringify(rows.map(r=>[r.phone,r.last_ts]));
  if(sig !== lastSig){
    lastSig = sig;
    const el = document.getElementById('list');
    el.innerHTML = rows.length ? rows.map(r => `
      <div class="conv ${r.phone===sel?'sel':''}" onclick="open_('${r.phone}')">
        <div class="top"><span class="ph">+${esc(r.phone)}</span><span class="tm">${fmt(r.last_ts)}</span></div>
        <div class="pv">${r.last_role==='assistant'?'Bot: ':''}${esc(r.last_msg)}</div>
        ${r.escalations?`<span class="badge hot">${r.escalations} escalation${r.escalations>1?'s':''}</span>`:''}
      </div>`).join('') : '<div class="empty">No conversations yet.</div>';
  }
  if(sel) loadChat(sel, true);
}
async function open_(phone){ sel = phone; lastSig=''; await loadList(); loadChat(phone); }
async function loadChat(phone, quiet){
  const msgs = await api('/admin/api/conversation/' + encodeURIComponent(phone));
  document.getElementById('head').textContent = '+' + phone;
  const box = document.getElementById('msgs');
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 60;
  box.innerHTML = msgs.map(m => m.role==='escalation'
    ? `<div class="esc"><span>🔔 ${esc(m.text)}</span></div>`
    : `<div class="row ${m.role}"><div class="b">${esc(m.text)}
        ${m.cards.length?`<div class="cards">📎 sent ${m.cards.length} product card(s): ${esc(m.cards.join(', '))}</div>`:''}
        <div class="t">${fmt(m.ts)}</div></div></div>`).join('');
  if(!quiet || atBottom) box.scrollTop = box.scrollHeight;
}
loadList(); setInterval(loadList, 10000);
</script></body></html>"""
