# run.ps1 – Wrapper script that adds CUDA 12 DLL paths before running main.py
# Usage:
#   .\run.ps1                     (mở giao diện Gradio Web UI)
#   .\run.ps1 --ui                (mở giao diện Gradio Web UI)
#   .\run.ps1 --video video.mp4   (chạy CLI file local)
#   .\run.ps1 --url <youtube_url> (chạy CLI tải và xử lý YouTube)

$venv = "$PSScriptRoot\.venv\Lib\site-packages"

# Add all nvidia/*/bin directories to PATH so CUDA libraries can be loaded
Get-ChildItem "$venv\nvidia" -Directory -ErrorAction SilentlyContinue | ForEach-Object {
    $bin = Join-Path $_.FullName "bin"
    if (Test-Path $bin) {
        $env:PATH = "$bin;" + $env:PATH
    }
}

# Activate venv if not already active
if (-not $env:VIRTUAL_ENV) {
    & "$PSScriptRoot\.venv\Scripts\Activate.ps1"
}

# Run main.py with all arguments passed to this script
python "$PSScriptRoot\main.py" @args
