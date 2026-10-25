$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path

if ($env:AUTOREG_PYTHON) {
    $py = $env:AUTOREG_PYTHON
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $py = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $py = "py"
} else {
    Write-Error "autoreg: python not found; set AUTOREG_PYTHON"
    exit 127
}

& $py "$dir\scripts\autoreg.py" @args
exit $LASTEXITCODE
