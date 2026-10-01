const {chromium}=require(process.env.SIMPLEMAIL_PLAYWRIGHT_MODULE || 'playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.SIMPLEMAIL_CHROMIUM?{executablePath:process.env.SIMPLEMAIL_CHROMIUM}:{})});
 const page=await browser.newPage({viewport:{width:1100,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{
  const clone=x=>JSON.parse(JSON.stringify(x));
  const profile={enabled:false,allow_filing:false,job:'Prepare replies for review',mode:'draft_for_review',allowed_recipients:[]};
  let filings={items:[],next_cursor:null};
  window.showFiling=()=>filings={items:[{id:'confirmed-move',status:'moved',source:{folder:'INBOX'},target:'Archive',reason:'Newsletter <img src=x onerror=attack()>'},{id:'uncertain-move',status:'uncertain',source:{folder:'INBOX'},target:'Archive',reason:'Interrupted move',warning:'Filing needs checking'}],next_cursor:'older'};
  let model={configured:false,has_key:false,active:false,status:'stopped',endpoint:'',model:'',api:'responses',detail:''};
  const baseReview={id:3,updated_at:'revision-one',headers:{subject:'Question <img src=x onerror=attack()>',sender:'customer@example.invalid'},note:'Missing facts <script>attack()</script>',draft_id:null,draft_status:null}; let review={...baseReview}; window.restoreUncertain=()=>review={...baseReview,draft_id:'uncertain-draft',draft_status:'uncertain'}; window.pywebview={api:{get_config:async()=>({accounts:[{id:'fixture',label:'Enquiries',email:'qa@example.invalid',identity:'qa@example.invalid',color:'#2563eb'}],active_account:'fixture'}),
   get_folders:async()=>({folders:[{key:'inbox',server:'INBOX',name:'Inbox',unread:0}]}),list_messages:async()=>({envelopes:[],inbox_unread:0,folder_unread:0}),check_update:async()=>({available:false}),set_active_account:async()=>{},
   get_agent_state:async()=>clone({profile,model,reviews:{items:review?[review]:[],next_cursor:null},drafts:{items:[],next_cursor:null},activity:{items:[],next_cursor:null},filings}),
   undo_filing:async(account,id)=>{window.qaUndo={account,id,enabled:profile.enabled};filings.items[0].status='undone';return {status:'undone'};},list_filing_actions:async(account,cursor)=>{window.qaPage={account,cursor};return {items:[{id:'older-move',status:'undone',source:{folder:'Archive'},target:'INBOX',reason:'Earlier filing'}],next_cursor:null};},
   get_agent_review:async()=>clone({work:review,message:{subject:review.headers.subject,sender:review.headers.sender,text:'Incoming <img src=x onerror=attack()> asks for order details.',truncated:false},conversation:{state:'waiting',note:'Awaiting details <img src=x onerror=attack()>',revision:1,untrusted_context:true}}), resolve_agent_review:async(account,id,rev,action)=>{window.qaDecision={account,id,rev,action,enabled:profile.enabled};review=null;return {status:action==='retry'?'pending':'handled'};}, get_model_state:async()=>clone(model),list_agent_activity:async()=>({items:[],next_cursor:null}),
   save_model_connection:async(account,endpoint,name,api,key,clear)=>{assertOwner=account;model={...model,endpoint,model:name,api,has_key:Boolean(key)||model.has_key,configured:true};return clone(model);},
   save_agent_settings:async(account,enabled,job,mode,allowed,allow_filing)=>{Object.assign(profile,{enabled,job,mode,allowed_recipients:allowed,allow_filing});if(!enabled)model={...model,active:false,status:'stopped'};return clone(profile);},
   start_model_worker:async()=>{model={...model,active:true,status:'idle'};return clone(model);}
  }};
 });
 await page.goto(require('node:url').pathToFileURL(require('node:path').join(__dirname,'../web/index.html')).href);
 await page.getByRole('button',{name:'Agent setup',exact:true}).click();
 await page.locator('#agent-job').waitFor();
 assert.equal(await page.locator('#agent-allow-filing').isChecked(),false);
 await page.locator('#needs-you-btn').click();
 await page.getByRole('button',{name:'Review message',exact:true}).click();
 await page.getByRole('button',{name:'Ask AI to try again',exact:true}).waitFor();
 assert.equal(await page.locator('#agent-content img').count(),0);
 assert.equal(await page.locator('#agent-content script').count(),0);
 await page.getByRole('button',{name:'Ask AI to try again',exact:true}).click();
 await page.locator('#agent-content').filter({hasText:'Nothing needs your attention'}).waitFor();
 const decision=await page.evaluate(()=>qaDecision);assert.equal(decision.action,'retry');assert.equal(decision.enabled,false);assert.equal(decision.rev,'revision-one');
 await page.evaluate(()=>restoreUncertain());
 await page.locator('#needs-you-btn').click();
 await page.getByRole('button',{name:'Review message',exact:true}).click();
 await page.getByRole('button',{name:'Mark handled',exact:true}).waitFor();
 assert.equal(await page.locator('#agent-review-retry').count(),0);
 assert.equal(await page.getByText('Conversation notes · waiting',{exact:true}).count(),1);
 assert.equal(await page.locator('#agent-content img').count(),0);
 await page.locator('#inbox-btn').click();
 assert.equal(await page.locator('#list-pane').isVisible(),true);
 await page.locator('#agent-btn').click();
 await page.locator('#agent-job').fill('Unsaved job');
 page.once('dialog',d=>d.dismiss());
 await page.locator('#activity-btn').click();
 assert.equal(await page.locator('#agent-job').inputValue(),'Unsaved job');
 page.once('dialog',d=>d.accept());
 await page.locator('#activity-btn').click();
 assert.equal(await page.locator('#list-pane').isVisible(),false);
 assert.equal(await page.locator('#activity-btn').getAttribute('aria-current'),'page');
 await page.evaluate(()=>showFiling());
 await page.locator('#inbox-btn').click();
 await page.locator('#activity-btn').click();
 assert.equal(await page.getByRole('button',{name:'Undo move',exact:true}).count(),1);
 assert.equal(await page.locator('#agent-content img').count(),0);
 await page.getByRole('button',{name:'More filing',exact:true}).click();
 await page.getByText('Earlier filing',{exact:true}).waitFor();
 assert.deepEqual(await page.evaluate(()=>qaPage),{account:'fixture',cursor:'older'});
 await page.getByRole('button',{name:'Undo move',exact:true}).click();
 await page.getByText('INBOX → Archive · Restored',{exact:true}).waitFor();
 assert.deepEqual(await page.evaluate(()=>qaUndo),{account:'fixture',id:'confirmed-move',enabled:false});
 assert.equal(await page.getByRole('button',{name:'Undo move',exact:true}).count(),0);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({ownerReview:true,filingUndoWhilePaused:true,uncertainUndoBlocked:true,filingPagination:true,filingDefaultOff:true,conversationNotesEscaped:true,untrustedTextEscaped:true,revisionBoundAction:true,retryKeepsPaused:true,uncertainRetryBlocked:true,errors,passed:true}));
 await browser.close();
})().catch(e=>{console.error(e);process.exitCode=1});
