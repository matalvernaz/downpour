# Install or update the build in dist\Downpour for the current user.
#
#   powershell -ExecutionPolicy Bypass -File tools\install.ps1 [-Target <folder>]
#
# Copies the build to %LOCALAPPDATA%\Programs\Downpour (no administrator
# rights needed), adds a Start menu shortcut and registers that copy as the
# browsers' helper. Downpour's own data in %LOCALAPPDATA%\Downpour is not
# touched. Run build.cmd first.

param([string]$Target = (Join-Path $env:LOCALAPPDATA 'Programs\Downpour'))

$ErrorActionPreference = 'Stop'
$source = Join-Path (Split-Path -Parent $PSScriptRoot) 'dist\Downpour'
if (-not (Test-Path (Join-Path $source 'Downpour.exe'))) {
    throw "No build in $source. Run build.cmd first."
}

# Running copies hold their files open.
Get-Process -Name 'Downpour', 'downpour-cli' -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Output "Closing $($_.ProcessName) ($($_.Id))"
    Stop-Process -Id $_.Id -Force
    $_.WaitForExit(10000) | Out-Null
}

New-Item -ItemType Directory -Force -Path $Target | Out-Null
# Mirror the build, but never delete a portable copy's data folder.
robocopy $source $Target /MIR /XD data /XF portable.txt /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "Copying the build failed (robocopy exit code $LASTEXITCODE)" }
Write-Output "Installed to $Target"

$shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Downpour.lnk'
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = Join-Path $Target 'Downpour.exe'
$link.WorkingDirectory = $Target
$link.Description = 'Downpour download manager'
$link.Save()
Write-Output "Start menu shortcut: $shortcut"

& (Join-Path $Target 'downpour-cli.exe') helper install
if ($LASTEXITCODE -ne 0) { throw "Registering the browser helper failed" }
