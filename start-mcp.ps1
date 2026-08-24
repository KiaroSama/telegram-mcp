[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArguments = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$exitCode = 1
$logPath = $null

# A server run can log for days, so the file needs a ceiling as well as a count:
# the tee below rolls at LogMaxBytes keeping one previous part, and Remove-StaleFiles
# keeps the newest LogRetention runs.
$script:LogRetention = 10
$script:LogMaxBytes = 5MB

function Get-StateDirectory {
    <#
      Where runtime state goes: NOT beside the source. `logs/` next to the script
      lands inside the git checkout, gets committed or synced, and inherits
      whatever the repository directory grants. Same rule as
      `telegram_mcp.aliases.aliases_file_path`, so there is one place to lock down.
    #>
    $base = if ($env:XDG_STATE_HOME) { $env:XDG_STATE_HOME } else { Join-Path $HOME '.local/state' }
    return Join-Path $base 'telegram-mcp'
}

function Set-OwnerOnlyAcl {
    <#
      Leave exactly one access entry on a private file. `/inheritance:r` is the
      half that matters: `/grant` alone ADDS an entry and leaves the inherited
      BUILTIN\Users one in place. Mirrors `telegram_mcp.aliases.restrict_to_owner`.
    #>
    param([Parameter(Mandatory)] [string] $Path)
    $principal = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
    if (-not $principal) { return $false }
    try {
        & icacls $Path '/inheritance:r' '/grant:r' "${principal}:(F)" *> $null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Remove-StaleFiles {
    param(
        [Parameter(Mandatory)] [string] $Directory,
        [Parameter(Mandatory)] [string] $Filter,
        [Parameter(Mandatory)] [int] $Keep
    )
    # Named by a UTC timestamp, so the name order IS the age order.
    $files = @(
        Get-ChildItem -LiteralPath $Directory -Filter $Filter -File -Force -ErrorAction SilentlyContinue |
            Sort-Object Name
    )
    for ($index = 0; $index -lt $files.Count - $Keep; $index++) {
        Remove-Item -LiteralPath $files[$index].FullName -Force -ErrorAction SilentlyContinue
    }
}

try {
    try {
        $logsDirectory = Join-Path (Get-StateDirectory) 'logs'
        [void] (New-Item -ItemType Directory -Path $logsDirectory -Force)

        $timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
        $logPath = Join-Path $logsDirectory "start-mcp_$timestamp.log"
        $suffix = 1
        while (Test-Path -LiteralPath $logPath) {
            $logPath = Join-Path $logsDirectory "start-mcp_${timestamp}_$suffix.log"
            $suffix++
        }

        [IO.File]::WriteAllText($logPath, '', [Text.UTF8Encoding]::new($true))
        [void] (Set-OwnerOnlyAcl -Path $logPath)
        Remove-StaleFiles -Directory $logsDirectory -Filter 'start-mcp_*.log' -Keep $script:LogRetention
    }
    catch {
        Write-Warning "File logging is unavailable: $($_.Exception.Message)"
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv was not found in PATH. Install uv, then run this launcher again."
    }

    $logDisplay = if ($logPath) { $logPath } else { 'unavailable' }
    $startMessage = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [INFO] [launcher] Starting: uv run main.py; log=$logDisplay"
    Write-Host $startMessage
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$startMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        if ($logPath) {
            $pythonWrapper = @'
import os, re, runpy, sys, threading, traceback

ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
lock = threading.RLock()
log_path, max_bytes, script, *args = sys.argv[1:]
max_bytes = int(max_bytes)
log = open(log_path, "a", encoding="utf-8", buffering=1)

def roll():
    # One previous part, not an archive: a server that logs for days must not be
    # able to fill the disk, and the interesting end of a wedged run is the tail.
    global log
    log.close()
    try:
        os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    log = open(log_path, "a", encoding="utf-8", buffering=1)

class Tee:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        with lock:
            written = self.stream.write(text)
            self.stream.flush()
            log.write(ansi.sub("", text))
            if max_bytes and log.tell() > max_bytes:
                roll()
        return written

    def flush(self):
        with lock:
            self.stream.flush()
            log.flush()

    def isatty(self):
        return self.stream.isatty()

    def __getattr__(self, name):
        return getattr(self.stream, name)

stdout, stderr = sys.stdout, sys.stderr
sys.stdout, sys.stderr = Tee(stdout), Tee(stderr)
sys.argv = [script, *args]
exit_code = 0
try:
    runpy.run_path(script, run_name="__main__")
except KeyboardInterrupt:
    pass
except SystemExit as exc:
    exit_code = exc.code if isinstance(exc.code, int) else 1
    if exc.code is not None and not isinstance(exc.code, int):
        print(exc.code, file=sys.stderr)
except BaseException:
    traceback.print_exc()
    exit_code = 1
finally:
    sys.stdout.flush()
    sys.stderr.flush()
if exit_code:
    raise SystemExit(exit_code)
'@
            & $uv.Path run python -c $pythonWrapper $logPath $script:LogMaxBytes `
                (Join-Path $PSScriptRoot 'main.py') @ServerArguments
        }
        else {
            & $uv.Path run main.py @ServerArguments
        }
        $nativeExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction SilentlyContinue
        $exitCode = if ($null -eq $nativeExitCode) { 0 } else { [int] $nativeExitCode }
    }
    finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        Write-Error "uv exited with code $exitCode." -ErrorAction Continue
    }
}
catch {
    $errorMessage = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [ERROR] [launcher] $($_.Exception.Message)"
    Write-Error $errorMessage -ErrorAction Continue
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$errorMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }
}
finally {
    $stopMessage = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [INFO] [launcher] Stopped with exit code $exitCode."
    Write-Host $stopMessage
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$stopMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }
}

exit $exitCode
