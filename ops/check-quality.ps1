# ops/check-quality.ps1
# Единая проверка Windows: pinned зависимости, Ruff и pytest; логи по умолчанию видны.
param([switch]$Live, [switch]$Browser)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
function Invoke-Checked {
    # Немедленно завершает проверку при ошибке внешнего инструмента.
    param([string]$Command, [string[]]$Arguments)
    Get-Command $Command -ErrorAction Stop | Out-Null
    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell при 2>&1 превращает обычный stderr uv в ErrorRecord.
        # Итог внешней команды определяет её exit code, вывод остаётся видимым.
        $ErrorActionPreference = 'Continue'
        & $Command @Arguments
        $commandExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($commandExitCode -ne 0) { throw "Проверка завершилась с кодом $commandExitCode : $Command" }
}
Invoke-Checked 'uv' @('sync', '--frozen')
if ($Browser) { Invoke-Checked 'uv' @('run', '--frozen', 'playwright', 'install', '--no-shell', 'chromium') }
Invoke-Checked 'uv' @('run', '--frozen', 'ruff', 'check', '.')
Invoke-Checked 'uv' @('run', '--frozen', 'ruff', 'format', '--check', '.')
Invoke-Checked 'git' @('diff', '--check')
# Полный прогон не требует pytest-кеша, который может принадлежать другому запуску/пользователю.
$pytestArguments = @('run', '--frozen', 'pytest', '-p', 'no:cacheprovider', '--cov=app', '--cov-report=term-missing')
if ($Live) { $pytestArguments += '--live' }
if ($Browser) { $pytestArguments += '--browser' }
Invoke-Checked 'uv' $pytestArguments
