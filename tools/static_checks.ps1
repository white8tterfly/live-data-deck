# 静态检查（L3）：把 code-comment-style 里原先靠人手敲的命令固化下来。
#
# 检查项：注释里的装饰性横幅、注释里的 emoji、书面腔套话（模板词库）、C0x 功能编号残留、
# Python 语法、JS 语法、应用可构造、前端资源 ?v= 是否回退。
# 文本扫描刻意只看注释行，避免把 UI 字符串里合法的 emoji 与术语误判成问题。
#
# 用法：powershell -ExecutionPolicy Bypass -File tools\static_checks.ps1

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Python = 'python'
$script:Fail = 0
$script:Skip = 0

function Add-Check {
    param([string]$Id, [string]$Name, [bool]$Ok, [string]$Detail)
    $mark = if ($Ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("{0}  {1}  {2}  {3}" -f $mark, $Id, $Name, $Detail)
    if (-not $Ok) { $script:Fail++ }
}

function Add-Skip {
    param([string]$Id, [string]$Name, [string]$Detail)
    Write-Host ("SKIP  {0}  {1}  {2}" -f $Id, $Name, $Detail)
    $script:Skip++
}

# 扫描范围：手写源码；排除构建产物、依赖目录与本地私有目录
$excludeDirs = @('build', 'dist', 'node_modules', '.venv', 'venv', '__pycache__',
                 '.pytest_cache', '.ruff_cache', '.trae', '.git', 'data', 'config')
$exts = @('.py', '.js', '.css', '.html', '.ps1', '.spec')

function Get-SourceFiles {
    param([string[]]$Extensions)
    $all = Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue
    $all | Where-Object {
        $rel = $_.FullName.Substring($Root.Length).TrimStart('\')
        $parts = $rel.Split('\')
        $blocked = $false
        foreach ($p in $parts) { if ($excludeDirs -contains $p) { $blocked = $true; break } }
        (-not $blocked) -and ($Extensions -contains $_.Extension.ToLower())
    }
}

$sourceFiles = @(Get-SourceFiles -Extensions $exts)
Write-Host ("静态检查开始：扫描 {0} 个源文件（根目录 {1}）" -f $sourceFiles.Count, $Root)

# 判断一行是否属于注释（覆盖 py 的 #、js/css 的 // 与 /* */、html 的 <!-- -->）
$commentRe = [regex]'^\s*(#|//|/\*|\*|<!--)'
# emoji：BMP 里的符号区 + 代理对高位（非 BMP 字符）
$emojiRe = [regex]"[\u2600-\u27BF\u2B00-\u2BFF\uFE0F\uD800-\uDBFF]"
$bannerRe = [regex]'(={5,}|-{5,}|_{5,}|\*{5,})'
$templateRe = [regex]'旨在|以便|从而|确保|请注意|值得注意的是|综上所述|总而言之|本函数|本模块'
$c0xRe = [regex]'C0[0-9]'

function Scan-Comments {
    param([string]$PatternName, [regex]$Re, [string]$Id, [string]$Name)
    $hits = @()
    foreach ($f in $sourceFiles) {
        $rel = $f.FullName.Substring($Root.Length).TrimStart('\')
        $n = 0
        foreach ($line in (Get-Content -LiteralPath $f.FullName -Encoding UTF8 -ErrorAction SilentlyContinue)) {
            $n++
            if ($line -notmatch $commentRe) { continue }
            if ($Re.IsMatch($line)) { $hits += "$rel`:$n" }
        }
    }
    Add-Check $Id $Name ($hits.Count -eq 0) `
        ($(if ($hits.Count -eq 0) { "$PatternName 零命中" } else { "$PatternName 命中 $($hits.Count) 处：" + (($hits | Select-Object -First 8) -join ', ') }))
}

Scan-Comments -PatternName '装饰性横幅' -Re $bannerRe -Id 'ST-01' -Name '注释无装饰性横幅'
Scan-Comments -PatternName '注释 emoji' -Re $emojiRe -Id 'ST-02' -Name '注释无 emoji'
Scan-Comments -PatternName '模板词' -Re $templateRe -Id 'ST-03' -Name '注释无模板词'
Scan-Comments -PatternName 'C0x 编号' -Re $c0xRe -Id 'ST-04' -Name '注释无 C0x 编号残留'

# ST-05 Python 语法
$pyFiles = @($sourceFiles | Where-Object { $_.Extension -eq '.py' })
if ($pyFiles.Count -gt 0) {
    $out = & $Python -m py_compile @($pyFiles | ForEach-Object { $_.FullName }) 2>&1
    $ok = ($LASTEXITCODE -eq 0)
    Add-Check 'ST-05' 'Python 语法' $ok `
        ($(if ($ok) { "$($pyFiles.Count) 个文件通过 py_compile" } else { ($out | Select-Object -First 5) -join ' | ' }))
} else {
    Add-Skip 'ST-05' 'Python 语法' '未找到 .py 文件'
}

# ST-06 JS 语法（依赖本机 Node；缺失则跳过并注明）
$jsFiles = @($sourceFiles | Where-Object { $_.Extension -eq '.js' })
$node = Get-Command node -ErrorAction SilentlyContinue
if ($null -eq $node) {
    Add-Skip 'ST-06' 'JS 语法' '本机无 Node，跳过 node --check'
} else {
    $bad = @()
    foreach ($f in $jsFiles) {
        $out = & node --check $f.FullName 2>&1
        if ($LASTEXITCODE -ne 0) { $bad += "$($f.Name)：$(($out | Select-Object -First 2) -join ' ')" }
    }
    Add-Check 'ST-06' 'JS 语法' ($bad.Count -eq 0) `
        ($(if ($bad.Count -eq 0) { "$($jsFiles.Count) 个文件通过 node --check" } else { $bad -join '; ' }))
}

# ST-07 应用可构造（能 import 出 create_app 并统计路由数）
$routeOut = & $Python -c "from server.app import create_app; app = create_app(); print(len(app.routes))" 2>&1
$routeOk = ($LASTEXITCODE -eq 0)
Add-Check 'ST-07' '应用可构造' $routeOk `
    ($(if ($routeOk) { "create_app() 成功，注册路由 $($routeOut -join '') 条" } else { ($routeOut | Select-Object -First 3) -join ' | ' }))

# ST-08 前端资源 ?v= 递增比对（快照存 tools/.version_snapshot.json）
$indexPath = Join-Path $Root 'web\index.html'
$snapshotPath = Join-Path $PSScriptRoot '.version_snapshot.json'
$assets = @{}
if (Test-Path $indexPath) {
    $html = Get-Content -LiteralPath $indexPath -Raw -Encoding UTF8
    foreach ($name in @('styles.css', 'onboarding.js', 'pulse-strip.js', 'app.js')) {
        $m = [regex]::Match($html, [regex]::Escape($name) + '\?v=(\d+)')
        if ($m.Success) { $assets[$name] = [int]$m.Groups[1].Value }
    }
}
if ($assets.Count -lt 4) {
    Add-Check 'ST-08' '前端资源 ?v=' $false "web/index.html 只解析出 $($assets.Count)/4 个资源版本号"
} elseif (-not (Test-Path $snapshotPath)) {
    ($assets | ConvertTo-Json) | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
    Add-Check 'ST-08' '前端资源 ?v=' $true "首次运行，已建立基线快照（$(($assets.Keys | ForEach-Object { "$_=$($assets[$_])" }) -join ' ')）"
} else {
    $prev = Get-Content -LiteralPath $snapshotPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $regressed = @()
    foreach ($k in $assets.Keys) {
        $p = $prev.$k
        if ($null -ne $p -and $assets[$k] -lt [int]$p) { $regressed += "$k $p→$($assets[$k])" }
    }
    if ($regressed.Count -eq 0) {
        ($assets | ConvertTo-Json) | Set-Content -LiteralPath $snapshotPath -Encoding UTF8
        Add-Check 'ST-08' '前端资源 ?v=' $true "无回退（$(($assets.Keys | ForEach-Object { "$_=$($assets[$_])" }) -join ' ')）"
    } else {
        Add-Check 'ST-08' '前端资源 ?v=' $false ("版本号回退：" + ($regressed -join ', '))
    }
}

Write-Host ("静态检查结束：失败 {0}，跳过 {1}" -f $script:Fail, $script:Skip)
if ($script:Fail -gt 0) { exit 1 } else { exit 0 }
