# ops/check-quality.ps1
# Единая проверка Windows: pinned зависимости, Ruff и pytest; логи по умолчанию видны.
param([switch]$Live, [switch]$Browser)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
function Invoke-Checked {
    # Немедленно завершает проверку при ошибке внешнего инструмента.
    param([string]$Command, [string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Проверка завершилась с кодом $LASTEXITCODE : $Command" }
}
Invoke-Checked 'uv' @('sync', '--frozen')
if ($Browser) { Invoke-Checked 'uv' @('run', '--frozen', 'playwright', 'install', '--no-shell', 'chromium') }
Invoke-Checked 'uv' @('run', '--frozen', 'ruff', 'check', '.')
Invoke-Checked 'uv' @('run', '--frozen', 'ruff', 'format', '--check', '.')
Invoke-Checked 'git' @('diff', '--check')
$pytestArguments = @('run', '--frozen', 'pytest', '--cov=app', '--cov-report=term-missing')
if ($Live) { $pytestArguments += '--live' }
if ($Browser) { $pytestArguments += '--browser' }
Invoke-Checked 'uv' $pytestArguments
