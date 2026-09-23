$ErrorActionPreference = "Stop"
$BaseDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PasswordFile = Join-Path $BaseDir "root_password.dpapi"
$PythonScript = Join-Path $BaseDir "sokol_mass_audio.py"
$TaskLog = Join-Path $BaseDir "scheduled_task.log"

function Write-TaskLog {
    param([string]$Message)
    $Line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $TaskLog -Value $Line -Encoding ASCII
}

try {
    Write-TaskLog "START user=$([Security.Principal.WindowsIdentity]::GetCurrent().Name)"
    if (-not (Test-Path $PasswordFile)) {
        throw "Encrypted password file was not found: $PasswordFile"
    }
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw "Python launcher 'py' was not found."
    }

    Start-Sleep -Seconds 30

    # Read both old UTF-8-BOM files and new plain ASCII files safely.
    $Encrypted = [IO.File]::ReadAllText($PasswordFile, [Text.Encoding]::UTF8)
    $Encrypted = $Encrypted.Trim().TrimStart([char]0xFEFF)
    if ([string]::IsNullOrWhiteSpace($Encrypted)) {
        throw "Encrypted password file is empty."
    }
    $SecurePassword = ConvertTo-SecureString -String $Encrypted
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecurePassword)
    try {
        $env:SOKOL_ROOT_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
        & py -3 $PythonScript activate --password-env
        $PythonExitCode = $LASTEXITCODE
    } finally {
        Remove-Item Env:SOKOL_ROOT_PASSWORD -ErrorAction SilentlyContinue
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }

    if ($PythonExitCode -ne 0) {
        throw "Offline activation failed with exit code $PythonExitCode. Check results.csv."
    }
    Write-TaskLog "SUCCESS exit_code=0"
    exit 0
} catch {
    Write-TaskLog "ERROR $($_.Exception.Message)"
    exit 1
}
