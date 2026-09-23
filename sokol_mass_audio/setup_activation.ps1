$ErrorActionPreference = "Stop"
$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "Sokol rev5 Offline Activation"
$PasswordFile = Join-Path $BaseDir "root_password.dpapi"
$ActivateScript = Join-Path $BaseDir "activate_offline.ps1"
$PythonScript = Join-Path $BaseDir "sokol_mass_audio.py"
$StateFile = Join-Path $BaseDir "deployment_state.json"

$Tomorrow = (Get-Date).Date.AddDays(1).ToString("dd.MM.yyyy")
$DateText = Read-Host "Activation date (default $Tomorrow)"
if ([string]::IsNullOrWhiteSpace($DateText)) { $DateText = $Tomorrow }
$TimeText = Read-Host "Activation time (default 08:00)"
if ([string]::IsNullOrWhiteSpace($TimeText)) { $TimeText = "08:00" }

try {
    $ActivateAt = [datetime]::ParseExact(
        "$DateText $TimeText",
        "dd.MM.yyyy HH:mm",
        [Globalization.CultureInfo]::InvariantCulture
    )
} catch {
    Write-Host "Invalid date or time. Example: 03.09.2026 and 08:00" -ForegroundColor Red
    exit 2
}

if ($ActivateAt -le (Get-Date)) {
    Write-Host "The selected activation time has already passed." -ForegroundColor Red
    exit 2
}

if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
    Write-Host "Python launcher 'py' was not found." -ForegroundColor Red
    exit 3
}

$SecurePassword = Read-Host "Common root password" -AsSecureString
$EncryptedPassword = $SecurePassword | ConvertFrom-SecureString
[IO.File]::WriteAllText($PasswordFile, $EncryptedPassword, [Text.Encoding]::ASCII)

$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
try {
    $env:SOKOL_ROOT_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    Write-Host "Preparing panels and uploading 9.wav. Panels remain Online for now..." -ForegroundColor Cyan
    & py -3 $PythonScript prepare --password-env
    $PrepareCode = $LASTEXITCODE
} finally {
    Remove-Item Env:SOKOL_ROOT_PASSWORD -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}

if (-not (Test-Path $StateFile)) {
    Write-Host "Preparation state file was not created. Task will not be scheduled." -ForegroundColor Red
    exit 4
}
$PreparedCount = ((Get-Content -Path $StateFile -Raw | ConvertFrom-Json).panels.PSObject.Properties).Count
if ($PreparedCount -eq 0) {
    Write-Host "No panels were prepared. Task will not be scheduled." -ForegroundColor Red
    exit 5
}
if ($PrepareCode -ne 0) {
    Write-Host "Some panels failed preparation. Only $PreparedCount prepared panels will be activated." -ForegroundColor Yellow
    Write-Host "See results.csv for details." -ForegroundColor Yellow
}

$ActionArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ActivateScript`""
$Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $ActionArguments
$Trigger = New-ScheduledTaskTrigger -Once -At $ActivateAt
$Settings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$UserId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $Principal `
        -Description "Switch prepared Sokol rev.5 panels to Offline mode" `
        -Force | Out-Null
} catch {
    Write-Host "Failed to create the Windows task. Run prepare_and_schedule.bat as Administrator." -ForegroundColor Red
    throw
}

Write-Host "Task created: $TaskName" -ForegroundColor Green
Write-Host "Activation: $($ActivateAt.ToString('dd.MM.yyyy HH:mm'))" -ForegroundColor Green
Write-Host "Prepared panels: $PreparedCount" -ForegroundColor Green
Write-Host "This window can now be closed. Do not shut down or sign out of Windows." -ForegroundColor Green

