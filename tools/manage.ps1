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
    if ($LASTEXITCODE -ne 0) { throw "$Program failed (exit $LASTEXITCODE)." }
}

function Assert-Python {
    param([string]$Program, [string[]]$Prefix = @())
    $check = "import sys,struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3,12) and sys.version_info >= (3,12,1) and struct.calcsize('P') == 8, 'Requires Windows x64 CPython 3.12.1+ (3.12 series)'; print(sys.executable); print(sys.version)"
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
            else { throw 'Install Windows x64 Python 3.12 from https://www.python.org/downloads/windows/ then rerun setup.cmd.' }
            Assert-Python -Program $basePython -Prefix $pythonPrefix
            Invoke-Checked -Program $basePython -Arguments ($pythonPrefix + @('-m', 'venv', (Join-Path $projectRoot '.venv')))
        }
        Assert-Python -Program $venvPython
        Invoke-Checked -Program $venvPython -Arguments @('-m', 'pip', 'install', '-r', (Join-Path $projectRoot 'requirements.txt'))
        Invoke-Checked -Program $venvPython -Arguments @('-m', 'pip', 'check')
        Write-Host 'Environment ready. Run start.cmd to launch; release.cmd to build a release.'
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
