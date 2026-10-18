# 直播数据舱 Windows 绿色版打包脚本（PowerShell 5 兼容）
# 用法（在项目根目录）：
#   powershell -ExecutionPolicy Bypass -File packaging\build_portable.ps1 -Version 1.2.0
# 产物：dist\LiveDataDeck-<version>-windows-x64.zip
# 注意：不会把 config\settings.json（含 SESSDATA）等本机配置打进包。
param(
    [Parameter(Mandatory = $true)][string]$Version
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$appName = "LiveDataDeck"
$pkgName = "$appName-$Version-windows-x64"
$distDir = Join-Path $root "dist"
$stageRoot = Join-Path $distDir "stage"
$stage = Join-Path $stageRoot $pkgName
$zip = Join-Path $distDir "$pkgName.zip"

# 打包前把 packaging\version_info.txt 的四段版本号改成 -Version 传进来的值，
# 免得 exe 属性里的版本号与 recorder\__init__.py 的 __version__ 各自漂移。
# 调用时仍需人工保证 -Version 等于 __version__。
$viPath = Join-Path $root "packaging\version_info.txt"
$vi = [int[]]($Version -split '\.')
if ($vi.Count -lt 2 -or $vi.Count -gt 4) { throw "版本号格式应为 x.y / x.y.z / x.y.z.w：$Version" }
while ($vi.Count -lt 4) { $vi += 0 }
$viTuple = "($($vi[0]), $($vi[1]), $($vi[2]), $($vi[3]))"
$viText = Get-Content -Path $viPath -Raw -Encoding UTF8
$viText = $viText -replace 'filevers=\([^)]*\)', "filevers=$viTuple"
$viText = $viText -replace 'prodvers=\([^)]*\)', "prodvers=$viTuple"
$viText = $viText -replace "StringStruct\(u'FileVersion', u'[^']*'\)", "StringStruct(u'FileVersion', u'$Version')"
$viText = $viText -replace "StringStruct\(u'ProductVersion', u'[^']*'\)", "StringStruct(u'ProductVersion', u'$Version')"
# 用无 BOM UTF-8 写回：PyInstaller 能正确解码中文，且避免每次打包都给文件加上 BOM 产生多余的 diff
[System.IO.File]::WriteAllText($viPath, $viText, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "[1/5] PyInstaller 打包..." -ForegroundColor Cyan
pyinstaller LiveDataDeck.spec --noconfirm --clean | Out-Null
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

Write-Host "[2/5] 组装发布目录 $pkgName ..." -ForegroundColor Cyan
if (Test-Path $stageRoot) { Remove-Item -Recurse -Force $stageRoot }
New-Item -ItemType Directory -Force -Path $stage | Out-Null
$built = Join-Path $distDir $appName
Copy-Item -Recurse (Join-Path $built "_internal") (Join-Path $stage "_internal")
Copy-Item (Join-Path $built "$appName.exe") (Join-Path $stage "$appName.exe")
Copy-Item (Join-Path $root "packaging\使用说明.txt") (Join-Path $stage "使用说明.txt")
# MIT 许可证与第三方组件/商标声明必须随二进制分发物一同提供
Copy-Item (Join-Path $root "LICENSE") (Join-Path $stage "LICENSE")
Copy-Item (Join-Path $root "NOTICE") (Join-Path $stage "NOTICE")
# config/、data/ 各放一个说明 txt，提醒这两个目录含凭证与录制数据，别外传
New-Item -ItemType Directory -Force -Path (Join-Path $stage "config"), (Join-Path $stage "data") | Out-Null
Set-Content -Path (Join-Path $stage "config\（配置首次运行自动生成，请勿外传此文件夹）.txt") `
    -Value "settings.json 与 targets.json 在程序首次保存设置时自动生成，包含你的 SESSDATA，请勿外传。" -Encoding UTF8
Set-Content -Path (Join-Path $stage "data\（录制数据首次运行后自动存放到这里）.txt") `
    -Value "弹幕/人气/礼物等录制数据保存在此，可在设置页修改数据目录。" -Encoding UTF8

# 手工建 zip 而不用 Compress-Archive：条目一律转成正斜杠，解压到 Linux/macOS 也不乱
Write-Host "[3/5] 压缩 zip（正斜杠条目，跨平台兼容）..." -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item -Force $zip }
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$fs = [System.IO.File]::Open($zip, [System.IO.FileMode]::Create)
$arc = New-Object System.IO.Compression.ZipArchive($fs, [System.IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($f in (Get-ChildItem -Recurse -File $stage)) {
        $rel = $f.FullName.Substring($stageRoot.Length + 1).Replace('\', '/')
        $entry = $arc.CreateEntry($rel, [System.IO.Compression.CompressionLevel]::Optimal)
        $es = $entry.Open(); $rs = [System.IO.File]::OpenRead($f.FullName)
        $rs.CopyTo($es); $es.Close(); $rs.Close()
    }
} finally { $arc.Dispose(); $fs.Close() }

# zip 内容自检：确认没混进 .py 源码 / settings.json / .env / 反斜杠条目，
# 并核对 exe、使用说明、LICENSE、NOTICE、web/index.html 这几个必需项齐全
Write-Host "[4/5] 内容自检..." -ForegroundColor Cyan
$z = [System.IO.Compression.ZipFile]::OpenRead($zip)
try {
    $names = $z.Entries.FullName
    $leak = $names | Where-Object { $_ -match '\.py$' -or $_ -match 'settings\.json' -or $_ -match '\\' -or $_ -match '\.env$' }
    if ($leak) { throw "zip 自检失败，存在源码/配置/反斜杠条目：$($leak -join ', ')" }
    foreach ($m in @("$pkgName/$appName.exe", "$pkgName/使用说明.txt", "$pkgName/LICENSE", "$pkgName/NOTICE", "$pkgName/_internal/web/index.html")) {
        if ($names -notcontains $m) { throw "zip 自检失败，缺少 $m" }
    }
    Write-Host ("  自检通过：{0} 个条目，{1:N1} MB" -f $names.Count, ((Get-Item $zip).Length / 1MB)) -ForegroundColor Green
} finally { $z.Dispose() }

Write-Host "[5/5] 完成。发布前请务必：解压 zip 到临时目录，启动 exe 验证首页与 /api/state。" -ForegroundColor Yellow
Write-Host $zip
