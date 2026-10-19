'use strict';
const el = id => document.getElementById(id);
let config, token, expires = 0, connections = [], boxes = [];
const status = text => { el('status').textContent = text; };
const guarded = action => async event => { try { await action(event); } catch (error) { status(error.message || 'Request failed'); } };
async function accessToken() {
  if (token && Date.now() < expires) return token;
  const refresh = sessionStorage.getItem('refresh');
  if (!refresh) throw Error('Sign in through Back to inbox, then reopen AI connections.');
  const response = await fetch(config.auth_url + '/oauth2/token', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams({grant_type:'refresh_token', client_id:config.client_id, refresh_token:refresh})});
  if (!response.ok) throw Error('Session expired. Sign in through Back to inbox.');
  const value = await response.json(); token = value.access_token; expires = Date.now() + (value.expires_in - 30) * 1000; return token;
}
async function request(method, path, data) {
  const response = await fetch(path.startsWith('/') ? path : '/ai/' + path, {method, headers:{Authorization:'Bearer ' + await accessToken(), 'Content-Type':'application/json'}, body:data ? JSON.stringify(data) : undefined});
  const value = await response.json(); if (!response.ok) { const error = Error(value.error || 'Request failed'); error.status = response.status; throw error; } return value;
}
function text(tag, value) { const node = document.createElement(tag); node.textContent = value; return node; }
function renderPermissions() {
  const connection = connections.find(c => c.sk === el('client').value);
  el('enabled').checked = connection?.enabled === true; el('mailboxes').replaceChildren();
  for (const box of boxes) {
    const group = document.createElement('fieldset'); group.append(text('legend', box.address));
    for (const [permission, title] of [['read','Read and search'], ['draft','Save drafts'], ['send_approved','Send an owner-approved revision']]) {
      const label = text('label', title), input = document.createElement('input'); input.type = 'checkbox'; input.dataset.mailbox = box.name; input.dataset.permission = permission;
      input.checked = connection?.mailboxes?.[box.name]?.permissions?.includes(permission) || false; label.prepend(input); group.append(label);
    }
    el('mailboxes').append(group);
  }
}
async function drafts() {
  const result = await request('GET', 'drafts'); el('drafts').replaceChildren();
  if (!result.drafts.length) el('drafts').append(text('p', 'No AI drafts yet.'));
  for (const draft of result.drafts) {
    const button = text('button', `${draft.mailbox} · ${draft.state} · ${new Date(draft.created * 1000).toLocaleString()}`);
    button.disabled = !draft.draft_id; button.onclick = guarded(() => review(draft.id)); el('drafts').append(button);
  }
}
async function review(reference) {
  const draft = await request('GET', 'drafts/' + encodeURIComponent(reference)); const container = el('review'); container.hidden = false; container.replaceChildren();
  container.append(text('h2', draft.content.subject || '(no subject)'), text('p','From: ' + draft.sender));
  for (const field of ['to','cc','bcc']) container.append(text('p',field.toUpperCase() + ': ' + (draft.content[field] || 'None')));
  container.append(text('pre', draft.content.body));
  for (const attachment of draft.content.attachments) {
    const button = text('button', 'Download ' + attachment.name);
    button.onclick = () => { const bytes = Uint8Array.from(atob(attachment.data), c => c.charCodeAt(0)); const url = URL.createObjectURL(new Blob([bytes],{type:'application/octet-stream'})); const a = document.createElement('a'); a.href=url; a.download=attachment.name; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000); };
    container.append(button);
  }
  const approve = text('button','Approve this exact message for sending'); approve.onclick = guarded(async () => { approve.disabled=true; try { await request('POST','approve',{reference,digest:draft.digest}); status('Approved for 24 hours. The connected AI may now send this exact revision.'); } finally { approve.disabled=false; } });
  const revoke = text('button','Revoke approval'); revoke.onclick=guarded(async()=>{await request('POST','revoke',{reference});status('Approval revoked.');});
  container.append(approve,revoke);
}
el('permissions').onsubmit = guarded(async event => {
  event.preventDefault(); const mailboxes = {};
  for (const input of el('mailboxes').querySelectorAll('input:checked')) (mailboxes[input.dataset.mailbox] ||= []).push(input.dataset.permission);
  await request('POST','connections',{client_id:el('client').value,enabled:el('enabled').checked,mailboxes});
  connections=(await request('GET','connections')).connections; status('Connection permissions saved. Revocations apply to the next request.');
});
el('client').onchange=renderPermissions; el('refresh').onclick=guarded(drafts);
guarded(async()=>{
  config=await (await fetch('config.json')).json(); el('endpoint').value=location.origin+'/mcp';
  window.SimpleMailReceivingHealth.mount(request);
  const [settings, mailboxes]=await Promise.all([request('GET','connections'),request('GET','mailboxes')]); connections=settings.connections; boxes=mailboxes.mailboxes;
  for(const client of settings.client_ids.filter(Boolean))el('client').add(new Option(client,client));
  renderPermissions(); await drafts(); status('Only the mailboxes and actions you enable are available to this AI connection.');
})();
