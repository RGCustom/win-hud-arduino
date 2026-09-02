"""
screens.py  (win-hud-arduino)

Хранилище OLED-экранов + логика ротации - портировано из shkaf-hud. Движок
(CRUD/build_active_screens/RotationState) не изменился НИ СТРОЧКОЙ логики:
он уже был написан общим - опирается на templates.template_group() и
variables.group_count(), а не на конкретные переменные проекта. В
win-hud-arduino повторяющихся групп нет вообще (variables.REPEATING_GROUPS
пуст), поэтому build_active_screens() тут всегда идёт по "обычной" (не
repeating) ветке рендера - никакого экрана в N копий, никакого "0 элементов -
пропустить экран целиком".

Изменилось только:
  - DEFAULT_SCREENS - под новые переменные (CPU/RAM/GPU/диски/сеть/звук/
    раскладка вместо Cache/Array/Plex/qBittorrent)
  - CONFIG_DIR - дефолт под Windows (%APPDATA%\\win-hud-arduino), не /config
    докер-тома
"""

import json
import os
import time
import uuid

import templates
import variables

# На Windows %APPDATA% всегда есть (обычно C:\Users\<user>\AppData\Roaming) -
# берём его как базу для конфига, аналог CONFIG_DIR=/config в shkaf-hud.
# CONFIG_DIR всё равно можно переопределить переменной окружения, если нужно
# хранить конфиг в другом месте.
_DEFAULT_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "win-hud-arduino")
CONFIG_DIR = os.environ.get("CONFIG_DIR", _DEFAULT_CONFIG_DIR)
SCREENS_FILE = os.path.join(CONFIG_DIR, "screens.json")

DEFAULT_SCREENS = [
    {
        "id": "default-cpuram",
        "name": "CPU/RAM",
        "l1": "CPU {cpu_pct}%",
        "l2": "RAM {ram_pct}%",
        "l3": "{cpu_freq_mhz}MHz",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-gpu",
        "name": "GPU",
        "l1": "{gpu_name:16}",
        "l2": "GPU {gpu_pct}% {gpu_temp_c:.0f}C",
        "l3": "VRAM {gpu_vram_pct}%",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-disks",
        "name": "Disks",
        "l1": "Disk {disk1_letter}: {disk1_used_pct}%",
        "l2": "Disk {disk2_letter}: {disk2_used_pct}%",
        "l3": "Free {disk1_free_gb:.0f}GB",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-net1",
        "name": "Network",
        "l1": "{net1_name} {net1_speed}",
        "l2": "\u2193 {net1_rx}",
        "l3": "\u2191 {net1_tx}",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-audio",
        "name": "Audio",
        "l1": "Vol {volume_pct}%  {volume_muted}",
        "l2": "{audio_device_name:16}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
    },
    {
        # media_title/media_artist резолвятся в None, когда сейчас ничего не
        # играет (см. metrics_windows.MediaMonitor) - экран автоматически
        # выпадает из ротации через общий механизм build_active_screens()
        # ниже, отдельной логики "показывать только когда играет" тут нет.
        "id": "default-nowplaying",
        "name": "Now Playing",
        "l1": "{media_title:16}",
        "l2": "{media_artist:16}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
    },
    {
        "id": "default-clock",
        "name": "Clock",
        "l1": "{time_now}   [{keyboard_layout}]",
        "l2": "Uptime {uptime}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
    },
]


def load_screens():
    try:
        with open(SCREENS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return data
    except Exception:
        pass
    return [dict(s) for s in DEFAULT_SCREENS]


def save_screens(screens):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SCREENS_FILE, "w") as f:
        json.dump(screens, f)


# ---------------- CRUD (без изменений относительно shkaf-hud) ----------------

def new_screen(name="New screen", l1="", l2="", l3="", duration=4.0):
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "l1": l1, "l2": l2, "l3": l3,
        "duration": max(1.0, float(duration)),
        "enabled": True,
    }


def create_screen(screens, data):
    screen = new_screen(
        name=data.get("name", "New screen"),
        l1=data.get("l1", ""), l2=data.get("l2", ""), l3=data.get("l3", ""),
        duration=data.get("duration", 4.0),
    )
    screens.append(screen)
    return screens, screen


def update_screen(screens, screen_id, data):
    for s in screens:
        if s["id"] == screen_id:
            for field in ("name", "l1", "l2", "l3"):
                if field in data:
                    s[field] = data[field]
            if "duration" in data:
                s["duration"] = max(1.0, float(data["duration"]))
            if "enabled" in data:
                s["enabled"] = bool(data["enabled"])
            return screens, s
    return screens, None


def delete_screen(screens, screen_id):
    return [s for s in screens if s["id"] != screen_id]


def reorder_screens(screens, id_order):
    by_id = {s["id"]: s for s in screens}
    reordered = [by_id[i] for i in id_order if i in by_id]
    missing = [s for s in screens if s["id"] not in id_order]
    return reordered + missing


# ---------------- рендер активного списка (без изменений логики) ----------------

def build_active_screens(screens, context):
    """
    Возвращает список готовых к показу экранов:
        [{"screen_id": ..., "lines": [l1,l2,l3], "duration": float}, ...]

    В win-hud-arduino повторяющихся групп нет (см. шапку файла) - в
    результате templates.template_group() всегда возвращает пустой set(),
    и каждый экран идёт по ветке "обычный (нерепитящийся)". Ветка с
    разворачиванием в N копий оставлена нетронутой ради совместимости
    (общий код с shkaf-hud) - она просто никогда не выполнится, пока в
    variables.py не появится хотя бы одна group != "scalar" переменная.

    ВАЖНО - как правильно делать "экран не включается, если ..." (портировано
    из shkaf-hud, см. пример disk1/disk2/net1/net2 - экран гаснет, если буква
    диска/интерфейс не выбраны в /settings; и media - экран гаснет, если
    сейчас ничего не играет, см. metrics_windows.MediaMonitor):

    Экран автоматически выпадает из ротации, если ХОТЯ БЫ ОДНА переменная в
    его l1/l2/l3 резолвится в None (см. ok1/ok2/ok3 ниже - all_resolved из
    templates.render()). НЕ пишите условие видимости экрана здесь, в
    screens.py - вместо этого resolver соответствующей переменной (в
    variables.py) или, чаще, источник данных в context (metrics_windows.py/
    pc_hud.py) должен класть None именно в тот момент, когда данных "нет по
    смыслу" (а не только когда их технически не удалось прочитать). Дальше
    этот же общий механизм сработает сам - для ЛЮБОГО будущего экрана,
    условного или нет, без специального кода тут.
    """
    active = []

    for screen in screens:
        if not screen.get("enabled", True):
            continue

        l1, l2, l3 = screen.get("l1", ""), screen.get("l2", ""), screen.get("l3", "")
        groups = set()
        for tpl in (l1, l2, l3):
            groups |= templates.template_group(tpl)

        if len(groups) > 1:
            continue

        if not groups:
            r1, ok1 = templates.render(l1, context)
            r2, ok2 = templates.render(l2, context)
            r3, ok3 = templates.render(l3, context)
            if ok1 and ok2 and ok3:
                active.append({"screen_id": screen["id"], "lines": [r1, r2, r3], "duration": screen["duration"]})
            continue

        # недостижимая ветка в win-hud-arduino (см. докстринг выше) - оставлена
        # для совместимости с общим кодом, на случай если позже появится
        # повторяющаяся группа переменных
        group_name = next(iter(groups))
        count = variables.group_count(group_name, context)
        for idx in range(count):
            r1, ok1 = templates.render(l1, context, index=idx)
            r2, ok2 = templates.render(l2, context, index=idx)
            r3, ok3 = templates.render(l3, context, index=idx)
            if ok1 and ok2 and ok3:
                active.append({
                    "screen_id": f"{screen['id']}#{idx}",
                    "lines": [r1, r2, r3],
                    "duration": screen["duration"],
                })

    return active


# ---------------- ротация (без изменений) ----------------

class RotationState:
    """Живёт в памяти главного цикла - продвигает текущий экран по его
    собственному duration, переживает изменение длины активного списка
    между тиками."""

    def __init__(self):
        self.index = 0
        self.switched_at = time.time()

    def current_lines(self, screens, context, now=None):
        now = now if now is not None else time.time()
        active = build_active_screens(screens, context)

        if not active:
            return ["", "", ""]

        if self.index >= len(active):
            self.index = 0
            self.switched_at = now

        current = active[self.index]

        if now - self.switched_at >= current["duration"]:
            self.index = (self.index + 1) % len(active)
            self.switched_at = now
            current = active[self.index]

        return current["lines"]
