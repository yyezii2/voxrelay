param(
    [switch]$RegisterDsh,
    [switch]$UnregisterDsh,
    [string]$DshProfile
)

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

# ---------------------------------------------------------------- DSH 面板
# 服务能跑 != 面板能出现：DSH 的插件清单是独立的一份，得单独写一笔。
if ($RegisterDsh -or $UnregisterDsh) {
    Write-Host ""
    $action = if ($RegisterDsh) { "register" } else { "unregister" }
    $dshArgs = @("dsh_link.py", $action)
    if ($DshProfile) { $dshArgs += @("--profile", $DshProfile) }

    Write-Host ("DSH 插件：{0}" -f $action) -ForegroundColor Cyan
    # 直接调，不接管编码：python 连控制台时走 Windows 控制台 API，
    # 中文本来就正常；硬改 UTF-8 反而会在 936 代码页的终端里变乱码。
    & $vpy @dshArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Host "DSH 登记失败，看上面的提示；服务本身已经装好了。" -ForegroundColor Yellow
        exit $LASTEXITCODE
    }
}

Write-Host ""
Write-Host "完成。双击 start.bat 启动，浏览器打开 http://127.0.0.1:$port" -ForegroundColor Green
Write-Host "想加自己的音色：把模型放进 models\gsv\<名字>\ 或 models\rvc\<名字>\，见 models\README.md"
if (-not ($RegisterDsh -or $UnregisterDsh)) {
    Write-Host ""
    Write-Host "想在 DSH 侧边栏用面板：再跑一次 install.ps1 -RegisterDsh，然后重启 DSH。"
}
