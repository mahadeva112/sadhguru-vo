# Creates "Sadhguru VO.lnk" next to this script (and optionally on the Desktop).
#
# The shortcut targets pythonw.exe directly rather than the .bat, because:
#   * Windows refuses to pin a .bat to the taskbar, but pins .lnk files happily
#   * pythonw.exe starts the GUI with no console window at all
#   * a .lnk carries its own IconLocation, so the custom .ico is what shows up
#
# Run it any time - it overwrites the existing shortcut in place.

$ErrorActionPreference = 'Stop'

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$icon   = Join-Path $root 'assets\sadhguru_vo.ico'
$appPy  = Join-Path $root 'app.py'
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
$python  = Join-Path $root '.venv\Scripts\python.exe'
$lnk    = Join-Path $root 'Sadhguru VO.lnk'

# Prefer the venv's pythonw; fall back to the venv python, then to whatever
# pythonw is on PATH so the shortcut still works before setup has been run.
$target = $null
foreach ($cand in @($pythonw, $python)) {
    if (Test-Path $cand) { $target = $cand; break }
}
if (-not $target) {
    $onPath = (Get-Command pythonw -ErrorAction SilentlyContinue)
    if ($onPath) { $target = $onPath.Source }
}
if (-not $target) {
    Write-Warning "No Python found. Run setup_windows.bat first, then re-run this script."
    exit 1
}
if (-not (Test-Path $appPy)) {
    Write-Error "app.py not found next to this script - is the folder complete?"
    exit 1
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnk)
$sc.TargetPath       = $target
$sc.Arguments        = '"' + $appPy + '"'
$sc.WorkingDirectory = $root
$sc.Description      = 'Sadhguru VO - two-step ElevenLabs voice-over pipeline'
$sc.WindowStyle      = 1
if (Test-Path $icon) {
    $sc.IconLocation = "$icon,0"
} else {
    Write-Warning "assets\sadhguru_vo.ico is missing - the shortcut will use the Python icon."
}
$sc.Save()

Write-Host "Created: $lnk"
Write-Host "  target : $target"
Write-Host "  args   : $($sc.Arguments)"
Write-Host ""
Write-Host "To pin it: right-click 'Sadhguru VO.lnk' -> Show more options -> Pin to taskbar."
Write-Host "(On Windows 11 you can also drag the .lnk onto the taskbar.)"

# Offer a Desktop copy too - handy, and pinning from the Desktop is the path
# most people already know.
$desktop = [Environment]::GetFolderPath('Desktop')
if ($desktop -and (Test-Path $desktop)) {
    try {
        Copy-Item $lnk (Join-Path $desktop 'Sadhguru VO.lnk') -Force
        Write-Host "Also copied to the Desktop."
    } catch {
        Write-Warning "Could not copy to the Desktop: $($_.Exception.Message)"
    }
}
