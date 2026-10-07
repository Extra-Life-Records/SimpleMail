const {chromium}=require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
const {pathToFileURL}=require('node:url');
const path=require('node:path');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.SIMPLEMAIL_CHROMIUM?{executablePath:process.env.SIMPLEMAIL_CHROMIUM}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:800}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   window.calls=[];
   window.pywebview={api:{cloud_config:async()=>({configured:true}),cloud_request:async(method,path,data)=>{
    calls.push({method,path,data});
    if(path==='/mailboxes')return {mailboxes:[{name:'one',address:'one@playloudr.com',state:'Reserved'},{name:'two',address:'two@playloudr.com',state:'Reserved'}]};
    if(path.includes('/sends/'))return {status:'accepted'};
    if(path.endsWith('/send'))throw Error('connection lost');
    if(path.endsWith('/drafts'))return {id:'a'.repeat(32)};
    if(method==='PATCH')return {ok:true};
    if(path.includes('/messages?'))return {messages:[{id:'abc123',subject:'A real message',sender:'Person',folder:'Inbox'}],cursor:null};
    return {subject:'A real message',sender:'Person',date:'Today',reply_to:'person@example.com',body:'<img src="https://tracker.invalid/pixel"> untrusted',attachments:[],folder:'Inbox'};
   }}};
   window.addEventListener('DOMContentLoaded',()=>window.dispatchEvent(new Event('pywebviewready')));
  });
  await page.goto(pathToFileURL(path.resolve('cloud/src/static/index.html')).href);
  await page.getByText('A real message',{exact:true}).click();
  await page.getByText('<img src="https://tracker.invalid/pixel"> untrusted',{exact:true}).waitFor();
  assert.equal(await page.locator('#reading img').count(),0);
  if(process.env.SIMPLEMAIL_CLOUD_SCREENSHOT)await page.screenshot({path:process.env.SIMPLEMAIL_CLOUD_SCREENSHOT,fullPage:true});
  await page.getByRole('button',{name:'Reply',exact:true}).click();
  assert.equal(await page.locator('#to').inputValue(),'person@example.com');
  await page.locator('#body').fill('Reply body');await page.getByRole('button',{name:'Send',exact:true}).click();
  await page.getByText(/Send outcome may be uncertain/).waitFor();
  await page.reload();await page.getByRole('button',{name:'Check previous send'}).waitFor();
  await page.getByRole('button',{name:'New message'}).click();await page.getByText(/Resolve the previous send outcome/).waitFor();
  await page.getByRole('button',{name:'Check previous send'}).click();
  await page.getByText('Provider accepted the previous send.',{exact:true}).waitFor();
  assert.equal(await page.evaluate(()=>calls.filter(c=>c.path.endsWith('/send')).length),0);
  await page.getByRole('button',{name:'New message'}).click();await page.locator('#editor').waitFor();
  assert.deepEqual(errors,[]);console.log('Cloud inbox: safe rendering, reply, draft-before-send, uncertain-send reload recovery passed');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
