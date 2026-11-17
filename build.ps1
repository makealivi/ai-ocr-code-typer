$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    throw '请先创建 .venv 并安装 requirements.txt 和 pyinstaller。'
}
$taskData = Join-Path $PSScriptRoot 'release\启动\data'
$taskBackup = Join-Path $PSScriptRoot ('build\data-backup-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $taskData) {
    Copy-Item -LiteralPath $taskData -Destination $taskBackup -Recurse
}
& $taskPython -m PyInstaller --noconfirm --windowed --disable-windowed-traceback --onedir --name 启动 --distpath release --workpath build --add-data 'web;web' --add-data 'assets;assets' --collect-all rapidocr_onnxruntime --collect-binaries onnxruntime --collect-data onnxruntime --collect-binaries pyclipper --collect-binaries shapely --exclude-module matplotlib --exclude-module pandas --exclude-module scipy --exclude-module torch --exclude-module tensorflow --exclude-module IPython --exclude-module pytest main.py
if ($LASTEXITCODE -ne 0) { throw '打包失败，请查看构建输出。' }
if (Test-Path -LiteralPath $taskBackup) {
    Copy-Item -LiteralPath $taskBackup -Destination $taskData -Recurse
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $PSScriptRoot 'release\启动\使用说明.md')
Write-Output '启动文件：release\启动\启动.exe'
