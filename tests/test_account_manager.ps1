# Exercise the account manager's .env editing against a throwaway copy.
#
# The real .env is never touched: the script is copied into a temp directory with
# a fake .env beside it, and every assertion runs there. The menu itself is
# interactive, so this drives the functions directly by dot-sourcing the parts
# that do the work.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$projectRoot = Split-Path -Parent $PSScriptRoot
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ("tg-accounts-" + [guid]::NewGuid())
[void] (New-Item -ItemType Directory -Path $sandbox)

try {
    Copy-Item -LiteralPath (Join-Path $projectRoot 'Manage-Accounts.ps1') -Destination $sandbox

    $envPath = Join-Path $sandbox '.env'
    $original = @(
        '# a comment that must survive'
        'TELEGRAM_API_ID=12345'
        'TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef'
        ''
        'TELEGRAM_SESSION_STRING=1AAAAAoriginal'
        'TELEGRAM_SESSION_STRING_WORK=1AAAAAwork'
        'SOME_OTHER_KEY=keep me'
    )
    [IO.File]::WriteAllLines($envPath, $original, [Text.UTF8Encoding]::new($false))

    # Load only the function definitions: run the file up to the point it starts
    # the menu, by extracting the function blocks.
    $source = Get-Content -LiteralPath (Join-Path $sandbox 'Manage-Accounts.ps1') -Raw
    $PSScriptRoot_shim = $sandbox
    $functions = [regex]::Matches($source, '(?ms)^function [\w-]+ \{.*?^\}')
    if ($functions.Count -lt 6) { throw "Expected the function blocks, found $($functions.Count)." }
    $harness = @"
`$PSScriptRoot = '$sandbox'
`$envPath = Join-Path `$PSScriptRoot '.env'
`$script:LogPath = `$null
$($functions.Value -join "`n`n")
"@
    . ([ScriptBlock]::Create($harness))

    # --- discovery ---------------------------------------------------------
    $accounts = Get-Accounts
    if ($accounts.Count -ne 2) { throw "Expected 2 accounts, found $($accounts.Count): $($accounts.Keys -join ', ')" }
    if (-not $accounts.Contains('default')) { throw 'The unsuffixed session was not labelled default.' }
    if (-not $accounts.Contains('work')) { throw 'The suffixed session was not found.' }
    Write-Host 'ok  discovers both the unsuffixed default and a suffixed label'

    # --- add ---------------------------------------------------------------
    $backup = Backup-EnvFile
    if (-not (Test-Path -LiteralPath $backup)) { throw 'Backup-EnvFile returned a path that does not exist.' }
    Set-EnvValue -Key 'TELEGRAM_SESSION_STRING_PERSONAL' -Value '1AAAAApersonal'
    $after = Get-Accounts
    if ($after.Count -ne 3) { throw "Add did not take: $($after.Keys -join ', ')" }
    Write-Host 'ok  adding a label writes a new key'

    # --- everything else survives -------------------------------------------
    $text = [IO.File]::ReadAllText($envPath)
    foreach ($expected in '# a comment that must survive', 'TELEGRAM_API_ID=12345', 'SOME_OTHER_KEY=keep me') {
        if ($text -notmatch [regex]::Escape($expected)) { throw "Rewriting .env lost: $expected" }
    }
    Write-Host 'ok  comments and unrelated keys survive a rewrite'

    # --- replace in place, not append ---------------------------------------
    Set-EnvValue -Key 'TELEGRAM_SESSION_STRING_WORK' -Value '1AAAAAwork-replaced'
    $workLines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_ -match '^TELEGRAM_SESSION_STRING_WORK=' })
    if ($workLines.Count -ne 1) { throw "Replacing a key appended a duplicate ($($workLines.Count) lines)." }
    if ($workLines[0] -notmatch 'replaced$') { throw 'The replacement value was not written.' }
    Write-Host 'ok  replacing a label edits its line instead of appending a second'

    # --- remove --------------------------------------------------------------
    Remove-EnvKey -Key 'TELEGRAM_SESSION_STRING_WORK'
    $afterRemove = Get-Accounts
    if ($afterRemove.Contains('work')) { throw 'Remove did not take.' }
    if (-not $afterRemove.Contains('default') -or -not $afterRemove.Contains('personal')) {
        throw "Remove took the wrong accounts with it: $($afterRemove.Keys -join ', ')"
    }
    Write-Host 'ok  removing one label leaves the others alone'

    # --- the backup is a real restore point ----------------------------------
    Copy-Item -LiteralPath $backup -Destination $envPath -Force
    $restored = Get-Accounts
    if ($restored.Count -ne 2 -or -not $restored.Contains('work')) {
        throw "Restoring the backup did not bring the file back: $($restored.Keys -join ', ')"
    }
    Write-Host 'ok  the backup restores the file by a plain copy'

    # --- a commented-out account is not an account ---------------------------
    [IO.File]::WriteAllLines($envPath, @('#TELEGRAM_SESSION_STRING_OLD=1AAAAAold', 'TELEGRAM_SESSION_STRING=1AAAAAx'),
        [Text.UTF8Encoding]::new($false))
    $commented = Get-Accounts
    if ($commented.Contains('old')) { throw 'A commented-out line was read as a configured account.' }
    Write-Host 'ok  a commented-out session line is ignored'

    Write-Host ''
    Write-Host 'Account manager checks passed.' -ForegroundColor Green
}
finally {
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
