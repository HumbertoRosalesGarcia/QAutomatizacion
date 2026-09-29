param(
    [Parameter(ValueFromRemainingArguments=$true)]
    $argsList
)
$launcherPath = Join-Path $PSScriptRoot "launcher.py"
python $launcherPath @argsList
