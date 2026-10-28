// The maintenance workbook and downloaded copies share one presentation source.
// Values, formulas, trusted input coordinates and protection remain in adapters.
const borders = {preset:'all',style:'thin',color:'#8498AA'};

export function column(n) {
  let value = '';
  for (; n; n = Math.floor((n - 1) / 26)) value = String.fromCharCode(65 + (n - 1) % 26) + value;
  return value;
}

export function planTitle() {
  return 'SOP计划';
}

export function planPeriod(months) {
  return months[0] === months.at(-1) ? months[0] : `${months[0]}—${months.at(-1)}`;
}

export function planGuidance(includeLocks = false, hasReadonly = false) {
  return '单位：辆。只填版本月计划量；可在主表新增、删除行或修改名称，删除表示停用并保留历史。合计自动计算。'
    + (includeLocks ? '锁单行无需填写。' : '')
    + (hasReadonly ? '汇总代际由成员计划自动相加，只读；请切换单代际修改。' : '');
}

// This function never writes user text. Call the adapter's safe text writer for
// title, subtitle and headers; header merges are also supplied by its layout.
export function setupPlanSheet(sheet, {
  lastColumn, visibleLastColumn, lastRow, identityColumns = 3,
  columns, headerRow = 3, firstDataRow = 5,
}) {
  const last = column(lastColumn), visibleLast = column(visibleLastColumn);
  sheet.showGridLines = false;
  sheet.getRange(`A1:${last}${lastRow}`).format = {
    font:{name:'Microsoft YaHei',size:11,color:'#243649'},
    rowHeight:28, verticalAlignment:'center',
  };
  const identityWidths = [10,20,32];
  for (let c = 1; c <= identityColumns; c++) {
    sheet.getRange(`${column(c)}:${column(c)}`).format.columnWidth = identityWidths[c - 1] ?? 12;
  }
  if (visibleLastColumn > identityColumns) {
    sheet.getRange(`${column(identityColumns + 1)}:${visibleLast}`).format.columnWidth = 10;
  }
  for (const col of columns.filter(c=>c.kind==='year_total')) {
    sheet.getRange(`${column(col.column)}:${column(col.column)}`).format.columnWidth = 14;
  }
  if (lastColumn > visibleLastColumn) {
    sheet.getRange(`${column(visibleLastColumn + 1)}:${last}`).format.columnWidth = 24;
  }
  const identityLast = column(identityColumns), periodFirst = column(identityColumns + 1);
  sheet.getRange(`A1:${identityLast}1`).merge();
  sheet.getRange(`${periodFirst}1:${visibleLast}1`).merge();
  sheet.getRange(`A2:${visibleLast}2`).merge();
  sheet.getRange(`A1:${identityLast}1`).format = {
    font:{name:'Microsoft YaHei',size:18,bold:true,color:'#086BAE'},
    horizontalAlignment:'center', rowHeight:40,
  };
  sheet.getRange(`${periodFirst}1:${visibleLast}1`).format = {
    font:{name:'Microsoft YaHei',size:12,bold:true,color:'#52677C'},
    horizontalAlignment:'left', rowHeight:40,
  };
  sheet.getRange(`A2:${visibleLast}2`).format = {
    font:{name:'Microsoft YaHei',size:10,color:'#52677C'},
    rowHeight:29, horizontalAlignment:'left',
  };
  sheet.getRange(`A${headerRow}:${last}${firstDataRow - 1}`).format = {
    fill:'#0871B8', font:{name:'Microsoft YaHei',size:11,bold:true,color:'#FFFFFF'},
    horizontalAlignment:'center', rowHeight:30, borders,
  };
  for (const col of columns.filter(c=>c.kind==='month'&&c.historical)) {
    sheet.getRange(`${column(col.column)}${firstDataRow - 1}`).format.fill = '#6F8498';
  }
}

export function stylePlanRow(sheet, {
  row, kind, identityColumns = 3, visibleLastColumn, columns,
  version = '', monthStates = {},
}) {
  const visibleLast = column(visibleLastColumn), identityLast = column(identityColumns);
  const numericRange = `${column(identityColumns + 1)}${row}:${visibleLast}${row}`;
  sheet.getRange(`A${row}:${visibleLast}${row}`).format.borders = borders;
  if (visibleLastColumn > identityColumns) {
    sheet.getRange(numericRange).format = {numberFormat:'#,##0',horizontalAlignment:'right'};
  }
  if (kind === 'product' || kind === 'manual_entry') {
    sheet.getRange(`A${row}:${identityLast}${row}`).format = {
      ...(kind === 'manual_entry' ? {fill:'#FFFFFF'} : {}),
      font:{bold:true}, horizontalAlignment:'center', wrapText:true,
    };
    if (kind === 'product') sheet.getRange(`${identityLast}${row}`).format.rowHeight = version.length > 20 ? 46 : 30;
    for (const col of columns.filter(c=>c.kind==='month')) {
      const address = `${column(col.column)}${row}`;
      const state = monthStates[address] ?? {};
      const active = state.editable !== false;
      sheet.getRange(address).format.fill = !active ? '#DCE2E8' : col.historical ? '#F0F2F4' : '#FFFFFF';
      if (kind === 'product' && state.applicable === false) sheet.getRange(address).format.font.color = '#68798A';
    }
  } else {
    sheet.getRange(`A${row}:${visibleLast}${row}`).format = {
      fill:kind === 'grand_total' ? '#DCEAF7' : '#EAF2F9', font:{bold:true},
    };
    if (visibleLastColumn > identityColumns) sheet.getRange(numericRange).format.font.color = '#B62025';
  }
  for (const col of columns.filter(c=>c.kind==='year_total')) {
    sheet.getRange(`${column(col.column)}${row}`).format = {fill:'#FFF8E4',font:{bold:true,color:'#B62025'}};
  }
}

export function styleManualSection(sheet, {section, visibleLastColumn, lastColumn, columns}) {
  const visibleLast = column(visibleLastColumn), last = column(lastColumn);
  sheet.getRange(`A${section.title_row}:${visibleLast}${section.title_row}`).format = {
    fill:'#DCEAF7',font:{bold:true,color:'#086BAE'},rowHeight:36,
  };
  sheet.getRange(`A${section.header_row}:${last}${section.header_row + 1}`).format = {
    fill:'#0871B8',font:{bold:true,color:'#FFFFFF'},horizontalAlignment:'center',rowHeight:30,borders,
  };
  for (const col of columns.filter(c=>c.kind==='month'&&c.historical)) {
    sheet.getRange(`${column(col.column)}${section.header_row + 1}`).format.fill = '#6F8498';
  }
}
