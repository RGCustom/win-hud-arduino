#!/usr/bin/env python3
"""
pc_hud.py  (win-hud-arduino)

Главный скрипт - аналог shkaf_stats_bridge.py, но под Windows-хост и с двумя
принципиальными отличиями от shkaf-hud:

  1. Serial - ДВУСТОРОННИЙ. Плата не только принимает BAR/BRI/CON/L1-3, но и
     сама шлёт события энкодера (ENC:/BTN:, см. protocol.parse_incoming_line).
     Вместо отдельного потока-читателя главный цикл крутится с маленьким
     тиком (TICK_INTERVAL, по умолчанию 100мс) и на каждой итерации сначала
     неблокирующе вычитывает всё, что накопилось в порту, потом (не на
     каждой итерации, а раз в POLL_INTERVAL) пересчитывает "медленные"
     метрики (CPU/RAM/GPU/диски/сеть/OLED-экраны). BAR-пиксели пересчитываются
     и шлются КАЖДЫЙ тик - чтобы OSD громкости откликался на вращение
     энкодера быстро, а не раз в секунду. Один поток = один писатель в
     serial - никаких блокировок между чтением и записью не требуется.

  2. Запуск - трей-иконка (pystray), а не голый процесс в Docker. Flask и
     основной цикл метрик крутятся в фоновых потоках, сама трей-иконка
     блокирует главный поток (так требует pystray на Windows).

  3. Tautulli (Plex)/qBittorrent - ТРЕТИЙ фоновый поток (integrations_loop) -
     см. подробное обоснование у константы INTEGRATIONS_POLL_INTERVAL ниже:
     это обычные сетевые HTTP-сервисы, которые могут зависнуть/быть
     выключены, и держать такой риск в главном цикле (там же VU/BAR на
     TICK_INTERVAL, 25 Гц по умолчанию) означало бы регресс к проблеме,
     которую уже решали для read_vu() (см. комментарий там же в
     metrics_windows.py) - только тут причина не баг, а сама природа
     сетевого вызова.

  4. Лог serial-обмена (см. _log_serial()/api_serial_log() ниже) -
     кольцевой буфер в памяти процесса, куда пишется КАЖДАЯ строка,
     реально ушедшая на плату (tx) или пришедшая от платы (rx). Питает
     терминал "СЕРИЙНЫЙ ПОРТ" на странице / (см. SENSORS_PAGE_HTML) - это
     тот же самый serial-обмен, что и BAR:/L1-3:/ENC:/BTN:, просто с
     человекочитаемым логом поверх.

  5. Лог программы (см. _log_app()/_StdoutTee/api_app_log() ниже) - ВТОРОЙ,
     независимый кольцевой буфер - подключение/отключение платы, ошибки
     чтения звука/GPU/сети и т.п., т.е. всё, что и так печатается через
     print(..., flush=True) по всему проекту (metrics_windows.py, flash.py,
     сам pc_hud.py). Вместо переписывания каждого print() на вызов отдельной
     функции логирования - перехватывается сам sys.stdout ОДИН РАЗ (см.
     _StdoutTee) - консоль (если она есть, без --windowed) по-прежнему
     получает все строки как раньше, а КАЖДАЯ завершённая строка
     ДОПОЛНИТЕЛЬНО оседает в _app_log. Питает терминал "ЛОГ ПРОГРАММЫ" на /
     - независимый от serial-лога, свой ring buffer, свой набор чекбоксов.

     Werkzeug (встроенный HTTP-сервер Flask) по умолчанию логирует КАЖДЫЙ
     запрос строкой вида '127.0.0.1 - - [...] "GET /api/state ..." 200 -'
     через свой logging-логгер "werkzeug" (НЕ через print()) - раз /api/state
     и /api/app_log сами опрашиваются раз в доли секунды с фронтенда (см.
     refresh()/pollAppLog() в SENSORS_PAGE_HTML), эти строки моментально
     заваливают лог программы бесполезным шумом. Раз это идёт через
     logging, а не print(), _StdoutTee их в принципе не видит - тем не
     менее уровень логгера "werkzeug" явно поднят до ERROR в run_web() ниже,
     чтобы Flask вообще не форматировал и не печатал такие строки (дешевле,
     чем текстовый фильтр по паттерну "GET ... HTTP/1.1").

  6. Мониторинг произвольных ресурсов (ping/TCP-порт, см. metrics_ping.py) -
     ЧЕТВЁРТЫЙ фоновый поток (monitor_loop) - тот же принцип обособления от
     главного цикла, что и у integrations_loop (см. п.3 выше): проверка
     доступности с таймаутом не должна блокировать VU/BAR. Интервал у
     мониторинга концептуально другой (минуты, не секунды - живая настройка
     cfg["ping_interval_seconds"], см. DEFAULT_SETTINGS), поэтому это
     ОТДЕЛЬНЫЙ поток, не смешанный с Tautulli/qBittorrent (там ритм секундный).
     Число целей заранее неизвестно и задаётся пользователем в /settings
     (cfg["mon_targets"]) - см. variables.py, repeating-группа "mon".

Зависимости (requirements.txt):
    pyserial, flask, psutil, pynvml, pycaw, comtypes, pywin32, pystray, pillow
    (metrics_ping.py новых зависимостей не добавляет - subprocess/socket
    только из стандартной библиотеки)
"""

import copy
import json
import logging
import os
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque

import serial
from flask import Flask, request, jsonify, Response, send_file

import variables
import templates
import screens
import screens_webui
import settings_webui
import protocol
import ledbar
import osd
import history
import metrics_windows
import metrics_tautulli
import metrics_qbittorrent
import metrics_ping
import flash
import flash_webui

SCRIPT_VERSION = "2026-09-24-1"

CONTAINER_START_TIME = time.time()

# ---------------- КОНФИГ ----------------

BAUD = int(os.environ.get("BAUD", "115200"))

# NET_MAX_MBPS - что считать 100% на LED-метрике "net" (использует тот же
# интерфейс, что выбран для OLED-экрана Network 1 - net1_iface в settings,
# отдельного "LED-only" интерфейса не заводим, чтобы не плодить настройки).
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "300"))

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))       # медленные метрики (CPU/RAM/GPU/диски/сеть/экраны)
TICK_INTERVAL = float(os.environ.get("TICK_INTERVAL", "0.04"))        # частота главного цикла (BAR/serial-чтение) -
                                                                        # 25 Гц; понижено с прежних 0.1с (10 Гц) - для
                                                                        # VU-эквалайзера (см. read_vu() в
                                                                        # metrics_windows.py) 10 Гц ощущались
                                                                        # ступенчато. Если при большом числе
                                                                        # светодиодов упрётесь в пропускную
                                                                        # способность serial (115200 бод) - можно
                                                                        # поднять обратно через переменную окружения
                                                                        # TICK_INTERVAL=0.06 и т.п., без правки кода.
FULL_RESYNC_SECONDS = float(os.environ.get("FULL_RESYNC_SECONDS", "30"))

# Tautulli/qBittorrent - обычные сетевые HTTP-сервисы (могут тормозить/быть
# выключены/недоступны по сети, в отличие от локальных psutil/pynvml/pycaw-
# вызовов остальных метрик) - опрашиваются ОТДЕЛЬНЫМ фоновым потоком
# (integrations_loop ниже), не главным циклом (metrics_main_loop), и своим,
# более редким интервалом - этим данным не нужна секундная свежесть, а
# заблокировать ими главный цикл (там же VU/BAR/serial на TICK_INTERVAL,
# 25 Гц по умолчанию) означало бы вернуть ровно ту проблему, которую уже
# решали для read_vu() (см. комментарий там же в metrics_windows.py) - только
# тут причина не баг в коде, а сама природа сетевого вызова (см.
# REQUEST_TIMEOUT в metrics_tautulli.py/metrics_qbittorrent.py - до
# нескольких секунд на один недоступный сервис).
INTEGRATIONS_POLL_INTERVAL = float(os.environ.get("INTEGRATIONS_POLL_INTERVAL", "5.0"))

# Мониторинг ресурсов (ping/TCP, см. metrics_ping.py) - ЖИВАЯ настройка в
# /settings (cfg["ping_interval_seconds"]), а не переменная окружения, как и
# serial_port/tautulli_url и т.п. ниже - см. обсуждение в чате: пользователю
# явно хотелось крутить интервал через веб, не через env var. Константы ниже -
# только ДЕФОЛТЫ для DEFAULT_SETTINGS при самом первом запуске (см. там же),
# дальше живут в settings.json, как и tick_interval.
DEFAULT_PING_INTERVAL_SECONDS = 120.0   # 2 минуты - цели мониторинга не
                                          # нуждаются в секундной свежести,
                                          # в отличие от Tautulli/qBittorrent
DEFAULT_PING_TIMEOUT_MS = 800
DEFAULT_PING_FAIL_THRESHOLD = 2     # столько провалов подряд, прежде чем
                                      # реально признать offline (см.
                                      # гистерезис в metrics_ping.PingMonitor)
DEFAULT_PING_RECOVER_THRESHOLD = 1  # столько успехов подряд для возврата в online

# Порог показа топ-процесса (top_process_name) больше НЕ глобальная настройка:
# он задаётся пороговым условием самого экрана на /screens (например
# top_process_cpu_pct >= 25, см. screens.py, conditions) - верное значение
# зависит от железа (psutil отдаёт CPU% ненормализованным по числу потоков),
# и разным экранам могут быть нужны разные пороги. TopProcessMonitor теперь
# всегда отдаёт реальный топ-процесс (см. metrics_windows.py).

WEB_PORT = int(os.environ.get("WEB_PORT", "8189"))

_DEFAULT_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "win-hud-arduino")
CONFIG_DIR = os.environ.get("CONFIG_DIR", _DEFAULT_CONFIG_DIR)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ICON_PATH = os.path.join(ASSETS_DIR, "icon.png")
FAVICON_PATH = os.path.join(ASSETS_DIR, "favicon.png")

# ---------------- дефолтные настройки ----------------
# Лента одна, но внутренний ключ "bar0" сохранён ради совместимости с уже
# написанным settings_webui.py/JS (тот же формат {bar0: value} на все API).

DEFAULT_COLORS = {"bar0": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"}}
DEFAULT_COLORS_TOP = copy.deepcopy(DEFAULT_COLORS)
DEFAULT_ASSIGNMENT = {"bar0": "cpu"}
DEFAULT_ASSIGNMENT_TOP = dict(DEFAULT_ASSIGNMENT)
DEFAULT_SOLID = {"bar0": False}
DEFAULT_SOLID_TOP = {"bar0": False}
DEFAULT_MODE = {"bar0": "classic"}
DEFAULT_PEAK = {"bar0": {"enabled": False, "style": "hold"}}
DEFAULT_BRIGHTNESS = 15
DEFAULT_PEAK_HOLD_SECONDS = 2.0
DEFAULT_PEAK_FADE_SECONDS = 1.5

DEFAULT_ENCODER = {
    "volume_step_pct": 2,          # % громкости на один "клик" вращения
    "click_action": "mute_toggle",  # mute_toggle | switch_device (последнее - заглушка, см. metrics_windows.py)
    "osd_hold_seconds": 3.0,       # сколько держится OSD после последнего вращения/клика
    "mute_color": "FF0000",
    "warning_color": "FFA500",
    "warning_threshold_pct": 95,
    "volume_colors": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
    # В каком из четырёх режимов ленты (classic/center/edges/flat, см.
    # BAR_MODES ниже и ledbar.compute_volume_osd_pixels()) рисовать OSD
    # громкости - ОТДЕЛЬНАЯ настройка от режима обычной метрики
    # (cfg["mode"]["bar0"]) - раньше OSD ВСЕГДА рисовался как center,
    # независимо от того, что выбрано для метрики (см. обсуждение в чате).
    # Дефолт "center" - прежнее поведение без изменений для тех, кто ещё не
    # трогал эту настройку.
    "osd_bar_mode": "center",
}

# ---- OSD-очередь (см. osd.py/класс OsdManager и обсуждение в чате про
# унификацию: раскладка клавиатуры и смена аудио-устройства тоже popup'ы
# через ТУ ЖЕ очередь, что и громкость (DEFAULT_ENCODER.osd_hold_seconds
# выше - её длительность не трогаем, у каждого типа своя). Приоритет
# прерывания зашит в OSD_TYPES (см. osd.py), тут только тайминги/цвета -
# живые настройки, редактируются в /settings.
DEFAULT_LAYOUT_HOLD_SECONDS = 1.2   # короче, чем у громкости - раскладка меняется
                                      # часто, задерживать надолго не нужно (см.
                                      # обсуждение в чате - "боковым зрением зацепить")
DEFAULT_DEVICE_HOLD_SECONDS = 2.0
DEFAULT_OSD_COOLDOWN_SECONDS = 0.5   # минимальный интервал между ЛЮБЫМИ двумя
                                      # срабатываниями OSD (любого типа) - защита
                                      # от дребезга источника (см. осуждение п.3.3/4)
# Цвет LED-вспышки на layout OSD, по коду раскладки (см.
# metrics_windows._PRIMARY_LANG_NAMES за списком известных кодов) - "_default"
# используется для языков, для которых отдельный цвет не задан. Только для
# типа "layout" - у "device" вспышки на ленте нет вообще (см. обсуждение -
# смена устройства вывода не настолько срочное событие, только OLED-текст).
DEFAULT_LAYOUT_COLORS = {
    "EN": "1E90FF", "RU": "FF4500", "UA": "FFD700", "_default": "808080",
}

# ---- Приоритетная ротация экранов - см. screens.RotationState.
# "Каждый N-й слот" для priority/ambient дорожек - см. докстринг screens.py
# за полным описанием алгоритма (round-robin внутри дорожки + форс-прерывание
# только у priority). Живые настройки в /settings, а не константы - т.к.
# "насколько часто" это вопрос личного вкуса пользователя, как и tick_interval.
# (tier "priority" - прежнее "personal", "приоритетный" вместо "личный";
# ключи настроек переименованы соответственно - см. _LEGACY_SETTING_RENAMES.)
DEFAULT_BOOST_PRIORITY = 2
DEFAULT_BOOST_AMBIENT = 4

BAR_METRICS = {
    "cpu": "CPU",
    "ram": "RAM",
    "gpu": "GPU загрузка",
    "gpu_vram": "GPU VRAM",
    "disk1": "Диск 1, %",
    "disk2": "Диск 2, %",
    "net": "NET (Network 1, для LED)",
    # VU-метр - реальный уровень играющего звука (пики сигнала), НЕ системная
    # громкость (volume_pct) - см. metrics_windows.AudioController.read_vu().
    # peak - общий пик по всем каналам сразу (удобно для classic-режима);
    # left/right - для честного стерео в center-режиме (низ=left, верх=right).
    "vu_peak": "VU: пик громкости (звук)",
    "vu_left": "VU: левый канал",
    "vu_right": "VU: правый канал",
}

CLICK_ACTIONS = ("mute_toggle", "switch_device")

# Режимы ленты - см. ledbar.py (докстринг модуля) за подробным описанием
# геометрии каждого режима:
#   classic - обычный градиент по всей длине ленты (одна метрика)
#   center  - растёт от центра к обоим краям (две половины/метрики)
#   edges   - растёт от обоих краёв к центру (две половины/метрики,
#             зеркально center)
#   flat    - вся лента одним сплошным "плывущим" цветом (одна метрика,
#             без позиционного заполнения)
# center/edges используют одну и ту же пару настроек (assignment_top,
# colors_top, solid_top - "правая половина") - при переключении между ними
# эти настройки не сбрасываются и не дублируются.
BAR_MODES = ("classic", "center", "edges", "flat")

DEFAULT_SETTINGS = {
    "colors": DEFAULT_COLORS,
    "colors_top": DEFAULT_COLORS_TOP,
    "assignment": DEFAULT_ASSIGNMENT,
    "assignment_top": DEFAULT_ASSIGNMENT_TOP,
    "mode": DEFAULT_MODE,
    "brightness": DEFAULT_BRIGHTNESS,
    "solid": DEFAULT_SOLID,
    "solid_top": DEFAULT_SOLID_TOP,
    "peak": DEFAULT_PEAK,
    "peak_hold_seconds": DEFAULT_PEAK_HOLD_SECONDS,
    "peak_fade_seconds": DEFAULT_PEAK_FADE_SECONDS,
    "contrast": 255,
    "leds_count": 30,
    # Реверс ленты - на случай, если она физически подключена/повёрнута
    # "задом наперёд" относительно того, что ожидает ledbar.py (level 0 =
    # "начало" полосы). Проще калибровки LED_MAP в прошивке - переключается
    # на лету из /settings, без пересборки/перезаливки. Разворачивает УЖЕ
    # ГОТОВЫЙ список пикселей ОДИН РАЗ в главном цикле (см.
    # metrics_main_loop ниже) - работает одинаково для ЛЮБОГО режима ленты
    # (classic/center/edges/flat/volume_osd), т.к. применяется уже ПОСЛЕ
    # того, как конкретный режим посчитал свои пиксели.
    "leds_reverse": False,
    # Частота главного цикла (VU/BAR-обновления, чтение serial), в секундах -
    # см. также TICK_INTERVAL (env var) в шапке файла. Дефолт тут = значению
    # TICK_INTERVAL на момент старта - т.е. пока настройку никто не трогал
    # через /settings, поведение то же, что было раньше (управлялось только
    # переменной окружения). Как только пользователь один раз сохранит
    # значение через /api/tick_interval, оно осядет в settings.json и с
    # этого момента будет ПЕРЕВЕШИВАТЬ переменную окружения при каждом
    # следующем запуске (см. load_settings() - saved-значение всегда в
    # приоритете над DEFAULT_SETTINGS). Это осознанный компромисс: раз
    # настройка живёт в /settings как обычный слайдер, она должна вести
    # себя как остальные - переживать перезапуски независимо от env.
    "tick_interval": TICK_INTERVAL,
    "serial_port": "",
    "net1_iface": "",
    "net2_iface": "",
    "disk1_letter": "",
    "disk2_letter": "",
    "encoder": DEFAULT_ENCODER,
    # OSD раскладки/устройства - см. DEFAULT_LAYOUT_HOLD_SECONDS и т.п. выше
    # за обоснованием значений. layout_colors - словарь код_раскладки -> hex,
    # merge-логика load_settings() обрабатывает его так же, как colors/bar0
    # (dict-of-dict, один уровень вложенности) - пользователь может
    # переопределить/добавить отдельные языки, не обнуляя остальные.
    "layout_hold_seconds": DEFAULT_LAYOUT_HOLD_SECONDS,
    "device_hold_seconds": DEFAULT_DEVICE_HOLD_SECONDS,
    "osd_cooldown_seconds": DEFAULT_OSD_COOLDOWN_SECONDS,
    "layout_colors": DEFAULT_LAYOUT_COLORS,
    # Приоритетная ротация экранов (tier=priority/ambient, см. screens.py) -
    # "каждый N-й слот" для каждой дорожки.
    "boost_priority": DEFAULT_BOOST_PRIORITY,
    "boost_ambient": DEFAULT_BOOST_AMBIENT,
    # Tautulli (Plex) - адрес/ключ подключения, тот же принцип, что и
    # serial_port/net1_iface выше - живая настройка в /settings, а не
    # переменная окружения. my_plex_user - если заполнено и совпадает
    # со stream_user активного сеанса (Tautulli отдаёт friendly_name) - этот
    # сеанс считается tier="priority" (тот же человек смотрит на этом же ПК),
    # а не "ambient" - см. обсуждение в чате про "чужой/свой Plex-сеанс".
    # Пусто (дефолт) - ВСЕ Plex-сеансы считаются ambient, безопасное поведение.
    "tautulli_url": "",
    "tautulli_api_key": "",
    "my_plex_user": "",
    # qBittorrent - ДВА сервера (два разных инстанса с разными IP и разными
    # API-ключами, см. metrics_qbittorrent.py) - плоские qbt1_*/qbt2_* ключи,
    # тот же паттерн, что net1_iface/net2_iface/disk1_letter/disk2_letter
    # выше. API-ключ хранится в settings.json открытым текстом - тот же
    # уровень доверия, что и у остальных данных приложения (локальный файл
    # на машине пользователя, Flask слушает только 127.0.0.1 - см. run_web()
    # ниже), шифрование тут избыточно.
    "qbt1_url": "",
    "qbt1_api_key": "",
    "qbt2_url": "",
    "qbt2_api_key": "",
    # Мониторинг ресурсов (см. metrics_ping.py/monitor_loop ниже) -
    # mon_targets - список целей [{"id","label","host","port"}, ...],
    # задаётся и меняется ЦЕЛИКОМ через /api/monitor_targets (см.
    # _sanitize_mon_targets() ниже) - пустой список по умолчанию, ни одной
    # цели не заведено (та же логика, что qbt1_url="" - интеграция просто
    # выключена, пока пользователь ничего не настроил). Остальные три ключа -
    # тайминги проверки, см. DEFAULT_PING_* выше за обоснованием дефолтов.
    "mon_targets": [],
    "ping_interval_seconds": DEFAULT_PING_INTERVAL_SECONDS,
    "ping_timeout_ms": DEFAULT_PING_TIMEOUT_MS,
    "ping_fail_threshold": DEFAULT_PING_FAIL_THRESHOLD,
    "ping_recover_threshold": DEFAULT_PING_RECOVER_THRESHOLD,
    # avrdude - путь к папке (или сразу к avrdude.exe), если он не в PATH -
    # см. flash.resolve_avrdude_exe(). Живая настройка со страницы /flash,
    # тот же принцип, что serial_port/tautulli_url и т.п. выше. Пусто -
    # используется переменная окружения AVRDUDE_PATH, а если и её нет -
    # обычный поиск "avrdude" в PATH (старое поведение без изменений).
    "avrdude_path": "",
}


# Прежние ключи settings.json -> текущие (tier "personal" переименован в
# "priority", "личный" -> "приоритетный"). Значение переносится один раз при
# загрузке, если нового ключа ещё нет; старый ключ дальше игнорируется (его
# нет в DEFAULT_SETTINGS) и пропадает из файла при первом же сохранении.
# top_process_min_cpu_pct тут НЕ переносится: порог переехал в условия экранов
# и мигрирует отдельно - см. screens._migrate_top_process_conditions().
_LEGACY_SETTING_RENAMES = {
    "priority_boost_personal": "boost_priority",
    "priority_boost_ambient": "boost_ambient",
}


def load_settings():
    """Рекурсивный merge с дефолтами - без изменений логики относительно
    shkaf-hud (двухуровневый merge для dict-of-dict уже покрывает и
    encoder.volume_colors, т.к. это тоже плоский dict на верхнем уровне
    вложенности - см. DEFAULT_ENCODER). mon_targets - СПИСОК, не dict, поэтому
    под dict-ветку merge не попадает и просто целиком берётся из saved
    (см. else-ветку ниже) - ровно то поведение, которое нужно: список целей
    сохраняется/загружается атомарно, без попытки "слить" элементы с дефолтом
    (дефолт для него и так всегда пустой []."""
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except Exception:
        saved = {}

    if isinstance(saved, dict):
        for old_key, new_key in _LEGACY_SETTING_RENAMES.items():
            if old_key in saved and new_key not in saved:
                saved[new_key] = saved[old_key]

    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    for key, default_val in DEFAULT_SETTINGS.items():
        if key not in saved:
            continue
        saved_val = saved[key]
        if isinstance(default_val, dict):
            if isinstance(saved_val, dict):
                for sub_key, sub_val in saved_val.items():
                    if sub_key in cfg[key] and isinstance(cfg[key][sub_key], dict) and isinstance(sub_val, dict):
                        cfg[key][sub_key].update(sub_val)
                    elif sub_key in cfg[key] and not isinstance(cfg[key][sub_key], dict):
                        cfg[key][sub_key] = sub_val
        else:
            cfg[key] = saved_val
    return cfg


def save_settings(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f)


def _sanitize_mon_targets(raw):
    """Валидирует список целей мониторинга, пришедший из /api/monitor_targets
    (см. metrics_ping.PingMonitor.read() за тем, как именно потребляется
    каждое поле дальше). Записи без host отбрасываются молча (пустая строка
    "ничего не мониторит", тот же принцип, что у net2_iface=""); id
    генерируется, если его нет (новая строка, добавленная в редакторе на
    /settings) - список целей сохраняется целиком одним POST (не через
    отдельный CRUD по id, как /api/screens/*, см. api_monitor_targets() ниже),
    поэтому фронтенду не нужно самому изобретать уникальный id для новой
    строки - достаточно прислать её без id вовсе."""
    out = []
    for t in (raw or []):
        if not isinstance(t, dict):
            continue
        host = (t.get("host") or "").strip()
        if not host:
            continue
        label = (t.get("label") or "").strip() or host
        tid = t.get("id") or uuid.uuid4().hex[:12]
        port = t.get("port")
        try:
            port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError):
            port = None
        if port is not None:
            port = max(1, min(65535, port))
        out.append({"id": tid, "label": label, "host": host, "port": port})
    return out


state_lock = threading.Lock()
state = {
    "bar": {"mode": "classic", "pixels": [], "pct_bottom": 0, "pct_top": None, "osd_active": False},
    "cfg": load_settings(),
    "serial_connected": False,
    "oled_lines": ["", "", ""],
}

_last_context = {}
_context_lock = threading.Lock()

flashing_event = threading.Event()

gpu_monitor = metrics_windows.GpuMonitor()
audio_controller = metrics_windows.AudioController()
media_monitor = metrics_windows.MediaMonitor()
top_process_monitor = metrics_windows.TopProcessMonitor()

# Tautulli/qBittorrent - см. integrations_loop() ниже и обоснование у
# INTEGRATIONS_POLL_INTERVAL в шапке файла: отдельный фоновый поток пишет
# сюда результат под своим локом, metrics_main_loop только читает
# (get_integrations_state()) - ни одного сетевого вызова в главном цикле.
_integrations_lock = threading.Lock()
_integrations_state = {
    "plex_movies": 0, "plex_series": 0, "plex_songs": 0,
    "plex_server_status": "offline", "plex_transcode_count": None, "plex_users_count": 0,
    "streams": [], "recent": [],
    "qbt_total_dl": "0 B/s", "qbt_total_ul": "0 B/s", "qbt_ratio": 0.0, "qbt_free_space_gb": "?",
    "qbt_count_all": 0, "torrents": [],
    # числовые двойники qbt_total_dl/ul (Мбит/с) для пороговых условий экранов;
    # None = нет данных (интеграция выключена/сервер недоступен)
    "qbt_dl_mbps": None, "qbt_ul_mbps": None,
    # top_process_* - топ-процесс по CPU переехал сюда из главного
    # цикла (metrics_main_loop) в integrations_loop, см. комментарий там же -
    # psutil.Process.cpu_percent()/memory_percent()/name() на КАЖДЫЙ процесс
    # в системе на Windows оказался достаточно тяжёлым, чтобы раз в секунду
    # (POLL_INTERVAL) блокировать главный цикл на заметное время и приводить
    # к тому, что VU/BAR/serial обновлялись гораздо реже tick_interval,
    # независимо от значения слайдера в /settings.
    "top_process_name": None, "top_process_cpu_pct": 0.0, "top_process_ram_pct": 0.0,
}

# Мониторинг ресурсов (см. monitor_loop() ниже и metrics_ping.py) - ОТДЕЛЬНЫЙ
# от _integrations_state буфер (свой лок, свой поток-писатель) - не смешан с
# Tautulli/qBittorrent ВЫШЕ намеренно: у мониторинга свой, гораздо более
# редкий интервал (минуты, см. cfg["ping_interval_seconds"]), общий буфер с
# integrations_loop заставил бы их либо делить один интервал (потеряв смысл
# отдельной настройки), либо городить в одном потоке два независимых
# таймера - вместо этого проще и понятнее держать полностью отдельный поток.
_monitor_lock = threading.Lock()
_monitor_state = {
    "mon": [],
    "mon_down_count": 0,
    "mon_down_names": None,
}

# ---------------- лог serial-обмена (для терминала на /) ----------------
# Кольцевой буфер последних строк - живёт ТОЛЬКО в памяти процесса (как и
# остальной module-level state), не сохраняется на диск. Пишутся сюда РЕАЛЬНО
# отправленные/полученные serial-строки (см. metrics_main_loop ниже - хуки в
# точке чтения входящих строк и в точке ser.write()) - это та же самая
# информация, что видна в протоколе (BAR:/BRI:/CON:/L1-3:/ENC:/BTN:), просто
# с человекочитаемым временем и направлением поверх, для отладки на живом
# железе без отдельного serial-монитора.
_SERIAL_LOG_MAXLEN = 500
_SERIAL_LOG_TEXT_LIMIT = 300  # BAR: с большим leds_count может быть длинной -
                               # обрезаем для читаемости терминала, это лог
                               # для человека, не протокольный дамп

_serial_log_lock = threading.Lock()
_serial_log = deque(maxlen=_SERIAL_LOG_MAXLEN)
_serial_log_seq = 0  # монотонно растущий id - НЕ len(deque) (deque сам роняет
                      # старые записи при переполнении maxlen) - нужен, чтобы
                      # фронтенд мог опрашивать инкрементально (?after=<id>),
                      # не перекачивая весь буфер на каждый тик.


def _log_serial(direction, text):
    """direction: 'rx' (пришло от платы) | 'tx' (отправлено на плату)."""
    global _serial_log_seq
    if len(text) > _SERIAL_LOG_TEXT_LIMIT:
        text = text[:_SERIAL_LOG_TEXT_LIMIT] + "…"
    with _serial_log_lock:
        _serial_log_seq += 1
        _serial_log.append({"id": _serial_log_seq, "ts": time.time(), "dir": direction, "text": text})


# ---------------- лог программы (для терминала "Лог программы" на /) ----------------
# ВТОРОЙ, независимый от serial-лога кольцевой буфер - подключение/отключение
# платы, ошибки чтения звука/GPU/сети, старт/стоп потоков и т.п. Всё это и
# так уже печатается через print(..., flush=True) по всему проекту
# (metrics_windows.py/flash.py/сам pc_hud.py) - вместо переписывания каждого
# такого print() на отдельный вызов логгера, перехватывается сам sys.stdout
# ОДИН РАЗ (см. _StdoutTee ниже, устанавливается в main()) - реальная
# консоль (если она есть, т.е. без --windowed при сборке PyInstaller) по-
# прежнему получает все строки как раньше, а КАЖДАЯ завершённая строка
# ДОПОЛНИТЕЛЬНО оседает сюда.
_APP_LOG_MAXLEN = 500
_APP_LOG_TEXT_LIMIT = 500


def _log_app(text):
    """Одна ЗАВЕРШЁННАЯ строка (без '\\n') лога программы - см. _StdoutTee
    ниже за тем, откуда она берётся. Пустые строки (например от print() без
    аргументов) не логируются - не несут информации, только шумят терминал."""
    global _app_log_seq
    text = text.rstrip("\r")
    if not text:
        return
    if len(text) > _APP_LOG_TEXT_LIMIT:
        text = text[:_APP_LOG_TEXT_LIMIT] + "…"
    with _app_log_lock:
        _app_log_seq += 1
        _app_log.append({"id": _app_log_seq, "ts": time.time(), "text": text})


_app_log_lock = threading.Lock()
_app_log = deque(maxlen=_APP_LOG_MAXLEN)
_app_log_seq = 0


class _StdoutTee:
    """Перехватывает sys.stdout/sys.stderr построчно - каждая строка
    одновременно (а) пишется в РЕАЛЬНЫЙ поток (если он есть - без него, при
    сборке --windowed, это просто no-op, см. write() ниже) И (б) попадает в
    _app_log (см. _log_app() выше). Буферизует до символа '\\n', т.к.
    print() может вызвать write() несколькими кусками (сам текст, потом
    отдельно перевод строки) - логировать нужно только ПОЛНЫЕ строки, не
    произвольные фрагменты записи.

    Устанавливается ОДИН РАЗ в main() (см. sys.stdout = _StdoutTee(...)) -
    благодаря этому НИ ОДИН print(..., flush=True) по всему проекту не
    нужно переписывать на отдельный вызов логгера: они и так уже есть везде,
    где важно видеть событие (подключение/отключение serial, ошибки
    аудио/GPU/сети - см. metrics_windows.py/flash.py/сам этот файл).

    ВАЖНО: это ловит только print()/sys.stdout.write() - НЕ логи через
    модуль logging (например Werkzeug пишет свой access-лог именно через
    logging, а не print()) - см. run_web() ниже, где уровень логгера
    "werkzeug" явно поднят до ERROR, чтобы такие строки вообще не
    генерировались (а не фильтровались тут постфактум)."""

    def __init__(self, original):
        self._original = original
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, text):
        if self._original is not None:
            try:
                self._original.write(text)
            except Exception:
                pass
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                _log_app(line)
        return len(text)

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except Exception:
                pass

    def isatty(self):
        try:
            return bool(self._original and self._original.isatty())
        except Exception:
            return False


def get_context():
    with _context_lock:
        return dict(_last_context)


def get_integrations_state():
    with _integrations_lock:
        return dict(_integrations_state)


def get_monitor_state():
    with _monitor_lock:
        return dict(_monitor_state)


# ---------------- форматтеры (аналог shkaf-hud) ----------------

# Короткие русские имена дней недели по tm_wday (0=Пн..6=Вс) - НЕ через
# time.strftime("%a"), т.к. это зависит от локали ОС (на англоязычной
# Windows дало бы "Mon"/"Tue"/... независимо от языка интерфейса) - тут же
# нужен предсказуемый результат независимо от локали хоста, как и у
# keyboard_layout в metrics_windows.py (своя таблица, а не системный API).
_WEEKDAY_NAMES_RU = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def format_weekday_name():
    return _WEEKDAY_NAMES_RU[time.localtime().tm_wday]


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_rate(bytes_delta, dt):
    if dt <= 0 or bytes_delta < 0:
        return "0Kbps"
    bits_per_sec = bytes_delta * 8 / dt
    mbps = bits_per_sec / 1_000_000
    if mbps >= 1:
        return f"{mbps:.1f}Mbps"
    kbps = bits_per_sec / 1000
    return f"{kbps:.0f}Kbps"


def rate_mbps(bytes_delta, dt):
    """Скорость в Мбит/с ЧИСЛОМ (float) - числовой двойник format_rate() для
    пороговых условий экранов (net*_rx_mbps/net*_tx_mbps в variables.py):
    порог на строку вроде "12.3Mbps" не повесить. Единицы те же, что у
    format_rate() (мегабит = 1_000_000 бит)."""
    if dt <= 0 or bytes_delta < 0:
        return 0.0
    return round(bytes_delta * 8 / dt / 1_000_000, 2)


def format_bytes_total(bytes_val):
    if bytes_val is None or bytes_val < 0:
        return "0MB"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = bytes_val / (1024 ** 2)
    return f"{mb:.0f}MB"


def format_speed_mbps(mbps):
    if not mbps:
        return "?"
    if mbps >= 1000:
        return f"{mbps / 1000:g}Gbit"
    return f"{mbps}Mbit"

# ---------------- assets (иконка трея/favicon - генерируются, если отсутствуют) ----------------

def _ensure_assets():
    """Если assets/icon.png нет (например первый запуск из исходников, а не
    из готового дистрибутива) - рисуем простую иконку через Pillow, чтобы
    трею и /favicon.png/ /icon.png было что раздавать без внешних файлов."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    if os.path.isfile(ICON_PATH) and os.path.isfile(FAVICON_PATH):
        return
    try:
        from PIL import Image, ImageDraw
        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, size - 8, size - 8), fill=(255, 140, 47, 255))
        draw.ellipse((size * 0.28, size * 0.28, size * 0.72, size * 0.72), fill=(23, 24, 26, 255))
        img.save(ICON_PATH)
        img.resize((32, 32)).save(FAVICON_PATH)
    except Exception as e:
        print(f"[assets] не удалось сгенерировать иконку: {e}", flush=True)


# ---------------- serial ----------------

def try_open_serial(port):
    if not port:
        return None
    try:
        s = serial.Serial(port, BAUD, timeout=0)  # timeout=0 - неблокирующее чтение
        time.sleep(2)
        with state_lock:
            state["serial_connected"] = True
        print(f"[serial] connected: {port}", flush=True)
        return s
    except (serial.SerialException, OSError) as e:
        with state_lock:
            state["serial_connected"] = False
        print(f"[serial] connect to {port} failed: {e}", flush=True)
        return None


# ---------------- энкодер: применение событий ----------------

def apply_encoder_delta(delta, cfg):
    step = cfg["encoder"]["volume_step_pct"]
    audio_controller.set_volume_relative(delta * step)


def apply_button_click(cfg):
    action = cfg["encoder"]["click_action"]
    if action == "mute_toggle":
        audio_controller.toggle_mute()
    elif action == "switch_device":
        audio_controller.switch_output_device()


# ---------------- веб-интерфейс (Sensors) ----------------

app = Flask(__name__)

SENSORS_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>win-hud-arduino</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<link rel="icon" type="image/png" href="/favicon.png">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a; --panel: #1f2123; --border: #2c2e31;
    --text: #e6e6e6; --muted: #8a8d91; --accent: #ff8c2f; --danger: #e0483e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:24px 16px 60px; }
  .wrap { max-width:560px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; flex-wrap:wrap; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .banner { display:none; background:#3a2418; border:1px solid var(--danger); color:#ffb3ab;
            border-radius:10px; padding:12px 16px; margin-bottom:18px; font-size:13px;
            align-items:center; gap:10px; }
  .banner.show { display:flex; }
  .banner .b-dot { width:8px; height:8px; border-radius:50%; background:var(--danger); flex-shrink:0; }

  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:22px; margin-bottom:18px; }
  .card h2 { font-size:11px; color:var(--muted); margin:0 0 18px; font-weight:600; }
  .card h2 .log-controls { float:right; display:flex; align-items:center; gap:12px; font-weight:400; }
  .card h2 .log-controls label { font-size:11px; color:var(--muted); display:flex; align-items:center;
                                  gap:5px; cursor:pointer; }
  .card h2 .log-controls input[type=checkbox] { width:13px; height:13px; }
  .card h2 .log-controls button { background:none; border:1px solid var(--border); color:var(--muted);
                                   border-radius:5px; padding:2px 8px; font-size:11px; cursor:pointer; }
  .card h2 .log-controls button:hover { color:var(--text); border-color:var(--accent); }

  .log-box { background:#000; color:#9fd3a0; font-family:monospace; font-size:11px; line-height:1.5;
             padding:12px; border-radius:8px; height:220px; overflow-y:auto; white-space:pre-wrap;
             word-break:break-all; transition:opacity .15s; }
  .log-box.log-disabled { opacity:0.35; }

  .strip-track { width:100%; height:36px; background:#101112; border-radius:6px;
                 display:flex; flex-direction:row; overflow:hidden; border:1px solid var(--border);
                 padding:2px; gap:1px; }
  .led-px { flex:1 1 auto; min-width:1px; border-radius:1px; background:#101112; transition:background .15s; }
  .label { font-size:12px; color:var(--muted); text-align:center; margin-top:8px; }
  .label b { color:var(--text); font-size:13px; }
  .osd-badge { display:none; background:var(--accent); color:#151515; font-size:10px; font-weight:700;
               border-radius:4px; padding:2px 6px; margin-left:8px; }
  .osd-badge.show { display:inline-block; }

  .brightness-row, .field-row { display:flex; align-items:center; gap:10px; margin-top:12px; font-size:13px; }
  .brightness-row label, .field-row label { color:var(--muted); min-width:110px; }
  .brightness-row input[type=range] { flex:1; }
  .brightness-row .val { min-width:36px; text-align:right; color:var(--text); }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/" class="active">Sensors</a><a href="/settings">Settings</a><a href="/screens">OLED screens</a><a href="/flash">Flash</a></div>

  <div class="banner" id="banner"><span class="b-dot"></span>
    Pro Micro не подключена - лента и OLED не обновляются, метрики продолжают собираться</div>

  <div class="card">
    <h2>ЛЕНТА <span class="osd-badge" id="osd-badge">OSD</span></h2>
    <div class="strip-track" id="pixels-strip"></div>
    <div class="label"><b><span id="val-strip"></span></b></div>
    <div class="brightness-row">
      <label>Яркость</label>
      <input type="range" id="brightness" min="0" max="100" value="15">
      <span class="val" id="brightness-val">15%</span>
    </div>
  </div>

  <div class="card">
    <h2>OLED (текущий экран)</h2>
    <div style="background:#000;color:#7fd8ff;font-family:monospace;font-size:18px;padding:16px;border-radius:8px;line-height:1.5" id="oled"></div>
    <div class="brightness-row">
      <label>Контраст</label>
      <input type="range" id="contrast" min="0" max="255" value="255">
      <span class="val" id="contrast-val">255</span>
    </div>
  </div>

  <div class="card">
    <h2>СЕРИЙНЫЙ ПОРТ (лог)
      <span class="log-controls">
        <label><input type="checkbox" id="terminal-enabled" checked> включено</label>
        <label><input type="checkbox" id="terminal-hide-bar"> скрыть BAR:</label>
        <label><input type="checkbox" id="terminal-autoscroll" checked> автопрокрутка</label>
        <button id="terminal-clear">Очистить</button>
      </span>
    </h2>
    <div class="log-box" id="serial-terminal"></div>
  </div>

  <div class="card">
    <h2>ЛОГ ПРОГРАММЫ
      <span class="log-controls">
        <label><input type="checkbox" id="applog-enabled" checked> включено</label>
        <label><input type="checkbox" id="applog-autoscroll" checked> автопрокрутка</label>
        <button id="applog-clear">Очистить</button>
      </span>
    </h2>
    <div class="log-box" id="app-terminal"></div>
  </div>

  <footer>win-hud-arduino</footer>
</div>

<script>
let editingBrightness = false, editingContrast = false;
let pixelsBuilt = false, lastLedsCount = 0;
// Интервал опроса /api/state - ДО первого успешного ответа используется
// дефолт 500мс, дальше refresh() сам подстраивает его под cfg.tick_interval
// (та же живая настройка "Частота опроса", что управляет главным циклом
// в pc_hud.py) - см. refresh() ниже.
let pollDelayMs = 500;

const brightnessEl = document.getElementById("brightness");
brightnessEl.addEventListener("input", () => {
  editingBrightness = true;
  document.getElementById("brightness-val").textContent = brightnessEl.value + "%";
});
brightnessEl.addEventListener("change", () => {
  fetch("/api/brightness", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(brightnessEl.value) }) }).then(() => editingBrightness = false);
});

const contrastEl = document.getElementById("contrast");
contrastEl.addEventListener("input", () => {
  editingContrast = true;
  document.getElementById("contrast-val").textContent = contrastEl.value;
});
contrastEl.addEventListener("change", () => {
  fetch("/api/contrast", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(contrastEl.value) }) }).then(() => editingContrast = false);
});

function buildPixelGrid(ledsCount) {
  const track = document.getElementById("pixels-strip");
  track.innerHTML = "";
  for (let i = 0; i < ledsCount; i++) {
    const sq = document.createElement("div");
    sq.className = "led-px";
    sq.id = "px-" + i;
    track.appendChild(sq);
  }
  pixelsBuilt = true;
  lastLedsCount = ledsCount;
}

function refresh() {
  fetch("/api/state").then(r => r.json()).then(s => {
    document.getElementById("banner").classList.toggle("show", !s.serial_connected);
    if (!pixelsBuilt || lastLedsCount !== s.leds_count) buildPixelGrid(s.leds_count);

    const bar = s.bar;
    bar.pixels.forEach((hex, i) => {
      const px = document.getElementById("px-" + i);
      if (px) px.style.background = "#" + hex;
    });
    const label = bar.mode === "center" ? (bar.pct_bottom + "% / " + bar.pct_top + "%") : (bar.pct_bottom + "%");
    document.getElementById("val-strip").textContent = label;
    const osdBadge = document.getElementById("osd-badge");
    osdBadge.classList.toggle("show", bar.osd_active);
    if (bar.osd_active && bar.osd_type) {
      osdBadge.textContent = bar.osd_type.toUpperCase() + " OSD";
    }

    if (!editingBrightness) {
      brightnessEl.value = s.cfg.brightness;
      document.getElementById("brightness-val").textContent = s.cfg.brightness + "%";
    }
    if (!editingContrast) {
      contrastEl.value = s.cfg.contrast;
      document.getElementById("contrast-val").textContent = s.cfg.contrast;
    }

    document.getElementById("oled").innerHTML = s.oled_lines.map(l => l || "&nbsp;").join("<br>");

    // Подстраиваем частоту опроса ПРЕВЬЮ под cfg.tick_interval - ту же
    // живую настройку (слайдер "Частота опроса" на /settings), что
    // управляет частотой главного цикла (VU/лента/serial) в pc_hud.py.
    // Без этого превью на / всегда опрашивалось бы фиксированным
    // интервалом (раньше - 500мс), независимо от того, насколько быстро
    // в реальности обновляются данные тиком - VU-метр выглядел заметно
    // "тормознее", чем на самой ленте. Нижняя граница 20мс - та же, что и
    // у /api/tick_interval (см. api_tick_interval() выше).
    pollDelayMs = Math.max(20, Math.round((s.cfg.tick_interval || 0.04) * 1000));
  }).catch(() => {
    // сеть/сервер недоступны на этой итерации - не роняем цикл опроса,
    // просто повторим на последнем известном pollDelayMs (см. finally ниже)
  }).finally(() => {
    setTimeout(refresh, pollDelayMs);
  });
}

// ---- Общий хелпер для обоих терминалов: свитч "включено" не только
// скрывает панель визуально (opacity через .log-disabled), но и реально
// останавливает поллинг (see pollFn ниже - каждый вызов сам проверяет
// enabledEl.checked и просто не шлёт запрос, если выключено) - т.е. "не
// собирать данные, пока выключено" в смысле "не тратить сеть/CPU на клиенте",
// а не в смысле остановки самого backend-буфера (тот продолжает копить
// строки в фоне - это дёшево, ring buffer ограничен по размеру).
function wireLogToggle(enabledEl, boxEl) {
  const apply = () => boxEl.classList.toggle("log-disabled", !enabledEl.checked);
  enabledEl.addEventListener("change", apply);
  apply();
}

// Сохраняет/восстанавливает состояние чекбокса в localStorage - без этого
// чекбоксы терминалов (включено/автопрокрутка/скрыть BAR:) сбрасывались на
// хардкод из HTML (checked/без checked в SENSORS_PAGE_HTML) при КАЖДОЙ
// перезагрузке страницы, т.к. их состояние нигде не сохранялось. key -
// уникальный ключ в localStorage; defaultChecked - что использовать, если
// сохранённого значения ещё нет (первый запуск в этом браузере) - берётся
// из ТЕКУЩЕГО el.checked, т.е. из хардкода в HTML, чтобы поведение "из
// коробки" (до первого изменения пользователем) не поменялось.
function persistCheckbox(el, key) {
  const saved = localStorage.getItem(key);
  el.checked = saved !== null ? saved === "1" : el.checked;
  el.addEventListener("change", () => localStorage.setItem(key, el.checked ? "1" : "0"));
}

// ---- Терминал serial-обмена (см. _log_serial()/api_serial_log() в pc_hud.py) ----
let serialLogAfter = 0;
const terminalEl = document.getElementById("serial-terminal");
const terminalEnabledEl = document.getElementById("terminal-enabled");
const autoscrollEl = document.getElementById("terminal-autoscroll");
const hideBarEl = document.getElementById("terminal-hide-bar");
const clearBtnEl = document.getElementById("terminal-clear");
persistCheckbox(terminalEnabledEl, "winhud_terminal_enabled");
persistCheckbox(autoscrollEl, "winhud_terminal_autoscroll");
persistCheckbox(hideBarEl, "winhud_terminal_hide_bar");
wireLogToggle(terminalEnabledEl, terminalEl);

// "Скрыть BAR:" - протокол пайп-разделённый (см. protocol.ProtocolState.build()/
// L1-3:.ino) - BAR: это ОДНО поле среди прочих в строке, поэтому фильтруем
// по частям после split("|"), а не всю строку целиком: полный ресинк
// (FULL_RESYNC_SECONDS) шлёт BRI/CON/L1-3 В ТОЙ ЖЕ строке, что и BAR - грубое
// "скрыть строки с BAR" спрятало бы и их. Если после фильтра в строке ничего
// не осталось (был чистый BAR-тик без остальных полей) - строка не рисуется
// вовсе, а не показывается пустой. Применяется только к НОВЫМ записям -
// переключение чекбокса не переразбирает уже отрисованные строки (буфер DOM
// не хранит исходный e.text) - осознанное упрощение, не стоит усложнять ради
// ретроактивной перефильтровки чисто отладочного лога.
function formatSerialEntry(e) {
  if (!hideBarEl.checked) return e.text;
  const parts = e.text.split("|").filter(p => !p.startsWith("BAR:"));
  return parts.length ? parts.join("|") : null;
}

function pollSerialLog() {
  if (!terminalEnabledEl.checked) return;
  fetch("/api/serial_log?after=" + serialLogAfter).then(r => r.json()).then(data => {
    if (!data.entries.length) return;
    data.entries.forEach(e => {
      serialLogAfter = e.id;  // курсор двигаем ВСЕГДА, даже если строка отфильтрована -
                                // иначе скрытые BAR-тики запрашивались бы повторно на
                                // каждый следующий poll
      const text = formatSerialEntry(e);
      if (text === null) return;
      const row = document.createElement("div");
      const t = new Date(e.ts * 1000).toLocaleTimeString();
      row.style.color = e.dir === "tx" ? "#7fd8ff" : "#9fd3a0";
      row.textContent = "[" + t + "] " + (e.dir === "tx" ? "\u2192 " : "\u2190 ") + text;
      terminalEl.appendChild(row);
    });
    while (terminalEl.childNodes.length > 500) terminalEl.removeChild(terminalEl.firstChild);
    if (autoscrollEl.checked) terminalEl.scrollTop = terminalEl.scrollHeight;
  }).catch(() => {});
}

clearBtnEl.addEventListener("click", () => {
  fetch("/api/serial_log/clear", { method: "POST" }).then(() => {
    terminalEl.innerHTML = "";
  });
});

setInterval(pollSerialLog, 500);
pollSerialLog();

// ---- Терминал лога программы (см. _log_app()/_StdoutTee/api_app_log() в
// pc_hud.py) - независимый от serial-терминала: свой курсор, свой чекбокс
// "включено"/"автопрокрутка", своя кнопка "Очистить". Строки, похожие на
// сообщение об ошибке, подсвечиваются - тот же прицип "человекочитаемый
// лог", что и у serial-терминала (там - направление tx/rx цветом).
let appLogAfter = 0;
const appTerminalEl = document.getElementById("app-terminal");
const appLogEnabledEl = document.getElementById("applog-enabled");
const appLogAutoscrollEl = document.getElementById("applog-autoscroll");
const appLogClearBtnEl = document.getElementById("applog-clear");
persistCheckbox(appLogEnabledEl, "winhud_applog_enabled");
persistCheckbox(appLogAutoscrollEl, "winhud_applog_autoscroll");
wireLogToggle(appLogEnabledEl, appTerminalEl);

const APP_LOG_ERROR_HINTS = ["ошиб", "failed", "error", "traceback", "exception"];
function isErrorLine(text) {
  const lower = text.toLowerCase();
  return APP_LOG_ERROR_HINTS.some(h => lower.includes(h));
}

function pollAppLog() {
  if (!appLogEnabledEl.checked) return;
  fetch("/api/app_log?after=" + appLogAfter).then(r => r.json()).then(data => {
    if (!data.entries.length) return;
    data.entries.forEach(e => {
      appLogAfter = e.id;
      const row = document.createElement("div");
      const t = new Date(e.ts * 1000).toLocaleTimeString();
      row.style.color = isErrorLine(e.text) ? "#ffb3ab" : "#9fd3a0";
      row.textContent = "[" + t + "] " + e.text;
      appTerminalEl.appendChild(row);
    });
    while (appTerminalEl.childNodes.length > 500) appTerminalEl.removeChild(appTerminalEl.firstChild);
    if (appLogAutoscrollEl.checked) appTerminalEl.scrollTop = appTerminalEl.scrollHeight;
  }).catch(() => {});
}

appLogClearBtnEl.addEventListener("click", () => {
  fetch("/api/app_log/clear", { method: "POST" }).then(() => {
    appTerminalEl.innerHTML = "";
  });
});

setInterval(pollAppLog, 500);
pollAppLog();

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }

refresh();  // дальше сама себя перепланирует через setTimeout(pollDelayMs) - см. refresh() выше
</script>
</body></html>
"""


@app.route("/")
def index():
    return Response(SENSORS_PAGE_HTML, mimetype="text/html")


@app.route("/favicon.png")
def favicon():
    return send_file(FAVICON_PATH, mimetype="image/png")


@app.route("/icon.png")
def icon_png():
    return send_file(ICON_PATH, mimetype="image/png")


@app.route("/api/state")
def api_state():
    with state_lock:
        out = dict(state)
        out["metrics"] = BAR_METRICS
        out["click_actions"] = CLICK_ACTIONS
        out["bar_modes"] = BAR_MODES
        out["available_interfaces"] = metrics_windows.list_network_interfaces()
        out["available_disks"] = metrics_windows.list_disk_letters()
        out["available_ports"] = sorted(flash.list_com_ports())
        out["leds_count"] = state["cfg"]["leds_count"]
        return jsonify(out)


@app.route("/api/serial_log")
def api_serial_log():
    """Последние строки serial-обмена для терминала на / - ?after=<id>
    отдаёт только записи новее указанного id (инкрементальный опрос с
    фронтенда без перекачки всего буфера каждый раз)."""
    after = request.args.get("after", type=int, default=0)
    with _serial_log_lock:
        entries = [e for e in _serial_log if e["id"] > after]
    return jsonify({"entries": entries})


@app.route("/api/serial_log/clear", methods=["POST"])
def api_serial_log_clear():
    """Очистить буфер серийного лога (кнопка "Очистить" в терминале на /) -
    _serial_log_seq НЕ сбрасывается (id продолжают расти монотонно) - только
    сам deque пустеет, чтобы не плодить коллизии id, если фронтенд как-то
    закэшировал старый serialLogAfter."""
    with _serial_log_lock:
        _serial_log.clear()
    return jsonify({"ok": True})


@app.route("/api/app_log")
def api_app_log():
    """Последние строки лога программы (см. _log_app()/_StdoutTee выше) -
    тот же паттерн инкрементального опроса, что и /api/serial_log."""
    after = request.args.get("after", type=int, default=0)
    with _app_log_lock:
        entries = [e for e in _app_log if e["id"] > after]
    return jsonify({"entries": entries})


@app.route("/api/app_log/clear", methods=["POST"])
def api_app_log_clear():
    """Очистить буфер лога программы - независим от /api/serial_log/clear."""
    with _app_log_lock:
        _app_log.clear()
    return jsonify({"ok": True})


@app.route("/api/colors", methods=["POST"])
def api_colors():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            for stop in ("c1", "c2", "c3"):
                if stop in body["bar0"]:
                    state["cfg"]["colors"]["bar0"][stop] = body["bar0"][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/colors_top", methods=["POST"])
def api_colors_top():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            for stop in ("c1", "c2", "c3"):
                if stop in body["bar0"]:
                    state["cfg"]["colors_top"]["bar0"][stop] = body["bar0"][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/assignment", methods=["POST"])
def api_assignment():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body and body["bar0"] in BAR_METRICS:
            state["cfg"]["assignment"]["bar0"] = body["bar0"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/assignment_top", methods=["POST"])
def api_assignment_top():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body and body["bar0"] in BAR_METRICS:
            state["cfg"]["assignment_top"]["bar0"] = body["bar0"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body and body["bar0"] in BAR_MODES:
            state["cfg"]["mode"]["bar0"] = body["bar0"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/solid", methods=["POST"])
def api_solid():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            state["cfg"]["solid"]["bar0"] = bool(body["bar0"])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/solid_top", methods=["POST"])
def api_solid_top():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            state["cfg"]["solid_top"]["bar0"] = bool(body["bar0"])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/brightness", methods=["POST"])
def api_brightness():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["brightness"] = max(0, min(100, int(body.get("value", state["cfg"]["brightness"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/contrast", methods=["POST"])
def api_contrast():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["contrast"] = max(0, min(255, int(body.get("value", state["cfg"]["contrast"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/peak", methods=["POST"])
def api_peak():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            entry = body["bar0"]
            if "enabled" in entry:
                state["cfg"]["peak"]["bar0"]["enabled"] = bool(entry["enabled"])
            if "style" in entry and entry["style"] in ("hold", "fade"):
                state["cfg"]["peak"]["bar0"]["style"] = entry["style"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/peak_timing", methods=["POST"])
def api_peak_timing():
    body = request.get_json(force=True)
    with state_lock:
        if "hold_seconds" in body:
            state["cfg"]["peak_hold_seconds"] = round(max(0.0, min(10.0, float(body["hold_seconds"]))), 1)
        if "fade_seconds" in body:
            state["cfg"]["peak_fade_seconds"] = round(max(0.0, min(10.0, float(body["fade_seconds"]))), 1)
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/leds_count", methods=["POST"])
def api_leds_count():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["leds_count"] = max(1, min(300, int(body.get("value", state["cfg"]["leds_count"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/leds_reverse", methods=["POST"])
def api_leds_reverse():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["leds_reverse"] = bool(body.get("value", state["cfg"]["leds_reverse"]))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/tick_interval", methods=["POST"])
def api_tick_interval():
    """Частота главного цикла (VU/лента/serial) - см. tick_interval в
    DEFAULT_SETTINGS выше и использование в главном цикле ниже. Границы
    0.02с (50 Гц) - 0.5с (2 Гц): нижняя - чтобы не заспамить serial-порт
    при большом leds_count (см. предупреждение в README про пропускную
    способность 115200 бод), верхняя - чтобы настройка не превращала ленту
    в полностью неотзывчивую по ошибке."""
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["tick_interval"] = round(
            max(0.02, min(0.5, float(body.get("value", state["cfg"]["tick_interval"])))), 3
        )
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/serial_port", methods=["POST"])
def api_serial_port():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["serial_port"] = body.get("value", state["cfg"]["serial_port"])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/avrdude_path", methods=["POST"])
def api_avrdude_path():
    """Путь к папке с avrdude.exe (или сразу к самому .exe) - см.
    flash.resolve_avrdude_exe(). Читается flash_webui.py непосредственно
    перед запуском прошивки (живая настройка, как и serial_port выше -
    немедленное переподключение тут не нужно, значение просто лежит в
    settings.json до следующего клика "Прошить")."""
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["avrdude_path"] = body.get("value", state["cfg"]["avrdude_path"]).strip()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/disks", methods=["POST"])
def api_disks():
    body = request.get_json(force=True)
    with state_lock:
        if "disk1_letter" in body:
            state["cfg"]["disk1_letter"] = body["disk1_letter"]
        if "disk2_letter" in body:
            state["cfg"]["disk2_letter"] = body["disk2_letter"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/net-ifaces", methods=["POST"])
def api_net_ifaces():
    body = request.get_json(force=True)
    with state_lock:
        if "net1_iface" in body:
            state["cfg"]["net1_iface"] = body["net1_iface"]
        if "net2_iface" in body:
            state["cfg"]["net2_iface"] = body["net2_iface"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/tautulli", methods=["POST"])
def api_tautulli():
    """Адрес и API-ключ Tautulli (см. metrics_tautulli.py) - опрашивается
    ОТДЕЛЬНЫМ фоновым потоком (integrations_loop, см. ниже), поэтому
    сохранение тут не требует немедленного переподключения - новое значение
    cfg подхватится этим потоком на его следующем тике
    (INTEGRATIONS_POLL_INTERVAL, по умолчанию 5с).

    my_plex_user - та же карточка на /settings, см. обсуждение в чате
    про "свой/чужой Plex-сеанс" - сравнивается со stream_user активного
    сеанса построчно в screens.build_active_screens() при вычислении tier
    конкретной копии repeating-экрана "stream" - см. там же за подробностями
    point-override. Пусто (дефолт) - ВСЕ сеансы считаются ambient."""
    body = request.get_json(force=True)
    with state_lock:
        if "url" in body:
            state["cfg"]["tautulli_url"] = body["url"].strip()
        if "api_key" in body:
            state["cfg"]["tautulli_api_key"] = body["api_key"].strip()
        if "my_plex_user" in body:
            state["cfg"]["my_plex_user"] = body["my_plex_user"].strip()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/osd", methods=["POST"])
def api_osd():
    """Тайминги/цвета OSD-попапов раскладки/устройства + общий кулдаун (см.
    osd.py) - громкость (osd_hold_seconds/mute_color/warning_*) остаётся в
    /api/encoder, как и раньше (обратная совместимость с уже сохранёнными
    settings.json, см. osd._hold_seconds() за обоснованием этого решения).
    layout_colors - точечное обновление ОДНОГО кода раскладки за запрос
    (body: {"layout_colors": {"RU": "FF4500"}}) - тот же паттерн, что и
    encoder.volume_colors (dict.update, не полная замена словаря), чтобы
    правка одного языка в UI не затирала остальные."""
    body = request.get_json(force=True)
    with state_lock:
        if "layout_hold_seconds" in body:
            state["cfg"]["layout_hold_seconds"] = round(max(0.3, min(5.0, float(body["layout_hold_seconds"]))), 1)
        if "device_hold_seconds" in body:
            state["cfg"]["device_hold_seconds"] = round(max(0.5, min(10.0, float(body["device_hold_seconds"]))), 1)
        if "osd_cooldown_seconds" in body:
            state["cfg"]["osd_cooldown_seconds"] = round(max(0.0, min(5.0, float(body["osd_cooldown_seconds"]))), 1)
        if "layout_colors" in body and isinstance(body["layout_colors"], dict):
            for code, color in body["layout_colors"].items():
                if isinstance(color, str) and color:
                    state["cfg"]["layout_colors"][code] = color.upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/priority_boost", methods=["POST"])
def api_priority_boost():
    """"Каждый N-й слот" для priority/ambient дорожек ротации (см.
    screens.RotationState) - нижняя граница 1 (приоритетный/фоновый экран
    получает ВООБЩЕ КАЖДЫЙ слот - предельный случай, отдельно не запрещаем,
    это осмысленная настройка "показывать только это"), верхняя -
    произвольный разумный потолок, чтобы не запутаться в UI.

    Ключи тела: boost_priority, boost_ambient. Прежние имена
    (priority_boost_personal/priority_boost_ambient - до переименования tier
    "personal" -> "priority") принимаются как алиасы: страница /settings
    прежней версии продолжает сохранять настройки, пока её не заменили."""
    body = request.get_json(force=True)
    with state_lock:
        for new_key, legacy_key in (("boost_priority", "priority_boost_personal"),
                                     ("boost_ambient", "priority_boost_ambient")):
            raw = body.get(new_key, body.get(legacy_key))
            if raw is not None:
                state["cfg"][new_key] = max(1, min(20, int(raw)))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/qbittorrent", methods=["POST"])
def api_qbittorrent():
    """Адрес/API-ключ ОБОИХ серверов qBittorrent за один запрос (см.
    metrics_qbittorrent.py) - body: {"qbt1_url":.., "qbt1_api_key":..,
    "qbt2_url":.., "qbt2_api_key":..} - тот же принцип, что и /api/net-ifaces
    (net1_iface/net2_iface одним POST). Опрашивается фоновым потоком
    (integrations_loop), поэтому сохранение тут не требует немедленного
    переподключения."""
    body = request.get_json(force=True)
    with state_lock:
        for key in ("qbt1_url", "qbt1_api_key", "qbt2_url", "qbt2_api_key"):
            if key in body:
                state["cfg"][key] = body[key].strip()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/monitor_targets", methods=["POST"])
def api_monitor_targets():
    """Полная замена списка целей мониторинга (см. metrics_ping.py) - body:
    {"targets": [{"id":.., "label":.., "host":.., "port":..}, ...]}. В
    отличие от /api/screens/* тут НЕТ отдельного CRUD по id (POST create/
    PUT update/DELETE) - список целей маленький и правится в /settings
    целиком за один запрос, тот же принцип, что у layout_colors/карточки OSD:
    фронтенд держит полный список в памяти (как screensCache на /screens) и
    шлёт его целиком при любом изменении строки. _sanitize_mon_targets()
    сама генерирует id для новых строк (см. её докстринг) - фронтенду не
    нужно самому изобретать уникальный id.

    Опрашивается фоновым потоком (monitor_loop, см. ниже), поэтому
    сохранение тут не требует немедленного переподключения - новый список
    целей подхватится на следующем тике monitor_loop (cfg["ping_interval_seconds"])."""
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["mon_targets"] = _sanitize_mon_targets(body.get("targets"))
        save_settings(state["cfg"])
        return jsonify({"targets": state["cfg"]["mon_targets"]})


@app.route("/api/ping_settings", methods=["POST"])
def api_ping_settings():
    """Интервал/таймаут/гистерезис проверки ресурсов (см. monitor_loop() и
    metrics_ping.PingMonitor.read()) - тот же принцип, что /api/priority_boost:
    несколько связанных числовых настроек одним эндпоинтом. Границы интервала
    5с-3600с - нижняя защищает от случайного "пинговать каждую секунду"
    (для внешних/множества ресурсов это лишняя нагрузка на сеть без всякой
    пользы - мониторинг доступности не нуждается в секундной свежести, см.
    обсуждение в чате), верхняя - час, разумный потолок для UI."""
    body = request.get_json(force=True)
    with state_lock:
        if "ping_interval_seconds" in body:
            state["cfg"]["ping_interval_seconds"] = round(max(5.0, min(3600.0, float(body["ping_interval_seconds"]))), 1)
        if "ping_timeout_ms" in body:
            state["cfg"]["ping_timeout_ms"] = max(50, min(10000, int(body["ping_timeout_ms"])))
        if "ping_fail_threshold" in body:
            state["cfg"]["ping_fail_threshold"] = max(1, min(10, int(body["ping_fail_threshold"])))
        if "ping_recover_threshold" in body:
            state["cfg"]["ping_recover_threshold"] = max(1, min(10, int(body["ping_recover_threshold"])))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/encoder", methods=["POST"])
def api_encoder():
    body = request.get_json(force=True)
    with state_lock:
        enc = state["cfg"]["encoder"]
        if "volume_step_pct" in body:
            enc["volume_step_pct"] = max(1, min(20, int(body["volume_step_pct"])))
        if "click_action" in body and body["click_action"] in CLICK_ACTIONS:
            enc["click_action"] = body["click_action"]
        if "osd_hold_seconds" in body:
            enc["osd_hold_seconds"] = round(max(0.5, min(10.0, float(body["osd_hold_seconds"]))), 1)
        if "mute_color" in body:
            enc["mute_color"] = body["mute_color"].upper()
        if "warning_color" in body:
            enc["warning_color"] = body["warning_color"].upper()
        if "warning_threshold_pct" in body:
            enc["warning_threshold_pct"] = max(50, min(100, int(body["warning_threshold_pct"])))
        if "volume_colors" in body:
            for stop in ("c1", "c2", "c3"):
                if stop in body["volume_colors"]:
                    enc["volume_colors"][stop] = body["volume_colors"][stop].upper()
        if "osd_bar_mode" in body and body["osd_bar_mode"] in BAR_MODES:
            enc["osd_bar_mode"] = body["osd_bar_mode"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


screens_webui.register_screens_routes(app, get_context)
settings_webui.register_settings_routes(app)
flash_webui.register_flash_routes(
    app, lambda: state["cfg"]["serial_port"], flashing_event,
    is_serial_free=lambda: not state["serial_connected"],
    get_avrdude_path=lambda: state["cfg"]["avrdude_path"],
)


def run_web():
    # Werkzeug (встроенный dev-сервер Flask) по умолчанию пишет access-лог
    # НА КАЖДЫЙ HTTP-запрос через logging-логгер "werkzeug" (не через
    # print()) - т.к. фронтенд сам опрашивает /api/state и /api/app_log
    # каждые доли секунды (см. refresh()/pollAppLog() в SENSORS_PAGE_HTML),
    # это заваливает "Лог программы" бесполезным шумом вида
    # '127.0.0.1 - - [...] "GET /api/state HTTP/1.1" 200 -'. Поднимаем
    # уровень логгера до ERROR - строки такого уровня не генерируются
    # вовсе, что дешевле и надёжнее, чем текстовый фильтр по паттерну после
    # того, как строка уже сформирована.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app.run(host="127.0.0.1", port=WEB_PORT, use_reloader=False)


# ---------------- главный цикл метрик + serial ----------------

def metrics_main_loop(stop_event):
    print(f"[win-hud-arduino] metrics loop starting, version {SCRIPT_VERSION}", flush=True)

    # COM (comtypes/pycaw) должен быть инициализирован В ЭТОМ ПОТОКЕ до
    # первого обращения к audio_controller - иначе pycaw падает с
    # "Не был произведен вызов CoInitialize" (COM per-thread, а этот цикл
    # живёт в отдельном от главного потоке). См. metrics_windows.init_com_for_thread().
    metrics_windows.init_com_for_thread()

    with state_lock:
        state["cfg"] = load_settings()

    ser = None
    last_reconnect_attempt = 0.0

    prev_net_iface = {"net1": None, "net2": None}
    prev_net_counters = {"net1": (None, None), "net2": (None, None)}
    net_base_counters = {"net1": (None, None), "net2": (None, None)}

    # (read_bytes, write_bytes) с прошлого тика медленных метрик - для
    # расчёта скорости диск I/O (МБ/с) дельтой, тот же паттерн, что и
    # prev_net_counters выше (см. metrics_windows.read_disk_io_counters()).
    prev_disk_io_counters = (None, None)

    peak_trackers = {"bottom": ledbar.PeakHold(), "top": ledbar.PeakHold()}

    rotation = screens.RotationState()
    proto = protocol.ProtocolState(full_resync_seconds=FULL_RESYNC_SECONDS)

    # История для мини-графиков (cpu_graph/ram_graph/... на OLED, см.
    # history.py/variables._graph()) - один инстанс на процесс, живёт тут
    # же, где peak_trackers/rotation/osd_manager - состояние между тиками
    # главного цикла, персистентность на диск не нужна (график истории
    # теряется при перезапуске приложения, как и peak hold).
    metric_history = history.MetricHistory()

    common_metrics = {
        "cpu": 0.0, "ram": 0.0, "gpu": 0.0, "gpu_vram": 0.0, "disk1": 0.0, "disk2": 0.0, "net": 0.0,
        "vu_peak": 0.0, "vu_left": 0.0, "vu_right": 0.0,
    }
    audio_state = {"volume_pct": 0, "volume_muted": "нет", "audio_device_name": "N/A"}
    media_state = {"media_title": None, "media_artist": None, "media_playing": "нет"}
    lines = ["", "", ""]

    # OSD-очередь (громкость/раскладка/устройство вывода) - см. osd.py за
    # унификацией: раньше тут были только osd_active/osd_until под громкость,
    # теперь единый OsdManager с приоритетом и общим кулдауном.
    osd_manager = osd.OsdManager()

    # pixels/bar_state - объявлены ДО цикла и переиспользуются между
    # итерациями НАМЕРЕННО: когда активен OSD-тип с pixels=None (см.
    # osd._render_device) - "не подменять ленту" реализуется буквально как
    # "не переприсваивать эти переменные в этой итерации", то есть лента
    # остаётся ровно такой же, какой её оставила ПРЕДЫДУЩАЯ итерация (обычная
    # метрика/другой OSD) - без явного "запоминания" пришлось бы городить
    # отдельный кэш. Дефолт ниже используется только в теории (на первой же
    # итерации OsdManager пуст, activate ещё никто не успел).
    pixels = ["000000"] * DEFAULT_SETTINGS["leds_count"]
    bar_state = {"mode": "classic", "pixels": pixels, "pct_bottom": 0, "pct_top": None,
                 "osd_active": False, "osd_type": None}

    # watched-value diff для layout/device OSD (см. osd.py про природу этих
    # триггеров - НЕ событие, а сравнение значения тик-к-тику). None -
    # "ещё не было ни одного успешного чтения" - первый тик после старта
    # процесса НЕ должен триггерить popup (иначе при запуске мигнёт
    # бесполезный "сменили на текущий язык/устройство").
    #
    # ВАЖНО: раскладка клавиатуры (watched_layout/watched_layout_pid)
    # проверяется КАЖДЫЙ тик главного цикла (см. блок сразу после чтения
    # serial ниже), А НЕ раз в POLL_INTERVAL, как остальные метрики -
    # иначе OSD-попап смены раскладки ощутимо запаздывал (до POLL_INTERVAL
    # секунд, по умолчанию 1с) относительно реального переключения языка -
    # в отличие от энкодера (событие с платы, тоже разбирается каждый тик),
    # раскладка - watched-value diff, и её нужно было явно вынести из
    # "медленного" блока, чтобы popup срабатывал так же быстро. Само чтение
    # (GetForegroundWindow/GetKeyboardLayout, см.
    # metrics_windows.read_keyboard_state()) - пара дешёвых системных
    # вызовов, лишней нагрузки на TICK_INTERVAL (~25Гц по умолчанию) не
    # создаёт.
    #
    # audio_device_name (watched_device_name) остаётся на прежнем ритме -
    # раз в POLL_INTERVAL, внутри блока медленных метрик ниже: смена
    # устройства вывода не настолько срочное событие (см. её низкий
    # приоритет в osd.OSD_TYPES), и её чтение (pycaw) заметно тяжелее
    # простого чтения раскладки.
    watched_layout = None
    watched_layout_pid = None
    watched_device_name = None
    # Последнее прочитанное значение раскладки - обновляется каждый тик (см.
    # ниже), используется при сборке context() внутри медленного блока -
    # чтобы не читать его там ещё раз.
    keyboard_layout = None

    last_metrics_tick = 0.0
    # last_vu_time - ОТДЕЛЬНЫЙ от last_metrics_tick таймер: VU обновляется
    # каждый тик (TICK_INTERVAL, ~100мс), а не раз в POLL_INTERVAL (1с) как
    # остальные метрики - иначе индикатор реального звука будет заметно
    # дёрганым/с лагом. dt между тиками нужен AudioController.read_vu() для
    # плавного затухания пика (см. VU_RELEASE_SECONDS в metrics_windows.py).
    last_vu_time = time.time()
    read_buffer = ""

    while not stop_event.is_set():
        loop_t0 = time.time()
        now = loop_t0

        with state_lock:
            cfg = copy.deepcopy(state["cfg"])

        # ---- (пере)подключение / отключение на время прошивки ----
        if flashing_event.is_set():
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
            last_reconnect_attempt = now
        elif ser is None and now - last_reconnect_attempt > 5:
            ser = try_open_serial(cfg["serial_port"])
            if ser is not None:
                proto.reset()
            last_reconnect_attempt = now

        # ---- неблокирующее чтение входящих строк (ENC:/BTN:) ----
        if ser is not None and not flashing_event.is_set():
            try:
                waiting = ser.in_waiting
                if waiting:
                    read_buffer += ser.read(waiting).decode("utf-8", errors="ignore")
                    while "\n" in read_buffer:
                        line, read_buffer = read_buffer.split("\n", 1)
                        line_stripped = line.strip()
                        if line_stripped:
                            # Лог serial-обмена для терминала на / - см.
                            # _log_serial() выше. Пишем ЛЮБУЮ непустую строку
                            # от платы, даже если parse_incoming_line() ниже
                            # её не разберёт (serial-мусор на подключении -
                            # это тоже полезно видеть в терминале при отладке).
                            _log_serial("rx", line_stripped)
                        event = protocol.parse_incoming_line(line)
                        if event is None:
                            continue
                        kind, value = event
                        if kind == "encoder":
                            apply_encoder_delta(value, cfg)
                        elif kind == "button":
                            apply_button_click(cfg)
                        # любое событие энкодера/кнопки - показать OSD громкости
                        # через единую очередь (см. osd.py) - push() сам решает,
                        # прервать ли уже показываемый более приоритетный (layout)
                        # popup, встать в очередь, или показаться сразу; общий
                        # кулдаун (cfg["osd_cooldown_seconds"]) тоже внутри push().
                        audio_state = audio_controller.read_state()
                        osd_manager.push(
                            "volume",
                            {"volume_pct": audio_state["volume_pct"], "muted": audio_state["volume_muted"] == "да"},
                            now, cfg,
                        )
            except (serial.SerialException, OSError) as e:
                print(f"[serial] read failed, will reconnect: {e}", flush=True)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
                last_reconnect_attempt = now

        # ---- раскладка клавиатуры: КАЖДЫЙ тик, НЕ раз в POLL_INTERVAL -
        # см. подробное обоснование у объявления watched_layout выше (без
        # этого попап смены раскладки запаздывал на секунду и более).
        # watched-value diff - та же логика 1-в-1, что раньше жила внутри
        # блока медленных метрик, просто перенесена сюда, чтобы срабатывать
        # с частотой TICK_INTERVAL, а не POLL_INTERVAL.
        keyboard_state = metrics_windows.read_keyboard_state()
        keyboard_layout = keyboard_state["keyboard_layout"]
        foreground_pid = keyboard_state["foreground_pid"]

        # ---- watched-value diff: раскладка клавиатуры (см. osd.py) -
        # триггерим popup, только если раскладка ДЕЙСТВИТЕЛЬНО сменилась
        # (не первый тик после старта - watched_layout is not None) И
        # foreground_pid НЕ изменился одновременно с ней - иначе это не
        # реальное переключение языка, а alt-tab между окнами с разной
        # per-window раскладкой (см. подробное обоснование в докстринге
        # metrics_windows.read_keyboard_state()).
        if (
            watched_layout is not None
            and keyboard_layout is not None
            and keyboard_layout != watched_layout
            and foreground_pid == watched_layout_pid
        ):
            osd_manager.push("layout", {"layout": keyboard_layout}, now, cfg)
        watched_layout = keyboard_layout
        watched_layout_pid = foreground_pid

        # ---- медленные метрики: раз в POLL_INTERVAL ----
        if now - last_metrics_tick >= POLL_INTERVAL:
            dt = now - last_metrics_tick if last_metrics_tick else POLL_INTERVAL
            last_metrics_tick = now

            cpu_pct, cpu_pct_core_max, cpu_freq_mhz = metrics_windows.read_cpu_stats()
            ram_pct, ram_used_gb, ram_total_gb = metrics_windows.read_ram_stats()
            gpu_stats = gpu_monitor.read()

            disk1 = metrics_windows.read_disk_usage(cfg["disk1_letter"])
            disk2 = metrics_windows.read_disk_usage(cfg["disk2_letter"])

            disks_ctx = {}
            if cfg["disk1_letter"] and disk1:
                disks_ctx[cfg["disk1_letter"]] = disk1
            if cfg["disk2_letter"] and disk2:
                disks_ctx[cfg["disk2_letter"]] = disk2

            # сеть по слотам net1/net2 (для OLED) + метрика "net" для ленты
            # (использует net1 - отдельного LED-only интерфейса не заводим)
            net_ctx = {"net1": None, "net2": None}
            net_pct = 0.0
            for slot in ("net1", "net2"):
                iface = cfg[f"{slot}_iface"]
                if iface != prev_net_iface[slot]:
                    prev_net_counters[slot] = (None, None)
                    net_base_counters[slot] = (None, None)
                    prev_net_iface[slot] = iface
                if not iface:
                    continue
                rx, tx = metrics_windows.read_iface_counters(iface)
                if rx is None:
                    continue
                prev_rx, prev_tx = prev_net_counters[slot]
                rx_str = format_rate(rx - prev_rx, dt) if prev_rx is not None else "0Kbps"
                tx_str = format_rate(tx - prev_tx, dt) if prev_tx is not None else "0Kbps"
                rx_mbps = rate_mbps(rx - prev_rx, dt) if prev_rx is not None else 0.0
                tx_mbps = rate_mbps(tx - prev_tx, dt) if prev_tx is not None else 0.0

                if prev_rx is not None and dt > 0 and slot == "net1":
                    mbps = (rx - prev_rx) * 8 / 1_000_000 / dt
                    net_pct = max(0.0, min(100.0, mbps / NET_MAX_MBPS * 100.0))

                base_rx, base_tx = net_base_counters[slot]
                if base_rx is None:
                    base_rx, base_tx = rx, tx
                total_rx_str = format_bytes_total(rx - base_rx)
                total_tx_str = format_bytes_total(tx - base_tx)

                prev_net_counters[slot] = (rx, tx)
                net_base_counters[slot] = (base_rx, base_tx)

                net_ctx[slot] = {
                    "name": iface,
                    "speed": format_speed_mbps(metrics_windows.read_iface_speed_mbps(iface)),
                    "rx": rx_str, "tx": tx_str,
                    "total_rx": total_rx_str, "total_tx": total_tx_str,
                    "rx_mbps": rx_mbps, "tx_mbps": tx_mbps,
                }

            # ---- диск I/O (суммарно по всем дискам) - скорость чтения/записи, МБ/с ----
            read_bytes, write_bytes = metrics_windows.read_disk_io_counters()
            if read_bytes is not None and prev_disk_io_counters[0] is not None and dt > 0:
                disk_io_read_mbps = round((read_bytes - prev_disk_io_counters[0]) / (1024 ** 2) / dt, 1)
                disk_io_write_mbps = round((write_bytes - prev_disk_io_counters[1]) / (1024 ** 2) / dt, 1)
            else:
                disk_io_read_mbps = 0.0
                disk_io_write_mbps = 0.0
            if read_bytes is not None:
                prev_disk_io_counters = (read_bytes, write_bytes)

            audio_state = audio_controller.read_state()
            media_state = media_monitor.read()
            # keyboard_layout - уже прочитан ВЫШЕ, КАЖДЫЙ тик (см. блок
            # сразу после чтения serial в начале while) - тут повторно НЕ
            # читается, просто используется в context ниже как есть.

            # ---- watched-value diff: устройство вывода звука (см. osd.py) -
            # без фильтра по окну (переключение устройства не связано с
            # фокусом окна, в отличие от раскладки выше). "N/A" (pycaw
            # недоступен/устройство временно не определилось) не триггерит -
            # иначе временный сбой чтения устройства выглядел бы как "смена".
            if (
                watched_device_name is not None
                and audio_state["audio_device_name"] not in (None, "N/A")
                and audio_state["audio_device_name"] != watched_device_name
            ):
                osd_manager.push("device", {"device_name": audio_state["audio_device_name"]}, now, cfg)
            watched_device_name = audio_state["audio_device_name"]

            # ---- Tautulli (Plex) / qBittorrent / мониторинг ресурсов -
            # ТОЛЬКО чтение уже готового результата фоновых потоков
            # (integrations_loop/monitor_loop), никаких сетевых вызовов
            # прямо тут - см. обоснование у INTEGRATIONS_POLL_INTERVAL/
            # DEFAULT_PING_INTERVAL_SECONDS в шапке файла.
            integrations = get_integrations_state()
            monitor_state = get_monitor_state()

            # Захватываем последнее посчитанное VU-значение ДО того, как
            # common_metrics будет переприсвоен целиком ниже - сам блок VU
            # находится ПОСЛЕ медленных метрик по циклу (см. комментарий там
            # же про "иначе vu-ключи терялись бы") и обновляет common_metrics
            # каждый БЫСТРЫЙ тик, а не раз в POLL_INTERVAL - у истории графика
            # (см. ниже) для минутного тренда более частая запись не нужна,
            # хватает того же ритма, что и у cpu/ram/gpu. Лаг в один тик
            # (~TICK_INTERVAL) для минутного графика незначим.
            prev_vu_peak = common_metrics.get("vu_peak", 0.0)

            common_metrics = {
                "cpu": cpu_pct, "ram": ram_pct,
                "gpu": gpu_stats["gpu_pct"], "gpu_vram": gpu_stats["gpu_vram_pct"],
                "disk1": disk1["used_pct"] if disk1 else 0.0,
                "disk2": disk2["used_pct"] if disk2 else 0.0,
                "net": net_pct,
            }

            # История для мини-графиков (cpu_graph/ram_graph/... - см.
            # history.py/variables._graph()) - раз в POLL_INTERVAL, тем же
            # ритмом, что и сами common_metrics выше (а не каждый быстрый
            # тик, как VU для ленты) - при том же размере буфера
            # (history._BUFFER_MAXLEN) это даёт заметно больший реальный
            # охват по времени, а минутному тренду секундная точность
            # избыточна. Простой цикл по common_metrics.items() автоматически
            # подхватит любую метрику, которую добавят сюда в будущем -
            # отдельного списка ключей поддерживать не нужно.
            for metric_key, metric_value in common_metrics.items():
                metric_history.record(metric_key, metric_value, now=now)
            metric_history.record("vu_peak", prev_vu_peak, now=now)

            context = {
                "cpu_pct": round(cpu_pct), "cpu_pct_core_max": round(cpu_pct_core_max),
                "cpu_freq_mhz": cpu_freq_mhz,
                "ram_pct": round(ram_pct), "ram_used_gb": ram_used_gb, "ram_total_gb": ram_total_gb,
                "gpu_name": gpu_stats["gpu_name"], "gpu_pct": round(gpu_stats["gpu_pct"]),
                "gpu_temp_c": gpu_stats["gpu_temp_c"],
                "gpu_vram_used_gb": gpu_stats["gpu_vram_used_gb"], "gpu_vram_total_gb": gpu_stats["gpu_vram_total_gb"],
                "gpu_vram_pct": round(gpu_stats["gpu_vram_pct"]), "gpu_power_w": gpu_stats["gpu_power_w"],
                "disk_slots": {"disk1_letter": cfg["disk1_letter"], "disk2_letter": cfg["disk2_letter"]},
                "disks": disks_ctx,
                "disk_io_read_mbps": disk_io_read_mbps, "disk_io_write_mbps": disk_io_write_mbps,
                "net": net_ctx,
                "uptime": format_duration(time.time() - _boot_time()),
                "container_uptime": format_duration(now - CONTAINER_START_TIME),
                "time_now": time.strftime("%H:%M"),
                "weekday_name": format_weekday_name(),
                "date_now": time.strftime("%d.%m"),
                "year_now": time.strftime("%Y"),
                # top_process_name/top_process_cpu_pct/top_process_ram_pct -
                # НЕ читаются тут напрямую (см. удалённый top_process_monitor.read()
                # выше) - приходят через **integrations ниже, т.к. опрос
                # переехал в integrations_loop (см. пояснение у
                # _integrations_state/integrations_loop).
                "volume_pct": audio_state["volume_pct"], "volume_muted": audio_state["volume_muted"],
                "audio_device_name": audio_state["audio_device_name"],
                # VU (реальный уровень звука) для OLED-шаблонов - берём уже
                # посчитанное значение из common_metrics (обновляется каждый
                # тик ниже по циклу, см. блок "VU" после медленных метрик) -
                # отдельный COM-вызов тут не нужен, лаг не больше одного тика
                # (~TICK_INTERVAL), для текстового экрана это незаметно.
                "vu_peak_pct": round(common_metrics.get("vu_peak", 0.0)),
                "vu_left_pct": round(common_metrics.get("vu_left", 0.0)),
                "vu_right_pct": round(common_metrics.get("vu_right", 0.0)),
                "keyboard_layout": keyboard_layout,
                "media_title": media_state["media_title"],
                "media_artist": media_state["media_artist"],
                "media_playing": media_state["media_playing"],
                # my_plex_user - НЕ переменная OLED-шаблонов (не зарегистрирована
                # в variables.VARIABLES, не появится в легенде /screens) -
                # используется ТОЛЬКО screens.build_active_screens() для
                # point-override tier у отдельных копий repeating-группы
                # "stream" (см. обсуждение в чате про "свой/чужой Plex-сеанс"
                # и докстринг build_active_screens() в screens.py).
                "my_plex_user": cfg.get("my_plex_user", ""),
                # metric_history - тот же служебный, не-переменная-шаблона
                # ключ context, что и my_plex_user выше - используется ТОЛЬКО
                # резолверами *_graph (см. variables._graph()) для доступа к
                # накопленной истории cpu/ram/gpu/... за последние секунды,
                # сам по себе переменной шаблона не является и в легенде на
                # /screens не появится (не зарегистрирован в variables.VARIABLES).
                "metric_history": metric_history,
                # Plex (через Tautulli) + qBittorrent - integrations уже
                # содержит РОВНО те ключи, что ожидают резолверы variables.py
                # (plex_*/streams/recent/qbt_*/torrents) - см.
                # get_integrations_state()/integrations_loop() ниже.
                **integrations,
                # Мониторинг ресурсов (ping/TCP, см. metrics_ping.py) -
                # monitor_state уже содержит РОВНО те ключи, что ожидают
                # резолверы variables.py (mon/mon_down_count/mon_down_names) -
                # см. get_monitor_state()/monitor_loop() ниже.
                **monitor_state,
            }
            with _context_lock:
                _last_context.clear()
                _last_context.update(context)

            current_screens = screens_webui.get_screens()
            # Гейт по OsdManager - если сейчас показывается ЛЮБОЙ OSD-попап
            # (громкость/раскладка/устройство), rotation.current_lines() НЕ
            # вызывается вовсе: её внутренние курсоры/таймеры/started_at не
            # двигаются, ротация реально "стоит на паузе" (а не просто её
            # результат визуально перезаписывается, как было раньше только
            # для громкости) - продолжится с того же места сама, как только
            # OSD-очередь опустеет. osd_manager.tick(now, cfg) тут ничего не
            # меняет состояние очереди даже при повторном вызове с тем же
            # now (см. осд.py) - тот же теккущий/следующий popup будет ещё
            # раз прочитан ниже, в блоке расчёта ленты, идемпотентно.
            if osd_manager.tick(now, cfg) is None:
                lines = rotation.current_lines(
                    current_screens, context, now=now,
                    boost_priority=cfg.get("boost_priority", DEFAULT_BOOST_PRIORITY),
                    boost_ambient=cfg.get("boost_ambient", DEFAULT_BOOST_AMBIENT),
                )
        #    with state_lock:
        #        state["oled_lines"] = lines

        # ---- VU (реальный уровень звука): каждый тик, НЕ раз в POLL_INTERVAL -
        # иначе индикатор ощутимо дёргается/лагает при интервале в секунду.
        # dt считаем по факту прошедшего времени между итерациями (а не
        # "теоретический" TICK_INTERVAL) - на случай, если предыдущая
        # итерация подвисла на serial write/read. Пишем в common_metrics
        # ПОСЛЕ блока медленных метрик выше - там common_metrics иногда
        # переприсваивается целиком, и vu-ключи иначе терялись бы до
        # следующего POLL_INTERVAL.
        vu_dt = now - last_vu_time
        last_vu_time = now
        try:
            vu_state = audio_controller.read_vu(dt=vu_dt if vu_dt > 0 else cfg.get("tick_interval", TICK_INTERVAL))
        except Exception as e:
            # Страховка: на реальном запуске необработанное исключение
            # именно отсюда (AttributeError из-за неполного объявления
            # IAudioMeterInformation в pycaw - см. metrics_windows.py) убило
            # ВЕСЬ поток metrics_main_loop целиком, а не только VU - экран
            # переставал обновляться вообще (CPU/RAM/лента/OLED - всё
            # замирало). read_vu() теперь сама не должна бросать исключения,
            # но эта обвязка - защита именно от того, чтобы ЛЮБАЯ будущая
            # ошибка в чтении звука не могла повторить тот же сценарий.
            print(f"[audio] read_vu() unexpected error, VU отключён на этот тик: {e}", flush=True)
            vu_state = {"vu_peak_pct": 0.0, "vu_left_pct": 0.0, "vu_right_pct": 0.0}
        common_metrics["vu_peak"] = vu_state["vu_peak_pct"]
        common_metrics["vu_left"] = vu_state["vu_left_pct"]
        common_metrics["vu_right"] = vu_state["vu_right_pct"]

        # ---- лента: OSD popup (громкость/раскладка/устройство) ИЛИ обычная
        # метрика (каждый тик) - см. osd.py за унификацией трёх типов ----
        leds_count = cfg["leds_count"]
        osd_result = osd_manager.tick(now, cfg)

        if osd_result is not None:
            # ЛЮБОЙ активный OSD-тип - OLED полностью заменяется попапом.
            # Лента подменяется, ТОЛЬКО если рендерер это предусмотрел (см.
            # osd.OSD_TYPES - у "device" render возвращает pixels=None,
            # означающее "не трогай ленту"). В этом случае pixels/bar_state
            # НЕ переприсваиваются вовсе и остаются такими, какими их
            # оставила ПРЕДЫДУЩАЯ итерация (см. их объявление до while) -
            # "не подменять" реализовано буквально, без отдельного кэша.
            #
            # peak_trackers/обычная метрика бара (см. else-ветку ниже) НЕ
            # пересчитываются, пока показывается любой OSD - та же пауза,
            # что раньше была только у громкости (после окончания OSD
            # peak hold продолжит отсчёт от значения ДО паузы, не от
            # накопленного "в фоне" - это осознанное поведение, тот же
            # компромисс, что был и в прежнем коде).
            osd_lines, osd_pixels = osd_manager.render(osd_result["type"], osd_result["payload"], cfg, leds_count)
            lines = osd_lines
            if osd_pixels is not None:
                pixels = osd_pixels
                # pct_bottom - для превью на / (см. SENSORS_PAGE_HTML ниже) -
                # volume_pct для типа "volume", иначе просто "полная шкала"
                # (100) ради вменяемого числа в UI, содержательного смысла
                # как у обычных метрик тут нет (см. osd._render_layout).
                pct_display = osd_result["payload"].get("volume_pct", 100)
                bar_state = {
                    "mode": f"{osd_result['type']}_osd", "pixels": pixels,
                    "pct_bottom": pct_display, "pct_top": None,
                    "osd_active": True, "osd_type": osd_result["type"],
                }
            else:
                bar_state = dict(bar_state)
                bar_state["osd_active"] = True
                bar_state["osd_type"] = osd_result["type"]
        else:
            bar_mode = cfg["mode"]["bar0"]
            peak_info = cfg["peak"]["bar0"]
            peak_enabled = peak_info["enabled"]

            peak_trackers["bottom"].set_style(peak_info["style"])
            peak_trackers["bottom"].set_timings(cfg["peak_hold_seconds"], cfg["peak_fade_seconds"])

            pct_bottom = round(common_metrics.get(cfg["assignment"]["bar0"], 0))
            bottom_peak = peak_trackers["bottom"].update(pct_bottom, now)

            if bar_mode in ("center", "edges"):
                # center и edges - геометрически одна и та же пара половин
                # (те же assignment_top/colors_top/solid_top, тот же
                # top-трекер peak hold) - отличается только сама функция
                # расчёта пикселей в ledbar.py (направление роста внутри
                # половины). См. докстринг ledbar.compute_bar_pixels_edges().
                peak_trackers["top"].set_style(peak_info["style"])
                peak_trackers["top"].set_timings(cfg["peak_hold_seconds"], cfg["peak_fade_seconds"])

                pct_top = round(common_metrics.get(cfg["assignment_top"]["bar0"], 0))
                top_peak = peak_trackers["top"].update(pct_top, now)

                compute_fn = ledbar.compute_bar_pixels_center if bar_mode == "center" else ledbar.compute_bar_pixels_edges
                pixels = compute_fn(
                    pct_bottom, pct_top,
                    cfg["colors"]["bar0"]["c1"], cfg["colors"]["bar0"]["c2"], cfg["colors"]["bar0"]["c3"], cfg["solid"]["bar0"],
                    cfg["colors_top"]["bar0"]["c1"], cfg["colors_top"]["bar0"]["c2"], cfg["colors_top"]["bar0"]["c3"], cfg["solid_top"]["bar0"],
                    leds_per_bar=leds_count,
                    peak_pct_bottom=bottom_peak if peak_enabled else None,
                    peak_pct_top=top_peak if peak_enabled else None,
                )
                bar_state = {"mode": bar_mode, "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": pct_top,
                             "osd_active": False, "osd_type": None}
            elif bar_mode == "flat":
                # flat - однометричный режим (как classic - только нижняя/
                # единственная метрика assignment, без top-половины), но без
                # peak hold: у "заливки всей ленты одним цветом" нет позиции,
                # куда ставить точку недавнего максимума (см. докстринг
                # ledbar.compute_bar_pixels_flat()) - top-трекер и
                # peak_enabled тут осознанно не используются.
                pixels = ledbar.compute_bar_pixels_flat(
                    pct_bottom,
                    cfg["colors"]["bar0"]["c1"], cfg["colors"]["bar0"]["c2"], cfg["colors"]["bar0"]["c3"],
                    leds_per_bar=leds_count,
                )
                bar_state = {"mode": "flat", "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": None,
                             "osd_active": False, "osd_type": None}
            else:
                pixels = ledbar.compute_bar_pixels(
                    pct_bottom, cfg["colors"]["bar0"]["c1"], cfg["colors"]["bar0"]["c2"], cfg["colors"]["bar0"]["c3"], cfg["solid"]["bar0"],
                    leds_per_bar=leds_count,
                    peak_pct=bottom_peak if peak_enabled else None,
                )
                bar_state = {"mode": "classic", "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": None,
                             "osd_active": False, "osd_type": None}

        if cfg.get("leds_reverse"):
            # Физический реверс - см. докстринг leds_reverse в
            # DEFAULT_SETTINGS выше. Разворачиваем УЖЕ ГОТОВЫЙ список
            # пикселей здесь, ОДИН РАЗ, для результата ЛЮБОЙ ветки выше
            # (OSD громкости и все режимы classic/center/edges/flat) -
            # переприсваиваем и pixels (используется ниже при упаковке в
            # BAR: для платы), и bar_state["pixels"] (уходит в state["bar"]
            # для превью на /), чтобы превью на сайте всегда совпадало с
            # тем, что реально отправляется на плату.
            pixels = list(reversed(pixels))
            bar_state["pixels"] = pixels

        with state_lock:
            state["bar"] = bar_state
            state["oled_lines"] = lines

        # ---- собрать и отправить serial-строку ----
        proto_values = {
            "BAR": protocol.pack_bar_pixels(pixels),
            "BRI": str(cfg["brightness"]),
            "CON": str(cfg["contrast"]),
            "L1": lines[0], "L2": lines[1], "L3": lines[2],
        }
        line_to_send = proto.build(proto_values, now=now)

        if ser is not None and not flashing_event.is_set() and line_to_send is not None:
            try:
                ser.write((line_to_send + "\n").encode("utf-8"))
                _log_serial("tx", line_to_send)
            except (serial.SerialException, OSError) as e:
                print(f"[serial] write failed, will reconnect: {e}", flush=True)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
                last_reconnect_attempt = now

        elapsed = time.time() - loop_t0
        # cfg["tick_interval"] - живая настройка из /settings (см.
        # /api/tick_interval и DEFAULT_SETTINGS выше), а не статическая
        # TICK_INTERVAL - cfg уже перечитывается из state в начале КАЖДОЙ
        # итерации цикла (см. "cfg = copy.deepcopy(state[\"cfg\"])" в самом
        # начале while), поэтому смена значения в /settings подхватывается
        # на следующем же тике, без перезапуска pc_hud.py. TICK_INTERVAL
        # (env var) используется только как дефолт при самом первом запуске
        # (см. DEFAULT_SETTINGS) - .get() тут на случай уже сохранённого
        # settings.json от версии ДО этой настройки (там ключа ещё нет).
        tick_interval = cfg.get("tick_interval", TICK_INTERVAL)
        stop_event.wait(timeout=max(0.0, tick_interval - elapsed))

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
    print("[win-hud-arduino] metrics loop stopped", flush=True)


def integrations_loop(stop_event):
    """
    Опрашивает Tautulli (Plex), qBittorrent И топ-процесс по CPU - см.
    metrics_tautulli.py/metrics_qbittorrent.py/metrics_windows.TopProcessMonitor -
    в ОТДЕЛЬНОМ от metrics_main_loop потоке, своим интервалом
    (INTEGRATIONS_POLL_INTERVAL, см. шапку файла). Результат кладётся в
    _integrations_state под _integrations_lock; metrics_main_loop только
    читает его (get_integrations_state()) - ни одного сетевого вызова и
    ни одного тяжёлого psutil-обхода процессов в главном цикле.

    ПОЧЕМУ ТУТ ЖИВЁТ ТОП-ПРОЦЕСС (не сетевая интеграция, но переехал сюда же):
    top_process_monitor.read() раньше вызывался прямо в metrics_main_loop,
    в том же блоке "раз в POLL_INTERVAL", что и остальные метрики. На
    практике на Windows psutil.Process.cpu_percent()/memory_percent()/
    name() на КАЖДЫЙ процесс в системе (а _procs со временем накапливает
    их все, не только "топ-N") оказались достаточно дорогими, чтобы этот
    вызов занимал заметную долю секунды. Поскольку это происходило раз в
    POLL_INTERVAL (1с по умолчанию) ВНУТРИ главного цикла - того же цикла,
    что шлёт BAR/читает serial/обновляет VU на каждый tick_interval - лента
    и VU-метр фактически переставали успевать обновляться быстрее ~1 Гц,
    независимо от значения слайдера "Частота опроса" в /settings (см. отчёт
    Konstantin: "лента обновляется медленно, VU раз в секунду"). Тот же
    класс проблемы, что уже решался для read_vu() (см. комментарий в
    metrics_windows.py) и для самих Tautulli/qBittorrent (см. обоснование у
    INTEGRATIONS_POLL_INTERVAL выше) - секундная свежесть топ-процессу тоже
    не нужна, поэтому он просто присоединился к этому же фоновому потоку и
    интервалу, а не заводит третий отдельный поток ради одной метрики.

    TautulliClient/QbittorrentClient создаются ЗДЕСЬ, а не на уровне
    модуля - у обоих есть внутреннее состояние между вызовами (кэш библиотек
    у Tautulli, сессионная cookie у qBittorrent), которое должно жить в
    ОДНОМ потоке последовательно, а не делиться с чем-либо ещё.
    top_process_monitor, в отличие от них, СОЗДАЁТСЯ на уровне модуля (см.
    выше, рядом с gpu_monitor/audio_controller/media_monitor) - раньше он
    вызывался из metrics_main_loop, теперь исключительно отсюда; смены
    треда, из которого идут обращения к нему, достаточно - никакой гонки
    не возникает, т.к. второй читатель этого инстанса не появился.
    """
    tautulli_client = metrics_tautulli.TautulliClient()
    qbt_client = metrics_qbittorrent.QbittorrentClient()

    while not stop_event.is_set():
        with state_lock:
            cfg = copy.deepcopy(state["cfg"])

        tautulli_data = tautulli_client.read(cfg["tautulli_url"], cfg["tautulli_api_key"])
        qbt_servers = [
            {"url": cfg["qbt1_url"], "api_key": cfg["qbt1_api_key"]},
            {"url": cfg["qbt2_url"], "api_key": cfg["qbt2_api_key"]},
        ]
        qbt_data = qbt_client.read(qbt_servers)
        # Без порога - монитор всегда отдаёт реальный топ-процесс, "показывать
        # ли его" решает пороговое условие экрана (см. screens.py conditions).
        top_process_data = top_process_monitor.read()

        with _integrations_lock:
            _integrations_state.update(tautulli_data)
            _integrations_state.update(qbt_data)
            _integrations_state.update(top_process_data)

        stop_event.wait(timeout=INTEGRATIONS_POLL_INTERVAL)


def monitor_loop(stop_event):
    """
    Опрашивает произвольные ресурсы (ping/TCP-порт, см. metrics_ping.py) -
    ОТДЕЛЬНЫЙ от integrations_loop фоновый поток, СВОИМ интервалом
    (cfg["ping_interval_seconds"], минуты, а не секунды - живая настройка в
    /settings, см. DEFAULT_PING_INTERVAL_SECONDS в шапке файла). Не смешан с
    Tautulli/qBittorrent намеренно - см. обсуждение в чате: у мониторинга
    ресурсов принципиально другой, гораздо более редкий ритм, общий поток с
    integrations_loop заставил бы либо делить один интервал на всех, либо
    городить второй таймер внутри одного потока - отдельный поток проще.

    PingMonitor создаётся ЗДЕСЬ, а не на уровне модуля - как и
    TautulliClient/QbittorrentClient в integrations_loop выше, у него есть
    внутреннее состояние МЕЖДУ вызовами (гистерезис по каждой цели, см.
    metrics_ping._TargetState) - оно должно обновляться последовательно
    ОДНИМ потоком, а не делиться с чем-либо ещё.

    Результат кладётся в _monitor_state под _monitor_lock; metrics_main_loop
    только читает его (get_monitor_state()) - ни одного subprocess/socket-
    вызова в главном цикле.
    """
    ping_monitor = metrics_ping.PingMonitor()

    while not stop_event.is_set():
        with state_lock:
            cfg = copy.deepcopy(state["cfg"])

        result = ping_monitor.read(
            cfg.get("mon_targets", []),
            timeout_ms=cfg.get("ping_timeout_ms", DEFAULT_PING_TIMEOUT_MS),
            fail_threshold=cfg.get("ping_fail_threshold", DEFAULT_PING_FAIL_THRESHOLD),
            recover_threshold=cfg.get("ping_recover_threshold", DEFAULT_PING_RECOVER_THRESHOLD),
        )

        with _monitor_lock:
            _monitor_state.update(result)

        # Интервал - живая настройка, перечитывается из cfg КАЖДЫЙ виток (та
        # же логика, что у tick_interval в metrics_main_loop) - смена
        # значения в /settings подхватывается на следующем цикле опроса, без
        # перезапуска потока. Не вычитаем время, потраченное на сам read()
        # (может занять заметную долю секунды при многих целях и таймаутах) -
        # тот же осознанно простой подход, что и у integrations_loop выше.
        interval = max(5.0, float(cfg.get("ping_interval_seconds", DEFAULT_PING_INTERVAL_SECONDS)))
        stop_event.wait(timeout=interval)


def _boot_time():
    import psutil
    return psutil.boot_time()


# ---------------- трей-иконка ----------------

def build_tray_icon(stop_event):
    import pystray
    from PIL import Image

    image = Image.open(ICON_PATH)

    def on_open(icon, item):
        webbrowser.open(f"http://127.0.0.1:{WEB_PORT}/")

    def on_quit(icon, item):
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть панель", on_open, default=True),
        pystray.MenuItem("Выход", on_quit),
    )
    return pystray.Icon("win-hud-arduino", image, "win-hud-arduino", menu)


def main():
    # Перехват stdout/stderr ДО первых print() - см. _StdoutTee/_log_app()
    # выше: это единственное место, где устанавливается тея, дальше ЛЮБОЙ
    # print(...) по всему проекту (в этом файле и в остальных модулях)
    # автоматически питает терминал "Лог программы" на /, без единой правки
    # в самих модулях. Оригинальный sys.stdout может быть None при сборке
    # --windowed без консоли - _StdoutTee.write()/flush() на этот случай
    # просто проглатывают ошибку записи в оригинал и продолжают логировать
    # в буфер.
    sys.stdout = _StdoutTee(sys.stdout)
    sys.stderr = _StdoutTee(sys.stderr)

    print(f"[win-hud-arduino] starting, version {SCRIPT_VERSION}", flush=True)
    _ensure_assets()

    stop_event = threading.Event()
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=metrics_main_loop, args=(stop_event,), daemon=True).start()
    threading.Thread(target=integrations_loop, args=(stop_event,), daemon=True).start()
    threading.Thread(target=monitor_loop, args=(stop_event,), daemon=True).start()

    icon = build_tray_icon(stop_event)
    icon.run()  # блокирует главный поток, пока не нажмут "Выход"

    stop_event.set()
    time.sleep(0.3)
    gpu_monitor.shutdown()
    print("[win-hud-arduino] stopped", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
