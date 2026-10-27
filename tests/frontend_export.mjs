import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../web/app.js', import.meta.url), 'utf8');
const definitions = source.split("document.addEventListener('click'")[0];
function createContext(savedLocks = null, legacy = false) {
  const storage = new Map(savedLocks === null ? [] : [[legacy ? 'sop-show-locks' : 'sop-show-locks-v2', savedLocks]]);
  const context = vm.createContext({ localStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  } });
  vm.runInContext(definitions, context);
  return context;
}
const context = createContext();
const run = code => vm.runInContext(code, context);
const get = code => JSON.parse(JSON.stringify(run(code)));
const getAsync = async code => JSON.parse(JSON.stringify(await run(code)));
assert.equal(run('showLocks'), true, 'Lock rows are initially visible.');
assert.equal(vm.runInContext('showLocks', createContext('false', true)), true, 'The old ambiguous hidden choice resets to visible once.');
assert.equal(vm.runInContext('showLocks', createContext('false')), false, 'A new hidden choice survives reload.');
assert.equal(vm.runInContext('showLocks', createContext('true')), true);

run(`
  state = {
    current_month:'2026-10',plan_end:'2027-12',months:['2026-10','2027-12'],
    history_months:['2026-07','2026-08','2026-09'],meta:{version_lock_basis:'source_version_quantity'},
    products:[
      {id:'a1',brand:'问界',model:'M5',version:'Max',generation:'a-m5',active:true,start_month:'2026-01'},
      {id:'a2',brand:'问界',model:'M5',version:'Ultra',generation:'a-m5',active:true,start_month:'2026-01'},
      {id:'a3',brand:'问界',model:'M7',version:'Pro',generation:'a-m7',active:true,start_month:'2026-01'},
      {id:'b1',brand:'智界',model:'M5',version:'Max',generation:'b-m5',active:true,start_month:'2026-01'}
    ],
    plans:[
      {product_id:'a1',month:'2026-01',sop:0},
      {product_id:'a1',month:'2026-02',sop:20},
      {product_id:'a2',month:'2026-02',sop:30},
      {product_id:'a3',month:'2026-01',sop:40},
      {product_id:'b1',month:'2026-01',sop:7},
      {product_id:'b1',month:'2026-02',sop:0}
    ],
    sales:[
      {product_id:'a1',month:'2026-01',metric:'locked_orders',value:80},
      {product_id:'a2',month:'2026-01',metric:'locked_orders',value:70},
      {product_id:'a1',month:'2026-02',metric:'locked_orders',value:100},
      {product_id:'a2',month:'2026-02',metric:'locked_orders',value:110},
      {product_id:'a3',month:'2026-01',metric:'locked_orders',value:15},
      {product_id:'b1',month:'2026-01',metric:'locked_orders',value:0},
      {product_id:'b1',month:'2026-02',metric:'locked_orders',value:9}
    ],
    model_sales:[
      {generation:'a-m5',month:'2026-01',metric:'locked_orders',value:200},
      {generation:'a-m5',month:'2026-02',metric:'locked_orders',value:250},
      {generation:'a-m7',month:'2026-01',metric:'locked_orders',value:15},
      {generation:'b-m5',month:'2026-01',metric:'locked_orders',value:0},
      {generation:'b-m5',month:'2026-02',metric:'locked_orders',value:9}
    ]
  };
  metric='locked_orders';brand='';model='';query='';range='rolling';indexData();
`);

const options = {includeLocks:true,includeVersions:true,selectedMonths:['2026-02','2026-01','2026-02']};
const maintenanceDefaults = get('maintenanceExportOptions()');
assert.deepEqual(maintenanceDefaults, {includeLocks:false,includeVersions:true,allProducts:true,
  selectedMonths:Array.from({length:24},(_,i)=>`${2026+Math.floor(i/12)}-${String(i%12+1).padStart(2,'0')}`)});
run("brand='问界';model='M5';query='Max';range='annual';planYear='2027';showLocks=true");
assert.deepEqual(get('maintenanceExportOptions()'), maintenanceDefaults,
  'The maintenance template keeps its full scope regardless of page filters, view or lock visibility.');
run("brand='';model='';query='';range='rolling';planYear=''");
context.testOptions = structuredClone(options);
const detailed = get('buildPlanExport(testOptions)');
assert.deepEqual(detailed.headers, ['层级','品牌','车型','版本','指标','2026-01','2026-02','数据说明']);
assert.deepEqual(detailed.selectedMonths, ['2026-01','2026-02']);
assert.deepEqual(context.testOptions, options, 'Building an export must not sort or mutate the supplied options.');
assert.equal(detailed.rows.length, 14, 'Three model summaries plus four version details, each with two metrics.');
assert.ok(detailed.rows.every(row => row.length === detailed.headers.length));
assert.equal(detailed.rowProductIds.length, detailed.rows.length);
assert.deepEqual(detailed.selectedProductIds, ['a1','a2','a3','b1']);
assert.deepEqual(detailed.rowProductIds.slice(0,6), [null,null,'a1','a2','a1','a2'], 'Each detail metric retains the stable version ID; summaries are not writable IDs.');
assert.deepEqual([...new Set(detailed.rows.map(row => row[0]))].sort(), ['版本明细','车型汇总'].sort());

const matchingRows = (result, {level,brand,model,version,metric}) => {
  const h = name => result.headers.indexOf(name);
  return result.rows.filter(row => (level === undefined || row[h('层级')] === level)
    && (brand === undefined || row[h('品牌')] === brand)
    && (model === undefined || row[h('车型')] === model)
    && (version === undefined || row[h('版本')] === version)
    && (metric === undefined || row[h('指标')] === metric));
};
const oneRow = (result, criteria) => {
  const rows = matchingRows(result, criteria);
  assert.equal(rows.length, 1, `Expected one row: ${JSON.stringify(criteria)}`);
  return Object.fromEntries(result.headers.map((header, i) => [header, rows[0][i]]));
};
const aModel = metric => oneRow(detailed, {level:'车型汇总',brand:'问界',model:'M5',metric});
assert.equal(aModel('锁单数')['2026-01'], 200);
assert.equal(aModel('锁单数')['2026-02'], 250);
assert.match(aModel('锁单数')['数据说明'], /交车锁单.*原值.*不按版本重算/);
assert.equal(get("sumSales(state.products,'2026-01').value"), 215, 'Brand totals use the model lock row even when version quantities sum differently.');
assert.equal(aModel('SOP计划量')['2026-01'], 0, 'A known zero remains numeric even when another version is missing.');
assert.equal(aModel('SOP计划量')['2026-02'], 50);
assert.match(aModel('SOP计划量')['数据说明'], /缺项|仅已知|部分|未填|未维护/);
assert.match(aModel('SOP计划量')['数据说明'], /明细|重复|汇总/, 'Summary/detail rows must identify their overlapping scope.');

// Same model/version names under another brand must never be merged.
const bSOP = oneRow(detailed, {level:'车型汇总',brand:'智界',model:'M5',metric:'SOP计划量'});
assert.equal(bSOP['2026-01'], 7);
assert.equal(bSOP['2026-02'], 0);
const bLocks = oneRow(detailed, {level:'车型汇总',brand:'智界',model:'M5',metric:'锁单数'});
assert.equal(bLocks['2026-01'], 0);
assert.equal(bLocks['2026-02'], 9);

// Version quantities and the model lock row are independent source values.
for (const [version,january,february] of [['Max',80,100],['Ultra',70,110]]) {
  const row = oneRow(detailed, {level:'版本明细',brand:'问界',model:'M5',version,metric:'锁单数'});
  assert.equal(row['2026-01'], january);
  assert.equal(row['2026-02'], february);
  assert.match(row['数据说明'], /版本／数量.*原值/);
}
const absentPlan = oneRow(detailed, {level:'版本明细',brand:'问界',model:'M5',version:'Ultra',metric:'SOP计划量'});
assert.equal(absentPlan['2026-01'], null);
assert.equal(absentPlan['2026-02'], 30);
assert.match(absentPlan['数据说明'], /未填|待维护|缺项|缺失|未维护/);
for (const row of detailed.rows) {
  for (const month of detailed.selectedMonths) {
    const value = row[detailed.headers.indexOf(month)];
    assert.ok(value === null || typeof value === 'number', 'Quantity cells contain raw numbers or null, never display strings.');
  }
}

const groupRows = matchingRows(detailed, {brand:'问界',model:'M5'});
assert.deepEqual(groupRows.map(row => [row[0],row[4]]), [
  ['车型汇总','锁单数'],['车型汇总','SOP计划量'],
  ['版本明细','锁单数'],['版本明细','锁单数'],
  ['版本明细','SOP计划量'],['版本明细','SOP计划量'],
]);
assert.deepEqual(groupRows.filter(row => row[0] === '版本明细').map(row => row[3]), ['Max','Ultra','Max','Ultra']);

// A version structure percentage uses the version rows as its denominator.
let detailHtml='';
context.captureDetail=html=>{detailHtml=html;};
context.fetch=async()=>({ok:true,json:async()=>({items:[]})});
run("openDialog=captureDetail;state.current_month='2026-03';target='2026-02'");
await vm.runInContext("showDetail('a1')",context);
assert.match(detailHtml,/版本锁单占比按版本行合计计算/);
assert.match(detailHtml,/47\.6%/);
assert.match(detailHtml,/52\.4%/);
run("state.current_month='2026-10';target='';selected=null");

// Each export choice is explicit and independent from the on-screen lock switch.
run('showLocks=false');
const summary = get("buildPlanExport({includeLocks:true,includeVersions:false,selectedMonths:['2026-02','2026-01']})");
assert.deepEqual(summary.headers, ['层级','品牌','车型','指标','2026-01','2026-02','数据说明']);
assert.equal(summary.rows.length, 6);
assert.ok(summary.rows.every(row => row[0] === '车型汇总'));
assert.ok(summary.rows.some(row => row[3] === '锁单数'));
assert.ok(summary.rowProductIds.every(id => id === null));
assert.deepEqual(summary.selectedProductIds, ['a1','a2','a3','b1'], 'Summary exports still preserve the selected catalog scope.');
run('showLocks=true');
const sopOnly = get("buildPlanExport({includeLocks:false,includeVersions:true,selectedMonths:['2026-02']})");
assert.equal(sopOnly.rows.length, 7);
assert.ok(sopOnly.rows.every(row => row[sopOnly.headers.indexOf('指标')] === 'SOP计划量'));
assert.deepEqual(sopOnly.selectedMonths, ['2026-02']);
assert.ok(!sopOnly.headers.includes('2026-01'));
const compact = get("buildPlanExport({includeLocks:false,includeVersions:false,selectedMonths:['2026-01']})");
assert.equal(compact.rows.length, 3);
assert.ok(compact.rows.every(row => row[0] === '车型汇总' && row[3] === 'SOP计划量'));

// Export follows the currently selected versions without pretending they form the complete model.
run("brand='问界';model='M5';query='Max'");
const subset = get("buildPlanExport({includeLocks:true,includeVersions:false,selectedMonths:['2026-01','2026-02']})");
assert.equal(subset.rows.length, 2);
assert.deepEqual(subset.selectedProductIds, ['a1'], 'A filtered summary must not silently include the other model versions.');
const subsetLocks = oneRow(subset, {level:'车型汇总',brand:'问界',model:'M5',metric:'锁单数'});
assert.equal(subsetLocks['2026-01'], 80, 'A selected subset uses only its selected version, not the whole model lock total.');
assert.equal(subsetLocks['2026-02'], 100);
const subsetSOP = oneRow(subset, {level:'车型汇总',brand:'问界',model:'M5',metric:'SOP计划量'});
assert.equal(subsetSOP['2026-02'], 20);
assert.match(subsetSOP['数据说明'], /部分版本|筛选|已选|选择/);
run("brand='';model='';query=''");

// A missing model lock row stays missing even if every version row has a value.
run("state.model_sales=state.model_sales.filter(r=>!(r.generation==='a-m5'&&r.month==='2026-02'))");
const noModelRow=get("buildPlanExport({includeLocks:true,includeVersions:true,selectedMonths:['2026-02']})");
assert.equal(oneRow(noModelRow,{level:'车型汇总',brand:'问界',model:'M5',metric:'锁单数'})['2026-02'],null);
assert.equal(oneRow(noModelRow,{level:'版本明细',brand:'问界',model:'M5',version:'Max',metric:'锁单数'})['2026-02'],100);
assert.equal(oneRow(noModelRow,{level:'版本明细',brand:'问界',model:'M5',version:'Ultra',metric:'锁单数'})['2026-02'],110);
run("state.model_sales.push({generation:'a-m5',month:'2026-02',metric:'locked_orders',value:250})");

for (const selectedMonths of [[], ['2026-1'], ['2026-13'], ['2028-01'], ['2026-01','2028-01']]) {
  context.invalidOptions = {includeLocks:true,includeVersions:true,selectedMonths};
  assert.throws(() => run('buildPlanExport(invalidOptions)'), /月|范围|选择|有效/, `Reject invalid selection: ${selectedMonths}`);
}

// Screen row structure remains valid when lock rows are hidden, including exact edit targets.
const trRows = html => [...html.matchAll(/<tr\b[^]*?<\/tr>/g)].map(match => match[0]);
run("var modelProducts=state.products.filter(p=>p.brand==='问界'&&p.model==='M5');showLocks=true");
let tableRows = trRows(get("planRow(modelProducts,'M5','model','问界|M5',['2026-01','2026-02'])"));
assert.equal(tableRows.length, 2);
assert.match(tableRows[0], /rowspan="2"/);
assert.deepEqual(tableRows.map(row => row.match(/data-plan-metric="([^"]+)"/)[1]), ['locks','sop']);
assert.equal(tableRows.filter(row => row.includes('class="sticky-name"')).length, 1);
run('showLocks=false');
tableRows = trRows(get("planRow(modelProducts,'M5','model','问界|M5',['2026-01','2026-02'])"));
assert.equal(tableRows.length, 1);
assert.doesNotMatch(tableRows[0], /rowspan="2"|data-plan-metric="locks"/);
assert.match(tableRows[0], /class="sticky-name"/);
let versionRows = trRows(get("planVersionRows(modelProducts,['2026-01','2026-02'])"));
assert.deepEqual(versionRows.map(row => row.match(/data-version="([^"]+)"/)[1]), ['a1','a2']);
assert.ok(versionRows.every(row => row.includes('data-plan-metric="sop"')));
assert.match(versionRows[0], /data-edit="a1" data-month="2026-01"/);
assert.match(versionRows[1], /data-edit="a2" data-month="2026-02"/);
run('showLocks=true');
versionRows = trRows(get("planVersionRows(modelProducts,['2026-01','2026-02'])"));
assert.deepEqual(versionRows.map(row => row.match(/data-plan-metric="([^"]+)"/)[1]), ['locks','locks','sop','sop']);

// Lifecycle exclusions are explained without inventing historical zeroes.
run("state.products.push({id:'future',brand:'问界',model:'未来车型',version:'Max',active:true,start_month:'2027-01'})");
const future = get("buildPlanExport({includeLocks:false,includeVersions:true,selectedMonths:['2026-01']},state.products.filter(p=>p.id==='future'))");
const futureRow = oneRow(future, {level:'版本明细',brand:'问界',model:'未来车型',version:'Max',metric:'SOP计划量'});
assert.equal(futureRow['2026-01'], null);
assert.match(futureRow['数据说明'], /未适用|不适用/);

// XLSX downloads post the raw export payload, await the server, and clean up browser resources.
// The maintenance workbook is optional for export; a missing file must not disable this path.
run("state.workbook_sync={status:'error',message:'维护表已被删除或移动',path:'input_file/SOP计划维护.xlsx'}");
const requests = [], downloadEvents = [], objectURLs = [], revokedURLs = [], timers = [], messages = [];
const workbookBlob = new Blob(['fixture-xlsx'], {type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
context.fetch = async (url, options) => {
  requests.push({url, options});
  return {ok:true, blob:async () => workbookBlob};
};
context.document = {
  createElement: tag => {
    assert.equal(tag, 'a');
    const anchor = {
      href:'', download:'', style:{},
      click() { downloadEvents.push({action:'click', href:this.href, name:this.download}); },
      remove() { downloadEvents.push({action:'remove'}); },
    };
    return anchor;
  },
  body:{append:anchor => downloadEvents.push({action:'append', anchor})},
};
context.URL = {
  createObjectURL:blob => { objectURLs.push(blob); return 'blob:test-plan-workbook'; },
  revokeObjectURL:url => revokedURLs.push(url),
};
context.setTimeout = (callback, delay) => { timers.push({callback, delay}); return timers.length; };
context.recordToast = message => messages.push(message);
run('toast=recordToast');
const downloaded = await getAsync("downloadPlanExport({includeLocks:false,includeVersions:false,selectedMonths:['2026-02','2026-01']})");
assert.equal(requests.length, 1);
assert.equal(requests[0].url, '/api/plans/export-xlsx');
assert.equal(requests[0].options.method, 'POST');
assert.match(requests[0].options.headers['Content-Type'], /application\/json/);
const sent = JSON.parse(requests[0].options.body);
assert.deepEqual(sent, {
  schema_version:1, current_month:'2026-10', months:['2026-01','2026-02'],
  include_locks:false, include_versions:false, scope:'全部品牌 · 全部车型',
  headers:downloaded.headers, rows:downloaded.rows, annual_plans:downloaded.annualPlans,
  row_product_ids:downloaded.rowProductIds, selected_product_ids:downloaded.selectedProductIds,
});
assert.ok(sent.rows.every(row => row[sent.headers.indexOf('指标')] === 'SOP计划量'));
assert.deepEqual(downloadEvents.map(event => event.action), ['append','click','remove']);
assert.match(downloadEvents[1].name, /2026-01_2026-02_2个月_车型汇总\.xlsx$/);
assert.equal(downloadEvents[1].href, 'blob:test-plan-workbook');
assert.equal(objectURLs[0], workbookBlob);
assert.equal(timers.length, 1);
assert.equal(timers[0].delay, 10000);
assert.deepEqual(revokedURLs, []);
timers[0].callback();
assert.deepEqual(revokedURLs, ['blob:test-plan-workbook']);
assert.equal(messages.length, 1);

// Server errors must surface without a false success toast or a downloadable error document.
context.fetch = async () => ({ok:false, json:async () => ({error:'Excel生成失败，请重试'})});
await assert.rejects(() => run("downloadPlanExport({includeLocks:false,includeVersions:false,selectedMonths:['2026-01']})"), /Excel生成失败，请重试/);
context.fetch = async () => ({ok:false, json:async () => { throw new Error('invalid JSON'); }});
await assert.rejects(() => run("downloadPlanExport({includeLocks:false,includeVersions:false,selectedMonths:['2026-01']})"), /导出|生成|下载|失败/);
assert.equal(downloadEvents.length, 3);
assert.equal(objectURLs.length, 1);
assert.equal(messages.length, 1);

// Conflicting old/new model sources must not suppress unrelated known version locks.
run(`
  state.products=[
    {id:'aliased',brand:'问界',model:'M9',version:'Max',generation:'new-m9',source_generations:['old-m9','new-m9'],active:true},
    {id:'normal',brand:'问界',model:'M9',version:'Ultra',generation:'normal-m9',active:true}
  ];
  state.plans=[];
  state.sales=[{product_id:'normal',month:'2026-01',metric:'locked_orders',value:17}];
  state.model_sales=[
    {generation:'old-m9',month:'2026-01',metric:'locked_orders',value:200},
    {generation:'new-m9',month:'2026-01',metric:'locked_orders',value:200},
    {generation:'normal-m9',month:'2026-01',metric:'locked_orders',value:17}
  ];
  indexData();
`);
const conflicted = get("buildPlanExport({includeLocks:true,includeVersions:true,selectedMonths:['2026-01']})");
const conflictSummary = oneRow(conflicted, {level:'车型汇总',brand:'问界',model:'M9',metric:'锁单数'});
assert.equal(conflictSummary['2026-01'], 17, 'Keep known version locks, without adding either conflicting model total.');
assert.match(conflictSummary['数据说明'], /新旧来源同月冲突/);
assert.match(conflictSummary['数据说明'], /冲突部分未汇总/);
assert.match(conflictSummary['数据说明'], /仅已知|缺项/);
assert.equal(oneRow(conflicted, {level:'版本明细',brand:'问界',model:'M9',version:'Max',metric:'锁单数'})['2026-01'], null);
assert.equal(oneRow(conflicted, {level:'版本明细',brand:'问界',model:'M9',version:'Ultra',metric:'锁单数'})['2026-01'], 17);

// Versions may have the same display name while retaining distinct generation identities.
run(`
  state.products=[
    {id:'m5-max-2025',brand:'问界',model:'M5',version:'Max',generation:'m5-2025',source_generation:'2025款M5',active:true},
    {id:'m5-max-2026',brand:'问界',model:'M5',version:'Max',generation:'m5-2026',source_generation:'2026款M5',active:true}
  ];
  state.plans=[
    {product_id:'m5-max-2025',month:'2026-01',sop:11},
    {product_id:'m5-max-2026',month:'2026-01',sop:29}
  ];
  state.sales=[];
  state.model_sales=[];
  indexData();
`);
const sameName = get("buildPlanExport({includeLocks:false,includeVersions:true,selectedMonths:['2026-01']})");
assert.deepEqual(sameName.headers, ['层级','品牌','车型','版本','指标','2026-01','数据说明']);
assert.equal(oneRow(sameName, {level:'车型汇总',brand:'问界',model:'M5',metric:'SOP计划量'})['2026-01'], 40);
const sameNameDetails = matchingRows(sameName, {level:'版本明细',brand:'问界',model:'M5',version:'Max',metric:'SOP计划量'});
assert.equal(sameNameDetails.length, 2, 'Both stable versions survive export despite identical display names.');
assert.deepEqual(sameName.rowProductIds, [null,'m5-max-2025','m5-max-2026'], 'Writable identity must not rely on repeated display names.');
for (const expected of [
  {id:'m5-max-2025',source:'2025款M5',value:11},
  {id:'m5-max-2026',source:'2026款M5',value:29},
]) {
  const matching = sameNameDetails.filter(row => row[sameName.headers.indexOf('2026-01')] === expected.value);
  assert.equal(matching.length, 1);
  const notes = matching[0][sameName.headers.indexOf('数据说明')];
  assert.ok(notes.includes(expected.id), 'The quantity must remain attributable to its stable version ID.');
  assert.ok(notes.includes(expected.source), 'The source generation distinguishes identically named versions.');
  context.selectedStableId = expected.id;
  const selected = get("buildPlanExport({includeLocks:false,includeVersions:true,selectedMonths:['2026-01']},state.products.filter(p=>p.id===selectedStableId))");
  const selectedDetail = oneRow(selected, {level:'版本明细',brand:'问界',model:'M5',version:'Max',metric:'SOP计划量'});
  assert.equal(selectedDetail['2026-01'], expected.value);
  assert.ok(selectedDetail['数据说明'].includes(expected.id), 'Selecting one version must not remove its stable identity.');
  assert.ok(selectedDetail['数据说明'].includes(expected.source), 'Duplicate detection uses the full catalog, not only selected versions.');
}

// Annual-plan columns use the full calendar year, not only the selected export months.
run(`
  state.products=[
    {id:'annual-known',brand:'问界',model:'M7',version:'Max',active:true},
    {id:'annual-zero',brand:'问界',model:'M7',version:'Ultra',active:true},
    {id:'annual-empty',brand:'问界',model:'M7',version:'Pro',active:true}
  ];
  state.plans=[
    {product_id:'annual-known',month:'2026-01',sop:7},
    {product_id:'annual-known',month:'2026-12',sop:3},
    {product_id:'annual-known',month:'2027-01',sop:999},
    ...yearMonths('2026').map(month=>({product_id:'annual-zero',month,sop:0}))
  ];
  state.sales=[];state.model_sales=[];indexData();
`);
const annualExport = get("buildPlanExport({includeLocks:true,includeVersions:true,selectedMonths:['2027-01','2026-12']})");
assert.equal(annualExport.annualPlans.length, annualExport.rows.length);
for (const [index, row] of annualExport.rows.entries()) {
  assert.deepEqual(Object.keys(annualExport.annualPlans[index]), ['2026'], 'Only selected December months add their calendar year.');
  const yearly = annualExport.annualPlans[index]['2026'];
  if (row[annualExport.headers.indexOf('指标')] === '锁单数') {
    assert.equal(yearly, null, 'The annual-plan column never contains lock quantities.');
  } else if (row[0] === '车型汇总' || row[annualExport.headers.indexOf('版本')] === 'Max') {
    assert.equal(yearly, 10, 'January outside the selection is included; the following January is excluded.');
  } else if (row[annualExport.headers.indexOf('版本')] === 'Ultra') {
    assert.equal(yearly, 0);
  } else {
    assert.equal(yearly, null);
  }
}
const withoutDecember = get("buildPlanExport({includeLocks:false,includeVersions:true,selectedMonths:['2026-11','2027-01']})");
assert.ok(withoutDecember.annualPlans.every(annual => Object.keys(annual).length === 0));

console.log('PASS: XLSX download/error handling, export choices, annual scope, model/version identity, selected months, null/zero, lifecycle/conflict notes, and lock-row toggles.');
