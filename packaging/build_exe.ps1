$ErrorActionPreference = "Stop"

Set-Location -Path (Resolve-Path (Join-Path $PSScriptRoot ".."))

python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host "未检测到 PyInstaller。请先安装：" -ForegroundColor Yellow
  Write-Host "  python -m pip install pyinstaller" -ForegroundColor Yellow
  exit 1
}

pyinstaller --noconfirm --clean .\packaging\MapDigitizerPro.spec
if ($LASTEXITCODE -ne 0) {
  throw "PyInstaller 打包失败（exit code=$LASTEXITCODE）。请向上滚动查看具体报错。"
}

Write-Host "完成：dist\\MapDigitizerPro\\MapDigitizerPro.exe" -ForegroundColor Green
