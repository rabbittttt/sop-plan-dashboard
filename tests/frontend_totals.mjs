import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const context = vm.createContext({ localStorage: { getItem: () => '' } });
vm.runInContext(source.split("document.addEventListener('click'")[0], context);
vm.runInContext(`
  state = { products: [{id:'a',generation:'g'},{id:'b',generation:'g'}],
    model_sales: [{generation:'g',month:'2026-08',metric:'locked_orders',value:200}],
    current_month:'2026-10', plan_end:'2027-12', sales:[], history_months:['2026-08','2026-09'],months:['2026-10'] };
  metric = 'locked_orders';
`, context);
const get = expression => JSON.parse(JSON.stringify(vm.runInContext(expression, context)));
assert.deepEqual(get("sumSales(state.products,'2026-08')"),
  {value:200,known:2,total:2,model_reference:true});
assert.deepEqual(get("sumPlan(state.products,'2026-08','sop')"),
  {value:null,known:0,total:2});
vm.runInContext("planIndex.set('a|2026-08',{sop:0,forecast:80})", context);
assert.deepEqual(get("sumPlan(state.products,'2026-08','sop')"),
  {value:0,known:1,total:2});
vm.runInContext("planIndex.set('b|2026-08',{sop:50,forecast:90})", context);
assert.deepEqual(get("sumPlan(state.products,'2026-08','sop')"),
  {value:50,known:2,total:2});
assert.deepEqual(get("sumPlan(state.products,'2026-08','forecast')"),
  {value:170,known:2,total:2});
assert.equal(get("sumSales([state.products[0]],'2026-08').value"), null);
assert.equal(get("sumPlan(state.products,'2026-09','sop').value"), null);
const row = get("planRow(state.products,'合计','total','',['2026-08'])");
assert.match(row, /history-cell number[^]*?200/);
assert.match(row, /sop-cell number past-month">50<\/td>/);
assert.doesNotMatch(row, /<sup\b|†/);
// All version locks precede all version plans, and edits retain the exact version/month.
vm.runInContext(`
  state.products = [
    {id:'a',generation:'g',model:'M5',version:'Max'},
    {id:'b',generation:'g',model:'M5',version:'Max+'},
    {id:'c',generation:'g',model:'M5',version:'Ultra'}
  ];
  salesIndex = new Map([['a|2026-08|locked_orders',11],['b|2026-08|locked_orders',22],['c|2026-08|locked_orders',33]]);
`, context);
const detail = get("planVersionRows(state.products,['2026-08','2026-09'])");
const detailRows = [...detail.matchAll(/<tr\b[^]*?<\/tr>/g)].map(m=>m[0]);
assert.equal(detailRows.length, 6);
assert.deepEqual(detailRows.map(r=>r.match(/data-plan-metric="([^"]+)"/)[1]),
  ['locks','locks','locks','sop','sop','sop']);
assert.deepEqual(detailRows.map(r=>r.match(/data-version="([^"]+)"/)[1]), ['a','b','c','a','b','c']);
for(const r of detailRows) assert.equal((r.match(/class="(?:history|sop)-cell number past-month"/g)||[]).length, 2);
assert.ok(detailRows.slice(0,3).every(r=>!r.includes('data-edit=')));
assert.match(detailRows[3], /data-edit="a" data-month="2026-08"[^]*?>0<\/button>/);
assert.match(detailRows[4], /data-edit="b" data-month="2026-08"[^]*?>50<\/button>/);
assert.match(detailRows[5], /data-edit="c" data-month="2026-09"[^]*?title="待维护，点击输入计划量"><span class="missing">—<\/span><\/button>/);
const frozenRows=get("snapshotRows({products:[{id:'a'}],meta:{months:['2026-10']},plans:[{product_id:'old-version',month:'2026-09',sop:0,forecast:8}]})");
assert.equal(frozenRows.length,4);
assert.deepEqual(frozenRows.find(r=>r.product_id==='old-version'&&r.month==='2026-09'),
  {product_id:'old-version',month:'2026-09',sop:0,forecast:8});
assert.equal(frozenRows.find(r=>r.product_id==='a'&&r.month==='2026-10').sop,null);

// Annual totals never turn absent data into zero or label a partial year as complete.
let annual=get("annualQuantity(state.products,yearMonths('2026'),'sop')");
assert.equal(annual.value,50);
assert.equal(annual.knownMonths,1);
assert.equal(annual.partial,true);
assert.equal(annual.complete,false);
assert.match(get("annualCell(state.products,yearMonths('2026'),'sop')"),/已知 1\/12 月 · 缺项/);
annual=get("annualQuantity(state.products,yearMonths('2027'),'sop')");
assert.equal(annual.value,null);
assert.equal(annual.knownMonths,0);
assert.equal(annual.complete,false);
vm.runInContext("yearMonths('2027').forEach(m=>state.products.forEach(p=>planIndex.set(key(p.id,m),{sop:0})))",context);
annual=get("annualQuantity(state.products,yearMonths('2027'),'sop')");
assert.equal(annual.value,0);
assert.equal(annual.complete,true);
assert.match(get("annualCell(state.products,yearMonths('2027'),'sop')"),/数据齐全/);
vm.runInContext("salesIndex=new Map()",context);
annual=get("annualQuantity(state.products,yearMonths('2026'),'locks')");
assert.equal(annual.value,200);
assert.equal(annual.knownMonths,1);
assert.equal(annual.complete,false);
assert.equal(annual.model_reference,true);
assert.doesNotMatch(get("annualCell(state.products,yearMonths('2026'),'locks')"), /<sup\b|†/);

// M+6 shows five previous months, the current month, and six future months.
assert.equal(get('range'),'rolling');
assert.deepEqual(get('months()'),['2026-05','2026-06','2026-07','2026-08','2026-09','2026-10','2026-11','2026-12','2027-01','2027-02','2027-03','2027-04']);
vm.runInContext("state.current_month='2026-12'",context);
assert.deepEqual(get('months()'),['2026-07','2026-08','2026-09','2026-10','2026-11','2026-12','2027-01','2027-02','2027-03','2027-04','2027-05','2027-06']);
assert.equal(get("annualQuantity(state.products,months(),'sop').complete"),false);
vm.runInContext("state.current_month='2027-06'",context);
assert.equal(get("annualQuantity(state.products,months(),'sop').complete"),true);
assert.match(get("annualCell(state.products,months(),'sop')"),/数据齐全/);
assert.doesNotMatch(get("annualCell(state.products,months(),'sop')"),/全年完整/);
vm.runInContext("state.current_month='2027-01'",context);
assert.equal(get('months()[0]'),'2026-08');
assert.equal(get('months()[5]'),'2027-01');
assert.equal(get('months().at(-1)'),'2027-07');
vm.runInContext("state.current_month='2027-07';state.plan_end='2028-01'",context);
assert.equal(get('months().at(-1)'),'2028-01');
vm.runInContext("state.current_month='2027-08';state.plan_end='2028-02'",context);
assert.equal(get('months().at(-1)'),'2028-02');
vm.runInContext("state.current_month='2026-10';state.plan_end='2027-12'",context);

// The full calendar-year input range opens history, but not months beyond the horizon.
vm.runInContext(`
  state.months=['2027-07','2028-01'];state.plan_end='2028-01';
  state.plans=[{product_id:'a',month:'2025-12',sop:0},{product_id:'a',month:'2028-04',sop:5}];
`,context);
assert.deepEqual(get('planYears()'),['2025','2026','2027','2028']);
assert.equal(get("isPlanMonthOpen('2025-12')"),true);
assert.equal(get("isPlanMonthOpen('2026-01')"),true);
assert.equal(get("isPlanMonthOpen('2028-01')"),true);
assert.equal(get("isPlanMonthOpen('2028-02')"),false);
assert.equal(get("isPlanMonthOpen('2028-04')"),false);
const closed=get("planVersionRows([state.products[0]],['2028-01','2028-02'])");
assert.match(closed,/data-edit="a" data-month="2028-01"/);
assert.doesNotMatch(closed,/data-edit="a" data-month="2028-02"/);
assert.match(closed,/closed-month[^]*?未开放/);

// The default page uses M+6; edits and export follow that exact visible period.
const app={innerHTML:''};
context.document={querySelector:s=>s==='#app'?app:null};
vm.runInContext(`
  state.products.forEach(p=>p.brand='问界');state.meta={};
  state.workbook_sync={status:'pending',message:'等待Excel关闭后回写'};
  state.months=Array.from({length:22},(_,i)=>shift('2026-03',i));
  state.plan_end='2027-12';target='2026-10';planning();
`,context);
let rendered=app.innerHTML;
assert.equal([...rendered.match(/<thead>[^]*?<\/thead>/)[0].matchAll(/<th\b/g)].length,15);
assert.match(rendered,/SOP计划 · M\+6/);
assert.match(rendered,/2026\.05—2027\.04/);
assert.doesNotMatch(rendered,/12个月合计/);
assert.match(rendered,/2026年<span class="annual-caption">年度计划量/);
assert.match(rendered,/前5个月＋当月＋未来6个月/);
const rollingHeaders=rendered.match(/<thead>[^]*?<\/thead>/)[0];
assert.equal((rollingHeaders.match(/past-month/g)||[]).length,5);
assert.deepEqual([...rollingHeaders.matchAll(/class="month-tag">([^<]+)</g)].map(m=>m[1]),['M-5','M-4','M-3','M-2','M-1','本月','M+1','M+2','M+3','M+4','M+5','M+6']);
assert.doesNotMatch(rendered,/<h1>[^<]*年度|id="year-filter"|<sup\b|†/);
assert.equal(get('target'),'2026-10');
const exportRequests=[],exportDownloads=[];
context.fetch=async(url,options)=>{
  exportRequests.push({url,body:JSON.parse(options.body)});
  return {ok:true,blob:async()=>new Blob(['xlsx-fixture'])};
};
context.document.createElement=()=>({style:{},click(){exportDownloads.push(this.download)},remove(){}});
context.document.body={append:()=>{}};
context.URL={createObjectURL:()=> 'blob:totals-workbook',revokeObjectURL:()=>{}};
context.setTimeout=()=>{};
vm.runInContext('toast=()=>{}',context);
const rollingExport=JSON.parse(JSON.stringify(await vm.runInContext("downloadPlanExport({includeLocks:true,includeVersions:true,selectedMonths:months()})",context)));
assert.equal(rollingExport.rows.length,8);
assert.deepEqual(rollingExport.headers.filter(h=>/^\d{4}-\d{2}$/.test(h)),get('months()'));
assert.equal(exportRequests[0].url,'/api/plans/export-xlsx');
assert.deepEqual(exportRequests[0].body.months,get('months()'));
assert.deepEqual(exportRequests[0].body.rows,rollingExport.rows);
assert.deepEqual(exportRequests[0].body.annual_plans,rollingExport.annualPlans);
assert.match(exportDownloads[0],/2026-05_2027-04_12个月_版本明细\.xlsx$/);

// Annual view remains available; past styling never changes editability.
vm.runInContext(`
  range='annual';state.months.push('2028-01');state.plan_end='2028-01';
  selectPlanYear('2028');planning();
`,context);
rendered=app.innerHTML;
const headers=[...rendered.match(/<thead>[^]*?<\/thead>/)[0].matchAll(/<th\b[^]*?<\/th>/g)];
assert.equal(headers.length,15);
assert.match(rendered,/<h1>SOP计划<\/h1>/);
assert.match(rendered,/2028年1—12月/);
assert.match(rendered,/2028年合计/);
assert.match(rendered,/Excel待同步/);
assert.doesNotMatch(rendered,/class="stats"|历史锁单参考|品牌维护进度/);
assert.equal(get('target'),'2028-01');
vm.runInContext("selectPlanYear('2026');planning()",context);
const annualHeaders=[...app.innerHTML.match(/<thead>[^]*?<\/thead>/)[0].matchAll(/<th\b[^]*?<\/th>/g)].map(m=>m[0]);
assert.equal(annualHeaders.filter(h=>h.includes('past-month')).length,9);
const pastEditable=get("planVersionRows([state.products[0]],['2026-08','2026-10','2026-11'])");
assert.equal((pastEditable.match(/class="(?:history|sop)-cell number past-month"/g)||[]).length,2);
assert.match(pastEditable,/data-edit="a" data-month="2026-08"/);
const calendarExport=JSON.parse(JSON.stringify(await vm.runInContext("downloadPlanExport({includeLocks:true,includeVersions:true,selectedMonths:months()})",context)));
assert.equal(calendarExport.rows.length,8);
assert.deepEqual(calendarExport.headers.filter(h=>/^\d{4}-\d{2}$/.test(h)),get('yearMonths()'));
assert.deepEqual(exportRequests[1].body.months,get('yearMonths()'));
assert.deepEqual(exportRequests[1].body.annual_plans,calendarExport.annualPlans);
assert.match(exportDownloads[1],/2026-01_2026-12_12个月_版本明细\.xlsx$/);
vm.runInContext("state.workbook_sync={status:'synced'}",context);
assert.match(get('workbookStrip()'),/Excel已同步/);
vm.runInContext("state.workbook_sync={status:'conflict'}",context);
assert.match(get('workbookStrip()'),/查看问题/);
// Calendar-year SOP totals include months outside the rolling window, but no next-year values.
vm.runInContext(`
  state={current_month:'2026-10',plan_end:'2027-12',meta:{},sales:[],plans:[],model_sales:[],
    products:[{id:'a',generation:'g',brand:'问界',model:'M5',version:'Max'},
              {id:'b',generation:'g',brand:'问界',model:'M5',version:'Ultra'}],
    months:Array.from({length:24},(_,i)=>shift('2026-01',i))};
  range='rolling';brand='';model='';query='';target='';planYear='';indexData();
  yearMonths('2026').forEach((m,i)=>{
    planIndex.set(key('a',m),{sop:i<4?10:20});
    planIndex.set(key('b',m),{sop:5});
  });
  yearMonths('2027').forEach(m=>state.products.forEach(p=>planIndex.set(key(p.id,m),{sop:1000})));
  planning();
`,context);
const cellList=html=>[...html.matchAll(/<(?:td|th)\b[^]*?<\/(?:td|th)>/g)].map(m=>m[0]);
const calendarSOPCells=cellList(get("planMetricRow(state.products,'M5','model','g',months(),'sop')"));
assert.equal(calendarSOPCells.length,15);
assert.match(calendarSOPCells[10],/data-plan-year="2026"[^]*?<strong>260<\/strong>/);
assert.match(calendarSOPCells[10],/数据齐全/);
assert.doesNotMatch(calendarSOPCells[10],/data-edit=|data-month=/);
assert.equal(get("annualQuantity([state.products[0]],yearMonths('2026'),'sop').value"),200);
assert.match(get("calendarPlanCell([state.products[0]],'2026','sop')"),/<strong>200<\/strong>/);
assert.match(get("calendarPlanCell([state.products[1]],'2026','sop')"),/<strong>60<\/strong>/);
const calendarLockCells=cellList(get("planMetricRow(state.products,'M5','model','g',months(),'locks')"));
assert.equal(calendarLockCells.length,15);
assert.match(calendarLockCells[10],/data-plan-year="2026"[^]*?<span class="missing">—<\/span>/);
const calendarHeaders=cellList(app.innerHTML.match(/<thead>[^]*?<\/thead>/)[0]);
assert.equal(calendarHeaders.length,15);
assert.match(calendarHeaders[9],/2026\.12/);
assert.match(calendarHeaders[10],/data-plan-year="2026"/);
assert.match(calendarHeaders[11],/2027\.01/);
assert.match(calendarHeaders.at(-1),/2027\.04/);
assert.doesNotMatch(app.innerHTML,/12个月合计/);
// Missing one version/month remains partial; a complete explicit-zero year remains zero.
vm.runInContext("planIndex.delete('a|2026-01')",context);
assert.match(get("calendarPlanCell(state.products,'2026','sop')"),/<strong>250<\/strong>/);
assert.match(get("calendarPlanCell(state.products,'2026','sop')"),/已知 12\/12 月 · 缺项/);
assert.doesNotMatch(get("calendarPlanCell(state.products,'2026','sop')"),/数据齐全/);
vm.runInContext("yearMonths('2027').forEach(m=>state.products.forEach(p=>planIndex.set(key(p.id,m),{sop:0})))",context);
assert.match(get("calendarPlanCell(state.products,'2027','sop')"),/<strong>0<\/strong><small>数据齐全/);
assert.match(get("calendarPlanCell(state.products,'2025','sop')"),/<strong>—<\/strong><small>暂无数据/);
// December controls the year and insertion position, including the window's two edges.
for(const [current,year,index] of [['2027-01','2026',7],['2027-05','2026',3],['2027-06','2027',14]]) {
  vm.runInContext(`state.current_month='${current}';planning()`,context);
  const cells=cellList(app.innerHTML.match(/<thead>[^]*?<\/thead>/)[0]);
  assert.equal(cells.length,15);
  assert.equal(cells.filter(c=>c.includes('data-plan-year=')).length,1);
  assert.match(cells[index],new RegExp('data-plan-year="'+year+'"'));
  assert.match(cells[index-1],new RegExp(year+'\\.12'));
}
// A future manual version must not hide the source model's known lock total.
vm.runInContext(`
  state.current_month='2026-10';state.plan_end='2027-12';state.sales=[];state.months=['2026-10'];
  state.products=[{id:'a',generation:'g',active:true},{id:'b',generation:'g',active:false},{id:'future',active:true}];
  state.model_sales=[{generation:'g',month:'2026-08',metric:'locked_orders',value:200}];
  state.plans=[];salesIndex=new Map();planIndex=new Map();
`,context);
assert.deepEqual(get("sumSales(state.products,'2026-08')"),{value:200,known:2,total:3,model_reference:true});
assert.equal(get("planMonths()[0]"),'2026-01');
assert.equal(get("planMonths().at(-1)"),'2027-12');
assert.equal(get("canEditProduct(state.products[1],'2026-09')"),true);
assert.equal(get("canEditProduct(state.products[1],'2026-10')"),false);
assert.equal(get("canEditProduct(state.products[0],'2028-01')"),false);
vm.runInContext("planIndex.set('a|2026-10',{sop:20});planIndex.set('future|2026-10',{sop:0})",context);
assert.deepEqual(get("sumPlan(state.products,'2026-10','sop')"),{value:20,known:2,total:2});
vm.runInContext("planIndex.set('b|2026-10',{sop:30})",context);
assert.deepEqual(get("sumPlan(state.products,'2026-10','sop')"),{value:50,known:3,total:3});
assert.doesNotMatch(get("planMetricRow([state.products[1]],'Max','version','b',['2026-10'],'sop')"),/data-edit=/);
assert.match(get("planMetricRow([state.products[1]],'Max','version','b',['2026-09'],'sop')"),/data-edit="b"/);
// A source model with a single version has model totals, never invented version actuals.
vm.runInContext("state.products=[{id:'a',generation:'g'}]",context);
assert.equal(get("sumSales(state.products,'2026-08').value"),null);
assert.match(get("planMetricRow(state.products,'M5','model','m',['2026-08'],'locks')"),/>200<\/span>/);
assert.doesNotMatch(get("planMetricRow(state.products,'Max','version','a',['2026-08'],'locks')"),/>200</);
// Later introductions must not turn a complete earlier year into a missing plan.
vm.runInContext(`
  state.products=[{id:'a',generation:'g',active:true},{id:'future',active:true,start_month:'2027-01'}];
  planIndex=new Map();yearMonths('2026').forEach(m=>planIndex.set(key('a',m),{sop:10}));
`,context);
assert.equal(get("annualQuantity(state.products,yearMonths('2026'),'sop').value"),120);
assert.equal(get("annualQuantity(state.products,yearMonths('2026'),'sop').complete"),true);
assert.equal(get("canEditProduct(state.products[1],'2026-12')"),false);
assert.equal(get("canEditProduct(state.products[1],'2027-01')"),true);
vm.runInContext("planIndex.set('future|2026-09',{sop:4})",context);
assert.equal(get("annualQuantity(state.products,yearMonths('2026'),'sop').value"),124);
assert.equal(get("canEditProduct(state.products[1],'2026-09')"),true);
// Source renaming keeps old-generation model history accessible without duplicate version values.
vm.runInContext(`
  state.products=[{id:'a',generation:'new',source_generations:['g','new']}];
  state.model_sales=[{generation:'g',month:'2026-08',metric:'locked_orders',value:200},{generation:'new',month:'2026-09',metric:'locked_orders',value:230}];
`,context);
assert.equal(get("sumSales(state.products,'2026-08',salesIndex,true).value"),200);
assert.equal(get("sumSales(state.products,'2026-09',salesIndex,true).value"),230);
vm.runInContext("salesIndex.set('a|2026-08|locked_orders',30)",context);
assert.equal(get("sumSales(state.products,'2026-08',salesIndex,false).value"),30);
vm.runInContext("salesIndex=new Map();state.model_sales.push({generation:'new',month:'2026-08',metric:'locked_orders',value:200})",context);
assert.deepEqual(get("sumSales(state.products,'2026-08',salesIndex,true)"),{value:null,known:0,total:1,reference_conflict:true});
assert.match(get("planMetricRow(state.products,'M5','model','m',['2026-08'],'locks')"),/来源冲突/);
vm.runInContext(`
  var saves=0,closedAfterSave=0,saveMessages=[];
  api=async()=>{saves++;return {ok:true}};
  closeDialog=()=>{closedAfterSave++};load=async()=>{throw new Error('connection unavailable')};
  toast=message=>saveMessages.push(message);localStorage.setItem=()=>{};
`,context);
await vm.runInContext("saveCatalog([{brand:'A',model:'New',version:'Max'}],'test')",context);
assert.equal(get('saves'),1);
assert.equal(get('closedAfterSave'),1);
assert.match(get('saveMessages[0]'),/目录已保存.*无需重复提交/);
console.log('PASS: dynamic catalog, product lifetimes, model locks, annual/rolling totals, aliases, export, and row alignment.');
