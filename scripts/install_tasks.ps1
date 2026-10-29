# Registra en el Programador de tareas de Windows (tu usuario, sin administrador):
#   1) Registro de reproducciones cada 2 horas   ->  python cron.py --record
#   2) Playlists completas una vez al día        ->  python cron.py
# Uso (una vez, desde la carpeta del proyecto):
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
# Para quitarlas:
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Remove

param(
    [switch]$Remove,
    [string]$Python = "python",
    [string]$DailyTime = "09:00",
    [int]$RecordEveryHours = 2
)

$ErrorActionPreference = "Stop"
$projectDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$names = @{
    Record = "autospoti - registro cada ${RecordEveryHours}h"
    Daily  = "autospoti - playlists diarias"
}

if ($Remove) {
    foreach ($n in $names.Values) {
        Unregister-ScheduledTask -TaskName $n -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Eliminada (si existía): $n"
    }
    exit 0
}

$pythonExe = (Get-Command $Python -ErrorAction Stop).Source
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

# 1) Registro: cada N horas, empezando ahora.
$recordTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Hours $RecordEveryHours)
$recordAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "cron.py --record" -WorkingDirectory $projectDir
Register-ScheduledTask -TaskName $names.Record -Action $recordAction -Trigger $recordTrigger `
    -Settings $settings -Principal $principal -Force | Out-Null

# 2) Playlists: una vez al día.
$dailyTrigger = New-ScheduledTaskTrigger -Daily -At $DailyTime
$dailyAction = New-ScheduledTaskAction -Execute $pythonExe -Argument "cron.py" -WorkingDirectory $projectDir
Register-ScheduledTask -TaskName $names.Daily -Action $dailyAction -Trigger $dailyTrigger `
    -Settings $settings -Principal $principal -Force | Out-Null

Write-Host "Tareas registradas:"
Write-Host " - $($names.Record): cada $RecordEveryHours h"
Write-Host " - $($names.Daily): cada día a las $DailyTime"
Write-Host "Revisa su estado con:  Get-ScheduledTask -TaskName 'autospoti*'"
Write-Host "Logs: Programador de tareas > Historial (o ejecútalas a mano desde 'Ejecutar')."
