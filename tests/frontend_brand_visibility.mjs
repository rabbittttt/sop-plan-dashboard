import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const storage=new Map(),screen={innerHTML:''},scroll={scrollLeft:123};
let focused=false;
const context=vm.createContext({
  localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,value)},
  document:{querySelector:selector=>selector==='#app'?screen:selector==='.plan-card .table-scroll'?scroll:
    selector==='[data-action="toggle-wenjie"]'?{focus(){focused=true;}}:null},
});
vm.runInContext(source.split("document.addEventListener('click'")[0],context);
const run=code=>vm.runInContext(code,context);
const get=code=>JSON.parse(JSON.stringify(run(code)));
run(`
  state={current_month:'2026-10',plan_end:'2027-12',meta:{},sales:[],model_sales:[],
    products:['问界','尚界','尊界','智界','享界'].map((brand,i)=>({id:String(i),brand,model:'M'+i,version:'Max',active:true})),
    plans:[]};
  state.plans=state.products.map((p,i)=>({product_id:p.id,month:'2026-10',sop:(i+1)*100}));
  metric='locked_orders';indexData();planning();
`);
const brandRows=()=>[...screen.innerHTML.matchAll(/<tr class="group-row[^"]*"[^>]*><th[^>]*><strong>(.*?)<\/strong>/g)].map(m=>m[1]);
const order=['智界','享界','尊界','尚界','问界'];
assert.deepEqual(brandRows(),order);
assert.deepEqual(get('products().map(p=>p.brand)'),order);
assert.equal(run('showWenjie'),true);
await run("handleAction('toggle-wenjie')");
assert.deepEqual(brandRows(),order.slice(0,4));
assert.equal(get("sumPlan(products(),'2026-10','sop').value"),1400);
assert.match(screen.innerHTML,/合计（不含问界）/);
assert.doesNotMatch(screen.innerHTML,/<option value="问界"|<option value="M0"/);
assert.equal(storage.get('sop-show-wenjie'),'false');
assert.equal(scroll.scrollLeft,123);
assert.equal(focused,true);
assert.match(screen.innerHTML,/data-action="toggle-wenjie" aria-pressed="false"/);

// Navigation from another page cannot leave a hidden brand as a stale filter.
run("page='matching';brand='问界';model='M0';selected='0'");
assert.deepEqual(get('products().map(p=>p.id)'),['0'],'The matching page retains its own full brand scope.');
run("page='planning';planning()");
assert.equal(run('brand'),'');assert.equal(run('model'),'');assert.equal(run('selected'),null);
assert.deepEqual(brandRows(),order.slice(0,4));
assert.equal(get('exportProducts(maintenanceExportOptions()).length'),5);
assert.equal(get('state.products.length'),5);

// A new load uses the persisted visibility preference.
const reloaded=vm.createContext({localStorage:{getItem:key=>storage.get(key)??null}});
vm.runInContext(source.split("document.addEventListener('click'")[0],reloaded);
assert.equal(vm.runInContext('showWenjie',reloaded),false);
await run("handleAction('toggle-wenjie')");
assert.deepEqual(brandRows(),order);
assert.equal(get("sumPlan(products(),'2026-10','sop').value"),1500);
console.log('PASS: brand order, visibility, totals, saved preference, focus, navigation and full maintenance scope.');
