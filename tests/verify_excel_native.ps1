param([Parameter(Mandatory=$true)][string[]]$Paths)
# Optional Windows acceptance check using an installed Microsoft Excel.
# Normal read-only open deliberately does not request Excel's repair mode.
$ErrorActionPreference = 'Stop'
$sopExcel = New-Object -ComObject Excel.Application
$sopExcel.Visible = $false
$sopExcel.DisplayAlerts = $false
try {
    foreach ($sopPath in $Paths) {
        $sopFile = (Resolve-Path -LiteralPath $sopPath).Path
        if ([IO.Path]::GetExtension($sopFile) -ne '.xlsx') { throw 'Only .xlsx test files are supported.' }
        $sopBook = $null
        $sopSheet = $null
        try {
            $sopBook = $sopExcel.Workbooks.Open($sopFile, 0, $true)
            $sopSheet = $sopBook.Worksheets.Item(1)
            $sopVersionHeader = [string]::Concat([char]0x7248, [char]0x672C)
            $sopIdentityColumns = if ($sopSheet.Cells.Item(3, 3).Value2 -eq $sopVersionHeader) { 3 } else { 2 }
            if ($sopSheet.Range('A1').MergeArea.Columns.Count -ne $sopIdentityColumns) {
                throw "Title merge was lost: $sopFile"
            }
            if ($sopSheet.Columns.Item(2).ColumnWidth -le $sopSheet.Columns.Item(1).ColumnWidth) {
                throw "Column widths were lost: $sopFile"
            }
            $sopLastColumn = $sopSheet.UsedRange.Columns.Count
            foreach ($sopColumn in (($sopLastColumn-2)..$sopLastColumn)) {
                if (-not $sopSheet.Columns.Item($sopColumn).Hidden) {
                    throw "Import-only column $sopColumn is visible: $sopFile"
                }
            }
            [PSCustomObject]@{
                File=$sopFile; Opened=$true; ExcelVersion=$sopExcel.Version;
                Build=$sopExcel.Build; TitleMerge=$sopSheet.Range('A1').MergeArea.Address();
                HiddenColumns=3
            } | ConvertTo-Json -Compress
        } finally {
            if ($sopSheet) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($sopSheet) }
            if ($sopBook) {
                $sopBook.Close($false)
                [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($sopBook)
            }
        }
    }
} finally {
    $sopExcel.Quit()
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($sopExcel)
}
