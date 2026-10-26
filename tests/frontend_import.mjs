import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const definitions=source.split("document.addEventListener('click'")[0];
const requests=[],messages=[];
let readCount=0,readFailure=false,refreshes=0,responseHandler;
const context=vm.createContext({
  localStorage:{getItem:()=>null},
  fetch:async(url,options)=>{requests.push({url,body:JSON.parse(options.body),options});return responseHandler();},
  FileReader:class {
    readAsDataURL(file){readCount++;if(readFailure)return this.onerror();this.result=`data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,${file.encoded||'dGVzdA=='}`;this.onload();}
  },
});
vm.runInContext(definitions,context);
const run=code=>vm.runInContext(code,context);
const clone=value=>JSON.parse(JSON.stringify(value));
run("state={products:[{id:'p1',brand:'问界',model:'M5',version:'Max <一>'}]}");
const catalogConflict=run("planConflictTableHtml([{product_id:'p1',kind:'catalog',field:'version',baseline:'旧名',workbook:'新<名>',database:'网页名'},{product_id:'p1',kind:'catalog',field:'active',baseline:true,workbook:false,database:true,reason:'plans_changed'}])");
assert.match(catalogConflict,/版本名称/);
assert.match(catalogConflict,/新&lt;名&gt;/);
assert.match(catalogConflict,/启用状态/);
assert.match(catalogConflict,/停用/);
assert.match(catalogConflict,/导出后计划已更新/);
assert.doesNotMatch(catalogConflict,/NaN|undefined/);
context.validFile={name:'SOP计划.xlsx',size:100,encoded:'cGxhbjEyMw=='};

for(const file of [null,{name:'计划.xls',size:100},{name:'计划.xlsx',size:0},{name:'计划.xlsx',size:12*1024*1024+1}]){
  context.testFile=file;
  assert.throws(()=>run('validatePlanImportFile(testFile)'),/请选择|请先|为空|12MB/);
}
context.testFile={name:'计划.XLSX',size:12*1024*1024};
assert.equal(run('validatePlanImportFile(testFile).size'),12*1024*1024);
const prepared=clone(await run('readPlanImportFile(validFile)'));
assert.deepEqual(prepared,{filename:'SOP计划.xlsx',content_base64:'cGxhbjEyMw=='});
context.prepared=prepared;
responseHandler=()=>({ok:true,json:async()=>({ok:true,imported:1,created:0})});
await run('submitPlanImport(prepared)');
assert.deepEqual(requests.at(-1).body,prepared);
assert.equal(requests.at(-1).url,'/api/plans/import-xlsx');
await run("submitPlanImport(prepared,'workbook')");
assert.deepEqual(requests.at(-1).body,{...prepared,choice:'workbook'});
await assert.rejects(()=>run("submitPlanImport(prepared,'unexpected')"),/处理方式/);
responseHandler=()=>({ok:false,status:409,json:async()=>({source:'maintenance',conflicts:[]})});
await assert.rejects(()=>run('submitPlanImport(prepared)'),err=>err.status===409&&err.data.source==='maintenance');
responseHandler=()=>({ok:false,status:502,json:async()=>{throw new Error('not JSON');}});
await assert.rejects(()=>run('submitPlanImport(prepared)'),/未能确认导入结果/);
readFailure=true;
await assert.rejects(()=>run('readPlanImportFile(validFile)'),/读取文件失败/);
readFailure=false;

// Exercise the actual import dialog handlers without a live service or any production data.
class Element {
  constructor(){this.disabled=false;this.hidden=false;this.textContent='';this.attrs={};this.dataset={};this.listeners={};this.files=[];this.focused=false;this.html='';}
  setAttribute(k,v){this.attrs[k]=v;}
  getAttribute(k){return this.attrs[k];}
  addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);}
  removeEventListener(k,fn){this.listeners[k]=(this.listeners[k]||[]).filter(x=>x!==fn);}
  focus(){this.focused=true;}
  set innerHTML(value){this.html=value;if(this.conflictContainer){this.choices=[...value.matchAll(/data-import-choice="([^"]+)"/g)].map(match=>{const el=new Element();el.dataset.importChoice=match[1];return el;});this.maintenanceChoices=[...value.matchAll(/data-maintenance-choice="([^"]+)"/g)].map(match=>{const el=new Element();el.dataset.maintenanceChoice=match[1];return el;});}}
  get innerHTML(){return this.html;}
}
function setupDialog(){
  const ids=['import-plan-form','import-plan-file','import-save','import-error','import-progress','import-conflicts','edit-dialog'];
  const map=new Map(ids.map(id=>['#'+id,new Element()]));
  const form=map.get('#import-plan-form'),input=map.get('#import-plan-file'),save=map.get('#import-save'),conflicts=map.get('#import-conflicts'),dialog=map.get('#edit-dialog');
  const close=new Element(),cancel=new Element();save.disabled=true;conflicts.conflictContainer=true;conflicts.choices=[];conflicts.maintenanceChoices=[];
  form.querySelectorAll=selector=>{assert.equal(selector,'input,button');return[input,save,cancel,...conflicts.choices,...conflicts.maintenanceChoices];};
  conflicts.querySelectorAll=selector=>{assert.ok(['[data-import-choice]','[data-maintenance-choice]'].includes(selector));return selector==='[data-import-choice]'?conflicts.choices:conflicts.maintenanceChoices;};
  dialog.querySelector=selector=>{assert.equal(selector,'[data-close]');return close;};
  context.document={querySelector:selector=>map.get(selector)||null};
  context.openTestDialog=html=>{dialog.open=true;dialog.html=html;};
  context.closeTestDialog=()=>{dialog.open=false;(dialog.listeners.close||[]).forEach(fn=>fn());};
  context.refreshTestData=async()=>{refreshes++;};
  context.recordMessage=(text,error=false)=>messages.push({text,error});
  run('openDialog=openTestDialog;closeDialog=closeTestDialog;load=refreshTestData;toast=recordMessage;importPlans()');
  return {form,input,save,conflicts,dialog,close,error:map.get('#import-error'),progress:map.get('#import-progress')};
}
const submitEvent={preventDefault(){}};
const chooseFile=ui=>{ui.input.files=[context.validFile];ui.input.onchange();};
const conflict={product_id:'p1',month:'2026-10',baseline:100,workbook:120,database:110};
let ui=setupDialog();
assert.equal(ui.save.disabled,true);
assert.match(ui.dialog.html,/含版本明细/);
chooseFile(ui);
assert.equal(ui.save.disabled,false);
let resolveResponse;
responseHandler=()=>new Promise(resolve=>{resolveResponse=resolve;});
const requestCount=requests.length,readsBefore=readCount;
const pending=ui.form.onsubmit(submitEvent);
await new Promise(resolve=>setImmediate(resolve));
assert.equal(requests.length,requestCount+1);
assert.equal(ui.form.getAttribute('aria-busy'),'true');
assert.equal(ui.input.disabled,true);
assert.equal(ui.close.disabled,true);
assert.match(ui.progress.textContent,/正在检查/);
let canceled=false;
ui.dialog.listeners.cancel.forEach(fn=>fn({preventDefault(){canceled=true;}}));
assert.equal(canceled,true,'Escape cannot hide an in-flight import.');
await ui.form.onsubmit(submitEvent);
assert.equal(requests.length,requestCount+1,'Repeated submit is suppressed while importing.');
resolveResponse({ok:false,status:409,json:async()=>({status:'conflict',conflicts:[conflict]})});
await pending;
assert.equal(ui.dialog.open,true);
assert.equal(ui.close.disabled,false);
assert.equal(ui.error.focused,true);
assert.match(ui.error.textContent,/同时修改/);
assert.match(ui.conflicts.innerHTML,/Max &lt;一&gt;/);
assert.match(ui.conflicts.innerHTML,/100/);
assert.match(ui.conflicts.innerHTML,/120/);
assert.match(ui.conflicts.innerHTML,/110/);
assert.equal(ui.save.hidden,true);
assert.equal(ui.conflicts.choices.length,2);
responseHandler=()=>({ok:true,json:async()=>({ok:true,imported:2,created:1,warning:'维护表被Excel占用，关闭后自动回写'})});
await ui.conflicts.choices.find(el=>el.dataset.importChoice==='db').onclick();
assert.deepEqual(requests.at(-1).body,{...prepared,choice:'db'});
assert.equal(readCount,readsBefore+1,'Conflict resolution reuses the same uploaded bytes.');
assert.equal(refreshes,1);
assert.equal(ui.dialog.open,false);
assert.match(messages.at(-1).text,/已导入 2 项计划，新增 1 个版本/);
assert.match(messages.at(-1).text,/关闭后自动回写/);

ui=setupDialog();chooseFile(ui);
responseHandler=()=>({ok:false,status:409,json:async()=>({source:'maintenance',error:'统一维护表需要先解决同步冲突',conflicts:[conflict]})});
await ui.form.onsubmit(submitEvent);
assert.equal(ui.dialog.open,true);
assert.equal(ui.conflicts.choices.length,0,'Maintenance conflicts must not be resolved with uploaded-file choices.');
assert.match(ui.conflicts.innerHTML,/统一维护表值/);
assert.match(ui.error.textContent,/维护表/);
const maintenanceReads=readCount,maintenanceRequests=requests.length;
responseHandler=()=>({ok:true,json:async()=>({ok:true,status:'synced'})});
await ui.conflicts.maintenanceChoices.find(el=>el.dataset.maintenanceChoice==='workbook').onclick();
assert.equal(requests.length,maintenanceRequests+1);
assert.equal(requests.at(-1).url,'/api/plan-workbook/sync','The maintenance choice must never submit uploaded values.');
assert.deepEqual(requests.at(-1).body,{choice:'workbook'});
assert.equal(ui.dialog.open,true);
assert.equal(ui.save.hidden,false);
assert.match(ui.conflicts.innerHTML,/已保留选中的文件/);
assert.equal(ui.input.files[0],context.validFile);
responseHandler=()=>({ok:false,status:400,json:async()=>({error:'文件不是看板生成的计划表，请重新导出'})});
await ui.form.onsubmit(submitEvent);
assert.equal(readCount,maintenanceReads,'Handling maintenance conflicts must preserve the prepared upload bytes.');
assert.deepEqual(requests.at(-1).body,prepared,'After maintenance is resolved, upload starts with no carried-over resolution choice.');
assert.equal(ui.dialog.open,true);
assert.match(ui.error.textContent,/重新导出/);
assert.equal(ui.save.disabled,false);
ui.input.files=[{name:'wrong.csv',size:123}];ui.input.onchange();
assert.equal(ui.save.disabled,true);
assert.equal(ui.conflicts.innerHTML,'');
assert.match(ui.error.textContent,/xlsx/);
chooseFile(ui);
assert.equal(ui.error.textContent,'');
responseHandler=()=>({ok:true,json:async()=>({ok:true,imported:0,created:0})});
await ui.form.onsubmit(submitEvent);
assert.match(messages.at(-1).text,/没有需要导入的新修改/);

ui=setupDialog();chooseFile(ui);
run("load=async()=>{throw new Error('refresh unavailable')}");
responseHandler=()=>({ok:true,json:async()=>({ok:true,imported:1,created:0,warning:'Excel待回写'})});
await ui.form.onsubmit(submitEvent);
assert.equal(ui.dialog.open,false);
assert.match(messages.at(-1).text,/已导入 1 项计划.*页面刷新失败.*Excel待回写/);
assert.doesNotMatch(messages.at(-1).text,/导入失败/);

console.log('PASS: import validation, upload, loading, duplicate prevention, accessible errors, conflict choices, maintenance conflict separation, writeback warning, and safe retry.');
