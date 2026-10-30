[CmdletBinding()]
param(
    [ValidateSet('Setup', 'Run', 'Check', 'Release')]
    [string]$Action = 'Setup',
    [string]$Python = '',
    [string]$Output = ''
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$projectRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "命令执行失败（退出码 $LASTEXITCODE）：$Program" }
}

function Assert-Python {
    param([string]$Program, [string[]]$Prefix = @())
    $check = "import sys,struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3,12) and sys.version_info >= (3,12,1) and struct.calcsize('P') == 8, '需要 Windows x64 CPython 3.12.1 或更高的 3.12.x 版本'; print(sys.executable); print(sys.version)"
    Invoke-Checked -Program $Program -Arguments ($Prefix + @('-c', $check))
}

try {
    Push-Location -LiteralPath $projectRoot
    if ($Action -eq 'Setup' -or -not (Test-Path -LiteralPath $venvPython)) {
        if (-not (Test-Path -LiteralPath $venvPython)) {
            $pythonPrefix = @()
            if ($Python) { $basePython = $Python }
            elseif (Get-Command py -ErrorAction SilentlyContinue) {
                $basePython = 'py'
                $pythonPrefix = @('-3.12')
            }
            elseif (Get-Command python -ErrorAction SilentlyContinue) { $basePython = 'python' }
            else { throw '未找到 Python。请安装 Windows x64 CPython 3.12（最低 3.12.1），然后重新运行 setup.cmd：https://www.python.org/downloads/windows/' }
            Assert-Python -Program $basePython -Prefix $pythonPrefix
            Invoke-Checked -Program $basePython -Arguments ($pythonPrefix + @('-m', 'venv', (Join-Path $projectRoot '.venv')))
        }
        Assert-Python -Program $venvPython
        Invoke-Checked -Program $venvPython -Arguments @('-m', 'pip', 'install', '-r', (Join-Path $projectRoot 'requirements.txt'))
        Invoke-Checked -Program $venvPython -Arguments @('-m', 'pip', 'check')
        Write-Host '环境准备完成。运行 start.cmd 启动程序；运行 release.cmd 检查并构建发布包。'
    }
    else { Assert-Python -Program $venvPython }

    if ($Action -eq 'Run') {
        Invoke-Checked -Program $venvPython -Arguments @('-X', 'utf8', '-B', (Join-Path $projectRoot 'pyeyedata_launcher.py'))
    }
    elseif ($Action -in @('Check', 'Release')) {
        $scriptName = if ($Action -eq 'Check') { 'run_checks.py' } else { 'release_windows.py' }
        $scriptArgs = @('-X', 'utf8', '-B', (Join-Path $PSScriptRoot $scriptName))
        if ($Output) { $scriptArgs += @('--output', $Output) }
        Invoke-Checked -Program $venvPython -Arguments $scriptArgs
    }
}
catch {
    Write-Error -Message $_ -ErrorAction Continue
    exit 1
}
finally { Pop-Location }
