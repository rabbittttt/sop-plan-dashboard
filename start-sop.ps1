param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$sopRoot = $PSScriptRoot
$sopPort = 8876
$sopUrl = "http://127.0.0.1:$sopPort"
$sopRuntime = $env:SOP_PYTHON_PATH
if ($sopRuntime -and -not (Test-Path -LiteralPath $sopRuntime -PathType Leaf)) {
    throw "SOP_PYTHON_PATH 指向的 Python 不存在：$sopRuntime"
}
if (-not $sopRuntime) {
    $sopRuntime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
}
if (-not (Test-Path -LiteralPath $sopRuntime -PathType Leaf)) {
    $sopPython = Get-Command python -ErrorAction SilentlyContinue
    if (-not $sopPython) { throw '找不到 Python。请安装 Python 3.10 或更新版本，并安装 requirements.txt 中的依赖。' }
    $sopRuntime = $sopPython.Source
}
$env:SOP_PYTHON_PATH = $sopRuntime
$sopDataDir = Join-Path $sopRoot 'data'
New-Item -ItemType Directory -Path $sopDataDir -Force | Out-Null
$sopReady = $false
try {
    $sopResponse = Invoke-RestMethod -Uri "$sopUrl/api/health" -TimeoutSec 3
    $sopReady = $sopResponse.app_id -eq 'hima-sop' -and $sopResponse.workspace -eq $sopRoot
} catch { }
if (-not $sopReady) {
    $sopSocket = New-Object System.Net.Sockets.TcpClient
    try { $sopPortBusy = $sopSocket.ConnectAsync('127.0.0.1', $sopPort).Wait(500) -and $sopSocket.Connected } catch { $sopPortBusy = $false } finally { $sopSocket.Dispose() }
    if ($sopPortBusy) { throw "端口 $sopPort 已被其他应用占用。未启动或停止任何其他服务。" }
    $sopServer = Join-Path $sopRoot 'server.py'
    $sopProcess = Start-Process -FilePath $sopRuntime -ArgumentList @(('"' + $sopServer + '"'), '--port', $sopPort) -WorkingDirectory $sopRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $sopDataDir 'service.out.log') -RedirectStandardError (Join-Path $sopDataDir 'service.err.log') -PassThru
    Set-Content -LiteralPath (Join-Path $sopDataDir 'service.pid') -Value $sopProcess.Id -Encoding ASCII
    @{ pid=$sopProcess.Id; started_at=$sopProcess.StartTime.ToString('o'); workspace=$sopRoot; executable=$sopRuntime; port=$sopPort } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $sopDataDir 'service.json') -Encoding UTF8
    for ($sopAttempt = 0; $sopAttempt -lt 60; $sopAttempt++) {
        Start-Sleep -Milliseconds 500
        try {
            $sopResponse = Invoke-RestMethod -Uri "$sopUrl/api/health" -TimeoutSec 2
            if ($sopResponse.app_id -eq 'hima-sop' -and $sopResponse.workspace -eq $sopRoot) { $sopReady = $true; break }
        } catch { }
        if ($sopProcess.HasExited) { break }
    }
}
if (-not $sopReady) { throw "看板服务未启动。请查看 $sopDataDir\service.err.log。" }
if (-not $NoBrowser) { Start-Process $sopUrl }
Write-Output "SOP 看板已就绪：$sopUrl"
