<#
rename_nav_labels.ps1  (win-hud-arduino)

Переименовывает пункты навигации на всех веб-страницах проекта:
    Sensors      -> Main
    OLED screens -> Screens

Эти строки продублированы в HTML-навигации внутри пяти файлов (каждый
рисует свою страницу отдельным Flask-роутом):
    pc_hud.py         - страница /
    flash_webui.py    - страница /flash
    offline_webui.py  - страница /offline
    screens_webui.py  - страница /screens
    settings_webui.py - страница /settings

Ищет и заменяет ТОЧНО ">Sensors<" и ">OLED screens<" (с угловыми скобками) -
это защищает от случайной замены слова "Sensors"/"OLED screens", если оно
вдруг встретится где-то ещё (например в комментарии) - меняется только сам
текст ссылки в навигации.

Использование:
    powershell -ExecutionPolicy Bypass -File rename_nav_labels.ps1
    (или просто правой кнопкой -> "Выполнить с помощью PowerShell",
     если ExecutionPolicy уже разрешает локальные скрипты)

По умолчанию скрипт ищет файлы рядом с собой (в той же папке, где лежит
сам .ps1) - т.е. положи его в корень репозитория win-hud-arduino, рядом
с pc_hud.py. Можно указать другую папку явно:
    powershell -File rename_nav_labels.ps1 -RepoPath "C:\path\to\win-hud-arduino"

Перед изменением каждый файл копируется рядом как "<имя>.bak" (если .bak
уже существует - перезаписывается, чтобы повторный запуск не плодил
.bak.bak.bak). Файлы читаются/пишутся как UTF-8 БЕЗ BOM - так же, как они
лежат в репозитории (Python-исходники с кириллицей внутри) - использование
Set-Content -Encoding utf8 в Windows PowerShell 5.1 добавило бы BOM и могло
бы сломать git diff/сравнение, поэтому запись идёт через .NET
[System.IO.File]::WriteAllText с явным UTF8Encoding($false).
#>

param(
    [string]$RepoPath = $PSScriptRoot
)

$files = @(
    "pc_hud.py",
    "flash_webui.py",
    "offline_webui.py",
    "screens_webui.py",
    "settings_webui.py"
)

# Замены - именно с угловыми скобками, чтобы задеть только текст ссылки в
# навигации, а не слово "Sensors"/"OLED screens" где-то ещё в файле.
$replacements = @(
    @{ From = ">Sensors<";      To = ">Main<" },
    @{ From = ">OLED screens<"; To = ">Screens<" }
)

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$totalChanged = 0

foreach ($name in $files) {
    $path = Join-Path $RepoPath $name

    if (-not (Test-Path $path)) {
        Write-Warning "Файл не найден, пропускаю: $path"
        continue
    }

    $original = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8)
    $updated = $original
    $fileReplacements = 0

    foreach ($r in $replacements) {
        $count = ([regex]::Matches($updated, [regex]::Escape($r.From))).Count
        if ($count -gt 0) {
            $updated = $updated.Replace($r.From, $r.To)
            $fileReplacements += $count
        }
    }

    if ($fileReplacements -eq 0) {
        Write-Host "[$name] замен не найдено (уже переименовано или структура файла другая) - пропускаю запись" -ForegroundColor Yellow
        continue
    }

    $backupPath = "$path.bak"
    Copy-Item -Path $path -Destination $backupPath -Force

    [System.IO.File]::WriteAllText($path, $updated, $utf8NoBom)

    Write-Host "[$name] заменено вхождений: $fileReplacements (бэкап: $(Split-Path $backupPath -Leaf))" -ForegroundColor Green
    $totalChanged += $fileReplacements
}

Write-Host ""
Write-Host "Готово. Всего замен: $totalChanged" -ForegroundColor Cyan
Write-Host "Если что-то пошло не так - верни файлы из *.bak (переименуй, убрав .bak)."