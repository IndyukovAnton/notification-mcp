$ErrorActionPreference = 'Stop'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if ($uvCommand) {
    $notificationUv = $uvCommand.Source
} else {
    $notificationUv = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
}
if (-not (Test-Path -LiteralPath $notificationUv)) {
    Write-Host 'Install uv first, then run this installer again:'
    Write-Host 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    exit 1
}
& $notificationUv tool install --python 3.11 --force $PSScriptRoot
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $notificationUv tool update-shell
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ''
Write-Host 'Installed. Open a NEW terminal and run:'
Write-Host '  notification-mcp setup --client codex'
Write-Host '  or, with TELEGRAM_BOT_TOKEN in the client environment:'
Write-Host '  notification-mcp connect codex --token-env'
Write-Host ''
Write-Host 'Then restart Codex, check /mcp, and send /start to your Telegram bot.'
