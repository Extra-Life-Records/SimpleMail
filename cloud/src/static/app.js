'use strict';
const $ = id => document.getElementById(id);
let cfg, desktop, token, expiry = 0, selected = '', folder = 'Inbox', cursor, revision = 0;
let draftId, draftRevision, replyId, attachments = [], sendId = null, pendingSend = false;
let outstanding = JSON.parse(localStorage.getItem('pending-send') || 'null');
pendingSend=Boolean(outstanding);
const status = text => { $('status').textContent = text; };
const b64url = bytes => btoa(String.fromCharCode(...bytes)).replaceAll('+','-').replaceAll('/','_').replaceAll('=','');
const random = () => b64url(crypto.getRandomValues(new Uint8Array(32)));
async function login() {
  if (desktop) { status('Complete sign-in and MFA in your browser.'); await desktop.cloud_login(); return load(); }
  const verifier=random(), state=random();
  sessionStorage.setItem('pkce', JSON.stringify({verifier,state}));
  const challenge=b64url(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(verifier))));
  const params=new URLSearchParams({response_type:'code',client_id:cfg.client_id,redirect_uri:location.origin+location.pathname,scope:'openid email aws.cognito.signin.user.admin',state,code_challenge:challenge,code_challenge_method:'S256'});
  location.assign(cfg.auth_url+'/oauth2/authorize?'+params);
}
async function access() {
  if (token && Date.now()<expiry) return token;
  const refresh=sessionStorage.getItem('refresh');
  if(!refresh) throw Error('Sign in to continue.');
  const response=await fetch(cfg.auth_url+'/oauth2/token',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'refresh_token',client_id:cfg.client_id,refresh_token:refresh})});
  if(!response.ok){ sessionStorage.removeItem('refresh'); throw Error('Session expired. Sign in again.'); }
  const data=await response.json();token=data.access_token;expiry=Date.now()+(data.expires_in-30)*1000;return token;
}
async function request(method,path,data) {
  if(desktop) return desktop.cloud_request(method,path,data||null);
  const response=await fetch(location.origin+path,{method,headers:{Authorization:'Bearer '+await access(),'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});
  const result=await response.json(); if(!response.ok) throw Error(result.error||'Request failed');return result;
}
function base(){return '/mailboxes/'+selected;}
function guard(fn){return async event=>{try{await fn(event);}catch(e){status(String(e.message||e));}};}
async function load(){
  const result=await request('GET','/mailboxes');
  $('mailbox').replaceChildren();
  for(const box of result.mailboxes){const option=new Option(box.address,box.name);option.dataset.state=box.state;$('mailbox').add(option);}
  $('mail').hidden=!result.mailboxes.length;$('logout').hidden=false;$('login').hidden=true;
  if(!result.mailboxes.length){status('No mailbox is currently assigned to this login.');return;}
  selected=$('mailbox').value;await list();
  if(outstanding){$('check-send').hidden=false;status('A previous send needs its status checked before composing another message.');}
}
async function list(more=false){
  const current=++revision;
  $('assignment').textContent=$('mailbox').selectedOptions[0]?.dataset.state||'';
  if(!more){cursor=null;$('messages').replaceChildren();}
  const query=new URLSearchParams({folder:$('search').value.trim()?'All':folder,q:$('search').value.trim()});
  if(more&&cursor)query.set('cursor',cursor);
  status('Loading…');const page=await request('GET',base()+'/messages?'+query);
  if(current!==revision)return;
  for(const row of page.messages){const button=document.createElement('button');const title=document.createElement('strong');title.textContent=row.subject||'(no subject)';const detail=document.createElement('small');detail.textContent=row.sender+' · '+row.folder+(row.quarantined?' · Quarantined':'')+(row.status?' · '+row.status:'');button.append(title,detail);button.onclick=guard(()=>read(row.id));$('messages').append(button);}
  cursor=page.cursor;$('more').hidden=!cursor;
  status(cursor?'More messages are available. Load more to continue searching.':'Mailbox up to date.');
}
async function read(id){
  const box=selected, current=++revision;
  const row=await request('GET',base()+'/messages/'+id);
  if(box!==selected||current!==revision)return;
  if(row.draft){edit(row.draft);draftId=row.id;draftRevision=row.revision;return;}
  $('editor').hidden=true;$('reading').hidden=false;$('reading').replaceChildren();
  const title=document.createElement('h1');title.textContent=row.subject||'(no subject)';
  const sender=document.createElement('p');sender.textContent=row.sender+' · '+row.date;
  const text=document.createElement('pre');text.textContent=row.body;
  const actions=document.createElement('div');actions.className='actions';
  const reply=document.createElement('button');reply.textContent='Reply';reply.onclick=()=>{edit({to:row.reply_to,subject:/^re:/i.test(row.subject)?row.subject:'Re: '+row.subject,body:'\n\n'+row.body.split('\n').map(s=>'> '+s).join('\n')});replyId=id;};actions.append(reply);
  for(const target of ['Inbox','Junk','Trash'].filter(f=>f!==row.folder)){const b=document.createElement('button');b.textContent='Move to '+target;b.onclick=guard(async()=>{await request('PATCH',base()+'/messages/'+id,{folder:target});$('reading').replaceChildren();await list();});actions.append(b);}
  $('reading').append(title,sender,actions,text);
  for(const part of row.attachments){const b=document.createElement('button');b.textContent='Download '+part.name;b.onclick=guard(async()=>{const file=await request('GET',base()+'/messages/'+id+'/attachments/'+part.part);if(desktop){await desktop.cloud_save_attachment(file);return;}const bytes=Uint8Array.from(atob(file.data),c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([bytes],{type:'application/octet-stream'}));const a=document.createElement('a');a.href=url;a.download=file.name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});$('reading').append(b);}
  await request('PATCH',base()+'/messages/'+id,{seen:true});
}
function edit(data={}){
  if(pendingSend)throw Error('Resolve the previous send outcome before starting another message.');
  draftId=null;draftRevision=null;replyId=null;sendId=null;attachments=data.attachments||[];
  for(const k of ['to','cc','bcc','subject','body'])$(k).value=data[k]||'';
  $('attached').textContent=attachments.map(a=>a.name).join(', ');$('files').value='';
  $('editor').hidden=false;$('reading').hidden=true;
}
function payload(){return Object.fromEntries(['to','cc','bcc','subject','body'].map(k=>[k,$(k).value]));}
async function send(event){
  event.preventDefault();
  if(pendingSend)throw Error('Send outcome is uncertain. Refresh Sent or ask your administrator; do not resend.');
  sendId=sendId||crypto.randomUUID();pendingSend=true;status('Sending…');
  const box=selected;
  try{
    const saved=await request('POST',base()+'/drafts',{...payload(),attachments,id:draftId,revision:draftRevision});draftId=saved.id;draftRevision=saved.revision;
    outstanding={mailbox:box,request_id:sendId,draft_id:draftId};localStorage.setItem('pending-send',JSON.stringify(outstanding));$('check-send').hidden=false;
    const result=await request('POST',base()+'/send',{...payload(),attachments,request_id:sendId,reply_id:replyId});
    if(result.status!=='accepted'){status('Send status: '+result.status+'. Check with your administrator before retrying.');return;}
    pendingSend=false;outstanding=null;localStorage.removeItem('pending-send');$('check-send').hidden=true;$('editor').hidden=true;$('reading').hidden=false;
    if(draftId)await request('PATCH','/mailboxes/'+box+'/messages/'+draftId,{folder:'Trash'});
    await list();status('Accepted by the mail provider. Delivery is not yet confirmed.');
  }catch(e){if(!outstanding)pendingSend=false;status(outstanding?'Send outcome may be uncertain. Check Sent before retrying. '+String(e.message||e):'Draft could not be saved; no send was started. '+String(e.message||e));}
}
async function boot(){
  desktop=window.pywebview?.api;
  if(desktop){cfg=await desktop.cloud_config();if(!cfg.configured){$('setup').hidden=false;return;}}
  else{
    cfg=await(await fetch('config.json')).json();
    const params=new URLSearchParams(location.search);
    if(params.has('code')){
      const saved=JSON.parse(sessionStorage.getItem('pkce')||'null');
      if(!saved||saved.state!==params.get('state'))throw Error('Login state mismatch; start sign-in again.');
      const code=params.get('code');history.replaceState({},'',location.pathname);sessionStorage.removeItem('pkce');
      const response=await fetch(cfg.auth_url+'/oauth2/token',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({grant_type:'authorization_code',client_id:cfg.client_id,redirect_uri:location.origin+location.pathname,code,code_verifier:saved.verifier})});
      if(!response.ok)throw Error('Sign-in exchange failed.');const result=await response.json();sessionStorage.setItem('refresh',result.refresh_token);token=result.access_token;expiry=Date.now()+(result.expires_in-30)*1000;
    }
  }
  if(desktop||sessionStorage.getItem('refresh'))await load();
}
$('login').onclick=guard(login);
$('check-send').onclick=guard(async()=>{
  if(!outstanding)return;
  const result=await request('GET','/mailboxes/'+outstanding.mailbox+'/sends/'+outstanding.request_id);
    if(['accepted','delivered','bounced','complained','rejected','failed','not_started'].includes(result.status)){
    if(['accepted','delivered'].includes(result.status))await request('PATCH','/mailboxes/'+outstanding.mailbox+'/messages/'+outstanding.draft_id,{folder:'Trash'});
    pendingSend=false;outstanding=null;localStorage.removeItem('pending-send');$('check-send').hidden=true;
    status(result.status==='accepted'?'Provider accepted the previous send.': 'Previous send '+result.status+'. Your saved draft is available.');
  }else status('Previous send remains uncertain. Ask your administrator to reconcile it before retrying.');
});
$('logout').onclick=guard(async()=>{if(desktop)await desktop.cloud_logout();else{const refresh=sessionStorage.getItem('refresh');sessionStorage.removeItem('refresh');token=null;if(refresh)await fetch(cfg.auth_url+'/oauth2/revoke',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams({token:refresh,client_id:cfg.client_id})});}location.reload();});
$('connect').onclick=guard(async()=>{await desktop.cloud_configure({api_url:$('api-url').value,auth_url:$('auth-url').value,client_id:$('client-id').value});location.reload();});
$('mailbox').onchange=guard(async()=>{if(pendingSend){$('mailbox').value=selected;throw Error('Resolve the uncertain send before switching mailboxes.');}selected=$('mailbox').value;$('editor').hidden=true;$('reading').hidden=false;$('reading').replaceChildren();await list();});
for(const name of ['Inbox','Sent','Drafts','Junk','Trash']){const button=document.createElement('button');button.textContent=name;button.onclick=guard(async()=>{folder=name;$('search').value='';for(const b of $('folders').children)b.classList.toggle('active',b===button);await list();});$('folders').append(button);}
$('refresh').onclick=guard(()=>list());$('more').onclick=guard(()=>list(true));$('search').onchange=guard(()=>list());$('compose').onclick=guard(()=>edit());$('cancel').onclick=()=>{$('editor').hidden=true;$('reading').hidden=false;};
$('editor').onsubmit=guard(send);
$('save').onclick=guard(async()=>{if(pendingSend)throw Error('Resolve the send outcome first.');const result=await request('POST',base()+'/drafts',{...payload(),attachments,id:draftId,revision:draftRevision});draftId=result.id;draftRevision=result.revision;status('Draft saved.');});
$('files').onchange=guard(async()=>{const files=[...$('files').files];if(files.reduce((n,f)=>n+f.size,0)>3*1024*1024)throw Error('Attachments exceed 3 MB total.');attachments=[];for(const f of files){const bytes=new Uint8Array(await f.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.subarray(i,i+8192));attachments.push({name:f.name,data:btoa(binary)});}$('attached').textContent=attachments.map(a=>a.name).join(', ');});
if(location.protocol==='file:'||['localhost','127.0.0.1','::1'].includes(location.hostname)){
  let started=false;
  const start=()=>{if(started||!window.pywebview?.api)return;started=true;guard(boot)();};
  window.addEventListener('pywebviewready',start);start();
}else guard(boot)();
