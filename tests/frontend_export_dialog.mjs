import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const context=vm.createContext({localStorage:{getItem:()=>null}});
vm.runInContext(source.split("document.addEventListener('click'")[0],context);
const run=code=>vm.runInContext(code,context);
let form,map,monthInputs,controls,active,html,submitted,fail=false;
const element=(props={})=>({checked:false,disabled:false,textContent:'',attrs:{},dataset:{},
  setAttribute(k,v){this.attrs[k]=v;},getAttribute(k){return this.attrs[k];},
  removeAttribute(k){delete this.attrs[k];},...props});
context.captureDialog=markup=>{
  html=markup;
  map=new Map(['export-plan-form','export-scope','export-selection','export-error','export-locks','export-wenjie','export-versions','export-filtered']
    .map(id=>['#'+id,element()]));
  form=map.get('#export-plan-form');active=form;
  const save=element(),preset=element(),cancel=element();
  map.set('button[type="submit"]',save);
  map.set('[data-export-preset="maintenance"]',preset);
  monthInputs=[...html.matchAll(/<input type="checkbox" name="export-month" value="([^"]+)"[^>]*>/g)]
    .map(match=>element({value:match[1],checked:/\bchecked\b/.test(match[0])}));
  const monthButtons=['current','all','none'].map(value=>element({dataset:{exportMonths:value}}));
  controls=[...monthInputs,map.get('#export-locks'),map.get('#export-wenjie'),map.get('#export-versions'),map.get('#export-filtered'),save,preset,cancel,...monthButtons];
  form.querySelector=selector=>map.get(selector)||null;
  form.querySelectorAll=selector=>selector==='input[name="export-month"]'?monthInputs
    :selector==='[data-export-months]'?monthButtons:selector==='input,button'?controls:[];
};
context.document={querySelector:selector=>selector==='#export-plan-form'?active:null};
context.captureDownload=async options=>{
  submitted=JSON.parse(JSON.stringify(options));
  if(fail)throw new Error('计划表生成失败，测试错误信息');
};
context.closeCapturedDialog=()=>{active=null;};
run(`
  openDialog=captureDialog;downloadPlanExport=captureDownload;closeDialog=closeCapturedDialog;
  state={current_month:'2026-10',plan_end:'2027-12',months:['2026-10','2027-12'],
    products:[{id:'a',brand:'问界',model:'M5',version:'Max'},{id:'b',brand:'智界',model:'R7',version:'Pro'}]};
  brand='问界';model='M5';query='';range='rolling';showLocks=true;
  exportPlans();
`);
const chosen=()=>monthInputs.filter(input=>input.checked).map(input=>input.value);
const clickMonths=type=>form.querySelectorAll('[data-export-months]').find(button=>button.dataset.exportMonths===type).onclick();
const submit=()=>form.onsubmit({preventDefault(){},target:form});
assert.equal(chosen().length,24);
assert.equal(chosen()[0],'2026-01');
assert.equal(chosen().at(-1),'2027-12');
assert.equal(map.get('#export-locks').checked,false);
assert.equal(map.get('#export-versions').checked,true);
assert.equal(map.get('#export-wenjie').checked,true);
assert.equal(map.get('#export-filtered').checked,false);
assert.match(map.get('#export-scope').textContent,/全部品牌.*2 个车型/);
assert.equal(map.get('button[type="submit"]').textContent,'导出维护模板');

// User customizations remain available and can be reset in one click.
clickMonths('current');
assert.equal(chosen().length,12);
assert.equal(chosen()[0],'2026-05');
map.get('#export-locks').checked=true;map.get('#export-locks').onchange();
map.get('#export-versions').checked=false;map.get('#export-versions').onchange();
map.get('#export-filtered').checked=true;map.get('#export-filtered').onchange();
assert.match(map.get('#export-scope').textContent,/问界.*1 个车型/);
assert.equal(map.get('button[type="submit"]').textContent,'导出只读车型汇总');
fail=true;
await submit();
assert.equal(submitted.includeLocks,true);
assert.equal(submitted.includeVersions,false);
assert.equal(submitted.allProducts,false);
assert.equal(submitted.selectedMonths.length,12);
assert.match(map.get('#export-error').textContent,/测试错误信息/,'The finally block must retain the export error.');
assert.equal(map.get('button[type="submit"]').disabled,false);
clickMonths('none');
assert.equal(map.get('button[type="submit"]').disabled,true);
map.get('[data-export-preset="maintenance"]').onclick();
assert.equal(chosen().length,24);
assert.equal(map.get('#export-error').textContent,'');
fail=false;
await submit();
assert.deepEqual(submitted,{includeLocks:false,includeVersions:true,includeWenjie:true,allProducts:true,selectedMonths:chosen()});
assert.equal(active,null,'A successful export closes cleanly without touching removed dialog elements.');

// The export brand option follows the page initially but can be overridden.
run("showWenjie=false;brand='';model='';exportPlans()");
assert.equal(map.get('#export-wenjie').checked,false);
assert.match(map.get('#export-scope').textContent,/不含问界.*1 个车型/);
await submit();
assert.equal(submitted.includeWenjie,false);
run('exportPlans()');
map.get('#export-wenjie').checked=true;map.get('#export-wenjie').onchange();
assert.match(map.get('#export-scope').textContent,/全部品牌.*2 个车型/);
assert.equal(run('showWenjie'),false,'Export choices must not change the page preference.');
map.get('#export-wenjie').checked=false;map.get('#export-wenjie').onchange();
map.get('[data-export-preset="maintenance"]').onclick();
assert.equal(map.get('#export-wenjie').checked,true,'Restore maintenance format includes all brands.');
assert.equal(map.get('button[type="submit"]').textContent,'导出维护模板');
console.log('PASS: full maintenance defaults, custom scope/months/metrics, reset, export errors and successful close.');
