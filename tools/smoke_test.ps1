# 冒烟测试（L0）：几十秒内回答「这次改动之后，服务还起得来、界面还打得开、核心只读链路还活着吗」。
#
# 它自动化的是 AGENTS.md「多实例教训」里那条人工动作：旧进程占用 8787 时新进程 bind 失败退出，
# 但 API 仍由旧进程服务，表现为「端口有响应、新字段不出现」。这里用版本指纹断言来替代肉眼看字段：
# 请求 /openapi.json，断言 info.version 与 recorder.__version__ 一致；不一致就说明这个端口上的
# 进程不是本次要验的代码，直接判失败。
#
# 不验业务细节：需要真实数据的生成类接口（战报 PNG、去重、AI 解读）归 L1，冒烟不碰。
# 用临时端口而非 8787，避免干扰用户正在用的实例；跑完自动收进程、不留残留。
#
# 用法：powershell -ExecutionPolicy Bypass -File tools\smoke_test.ps1 [-Port 18787]

param(
    [int]$Port = 18787,
    [int]$BootTimeoutSec = 40
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = 'python'
$script:Results = @()
$script:Process = $null
$FailCount = 0

function Add-Result {
    param([string]$Id, [string]$Name, [bool]$Ok, [string]$Detail)
    $script:Results += [pscustomobject]@{ Id = $Id; Name = $Name; Ok = $Ok; Detail = $Detail }
    $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("{0}  {1}  {2}  {3}" -f $mark, $Id, $Name, $Detail)
    if (-not $Ok) { $script:FailCount++ }
}

function Test-PortListening {
    param([int]$TargetPort)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $TargetPort, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(300)
        return ($ok -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-PortOwnerPids {
    param([int]$TargetPort)
    $pids = @()
    $lines = netstat -ano | Select-String -Pattern (":$TargetPort\s")
    foreach ($line in $lines) {
        $text = ($line.ToString() -replace '\s+', ' ').Trim()
        if ($text -notmatch 'LISTENING') { continue }
        $parts = $text.Split(' ')
        $owner = $parts[-1]
        if ($owner -match '^\d+$') { $pids += [int]$owner }
    }
    return ($pids | Select-Object -Unique)
}

function Invoke-GetResult {
    # 返回 @{ Status = <int>; Body = <string>; ContentType = <string> }，不因非 2xx 抛异常
    param([string]$Url, [int]$TimeoutSec = 10)
    try {
        $resp = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing
        return @{ Status = [int]$resp.StatusCode; Body = $resp.Content;
                  ContentType = [string]$resp.Headers['Content-Type'] }
    } catch {
        $r = $_.Exception.Response
        if ($null -ne $r) {
            $code = [int]$r.StatusCode
            $body = ''
            try { $body = (New-Object System.IO.StreamReader($r.GetResponseStream())).ReadToEnd() } catch { }
            return @{ Status = $code; Body = $body; ContentType = '' }
        }
        return @{ Status = -1; Body = $_.Exception.Message; ContentType = '' }
    }
}

function Stop-SmokeProcess {
    if ($script:Process -and -not $script:Process.HasExited) {
        try { Stop-Process -Id $script:Process.Id -Force -ErrorAction SilentlyContinue } catch { }
        try { $script:Process.WaitForExit(5000) | Out-Null } catch { }
    }
    # run.py 若拉起了子进程，一并按命令行匹配清掉（只清本次端口对应的）
    Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match "run\.py" -and $_.CommandLine -match "--port $Port" } |
        ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch { } }
}

Write-Host ("冒烟测试开始（临时端口 {0}，仓库根 {1}）" -f $Port, $Root)

# SMK-前置：目标端口不得已被占用
$occupied = Get-PortOwnerPids -TargetPort $Port
if ($occupied.Count -gt 0) {
    Add-Result 'SMK-00' "端口前置检查" $false "端口 $Port 已被进程占用（PID $($occupied -join ',')），先释放再跑"
    Write-Host '端口被占用，已中止，不盲目起新进程。'
    exit 1
}
Add-Result 'SMK-00' '端口前置检查' $true "端口 $Port 空闲"

$stdoutLog = Join-Path $env:TEMP "ldd_smoke_$Port.out.log"
$stderrLog = Join-Path $env:TEMP "ldd_smoke_$Port.err.log"
$expectedVersion = ''
try { $expectedVersion = (& $Python -c 'import recorder; print(recorder.__version__)' 2>$null) } catch { }
if (-not $expectedVersion) { $expectedVersion = 'unknown' }

# SMK-01：进程启动
$started = $false
try {
    $script:Process = Start-Process -FilePath $Python `
        -ArgumentList @('run.py', '--no-browser', '--port', "$Port") `
        -PassThru -WindowStyle Hidden -WorkingDirectory $Root `
        -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
} catch {
    Add-Result 'SMK-01' '进程启动' $false "Start-Process 失败：$($_.Exception.Message)"
}

if ($script:Process) {
    $deadline = (Get-Date).AddSeconds($BootTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($script:Process.HasExited) { break }
        if (Test-PortListening -TargetPort $Port) { $started = $true; break }
        Start-Sleep -Milliseconds 400
    }
    Add-Result 'SMK-01' '进程启动' $started `
        ($(if ($started) { "端口 $Port 已进入 LISTENING" }
           else { "限时 ${BootTimeoutSec}s 内端口未就绪（进程退出=$($script:Process.HasExited)）" }))
}

if (-not $started) {
    if (Test-Path $stderrLog) {
        Write-Host '--- 启动期 stderr 末尾 20 行 ---'
        Get-Content $stderrLog -Tail 20 | ForEach-Object { Write-Host $_ }
    }
    Stop-SmokeProcess
    Write-Host ("冒烟中止：失败 {0} 项" -f $script:FailCount)
    exit 1
}

$base = "http://127.0.0.1:$Port"

# SMK-02：版本指纹（防旧进程串场）
$openapi = Invoke-GetResult -Url "$base/openapi.json"
$gotVersion = ''
if ($openapi.Status -eq 200) {
    try { $gotVersion = ($openapi.Body | ConvertFrom-Json).info.version } catch { }
}
Add-Result 'SMK-02' '版本指纹一致' ($openapi.Status -eq 200 -and $gotVersion -eq $expectedVersion) `
    "期望 v$expectedVersion / 实际 v$gotVersion（status=$($openapi.Status)）"

# SMK-03：运行态接口
$state = Invoke-GetResult -Url "$base/api/state"
$stateOk = $false
if ($state.Status -eq 200) {
    try {
        $obj = $state.Body | ConvertFrom-Json
        $stateOk = ($null -ne $obj.rooms) -and ($null -ne $obj.settings)
    } catch { }
}
$targets = Invoke-GetResult -Url "$base/api/targets"
$logs = Invoke-GetResult -Url "$base/api/logs"
Add-Result 'SMK-03' '运行态接口' ($stateOk -and $targets.Status -eq 200 -and $logs.Status -eq 200) `
    "/api/state=$($state.Status) 含rooms+settings=$stateOk / targets=$($targets.Status) / logs=$($logs.Status)"

# SMK-04：首页可达且引用四个带版本号的资源
$index = Invoke-GetResult -Url "$base/"
$assetRefs = 0
$hasTitle = $false
if ($index.Status -eq 200) {
    $hasTitle = ($index.Body -match '<title>')
    foreach ($name in @('styles.css', 'app.js', 'pulse-strip.js', 'onboarding.js')) {
        if ($index.Body -match [regex]::Escape($name + '?v=')) { $assetRefs++ }
    }
}
Add-Result 'SMK-04' '首页可达' ($index.Status -eq 200 -and $hasTitle -and $assetRefs -eq 4) `
    "status=$($index.Status) title=$hasTitle 带?v=资源=$assetRefs/4"

# SMK-05：四个静态资源可下载且 Content-Type 正确
$assetExpect = @{
    '/styles.css'     = 'text/css'
    '/app.js'         = 'javascript'
    '/pulse-strip.js' = 'javascript'
    '/onboarding.js'  = 'javascript'
}
$assetBad = @()
foreach ($k in $assetExpect.Keys) {
    $r = Invoke-GetResult -Url "$base$k"
    if ($r.Status -ne 200 -or $r.ContentType -notlike "*$($assetExpect[$k])*") {
        $assetBad += "$k(status=$($r.Status),ct=$($r.ContentType))"
    }
}
Add-Result 'SMK-05' '静态资源可达' ($assetBad.Count -eq 0) `
    ($(if ($assetBad.Count -eq 0) { '4/4 全部 200 且 Content-Type 正确' } else { $assetBad -join '; ' }))

# SMK-06：空数据不崩（无房间、无录制数据时只读接口不得 500）
$emptyUrls = @('/api/videos', '/api/report-theme', '/api/danmaku-style', '/api/files',
               '/api/cared-users', '/api/videos/ffmpeg', '/api/settings', '/api/logs')
$fiveHundred = @()
foreach ($u in $emptyUrls) {
    $r = Invoke-GetResult -Url "$base$u"
    if ($r.Status -eq -1 -or $r.Status -ge 500) { $fiveHundred += "$u=$($r.Status)" }
}
Add-Result 'SMK-06' '空数据不崩' ($fiveHundred.Count -eq 0) `
    ($(if ($fiveHundred.Count -eq 0) { "8 个只读接口无一 5xx" } else { $fiveHundred -join '; ' }))

# SMK-07：WS 握手（可选，失败仅告警）
$wsOk = $false
$wsNote = ''
try {
    $ws = New-Object System.Net.WebSockets.ClientWebSocket
    $cts = New-Object System.Threading.CancellationTokenSource
    $cts.CancelAfter(4000)
    $uri = [System.Uri]"ws://127.0.0.1:$Port/api/ws"
    $task = $ws.ConnectAsync($uri, $cts.Token)
    $task.Wait(5000) | Out-Null
    if ($ws.State -eq [System.Net.WebSockets.WebSocketState]::Open) {
        $buf = New-Object byte[] 4096
        $seg = New-Object System.ArraySegment[byte] -ArgumentList @(, $buf)
        $rtask = $ws.ReceiveAsync($seg, $cts.Token)
        $wsOk = $rtask.Wait(5000)
        $wsNote = "State=$($ws.State)"
    } else {
        $wsNote = "State=$($ws.State)"
    }
    try { $ws.Dispose() } catch { }
} catch {
    $wsNote = $_.Exception.Message
}
if (-not $wsOk) { Write-Host "WARN  SMK-07  WS 握手未通过（可选项，仅告警）：$wsNote" }
else { Write-Host "PASS  SMK-07  WS 握手通过" }

# SMK-08：停进程后端口释放、无残留
Stop-SmokeProcess
Start-Sleep -Milliseconds 800
$stillListening = Test-PortListening -TargetPort $Port
$leftOver = Get-PortOwnerPids -TargetPort $Port
Add-Result 'SMK-08' '端口释放与无残留' (-not $stillListening) `
    ($(if (-not $stillListening) { "端口 $Port 已释放" } else { "端口仍被 PID $($leftOver -join ',') 占用" }))

# 额外：8787 残留检查（不影响本层结论，只提示）
$mainLeft = @()
Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match "run\.py" } |
    ForEach-Object { $mainLeft += "PID $($_.ProcessId)" }
if ($mainLeft.Count -gt 0) { Write-Host ("WARN  本机仍有 run.py 进程存活：{0}" -f ($mainLeft -join ', ')) }

$pass = ($script:Results | Where-Object { $_.Ok }).Count
Write-Host ("冒烟结束：通过 {0}/{1}，失败 {2}" -f $pass, $script:Results.Count, $script:FailCount)
if ($script:FailCount -gt 0) { exit 1 } else { exit 0 }
