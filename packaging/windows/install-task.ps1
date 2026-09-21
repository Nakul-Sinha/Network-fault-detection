<#
.SYNOPSIS
    Install NetPulse Local so it starts when you log in.

.DESCRIPTION
    Registers a scheduled task under the current user rather than a Windows
    service, and the choice is deliberate. A service runs as SYSTEM in
    session 0, where there is no Wi-Fi association to read, no user session
    to send a toast to, and a different view of the network from the one the
    user's applications have. A logon task runs as the user, sees what they
    see, and needs no administrator rights to install.

    Nothing here requires elevation. If you are being prompted for it,
    something is wrong.

.PARAMETER Uninstall
    Remove the task. Stored data is left alone.

.PARAMETER Purge
    With -Uninstall, also delete every observation the agent has stored.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-task.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-task.ps1 -Uninstall -Purge
#>

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Purge
)

$ErrorActionPreference = 'Stop'
$TaskName = 'NetPulse Local'

function Get-NetPulseCommand {
    $command = Get-Command netpulse.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $python) {
        throw 'Neither netpulse nor python is on PATH. Install with: python -m pip install --user netpulse-local'
    }
    return $python.Source
}

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the '$TaskName' scheduled task."
    }
    else {
        Write-Host "No '$TaskName' task was registered."
    }

    if ($Purge) {
        try { & netpulse wipe --yes } catch { }
        $data = Join-Path $env:LOCALAPPDATA 'NetPulse'
        $config = Join-Path $env:APPDATA 'NetPulse'
        foreach ($path in @($data, $config)) {
            if (Test-Path $path) { Remove-Item -Recurse -Force $path }
        }
        Write-Host 'All stored data deleted.'
    }
    else {
        Write-Host 'Stored data kept. Re-run with -Purge to delete it too.'
    }
    return
}

$exe = Get-NetPulseCommand
if ($exe -like '*python.exe') {
    $arguments = '-m netpulse run'
}
else {
    $arguments = 'run'
}

Write-Host 'Checking what this machine can measure...'
if ($exe -like '*python.exe') { & $exe -m netpulse doctor } else { & $exe doctor }

$action = New-ScheduledTaskAction -Execute $exe -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Idle priority, no battery restrictions removed: the agent is meant to be
# unnoticeable, and PRD N4 already has it back off on battery itself.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -Priority 7

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description 'Predicts network trouble minutes ahead and explains which layer is failing. Runs locally; sends nothing off this machine.' `
    -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ''
Write-Host "NetPulse Local will start at every logon, and is running now."
Write-Host '  health      netpulse status'
Write-Host '  web UI      http://127.0.0.1:8787/'
Write-Host '  stop        Stop-ScheduledTask -TaskName ''NetPulse Local'''
Write-Host '  remove      install-task.ps1 -Uninstall'
