[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $ServerArguments = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$exitCode = 1
$logPath = $null

try {
    try {
        $logsDirectory = Join-Path $PSScriptRoot 'logs'
        [void] (New-Item -ItemType Directory -Path $logsDirectory -Force)

        $timestamp = [DateTime]::UtcNow.ToString('yyyy-MM-dd_HH-mm-ss_UTC')
        $logPath = Join-Path $logsDirectory "run_$timestamp.log"
        $suffix = 1
        while (Test-Path -LiteralPath $logPath) {
            $logPath = Join-Path $logsDirectory "run_${timestamp}_$suffix.log"
            $suffix++
        }

        [IO.File]::WriteAllText($logPath, '', [Text.UTF8Encoding]::new($true))
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
import re, runpy, sys, threading, traceback

ansi = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
lock = threading.RLock()
log_path, script, *args = sys.argv[1:]
log = open(log_path, "a", encoding="utf-8", buffering=1)

class Tee:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        with lock:
            written = self.stream.write(text)
            self.stream.flush()
            log.write(ansi.sub("", text))
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
            & $uv.Path run python -c $pythonWrapper $logPath (Join-Path $PSScriptRoot 'main.py') @ServerArguments
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
