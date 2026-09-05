<#
   一键改机：把 apk/ 里的四个 Miku 应用指向你自己的服务端并签名。
   用法（在本目录执行）：
     .\patch-apk.ps1                                  # 默认 http://192.168.31.104:8080
     .\patch-apk.ps1 -BaseUrl http://miku.example.com
     .\patch-apk.ps1 -BaseUrl http://miku.example.com -NoSharedLib
   参数：
     -BaseUrl     客户端要访问的服务端根地址（http:// 或 https:// 开头，不带末尾斜杠）
     -NoSharedLib 若设备没有刷 Xperia 联动包，加这个开关删掉 <uses-library> 依赖，
                  否则安装会报 INSTALL_FAILED_MISSING_SHARED_LIBRARY
   产物：本目录 patched/ 下的四个 apk，直接 adb install。
#>
param(
    [string]$BaseUrl = "http://192.168.31.104:8080",
    [switch]$NoSharedLib
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

# ---- 0. 环境与参数检查 ----
if (-not (Get-Command java -ErrorAction SilentlyContinue)) {
    throw "找不到 java：请先安装 JDK 8+ 并把 java 加入 PATH"
}
$Jars = @("apktool_2.9.3.jar", "uber-apk-signer-1.3.0.jar")
foreach ($j in $Jars) {
    if (-not (Test-Path -LiteralPath (Join-Path $Root $j))) {
        throw "缺少 $j，请把它放到本目录（$Root）"
    }
}
$BaseUrl = $BaseUrl.TrimEnd('/')
if ($BaseUrl -notmatch '^https?://') {
    throw "BaseUrl 必须以 http:// 或 https:// 开头，例如 http://miku.example.com"
}

# ---- 1. 准备目录 ----
$Work = Join-Path $Root "apk-work"
$Out  = Join-Path $Work "out"
$Patched = Join-Path $Root "patched"
Remove-Item -Recurse -Force $Work, $Patched -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Out, $Patched | Out-Null

# 旧域名清单（只替换主机部分，保留后面的路径）
$OldHosts = @(
    "http://distribute.mikuxperia.com",
    "http://auth.mikuxperia.com",
    "http://evawdt.otenki.co.jp"
)

$Targets = @(
    @{ apk = "com.mikuxperia.featuresongsplayerapp.apk"; dir = "featuresongs" },
    @{ apk = "com.mikuxperia.mikunewsapp.apk";           dir = "mikunews" },
    @{ apk = "com.mikuxperia.mikuweatherwidget.apk";     dir = "mikuweather" },
    @{ apk = "MikuDownloader.apk";                       dir = "downloader" }
)

foreach ($t in $Targets) {
    $srcApk = Join-Path (Join-Path $Root "apk") $t.apk
    if (-not (Test-Path -LiteralPath $srcApk)) {
        throw "找不到 $srcApk，请确认 apk/ 目录完整"
    }
    $dirPath = Join-Path $Work $t.dir

    Write-Host ""
    Write-Host "==> [1/3] 解包 $($t.apk)"
    & java -jar (Join-Path $Root "apktool_2.9.3.jar") d -f -o $dirPath $srcApk
    if ($LASTEXITCODE -ne 0) { throw "apktool 解包失败：$($t.apk)" }

    Write-Host "==> [2/3] 替换域名 -> $BaseUrl"
    $replaced = 0
    Get-ChildItem -Path $dirPath -Recurse -Filter *.smali | ForEach-Object {
        $text = [System.IO.File]::ReadAllText($_.FullName)
        $changed = $false
        foreach ($o in $OldHosts) {
            if ($text.IndexOf($o, [System.StringComparison]::Ordinal) -ge 0) {
                $text = $text.Replace($o, $BaseUrl)
                $changed = $true
            }
        }
        if ($changed) {
            [System.IO.File]::WriteAllText($_.FullName, $text, (New-Object System.Text.UTF8Encoding($false)))
            $replaced++
        }
    }
    Write-Host "        共改动 $replaced 个 smali 文件"

    if ($NoSharedLib) {
        Write-Host "==>      删除 <uses-library> 依赖（-NoSharedLib）"
        $manifest = Join-Path $dirPath "AndroidManifest.xml"
        $m = [System.IO.File]::ReadAllText($manifest)
        $m = [regex]::Replace($m, '\s*<uses-library android:name="com\.mikuxperia\.mikuxperia_library"\s*/>', '')
        [System.IO.File]::WriteAllText($manifest, $m, (New-Object System.Text.UTF8Encoding($false)))
    }

    Write-Host "==> [3/3] 重打包 $($t.apk)"
    $outApk = Join-Path $Out ($t.apk -replace '\.apk$', '-unsigned.apk')
    & java -jar (Join-Path $Root "apktool_2.9.3.jar") b $dirPath -o $outApk
    if ($LASTEXITCODE -ne 0) { throw "apktool 重打包失败：$($t.apk)" }
}

# ---- 2. 签名（自动 zipalign，Android 4.2 认 v1）----
Write-Host ""
Write-Host "==> 签名"
& java -jar (Join-Path $Root "uber-apk-signer-1.3.0.jar") --apks $Out --allowResign
if ($LASTEXITCODE -ne 0) { throw "签名失败" }

# ---- 3. 收拢成品到 patched/ ----
Write-Host ""
Write-Host "==> 收拢成品"
$Results = @()
foreach ($t in $Targets) {
    $signedName = $t.apk -replace '\.apk$', '-aligned-debugSigned.apk'
    $signed = Join-Path $Out $signedName
    if (-not (Test-Path -LiteralPath $signed)) {
        throw "没找到签名产物 $signedName"
    }
    Copy-Item -LiteralPath $signed -Destination (Join-Path $Patched $t.apk)
    $Results += $t.apk
}

Write-Host ""
Write-Host "完成！成品在 patched/ 目录："
$Results | ForEach-Object { Write-Host "  patched/$_" }
Write-Host ""
Write-Host "安装到设备（设备上已有旧版请先 adb uninstall 同名包）："
Write-Host "  adb install patched/<上面的文件名>"
