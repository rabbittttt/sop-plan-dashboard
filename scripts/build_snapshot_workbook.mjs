import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Workbook, SpreadsheetFile, FileBlob } from '@oai/artifact-tool';
import { PYTHON } from './runtime.mjs';

const log = (...values) => process.stderr.write(values.map(String).join(' ') + '\n');
console.log = log;
const key = (id, month) => JSON.stringify([id, month]);
const column = number => {
  let text = '';
  for (; number; number = Math.floor((number - 1) / 26)) text = String.fromCharCode(65 + (number - 1) % 26) + text;
  return text;
};
const titleForKind = value => ({daily:'每日快照',sunday:'周日考核快照',manual:'会议快照'}[value] || value || '未记录');
const scalar = value => value == null ? null : typeof value === 'object' ? JSON.stringify(value) : value;
const textValue = value => typeof value === 'string' && /^[=+@-]/.test(value) ? "'" + value : value;

async function main() {
  let raw = '';
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) raw += chunk;
  const input = JSON.parse(raw.replace(/^\uFEFF/, ''));
  if (!input.output_path || !input.snapshot_data || typeof input.snapshot_data !== 'object') throw new Error('output_path and snapshot_data are required');
  const frozen = input.snapshot_data, snapshot = frozen.snapshot || {}, meta = frozen.meta || {};
  const plans = Array.isArray(frozen.plans) ? frozen.plans : [];
  const sourceProducts = Array.isArray(frozen.products) ? frozen.products : [];
  const exportedAt = String(input.exported_at ?? new Date().toISOString());
  const outputPath = path.resolve(input.output_path);
  const products = sourceProducts.map(p => ({...p}));
  const directory = new Map();
  for (const product of products) {
    if (typeof product.id !== 'string' || !product.id || directory.has(product.id)) throw new Error('Snapshot product IDs must be unique nonempty strings');
    directory.set(product.id, product);
  }
  const monthSet = new Set(Array.isArray(meta.months) ? meta.months : []);
  const lookup = new Map();
  let orphanCount = 0;
  for (const plan of plans) {
    if (typeof plan.product_id !== 'string' || !plan.product_id) throw new Error('Snapshot plan product_id is missing');
    if (typeof plan.month !== 'string' || !/^\d{4}-(0[1-9]|1[0-2])$/.test(plan.month)) throw new Error('Invalid snapshot plan month');
    const planKey = key(plan.product_id, plan.month);
    if (lookup.has(planKey)) throw new Error('Duplicate snapshot plan key');
    for (const field of ['sop','forecast']) if (plan[field] != null && (typeof plan[field] !== 'number' || !Number.isFinite(plan[field]))) throw new Error(`Invalid snapshot ${field} quantity`);
    lookup.set(planKey, plan);
    monthSet.add(plan.month);
    if (!directory.has(plan.product_id)) {
      const placeholder = {id:plan.product_id,brand:'未记录品牌',model:'快照目录缺失',version:'原产品ID保留'};
      products.push(placeholder); directory.set(placeholder.id, placeholder); orphanCount++;
    }
  }
  if ([...monthSet].some(month => typeof month !== 'string' || !/^\d{4}-(0[1-9]|1[0-2])$/.test(month))) throw new Error('Invalid snapshot metadata month');
  const months = [...monthSet].sort();
  const wb = Workbook.create();
  const grid = wb.worksheets.add('SOP计划');
  const detail = wb.worksheets.add('计划明细');
  const info = wb.worksheets.add('备份信息');
  const exactText = [[],[],[]];
  function write(tab, sheetIndex, row, col, matrix) {
    const rows = matrix.length, columns = matrix[0]?.length || 0;
    if (!rows || !columns) return;
    const normalized = matrix.map(record => record.map(scalar));
    normalized.forEach((record, r) => record.forEach((value, c) => {
      if (typeof value === 'string') {
        if (value.length > 32767) throw new Error('Snapshot text exceeds Excel cell limit');
        exactText[sheetIndex].push([`${column(col+c)}${row+r}`,value]);
      }
    }));
    tab.getRange(`${column(col)}${row}:${column(col+columns-1)}${row+rows-1}`).values = normalized.map(record => record.map(textValue));
  }
  function base(tab, range) {
    tab.showGridLines = false;
    tab.getRange(range).format = { font:{name:'Microsoft YaHei',size:11,color:'#253A54'}, verticalAlignment:'center', rowHeight:29 };
  }
  function heading(tab, lastColumn, row) {
    tab.getRange(`A${row}:${lastColumn}${row}`).format = {
      fill:'#173B64', font:{name:'Microsoft YaHei',size:11,bold:true,color:'#FFFFFF'},
      horizontalAlignment:'center',verticalAlignment:'center',rowHeight:32,
      borders:{insideVertical:{style:'thin',color:'#FFFFFF'}},
    };
  }
  const gridLastColumn = column(Math.max(4,4+months.length));
  const gridLastRow = Math.max(10,9+products.length);
  base(grid, `A1:${gridLastColumn}${gridLastRow}`);
  write(grid,0,2,1,[['鸿蒙智行 SOP计划快照']]);
  write(grid,0,3,1,[['固定值备份，不参与维护表同步。空白表示待维护，0表示明确不安排。']]);
  write(grid,0,4,1,[['业务日期',snapshot.business_date ?? null],['快照截点',snapshot.cutoff_at ?? meta.snapshot_cutoff_at ?? null],['快照生成',snapshot.created_at ?? null],['Excel生成',exportedAt]]);
  write(grid,0,9,1,[['品牌','车型','版本','产品ID',...months]]);
  if (products.length) write(grid,0,10,1,products.map(p => [p.brand ?? '',p.model ?? '',p.version ?? '',p.id,...months.map(month => lookup.get(key(p.id,month))?.sop ?? null)]));
  else write(grid,0,10,1,[['本快照未记录产品与SOP计划。']]);
  grid.getRange('A2').format.font = {name:'Microsoft YaHei',size:16,bold:true,color:'#183C65'};
  grid.getRange('A2:D2').format.rowHeight = 34;
  grid.getRange('A3:D3').format.font = {name:'Microsoft YaHei',size:11,color:'#60758E'};
  grid.getRange('A4:A7').format.font = {name:'Microsoft YaHei',size:11,color:'#64778D'};
  grid.getRange(`A1:A${gridLastRow}`).format.columnWidth = 13;
  grid.getRange(`B1:B${gridLastRow}`).format.columnWidth = 24;
  grid.getRange(`C1:C${gridLastRow}`).format.columnWidth = 40;
  grid.getRange(`D1:D${gridLastRow}`).format.columnWidth = 28;
  if (months.length) grid.getRange(`E1:${gridLastColumn}${gridLastRow}`).format.columnWidth = 14;
  heading(grid,gridLastColumn,9);
  if (products.length) {
    grid.getRange(`A10:D${gridLastRow}`).format.fill = '#F2F5F9';
    grid.getRange(`A10:D${gridLastRow}`).format.wrapText = true;
    grid.getRange(`D10:D${gridLastRow}`).format.font = {name:'Microsoft YaHei',size:10,color:'#61738A'};
    grid.getRange(`A10:${gridLastColumn}${gridLastRow}`).format.rowHeight = 34;
    if (months.length) grid.getRange(`E10:${gridLastColumn}${gridLastRow}`).format = {fill:'#FAFCFF',numberFormat:'#,##0',horizontalAlignment:'right',font:{name:'Microsoft YaHei',size:11,color:'#253A54'}};
    for (let index=0;index<products.length;index++) {
      const p=products[index],previous=products[index-1];
      if (!previous || p.brand!==previous.brand || p.model!==previous.model) grid.getRange(`A${index+10}:${gridLastColumn}${index+10}`).format.borders = {top:{style:'thin',color:'#CCD8E7'}};
      if (String(p.version ?? '').length>24) grid.getRange(`A${index+10}:${gridLastColumn}${index+10}`).format.rowHeight = 44;
    }
  }
  grid.freezePanes.freezeRows(9); grid.freezePanes.freezeColumns(4); grid.tabColor='#173B64';

  const planFields = ['product_id','month','forecast','sop','owner','reason','notes','revision','updated_at'];
  const extraFields = [...new Set(plans.flatMap(p=>Object.keys(p)))].filter(field=>!planFields.includes(field)).sort();
  const headers = ['product_id','品牌','车型','版本',...planFields.slice(1),...extraFields];
  const detailLastColumn = column(headers.length), detailLastRow=Math.max(6,5+plans.length);
  base(detail,`A1:${detailLastColumn}${detailLastRow}`);
  write(detail,1,2,1,[['计划明细（冻结记录）']]);
  write(detail,1,3,1,[['保留原计划字段与产品ID。forecast为原快照留存字段，不是本备份的输入项。']]);
  write(detail,1,5,1,[headers]);
  if (plans.length) write(detail,1,6,1,plans.map(plan=>{
    const p=directory.get(plan.product_id);
    return [plan.product_id,p?.brand ?? '',p?.model ?? '',p?.version ?? '',...planFields.slice(1).map(field=>plan[field] ?? null),...extraFields.map(field=>plan[field] ?? null)];
  }));
  else write(detail,1,6,1,[['本快照没有已保存的计划记录。']]);
  detail.getRange('A2').format.font = {name:'Microsoft YaHei',size:15,bold:true,color:'#183C65'};
  detail.getRange('A3').format.font = {name:'Microsoft YaHei',size:11,color:'#60758E'};
  const widths=[27,10,20,38,13,15,15,15,20,48,11,42];
  headers.forEach((name,index)=>detail.getRange(`${column(index+1)}1:${column(index+1)}${detailLastRow}`).format.columnWidth=widths[index] ?? 30);
  heading(detail,detailLastColumn,5);
  if (plans.length) {
    detail.getRange(`A6:${detailLastColumn}${detailLastRow}`).format.wrapText=true;
    detail.getRange(`A6:${detailLastColumn}${detailLastRow}`).format.rowHeight=42;
    detail.getRange(`F6:G${detailLastRow}`).setNumberFormat('#,##0');
    detail.getRange(`F6:G${detailLastRow}`).format.horizontalAlignment='right';
    detail.getRange(`K6:K${detailLastRow}`).setNumberFormat('0');
    detail.getRange(`L6:L${detailLastRow}`).format.wrapText=false;
    for (let r=6;r<=detailLastRow;r++) if(r%2===0) detail.getRange(`A${r}:${detailLastColumn}${r}`).format.fill='#F5F8FC';
  }
  detail.freezePanes.freezeRows(5); detail.freezePanes.freezeColumns(5); detail.tabColor='#7890AE';

  const sources = Array.isArray(frozen.sources) && frozen.sources.length ? frozen.sources : Array.isArray(meta.source_files) ? meta.source_files : [];
  const sourceLabel = source => typeof source==='string' ? source : [source.name || source.file || source.path || '',source.sheet || ''].filter(Boolean).join(' / ');
  const infoRows = [
    ['快照ID',snapshot.id ?? null],['快照类型',titleForKind(snapshot.kind)],['快照名称',snapshot.label ?? null],
    ['业务日期',snapshot.business_date ?? null],['规定截点',snapshot.cutoff_at ?? meta.snapshot_cutoff_at ?? null],
    ['快照实际生成时间',snapshot.created_at ?? null],['Excel实际生成时间',exportedAt],['时间口径','保留快照原始时间和时区偏移。业务截点按北京时间。'],
    ['备份性质','冻结SOP计划备份，不参与维护表同步。修改本文件不会更新看板。'],
    ['数量单位','辆'],['空白与0','空白表示未维护或快照未记录；0为明确保存的零计划。'],
    ['计划月份',months.length?months.join('、'):'快照未记录计划月份'],['快照产品数',sourceProducts.length],['目录缺失产品数',orphanCount],['原始计划记录数',plans.length],
    ['已填写SOP的记录数',plans.filter(p=>p.sop!=null).length],['来源数据截至',meta.data_through_label ?? meta.data_through ?? null],
    ['取数范围','仅使用这一快照的产品目录、计划记录和已开放月份，不取当前计划。'],
  ];
  sources.forEach((source,index)=>infoRows.push([`来源${index+1}`,sourceLabel(source)]));
  base(info,`A1:B${5+infoRows.length}`);
  write(info,2,2,1,[['备份信息']]);
  write(info,2,3,1,[['计划主表用于回看，计划明细保留原始字段用于核对。']]);
  write(info,2,5,1,[['项目','冻结值或说明']]);
  write(info,2,6,1,infoRows);
  info.getRange('A2').format.font={name:'Microsoft YaHei',size:15,bold:true,color:'#183C65'};
  info.getRange('A3').format.font={name:'Microsoft YaHei',size:11,color:'#60758E'};
  info.getRange(`A1:A${5+infoRows.length}`).format.columnWidth=27;
  info.getRange(`B1:B${5+infoRows.length}`).format.columnWidth=103;
  heading(info,'B',5);
  info.getRange(`A6:A${5+infoRows.length}`).format.fill='#F2F5F9';
  info.getRange(`B6:B${5+infoRows.length}`).format.wrapText=true;
  infoRows.forEach((record,index)=>{if(String(record[1]??'').length>65)info.getRange(`A${index+6}:B${index+6}`).format.rowHeight=44;});
  info.freezePanes.freezeRows(5);info.tabColor='#9AAABD';
  wb.recalculate();
  if(products.length&&months.length){
    const values=grid.getRange(`E10:${gridLastColumn}${gridLastRow}`).values;
    products.forEach((p,r)=>months.forEach((month,c)=>{
      const expected=lookup.get(key(p.id,month))?.sop??null,actual=values[r][c];
      if(expected==null?actual!=null&&actual!=='':actual!==expected)throw new Error('Snapshot grid value mismatch');
    }));
  }
  await fs.mkdir(path.dirname(outputPath),{recursive:true});
  const xlsx=await SpreadsheetFile.exportXlsx(wb);await xlsx.save(outputPath);
  await fs.rm(outputPath+'.inspect.ndjson',{force:true});
  // Artifact-tool may coerce ISO strings to Excel date serials. Preserve exact
  // frozen text (including UTC offsets and literal formula-like notes) natively.
  const patch = spawnSync(PYTHON,['-X','utf8','-c',String.raw`
import json,sys,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
p=Path(sys.argv[1]); text=json.load(sys.stdin); ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('',ns);ET.register_namespace('r','http://schemas.openxmlformats.org/officeDocument/2006/relationships')
with zipfile.ZipFile(p) as z: entries={i.filename:(i,z.read(i.filename)) for i in z.infolist()}
for index,values in enumerate(text):
    filename=f'xl/worksheets/sheet{index+1}.xml';root=ET.fromstring(entries[filename][1]);data=root.find('{'+ns+'}sheetData')
    rows={int(r.attrib['r']):r for r in data};cells={c.attrib['r']:c for r in data for c in r}
    for address,value in values:
        cell=cells.get(address)
        if cell is None:
            rnum=int(''.join(c for c in address if c.isdigit()));row=rows.get(rnum)
            if row is None: row=ET.SubElement(data,'{'+ns+'}row',{'r':str(rnum)});rows[rnum]=row
            cell=ET.SubElement(row,'{'+ns+'}c',{'r':address})
        cell.set('t','inlineStr')
        for child in list(cell):cell.remove(child)
        inline=ET.SubElement(cell,'{'+ns+'}is');t=ET.SubElement(inline,'{'+ns+'}t');t.text=value
        if value.strip()!=value:t.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
    views=root.find('{'+ns+'}sheetViews');view=views.find('{'+ns+'}sheetView') if views is not None else None
    if view is not None:
        pane=view.find('{'+ns+'}pane')
        if pane is None:pane=ET.SubElement(view,'{'+ns+'}pane')
        pane.attrib.update([{'xSplit':'4','ySplit':'9','topLeftCell':'E10','activePane':'bottomRight','state':'frozen'},{'xSplit':'5','ySplit':'5','topLeftCell':'F6','activePane':'bottomRight','state':'frozen'},{'ySplit':'5','topLeftCell':'A6','activePane':'bottomLeft','state':'frozen'}][index])
    entries[filename]=(entries[filename][0],ET.tostring(root,encoding='utf-8',xml_declaration=True))
temporary=p.with_name(p.name+'.text.tmp')
with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as z:
    for name,(entry,content) in entries.items():z.writestr(entry,content)
temporary.replace(p)
`,outputPath],{input:JSON.stringify(exactText),encoding:'utf8',windowsHide:true});
  if(patch.status!==0)throw new Error(patch.stderr||'Snapshot exact-text finalization failed');
  if(input.preview_path){
    const verified=await SpreadsheetFile.importXlsx(await FileBlob.load(outputPath));
    const previewPath=path.resolve(input.preview_path);await fs.mkdir(path.dirname(previewPath),{recursive:true});
    const views=[['SOP计划',`A1:${column(Math.min(9,4+months.length))}${Math.min(gridLastRow,16)}`,''],['计划明细',`A1:${detailLastColumn}${Math.min(detailLastRow,11)}`,'-detail'],['备份信息',`A1:B${Math.min(5+infoRows.length,25)}`,'-info']];
    for(const [sheetName,range,suffix]of views){const preview=await verified.render({sheetName,range,scale:1.25,format:'png'});const filename=suffix?previewPath.replace(/\.png$/i,suffix+'.png'):previewPath;await fs.writeFile(filename,new Uint8Array(await preview.arrayBuffer()));}
    const inspection=await verified.inspect({kind:'table',range:`'SOP计划'!A9:${column(Math.min(9,4+months.length))}${Math.min(gridLastRow,14)}`,include:'values,formulas',tableMaxRows:6,tableMaxCols:9,maxChars:2000});log(inspection.ndjson);
  }
  process.stdout.write(JSON.stringify({ok:true,output_path:outputPath,snapshot_id:snapshot.id??null,products:products.length,months:months.length,plan_rows:plans.length,orphan_products:orphanCount})+'\n');
}
main().catch(error=>{log(error?.stack??error);process.exitCode=1;});
