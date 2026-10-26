param([Parameter(Mandatory = $true)][string]$PythonwPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$python = (Resolve-Path -LiteralPath $PythonwPath -ErrorAction Stop).ProviderPath
$watcher = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot 'codex_autostart.py') -ErrorAction Stop).ProviderPath
if ([IO.Path]::GetFileName($python) -ne 'pythonw.exe') { throw 'Expected the windowed Python runtime' }
$command = '"' + $python + '" -B -X utf8 "' + $watcher + '" --watch'
$startup = New-CimInstance -CimClass (Get-CimClass -ClassName Win32_ProcessStartup) -ClientOnly -Property @{ShowWindow = [uint16]0}
# WMI creates the process outside the caller's job and process tree.
$created = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
    CommandLine = $command
    CurrentDirectory = $PSScriptRoot
    ProcessStartupInformation = $startup
}
if ($created.ReturnValue -ne 0) { throw ('Windows could not start the watcher: ' + $created.ReturnValue) }
[pscustomobject]@{watcher_process_id = $created.ProcessId; launcher = 'windows-wmi'} | ConvertTo-Json -Compress
