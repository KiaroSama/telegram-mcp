param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $Arguments
)

if ($Arguments.Count -lt 6 -or
    $Arguments[0] -ne 'run' -or
    $Arguments[1] -ne 'python' -or
    $Arguments[2] -ne '-c' -or
    [IO.Path]::GetFileName($Arguments[5]) -ne 'main.py') {
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
