import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const element=()=>({innerHTML:'',textContent:'',value:'detail',disabled:false,inert:false,attrs:{},focusCount:0,
  setAttribute(k,v){this.attrs[k]=v;},removeAttribute(k){delete this.attrs[k];},focus(){this.focusCount++;}});
const app=element(),control=element(),scroll={scrollLeft:91,scrollTop:12},dialog={open:false};
const nodes=new Map([['#app',app],['#generation-filter',control],['#current-time',element()],['#page-title',element()],['#edit-dialog',dialog]]);
const requests=[],toasts=[];
const base={current_month:'2026-10',plan_end:'2027-12',now:'2026-10-28T12:00:00+08:00',
  meta:{},products:[{id:'member',brand:'问界',model:'M9',version:'Max',active:true}],
  plans:[],sales:[],model_sales:[],settings:{generation_mode:'detail'},
  generation_view:{mode:'detail',rule_signature:'first',warnings:[]},workbook_sync:{status:'absent'}};
let serverState=structuredClone(base),status={status:'absent',generation_view:serverState.generation_view};
let post,reads=0,rejectRead=false;
const context=vm.createContext({localStorage:{getItem:()=>null},
  document:{hidden:false,querySelector:s=>nodes.get(s)||null,querySelectorAll:s=>s==='.table-scroll'?[scroll]:[]},
  requestAPI:async(path,body)=>{requests.push({path,body});
    if(path==='/api/settings')return post(body);
    if(path==='/api/plan-workbook/status')return status;
    if(path==='/api/state'){reads++;if(rejectRead)throw new Error('配置无法读取');return structuredClone(serverState);}
    throw new Error(path);},
  notify:(text,error)=>toasts.push({text,error}),
});
vm.runInContext(source.split("document.addEventListener('click'")[0],context);
const run=s=>vm.runInContext(s,context);
context.initialState=structuredClone(base);
run('state=initialState;api=requestAPI;toast=notify;indexData();planning()');
assert.match(app.innerHTML,/id="generation-filter"[^]*?value="detail" selected[^]*?value="summary"/);
assert.equal(run("exportScope({allProducts:true})"),'全部品牌 · 全部车型 · 单代际');

// Mode changes serialize edits and only render the server's replacement scope.
let release;
post=()=>new Promise(resolve=>{release=resolve;});
run("model='M9';query='Max';selected='member';matchProduct='member'");
context.control=control;
const changing=run("changeGenerationMode('summary',control)");
assert.equal(control.disabled,true);assert.equal(app.inert,true);assert.equal(app.attrs['aria-busy'],'true');
await run("changeGenerationMode('summary',control)");
assert.equal(requests.filter(r=>r.path==='/api/settings').length,1);
serverState={...structuredClone(base),settings:{generation_mode:'summary'},
  generation_view:{mode:'summary',rule_signature:'first',warnings:['部分成员暂无销量']},
  products:[{id:'group',brand:'问界',model:'F2N',version:'Max',read_only:true,member_product_ids:['member']},
            {id:'unrelated',brand:'智界',model:'R7',version:'Pro',active:true}],
  plans:[{product_id:'group',month:'2026-10',sop:300}]};
release({ok:true});await changing;
assert.equal(control.disabled,false);assert.equal(app.inert,false);assert.equal(control.value,'summary');
assert.equal(control.focusCount,1);assert.equal(run('model'),'');assert.equal(run('matchProduct'),'');
assert.match(app.innerHTML,/汇总代际计划由成员单代际自动相加，只读/);
assert.match(app.innerHTML,/部分成员暂无销量/);
assert.equal(run("canEditProduct(state.products[0],'2026-10')"),false);
assert.equal(run("canEditProduct(state.products[1],'2026-10')"),true);
const row=run("planVersionRows([state.products[0]],['2026-10'])");
assert.doesNotMatch(row,/data-edit=/);assert.match(row,/自动汇总 · 只读/);
assert.equal(run("exportScope({allProducts:true})"),'全部品牌 · 全部车型 · 汇总代际');
assert.doesNotMatch(run("editorOptions('group')"),/value="group"/);

// A derived zero with a missing member is known-only, not a complete zero/year.
run("yearMonths('2026').forEach(month=>planIndex.set(key('group',month),{sop:0,partial:month==='2026-10',known_members:month==='2026-10'?1:2,total_members:2}))");
assert.equal(run("sumPlan([state.products[0]],'2026-10','sop').value"),0);
assert.match(run("quantity(sumPlan([state.products[0]],'2026-10','sop'))"),/仅已知项合计，仍有缺项/);
assert.equal(run("annualQuantity([state.products[0]],yearMonths('2026'),'sop').complete"),false);
assert.match(run("annualCell([state.products[0]],yearMonths('2026'),'sop')"),/12\/12 月 · 缺项/);
assert.doesNotMatch(run("annualCell([state.products[0]],yearMonths('2026'),'sop')"),/数据齐全/);
run('indexData()');
const calls=requests.length;
await run("editPlan('group','2026-10')");
assert.equal(requests.length,calls);assert.match(toasts.at(-1).text,/只读/);

// A rejected write reloads the authoritative mode, retaining the original table.
post=async()=>{throw new Error('先处理计划表同步冲突');};
await run("changeGenerationMode('detail',control)");
assert.equal(control.value,'summary');assert.equal(run('generationMode()'),'summary');
assert.match(toasts.at(-1).text,/同步冲突/);assert.equal(app.inert,false);

// Polling detects live rules even when there is no maintenance file.
status={status:'absent',generation_view:{...serverState.generation_view,rule_signature:'second'}};
serverState.generation_view=status.generation_view;
const before=reads;
await run('pollWorkbookStatus()');
assert.equal(reads,before+1);assert.equal(run('state.generation_view.rule_signature'),'second');
assert.equal(scroll.scrollLeft,91);assert.equal(scroll.scrollTop,12);
await run('pollWorkbookStatus()');assert.equal(reads,before+1,'Unchanged rule/status does not repeatedly reload.');
status={status:'error',generation_view:{mode:'summary',rule_signature:'',error:'汇总规则文件不存在'}};
await run('pollWorkbookStatus()');
assert.match(app.innerHTML,/代际范围暂不可用/);assert.match(app.innerHTML,/汇总规则文件不存在/);
assert.throws(()=>run("buildPlanExport({selectedMonths:['2026-10']})"),/规则文件不存在/);
status={status:'absent',generation_view:{mode:'summary',rule_signature:'third'}};
serverState.generation_view=status.generation_view;
await run('pollWorkbookStatus()');
assert.doesNotMatch(app.innerHTML,/代际范围暂不可用/);
assert.equal(run('generationViewError'),'');

// If response recovery also fails, old values are no longer editable/exportable.
rejectRead=true;
await run("changeGenerationMode('detail',control)");
assert.match(app.innerHTML,/无法确认当前代际范围/);
assert.equal(app.inert,false);assert.equal(control.disabled,false);
assert.throws(()=>run("buildPlanExport({selectedMonths:['2026-10']})"),/无法确认/);
console.log('PASS: generation selector, serialized switching, read-only projections, rule reload/error recovery and export scope.');
