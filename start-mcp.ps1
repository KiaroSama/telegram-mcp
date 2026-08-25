[CmdletBinding()]
param(
    # Persist the server's DIAGNOSTIC output (stderr) to a file under the state
    # directory. Off by default, and never stdout: see the note below.
    [switch] $LogToFile,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArguments = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$exitCode = 1
$logPath = $null

# STDOUT IS THE MCP PROTOCOL CHANNEL.
#
# Under the stdio transport the server speaks JSON-RPC over stdout, and those
# frames carry complete tool results: message text, contact names, chat titles,
# files listed, whatever the client asked for. Teeing stdout wrote all of it to
# disk. Only stderr -- which the server uses for diagnostics, and which
# `telegram_mcp.safe_log` already bounds -- is ever persisted here, and only when
# the operator asks for it with -LogToFile or TELEGRAM_MCP_LAUNCHER_LOG.
$script:LogEnabled = $LogToFile.IsPresent -or
    ($env:TELEGRAM_MCP_LAUNCHER_LOG -in @('1', 'true', 'yes', 'on'))

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
        if (-not $script:LogEnabled) { throw 'not requested' }
        $logsDirectory = Join-Path (Get-StateDirectory) 'logs'
        [void] (New-Item -ItemType Directory -Path $logsDirectory -Force)
        [void] (Set-OwnerOnlyAcl -Path $logsDirectory)

        $timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
        $logPath = Join-Path $logsDirectory "start-mcp_$timestamp.log"
        # Bounded, and random once the obvious names are gone. The loop had no
        # ceiling: anything that keeps making the path exist - a directory left
        # under that name, a filter driver, a permission quirk - spun it forever,
        # and a launcher that never starts is worse than one that logs elsewhere.
        for ($suffix = 1; $suffix -le 20 -and (Test-Path -LiteralPath $logPath); $suffix++) {
            $logPath = Join-Path $logsDirectory "start-mcp_${timestamp}_$suffix.log"
        }
        if (Test-Path -LiteralPath $logPath) {
            $unique = [guid]::NewGuid().ToString('N').Substring(0, 8)
            $logPath = Join-Path $logsDirectory "start-mcp_${timestamp}_$unique.log"
        }

        [IO.File]::WriteAllText($logPath, '', [Text.UTF8Encoding]::new($true))
        [void] (Set-OwnerOnlyAcl -Path $logPath)
        # `.log` AND `.log.1`: the Python tee rolls a full log to `<name>.log.1`,
        # and a filter ending in `.log` never matched those, so every rotated part
        # survived every prune and the retention count bounded half the files.
        Remove-StaleFiles -Directory $logsDirectory -Filter 'start-mcp_*.log' -Keep $script:LogRetention
        Remove-StaleFiles -Directory $logsDirectory -Filter 'start-mcp_*.log.1' -Keep $script:LogRetention
    }
    catch {
        $logPath = $null
        if ($script:LogEnabled) {
            Write-Warning "File logging is unavailable: $($_.Exception.Message)"
        }
    }

    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        throw "uv was not found in PATH. Install uv, then run this launcher again."
    }

    $logDisplay = if ($logPath) { $logPath } else { 'unavailable' }
    $startMessage = "[$([DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss UTC'))] [INFO] [launcher] Starting: uv run main.py; log=$logDisplay"
    # stderr, not Write-Host: the host stream is stdout once redirected, and the
    # client reads this process's stdout as the MCP protocol.
    [Console]::Error.WriteLine($startMessage)
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$startMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }

    Push-Location -LiteralPath $PSScriptRoot
    try {
        if ($logPath) {
            $pythonWrapper = @'
import os, re, runpy, subprocess, sys, threading, traceback

# STDERR ONLY. On the stdio transport stdout carries the MCP protocol -- complete
# tool results, i.e. the user's own messages, contacts and files -- so it is
# passed straight through and never written to disk. stderr is diagnostics, which
# telegram_mcp.safe_log already bounds.
ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
lock = threading.RLock()
log_path, max_bytes, script, *args = sys.argv[1:]
max_bytes = int(max_bytes)

def secure(path):
    # Owner-only, on every file this wrapper creates -- the first one and every
    # one a rollover makes. os.chmod is not that guarantee on Windows: it toggles
    # the read-only attribute and cannot clear the read bit, so icacls is the
    # only owner-only mechanism available there without a dependency.
    # `/inheritance:r` is the half that matters: `/grant` alone ADDS an entry and
    # leaves the inherited BUILTIN\Users one in place.
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return
    user = os.environ.get("USERNAME")
    if not user:
        return
    domain = os.environ.get("USERDOMAIN")
    principal = (domain + "\\" + user) if domain else user
    try:
        subprocess.run(
            ["icacls", path, "/inheritance:r", "/grant:r", principal + ":(F)"],
            capture_output=True, timeout=10, check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

log = open(log_path, "a", encoding="utf-8", buffering=1)
secure(log_path)

def roll():
    # One previous part, not an archive: a server that logs for days must not be
    # able to fill the disk, and the interesting end of a wedged run is the tail.
    global log
    log.close()
    try:
        os.replace(log_path, log_path + ".1")
    except OSError:
        pass
    secure(log_path + ".1")
    # newline='' disables Windows newline translation. Without it a written line
    # ending becomes two bytes on disk while the cap counts one, so a ceiling
    # computed in bytes is wrong by the number of lines written - and the file
    # ends up over a limit the code believes it enforced.
    log = open(log_path, "a", encoding="utf-8", buffering=1, newline="")
    secure(log_path)

class Tee:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        with lock:
            written = self.stream.write(text)
            self.stream.flush()
            cleaned = ansi.sub('', text)
            if max_bytes:
                # Roll BEFORE writing when this chunk would cross the ceiling. The
                # old order wrote and measured afterwards, so the cap only ever
                # applied to the write AFTER the one that crossed it.
                if log.tell() + len(cleaned.encode('utf-8')) > max_bytes:
                    roll()
                # And a single write larger than the whole ceiling cannot be
                # bounded by rolling - the file would still hold it. Truncated,
                # and said so, because a silently shortened diagnostic is worse
                # than a short one.
                encoded = cleaned.encode('utf-8')
                if len(encoded) > max_bytes:
                    marker = '... [truncated at the log size ceiling]' + chr(10)
                    keep = max(0, max_bytes - len(marker.encode('utf-8')))
                    cleaned = encoded[:keep].decode('utf-8', 'ignore') + marker
            log.write(cleaned)
        return written

    def flush(self):
        with lock:
            self.stream.flush()
            log.flush()

    def isatty(self):
        return self.stream.isatty()

    def __getattr__(self, name):
        return getattr(self.stream, name)

stderr = sys.stderr
sys.stderr = Tee(stderr)
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
except BaseException as exc:
    # Two destinations, deliberately different. A traceback carries the
    # arguments and repr of every frame, which here means chat titles,
    # usernames, phone numbers and paths - and the log file is the thing an
    # operator attaches to a bug report. So the full traceback goes straight to
    # the terminal, bypassing the tee, and only its shape is persisted.
    traceback.print_exception(exc, file=stderr)
    stderr.flush()
    frames = traceback.extract_tb(exc.__traceback__)
    where = f'{frames[-1].filename}:{frames[-1].lineno}' if frames else 'unknown'
    print(f'{type(exc).__name__} at {where}', file=sys.stderr)
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
    [Console]::Error.WriteLine($stopMessage)
    if ($logPath) {
        [IO.File]::AppendAllText($logPath, "$stopMessage$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
    }
}

exit $exitCode
