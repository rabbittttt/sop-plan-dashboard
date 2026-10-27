import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Workbook, SpreadsheetFile, FileBlob } from '@oai/artifact-tool';
import { column, planTitle, planPeriod, setupPlanSheet, stylePlanRow, styleManualSection } from './plan_sheet_style.mjs';
import { PYTHON } from './runtime.mjs';

// One JSON payload on stdin; only the result is emitted on stdout.
const log = (...args) => process.stderr.write(args.map(String).join(' ') + '\n');
console.log = log;
const key = (id, month) => JSON.stringify([id, month]);
function quantity(value) {
  if (value == null) return null;
  if (!Number.isSafeInteger(value) || value < 0 || value > 1e12) throw new Error('SOP must be a nonnegative integer or null');
  return value;
}

async function main() {
  let input = '';
  // Decode UTF-8 across pipe chunk boundaries: a large Chinese JSON payload
  // can otherwise split a sheet name and silently lose a coordinate mapping.
  process.stdin.setEncoding('utf8');
  for await (const chunk of process.stdin) input += chunk;
  const data = JSON.parse(input.replace(/^\uFEFF/, ''));
  if (!((data.schema_version === 3 && data.layout_version === 'continuous-blue-v1') || (data.schema_version === 4 && data.layout_version === 'continuous-blue-v2') || (data.schema_version === 5 && ['continuous-inline-v1','continuous-inline-v2'].includes(data.layout_version))) || !data.layout?.sheets?.length || !data.output_path || !data.token) throw new Error('A trusted continuous layout and export token are required');
  const inline = data.schema_version === 5;
  const output = path.resolve(data.output_path);
  const plans = new Map(data.plans.map(p => [key(p.product_id, p.month), quantity(p.sop)]));
  const wb = Workbook.create();
  const names = [], exactText = [], unlocked = {}, sheetControls = {};
  // Retain string identity in the final package: automatic date inference must
  // never change a month key, an identifier or a user-visible product label.
  function addSheet(name) {
    names.push(name); exactText.push([]);
    return wb.worksheets.add(name);
  }
  function write(sheet, row, col, matrix) {
    const index = names.indexOf(sheet.name);
    const safe = matrix.map((values, ri) => values.map((value, ci) => {
      if (typeof value !== 'string') return value;
      exactText[index].push([`${column(col + ci)}${row + ri}`, value]);
      return /^[=+@-]/.test(value) ? "'" + value : value;
    }));
    sheet.getRange(`${column(col)}${row}:${column(col + matrix[0].length - 1)}${row + matrix.length - 1}`).values = safe;
  }
  for (const spec of data.layout.sheets) {
    const sheet = addSheet(spec.name);
    const last = column(spec.last_column), visibleLast = column(spec.visible_last_column);
    const mappings = data.layout.cells.filter(c => c.sheet === spec.name);
    const mappedCells = new Map(mappings.map(c=>[c.cell,c]));
    const displayValue = mapping => {
      const value = plans.get(key(mapping.product_id,mapping.month)) ?? null;
      return !inline && value == null && !mapping.applicable ? '未适用' : value;
    };
    const editable = new Set(mappings.filter(c => c.editable).map(c => c.cell));
    unlocked[spec.name] = data.layout.cells.filter(c => c.sheet === spec.name && c.editable).map(c => c.cell);
    sheetControls[spec.name] = {hidden_start:spec.product_column,freeze_rows:spec.first_data_row-1,top_left:`D${spec.first_data_row}`,structural:inline,hidden_rows:spec.rows.filter(r=>r.hidden).map(r=>r.row)};
    setupPlanSheet(sheet, {lastColumn:spec.last_column,visibleLastColumn:spec.visible_last_column,lastRow:spec.last_row,columns:spec.columns,headerRow:spec.header_row,firstDataRow:spec.first_data_row});
    sheet.getRange(`${column(spec.product_column)}:${last}`).format.columnWidth = 24;
    write(sheet, 1, 1, [[planTitle()]]);
    write(sheet, 1, 4, [[planPeriod(data.months)]]);
    write(sheet, 2, 1, [[inline?'单位：辆。直接插入、删除明细行或修改名称；删除表示停用，历史保留。保存后重算合计。':'单位：辆。空白表示未填写，0表示明确不安排。历史月灰底，可修正；深灰格只读。“未适用”无需填0。新增车型/版本在表底填写，保存后自动并入目录。']]);
    write(sheet, spec.header_row, 1, spec.headers);
    if (spec.manual_section) {
      const section = spec.manual_section;
      write(sheet, section.title_row, 1, [[section.title]]);
      write(sheet, section.header_row, 1, spec.headers);
      styleManualSection(sheet, {section,visibleLastColumn:spec.visible_last_column,lastColumn:spec.last_column,columns:spec.columns});
    }
    for (const item of spec.rows) {
      const r = item.row, values = Array(spec.last_column).fill(null);
      values[0] = item.label_a || null; values[1] = item.label_b || null; values[2] = item.label_c || null;
      values[spec.kind_column-1] = item.kind;
      if (inline) values[spec.key_column-1] = item.key;
      if (item.kind === 'product') {
        if (inline) unlocked[spec.name].push(`A${r}`,`B${r}`,`C${r}`);
        values[spec.product_column-1] = item.product_id;
        for (const col of spec.columns.filter(c=>c.kind==='month')) {
          const address = `${column(col.column)}${r}`;
          const mapping = mappedCells.get(address);
          if (!mapping) throw new Error(`Missing trusted input map: ${spec.name}!${address}; maps=${mappings.length}`);
          values[col.column-1] = displayValue(mapping);
        }
      }
      write(sheet, r, 1, [values]);
      const monthStates = {};
      for (const col of spec.columns.filter(c=>c.kind==='month')) {
        const address = `${column(col.column)}${r}`;
        if (item.kind === 'product') monthStates[address] = {editable:editable.has(address),applicable:mappedCells.get(address).applicable};
        if (item.kind === 'manual_entry') monthStates[address] = {editable:data.editable_months.includes(col.month)};
      }
      stylePlanRow(sheet, {row:r,kind:item.kind,visibleLastColumn:spec.visible_last_column,columns:spec.columns,version:item.version,monthStates});
      if (item.kind === 'product') {
        for (const col of spec.columns.filter(c=>c.kind==='month')) {
          const address = `${column(col.column)}${r}`;
          const active = editable.has(address);
          if (active) sheet.getRange(address).dataValidation = { rule:{type:'whole',operator:'between',formula1:0,formula2:1000000000000} };
        }
      } else if (item.kind === 'manual_entry') {
        unlocked[spec.name].push(`A${r}`,`B${r}`,`C${r}`);
        for (const col of spec.columns.filter(c=>c.kind==='month')) {
          const address = `${column(col.column)}${r}`;
          const active = data.editable_months.includes(col.month);
          if(active) {
            unlocked[spec.name].push(address);
            sheet.getRange(address).dataValidation = {rule:{type:'whole',operator:'between',formula1:0,formula2:1000000000000}};
          }
        }
      }
    }
    for (const merge of spec.merges) sheet.getRange(merge).merge();
    for (const [address, formula] of Object.entries(spec.formulas)) sheet.getRange(address).formulas = [[formula]];
    sheet.freezePanes.freezeRows(spec.first_data_row-1);
    sheet.freezePanes.freezeColumns(3);
    const grid = sheet.getRange(`A1:${last}${spec.last_row}`).values;
    for (const mapping of data.layout.cells.filter(c => c.sheet === spec.name)) {
      const [, letters, row] = /^([A-Z]+)(\d+)$/.exec(mapping.cell);
      const c = [...letters].reduce((total, ch) => total*26+ch.charCodeAt(0)-64, 0);
      const actual = grid[Number(row)-1][c-1], expected = displayValue(mapping);
      if (expected == null ? actual != null && actual !== '' : actual !== expected) throw new Error(`Input changed during export: ${spec.name}!${mapping.cell}`);
    }
  }
  const sync = addSheet('_sync');
  const baseline = [['product_id','month','baseline_sop','revision'], ...data.baseline.map(b => [b.product_id,b.month,quantity(b.baseline_sop),b.revision])];
  sync.getRange(`A1:B${baseline.length}`).setNumberFormat('@');
  write(sync,1,1,baseline);
  const meta = addSheet('_meta');
  write(meta,1,1,[['key','value'],['token',data.token],['generated_at',data.generated_at],['schema_version',data.schema_version],['layout_version',data.layout_version]]);
  const layout = addSheet('_layout');
  const mappings = [['product_id','month','sheet','cell','editable','applicable'], ...data.layout.cells.map(c => [c.product_id,c.month,c.sheet,c.cell,Number(c.editable),Number(c.applicable)])];
  layout.getRange(`A1:D${mappings.length}`).setNumberFormat('@');
  write(layout,1,1,mappings);
  wb.recalculate();
  if (data.preview_path) {
    log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:30},summary:'Formula verification'})).ndjson);
  }
  await fs.mkdir(path.dirname(output), {recursive:true});
  const xlsx = await SpreadsheetFile.exportXlsx(wb);
  await xlsx.save(output);
  await fs.rm(output+'.inspect.ndjson', {force:true});
  // Artifact-tool authors all cells, formulas and formatting. Its current API
  // omits Excel sheet visibility/protection, so finalize only those presentation
  // controls and literal text identity in OOXML, retaining formula caches.
  const finalized = spawnSync(PYTHON, ['-X', 'utf8', '-c', String.raw`
import copy,json,sys,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
p=Path(sys.argv[1]); config=json.loads(sys.stdin.read())
N='http://schemas.openxmlformats.org/spreadsheetml/2006/main'; ns={'m':N}; tag=lambda x:'{'+N+'}'+x
ET.register_namespace('',N); ET.register_namespace('r','http://schemas.openxmlformats.org/officeDocument/2006/relationships')
with zipfile.ZipFile(p) as z: entries={i.filename:(i,z.read(i.filename)) for i in z.infolist()}
def save(name,root): entries[name]=(entries[name][0],ET.tostring(root,encoding='utf-8',xml_declaration=True))
book=ET.fromstring(entries['xl/workbook.xml'][1])
for s in book.find('m:sheets',ns):
    if s.attrib['name'].startswith('_'): s.set('state','hidden')
save('xl/workbook.xml',book)
styles=ET.fromstring(entries['xl/styles.xml'][1]); xfs=styles.find('m:cellXfs',ns); unlocked_styles={}
for index,name in enumerate(config['names'],1):
    filename=f'xl/worksheets/sheet{index}.xml'; root=ET.fromstring(entries[filename][1])
    cells={c.attrib['r']:c for c in root.findall('m:sheetData/m:row/m:c',ns)}
    for address,text in config['exactText'][index-1]:
        cell=cells.get(address)
        if cell is None: raise ValueError('Missing exported string cell '+name+'!'+address)
        cell.set('t','inlineStr')
        for child in list(cell): cell.remove(child)
        inline=ET.SubElement(cell,tag('is')); ET.SubElement(inline,tag('t')).text=text
    if name in config['unlocked']:
        controls=config['sheetControls'][name]
        cols=root.find('m:cols',ns)
        for col in cols if cols is not None else []:
            if int(col.attrib['min'])>=controls['hidden_start']:
                col.attrib.update({'hidden':'1','width':'0','customWidth':'1'})
        view=root.find('m:sheetViews/m:sheetView',ns)
        if view is not None:
            pane=view.find('m:pane',ns)
            if pane is None: pane=ET.SubElement(view,tag('pane'))
            pane.attrib.update({'xSplit':'3','ySplit':str(controls['freeze_rows']),'topLeftCell':controls['top_left'],'activePane':'bottomRight','state':'frozen'})
        for address in config['unlocked'][name]:
            cell=cells.get(address)
            if cell is None: raise ValueError('Missing styled input cell '+name+'!'+address)
            original=int(cell.attrib.get('s','0'))
            if original not in unlocked_styles:
                style=copy.deepcopy(xfs[original])
                for old in list(style.findall('m:protection',ns)): style.remove(old)
                style.set('applyProtection','1'); ET.SubElement(style,tag('protection'),{'locked':'0','hidden':'0'})
                unlocked_styles[original]=len(xfs); xfs.append(style)
            cell.set('s',str(unlocked_styles[original]))
        if not controls.get('structural'):
            protection=ET.Element(tag('sheetProtection'),{'sheet':'1','objects':'1','scenarios':'1','selectLockedCells':'0','selectUnlockedCells':'0'})
            children=list(root); data=root.find('m:sheetData',ns); root.insert(children.index(data)+1,protection)
        hidden_rows=set(controls.get('hidden_rows',[]))
        for row in root.findall('m:sheetData/m:row',ns):
            if int(row.attrib['r']) in hidden_rows: row.set('hidden','1')
        for validation in root.findall('m:dataValidations/m:dataValidation',ns):
            validation.attrib.update({'allowBlank':'1','showInputMessage':'1','showErrorMessage':'1','errorStyle':'stop','promptTitle':'SOP计划量（辆）','prompt':'只填非负整数。空白表示未填写，0表示明确不安排。','errorTitle':'请输入非负整数','error':'SOP计划量须为非负整数；未填写时请留空。'})
    save(filename,root)
xfs.set('count',str(len(xfs))); save('xl/styles.xml',styles)
temporary=p.with_name(p.name+'.presentation.tmp')
with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as out:
    for name,(info,content) in entries.items(): out.writestr(info,content)
temporary.replace(p)
`, output], {input:JSON.stringify({names,exactText,unlocked,sheetControls}),encoding:'utf8',windowsHide:true});
  if (finalized.status !== 0) throw new Error(finalized.stderr || 'Workbook protection finalization failed');
  if (data.preview_path) {
    const preview = await SpreadsheetFile.importXlsx(await FileBlob.load(output));
    log((await preview.inspect({kind:'region',sheetId:data.layout.sheets[0].name,range:'A3:P8',tableMaxRows:6,tableMaxCols:16,maxChars:2000})).ndjson);
    await fs.mkdir(path.dirname(path.resolve(data.preview_path)), {recursive:true});
    for (const [i,spec] of data.layout.sheets.entries()) {
      const target = i ? data.preview_path.replace(/\.png$/i,`-${i}.png`) : data.preview_path;
      const png = await preview.render({sheetName:spec.name,range:`A1:${column(spec.visible_last_column)}${spec.last_row}`,scale:1.2,format:'png'});
      await fs.writeFile(target,new Uint8Array(await png.arrayBuffer()));
    }
  }
  process.stdout.write(JSON.stringify({ok:true,output_path:output,schema_version:data.schema_version,sheets:data.layout.sheets.map(s=>s.name),input_cells:Object.values(unlocked).reduce((n,a)=>n+a.length,0)})+'\n');
}
main().catch(error => {log(error?.stack ?? error); process.exitCode=1;});
