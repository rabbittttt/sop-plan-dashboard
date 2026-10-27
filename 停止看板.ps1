$ErrorActionPreference = 'Stop'
$sopRoot = $PSScriptRoot
$sopMetaPath = Join-Path $sopRoot 'data\service.json'
if (-not (Test-Path -LiteralPath $sopMetaPath)) { Write-Host '没有本项目启动记录。'; exit }
$sopMeta = Get-Content -LiteralPath $sopMetaPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($sopMeta.workspace -ne $sopRoot) { throw '启动记录不属于当前项目。' }
$sopProc = Get-Process -Id ([int]$sopMeta.pid) -ErrorAction SilentlyContinue
if (-not $sopProc) { Write-Host '服务已经停止。'; exit }
$sopExpectedStart = [DateTime]::Parse($sopMeta.started_at)
if ($sopProc.ProcessName -ne 'python' -or [Math]::Abs(($sopProc.StartTime - $sopExpectedStart).TotalSeconds) -gt 2) { throw '进程身份不匹配，已保留该进程。' }
Stop-Process -Id ([int]$sopMeta.pid) -Force -ErrorAction Stop
Write-Host '本项目看板服务已停止。其他网页不受影响。'
