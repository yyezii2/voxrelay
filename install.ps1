$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

Write-Host "VoxRelay 安装" -ForegroundColor Cyan

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "找不到 python。请先安装 Python 3.10+ 并勾选 Add to PATH。" -ForegroundColor Red
    exit 1
}
Write-Host ("使用 {0}" -f $py.Source)

if (-not (Test-Path ".venv")) {
    Write-Host "创建虚拟环境 .venv ..."
    & python -m venv .venv
}
$vpy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

Write-Host "安装 edge-tts ..."
& $vpy -m pip install --disable-pip-version-check --upgrade pip | Out-Null
& $vpy -m pip install --disable-pip-version-check edge-tts

if (-not (Test-Path "config.json")) {
    Copy-Item "config.example.json" "config.json"
    Write-Host "已生成 config.json"
}

$port = 8787
try {
    $port = (Get-Content "config.json" -Raw -Encoding UTF8 | ConvertFrom-Json).port
} catch { }

Write-Host ""
Write-Host "完成。双击 start.bat 启动，浏览器打开 http://127.0.0.1:$port" -ForegroundColor Green
Write-Host "想加自己的音色：把模型放进 models\gsv\<名字>\ 或 models\rvc\<名字>\，见 models\README.md"
