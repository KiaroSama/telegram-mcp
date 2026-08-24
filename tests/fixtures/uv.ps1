param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

# The updater's Python test invocation: `uv run python -m pytest ...`. It exits
# with TELEGRAM_MCP_FAKE_PYTEST_EXIT (default 0) so a caller can tell "the tests
# ran and failed" from "the tests never ran".
if ($Arguments.Count -ge 4 -and
    $Arguments[0] -eq 'run' -and
    $Arguments[1] -eq 'python' -and
    $Arguments[2] -eq '-m' -and
    $Arguments[3] -eq 'pytest') {
    [Console]::Out.WriteLine('fake-pytest-output')
    $pytestExit = 0
    if (-not [string]::IsNullOrWhiteSpace($env:TELEGRAM_MCP_FAKE_PYTEST_EXIT)) {
        $pytestExit = [int] $env:TELEGRAM_MCP_FAKE_PYTEST_EXIT
    }
    exit $pytestExit
}

# `uv run python -c <wrapper> <log path> <max bytes> <main.py> [server args...]`.
if ($Arguments.Count -lt 7 -or
    $Arguments[0] -ne 'run' -or
    $Arguments[1] -ne 'python' -or
    $Arguments[2] -ne '-c' -or
    $Arguments[5] -notmatch '^\d+$' -or
    [IO.Path]::GetFileName($Arguments[6]) -ne 'main.py') {
    throw "Unexpected uv arguments: $($Arguments -join ' ')"
}

$logPath = $Arguments[4]
[Console]::Out.WriteLine('fake-normal-output')
[Console]::Error.WriteLine('fake-error-output')
[IO.File]::AppendAllText(
    $logPath,
    "fake-normal-output$([Environment]::NewLine)fake-error-output$([Environment]::NewLine)",
    [Text.UTF8Encoding]::new($false)
)
