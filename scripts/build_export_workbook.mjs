import fs from 'node:fs/promises';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { Workbook, SpreadsheetFile, FileBlob } from '@oai/artifact-tool';
import { PYTHON } from './runtime.mjs';
import { compareBrands } from './brand_order.mjs';

const log = (...args) => process.stderr.write(args.map(String).join(' ') + '\n');
console.log = log;
import { column, planTitle, planPeriod, planGuidance, setupPlanSheet, stylePlanRow, styleManualSection } from './plan_sheet_style.mjs';
const quantity = value => value === null || (Number.isSafeInteger(value) && value >= 0);
const same = (a,b) => JSON.stringify(a) === JSON.stringify(b);

function validate(data) {
  if (data.schema_version!==1 || !data.output_path || !Array.isArray(data.months) || !data.months.length || typeof data.include_locks!=='boolean' || typeof data.include_versions!=='boolean') throw new Error('Invalid export contract');
  if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(data.current_month) || data.months.some(m=>typeof m!=='string'||!/^\d{4}-(0[1-9]|1[0-2])$/.test(m)) || !same(data.months,[...new Set(data.months)].sort())) throw new Error('Export months must be sorted unique YYYY-MM values');
  const headers=['层级','品牌','车型',...(data.include_versions?['版本']:[]),'指标',...data.months,'数据说明'];
  if (!same(data.headers,headers) || !Array.isArray(data.rows) || !data.rows.length || !Array.isArray(data.annual_plans) || data.annual_plans.length!==data.rows.length) throw new Error('Export rows and aligned annual plans are required');
  const metricIndex=data.include_versions?4:3, monthsIndex=metricIndex+1;
  const annualYears=data.months.filter(m=>m.endsWith('-12')).map(m=>m.slice(0,4));
  const roundtrip=data.roundtrip;
  if(roundtrip) {
    if(typeof roundtrip.token!=='string'||!roundtrip.token||roundtrip.token.length>200||!Array.isArray(roundtrip.row_product_ids)||roundtrip.row_product_ids.length!==data.rows.length)throw new Error('Invalid roundtrip identity contract');
    if(!Array.isArray(roundtrip.annual_known_counts)||roundtrip.annual_known_counts.length!==data.rows.length||!roundtrip.editable_months_by_product||typeof roundtrip.editable_months_by_product!=='object'||!Array.isArray(roundtrip.manual_editable_months))throw new Error('Invalid roundtrip editing contract');
    for(const months of [...Object.values(roundtrip.editable_months_by_product),roundtrip.manual_editable_months])if(!Array.isArray(months)||months.some(m=>!data.months.includes(m))||new Set(months).size!==months.length)throw new Error('Invalid editable months');
    if(roundtrip.inline_schema===2&&(!Array.isArray(roundtrip.products)||roundtrip.products.some(p=>!p||typeof p.id!=='string')))throw new Error('Inline rows require their trusted catalog');
  }
  const seenProducts=new Set();
  const rows=data.rows.map((row,index)=>{
    if (!Array.isArray(row)||row.length!==headers.length||!['车型汇总','版本明细'].includes(row[0])||(!data.include_versions&&row[0]==='版本明细')) throw new Error(`Invalid export row ${index+1}`);
    if (![row[1],row[2],...(data.include_versions?[row[3]]:[]),row.at(-1)].every(v=>typeof v==='string')) throw new Error(`Invalid label at row ${index+1}`);
    if (!['SOP计划量',...(data.include_locks?['锁单数']:[])].includes(row[metricIndex])) throw new Error(`Invalid metric at row ${index+1}`);
    const values=row.slice(monthsIndex,-1), annual=data.annual_plans[index];
    if (!values.every(quantity)||!annual||typeof annual!=='object'||Array.isArray(annual)||!same(Object.keys(annual).sort(),[...annualYears].sort())||!Object.values(annual).every(quantity)) throw new Error(`Invalid quantities at row ${index+1}`);
    if (row[metricIndex]==='锁单数'&&Object.values(annual).some(v=>v!==null)) throw new Error('Annual SOP columns cannot contain lock counts');
    const productId=roundtrip?.row_product_ids[index]??null,annualKnown=roundtrip?.annual_known_counts[index];
    if(roundtrip) {
      if(row[0]==='版本明细'?(typeof productId!=='string'||!productId||productId.length>200):productId!==null)throw new Error('Roundtrip product identity must match the row level');
      if(!annualKnown||typeof annualKnown!=='object'||Array.isArray(annualKnown)||!same(Object.keys(annualKnown).sort(),[...annualYears].sort())||Object.values(annualKnown).some(n=>!Number.isSafeInteger(n)||n<0))throw new Error('Invalid annual known counts');
      if(row[0]==='版本明细'&&row[metricIndex]==='SOP计划量') {
        if(seenProducts.has(productId))throw new Error('Duplicate SOP product identity');
        seenProducts.add(productId);
        if(!Array.isArray(roundtrip.editable_months_by_product[productId]))throw new Error('Missing product editable months');
      }
    }
    return {sourceIndex:index,productId,level:row[0],brand:row[1],model:row[2],version:data.include_versions?row[3]:'',metric:row[metricIndex],values,annual,annualKnown,notes:row.at(-1)};
  });
  return {rows,annualYears};
}

function makeLayout(data, parsed) {
  const inline=data.roundtrip?.inline_schema===2;
  const identity=['品牌','车型',...(data.include_versions?['版本']:[])], firstValue=identity.length+1;
  const columns=[],years=[];
  for(const year of [...new Set(data.months.map(m=>m.slice(0,4)))]) {
    const first=firstValue+columns.length, months=data.months.filter(m=>m.startsWith(year+'-'));
    for(const month of months) {
      columns.push({column:firstValue+columns.length,kind:'month',month,sourceIndex:data.months.indexOf(month),label:Number(month.slice(-2))+'月',historical:month<data.current_month});
      if(month.endsWith('-12'))columns.push({column:firstValue+columns.length,kind:'year_total',year,label:'年度合计'});
    }
    years.push({year,first_column:first,last_column:firstValue+columns.length-1,full:months.length===12});
  }
  const width=identity.length+columns.length, headers=[Array(width).fill(null),Array(width).fill(null)];
  identity.forEach((label,i)=>headers[0][i]=label);
  years.forEach(y=>headers[0][y.first_column-1]=y.year+'年');
  columns.forEach(c=>headers[1][c.column-1]=c.label);
  const headerMerges=r=>[...identity.map((_,i)=>column(i+1)+r+':'+column(i+1)+(r+1)),...years.filter(y=>y.last_column>y.first_column).map(y=>column(y.first_column)+r+':'+column(y.last_column)+r)];
  const merges=headerMerges(3), rows=[], fields=data.include_locks?['锁单数','SOP计划量']:['SOP计划量'];
  const brands=new Map(),brandTotals=new Map(fields.map(f=>[f,[]]));
  for(const source of parsed.rows) {
    if(!brands.has(source.brand))brands.set(source.brand,new Map());
    const models=brands.get(source.brand);
    if(!models.has(source.model))models.set(source.model,[]);
    models.get(source.model).push(source);
  }
  let next=5;
  function sourceRow(source,kind) {
    const row={row:next++,kind,metric:source.metric,productId:source.productId,labels:Array(identity.length).fill(null),values:{},formulas:{},version:source.version};
    for(const col of columns) {
      row.values[col.column]=col.kind==='month'?source.values[col.sourceIndex]:source.annual[col.year];
      if(kind==='product'&&source.metric==='SOP计划量'&&col.kind==='year_total'&&(data.roundtrip||years.find(y=>y.year===col.year).full)) {
        const y=years.find(y=>y.year===col.year),ref=column(y.first_column)+row.row+':'+column(col.column-1)+row.row;
        if(y.full)row.formulas[col.column]='=IF(COUNT('+ref+')=0,"",SUM('+ref+'))';
        else {
          const selected=columns.filter(c=>c.kind==='month'&&c.month.startsWith(col.year+'-')).map(c=>source.values[c.sourceIndex]).filter(v=>typeof v==='number');
          const offscreenCount=source.annualKnown[col.year]-selected.length,offscreenSum=(source.annual[col.year]??0)-selected.reduce((a,b)=>a+b,0);
          if(offscreenCount<0||offscreenCount>12-selected.length||!quantity(offscreenSum)||(offscreenCount===0&&offscreenSum!==0)||(source.annual[col.year]===null&&source.annualKnown[col.year]!==0))throw new Error('Annual SOP baseline does not reconcile with selected months');
          row.formulas[col.column]='=IF(COUNT('+ref+')+'+offscreenCount+'=0,"",SUM('+ref+')+'+offscreenSum+')';
        }
      }
    }
    rows.push(row);return row;
  }
  function sumInto(row,children) {
    for(const col of columns) {
      if(col.kind==='year_total'&&row.metric==='锁单数'){row.values[col.column]=null;continue;}
      const known=children.map(c=>c.values[col.column]).filter(v=>typeof v==='number');
      const expected=known.length?known.reduce((a,b)=>a+b,0):null;
      if(!quantity(expected))throw new Error('Combined export quantities exceed the safe integer range');
      if(Object.hasOwn(row.values,col.column)&&row.values[col.column]!==expected)throw new Error('Model SOP does not reconcile with its version rows');
      row.values[col.column]=expected;
      const refs=children.map(c=>column(col.column)+c.row).join(',');
      row.formulas[col.column]=refs?'=IF(COUNT('+refs+')=0,"",SUM('+refs+'))':'=""';
    }
  }
  function totalRow(metric,children,kind,label) {
    const row={row:next++,kind,metric,labels:Array(identity.length).fill(null),values:{},formulas:{}};
    row.labels[kind==='grand_total'?0:1]=label;sumInto(row,children);rows.push(row);return row;
  }
  function mergeTotal(r,grand=false) {
    const from=grand?1:2;
    if(identity.length>from)merges.push(column(from)+r+':'+column(identity.length)+r);
  }
  for(const [brand,models] of [...brands].sort(([left], [right]) => compareBrands(left, right))) {
    const brandFirst=next, modelTotals=new Map(fields.map(f=>[f,[]]));
    for(const [model,group] of models) {
      const summaries=fields.map(f=>group.filter(r=>r.level==='车型汇总'&&r.metric===f));
      if(summaries.some(list=>list.length!==1))throw new Error('Exactly one model summary per metric is required: '+brand+'/'+model);
      const details=fields.map(f=>group.filter(r=>r.level==='版本明细'&&r.metric===f));
      if(data.include_versions&&(!details[0].length||details.some(list=>list.length!==details[0].length)))throw new Error('Version metric rows do not align: '+brand+'/'+model);
      const first=next,detailRows=new Map(fields.map(f=>[f,[]]));
      if(data.include_versions) {
        for(let i=0;i<details[0].length;i++)if(details.some(list=>list[i].version!==details[0][i].version))throw new Error('Version order mismatch: '+brand+'/'+model);
        for(const list of details)for(const source of list) {
          const row=sourceRow(source,'product');
          row.labels[2]=source.version+(source.metric==='锁单数'?'（锁单数）':'');
          row.version=row.labels[2];detailRows.get(source.metric).push(row);
        }
        if(inline)for(const field of fields) {
          const group=detailRows.get(field);
          if(!group.length)continue;
          group[0].labels[0]=brand;
          group[0].labels[1]=model;
          // Keep lock and SOP blocks separate so the SOP anchor remains editable.
          if(group.length>1)for(const letter of ['A','B'])
            merges.push(letter+group[0].row+':'+letter+group.at(-1).row);
        }
      }
      if(next>first&&!inline) {
        rows.find(r=>r.row===first).labels[1]=model;
        if(next-first>1)merges.push('B'+first+':B'+(next-1));
      }
      for(const list of summaries) {
        const row=sourceRow(list[0],'model_total');
        row.labels[1]=model+(row.metric==='锁单数'?' 锁单':'')+' 合计';
        if(data.include_versions&&row.metric==='SOP计划量')sumInto(row,detailRows.get(row.metric));
        mergeTotal(row.row);modelTotals.get(row.metric).push(row);
      }
    }
    for(const field of fields) {
      const row=totalRow(field,modelTotals.get(field),'brand_total',brand+(field==='锁单数'?' 锁单':'')+' 合计');
      mergeTotal(row.row);brandTotals.get(field).push(row);
    }
    if(!inline||!data.include_versions) {
      rows.find(r=>r.row===brandFirst).labels[0]=brand;
      if(next-brandFirst>1)merges.push('A'+brandFirst+':A'+(next-1));
    }
    next++;
  }
  for(const field of fields) {
    const row=totalRow(field,brandTotals.get(field),'grand_total',field==='锁单数'?'锁单总计':'总计');
    mergeTotal(row.row,true);
  }
  if(inline) {
    const productColumn=width+1,kindColumn=width+2,keyColumn=width+3;
    headers[0].push(null,null,null);headers[1].push('product_id','row_kind','row_key');
    const products=new Map(data.roundtrip.products.map(p=>[p.id,p]));
    for(const row of rows) {
      row.key=row.kind==='product'?(row.metric==='锁单数'?'lock:':'plan:')+row.productId:'row:'+row.row;
      row.hidden=row.kind==='product'&&products.get(row.productId)?.active===false;
    }
    return {identity,firstValue,columns,headers,rows,merges,section:null,lastRow:next-1,width,lastColumn:keyColumn,productColumn,kindColumn,keyColumn,inline};
  }
  const titleRow=next+1,section={title_row:titleRow,header_row:titleRow+1,first_row:titleRow+3,last_row:titleRow+12,title:data.roundtrip&&data.include_versions?'新增车型/版本：填写品牌、车型、版本和月度计划量，保存后在看板导入。':'新增车型/版本（导出副本，请在统一维护表中填写）'};
  merges.push('A'+titleRow+':'+column(width)+titleRow,...headerMerges(section.header_row));
  for(let r=section.first_row;r<=section.last_row;r++) {
    const row={row:r,kind:'manual_entry',labels:Array(identity.length).fill(null),values:{},formulas:{}};
    for(const col of columns) {
      row.values[col.column]=null;
      if(col.kind==='year_total') {
        const y=years.find(y=>y.year===col.year),ref=column(y.first_column)+r+':'+column(col.column-1)+r;
        row.formulas[col.column]='=IF(COUNT('+ref+')=0,"",SUM('+ref+'))';
      }
    }
    rows.push(row);
  }
  return {identity,firstValue,columns,headers,rows,merges,section,lastRow:section.last_row,width,lastColumn:width,inline:false};
}

async function main() {
  let input='';process.stdin.setEncoding('utf8');
  for await(const chunk of process.stdin)input+=chunk;
  const data=JSON.parse(input.replace(/^\uFEFF/,'')),layout=makeLayout(data,validate(data));
  const output=path.resolve(data.output_path),wb=Workbook.create(),sheet=wb.worksheets.add('SOP计划');
  const last=column(layout.width),firstValue=column(layout.firstValue),exactText={'SOP计划':[]},unlocked=[],mapping=[];
  function writeTo(target,row,col,values) {
    const matrix=values.map((line,ri)=>line.map((value,ci)=>{
      if(typeof value!=='string')return value;
      exactText[target.name].push([column(col+ci)+(row+ri),value]);
      return /^[=+@-]/.test(value)?"'"+value:value;
    }));
    target.getRange(column(col)+row+':'+column(col+matrix[0].length-1)+(row+matrix.length-1)).values=matrix;
  }
  const write=(row,col,values)=>writeTo(sheet,row,col,values);
  const subtitle=layout.inline
    ?(data.include_versions?planGuidance(data.include_locks,data.roundtrip.products.some(p=>p.read_only)):'单位：辆。当前为车型汇总，回填计划请导出版本明细。年度合计为全年SOP计划量。')
    :data.roundtrip
    ?'单位：辆。空白表示未填写，0表示明确不安排。年度合计为全年SOP；锁单行只读。'+(data.include_versions?'修改月计划量后保存，在看板“导入Excel”回填。':'当前为车型汇总，只读；回填计划请导出版本明细。')
    :'单位：辆。空白表示未填写，0表示明确不安排。年度合计为全年SOP；带“锁单”字样的行为交车锁单数。导出副本不参与计划同步。';
  setupPlanSheet(sheet,{lastColumn:layout.lastColumn,visibleLastColumn:layout.width,lastRow:layout.lastRow,identityColumns:layout.identity.length,columns:layout.columns});
  if(layout.inline&&layout.width<=6) {
    sheet.getRange('A2:'+last+'2').format={wrapText:true,rowHeight:layout.width<=4?58:44};
  }
  if(layout.inline)sheet.getRange(column(layout.productColumn)+':'+column(layout.lastColumn)).format.columnWidth=24;
  write(1,1,[[planTitle()]]);write(1,layout.firstValue,[[planPeriod(data.months)]]);
  write(2,1,[[subtitle]]);write(3,1,layout.headers);
  if(layout.section) {
    write(layout.section.title_row,1,[[layout.section.title]]);write(layout.section.header_row,1,layout.headers);
    styleManualSection(sheet,{section:layout.section,visibleLastColumn:layout.width,lastColumn:layout.lastColumn,columns:layout.columns});
  }
  for(const row of layout.rows) {
    const values=Array(layout.lastColumn).fill(null);
    row.labels.forEach((v,i)=>values[i]=v);
    for(const col of layout.columns)if(!row.formulas[col.column])values[col.column-1]=row.values[col.column]??null;
    if(layout.inline) {
      values[layout.productColumn-1]=row.productId??null;
      values[layout.kindColumn-1]=row.kind;
      values[layout.keyColumn-1]=row.key;
    }
    write(row.row,1,[values]);
    const monthStates={};
    if(data.roundtrip&&data.include_versions&&(row.kind==='manual_entry'||(row.kind==='product'&&row.metric==='SOP计划量'))) {
      const editable=row.kind==='manual_entry'?data.roundtrip.manual_editable_months:data.roundtrip.editable_months_by_product[row.productId];
      const product=layout.inline?data.roundtrip.products.find(p=>p.id===row.productId):null;
      for(const col of layout.columns)if(col.kind==='month')monthStates[column(col.column)+row.row]={editable:editable.includes(col.month),
        ...(product?{applicable:editable.includes(col.month)||((!product.start_month||col.month>=product.start_month)&&(!product.end_month||col.month<=product.end_month))}:{})};
    }
    stylePlanRow(sheet,{row:row.row,kind:row.kind,identityColumns:layout.identity.length,visibleLastColumn:layout.width,columns:layout.columns,version:row.version||'',monthStates});
    for(const [c,formula] of Object.entries(row.formulas))sheet.getRange(column(Number(c))+row.row).formulas=[[formula]];
    if(data.roundtrip&&data.include_versions) {
      if(row.kind==='product'&&row.metric==='SOP计划量') {
        if(layout.inline&&!data.roundtrip.products.find(p=>p.id===row.productId)?.read_only)unlocked.push('A'+row.row,'B'+row.row,'C'+row.row);
        for(const col of layout.columns)if(col.kind==='month'&&data.roundtrip.editable_months_by_product[row.productId].includes(col.month)) {
          const address=column(col.column)+row.row;
          unlocked.push(address);mapping.push(['plan',row.productId,col.month,address,row.values[col.column]??null]);
          sheet.getRange(address).dataValidation={rule:{type:'whole',operator:'between',formula1:0,formula2:1000000000000}};
        }
      } else if(row.kind==='manual_entry') {
        ['brand','model','version'].forEach((key,i)=>{
          const address=column(i+1)+row.row;
          unlocked.push(address);mapping.push(['manual',null,key,address,null]);
        });
        for(const col of layout.columns)if(col.kind==='month'&&data.roundtrip.manual_editable_months.includes(col.month)) {
          const address=column(col.column)+row.row;
          unlocked.push(address);mapping.push(['manual',null,col.month,address,null]);
          sheet.getRange(address).dataValidation={rule:{type:'whole',operator:'between',formula1:0,formula2:1000000000000}};
        }
      }
    }
  }
  for(const merge of layout.merges)sheet.getRange(merge).merge();
  sheet.freezePanes.freezeRows(4);sheet.freezePanes.freezeColumns(layout.identity.length);
  if(data.roundtrip) {
    const metadata=wb.worksheets.add('_sop_import');exactText[metadata.name]=[];
    if(layout.inline) {
      const manifest={columns:layout.columns,merges:layout.merges,products:data.roundtrip.products,
        rows:layout.rows.map(row=>({row:row.row,key:row.key,kind:row.kind,product_id:row.productId??null,metric:row.metric,labels:row.labels,hidden:row.hidden,
          values:Object.fromEntries(layout.columns.filter(c=>c.kind==='month').map(c=>[c.month,row.values[c.column]??null]))})),
        product_column:layout.productColumn,kind_column:layout.kindColumn,key_column:layout.keyColumn,last_column:layout.lastColumn,
        headers:layout.headers,editable_months_by_product:data.roundtrip.editable_months_by_product,
        manual_editable_months:data.roundtrip.manual_editable_months,
        plan_revisions_by_product:data.roundtrip.plan_revisions_by_product??{}};
      const chars=Array.from(JSON.stringify(manifest)),chunks=[];
      for(let i=0;i<chars.length;i+=14000)chunks.push(chars.slice(i,i+14000).join(''));
      // Numeric staging avoids a modern checkbox control for this hidden flag.
      // The finalizer restores the standard XLSX boolean type used by import.
      const rows=[['SOP_IMPORT',2],['token',data.roundtrip.token],['include_versions',Number(data.include_versions)],['sheet',sheet.name],['manifest_json',null],...chunks.map((chunk,i)=>[i+1,chunk])];
      metadata.getRange('A1:B'+rows.length).setNumberFormat('@');
      writeTo(metadata,1,1,rows);
    } else {
      const rows=[['SOP_IMPORT',1,null,null,null],['token',data.roundtrip.token,null,null,null],['include_versions',Number(data.include_versions),null,null,null],['sheet',sheet.name,null,null,null],['kind','product_id','month','cell','baseline_sop'],...mapping];
      metadata.getRange('A1:D'+rows.length).setNumberFormat('@');
      writeTo(metadata,1,1,rows);
    }
  }
  wb.recalculate();
  const grid=sheet.getRange('A1:'+last+layout.lastRow).values;
  for(const row of layout.rows)for(const col of layout.columns) {
    const actual=grid[row.row-1][col.column-1],expected=row.values[col.column]??null;
    if(expected===null?actual!==null&&actual!==undefined&&actual!=='':actual!==expected)throw new Error('Export quantity verification failed at '+column(col.column)+row.row);
  }
  if(data.preview_path)log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},summary:'Export formula check'})).ndjson);
  await fs.mkdir(path.dirname(output),{recursive:true});
  await(await SpreadsheetFile.exportXlsx(wb)).save(output);
  await fs.rm(output+'.inspect.ndjson',{force:true});
  const finalized=spawnSync(PYTHON,['-X','utf8','-c',String.raw`
import copy,json,sys,zipfile,xml.etree.ElementTree as ET
from pathlib import Path
p=Path(sys.argv[1]); config=json.loads(sys.stdin.read()); N='http://schemas.openxmlformats.org/spreadsheetml/2006/main'; ns={'m':N}; tag=lambda s:'{'+N+'}'+s
ET.register_namespace('',N); ET.register_namespace('r','http://schemas.openxmlformats.org/officeDocument/2006/relationships')
with zipfile.ZipFile(p) as z: entries={i.filename:(i,z.read(i.filename)) for i in z.infolist()}
def save(name,root): entries[name]=(entries[name][0],ET.tostring(root,encoding='utf-8',xml_declaration=True))
book=ET.fromstring(entries['xl/workbook.xml'][1])
for index,s in enumerate(book.find('m:sheets',ns),1):
    if s.attrib['name']=='_sop_import': s.set('state','veryHidden')
    filename=f'xl/worksheets/sheet{index}.xml'; current=ET.fromstring(entries[filename][1]); cells={c.attrib['r']:c for c in current.findall('m:sheetData/m:row/m:c',ns)}
    for address,text in config['text'][s.attrib['name']]:
        cell=cells.get(address)
        if cell is None: raise ValueError('Missing exported text cell '+address)
        cell.set('t','inlineStr')
        for child in list(cell): cell.remove(child)
        ET.SubElement(ET.SubElement(cell,tag('is')),tag('t')).text=text
    if s.attrib['name']=='_sop_import':
        cell=cells.get('B3')
        if cell is None: raise ValueError('Missing exported version flag')
        cell.set('t','b')
        for child in list(cell): cell.remove(child)
        ET.SubElement(cell,tag('v')).text='1' if config['include_versions'] else '0'
    save(filename,current)
save('xl/workbook.xml',book)
name='xl/worksheets/sheet1.xml'; root=ET.fromstring(entries[name][1]); cells={c.attrib['r']:c for c in root.findall('m:sheetData/m:row/m:c',ns)}
styles=ET.fromstring(entries['xl/styles.xml'][1]); xfs=styles.find('m:cellXfs',ns); unlocked_styles={}
for address in config['unlocked']:
    cell=cells.get(address)
    if cell is None: raise ValueError('Missing styled input cell '+address)
    original=int(cell.attrib.get('s','0'))
    if original not in unlocked_styles:
        style=copy.deepcopy(xfs[original])
        for old in list(style.findall('m:protection',ns)): style.remove(old)
        style.set('applyProtection','1'); ET.SubElement(style,tag('protection'),{'locked':'0','hidden':'0'})
        unlocked_styles[original]=len(xfs); xfs.append(style)
    cell.set('s',str(unlocked_styles[original]))
xfs.set('count',str(len(xfs))); save('xl/styles.xml',styles)
view=root.find('m:sheetViews/m:sheetView',ns)
if view is not None:
    pane=view.find('m:pane',ns)
    if pane is None: pane=ET.SubElement(view,tag('pane'))
    pane.attrib.update({'xSplit':str(config['frozen']),'ySplit':'4','topLeftCell':config['topLeft'],'activePane':'bottomRight','state':'frozen'})
if not config['structural']:
    protection=ET.Element(tag('sheetProtection'),{'sheet':'1','objects':'1','scenarios':'1','selectLockedCells':'0','selectUnlockedCells':'0'})
    root.insert(list(root).index(root.find('m:sheetData',ns))+1,protection)
if config['hidden_start']:
    start,end=config['hidden_start'],config['hidden_end']
    cols=root.find('m:cols',ns)
    if cols is None:
        cols=ET.Element(tag('cols'))
        root.insert(list(root).index(root.find('m:sheetData',ns)),cols)
    covered=set()
    for col in list(cols):
        first,last=int(col.attrib['min']),int(col.attrib['max'])
        if last<start or first>end: continue
        if first<start:
            col.set('max',str(start-1))
            hidden=copy.deepcopy(col)
            hidden.set('min',str(start));hidden.set('max',str(last))
            cols.insert(list(cols).index(col)+1,hidden)
            col=hidden;first=start
        col.attrib.update({'hidden':'1','width':'0','customWidth':'1'})
        covered.update(range(first,min(last,end)+1))
    for number in range(start,end+1):
        if number not in covered: ET.SubElement(cols,tag('col'),{'min':str(number),'max':str(number),'width':'0','customWidth':'1','hidden':'1'})
hidden_rows=set(config['hidden_rows'])
for row in root.findall('m:sheetData/m:row',ns):
    if int(row.attrib['r']) in hidden_rows: row.set('hidden','1')
for validation in root.findall('m:dataValidations/m:dataValidation',ns):
    validation.attrib.update({'allowBlank':'1','showInputMessage':'1','showErrorMessage':'1','errorStyle':'stop','promptTitle':'SOP计划量（辆）','prompt':'只填非负整数。空白表示未填写，0表示明确不安排。','errorTitle':'请输入非负整数','error':'SOP计划量须为非负整数；未填写时请留空。'})
save(name,root)
temporary=p.with_name(p.name+'.presentation.tmp')
with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED) as z:
    for filename,(info,content) in entries.items(): z.writestr(info,content)
temporary.replace(p)
`,output],{input:JSON.stringify({text:exactText,unlocked,include_versions:data.include_versions,frozen:layout.identity.length,topLeft:`${firstValue}5`,structural:layout.inline&&data.include_versions,hidden_start:layout.inline?layout.productColumn:null,hidden_end:layout.lastColumn,hidden_rows:layout.rows.filter(r=>r.hidden).map(r=>r.row)}),encoding:'utf8',windowsHide:true});
  if(finalized.status!==0)throw new Error(finalized.stderr||'Export presentation finalization failed');
  if(data.preview_path) {
    const reopened=await SpreadsheetFile.importXlsx(await FileBlob.load(output));
    log((await reopened.inspect({kind:'region',sheetId:sheet.name,range:'A3:'+last+Math.min(layout.lastRow,12),tableMaxRows:10,tableMaxCols:16,maxChars:1800})).ndjson);
    const png=await reopened.render({sheetName:sheet.name,range:'A1:'+last+layout.lastRow,scale:1.2,format:'png'});
    await fs.mkdir(path.dirname(data.preview_path),{recursive:true});
    await fs.writeFile(data.preview_path,new Uint8Array(await png.arrayBuffer()));
  }
  process.stdout.write(JSON.stringify({ok:true,output_path:output})+'\n');
}
main().catch(error=>{log(error?.stack??error);process.exitCode=1;});
