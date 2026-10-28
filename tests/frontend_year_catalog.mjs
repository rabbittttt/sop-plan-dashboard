import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const nodes=new Map(),requests=[];
const element=(value='')=>({value,innerHTML:'',textContent:'',disabled:false,focus(){},querySelector(){return submit;}});
const submit=element();
const document={querySelector:selector=>nodes.get(selector)||null,querySelectorAll:()=>[]};
nodes.set('#app',element());
let html='';
const openForm=value=>{
  html=value;
  for(const match of value.matchAll(/<(input|form)\b([^>]*)>/g)){
    const id=match[2].match(/\bid="([^"]+)"/)?.[1];
    if(id)nodes.set('#'+id,element(match[2].match(/\bvalue="([^"]*)"/)?.[1]||''));
  }
  for(const match of value.matchAll(/<select\b([^>]*)>([\s\S]*?)<\/select>/g)){
    const id=match[1].match(/\bid="([^"]+)"/)?.[1];
    const choices=[...match[2].matchAll(/<option value="([^"]*)"([^>]*)>/g)];
    if(id)nodes.set('#'+id,element((choices.find(c=>c[2].includes('selected'))||choices[0])?.[1]||''));
  }
};
const initial={current_month:'2026-10',plan_end:'2027-12',meta:{},settings:{generation_mode:'detail'},
  products:[
    {id:'old',brand:'享界',model:'S9 2025款',catalog_model:'S9',version:'Max',source_generation:'享界 S9 2025款',revision:1},
    {id:'new',brand:'享界',model:'S9 2026款',catalog_model:'S9',version:'Max',source_generation:'享界 S9 2026款',revision:2},
    {id:'beta',brand:'享界',model:'S9 2026款',catalog_model:'S9 BETA',version:'Ultra',source_generation:'享界 S9 BETA 2026款',revision:3},
  ],plans:[],sales:[],model_sales:[],shipments:[],workbook_sync:{status:'absent'}};
const context=vm.createContext({document,localStorage:{getItem:()=>null,setItem(){}},initial,
  openForm,request:async(path,body)=>{requests.push({path,body:JSON.parse(JSON.stringify(body))});return{};}});
const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
vm.runInContext(source.split("document.addEventListener('click'")[0],context);
const run=code=>vm.runInContext(code,context),get=id=>nodes.get('#'+id);
run('state=initial;indexData();api=request;openDialog=openForm;closeDialog=()=>{};load=async()=>{};productManager=()=>{};toast=()=>{}');
const send=id=>get(id).onsubmit({preventDefault(){},target:get(id)});

// Two year-model groups and their export rows stay distinct.
run('planning()');
assert.match(get('app').innerHTML,/<option value="S9 2025款"/);
assert.match(get('app').innerHTML,/<option value="S9 2026款"/);
const exported=run("buildPlanExport({includeVersions:true,selectedMonths:['2026-10']})");
assert.deepEqual([...new Set(exported.rows.map(row=>row[2]))],['S9 2025款','S9 2026款']);

// Editing only a version submits the original catalog model, never the display suffix.
run("editProduct('old')");
assert.equal(get('catalog-model').value,'S9');
assert.match(html,/来源：享界 S9 2025款/);
get('catalog-version').value='Max 新名称';get('catalog-owner').value='测试';
await send('catalog-form');
assert.equal(requests.at(-1).body.changes[0].model,'S9');
assert.equal(requests.at(-1).body.changes[0].version,'Max 新名称');
assert.equal(requests.at(-1).body.changes[0].id,'old');

// A new manual version retains an explicitly selected year in its editable model.
run("brand='享界';model='S9 2026款';editProduct()");
assert.equal(get('catalog-model').value,'S9 2026款');
get('catalog-version').value='手工新版本';get('catalog-owner').value='测试';
await send('catalog-form');
assert.equal(requests.at(-1).body.changes[0].model,'S9 2026款');
assert.equal(requests.at(-1).body.changes[0].id,undefined);

// Renaming one year does not rename another year or erase a BETA source label on a no-op.
run("brand='享界';model='S9 2026款';renameModel()");
assert.equal(get('rename-from').value,'享界\tS9 2026款');
assert.equal(get('rename-to').value,'S9');
get('rename-owner').value='测试';
await send('rename-model-form');
assert.deepEqual(requests.at(-1).body.changes.map(c=>[c.id,c.model]),[['new','S9'],['beta','S9 BETA']]);
run("brand='享界';model='S9 2026款';renameModel()");
get('rename-to').value='S9改名';get('rename-owner').value='测试';
await send('rename-model-form');
assert.deepEqual(requests.at(-1).body.changes.map(c=>[c.id,c.model]),[['new','S9改名'],['beta','S9改名']]);

// Shared save protection also handles callers that pass an unchanged display name.
await run("saveCatalog([{id:'old',model:'S9 2025款',version:'Max'}],'测试')");
assert.equal(requests.at(-1).body.changes[0].model,'S9');
console.log('PASS: year-specific display/export scopes and catalog edits preserve source model identities.');
