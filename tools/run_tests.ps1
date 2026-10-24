# 自动化测试统一入口：按层跑 L0 冒烟 / L1 接口与单元 / L3 静态检查。
#
# -Layer all 时按 L0 → L1 → L3 顺序执行，任一层失败即中止：冒烟不过就不该继续往下跑。
# L2（前端 e2e）本轮未交付，选到它会明确提示而不是假装跑过。
#
# 用法：powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Layer all
#       powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Layer L1

param(
    [ValidateSet('L0', 'L1', 'L3', 'all')]
    [string]$Layer = 'all',
    [int]$Port = 18787
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = 'python'
$script:Summary = @()

function Invoke-Step {
    param([string]$Id, [string]$Name, [scriptblock]$Action)
    Write-Host ("开始 {0}" -f $Name)
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $Action
    $code = $LASTEXITCODE
    $sw.Stop()
    $ok = ($code -eq 0)
    $script:Summary += [pscustomobject]@{
        Id = $Id; Name = $Name; Ok = $ok
        Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
    Write-Host ("{0}  {1}  {2}  {3}s" -f ($(if ($ok) { '通过' } else { '失败' }), $Id, $Name, [math]::Round($sw.Elapsed.TotalSeconds, 1)))
    return $ok
}

$steps = @()
switch ($Layer) {
    'L0' { $steps = @('L0') }
    'L1' { $steps = @('L1') }
    'L3' { $steps = @('L3') }
    'all' { $steps = @('L0', 'L1', 'L3') }
}

foreach ($step in $steps) {
    switch ($step) {
        'L0' {
            $ok = Invoke-Step -Id 'L0' -Name '冒烟测试' -Action {
                & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'smoke_test.ps1') -Port $Port
            }
        }
        'L1' {
            $ok = Invoke-Step -Id 'L1' -Name '接口与单元测试' -Action {
                & $Python -m pytest tests -q
            }
        }
        'L3' {
            $ok = Invoke-Step -Id 'L3' -Name '静态检查' -Action {
                & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'static_checks.ps1')
            }
        }
    }
    if (-not $ok) {
        Write-Host '该层未通过，按「冒烟不过不该继续」的约定中止后续层。'
        break
    }
}

Write-Host ''
Write-Host '汇总：'
foreach ($s in $script:Summary) {
    Write-Host ("  {0}  {1}  {2}  {3}s" -f ($(if ($s.Ok) { 'PASS' } else { 'FAIL' }), $s.Id, $s.Name, $s.Seconds))
}
$failed = ($script:Summary | Where-Object { -not $_.Ok }).Count
if ($failed -gt 0) { exit 1 } else { exit 0 }
