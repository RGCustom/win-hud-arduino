pyinstaller --onedir --name win-hud-arduino --windowed --collect-all comtypes --collect-all pycaw --collect-all winsdk --collect-all pystray --hidden-import serial.tools.list_ports_windows --hidden-import win32timezone pc_hud.py

# ---- assets: копируем в dist\win-hud-arduino\_internal, чтобы не делать
# это вручную после каждой сборки. PyInstaller --onedir кладёт все
# зависимости (в т.ч. то, что приложение ищет рядом с собой при
# sys.frozen) в подпапку _internal, поэтому именно туда, а не в корень
# dist\win-hud-arduino, нужно класть assets\ (favicon.png/icon.png -
# см. _ensure_assets()/ASSETS_DIR в pc_hud.py).
$assetsSource = Join-Path $PSScriptRoot "assets"
$assetsDest = Join-Path $PSScriptRoot "dist\win-hud-arduino\_internal\assets"

if (Test-Path $assetsSource) {
    Write-Host "Копирую assets в $assetsDest ..."
    Copy-Item -Path $assetsSource -Destination $assetsDest -Recurse -Force
} else {
    Write-Warning "Папка assets не найдена рядом со скриптом ($assetsSource) - пропускаю копирование."
}
