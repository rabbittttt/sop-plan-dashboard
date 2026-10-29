/* Run with Node, an optional absolute path to Playwright and an optional origin.
 * Use http://sop-grid.test to cover insecure intranet HTTP without randomUUID.
 * All routes use synthetic data; no local service or business file is accessed.
 */
const {chromium} = require(process.argv[2] || 'playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const out = path.join(root, 'output_file/grid-check');
const origin = process.argv[3] || 'http://127.0.0.1:18877';

function fixture(mode='detail') {
  const product=(id,brand,model,version)=>({id,brand,model,version,catalog_model:model.replace(/ 20\d\d款$/,''),
    generation:brand+' '+model,source_generation:brand+' '+model,source_generations:[brand+' '+model],
    revision:1,active:true,start_month:'2026-01',end_month:null});
  const members=[product('a','享界','S9 2026款','Max'),product('b','享界','S9 2026款','Ultra')];
  return {current_month:'2026-10',plan_end:'2027-12',now:'2026-10-29T12:00:00+08:00',
    settings:{generation_mode:mode},generation_view:{mode,rule_signature:'fixture-rules',warnings:[]},
    meta:{quality:[],metrics:[]},workbook_sync:{status:'absent',message:'未建立维护表'},
    products:[product('x','智界','R7 2026款','Pro'),product('old','享界','S9 2024款','Max'),
      ...(mode==='summary'?[{...product('group','享界','X4 2026款','Max'),read_only:true,member_product_ids:['a','b']}]:members),
      product('w','问界','M5 2025款','Max')],
    plans:[{product_id:'a',month:'2026-10',sop:100,forecast:111,revision:1},
      {product_id:'a',month:'2026-11',sop:150,forecast:160,revision:1},
      {product_id:'b',month:'2026-10',sop:200,forecast:220,revision:1},
      {product_id:'old',month:'2026-10',sop:80,forecast:81,revision:1},
      {product_id:'x',month:'2026-10',sop:20,forecast:22,revision:1},
      {product_id:'w',month:'2026-10',sop:40,forecast:44,revision:1},
      {product_id:'group',month:'2026-10',sop:300,forecast:331,revision:0}],
    sales:[],model_sales:[],shipments:[],history_months:['2026-07','2026-08','2026-09']};
}

async function main() {
  await fs.mkdir(out,{recursive:true});
  let state=fixture(),failure='',savedRequests=[],exportRequest=null,stateRequests=0,loseResponse=false;
  const completed=new Map();
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  let page;
  try {
    page=await browser.newPage({viewport:{width:1440,height:1000}});const errors=[];
    page.on('pageerror',error=>errors.push(error.message));
    await page.addInitScript(()=>localStorage.setItem('sop-owner','浏览器测试'));
    await page.route(`${origin}/**`,async route=>{
      const url=new URL(route.request().url());
      if(url.pathname==='/api/state'){stateRequests++;return route.fulfill({json:state});}
      if(url.pathname==='/api/plan-workbook/status')return route.fulfill({json:{status:'absent',generation_view:state.generation_view}});
      if(url.pathname==='/api/settings') {
        state=fixture(route.request().postDataJSON().generation_mode);
        return route.fulfill({json:{ok:true,settings:state.settings}});
      }
      if(url.pathname==='/api/plans/batch-edit') {
        const request=route.request().postDataJSON();savedRequests.push(request);
        if(failure)return route.fulfill({status:409,json:{error:failure}});
        if(completed.has(request.request_id))return route.fulfill({json:completed.get(request.request_id)});
        for(const change of request.changes||[]) {
          let row=state.plans.find(p=>p.product_id===change.product_id&&p.month===change.month);
          if(!row){row={product_id:change.product_id,month:change.month,forecast:null,revision:0};state.plans.push(row);}
          row.sop=change.sop;row.revision++;
        }
        for(const change of request.catalog_changes||[])Object.assign(state.products.find(p=>p.id===change.id),change);
        const entry_ids={};
        for(const entry of request.entries||[]) {
          const id='created-'+entry.client_id;entry_ids[entry.client_id]=id;
          state.products.push({...entry,id,active:true,revision:1,start_month:'2026-10'});
          for(const plan of entry.plans||[])state.plans.push({...plan,product_id:id,forecast:null,revision:1});
        }
        const result={ok:true,entry_ids,created_count:(request.entries||[]).length,saved_count:(request.changes||[]).length};
        completed.set(request.request_id,result);
        if(loseResponse){loseResponse=false;return route.abort('connectionreset');}
        return route.fulfill({json:result});
      }
      if(url.pathname==='/api/plans/export-xlsx') {
        exportRequest=route.request().postDataJSON();
        return route.fulfill({body:Buffer.from('PKtest'),contentType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'});
      }
      if(url.pathname.startsWith('/api/'))throw new Error('Unexpected API: '+url.pathname);
      const name=url.pathname==='/'?'index.html':url.pathname.slice(1);
      const contentType=name.endsWith('.js')?'application/javascript':name.endsWith('.css')?'text/css':name.endsWith('.svg')?'image/svg+xml':'text/html';
      return route.fulfill({body:await fs.readFile(path.join(root,'web',name)),contentType});
    });
    await page.goto(origin+'/');
    if(!origin.includes('127.0.0.1')) {
      assert.equal(await page.evaluate(()=>window.isSecureContext),false,'Use real insecure HTTP browser semantics');
      assert.equal(await page.evaluate(()=>typeof crypto.randomUUID),'undefined','Exercise UUID fallback without the native method');
    }
    await page.locator('[data-action="bulk-plan"]').waitFor();
    const click=action=>page.locator(`[data-action="${action}"]`).click();
    const openViews=async()=>{if(!await page.locator('#table-view-name').count())await click('view-panel');};
    const cell=(id,month)=>page.locator(`[data-grid-id="${id}"][data-grid-month="${month}"]`);
    await openViews();
    await page.locator('#table-view-name').fill('初始视图');
    await click('view-save');
    await click('view-panel');
    await click('bulk-plan');
    await page.locator('#grid-owner').waitFor();
    assert.equal(await page.locator('#edit-dialog').evaluate(el=>el.open),false,'Batch edit must remain inside the main table');
    assert.equal(await cell('a','2026-10').inputValue(),'100');
    await cell('a','2026-10').fill('0');
    await cell('a','2026-11').fill('');
    await cell('x','2026-10').fill('-1');
    await click('grid-save');
    assert.match(await page.locator('#grid-error').innerText(),/非负整数/);
    assert.equal(await cell('x','2026-10').getAttribute('aria-invalid'),'true');
    assert.equal(savedRequests.length,0,'Invalid input must not be submitted');
    await cell('x','2026-10').fill('20');
    await page.locator('[data-grid-remove="old"]').click();
    assert.match(await page.locator('[data-grid-remove="old"]').innerText(),/撤销/);
    await page.locator('[data-grid-remove="old"]').click();
    assert.match(await page.locator('[data-grid-remove="old"]').innerText(),/删除/);
    await page.locator('[data-grid-remove="old"]').click();
    await page.locator('[data-grid-id="b"][data-grid-field="version"]').fill('Ultra改名');
    await click('grid-add');
    const newId=await page.locator('[data-new-row]').last().getAttribute('data-new-row');
    const newField=field=>page.locator(`[data-grid-id="${newId}"][data-grid-field="${field}"]`);
    await newField('brand').fill('享界');await newField('model').fill('S9 2026款');await newField('version').fill('新增Lite');
    await cell(newId,'2026-10').fill('5');
    const requestsBefore=stateRequests;
    await page.locator('#refresh-btn').click();
    await page.evaluate(()=>pollWorkbookStatus());
    await page.locator('[data-page="history"]').click();
    assert.equal(stateRequests,requestsBefore,'Refreshing or polling cannot replace an active draft');
    assert.equal(await cell('a','2026-10').inputValue(),'0');
    assert.equal(await cell(newId,'2026-10').inputValue(),'5');
    await page.screenshot({path:path.join(out,'editing-desktop.png'),fullPage:true});
    failure='计划已被其他人更新，请取消后刷新核对';
    await click('grid-save');
    await page.getByText(failure,{exact:false}).first().waitFor();
    assert.equal(await cell('a','2026-10').inputValue(),'0');
    assert.equal(await cell(newId,'2026-10').inputValue(),'5');
    const payload=savedRequests.at(-1);
    assert.ok(payload.changes.some(c=>c.product_id==='a'&&c.month==='2026-10'&&c.sop===0));
    assert.ok(payload.changes.some(c=>c.product_id==='a'&&c.month==='2026-11'&&c.sop===null));
    assert.ok(payload.catalog_changes.some(c=>c.id==='old'&&c.active===false&&c.plan_revisions['2026-10']===1));
    assert.ok(payload.catalog_changes.some(c=>c.id==='b'&&c.version==='Ultra改名'));
    assert.equal(payload.entries[0].plans[0].sop,5);
    failure='';loseResponse=true;
    await click('grid-save');
    await page.getByText('重试确认保存',{exact:true}).waitFor();
    const uncertainId=savedRequests.at(-1).request_id,createdCount=state.products.filter(p=>p.id.startsWith('created-')).length;
    assert.equal(await cell('a','2026-10').isDisabled(),true,'An uncertain save must not create a second mutable request');
    await click('grid-save');
    await page.locator('#grid-owner').waitFor({state:'detached'});
    assert.equal(savedRequests.at(-1).request_id,uncertainId);
    assert.equal(state.products.filter(p=>p.id.startsWith('created-')).length,createdCount);
    assert.equal(await page.locator('#grid-owner').count(),0);
    assert.equal(await page.locator('[data-version="old"]').count(),0,'Deleted row is hidden after saving');
    await page.reload();
    await page.locator('[data-action="bulk-plan"]').waitFor();
    await click('expand-all');
    assert.equal(await page.locator('[data-version="old"]').count(),0,'Deleted row stays hidden after page reload');
    console.log('PASS: draft editing, validation, conflict recovery and uncertain-response retry.');

    // Cancel never writes; draft row removal affects only that new row.
    await click('bulk-plan');
    await cell('a','2026-10').fill('99');
    await click('grid-add');
    const removable=await page.locator('[data-new-row]').last().getAttribute('data-new-row');
    await page.locator(`[data-grid-remove="${removable}"]`).click();
    assert.equal(await page.locator(`[data-new-row="${removable}"]`).count(),0);
    const callsBefore=savedRequests.length;
    await click('grid-cancel');
    assert.equal(savedRequests.length,callsBefore);
    assert.equal(state.plans.find(p=>p.product_id==='a'&&p.month==='2026-10').sop,0);

    // Hide versions/models, preserve totals, save and reload the named view.
    const hiddenBefore=Number((await page.locator('.saved-view-strip').innerText()).match(/已隐藏(\d+)组/)?.[1]||0);
    if(!await page.locator('[data-hide-level="version"][data-hide-id="a"]').count())await click('expand-all');
    const totalBefore=await page.locator('tr.total-row.metric-plan-row').innerText();
    await page.locator('[data-hide-level="version"][data-hide-id="a"]').first().click();
    assert.equal(await page.locator('[data-version="a"]').count(),0);
    assert.equal(await page.locator('tr.total-row.metric-plan-row').innerText(),totalBefore);
    await page.locator('[data-hide-level="model"][data-hide-id="智界|R7 2026款"]').first().click();
    assert.equal(await page.locator('[data-version="x"]').count(),0);
    await openViews();
    await page.locator('#table-view-name').fill('重点车型');
    await click('view-save');
    await page.reload();
    await page.locator('[data-action="bulk-plan"]').waitFor();
    assert.equal(await page.locator('[data-version="a"],[data-version="x"]').count(),0);
    assert.match(await page.locator('.saved-view-strip').innerText(),new RegExp(`已隐藏${hiddenBefore+2}组`));
    await openViews();
    await click('rows-restore');
    assert.ok(await page.locator('[data-version="a"]').count()>0);
    assert.ok(await page.locator('[data-version="x"]').count()>0);
    assert.ok(await page.locator('[data-version="old"]').count()>0,'Restoring hidden rows reveals preserved inactive history');
    assert.equal(state.products.find(p=>p.id==='old').active,false,'Restoring visibility must not reactivate the product');
    await page.locator('#saved-table-view').selectOption({label:'重点车型'});
    await click('view-apply');
    assert.equal(await page.locator('[data-version="a"],[data-version="x"],[data-version="old"]').count(),0,'Applying a named view restores its saved hidden rows');
    await click('rows-restore');

    // A summary view must keep derived rows read-only even in grid mode.
    await page.locator('#generation-filter').selectOption('summary');
    await click('bulk-plan');
    assert.equal(await page.locator('[data-grid-id="group"]').count(),0);
    assert.ok(await cell('x','2026-10').count()>0);
    await page.setViewportSize({width:1000,height:900});
    await page.screenshot({path:path.join(out,'editing-summary-narrow.png'),fullPage:true});
    await click('grid-cancel');
    await openViews();
    await page.locator('#saved-table-view').selectOption({label:'重点车型'});
    await click('view-apply');
    assert.match(await page.locator('#toast').innerText(),/先切换代际模式/);
    assert.deepEqual(errors,[]);
    console.log(`PASS (${origin}): in-table editing, zero/blank, add/delete/undo/cancel, failure recovery, idempotent retry, draft guards, persistent hidden rows/totals, named view reload and readonly summary.`);
  } catch(error) {
    if(page)await page.screenshot({path:path.join(out,'failure.png'),fullPage:true}).catch(()=>{});
    throw error;
  } finally {await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
