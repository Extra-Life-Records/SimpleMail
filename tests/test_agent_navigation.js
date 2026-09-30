const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const path=require('node:path');
function app() {
 const elements=new Map();
 const element=id=>{if(!elements.has(id)) {const classes=new Set();elements.set(id,{dataset:{},value:'',checked:false,textContent:'',classList:{add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),toggle:(x,on)=>on?classes.add(x):classes.delete(x)},addEventListener(){},setAttribute(){},focus(){},querySelectorAll:()=>[]});}return elements.get(id);};
 const context=vm.createContext({$:element,document:{body:element('body'),querySelectorAll:()=>[],addEventListener(){}},state:{currentFolder:'inbox'},activeAccount:()=>({id:'one',label:'Mailbox'}),api:{},toast(){},setInterval(){},confirm:()=>false,selectFolder(){}});
 const run=code=>vm.runInContext(code,context);
 run(fs.readFileSync(path.join(__dirname,'../web/agent.js'),'utf8'));
 // Rendering itself is exercised by the real-browser fixture; these tests target routing races and edit guards.
 run('renderAgent=()=>{}');
 return {run,api:run('api'),element};
}
test('leaving a pending agent view invalidates its late response',async()=>{
 const a=app();let resolve;a.api.get_agent_state=()=>new Promise(yes=>resolve=yes);
 const loading=a.run('openAgent("drafts")');
 assert.equal(a.run('agentVisible()'),true);
 assert.equal(a.run('leaveAgentWorkspace()'),true);
 resolve({profile:{}});await loading;
 assert.equal(a.run('agentView'),null);assert.equal(a.run('agentVisible()'),false);
});
test('latest workspace request wins when responses arrive out of order',async()=>{
 const a=app();const resolves=[];a.api.get_agent_state=()=>new Promise(yes=>resolves.push(yes));
 const older=a.run('openAgent("drafts")');const newer=a.run('openAgent("activity")');
 resolves[1]({marker:'new'});await newer;resolves[0]({marker:'old'});await older;
 assert.equal(a.run('agentView.tab'),'activity');assert.equal(a.run('agentView.data.marker'),'new');
});
test('busy actions and rejected draft discard keep the current workspace',async()=>{
 const a=app();a.api.get_agent_state=async()=>({});await a.run('openAgent("drafts")');
 a.run('agentView.busy=true');assert.equal(a.run('leaveAgentWorkspace()'),false);
 await a.run('openAgent("activity")');assert.equal(a.run('agentView.tab'),'drafts');
 a.run('agentView.busy=false;agentView.draft={payload:{to:"saved"}}');a.element('agent-edit-to').value='edited';
 assert.equal(a.run('leaveAgentWorkspace()'),false);assert.equal(a.run('agentVisible()'),true);
 a.run('confirm=()=>true');assert.equal(a.run('leaveAgentWorkspace()'),true);
});
