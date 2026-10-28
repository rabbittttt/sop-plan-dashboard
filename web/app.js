/* S&OP workbench: all business records are persisted by the local service. */
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt = v => v == null ? '—' : Number(v).toLocaleString('zh-CN', {maximumFractionDigits: 2});
const conflictValue = c => v => c.kind==='catalog' ? esc(typeof v==='boolean'?(v?'使用中':'停用'):v??'—') : fmt(v);
const conflictField = c => c.kind==='catalog' ? ({brand:'品牌',model:'车型',version:'版本名称',active:'启用状态'})[c.field]||'目录' : c.month||'';
const pct = v => v == null ? '—' : `${Number(v).toFixed(1)}%`;
const brands = ['智界','享界','尊界','尚界','问界'];
const titles = {planning:'SOP计划', history:'历史快照', matching:'计划匹配率', data:'数据与规则'};
const statusText = v => ({official:'正式成绩',incomplete:'数据未齐全',missing:'实绩待维护',pending:'待结算',in_progress:'考核月未结束',settled:'已结算',corrected:'已更正',future:'未到取数日',missing_snapshot:'历史快照缺失',missing_plan:'计划待维护',undefined_zero:'双零口径待确认',complete:'可计分'})[v] || v;
let state, page = 'planning', brand = '', model = '', query = '', target = '', metric = '', range = 'rolling';
let historyRange = 'recent';
let planYear = '';
// The old toggle text was ambiguous. Reset that saved choice once so the
// corrected model and version lock rows are visible; future choices persist.
const lockPreferenceKey = 'sop-show-locks-v2';
let showLocks = localStorage.getItem(lockPreferenceKey) !== 'false';
const wenjiePreferenceKey = 'sop-show-wenjie';
let showWenjie = localStorage.getItem(wenjiePreferenceKey) !== 'false';
let expanded = new Set(), allExpanded = false, selected = null, snapshotList = [], currentSnapshot = null;
let planIndex = new Map(), salesIndex = new Map(), shipmentIndex = new Map();
let owner = localStorage.getItem('sop-owner') || '';
let matchMonth = '', matchProduct = '', comparison = null, activeRequest = 0;
const key = (id, month) => `${id}|${month}`;
function shift(month, n) { const [y,m] = month.split('-').map(Number); const d = new Date(Date.UTC(y,m-1+n,1)); return d.toISOString().slice(0,7); }
function shortMonth(m) { return `${m.slice(0,4)}.${m.slice(5)}`; }
function dateText(t) { return t ? String(t).replace('T',' ').replace(/\.\d+/,'').replace(/\+08:00|Z/g,'').slice(0,19) : '尚未维护'; }
function toast(msg, error = false) { const el = $('#toast'); el.textContent = msg; el.className = `toast visible ${error?'error':''}`; clearTimeout(toast.timer); toast.timer = setTimeout(()=>el.className='toast',5000); }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message || `请求失败（${response.status}）`);
  return data;
}
function indexData() {
  planIndex = new Map((state.plans || []).map(p=>[key(p.product_id,p.month),p]));
  salesIndex = new Map((state.sales || []).map(p=>[`${key(p.product_id,p.month)}|${p.metric}`,p.value]));
  shipmentIndex = new Map((state.shipments || []).map(p=>[key(p.product_id,p.month),p]));
}
async function load(render = true) {
  state = await api('/api/state'); indexData();
  target ||= state.current_month; matchMonth ||= shift(state.current_month,-1); metric = 'locked_orders';
  $('#current-time').textContent = `北京时间 ${dateText(state.now)}`;
  const ob = $('#owner-btn'); if (ob) ob.textContent = owner || '设置维护人';
  if(render) await renderPage();
}
function compareBrands(a,b) {
  const rank=b=>brands.includes(b)?brands.indexOf(b):brands.length;
  return rank(a)-rank(b)||(a<b?-1:a>b?1:0);
}
function sortedProducts(ps) { return [...ps].sort((a,b)=>compareBrands(a.brand,b.brand)); }
function visibleBrands() { return [...new Set([...brands,...state.products.map(p=>p.brand)])].filter(b=>page!=='planning'||showWenjie||b!=='问界').sort(compareBrands); }
function products({includeWenjie=page!=='planning'||showWenjie,applyFilters=true}={}) {
  return sortedProducts(state.products.filter(p=>(includeWenjie||p.brand!=='问界')&&(!applyFilters||((!brand||p.brand===brand)&&(!model||p.model===model)&&(!query||`${p.brand} ${p.model} ${p.version}`.toLowerCase().includes(query.toLowerCase()))))));
}
function setWenjieVisibility(visible) {
  showWenjie=visible;localStorage.setItem(wenjiePreferenceKey,String(visible));
  normalizePlanningFilters();
}
function normalizePlanningFilters() {
  if(!showWenjie&&brand==='问界'){brand='';model='';query='';}
  const ps=products({includeWenjie:showWenjie,applyFilters:false});
  if(model&&!ps.some(p=>p.model===model&&(!brand||p.brand===brand)))model='';
  if(selected&&!ps.some(p=>p.id===selected))selected=null;
}
function planMonths() {
  const known=[...new Set([state.current_month,state.plan_end,...(state.sales||[]).map(s=>s.month),...(state.model_sales||[]).map(s=>s.month),...(state.plans||[]).map(p=>p.month),...(state.history_months||[]),...(state.months||[])])].filter(Boolean).sort();
  if(!known.length)return [];
  const first=`${known[0].slice(0,4)}-01`,last=known.at(-1),result=[];
  for(let month=first;month<=last;month=shift(month,1))result.push(month);
  return result;
}
function isPlanMonthOpen(month) { return (!state.plan_end||month<=state.plan_end)&&planMonths().includes(month); }
function productApplies(p,month) { return (!p.start_month||month>=p.start_month)&&(!p.end_month||month<=p.end_month); }
function canEditProduct(p,month) {
  const row=planIndex.get(key(p.id,month));
  const historicalCorrection=month<state.current_month&&(row?.sop!=null||row?.forecast!=null);
  return isPlanMonthOpen(month)&&(p.active!==false||month<state.current_month)&&(productApplies(p,month)||historicalCorrection);
}
function yearMonths(year=planYear) { return Array.from({length:12},(_,i)=>`${year}-${String(i+1).padStart(2,'0')}`); }
function planYears() { return [...new Set([state.current_month,...planMonths(),...(state.model_sales||[]).map(s=>s.month)].filter(Boolean).map(m=>m.slice(0,4)))].sort(); }
function selectPlanYear(year) {
  planYear=year;
}
function metricLabel() { return (state.meta.metrics||[]).find(m=>m.id===metric)?.label || metric; }
function months() { return range==='rolling' ? Array.from({length:12},(_,i)=>shift(state.current_month,i-5)) : yearMonths(); }
function historyMonths() { if(historyRange==='all') return [...new Set(state.sales.map(s=>s.month))].filter(m=>m<state.current_month).sort(); return state.history_months || [-3,-2,-1].map(n=>shift(state.current_month,n)); }
function productGenerations(p) { return p.source_generations?.length?p.source_generations:p.generation?[p.generation]:[]; }
function lockToggleLabel() { return `锁单数：${showLocks?'已显示':'已隐藏'}`; }
function wenjieToggleLabel() { return `问界：${showWenjie?'已显示':'已隐藏'}`; }
function latestVersionLockMonth() {
  return (state.sales||[]).filter(r=>r.metric==='locked_orders'&&r.value!=null).map(r=>r.month).sort().at(-1)||'';
}
function sumSales(ps,month, data=salesIndex,allowModelTotals=ps.length>1) {
  let value=0,modelReference=false,referenceConflict=false;
  const groups=new Map(),modelScoped=new Set(),known=new Set(),references=[];
  ps.forEach(p=>productGenerations(p).forEach(g=>{if(!groups.has(g))groups.set(g,[]);groups.get(g).push(p);}));
  for(const [generation,group] of groups){
    const all=state.products.filter(p=>productGenerations(p).includes(generation));
    const complete=allowModelTotals&&metric==='locked_orders'&&group.length===all.length;
    if(complete)group.forEach(p=>modelScoped.add(p.id));
    const ref=complete?(state.model_sales||[]).find(r=>r.generation===generation&&r.month===month&&r.metric==='locked_orders'):null;
    if(ref?.value!=null)references.push({group,ref});
  }
  for(const entry of references){
    const overlaps=references.some(other=>other!==entry&&entry.group.some(p=>other.group.some(q=>q.id===p.id)));
    if(overlaps){referenceConflict=true;continue;}
    value+=entry.ref.value;entry.group.forEach(p=>known.add(p.id));modelReference=true;
  }
  ps.filter(p=>!modelScoped.has(p.id)).forEach(p=>{const v=data.get(`${key(p.id,month)}|${metric}`);if(v!=null){value+=v;known.add(p.id);}});
  return {value:known.size?value:null,known:known.size,total:ps.length,...(modelReference?{model_reference:true}:{}),...(referenceConflict?{reference_conflict:true}:{})};
}
function sumPlan(ps,month,field,data=planIndex) {
  ps=ps.filter(p=>(productApplies(p,month)&&(p.active!==false||p.end_month||month<state.current_month))||data.get(key(p.id,month))?.[field]!=null);
  const vals=ps.map(p=>data.get(key(p.id,month))?.[field]); const known=vals.filter(v=>v!=null);
  return {value:known.length?known.reduce((a,b)=>a+b,0):null, known:known.length, total:ps.length};
}
function quantity(q, pending='待维护') {
  if(q.value==null)return q.reference_conflict?'<span class="missing" title="关联的新旧来源在同月都有车型锁单，请核对来源">来源冲突</span>':`<span class="missing">${pending}</span>`;
  const note=[q.model_reference?'源表交车锁单数量行，车型原值':'',q.reference_conflict?'同一版本的新旧来源同月冲突，冲突部分未汇总':'',q.known<q.total?'仅已知项合计，仍有缺项':''].filter(Boolean).join('；');
  return note?`<span title="${note}" aria-label="${fmt(q.value)}，${note}">${fmt(q.value)}</span>`:fmt(q.value);
}
function opt(value,label,current) { return `<option value="${esc(value)}" ${value===current?'selected':''}>${esc(label)}</option>`; }
function heading(sub, actions='') { return `<div class="page-heading"><div><h1>${titles[page]}</h1><p class="subtitle">${sub}</p></div><div class="toolbar">${actions}</div></div>`; }
function button(text,action,cls='secondary',extra='') { return `<button type="button" class="button ${cls}" data-action="${action}" ${extra}>${text}</button>`; }
function brandTabs() { return `<div class="brand-tabs" role="group" aria-label="品牌筛选">${['',...brands].map(b=>`<button class="brand-tab ${b===brand?'active':''}" data-brand="${b}" aria-pressed="${b===brand}">${b||'全部品牌'}<span>${state.products.filter(p=>!b||p.brand===b).length}</span></button>`).join('')}</div>`; }
function filterBar(extra='') {
  const ms=[...new Set(state.products.filter(p=>!brand||p.brand===brand).map(p=>p.model))];
  return `<div class="filterbar"><label class="field">车型<select id="model-filter">${opt('','全部车型',model)}${ms.map(m=>opt(m,m,model)).join('')}</select></label>${page==='matching'?'':`<label class="field search-field">搜索版本<input id="version-search" placeholder="输入车型或版本名称" value="${esc(query)}"></label>`}${extra}</div>`;
}
function selectedPanel(ps) {
  const picked=selected?ps.filter(p=>p.id===selected):ps;
  const dates=[...historyMonths(),state.current_month], aggregate=dates.map(m=>sumSales(picked,m)),totals=aggregate.map(q=>q.value);
  const max=Math.max(1,...totals.filter(v=>v!=null));
  return `<div class="two-col page-section"><section class="card"><div class="panel-head"><div><h2>历史锁单参考</h2><p>${selected?esc(picked[0]?.version||'已选版本'):'当前筛选范围'} · ${esc(metricLabel())}</p></div><span class="badge neutral">历史实际</span></div><div class="bar-chart" role="img" aria-label="最近三个月及本月订单参考">${dates.map((m,i)=>`<div class="bar-column"><span class="bar-value">${fmt(totals[i])}${aggregate[i].known<aggregate[i].total&&totals[i]!=null?'*':''}</span><div class="bar-track"><div class="bar ${i===dates.length-1?'current':''}" style="height:${totals[i]==null?0:Math.max(2,totals[i]/max*100)}%"></div></div><span>${shortMonth(m)}</span><small>${i===dates.length-1?'本月累计':'来源月值'}</small></div>`).join('')}</div><div class="chart-note">来源月值完整性待核对；缺失项不补0。锁单不等于实际发货量。</div></section><section class="card"><div class="panel-head"><div><h2>品牌维护进度</h2><p>${shortMonth(target)} · 仅统计SOP计划维护情况</p></div><span class="badge neutral">${ps.length} 个版本</span></div><div class="brand-progress">${brands.filter(b=>!brand||brand===b).map(b=>{
    const bp=ps.filter(p=>p.brand===b);const done=bp.filter(p=>{const v=planIndex.get(key(p.id,target));return v?.sop!=null;}).length;
    const latest=bp.map(p=>planIndex.get(key(p.id,target))).filter(Boolean).sort((a,b)=>b.updated_at.localeCompare(a.updated_at))[0];
    return `<div class="progress-row"><strong>${b}</strong><div><div class="progress-label"><span>${done}/${bp.length} 已维护</span><span>${latest?esc(latest.owner):'等待维护'}</span></div><div class="progress-track"><i style="width:${bp.length?done/bp.length*100:0}%"></i></div></div><span class="badge ${done===bp.length&&bp.length?'good':'warn'}">${done===bp.length&&bp.length?'已齐全':'待补齐'}</span></div>`;
  }).join('')}</div></section></div>`;
}
function annualQuantity(ps,visibleMonths,field,allowModelTotals=ps.length>1) {
  const quantities=visibleMonths.map(m=>field==='locks'?sumSales(ps,m,salesIndex,allowModelTotals):sumPlan(ps,m,'sop')).filter(q=>q.total>0);
  const known=quantities.filter(q=>q.value!=null),partial=known.some(q=>q.known<q.total);
  return {value:known.length?known.reduce((sum,q)=>sum+q.value,0):null,knownMonths:known.length,totalMonths:quantities.length,partial,complete:quantities.length>0&&known.length===quantities.length&&!partial,model_reference:known.some(q=>q.model_reference)};
}
function annualCell(ps,visibleMonths,field,planYear='',allowModelTotals=ps.length>1) {
  const q=annualQuantity(ps,visibleMonths,field,allowModelTotals);
  const note=q.complete?'数据齐全':q.value==null?'暂无数据':`已知 ${q.knownMonths}/${q.totalMonths} 月${q.partial?' · 缺项':''}`;
  return `<td class="annual-total number${planYear?' calendar-plan':''}${q.complete?'':' annual-partial'}"${planYear?` data-plan-year="${planYear}"`:''} title="${planYear?`${planYear}年1—12月SOP计划，含视图外月份；`:''}${q.complete?`全部${q.totalMonths}个月数据齐全`:'仅汇总已知值，不代表所选期间完整数量'}${q.model_reference?'；含车型汇总锁单':''}"><strong>${fmt(q.value)}</strong><small>${note}</small></td>`;
}
function calendarPlanCell(ps,year,field) {
  if(field==='locks')return `<td class="annual-total number calendar-plan" data-plan-year="${year}" title="此列仅统计${year}年度SOP计划量"><span class="missing">—</span></td>`;
  return annualCell(ps,yearMonths(year),'sop',year);
}
function calendarPlanHeader(year) {
  return `<th scope="col" class="annual-total calendar-plan" data-plan-year="${year}" title="汇总${year}年1—12月全部SOP计划，包含当前视图未展示的月份">${year}年<span class="annual-caption">年度计划量</span></th>`;
}
function planMetricRow(ps,label,level,id,visibleMonths,field,{isOpen=false,showName=true,rowSpan=1,blockStart=false}={}) {
  const one=level==='version', p=one?ps[0]:null;
  const cls=level==='total'?'total-row':level==='brand'?'group-row':one?'version-row':'model-row';
  const isLock=field==='locks';
  const labelContent=one?`<button class="row-link" data-detail="${p.id}">${esc(label)}</button>${p.active===false?'<small>已停用</small>':''}${p.name_conflict?'<small class="catalog-alert">同名待区分</small>':''}`:level==='model'?`<button class="row-toggle" data-expand="${esc(id)}" aria-expanded="${isOpen}"><span>${isOpen?'−':'+'}</span>${esc(label)} <em>${ps.length}版本</em></button>`:`<strong>${esc(label)}</strong> <small>${ps.length}版本</small>`;
  const cells=visibleMonths.map(m=>{
    const past=m<state.current_month?' past-month':'';
    let cell;
    if(isLock) {
      const actual=sumSales(ps,m,salesIndex,!one);
      const modelOnly=one&&state.meta?.version_lock_basis==='model_lock_row'&&(state.model_sales||[]).some(r=>r.generation===p.generation&&r.month===m&&r.value!=null);
      cell=`<td class="history-cell number${past}" title="${m>state.current_month?'尚未发生':modelOnly?'源表仅提供车型锁单数，未分配至版本':actual.value==null?one?'源表暂无该月版本锁单数':'源表暂无该月车型锁单数':one?'源表“版本／数量”行，版本原值':actual.model_reference?'源表“交车锁单／数量”行，车型原值':'当月来源锁单数'}">${quantity(actual,'—')}</td>`;
    } else {
      const q=sumPlan(ps,m,'sop');
      const open=isPlanMonthOpen(m)&&(!one||canEditProduct(p,m));
      const closedLabel=one&&!productApplies(p,m)?'未适用':one&&p.active===false?'—':'未开放';
      cell=`<td class="sop-cell number${past}${open?'':' closed-month'}"${open?'':` title="${one&&p.active===false?'版本已停用，已有计划保留':one&&!productApplies(p,m)?'此月不在版本适用月份内':'此月份不在已开放计划范围内'}"`}>${one&&open?`<button class="edit-cell" data-edit="${p.id}" data-month="${m}" aria-label="编辑${esc(p.version)} ${m} SOP计划${q.value==null?'（待维护）':''}" title="${q.value==null?'待维护，点击输入计划量':'点击维护计划量'}">${quantity(q,'—')}</button>`:quantity(q,open?'—':closedLabel)}</td>`;
    }
    return cell+(range==='rolling'&&m.endsWith('-12')?calendarPlanCell(ps,m.slice(0,4),field):'');
  }).join('');
  const nameCell=showName?`<th scope="row" class="sticky-name"${rowSpan>1?` rowspan="${rowSpan}"`:''}>${labelContent}</th>`:'';
  return `<tr class="${cls} ${isLock?'metric-lock-row':'metric-plan-row'}${!one&&showName?' summary-start':''}${blockStart?' metric-block-start':''}" data-plan-metric="${field}"${one?` data-version="${esc(p.id)}"`:''}>${nameCell}<td class="sticky-metric"><span class="metric-label ${isLock?'lock-label':'plan-label'}">${isLock?'锁单数':'SOP计划量'}</span></td>${cells}${range==='rolling'?'':annualCell(ps,visibleMonths,field,'',!one)}</tr>`;
}
function planRow(ps,label,level,id,visibleMonths,isOpen=false) {
  if(!showLocks)return planMetricRow(ps,label,level,id,visibleMonths,'sop',{isOpen});
  return planMetricRow(ps,label,level,id,visibleMonths,'locks',{isOpen,rowSpan:2})+
    planMetricRow(ps,label,level,id,visibleMonths,'sop',{showName:false});
}
function planVersionRows(ps,visibleMonths) {
  return (showLocks?['locks','sop']:['sop']).map(field=>ps.map((p,i)=>
    planMetricRow([p],p.version,'version',p.id,visibleMonths,field,{blockStart:i===0})
  ).join('')).join('');
}
function workbookStrip() {
  const w=state.workbook_sync||{},bad=['conflict','error'].includes(w.status);
  const synced=w.status==='synced',label=bad?'Excel同步待处理':({synced:'Excel已同步',absent:'未建维护表',pending:'Excel待同步',syncing:'Excel同步中',idle:'Excel准备同步'})[w.status]||'Excel准备同步';
  return `<span class="sheet-sync ${synced?'synced':bad||w.status==='pending'?'warn':'neutral'}" title="${esc(w.message||'Excel保存后自动同步')}" role="status"><i class="status-dot"></i>${label}</span>${bad?button('查看问题','workbook-status','small'):''}`;
}
function planning() {
  normalizePlanningFilters();
  const years=planYears();selectPlanYear(years.includes(planYear)?planYear:state.current_month.slice(0,4));
  const ps=products(),visible=months(),editable=visible.filter(isPlanMonthOpen),rolling=range==='rolling';
  if(!editable.includes(target))target=editable.find(m=>m===state.current_month)||editable[0]||'';
  const period=rolling?`${shortMonth(visible[0])}—${shortMonth(visible.at(-1))}`:`${planYear}年1—12月`;
  const models=[...new Set(products({applyFilters:false}).filter(p=>!brand||p.brand===brand).map(p=>p.model))];
  const notes=state.meta?.quality||[],modelLockBasis=state.meta?.version_lock_basis==='model_lock_row',versionLockBasis=state.meta?.version_lock_basis==='source_version_quantity',versionLockLatest=latestVersionLockMonth();
  let rows='';
  for(const b of [...new Set([...brands,...ps.map(p=>p.brand)])]) {
    const bp=ps.filter(p=>p.brand===b);if(!bp.length)continue;
    rows+=planRow(bp,b,'brand',b,visible);
    for(const m of [...new Set(bp.map(p=>p.model))]) {
      const mp=bp.filter(p=>p.model===m),id=`${b}|${m}`,opened=allExpanded||expanded.has(id)||!!query;
      rows+=planRow(mp,m,'model',id,visible,opened);
      if(opened)rows+=planVersionRows(mp,visible);
    }
  }
  rows+=planRow(ps,brand?`${brand}合计`:showWenjie?'鸿蒙智行合计':'合计（不含问界）','total','',visible);
  $('#app').innerHTML=`<div class="annual-toolbar"><div class="annual-filters"><label class="field inline-field year-field">视图<select id="range-filter">${opt('rolling','M+6（滚动12个月）',range)}${opt('annual','全年',range)}</select></label>${rolling?'':`<label class="field inline-field year-field">年份<select id="year-filter">${years.map(y=>opt(y,`${y}年`,planYear)).join('')}</select></label>`}<label class="field inline-field">品牌<select id="brand-filter">${opt('','全部品牌',brand)}${visibleBrands().map(b=>opt(b,b,brand)).join('')}</select></label><label class="field inline-field">车型<select id="model-filter">${opt('','全部车型',model)}${models.map(m=>opt(m,m,model)).join('')}</select></label><label class="field inline-field annual-search"><span class="visually-hidden">搜索版本</span><input id="version-search" placeholder="搜索车型 / 版本" value="${esc(query)}"></label></div><div class="toolbar">${button('车型与版本','manage-products','small')}${button(state.workbook_sync?.status==='absent'?'创建维护表':'Excel维护','open-workbook','primary small')}${button('同步','sync-workbook','small')}${button('导入Excel','import-plan','small')}${button('导出','export-plan','small')}</div></div>
    <div class="annual-heading"><h1>SOP计划${rolling?' · M+6':''}</h1><p>${period} · ${rolling?'前5个月＋当月＋未来6个月':'全年月度对照'}<span>单位：辆</span></p></div>
    <div class="sheet-tools"><div class="sheet-meta"><span>销量截止：<strong>${esc(state.meta?.data_through_label||state.meta?.data_through||'待确认')}</strong></span><span>计划开放至：<strong>${shortMonth(state.plan_end)}</strong></span>${workbookStrip()}</div><div class="toolbar"><label class="field inline-field">维护月份<select id="target-filter" ${editable.length?'':'disabled'}>${editable.length?editable.map(m=>opt(m,shortMonth(m),target)).join(''):'<option>未开放</option>'}</select></label>${button('批量维护','bulk-plan','small',editable.length?'':'disabled')}${button(wenjieToggleLabel(),'toggle-wenjie','small lock-toggle',`aria-pressed="${showWenjie}" title="点击${showWenjie?'隐藏':'显示'}问界，合计随展示范围变化"`)}${button(lockToggleLabel(),'toggle-locks','small lock-toggle',`aria-pressed="${showLocks}" title="点击${showLocks?'隐藏':'显示'}车型及版本锁单数"`)}${button(allExpanded?'收起版本':'展开版本','expand-all','ghost small')}</div></div>
    <section class="card plan-card annual-sheet${rolling?' rolling-sheet':''}" aria-label="${rolling?'M+6':planYear+'年'}SOP计划表"><div class="table-scroll" tabindex="0" aria-label="${showLocks?'锁单和':''}SOP计划表，可横向滚动"><table class="plan-table" style="--plan-month-count:${visible.length}"><thead><tr><th scope="col" class="sticky-name">品牌 / 车型 / 版本</th><th scope="col" class="sticky-metric">指标</th>${visible.map((m,i)=>`<th scope="col" class="${m===target?'selected-month':''}${m<state.current_month?' past-month':''}" title="${m}${m<state.current_month?' · 已过去月份':''}${isPlanMonthOpen(m)?'':' · SOP未开放'}">${rolling?shortMonth(m):`${Number(m.slice(5))}月`}${rolling?`<span class="month-tag">${i<5?`M${i-5}`:i===5?'本月':`M+${i-5}`}</span>`:m===state.current_month?'<span class="month-tag">本月</span>':''}</th>${rolling&&m.endsWith('-12')?calendarPlanHeader(m.slice(0,4)):''}`).join('')}${rolling?'':`<th scope="col" class="annual-total">${planYear}年合计</th>`}</tr></thead><tbody>${ps.length?rows:`<tr><td colspan="${visible.length+3}" class="empty-state">没有符合筛选条件的版本</td></tr>`}</tbody></table></div></section>
    <div class="sheet-footnote"><span>— 暂无数据或待维护；0为明确零值；灰底为已过去月份；未开放月份不可维护。${rolling?'年度计划量统计对应年1—12月，含视图外月份；':''}合计仅累加已知值。</span><span>${showLocks?(modelLockBasis?'锁单取源表“交车锁单”车型汇总，未分配至版本。':versionLockBasis?`车型锁单取“交车锁单”行，版本锁单取“版本／数量”行；版本锁单最新来源月：${versionLockLatest?shortMonth(versionLockLatest):'暂无'}。`:''):'锁单数已隐藏，可用上方按钮打开。'}点击车型展开版本${showLocks?'锁单和':''}计划。</span></div>
    ${notes.length?`<details class="sheet-notes"><summary>数据口径与来源说明</summary>${notes.map(n=>`<p>${esc(n.message)}</p>`).join('')}</details>`:''}`;
}
function openDialog(html) { $('#edit-dialog').classList.toggle('catalog-dialog',html.includes('id="catalog-manager"')); $('#edit-dialog').classList.toggle('export-dialog',html.includes('id="export-plan-form"')||html.includes('id="import-plan-form"')); $('#dialog-content').innerHTML=html; const title=$('h2',$('#dialog-content'));if(title)title.id='dialog-title';$('#edit-dialog').showModal(); }
function closeDialog() { $('#edit-dialog').close(); }
function productManager() {
  const catalog=state.products;
  openDialog(`<div id="catalog-manager"><h2>车型与版本</h2><p class="subtitle">目录取锁单来源与手工维护的全集；新增、改名后自动更新计划表。</p><div class="catalog-toolbar"><label class="field">查找<input id="catalog-search" placeholder="品牌、车型或版本"></label><div class="toolbar">${button('车型改名','rename-model')}${button('新增版本','new-product','primary')}</div></div><p class="form-help">车型来自销量工作表名，版本来自“版本／数量”区域。版本显示不带年款；未上市产品也可先建立并填写计划。手工版本与新来源的品牌、车型、版本唯一匹配时自动关联；多个代际同名时保留独立记录，核对后可用“关联来源”。</p><div class="table-scroll catalog-scroll"><table class="detail-table"><thead><tr><th>品牌 / 车型</th><th>版本</th><th>状态 / 来源</th><th>维护</th></tr></thead><tbody>${catalog.map(p=>`<tr data-catalog-search="${esc(`${p.brand} ${p.model} ${p.version}`.toLowerCase())}"><td>${esc(p.brand)}<strong>${esc(p.model)}</strong></td><td>${esc(p.version)}${p.name_conflict?'<small class="catalog-alert">同名记录，请核对来源并区分</small>':''}</td><td><span class="badge ${p.active===false?'neutral':'good'}">${p.active===false?'已停用':'使用中'}</span><small>${p.source_backed===false?'手工建立':p.source_present===false?'本次来源未包含':'销量来源'}</small>${p.name_conflict?`<small>${esc(p.source_generation||p.generation||'手工建立')}</small>`:''}</td><td><button type="button" class="button small" data-catalog-edit="${esc(p.id)}" aria-label="维护${esc(p.brand)} ${esc(p.model)} ${esc(p.version)}">维护</button><button type="button" class="button small" data-catalog-bind="${esc(p.id)}" aria-label="关联${esc(p.model)} ${esc(p.version)}的销量来源">关联来源</button></td></tr>`).join('')}</tbody></table></div><p class="form-help">日常Excel只填计划量；新增可直接在主表插入明细行，填写品牌、车型、版本和计划量；改名直接修改名称，删除整行表示停用。停用保留已有数量和历史快照，当月及未来不再新增计划。</p><div class="dialog-actions">${button('关闭','close-dialog')}</div></div>`);
  $('#catalog-search').oninput=e=>{const term=e.target.value.trim().toLowerCase();$$('[data-catalog-search]').forEach(row=>row.hidden=!row.dataset.catalogSearch.includes(term));};
}
function productFormError(err) {
  const el=$('#catalog-error');el.textContent=err.message;el.focus();
}
function bindProductSource(id) {
  const p=state.products.find(x=>x.id===id);if(!p)return;
  const candidates=state.products.filter(x=>x.id!==id&&x.source_backed&&x.source_present!==false&&!state.plans.some(r=>r.product_id===x.id)&&!(state.shipments||[]).some(r=>r.product_id===x.id));
  openDialog(`<form id="bind-source-form"><h2>关联销量来源</h2><p class="subtitle">${esc(p.brand)} · ${esc(p.model)} · ${esc(p.version)}</p><div id="catalog-error" class="inline-error" role="alert" tabindex="-1"></div><p>保留这个版本已填的计划，将选择的销量来源关联到同一条记录。适用于提前建立的版本上市，或原始文件改名。</p><div class="form-grid"><label class="field full">对应的销量版本<select id="bind-source" required><option value="">请选择并核对来源</option>${candidates.map(x=>opt(x.id,`${x.brand} ${x.model} · ${x.version}（${x.source_generation||x.generation||'销量来源'}）`,'' )).join('')}</select></label><label class="field full">维护人<input id="bind-owner" value="${esc(owner)}" required maxlength="100"></label></div><p class="form-help">只允许关联尚未维护计划或发货实绩的来源行。关联后目录只保留当前版本一行，原历史快照不变；来源已有业务记录或同月数据冲突时会提示处理，不自动覆盖。</p>${candidates.length?'':'<p class="inline-error">当前没有可关联的来源版本。</p>'}<div class="dialog-actions">${button('返回目录','manage-products')}<button type="submit" class="button primary" ${candidates.length?'':'disabled'}>关联并更新模板</button></div></form>`);
  $('#bind-source-form').onsubmit=async e=>{e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;try{
    await saveCatalog([{id:p.id,revision:p.revision||0,bind_source_id:$('#bind-source').value}],$('#bind-owner').value.trim());
  }catch(err){productFormError(err);save.disabled=false;}};
}
async function saveCatalog(changes,newOwner) {
  const result=await api('/api/products',{changes,owner:newOwner});
  owner=newOwner;localStorage.setItem('sop-owner',owner);
  closeDialog();brand='';model='';query='';
  try {await load();productManager();}
  catch(err){toast('目录已保存，页面刷新暂未完成；请点击“刷新数据”，无需重复提交。',true);return;}
  const sync=result.workbook_sync;
  toast(sync&&sync.status!=='synced'?`目录已保存；${sync.message}`:'目录与计划表已更新',sync?.status==='error'||sync?.status==='conflict');
}
function editProduct(id='') {
  const p=state.products.find(x=>x.id===id)||{brand:brand||'',model:model||'',version:'',active:true,start_month:state.current_month};
  const knownBrands=[...new Set([...brands,...state.products.map(x=>x.brand)])];
  openDialog(`<form id="catalog-form"><h2>${id?'维护版本':'新增版本'}</h2><p class="subtitle">${id?'改名沿用原记录，已填计划及历史不丢失。':'已有车型下增加版本，或填写新车型名称提前安排计划。'}</p><div id="catalog-error" class="inline-error" role="alert" tabindex="-1"></div><div class="form-grid"><label class="field">品牌<input id="catalog-brand" list="catalog-brands" value="${esc(p.brand)}" required maxlength="100"><datalist id="catalog-brands">${knownBrands.map(b=>`<option value="${esc(b)}">`).join('')}</datalist></label><label class="field">车型<input id="catalog-model" list="catalog-models" value="${esc(p.model)}" required maxlength="100"><datalist id="catalog-models">${[...new Set(state.products.map(x=>x.model))].map(m=>`<option value="${esc(m)}">`).join('')}</datalist></label><label class="field full">版本名称<input id="catalog-version" value="${esc(p.version)}" placeholder="如 Max 增程版，无需填写2026款" required maxlength="200"></label><label class="field">开始计划月份<input id="catalog-start" type="month" value="${esc(p.start_month&&p.start_month!=='1900-01'?p.start_month:'')}"></label><label class="field">结束计划月份（可留空）<input id="catalog-end" type="month" value="${esc(p.end_month||'')}"></label><label class="field">状态<select id="catalog-active">${opt('active','使用中',p.active===false?'inactive':'active')}${opt('inactive','已停用',p.active===false?'inactive':'active')}</select></label><label class="field">维护人<input id="catalog-owner" value="${esc(owner)}" required maxlength="100"></label></div>${id?`<p class="form-help">来源：${esc(p.source_generation||p.generation||'手工建立')}。只改显示名，不改变销量来源对应关系。整车所有版本一起改名，请使用“车型改名”。</p>`:''}<p class="form-help">停用后保留已有计划，不自动清空未来数量；历史月份仍可修正。不同产品请使用不同版本名称。</p><div class="dialog-actions">${button('返回目录','manage-products')}<button type="submit" class="button primary">保存并更新模板</button></div></form>`);
  $('#catalog-active').onchange=()=>{if($('#catalog-active').value==='active'&&p.active===false)$('#catalog-end').value='';};
  $('#catalog-form').onsubmit=async e=>{e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;try{
    const change={...(id?{id,revision:p.revision||0}:{}),brand:$('#catalog-brand').value.trim(),model:$('#catalog-model').value.trim(),version:$('#catalog-version').value.trim(),active:$('#catalog-active').value==='active',start_month:$('#catalog-start').value||null,end_month:$('#catalog-end').value||null};
    if(change.active&&p.active===false&&!$('#catalog-end').value)change.end_month=null;
    await saveCatalog([change],$('#catalog-owner').value.trim());
  }catch(err){productFormError(err);save.disabled=false;}};
}
function renameModel() {
  const groups=[...new Set(state.products.map(p=>`${p.brand}\t${p.model}`))];
  if(!groups.length)return editProduct();
  openDialog(`<form id="rename-model-form"><h2>车型改名</h2><p class="subtitle">将该车型下所有版本一起改名，保留原计划与销量关联。</p><div id="catalog-error" class="inline-error" role="alert" tabindex="-1"></div><div class="form-grid"><label class="field full">当前车型<select id="rename-from">${groups.map(g=>opt(g,g.replace('\t',' · '),`${brand}\t${model}`)).join('')}</select></label><label class="field">新车型名称<input id="rename-to" required maxlength="100"></label><label class="field">维护人<input id="rename-owner" value="${esc(owner)}" required maxlength="100"></label></div><div class="dialog-actions">${button('返回目录','manage-products')}<button type="submit" class="button primary">保存并更新模板</button></div></form>`);
  $('#rename-to').value=$('#rename-from').value.split('\t')[1];
  $('#rename-from').onchange=()=>{$('#rename-to').value=$('#rename-from').value.split('\t')[1];};
  $('#rename-model-form').onsubmit=async e=>{e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;try{
    const [b,m]=$('#rename-from').value.split('\t'),newModel=$('#rename-to').value.trim();
    const changes=state.products.filter(p=>p.brand===b&&p.model===m).map(p=>({id:p.id,revision:p.revision||0,brand:p.brand,model:newModel,version:p.version,active:p.active!==false}));
    await saveCatalog(changes,$('#rename-owner').value.trim());
  }catch(err){productFormError(err);save.disabled=false;}};
}
function bulkPlan() {
  if(!isPlanMonthOpen(target))return toast('此月份不在已开放计划范围内',true);
  const ps=products().filter(p=>canEditProduct(p,target));if(!ps.length)return toast('当前月份没有可维护版本',true);
  openDialog(`<form id="bulk-form"><h2>批量维护 · ${shortMonth(target)}</h2><p class="subtitle">当前筛选的 ${ps.length} 个版本。仅保存发生变化的数量。</p><div class="form-grid"><label class="field">维护人<input id="bulk-owner" value="${esc(owner)}" required maxlength="60"></label><label class="field">调整原因<select id="bulk-reason">${['首次维护','需求变化','供应变化','库存调整','产品变化','入库节奏变化','其他'].map(s=>opt(s,s,'首次维护')).join('')}</select></label><label class="field full">调整说明<input id="bulk-notes" maxlength="1000" placeholder="本轮调整的共同依据"></label></div><div class="table-scroll bulk-scroll"><table class="detail-table"><thead><tr><th>车型 / 版本</th><th>预测销量</th><th>SOP计划量</th></tr></thead><tbody>${ps.map(p=>{const r=planIndex.get(key(p.id,target))||{};return `<tr data-bulk-product="${p.id}"><td>${esc(p.brand)} ${esc(p.model)}<small>${esc(p.version)}</small></td><td><input aria-label="${esc(p.version)}预测销量" type="number" min="0" step="1" data-field="forecast" value="${r.forecast??''}" placeholder="待维护"></td><td><input aria-label="${esc(p.version)}SOP计划" type="number" min="0" step="1" data-field="sop" value="${r.sop??''}" placeholder="待维护"></td></tr>`}).join('')}</tbody></table></div><p class="form-help">空白与0分别保存；上层品牌和车型汇总自动计算。</p><div id="form-error" class="inline-error" role="alert"></div><div class="dialog-actions">${button('取消','close-dialog')}<button type="submit" class="button primary">保存本轮调整</button></div></form>`);
  $('#bulk-form').onsubmit=async e=>{e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;try{
    const changes=$$('[data-bulk-product]').map(row=>{const pid=row.dataset.bulkProduct,old=planIndex.get(key(pid,target))||{};const val=f=>$(`[data-field="${f}"]`,row).value===''?null:Number($(`[data-field="${f}"]`,row).value);return {product_id:pid,month:target,forecast:val('forecast'),sop:val('sop'),revision:old.revision||0,old};}).filter(c=>c.forecast!==(c.old.forecast??null)||c.sop!==(c.old.sop??null)).map(({old,...c})=>c);
    if(!changes.length){toast('数量没有变化');save.disabled=false;return;}
    const newOwner=$('#bulk-owner').value.trim();await api('/api/plans',{changes,owner:newOwner,reason:$('#bulk-reason').value,notes:$('#bulk-notes').value});owner=newOwner;localStorage.setItem('sop-owner',owner);closeDialog();await load();toast(`已保存 ${changes.length} 个版本的计划`);
  }catch(err){$('#form-error').textContent=err.message;save.disabled=false;}};
}
function editorOptions(id,ps=products()) {return ps.map(p=>opt(p.id,`${p.brand} · ${p.model} · ${p.version}`,id)).join('');}
function editPlan(id,month=target) {
  if(!isPlanMonthOpen(month))return toast('此月份不在已开放计划范围内',true);
  const p=state.products.find(p=>p.id===id); if(!p) return toast('请先选择一个版本',true);
  if(!canEditProduct(p,month))return toast('此版本在该月不适用或已停用，已有计划仍保留',true);
  const record=planIndex.get(key(id,month))||{};
  openDialog(`<form id="plan-form"><h2>维护月度计划</h2><p class="subtitle">预测销量和SOP分别保存，均为全月总量。</p><div class="form-grid"><label class="field full">版本<select id="edit-product">${editorOptions(id,state.products.filter(x=>x.brand===p.brand&&canEditProduct(x,month)))}</select></label><label class="field full">目标月份<select id="edit-month">${planMonths().filter(m=>canEditProduct(p,m)).map(m=>opt(m,shortMonth(m),month)).join('')}</select></label><label class="field">预测销量（辆）<input id="edit-forecast" type="number" min="0" step="1" placeholder="待维护" value="${record.forecast??''}"></label><label class="field">SOP计划量（辆）<input id="edit-sop" type="number" min="0" step="1" placeholder="待维护" value="${record.sop??''}"></label><label class="field">维护人<input id="edit-owner" value="${esc(owner)}" required maxlength="60" placeholder="请输入姓名"></label><label class="field">调整原因<select id="edit-reason">${['首次维护','需求变化','供应变化','库存调整','产品变化','入库节奏变化','其他'].map(s=>opt(s,s,record.reason||'首次维护')).join('')}</select></label><label class="field full">调整说明<textarea id="edit-notes" rows="3" maxlength="1000" placeholder="简要记录判断依据">${esc(record.notes||'')}</textarea></label></div><p class="form-help">留空表示待维护；明确不安排时填0。其他人已更新时会提示冲突。</p><div class="inline-error" id="form-error" role="alert"></div><div class="dialog-actions">${button('取消','close-dialog') }<button type="submit" class="button primary">保存计划</button></div></form>`);
  const reloadFields=()=>{
    const r=planIndex.get(key($('#edit-product').value,$('#edit-month').value))||{};
    $('#edit-forecast').value=r.forecast??'';$('#edit-sop').value=r.sop??'';$('#edit-notes').value=r.notes||'';$('#edit-reason').value=r.reason||'首次维护';
  };
  $('#edit-product').onchange=()=>{
    const chosen=state.products.find(x=>x.id===$('#edit-product').value),current=$('#edit-month').value;
    $('#edit-month').innerHTML=planMonths().filter(m=>canEditProduct(chosen,m)).map(m=>opt(m,shortMonth(m),current)).join('');
    reloadFields();
  };
  $('#edit-month').onchange=()=>{
    const id=$('#edit-product').value,m=$('#edit-month').value;
    $('#edit-product').innerHTML=editorOptions(id,state.products.filter(x=>x.brand===p.brand&&canEditProduct(x,m)));
    reloadFields();
  };
  $('#plan-form').onsubmit=async e=>{
    e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;
    try {const pid=$('#edit-product').value,m=$('#edit-month').value,r=planIndex.get(key(pid,m));const newOwner=$('#edit-owner').value.trim();
      const parse=id=>$(id).value===''?null:Number($(id).value);
      await api('/api/plans',{changes:[{product_id:pid,month:m,forecast:parse('#edit-forecast'),sop:parse('#edit-sop'),revision:r?.revision||0}],owner:newOwner,reason:$('#edit-reason').value,notes:$('#edit-notes').value});
      owner=newOwner;localStorage.setItem('sop-owner',owner);closeDialog();await load();toast('计划已保存，修改记录已留存');
    } catch(err) {$('#form-error').textContent=err.message;save.disabled=false;}
  };
}
async function showDetail(id) {
  const p=state.products.find(p=>p.id===id); selected=id;
  const history=await api(`/api/history?product_id=${encodeURIComponent(id)}&month=${target}`); history.items=history.items.filter(h=>h.entity==='plan');
  const relevant=state.products.filter(x=>x.brand===p.brand&&x.model===p.model),lm=shift(state.current_month,-1);
  const versionSalesTotal=sumSales(relevant,lm,salesIndex,false),planTotal=sumPlan(relevant,target,'sop');
  openDialog(`<div class="eyebrow">VERSION DETAIL</div><h2>${esc(p.model)} · ${esc(p.version)}</h2><p class="subtitle">${esc(p.brand)} · 关注月份 ${shortMonth(target)}</p><h3>版本结构对照</h3><p class="form-help">版本锁单占比按版本行合计计算；车型锁单另取“交车锁单”行，两者可能不同。</p><div class="table-scroll"><table class="detail-table"><thead><tr><th>版本</th><th>${lm}版本锁单占比</th><th>${target}计划占比</th></tr></thead><tbody>${relevant.map(v=>{const a=sumSales([v],lm).value,b=sumPlan([v],target,'sop').value;return `<tr><td>${esc(v.version)}</td><td>${a!=null&&versionSalesTotal.value>0&&versionSalesTotal.known===versionSalesTotal.total?pct(a/versionSalesTotal.value*100):'—'}</td><td>${b!=null&&planTotal.value>0&&planTotal.known===planTotal.total?pct(b/planTotal.value*100):'待维护齐全'}</td></tr>`}).join('')}</tbody></table></div><h3>同月修订记录</h3>${history.items.length?`<div class="activity">${history.items.map(h=>`<article><strong>${esc(h.owner||h.after?.owner||'维护人')}</strong><span>${dateText(h.recorded_at||h.updated_at||h.created_at||h.at)}</span><p>${esc(h.reason||h.after?.reason||'计划更新')} · ${esc(h.notes||h.after?.notes||'')}</p><p class="muted">预测 ${fmt(h.forecast_before??h.before?.forecast)} → ${fmt(h.forecast_after??h.after?.forecast??h.forecast)} ｜ SOP ${fmt(h.sop_before??h.before?.sop)} → ${fmt(h.sop_after??h.after?.sop??h.sop)}</p></article>`).join('')}</div>`:'<div class="empty-state compact">这个版本的目标月份尚无修订记录。</div>'}<div class="dialog-actions">${button('关闭','close-dialog')}${button('维护此版本','detail-edit','primary',`data-product="${id}"`)}</div>`);
}
function exportCSV(name,header,rows) {
  const quote=v=>{let s=String(v??''); if(/^[=+@\-]/.test(s)&&!/^\-?\d+(\.\d+)?$/.test(s))s="'"+s;return '"'+s.replace(/"/g,'""')+'"';};
  const blob=new Blob(['\uFEFF'+[header,...rows].map(r=>r.map(quote).join(',')).join('\r\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),5000);
}
function exportableMonths() { return [...new Set([...planMonths(),...months()])].sort(); }
function maintenanceExportOptions() {
  return {includeLocks:false,includeVersions:true,includeWenjie:true,selectedMonths:planMonths(),allProducts:true};
}
function buildPlanExport({includeLocks=false,includeVersions=true,selectedMonths=[]}={},ps=products()) {
  ps=sortedProducts(ps);
  const allowed=new Set(exportableMonths());
  if(!Array.isArray(selectedMonths)||!selectedMonths.length)throw new Error('请至少选择一个导出月份');
  if(selectedMonths.some(m=>typeof m!=='string'||!/^\d{4}-(0[1-9]|1[0-2])$/.test(m)||!allowed.has(m)))throw new Error('导出月份不在可选范围内');
  if(!ps.length)throw new Error('当前筛选范围没有可导出的车型或版本');
  const chosen=[...new Set(selectedMonths)].sort(),rows=[],annualPlans=[],rowProductIds=[],groups=new Map();
  const annualYears=chosen.filter(m=>m.endsWith('-12')).map(m=>m.slice(0,4));
  ps.forEach(p=>{const id=JSON.stringify([p.brand,p.model]);if(!groups.has(id))groups.set(id,[]);groups.get(id).push(p);});
  const fields=includeLocks?['locks','sop']:['sop'];
  for(const group of groups.values()) {
    const p=group[0],fullCount=state.products.filter(x=>x.brand===p.brand&&x.model===p.model).length;
    const append=(items,field,detail=false)=>{
      const notes=[],quantities=chosen.map(m=>field==='locks'?sumSales(items,m,salesIndex,!detail):sumPlan(items,m,'sop'));
      if(detail&&state.products.filter(x=>x.brand===p.brand&&x.model===p.model&&x.version===items[0].version).length>1) {
        const version=items[0],source=version.source_generation||productGenerations(version).join('、')||'手工建立';
        notes.push(`同名版本来源：${source}；版本编码：${version.id}`);
      }
      if(!detail&&group.length<fullCount)notes.push(`仅汇总筛选的 ${group.length}/${fullCount} 个版本`);
      if(!detail&&includeVersions)notes.push('车型汇总与版本明细范围相同，请勿重复相加');
      if(field==='locks') {
        notes.push('锁单为来源月值，完整性以源表为准');
        if(quantities.some(q=>q.model_reference))notes.push('车型锁单取源表“交车锁单／数量”原值，不按版本重算');
        if(detail&&state.meta?.version_lock_basis==='source_version_quantity')notes.push('版本锁单取源表“版本／数量”原值，与车型锁单分别记录');
        if(detail&&state.meta?.version_lock_basis==='model_lock_row')notes.push('源表仅有车型锁单时，版本锁单留空');
      }
      const describe=(test,label)=>{const ms=chosen.filter((m,i)=>test(quantities[i]));if(ms.length)notes.push(`${ms.join('、')}：${label}`);};
      describe(q=>q.total===0,'未适用');
      describe(q=>q.total>0&&q.value==null,field==='locks'?'锁单缺失，留空':'计划待维护，留空');
      describe(q=>q.value!=null&&q.known<q.total,'仅已知项合计，仍有缺项');
      describe(q=>q.reference_conflict,'新旧来源同月冲突，冲突部分未汇总');
      const annual={};
      for(const year of annualYears) {
        const total=field==='sop'?annualQuantity(items,yearMonths(year),'sop'):null;
        annual[year]=total?.value??null;
        if(total?.value!=null&&!total.complete)notes.push(`${year}年度计划仅含已知值，${total.knownMonths}/${total.totalMonths}个月有数据${total.partial?'，版本仍有缺项':''}`);
      }
      rows.push([detail?'版本明细':'车型汇总',p.brand,p.model,...(includeVersions?[detail?items[0].version:'']:[]),field==='locks'?'锁单数':'SOP计划量',...quantities.map(q=>q.value),notes.join('；')]);
      annualPlans.push(annual);
      rowProductIds.push(detail?items[0].id:null);
    };
    fields.forEach(field=>append(group,field));
    if(includeVersions)fields.forEach(field=>group.forEach(product=>append([product],field,true)));
  }
  return {headers:['层级','品牌','车型',...(includeVersions?['版本']:[]),'指标',...chosen,'数据说明'],rows,selectedMonths:chosen,annualPlans,rowProductIds,selectedProductIds:ps.map(p=>p.id)};
}
function exportProducts(options) { return products({includeWenjie:options.includeWenjie??showWenjie,applyFilters:!options.allProducts}); }
function exportScope(options) {
  const scope=options.allProducts?'全部品牌 · 全部车型':[brand||'全部品牌',model||'全部车型',query?`搜索“${query}”`:''].filter(Boolean).join(' · ');
  return scope+((options.includeWenjie??showWenjie)?'':'（不含问界）');
}
async function downloadPlanExport(options) {
  const result=buildPlanExport(options,exportProducts(options)),chosen=result.selectedMonths,includeVersions=result.headers.includes('版本');
  const scope=exportScope(options);
  const response=await fetch('/api/plans/export-xlsx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
    schema_version:1,current_month:state.current_month,months:chosen,include_locks:!!options.includeLocks,include_versions:includeVersions,
    scope,headers:result.headers,rows:result.rows,annual_plans:result.annualPlans,row_product_ids:result.rowProductIds,selected_product_ids:result.selectedProductIds
  })});
  if(!response.ok){let message='Excel导出未完成，请重试';try{message=(await response.json()).error||message;}catch{}throw new Error(message);}
  const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
  link.href=url;link.download=`SOP计划_${chosen[0]}_${chosen.at(-1)}_${chosen.length}个月_${includeVersions?'版本明细':'车型汇总'}.xlsx`;
  document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
  toast(`已导出所选 ${chosen.length} 个月的Excel${options.includeLocks?'（含锁单）':''}`);
  return result;
}
function exportPlans() {
  const defaults=maintenanceExportOptions(),maintenance=new Set(defaults.selectedMonths),available=exportableMonths(),current=new Set(months());
  const years=[...new Set(available.map(m=>m.slice(0,4)))],hasFilter=!!(brand||model||query);
  openDialog(`<form id="export-plan-form"><h2>导出SOP计划</h2><p id="export-scope" class="subtitle"></p><p class="form-help">排版与维护输入表一致：完整计划周期、版本明细、只含计划量。问界选项默认跟随页面显示，可单独调整。导出后可直接填写计划量，再通过“导入Excel”同步。</p><div id="export-error" class="inline-error" role="alert"></div><fieldset class="export-content-options"><legend>导出内容</legend><label class="export-check"><input type="checkbox" id="export-wenjie" checked><span>包含问界</span></label><label class="export-check"><input type="checkbox" id="export-locks"><span>增加锁单行</span></label><label class="export-check"><input type="checkbox" id="export-versions" checked><span>包含版本明细（可填写、可导回）</span></label>${hasFilter?'<label class="export-check"><input type="checkbox" id="export-filtered"><span>仅导出当前筛选</span></label>':''}</fieldset><p class="form-help">取消版本明细后仅导出只读车型汇总，不能回填计划。不含问界的版本表仍可导回，问界已有数据不变。</p><div class="export-month-heading"><h3>导出月份</h3><div class="toolbar"><button type="button" class="button ghost small" data-export-preset="maintenance">恢复维护表格式</button><button type="button" class="button ghost small" data-export-months="current">当前视图</button><button type="button" class="button ghost small" data-export-months="all">全部月份</button><button type="button" class="button ghost small" data-export-months="none">清空</button></div></div><div class="export-month-list">${years.map(y=>`<fieldset class="export-month-year"><legend>${y}年</legend><div class="export-month-grid">${available.filter(m=>m.startsWith(y+'-')).map(m=>`<label class="export-check"><input type="checkbox" name="export-month" value="${m}" aria-label="${y}年${Number(m.slice(5))}月" ${maintenance.has(m)?'checked':''}><span>${Number(m.slice(5))}月</span></label>`).join('')}</div></fieldset>`).join('')}</div><p id="export-selection" class="export-selection" role="status"></p><p class="form-help">品牌、车型、版本及月份的排版与维护表一致；12月后附全年计划量。空白与0分别保留，合计无需填写。可按需增加锁单行或调整导出月份。</p><div class="dialog-actions">${button('取消','close-dialog')}<button type="submit" class="button primary">导出维护模板</button></div></form>`);
  const form=$('#export-plan-form'),monthInputs=$$('input[name="export-month"]',form),save=$('button[type="submit"]',form);
  const versionOption=$('#export-versions',form),lockOption=$('#export-locks',form),wenjieOption=$('#export-wenjie',form),filterOption=$('#export-filtered',form);
  const reset=()=>{
    versionOption.checked=defaults.includeVersions;lockOption.checked=defaults.includeLocks;wenjieOption.checked=defaults.includeWenjie;
    if(filterOption)filterOption.checked=false;
    monthInputs.forEach(input=>input.checked=maintenance.has(input.value));
  };
  const update=(clearError=true)=>{
    const selectedMonths=monthInputs.filter(el=>el.checked).map(el=>el.value),count=selectedMonths.length,versions=versionOption.checked;
    const options={allProducts:!filterOption?.checked,includeWenjie:wenjieOption.checked},ps=exportProducts(options),scope=exportScope(options);
    const template=versions&&!lockOption.checked&&wenjieOption.checked&&!filterOption?.checked&&count===maintenance.size&&selectedMonths.every(m=>maintenance.has(m));
    $('#export-scope',form).textContent=`${scope} · ${new Set(ps.map(p=>JSON.stringify([p.brand,p.model]))).size} 个车型 / ${ps.length} 个版本`;
    $('#export-selection',form).textContent=count?`已选 ${count} 个月 · ${template?'与维护输入表一致':versions?'版本计划，可填写并导入':'只读车型汇总'}`:'请至少选择一个月份';
    save.textContent=template?'导出维护模板':versions?'导出版本计划':'导出只读车型汇总';
    save.disabled=!count||!ps.length;
    if(clearError)$('#export-error',form).textContent=ps.length?'':'当前筛选范围没有可导出的车型或版本';
  };
  $('[data-export-preset="maintenance"]',form).onclick=()=>{reset();update();};
  $$('[data-export-months]',form).forEach(el=>el.onclick=()=>{monthInputs.forEach(input=>input.checked=el.dataset.exportMonths==='all'||(el.dataset.exportMonths==='current'&&current.has(input.value)));update();});
  [...monthInputs,versionOption,lockOption,wenjieOption,...(filterOption?[filterOption]:[])].forEach(el=>el.onchange=()=>update());
  form.onsubmit=async e=>{
    e.preventDefault();if(form.getAttribute('aria-busy')==='true')return;
    const options={includeLocks:lockOption.checked,includeVersions:versionOption.checked,includeWenjie:wenjieOption.checked,selectedMonths:monthInputs.filter(el=>el.checked).map(el=>el.value),allProducts:!filterOption?.checked};
    const controls=$$('input,button',form),previous=controls.map(el=>el.disabled);
    form.setAttribute('aria-busy','true');controls.forEach(el=>el.disabled=true);save.textContent='正在生成Excel…';$('#export-error',form).textContent='';
    try{await downloadPlanExport(options);if($('#export-plan-form')===form)closeDialog();}
    catch(err){if($('#export-plan-form')===form)$('#export-error',form).textContent=err.message;else toast(err.message,true);}
    finally{form.removeAttribute('aria-busy');controls.forEach((el,i)=>el.disabled=previous[i]);if($('#export-plan-form')===form)update(false);}
  };
  reset();
  wenjieOption.checked=showWenjie;
  update();
}
const MAX_PLAN_IMPORT_BYTES=12*1024*1024;
function validatePlanImportFile(file) {
  if(!file)throw new Error('请先选择修改后的Excel文件');
  if(!/\.xlsx$/i.test(file.name))throw new Error('请选择 .xlsx 格式的SOP计划表');
  if(!file.size)throw new Error('文件为空，请重新保存Excel后选择');
  if(file.size>MAX_PLAN_IMPORT_BYTES)throw new Error('文件不能超过12MB，请缩小导出月份或版本范围');
  return file;
}
async function readPlanImportFile(file) {
  validatePlanImportFile(file);
  const content_base64=await new Promise((resolve,reject)=>{
    const reader=new FileReader();
    reader.onload=()=>{const encoded=String(reader.result||'').split(',')[1];encoded?resolve(encoded):reject(new Error('无法读取文件内容，请重新选择'));};
    reader.onerror=()=>reject(new Error('读取文件失败，请确认Excel已保存后重试'));
    reader.onabort=()=>reject(new Error('文件读取已取消，请重新选择'));
    reader.readAsDataURL(file);
  });
  return {filename:file.name,content_base64};
}
async function submitPlanImport(payload,choice) {
  if(choice!==undefined&&!['db','workbook'].includes(choice))throw new Error('请选择冲突项的处理方式');
  const body={filename:payload.filename,content_base64:payload.content_base64,...(choice?{choice}:{})};
  const response=await fetch('/api/plans/import-xlsx',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  let data;
  try{data=await response.json();}catch{throw new Error('未能确认导入结果，请刷新看板检查后重试');}
  if(!response.ok){const fallback=response.status===409?'表格与网页存在同时修改的计划，请核对后选择保留值':`导入未完成（${response.status}）`;const error=new Error(data.error||data.message||fallback);error.status=response.status;error.data=data;throw error;}
  return data;
}
function planConflictTableHtml(conflicts,maintenance=false) {
  return `<div class="table-scroll import-conflict-table"><table class="detail-table"><thead><tr><th>车型 / 版本 / 月份</th><th>${maintenance?'共同基准':'导出时'}</th><th>${maintenance?'统一维护表值':'Excel修改值'}</th><th>网页当前值</th></tr></thead><tbody>${conflicts.map(c=>{const p=state.products.find(p=>p.id===c.product_id)||c;return `<tr><td>${esc(p.brand)} ${esc(p.model)}<small>${esc(p.version||c.product_id)} · ${esc(conflictField(c))}${c.reason==='plans_changed'?'（导出后计划已更新）':''}</small></td><td>${conflictValue(c)(c.baseline)}</td><td>${conflictValue(c)(c.workbook)}</td><td>${conflictValue(c)(c.database)}</td></tr>`;}).join('')}</tbody></table></div>`;
}
function planImportConflictsHtml(conflicts) {
  return `${planConflictTableHtml(conflicts)}<p class="form-help">两边都修改了以上单元格。选择仅作用于这些冲突项，其余修改正常合并。</p><div class="dialog-actions import-conflict-actions"><button type="button" class="button secondary" data-import-choice="db">冲突项保留网页值</button><button type="button" class="button primary" data-import-choice="workbook">冲突项采用Excel值</button></div>`;
}
function importPlans() {
  openDialog(`<form id="import-plan-form"><h2>导入SOP计划</h2><p class="subtitle">将导出的Excel修改后导回，与网页维护同一份计划。</p><div id="import-error" class="inline-error" role="alert" tabindex="-1"></div><label class="field import-file-field">选择Excel文件<input id="import-plan-file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" aria-describedby="import-file-help" required></label><p id="import-file-help" class="form-help">请选择看板导出的含版本明细表（.xlsx，最大12MB）。可直接插入、删除版本计划明细行或修改名称。删除行表示停用并保留历史；锁单及合计不导入。</p><p id="import-progress" class="import-progress" role="status" aria-live="polite"></p><div id="import-conflicts"></div><div class="dialog-actions">${button('取消','close-dialog')}<button id="import-save" type="submit" class="button primary" disabled>导入计划</button></div></form>`);
  const form=$('#import-plan-form'),input=$('#import-plan-file'),save=$('#import-save'),error=$('#import-error'),progress=$('#import-progress'),conflicts=$('#import-conflicts'),dialog=$('#edit-dialog'),close=$('[data-close]',dialog);
  let prepared=null,pending=false,valid=false,hasConflicts=false,maintenanceBlocked=false;
  const cancel=e=>{if(pending)e.preventDefault();};
  dialog.addEventListener('cancel',cancel);
  dialog.addEventListener('close',()=>dialog.removeEventListener('cancel',cancel),{once:true});
  const showError=message=>{error.textContent=message;error.focus();};
  const setBusy=busy=>{pending=busy;form.setAttribute('aria-busy',String(busy));$$('input,button',form).forEach(el=>el.disabled=busy);close.disabled=busy;save.disabled=busy||!valid;save.hidden=hasConflicts||maintenanceBlocked;save.textContent=busy?'正在导入…':'导入计划';progress.textContent=busy?'正在检查表格并合并计划，请稍候…':'';};
  input.onchange=()=>{
    prepared=null;valid=false;hasConflicts=false;maintenanceBlocked=false;conflicts.innerHTML='';error.textContent='';
    try{validatePlanImportFile(input.files[0]);valid=true;}catch(err){if(input.files.length)showError(err.message);}
    setBusy(false);
  };
  const showMaintenance=w=>{
    maintenanceBlocked=true;hasConflicts=false;
    const items=w.conflicts||[];
    conflicts.innerHTML=`<p class="form-help">请先处理统一维护表的同步。以下选择作用于“Excel维护”的原始表；当前选中的导入文件已保留，稍后点击“导入计划”继续。</p>${items.length?planConflictTableHtml(items,true):''}<div class="dialog-actions import-conflict-actions">${items.length?'<button type="button" class="button secondary" data-maintenance-choice="db">维护表冲突保留网页值</button><button type="button" class="button primary" data-maintenance-choice="workbook">维护表冲突采用维护表值</button>':'<button type="button" class="button primary" data-maintenance-choice="retry">重新同步维护表</button>'}</div>`;
    $$('[data-maintenance-choice]',conflicts).forEach(el=>el.onclick=async()=>{
      if(pending)return;
      setBusy(true);progress.textContent='正在同步统一维护表…';error.textContent='';
      try{
        const result=await api('/api/plan-workbook/sync',el.dataset.maintenanceChoice==='retry'?{}:{choice:el.dataset.maintenanceChoice});
        if(result.ok===false||['conflict','error'].includes(result.status)){showMaintenance(result);showError(result.message||'维护表尚未完成同步，请处理后继续');return;}
        maintenanceBlocked=false;conflicts.innerHTML='<p class="form-help">统一维护表已同步。已保留选中的文件，点击“导入计划”继续。</p>';
        try{await load(false);}catch{showError('维护表已同步，页面数据刷新失败。可点击“导入计划”重新检查并合并。');}
      }catch(err){showError(err.message);}finally{setBusy(false);}
    });
  };
  const submit=async choice=>{
    if(pending)return;
    error.textContent='';setBusy(true);
    try{
      prepared||=await readPlanImportFile(input.files[0]);
      const result=await submitPlanImport(prepared,choice);
      closeDialog();
      const message=result.imported||result.created||result.catalog_updated?`已导入 ${result.imported||0} 项计划${result.created?`，新增 ${result.created} 个版本`:''}${result.catalog_updated?`，更新 ${result.catalog_updated} 个版本`:''}${result.deactivated?`（其中停用 ${result.deactivated} 个）`:''}`:'计划已核对，没有需要导入的新修改';
      try{await load();toast(result.warning?`${message}；${result.warning}`:message,!!result.warning);}catch{toast(`${message}；页面刷新失败，请点击刷新查看${result.warning?`。${result.warning}`:''}`,true);}
    }catch(err){
      if($('#import-plan-form')!==form){toast(err.message,true);return;}
      if(err.status===409&&err.data?.source==='maintenance'){
        showMaintenance(err.data);
      }else if(err.status===409&&Array.isArray(err.data?.conflicts)&&err.data.conflicts.length){
        hasConflicts=true;conflicts.innerHTML=planImportConflictsHtml(err.data.conflicts);
        $$('[data-import-choice]',conflicts).forEach(el=>el.onclick=()=>submit(el.dataset.importChoice));
      }
      showError(err.message);
    }finally{setBusy(false);}
  };
  form.onsubmit=e=>{e.preventDefault();return submit();};
}
function snapshotBackupLabel(backup) {
  return ({ready:'Excel已备份',error:'Excel待重试',pending:'Excel生成中'})[backup?.status]||'Excel待生成';
}
async function historyPage() {
  const [archive,backups]=await Promise.all([api('/api/snapshots'),api('/api/backups/status')]);
  snapshotList=archive.items;
  if(currentSnapshot) currentSnapshot=await api(`/api/snapshots/${encodeURIComponent(currentSnapshot.snapshot.id)}`);
  $('#app').innerHTML=heading('每日截点固定留存为Excel，可回看完整计划并比较两次判断的变化。',button('刷新备份状态','refresh-backups')+button('保存会议快照','create-snapshot','primary'))+
    `<div class="context-strip"><span><i class="status-dot"></i> 每天北京时间24:00</span><span>周日快照用于14周考核</span><span>已保存 <strong>${snapshotList.length}</strong> 份快照</span></div>
    <div class="sync-strip ${backups.failed?'warn':''}"><div><strong>Excel备份 · 已生成 ${backups.ready}/${backups.total} 份</strong><p>${backups.failed?'部分备份生成失败，将自动重试。':backups.pending?'正在生成剩余备份，原快照已保存。':'每日、周日和会议快照分别保存，历史文件不会被新计划覆盖。'}</p><small>${esc(backups.directory)}</small>${backups.last_error?`<p>${esc(backups.last_error)}</p>`:''}</div><span class="badge ${backups.failed?'warn':'neutral'}">.xlsx</span></div>
    <div class="two-col page-section"><section class="card"><div class="panel-head"><div><h2>快照档案</h2><p>业务日期与实际生成时间分别保留</p></div></div>${snapshotList.length?`<div class="snapshot-list">${snapshotList.map(s=>`<button class="snapshot-item ${s.id===currentSnapshot?.snapshot?.id?'active':''}" data-snapshot="${esc(s.id)}"><div><strong>${esc(s.label||s.business_date)}</strong><span class="badge ${s.kind==='manual'?'good':'neutral'}">${s.kind==='sunday'?'周日考核':s.kind==='daily'?'每日快照':'会议快照'}</span></div><p>截点 ${dateText(s.cutoff_at)}</p><small>实际保存 ${dateText(s.created_at)} · ${s.plan_count??0} 条计划 · ${snapshotBackupLabel(s.excel_backup)}</small></button>`).join('')}</div>`:'<div class="empty-state"><div class="empty-mark">24:00</div><h3>从今天开始积累计划历史</h3><p>服务开启后每天24点保存Excel快照。也可立即保存一份会议快照。</p></div>'}</section>
    <section class="card"><div class="panel-head"><div><h2>两次快照比较</h2><p>针对同一版本、同一目标月份比较</p></div></div><div class="card-body"><div class="form-grid"><label class="field">比较基准<select id="snapshot-left"><option value="">选择较早快照</option>${snapshotList.map(s=>opt(String(s.id),`${s.label||s.business_date} · ${dateText(s.cutoff_at)}`,'' )).join('')}</select></label><label class="field">当前快照<select id="snapshot-right"><option value="">选择较新快照</option>${snapshotList.map(s=>opt(String(s.id),`${s.label||s.business_date} · ${dateText(s.cutoff_at)}`,'' )).join('')}</select></label></div><div class="dialog-actions">${button('查看变化','compare-snapshots','primary')}</div><div id="comparison-result" class="empty-state compact">选择两份快照，查看预测销量与SOP的调整。</div></div></section></div><section id="snapshot-detail"></section><div class="callout subtle">Excel备份保留快照当时的版本月度SOP与维护记录。备份文件用于查阅和归档，不参与当前计划表自动同步。系统不会生成上线前的历史计划；恢复服务时只按已留存记录还原截点。</div>`;
  if(currentSnapshot) renderSnapshot(currentSnapshot);
}
function renderSnapshot(d) {
  const s=d.snapshot; const prods=snapshotProducts(d),list=snapshotRows(d);
  const lookup=new Map(prods.map(p=>[p.id,p]));
  $('#snapshot-detail').innerHTML=`<div class="card page-section"><div class="panel-head"><div><h2>${esc(s.label||s.business_date)} · 快照明细</h2><p>截点 ${dateText(s.cutoff_at)} · 实际生成 ${dateText(s.created_at)} · 历史只读</p></div>${button('下载Excel备份','export-snapshot')}</div><div class="table-scroll"><table class="detail-table"><thead><tr><th>品牌 / 车型</th><th>版本</th><th>目标月</th><th>预测销量</th><th>SOP计划量</th><th>维护人</th><th>调整说明</th></tr></thead><tbody>${list.length?list.map(v=>{const p=lookup.get(v.product_id)||{};return `<tr><td>${esc(p.brand)} / ${esc(p.model)}</td><td>${esc(p.version||v.product_id)}</td><td>${v.month}</td><td>${v.forecast==null?'待维护':fmt(v.forecast)}</td><td>${v.sop==null?'待维护':fmt(v.sop)}</td><td>${esc(v.owner)}</td><td>${esc(v.notes)}</td></tr>`}).join(''):'<tr><td colspan="7" class="empty-state">此截点尚无已维护计划，所有版本均为待维护。</td></tr>'}</tbody></table></div><div class="table-footer">已冻结销量参考数据：${esc(d.meta?.data_through||s.meta?.data_through||'见快照导出')} · 不随之后刷新改变</div></div>`;
}
function snapshotProducts(d) {
  const prods=[...(d.products||[])],ids=new Set(prods.map(p=>p.id));
  for(const plan of d.plans||[])if(!ids.has(plan.product_id)){
    prods.push({id:plan.product_id,brand:'未归类',model:'历史版本',version:plan.product_id});ids.add(plan.product_id);
  }
  return prods;
}
function snapshotRows(d) {
  const saved=new Map((d.plans||[]).map(r=>[key(r.product_id,r.month),r]));
  const visible=[...new Set([...(d.meta?.months||[]),...(d.plans||[]).map(r=>r.month)])].sort();
  return snapshotProducts(d).flatMap(p=>visible.map(month=>saved.get(key(p.id,month))||{product_id:p.id,month,forecast:null,sop:null,owner:'',notes:''}));
}
async function workbookStatusDialog() {
  const w=await api('/api/plan-workbook/status');
  if(w.status==='absent'){
    openDialog(`<h2>尚未建立维护表</h2><p class="subtitle">网页计划和导出可以直接使用。只有点击“创建维护表”才会在 input_file 中生成 SOP计划维护.xlsx。</p><p class="form-help">预定位置：${esc(w.path)}</p><div class="dialog-actions">${button('关闭','close-dialog')}${button('先导出模板','export-plan')}${button('创建维护表','open-workbook','primary')}</div>`);
    return;
  }
  openDialog(`<h2>计划表同步</h2><p class="subtitle">${esc(w.message||'')}</p><p class="form-help">维护表：${esc(w.path)}</p>${w.conflicts?.length?`<div class="table-scroll"><table class="detail-table"><thead><tr><th>版本 / 月份</th><th>共同基准</th><th>表格数值</th><th>网页当前值</th></tr></thead><tbody>${w.conflicts.map(c=>{const p=state.products.find(p=>p.id===c.product_id);return `<tr><td>${esc(p?.model||'')} ${esc(p?.version||c.product_id)}<small>${esc(conflictField(c))}${c.reason==='plans_changed'?'（导出后计划已更新）':''}</small></td><td>${conflictValue(c)(c.baseline)}</td><td>${conflictValue(c)(c.workbook)}</td><td>${conflictValue(c)(c.database)}</td></tr>`}).join('')}</tbody></table></div><p class="form-help">以下选择仅处理冲突项；没有冲突的修改仍按同一份计划合并。</p><div class="dialog-actions">${button('暂不处理','close-dialog')}${button('冲突项保留网页值','resolve-db')}${button('冲突项采用表格值','resolve-workbook','primary')}</div>`:`<p>保存Excel后自动读取；Excel占用文件时，网页的修改先保存在统一数据库，关闭Excel后回写。</p><div class="dialog-actions">${button('关闭','close-dialog')}${button('重新同步','sync-workbook','primary')}</div>`}`);
}
async function compareSnapshots() {
  const left=$('#snapshot-left').value,right=$('#snapshot-right').value;
  if(!left||!right||left===right)return toast('请选择两份不同的快照',true);
  comparison=await api(`/api/compare?left=${encodeURIComponent(left)}&right=${encodeURIComponent(right)}`);
  const changed=comparison.items.filter(v=>v.forecast_before!==v.forecast_after||v.sop_before!==v.sop_after);
  $('#comparison-result').className='';$('#comparison-result').innerHTML=changed.length?`<p class="form-help">${changed.length} 项数量发生变化</p><div class="table-scroll"><table class="detail-table"><thead><tr><th>版本 / 月份</th><th>预测 前→后</th><th>SOP 前→后</th></tr></thead><tbody>${changed.map(v=>{const p=state.products.find(x=>x.id===v.product_id);return `<tr><td>${esc(p?.model||'')} ${esc(p?.version||v.product_id)}<small>${v.month}</small></td><td>${fmt(v.forecast_before)} → ${fmt(v.forecast_after)}</td><td>${fmt(v.sop_before)} → ${fmt(v.sop_after)}</td></tr>`}).join('')}</tbody></table></div>`:'<div class="empty-state compact">两份快照的计划数量没有变化。</div>';
}
async function matchingPage() {
  const request=++activeRequest;
  const params=new URLSearchParams({month:matchMonth,brand,model,product_id:matchProduct});
  const d=await api('/api/matching?'+params);
  if(request!==activeRequest||page!=='matching')return;
  const ps=products();
  $('#app').innerHTML=heading('采用提前13周至0周的周日快照，与同一考核月实际发货量比较。',button('维护发货实绩','edit-shipments','primary'))+
    brandTabs()+filterBar(`<label class="field">考核月份<input id="match-month" type="month" value="${matchMonth}"></label><label class="field">考核版本<select id="match-product">${opt('','当前范围合计',matchProduct)}${ps.map(p=>opt(p.id,`${p.model} · ${p.version}`,matchProduct)).join('')}</select></label>`)+
    `<div class="stats"><div class="stat-card"><div class="stat-label">综合匹配率 P</div><div class="stat-number">${pct(d.score)}</div><div class="stat-foot">${esc(statusText(d.status)||'待数据齐全')}</div></div><div class="stat-card"><div class="stat-label">考核月实际发货量 Y <span>辆</span></div><div class="stat-number">${fmt(d.actual)}</div><div class="stat-foot">${esc(statusText(d.actual_status)||'待维护')} · 来源SP / IoC</div></div><div class="stat-card"><div class="stat-label">计划周次完整度</div><div class="stat-number">${d.complete_weeks??0}<span class="unit">/ 14</span></div><div class="stat-foot">缺失周不填0、不重分配权重</div></div><div class="stat-card"><div class="stat-label">提前0周 · 业务日期</div><div class="stat-number date-number">${esc(d.zero_week||'—')}</div><div class="stat-foot">周日24:00 · 北京时间</div></div></div>`+
    `<section class="card"><div class="panel-head"><div><h2>14周计划与发货对照</h2><p>每个周次均对应 ${shortMonth(matchMonth)} 全月计划</p></div><div class="legend"><span><i class="legend-square sop"></i>历史SOP计划</span><span><i class="legend-line"></i>实际发货</span></div></div><div class="match-chart">${matchingChart(d)}</div></section>`+
    `<section class="card page-section"><div class="panel-head"><div><h2>匹配率计算明细</h2><p>Pi = min(Y, Mi) ÷ max(Y, Mi) × 100%</p></div>${button('导出考核明细','export-match')}</div><div class="table-scroll"><table class="detail-table"><thead><tr><th>提前周次</th><th>规定取数日（24点）</th><th>采用快照</th><th>SOP计划 Mi</th><th>实际发货 Y</th><th>单周匹配率</th><th>权重</th><th>加权贡献</th><th>状态</th></tr></thead><tbody>${d.rows.map(r=>`<tr><td><span class="week-label">W${r.week}</span></td><td>${r.business_date}</td><td>${r.snapshot_id?`<button class="text-button" data-match-snapshot="${esc(r.snapshot_id)}">查看快照</button>`:'—'}</td><td>${fmt(r.plan)}</td><td>${fmt(r.actual)}</td><td>${pct(r.match)}</td><td>${fmt(r.weight*100)}%</td><td>${r.contribution==null?'—':Number(r.contribution).toFixed(2)+' pp'}</td><td><span class="badge ${r.match!=null?'good':'warn'}">${esc(statusText(r.status))}</span></td></tr>`).join('')}</tbody></table></div><div class="table-footer">W13–W9：每周2%　·　W8–W5：每周7.5%　·　W4–W1：每周10%　·　W0：20%</div></section><div class="callout subtle">只有14周计划完整、考核月结束且发货实绩结算后，才产生正式成绩。Y和Mi都为0时保留“口径待确认”。品牌/车型先汇总数量，再计算匹配率。</div>`;
  window.currentMatching=d;
}
function matchingChart(d) {
  const known=d.rows.filter(r=>r.plan!=null);
  if(!known.length&&d.actual==null)return '<div class="empty-state compact"><h3>等待周度计划与发货实绩</h3><p>正式周度快照将随时间积累，历史缺失会如实保留。</p><div class="week-track">'+d.rows.map(r=>`<span>W${r.week}</span>`).join('')+'</div></div>';
  const width=1000,height=180,max=Math.max(1,d.actual||0,...known.map(r=>r.plan));
  const x=i=>45+i*70,y=v=>height-30-v/max*(height-55);
  return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="14周SOP计划与实际发货对照"><line x1="30" x2="980" y1="150" y2="150" stroke="#dae2eb"/>${d.actual!=null?`<line x1="30" x2="980" y1="${y(d.actual)}" y2="${y(d.actual)}" stroke="#c58c26" stroke-width="2" stroke-dasharray="6 5"/><text x="970" y="${Math.max(12,y(d.actual)-8)}" text-anchor="end" fill="#93651b" font-size="12">发货 ${fmt(d.actual)}</text>`:''}${d.rows.map((r,i)=>`${r.plan!=null?`<rect x="${x(i)}" y="${y(r.plan)}" width="30" height="${150-y(r.plan)}" fill="#2d6c99" rx="3"><title>W${r.week}：${r.plan}辆</title></rect>`:''}<text x="${x(i)+15}" y="173" text-anchor="middle" fill="#64748b" font-size="12">W${r.week}</text>`).join('')}</svg>`;
}
function editShipments(id=matchProduct||products()[0]?.id) {
  const p=state.products.find(p=>p.id===id);if(!p)return toast('当前筛选没有版本',true);
  const r=shipmentIndex.get(key(id,matchMonth))||{};
  openDialog(`<form id="shipment-form"><div class="eyebrow">ACTUAL SHIPMENTS</div><h2>维护实际发货量</h2><p class="subtitle">填写SP或IoC的月度实绩，销量统计不会自动代入。</p><div class="form-grid"><label class="field full">版本<select id="ship-product">${editorOptions(id,products())}</select></label><label class="field">考核月份<input id="ship-month" type="month" value="${matchMonth}" required></label><label class="field">实际发货量（辆）<input id="ship-quantity" type="number" min="0" step="1" value="${r.quantity??''}" required></label><label class="field">来源<select id="ship-source">${['SP','IoC'].map(s=>opt(s,s,r.source||'SP')).join('')}</select></label><label class="field">结算状态<select id="ship-status">${[['pending','待结算'],['settled','已结算'],['corrected','已更正']].map(([v,l])=>opt(v,l,r.status||'pending')).join('')}</select></label><label class="field">维护人<input id="ship-owner" value="${esc(owner)}" required></label><label class="field">实绩批次<input id="ship-batch" value="${esc(r.batch||'')}" placeholder="如 SP-202609" required></label></div><div id="form-error" class="inline-error" role="alert"></div><div class="dialog-actions">${button('取消','close-dialog')}<button type="submit" class="button primary">保存实绩</button></div></form>`);
  const refill=()=>{const r=shipmentIndex.get(key($('#ship-product').value,$('#ship-month').value))||{};$('#ship-quantity').value=r.quantity??'';$('#ship-source').value=r.source||'SP';$('#ship-status').value=r.status||'pending';$('#ship-batch').value=r.batch||''};
  $('#ship-product').onchange=refill;$('#ship-month').onchange=refill;
  $('#shipment-form').onsubmit=async e=>{e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;try {const pid=$('#ship-product').value,m=$('#ship-month').value;
    await api('/api/shipments',{changes:[{product_id:pid,month:m,quantity:Number($('#ship-quantity').value),source:$('#ship-source').value,status:$('#ship-status').value,batch:$('#ship-batch').value,revision:shipmentIndex.get(key(pid,m))?.revision||0}],owner:$('#ship-owner').value.trim()});owner=$('#ship-owner').value.trim();localStorage.setItem('sop-owner',owner);closeDialog();await load();toast('发货实绩已保存');
  }catch(err){$('#form-error').textContent=err.message;save.disabled=false;}};
}
function dataPage() {
  const meta=state.meta,sources=state.sources?.length?state.sources:(meta.source_files||[]),q=meta.quality||[];
  $('#app').innerHTML=heading('查看数据来源、导入质量与计划日历，保持业务口径一致。',button('车型与版本','manage-products')+button('刷新销量文件','refresh-sales','primary'))+
    `<div class="two-col"><section class="card"><div class="panel-head"><div><h2>销量数据接入</h2><p>只更新历史参考，不覆盖人工计划</p></div><span class="badge good">已接入</span></div><div class="card-body"><dl class="info-list"><dt>文件目录</dt><dd>项目目录 / input_file</dd><dt>业务数据截至</dt><dd>${esc(meta.data_through||'待确认')}</dd><dt>自动更新</dt><dd>${esc(state.source_refresh?.message||'原始文件每分钟检查，保存稳定后自动读取')}</dd><dt>最近读取</dt><dd>${dateText(meta.generated_at)}</dd><dt>版本数量</dt><dd>${state.products.length} 个</dd><dt>参考口径</dt><dd>${meta.metrics.map(m=>esc(m.label)).join('、')}</dd></dl><h3>来源文件</h3><ul class="source-list">${sources.map(s=>`<li>${esc(typeof s==='string'?s:s.file||s.name||s.path||JSON.stringify(s))}${s.sheet?`<small>${esc(s.sheet)}</small>`:''}</li>`).join('')}</ul>${(meta.notes||[]).map(n=>`<p class="form-help">${esc(n)}</p>`).join('')}</div></section><section class="card"><div class="panel-head"><div><h2>计划与快照规则</h2><p>所有日期与截点使用北京时间</p></div></div><div class="card-body"><dl class="info-list"><dt>维护颗粒度</dt><dd>品牌 → 车型 → 版本 × 月份</dd><dt>本月计划口径</dt><dd>全月总量（含已发生部分）</dd><dt>年度扩展</dt><dd>每年9月开放到次年12月</dd><dt>当前计划截止</dt><dd>${shortMonth(state.plan_end)}</dd><dt>每天保存</dt><dd>24:00；周日24点快照同时用于考核</dd><dt>提前0周</dt><dd>月初周按周一至周日：月内超过4天取上周日，否则取当周日。2027年1月取1月3日24点。</dd></dl><label class="checkbox-field"><input id="bridge-toggle" type="checkbox" ${state.settings.bridge_m6!==false?'checked':''}>7、8月补足M+6（7月到次年1月，8月到次年2月）</label><p class="form-help">已确认7、8月补足M+6；每年9月扩展到次年12月。维护人姓名用于留痕，本机版不提供账号身份认证。</p>${button('保存规则','save-settings')}</div></section></div>`+
    `<section class="card page-section"><div class="panel-head"><div><h2>数据检查与口径说明</h2><p>显示来源实际包含的内容，不用推算代替历史数据</p></div></div><div class="card-body">${q.length?q.map(v=>`<div class="quality-row"><span class="badge ${v.status==='ok'||v.status==='passed'?'good':'neutral'}">${esc(v.level==='warning'?'待核对':v.status||'说明')}</span><span>${esc(typeof v==='string'?v:v.message||v.name||JSON.stringify(v))}</span></div>`).join(''):'<p>数据读取已完成。详细汇总与映射说明见来源记录。</p>'}<div class="callout">实际发货量由SP或IoC单独提供；预测销量、SOP计划量由负责人维护。未维护的数据不会自动补0。</div></div></section><section class="card page-section"><div class="panel-head"><div><h2>运行与留存</h2><p>本机服务提供持久保存，页面可以关闭</p></div></div><div class="card-body"><p>服务运行期间自动保存每日快照，电脑关机或服务退出期间无法实时执行；下次启动会依据修改日志还原截点，并标注实际生成时间。首次启用之前的计划历史不补造。</p><p class="form-help">计划与修改记录保存在本项目的 data/sop.sqlite3，表格只是同一数据的另一维护入口。需要多位同事共同维护时，应将同一服务部署到内网主机并配置身份访问。</p></div></section>`;
}
async function renderPage() {
  $('#page-title').textContent=titles[page];
  $$('[data-page]').forEach(b=>{b.classList.toggle('active',b.dataset.page===page);b.setAttribute('aria-current',b.dataset.page===page?'page':'false');});
  if(page==='planning')planning();else if(page==='history')await historyPage();else if(page==='matching')await matchingPage();else dataPage();
}
async function handleAction(action,el) {
  if(action==='close-dialog')return closeDialog();
  if(action==='manage-products')return productManager();
  if(action==='new-product')return editProduct();
  if(action==='rename-model')return renameModel();
  if(action==='open-workbook'){
    const creating=state.workbook_sync?.status==='absent';
    await api('/api/plan-workbook/open',{});
    if($('#edit-dialog').open)closeDialog();
    await load();
    return toast(creating?'已创建并打开维护表；修改后请保存':'已请求打开统一计划表；修改后请保存');
  }
  if(action==='workbook-status')return workbookStatusDialog();
  if(['sync-workbook','resolve-db','resolve-workbook'].includes(action)){
    el.disabled=true;
    try { const result=await api('/api/plan-workbook/sync',action.startsWith('resolve-')?{choice:action==='resolve-db'?'db':'workbook'}:{});
      if($('#edit-dialog').open)closeDialog();await load();
      if(result.status==='conflict')return workbookStatusDialog();
      toast(result.message||'计划表已同步',result.ok===false||result.status==='error');
    } finally {el.disabled=false;}return;
  }
  if(action==='edit-first')return editPlan(products()[0]?.id);
  if(action==='bulk-plan')return bulkPlan();
  if(action==='expand-all'){allExpanded=!allExpanded;return planning();}
  if(action==='toggle-locks'){
    const scroll=$('.plan-card .table-scroll'),left=scroll?.scrollLeft||0;
    showLocks=!showLocks;localStorage.setItem(lockPreferenceKey,String(showLocks));planning();
    $('.plan-card .table-scroll').scrollLeft=left;
    $('[data-action="toggle-locks"]').focus({preventScroll:true});return;
  }
  if(action==='export-plan')return exportPlans();
  if(action==='toggle-wenjie'){
    const left=$('.plan-card .table-scroll')?.scrollLeft||0;
    setWenjieVisibility(!showWenjie);planning();
    $('.plan-card .table-scroll').scrollLeft=left;
    $('[data-action="toggle-wenjie"]').focus({preventScroll:true});return;
  }
  if(action==='import-plan')return importPlans();
  if(action==='detail-edit'){closeDialog();return editPlan(el.dataset.product);}
  if(action==='edit-shipments')return editShipments();
  if(action==='save-settings'){await api('/api/settings',{bridge_m6:$('#bridge-toggle').checked});await load();return toast('计划范围规则已保存');}
  if(action==='refresh-sales') {el.disabled=true;const text=el.textContent;el.textContent='正在读取…';try{await api('/api/refresh',{});await load();toast('销量已刷新，人工计划已保留');}finally{el.disabled=false;el.textContent=text;}return;}
  if(action==='refresh-backups')return historyPage();
  if(action==='create-snapshot') {
    openDialog(`<form id="snapshot-form"><h2>保存会议快照</h2><p class="subtitle">保存当前全部已开放月份，之后修改不会覆盖这份记录。</p><label class="field">快照名称<input id="snapshot-label" value="${state.current_month} SOP会议" required maxlength="100"></label><div id="form-error" class="inline-error"></div><div class="dialog-actions">${button('取消','close-dialog')}<button class="button primary" type="submit">保存快照</button></div></form>`);
    $('#snapshot-form').onsubmit=async e=>{e.preventDefault();const save=$('button[type="submit"]',e.target);save.disabled=true;try{const saved=await api('/api/snapshots',{label:$('#snapshot-label').value});currentSnapshot=saved;closeDialog();await historyPage();toast(saved.excel_backup?.status==='ready'?'会议快照与Excel备份已保存':'快照已保存，Excel备份正在生成或等待重试',saved.excel_backup?.status==='error');}catch(err){$('#form-error').textContent=err.message;save.disabled=false;}};return;
  }
  if(action==='compare-snapshots')return compareSnapshots();
  if(action==='export-snapshot') {
    const s=currentSnapshot.snapshot,originalText=el.textContent;el.disabled=true;el.textContent='正在准备Excel…';
    try {
      const response=await fetch(`/api/snapshots/${encodeURIComponent(s.id)}/excel`);
      if(!response.ok){const failure=await response.json();throw new Error(failure.error||'Excel备份尚未生成，请稍后重试');}
      const blob=await response.blob(),url=URL.createObjectURL(blob),link=document.createElement('a');
      link.href=url;link.download=`SOP快照_${s.business_date}_${s.id}.xlsx`;document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),10000);
      await historyPage();toast('Excel备份已准备好');
    }finally{el.disabled=false;el.textContent=originalText;}
    return;
  }
  if(action==='export-match') {const d=window.currentMatching;return exportCSV(`匹配率_${matchMonth}.csv`,['提前周次','业务日期','SOP计划','实际发货','匹配率%','权重','贡献百分点','状态'],d.rows.map(r=>[r.week,r.business_date,r.plan,r.actual,r.match,r.weight,r.contribution,statusText(r.status)]));}
}
document.addEventListener('click',async e=>{
  const el=e.target.closest('button');if(!el)return;
  try {
    if(el.dataset.catalogBind){bindProductSource(el.dataset.catalogBind);return;}
    if(el.dataset.catalogEdit){editProduct(el.dataset.catalogEdit);return;}
    if(el.dataset.page){page=el.dataset.page;if(page==='matching')query='';await renderPage();return;}
    if('brand' in el.dataset){brand=el.dataset.brand;model='';matchProduct='';query='';selected=null;await renderPage();return;}
    if(el.dataset.range){range=el.dataset.range;planning();return;}
    if(el.dataset.expand){expanded.has(el.dataset.expand)?expanded.delete(el.dataset.expand):expanded.add(el.dataset.expand);planning();return;}
    if(el.dataset.edit){editPlan(el.dataset.edit,el.dataset.month);return;}
    if(el.dataset.detail){await showDetail(el.dataset.detail);return;}
    if(el.dataset.snapshot){currentSnapshot=await api(`/api/snapshots/${encodeURIComponent(el.dataset.snapshot)}`);renderSnapshot(currentSnapshot);return;}
    if(el.dataset.matchSnapshot){currentSnapshot=await api(`/api/snapshots/${encodeURIComponent(el.dataset.matchSnapshot)}`);page='history';await renderPage();return;}
    if(el.hasAttribute('data-close'))return closeDialog();
    if(el.dataset.action)await handleAction(el.dataset.action,el);
  }catch(err){toast(err.message,true);}
});
document.addEventListener('change',async e=>{
  try {
    if(e.target.id==='range-filter'){range=e.target.value==='annual'?'annual':'rolling';planning();}
    if(e.target.id==='year-filter'){selectPlanYear(e.target.value);planning();}
    if(e.target.id==='brand-filter'){brand=e.target.value;model='';matchProduct='';query='';selected=null;planning();}
    if(e.target.id==='model-filter'){model=e.target.value;selected=null;matchProduct='';await renderPage();}
    if(e.target.id==='history-range'){historyRange=e.target.value;planning();}
    if(e.target.id==='metric-filter'){metric=e.target.value;planning();}
    if(e.target.id==='target-filter'){target=e.target.value;planning();}
    if(e.target.id==='match-month'){if(!e.target.value)return;matchMonth=e.target.value;await matchingPage();}
    if(e.target.id==='match-product'){matchProduct=e.target.value;await matchingPage();}
  }catch(err){toast(err.message,true);}
});
let searchTimer;
document.addEventListener('input',e=>{if(e.target.id==='version-search'){clearTimeout(searchTimer);query=e.target.value;searchTimer=setTimeout(async()=>{const pos=e.target.selectionStart;await renderPage();const inp=$('#version-search');if(inp){inp.focus();inp.setSelectionRange(pos,pos)}},300);}});
$('#owner-btn')?.addEventListener('click',()=>{
  openDialog(`<form id="owner-form"><h2>设置维护人</h2><p class="subtitle">姓名将记录在之后保存的计划中。</p><label class="field">姓名<input id="owner-name" value="${esc(owner)}" required maxlength="60"></label><div class="dialog-actions">${button('取消','close-dialog')}<button class="button primary" type="submit">保存</button></div></form>`);
  $('#owner-form').onsubmit=e=>{e.preventDefault();owner=$('#owner-name').value.trim();localStorage.setItem('sop-owner',owner);$('#owner-btn').textContent=owner;closeDialog();};
});
$('#refresh-btn')?.addEventListener('click',async()=>{try{await load();toast('已载入最新计划');}catch(err){toast(err.message,true);}});
window.addEventListener('unhandledrejection',e=>toast(e.reason?.message||'加载失败，请重试',true));
load().catch(err=>{$('#app').innerHTML=`<div class="empty-state"><h1>暂时无法连接数据服务</h1><p>${esc(err.message)}</p><p>请使用项目中的启动脚本打开看板。</p></div>`;});

// Refresh only after a completed spreadsheet synchronization; preserve dialogs and scrolling.
setInterval(async()=>{
  if(!state||document.hidden||$('#edit-dialog').open)return;
  try{const sync=await api('/api/plan-workbook/status');const previous=state.workbook_sync||{};
    if(sync.last_sync!==previous.last_sync||sync.status!==previous.status||sync.message!==previous.message||JSON.stringify(sync.source_refresh||{})!==JSON.stringify(state.source_refresh||{})){
      const scrolls=$$('.table-scroll').map(el=>({left:el.scrollLeft,top:el.scrollTop}));
      await load();$$('.table-scroll').forEach((el,i)=>{if(scrolls[i]){el.scrollLeft=scrolls[i].left;el.scrollTop=scrolls[i].top;}});
    }
  }catch(e){/* The next scheduled check retries; manual refresh shows connection errors. */}
},8000);
