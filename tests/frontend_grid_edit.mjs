import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {randomUUID,randomFillSync} from 'node:crypto';
import vm from 'node:vm';

const nodes=new Map(),storage=new Map(),requests=[];
const element=()=>({innerHTML:'',textContent:'',value:'',focus(){},setAttribute(){},removeAttribute(){}});
for(const selector of ['#app','#grid-error','#table-view-name','#current-time','#owner-btn'])nodes.set(selector,element());
const initial={current_month:'2026-10',plan_end:'2027-12',now:'2026-10-29',meta:{},settings:{generation_mode:'detail'},generation_view:{mode:'detail',rule_signature:'abc'},
  products:[{id:'a',brand:'享界',model:'S9 2025款',catalog_model:'S9',source_generation:'享界 S9 2025款',version:'Max',revision:3},
    {id:'b',brand:'享界',model:'S9 2026款',catalog_model:'S9',source_generation:'享界 S9 2026款',version:'Ultra',revision:2},
    {id:'sum',brand:'智界',model:'合计 2026款',version:'Max',read_only:true,revision:0}],
  plans:[{product_id:'a',month:'2026-10',sop:10,forecast:88,revision:4},{product_id:'b',month:'2026-10',sop:20,revision:5},{product_id:'b',month:'2027-12',sop:null,revision:6}],
  sales:[],model_sales:[],shipments:[],workbook_sync:{status:'absent'}};
let response=async()=>({ok:true,status:200,json:async()=>({ok:true,saved_count:1,created_count:1,deactivated_count:1})});
const context=vm.createContext({document:{querySelector:s=>nodes.get(s)||null,querySelectorAll:()=>[]},
  localStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v)},crypto:{randomUUID,getRandomValues:array=>randomFillSync(array)},clearTimeout(){},initial,
  fetch:async(path,options)=>{requests.push({path,body:JSON.parse(options.body)});return response();}});
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
vm.runInContext(source.split("document.addEventListener('click'")[0],context);
const run=code=>vm.runInContext(code,context),plain=value=>JSON.parse(JSON.stringify(value));
run("let searchTimer;state=initial;owner='测试';indexData();toast=()=>{};openDialog=()=>{throw Error('unexpected modal')};");
// HTTP intranet origins may expose getRandomValues but not randomUUID.
run('crypto.randomUUID=undefined');
assert.match(run('gridUUID()'),/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
run('planning()');
assert.match(nodes.get('#app').innerHTML,/class="button primary small" data-action="bulk-plan"[^>]*>网页维护/);
assert.equal((nodes.get('#app').innerHTML.match(/data-action="bulk-plan"/g)||[]).length,1);
assert.match(nodes.get('#app').innerHTML,/创建Excel（可选）/);
assert.match(nodes.get('#app').innerHTML,/网页直接保存 · Excel可选/);
assert.doesNotMatch(nodes.get('#app').innerHTML,/data-action="sync-workbook"/);
nodes.get('#table-view-name').value='初始视图';run('saveTableView()');
const originalView=JSON.parse(storage.get('sop-table-views-v1')).views[0];

run('bulkPlan()');
assert.match(nodes.get('#app').innerHTML,/data-grid-month="2026-10"/);
assert.match(nodes.get('#app').innerHTML,/合计在保存后更新/);
assert.match(nodes.get('#app').innerHTML,/车型名称/);
assert.deepEqual(plain(run('[...gridDraft.rows.keys()]')),['a','b']);
await assert.rejects(run('load()'),/保存或取消/);
run("gridRow('a').values['2026-10']='0';gridRow('b').values['2026-10']='';gridRow('a').version='Max 更新';");
let payload=plain(run('gridPayload()'));
assert.deepEqual(payload.changes,[{product_id:'a',month:'2026-10',sop:0,revision:4},{product_id:'b',month:'2026-10',sop:null,revision:5}]);
assert.equal(payload.catalog_changes[0].version,'Max 更新');
assert.equal(payload.changes[0].forecast,undefined);
assert.equal(payload.catalog_changes[0].model,undefined,'The visible year must not rename the source model');

run("gridRemoveRow('b');gridAddRow('享界','S9 2026款','享界|S9 2026款');const added=[...gridDraft.rows.values()].find(r=>r.isNew);added.version='新增';added.values['2026-10']='3';");
payload=plain(run('gridPayload()'));
assert.deepEqual(payload.catalog_changes.find(c=>c.id==='b').plan_revisions,{'2026-10':5,'2027-12':6});
assert.equal(payload.changes.some(c=>c.product_id==='b'),false,'Deleted rows must not submit changed plans');
assert.equal(payload.entries[0].model,'S9 2026款');
assert.equal(payload.entries[0].plans[0].sop,3);
run("gridRow('a').values['2026-10']='-1'");
assert.throws(()=>run('gridPayload()'),/非负整数/);
run("gridRow('a').values['2026-10']='0'");

// An uncertain request locks the draft and retries exactly the same payload.
response=async()=>{throw new Error('network lost');};
await run('saveGrid()');
assert.equal(run('gridDraft.uncertain'),true);
const first=requests.at(-1).body;
run('gridAddRow()');
assert.equal(run('[...gridDraft.rows.values()].filter(r=>r.isNew).length'),1);
response=async()=>({ok:true,status:200,json:async()=>({ok:true,saved_count:1,created_count:1,deactivated_count:1,workbook_sync:{status:'pending'}})});
run('load=async()=>{}');
await run('saveGrid()');
assert.deepEqual(requests.at(-1).body,first);
assert.equal(run('gridDraft'),null);
assert.equal(run("rowHidden('version','b')"),true);
assert.equal(initial.plans[0].sop,10,'Draft values must not mutate persisted state');
assert.equal(JSON.parse(storage.get('sop-hidden-rows-v1'))['["detail","version","b"]'],'S9 2026款 · Ultra（已停用）');
assert.deepEqual(JSON.parse(storage.get('sop-table-views-v1')).views[0],originalView,'Deletion must not change the named view snapshot');
run('hiddenRows={};viewsRestored=false;restoreInitialView()');
assert.equal(run("rowHidden('version','b')"),true,'Deletion hiding must survive reload even when the active named view predates it');
run("delete hiddenRows[rowViewKey('version','b')];refreshHiddenRows();hiddenRows={};viewsRestored=false;restoreInitialView()");
assert.equal(run("rowHidden('version','b')"),false,'Restoring an inactive row must survive reload');
assert.equal(initial.plans[1].sop,20,'Restoring visibility must preserve historical data');

// Named view stores hiding and filters; totals and export source remain intact.
run("hiddenRows={};hideRow('version','a','S9 Max');range='annual';planYear='2026';brand='享界';");
nodes.get('#table-view-name').value='重点车型';run('saveTableView()');
assert.equal(JSON.parse(storage.get('sop-table-views-v1')).views.length,2);
run("hiddenRows={};range='rolling';brand='';applyTableView()");
assert.equal(run("rowHidden('version','a')"),true);
assert.equal(run('range'),'annual');
assert.equal(run('brand'),'享界');
assert.equal(run("sumPlan(products(),'2026-10','sop').value"),30);
run("state.generation_view.mode='summary'");
assert.throws(()=>run('applyTableView()'),/切换代际模式/);
run("state.generation_view.mode='detail';hiddenRows={};viewsRestored=false;restoreInitialView()");
assert.equal(run("rowHidden('version','a')"),true);
run('hiddenRows={};refreshHiddenRows();viewsRestored=false;restoreInitialView()');
assert.equal(run("rowHidden('version','a')"),false,'An explicitly empty working view must override an older named hiding snapshot');
assert.equal(run('range'),'annual','Working visibility must not discard saved view filters');
run('applyTableView()');
assert.equal(run("rowHidden('version','a')"),true,'Explicitly applying the named view must restore its unchanged snapshot');
assert.equal(JSON.parse(storage.get('sop-hidden-rows-v1'))['["detail","version","a"]'],'S9 Max');
run("state={...initial,products:[],plans:[]};gridDraft=null;hiddenRows={};brand='';model='';query='';range='rolling';indexData();planning();bulkPlan();gridAddRow();");
assert.equal(run('gridDraft.rows.size'),1,'An empty catalog must allow adding the first row without a workbook');
assert.match(nodes.get('#app').innerHTML,/无需创建Excel/);
assert.match(nodes.get('#app').innerHTML,/data-new-row=/);
assert.equal(requests.some(r=>r.path.startsWith('/api/plan-workbook/')),false);
console.log('PASS: direct webpage maintenance without Excel, empty-catalog row creation, inline edits, atomic payloads, uncertain retry, readonly rows, persistent hiding and saved views.');
