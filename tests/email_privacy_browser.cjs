const assert=require('node:assert/strict');
const {chromium}=require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.SIMPLEMAIL_CHROMIUM?{executablePath:process.env.SIMPLEMAIL_CHROMIUM}:{})});
 try {
 const page=await browser.newPage();const requests=[];const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('https://tracking.example.invalid/**',r=>{requests.push(r.request().url());return r.fulfill({status:200,contentType:'image/png',body:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7xkAAAAASUVORK5CYII=','base64')});});
 await page.addInitScript(()=>{
 const html='<img src="https://tracking.example.invalid/pixel" srcset="https://tracking.example.invalid/srcset 2x"><div style="background-image:url(https://tracking.example.invalid/css);color:red">Text</div><svg><image href="https://tracking.example.invalid/svg"/></svg><iframe src="https://tracking.example.invalid/frame"></iframe><video poster="https://tracking.example.invalid/poster"><source src="https://tracking.example.invalid/media"></video><link rel="stylesheet" href="https://tracking.example.invalid/style"><script>parent.attack=true</script><img src="javascript:attack()"><a href="javascript:attack()">Bad</a><a href="https://example.invalid">Safe</a>';
 window.pywebview={api:{get_config:async()=>({accounts:[{id:'fixture',label:'Fixture',email:'qa@example.invalid',identity:'qa@example.invalid',has_password:true,has_smtp_password:true}],active_account:'fixture'}),get_folders:async()=>({folders:[{key:'inbox',name:'Inbox',server:'INBOX',unread:0}]}),list_messages:async()=>({envelopes:[],inbox_unread:0}),set_active_account:async()=>{},check_update:async()=>({available:false}),get_message:async(a,f,uid)=>({subject:'Fixture',sender:'qa@example.invalid',text:'Text',html:uid==='plain'?'<p>No remote images</p>':html,attachments:[]})}};
 });
 await page.goto(process.env.SIMPLEMAIL_TEST_URL || require('node:url').pathToFileURL(require('node:path').join(__dirname,'../web/index.html')).href);
 await page.waitForFunction(()=>state.folders.length>0 && api);
 await page.evaluate(()=>openMessage('one'));
 await page.locator('#read-body iframe').waitFor();await page.waitForTimeout(300);
 assert.deepEqual(requests,[],'Default reading must make no remote requests, including during parsing');
 await page.getByRole('button',{name:'Load images',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#read-body iframe').srcdoc.includes('img-src data: https: http:'));
 await page.waitForTimeout(300);
 assert.deepEqual(requests,['https://tracking.example.invalid/pixel']);
 assert.equal(await page.evaluate(()=>Boolean(window.attack)),false);
 const frame=page.frameLocator('#read-body iframe');
 assert.equal(await frame.locator('a').first().getAttribute('href'),null);
 assert.equal(await frame.locator('a').last().getAttribute('rel'),'noopener noreferrer');
 assert.equal(await frame.locator('img').first().getAttribute('referrerpolicy'),'no-referrer');
 await page.evaluate(()=>openMessage('two'));await page.waitForTimeout(300);
 assert.equal(requests.length,1,'Permission must reset for the next message');
 await page.getByRole('button',{name:'Load images',exact:true}).waitFor();
 await page.evaluate(()=>openMessage('plain'));
 assert.equal(await page.getByRole('button',{name:'Load images',exact:true}).count(),0);
 // Detached controls must never grant permission after changing mailbox/view.
 await page.evaluate(async()=>{await openMessage('three');window.oldImages=document.querySelector('#read-body button');await openMessage('plain');oldImages.onclick();});
 assert.equal(requests.length,1);
 await page.evaluate(()=>{api.save_config=async data=>{window.savedAccount=structuredClone(data.accounts[0]);return {ok:true};};});
 await page.evaluate(()=>openSettings());
 assert.equal(await page.locator('#set-password').inputValue(),'');
 assert.match(await page.locator('#set-password').getAttribute('placeholder'),/Saved/);
 await page.locator('#set-password').fill('replacement');
 await page.locator('#settings-cancel').click();
 assert.equal(await page.locator('#set-password').inputValue(),'');
 await page.evaluate(()=>openSettings());
 await page.locator('#set-password').fill('replacement');
 await page.locator('#settings-save').click();
 await page.waitForFunction(()=>!document.querySelector('#settings-backdrop').classList.contains('show'));
 assert.equal(await page.evaluate(()=>savedAccount.password),'replacement');
 assert.equal(await page.locator('#set-password').inputValue(),'');
 assert.equal(await page.evaluate(()=>editAccounts[0].password),'');
 await page.evaluate(()=>openSettings());
 await page.locator('details.advanced summary').click();
 await page.locator('#set-smtp-use-mailbox').check();
 assert.equal(await page.locator('#set-smtp-password').isDisabled(),true);
 await page.locator('#settings-save').click();
 await page.waitForFunction(()=>!document.querySelector('#settings-backdrop').classList.contains('show'));
 assert.equal(await page.evaluate(()=>savedAccount.clear_smtp_password),true);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({defaultRequests:0,explicitImagesOnly:true,nextMessageBlocked:true,staleConsentBlocked:true,noExtraButton:true,unsafeLinksBlocked:true,credentialSettings:true,passed:true}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1});
