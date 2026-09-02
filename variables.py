"""
variables.py  (win-hud-arduino)

Реестр переменных для OLED-шаблонов (L1/L2/L3) - портировано из проекта
shkaf-hud, но под метрики Windows-PC вместо Unraid/Tautulli/qBittorrent.

Ничего сам не собирает - работает поверх "context": обычного словаря с уже
готовыми данными, который раз в тик формирует главный скрипт (pc_hud.py) и
передаёт сюда. Здесь только словарь "имя переменной -> как её достать из
context", плюс легенда для веб-интерфейса (те же /api/variables, /screens,
что и в shkaf-hud - код screens_webui.py/templates.py переехал без правок).

Два вида переменных:
  - "scalar" - одно значение, всегда одно и то же (cpu_pct, gpu_temp_c и т.п.)
  - служебные списки (сеть net1/net2, диски) - тоже scalar, просто с
    вложенным путём в context (см. _net_field/_disk_field)

В отличие от shkaf-hud, здесь НЕТ повторяющихся групп (Plex-стримы,
qBittorrent-торренты) - весь Media/qBittorrent пласт с этого PC не снимается,
поэтому REPEATING_GROUPS ниже пустой, а screens.py/templates.py (общий с
shkaf-hud код) просто никогда не пойдёт по repeating-ветке рендера.
"""

# ---------------- структура context (для справки) ----------------
#
# context = {
#     "cpu_pct": float, "cpu_pct_core_max": float, "cpu_freq_mhz": float|None,
#     "ram_pct": float, "ram_used_gb": float, "ram_total_gb": float,
#
#     "gpu_pct": float, "gpu_temp_c": float|None,
#     "gpu_vram_used_gb": float, "gpu_vram_total_gb": float,
#     "gpu_power_w": float|None, "gpu_name": str,
#
#     "disks": {
#         "C": {"used_pct": float, "free_gb": float, "total_gb": float},
#         "D": {...}, ...
#     },
#
#     "net": {
#         "net1": {"name": str, "speed": str, "rx": str, "tx": str,
#                   "total_rx": str, "total_tx": str} | None,
#         "net2": {...} | None,
#     },
#
#     "uptime": str, "container_uptime": str, "time_now": str,
#
#     "volume_pct": int, "volume_muted": str,   # "да"/"нет" - уже отформатировано
#     "audio_device_name": str,
#
#     "keyboard_layout": str,   # глобальная системная раскладка, напр. "RU"/"EN"
#
#     "media_title": str|None, "media_artist": str|None,   # None, если сейчас
#                                # ничего не играет (в т.ч. на паузе) - см.
#                                # metrics_windows.MediaMonitor
#     "media_playing": str,     # "да"/"нет" - уже отформатировано
# }


def _scalar(path):
    """path вида 'gpu_vram_used_gb' или 'net.net1.rx' - достаёт значение из
    context по цепочке ключей (разделитель '.')."""
    keys = path.split(".")

    def resolver(context, index=None):
        val = context
        for k in keys:
            if val is None:
                return None
            val = val.get(k)
        return val

    return resolver


def _net_field(slot, field):
    """slot='net1'|'net2' - context['net'][slot][field], None если интерфейс
    не выбран/недоступен (аналогично shkaf-hud)."""

    def resolver(context, index=None):
        net = context.get("net") or {}
        entry = net.get(slot)
        if not entry:
            return None
        return entry.get(field)

    return resolver


def _disk_field(letter_key, field):
    """letter_key - на какой ключ в settings смотреть за буквой диска
    (например 'disk1_letter'), field - что достать из context['disks'][буква].
    Буква диска настраивается в веб-интерфейсе (аналог net1_iface/net2_iface),
    поэтому резолвер сам берёт актуальную букву из context['disk_slots']."""

    def resolver(context, index=None):
        slots = context.get("disk_slots") or {}
        letter = slots.get(letter_key)
        if not letter:
            return None
        disks = context.get("disks") or {}
        entry = disks.get(letter)
        if not entry:
            return None
        return entry.get(field)

    return resolver


# ---------------- реестр ----------------
#
# group: всегда "scalar" в win-hud-arduino (повторяющихся групп нет - см. шапку файла)
# category: только для группировки легенды на /screens (buildLegend() в
#         screens_webui.py) - общий с shkaf-hud код, категории свои

VARIABLES = {
    # --- CPU / RAM ---
    "cpu_pct":          {"label": "Загрузка CPU, %",                       "group": "scalar", "category": "Система", "resolver": _scalar("cpu_pct")},
    "cpu_pct_core_max":  {"label": "Загрузка самого нагруженного ядра, %",  "group": "scalar", "category": "Система", "resolver": _scalar("cpu_pct_core_max")},
    "cpu_freq_mhz":      {"label": "Частота CPU, МГц (среднее по ядрам)",   "group": "scalar", "category": "Система", "resolver": _scalar("cpu_freq_mhz")},
    "ram_pct":           {"label": "Загрузка RAM, %",                      "group": "scalar", "category": "Система", "resolver": _scalar("ram_pct")},
    "ram_used_gb":       {"label": "RAM занято, GB",                       "group": "scalar", "category": "Система", "resolver": _scalar("ram_used_gb")},
    "ram_total_gb":      {"label": "RAM всего, GB",                        "group": "scalar", "category": "Система", "resolver": _scalar("ram_total_gb")},
    "uptime":            {"label": "Аптайм Windows",                       "group": "scalar", "category": "Система", "resolver": _scalar("uptime")},
    "container_uptime":  {"label": "Аптайм win-hud-arduino",               "group": "scalar", "category": "Система", "resolver": _scalar("container_uptime")},
    "time_now":          {"label": "Текущее время (ЧЧ:ММ)",                "group": "scalar", "category": "Система", "resolver": _scalar("time_now")},

    # --- GPU (NVIDIA, через pynvml) ---
    "gpu_name":          {"label": "GPU: модель",                  "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_name")},
    "gpu_pct":           {"label": "GPU: загрузка, %",             "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_pct")},
    "gpu_temp_c":        {"label": "GPU: температура, °C",         "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_temp_c")},
    "gpu_vram_used_gb":  {"label": "GPU: VRAM занято, GB",         "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_vram_used_gb")},
    "gpu_vram_total_gb": {"label": "GPU: VRAM всего, GB",          "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_vram_total_gb")},
    "gpu_vram_pct":      {"label": "GPU: VRAM занято, %",          "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_vram_pct")},
    "gpu_power_w":       {"label": "GPU: потребление, Вт",         "group": "scalar", "category": "GPU", "resolver": _scalar("gpu_power_w")},

    # --- Диски (буквы дисков настраиваются в веб-интерфейсе, аналог net1/net2) ---
    "disk1_letter":    {"label": "Диск 1: буква",           "group": "scalar", "category": "Диски", "resolver": lambda ctx, index=None: (ctx.get("disk_slots") or {}).get("disk1_letter")},
    "disk1_used_pct":  {"label": "Диск 1: занято, %",       "group": "scalar", "category": "Диски", "resolver": _disk_field("disk1_letter", "used_pct")},
    "disk1_free_gb":   {"label": "Диск 1: свободно, GB",    "group": "scalar", "category": "Диски", "resolver": _disk_field("disk1_letter", "free_gb")},
    "disk1_total_gb":  {"label": "Диск 1: всего, GB",       "group": "scalar", "category": "Диски", "resolver": _disk_field("disk1_letter", "total_gb")},

    "disk2_letter":    {"label": "Диск 2: буква",           "group": "scalar", "category": "Диски", "resolver": lambda ctx, index=None: (ctx.get("disk_slots") or {}).get("disk2_letter")},
    "disk2_used_pct":  {"label": "Диск 2: занято, %",       "group": "scalar", "category": "Диски", "resolver": _disk_field("disk2_letter", "used_pct")},
    "disk2_free_gb":   {"label": "Диск 2: свободно, GB",    "group": "scalar", "category": "Диски", "resolver": _disk_field("disk2_letter", "free_gb")},
    "disk2_total_gb":  {"label": "Диск 2: всего, GB",       "group": "scalar", "category": "Диски", "resolver": _disk_field("disk2_letter", "total_gb")},

    # --- Сеть, слот 1 ---
    "net1_name":       {"label": "Net1: имя интерфейса",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "name")},
    "net1_speed":      {"label": "Net1: скорость линка",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "speed")},
    "net1_rx":         {"label": "Net1: входящая скорость",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "rx")},
    "net1_tx":         {"label": "Net1: исходящая скорость", "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "tx")},
    "net1_total_rx":   {"label": "Net1: накоплено принято (с запуска)", "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "total_rx")},
    "net1_total_tx":   {"label": "Net1: накоплено отдано (с запуска)",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net1", "total_tx")},

    # --- Аудио (энкодер на плате крутит системную громкость, клик - настраиваемое
    # действие в /settings: mute/unmute, переключение устройства вывода и т.п.) ---
    "volume_pct":       {"label": "Громкость, %",                  "group": "scalar", "category": "Аудио", "resolver": _scalar("volume_pct")},
    "volume_muted":     {"label": "Звук выключен (да/нет)",        "group": "scalar", "category": "Аудио", "resolver": _scalar("volume_muted")},
    "audio_device_name": {"label": "Устройство вывода звука",      "group": "scalar", "category": "Аудио", "resolver": _scalar("audio_device_name")},

    # --- Клавиатура ---
    "keyboard_layout":  {"label": "Раскладка клавиатуры (RU/EN и т.п.)", "group": "scalar", "category": "Система", "resolver": _scalar("keyboard_layout")},

    # --- Now Playing (SMTC) - media_title/media_artist резолвятся в None,
    # если сейчас ничего не играет (включая паузу) - см. metrics_windows.MediaMonitor
    # и правило про условные экраны в докстринге screens.py ---
    "media_title":    {"label": "Трек: название",        "group": "scalar", "category": "Медиа", "resolver": _scalar("media_title")},
    "media_artist":   {"label": "Трек: исполнитель",      "group": "scalar", "category": "Медиа", "resolver": _scalar("media_artist")},
    "media_playing":  {"label": "Сейчас играет (да/нет)", "group": "scalar", "category": "Медиа", "resolver": _scalar("media_playing")},

    # --- Сеть, слот 2 ---
    "net2_name":       {"label": "Net2: имя интерфейса",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "name")},
    "net2_speed":      {"label": "Net2: скорость линка",     "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "speed")},
    "net2_rx":         {"label": "Net2: входящая скорость",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "rx")},
    "net2_tx":         {"label": "Net2: исходящая скорость", "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "tx")},
    "net2_total_rx":   {"label": "Net2: накоплено принято (с запуска)", "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "total_rx")},
    "net2_total_tx":   {"label": "Net2: накоплено отдано (с запуска)",  "group": "scalar", "category": "Сеть", "resolver": _net_field("net2", "total_tx")},
}

# Порядок категорий в легенде на /screens (buildLegend() в screens_webui.py -
# общий с shkaf-hud код, сортирует по этому списку, а не по алфавиту).
CATEGORY_ORDER = ["Система", "GPU", "Диски", "Сеть", "Аудио", "Медиа"]

# Повторяющихся групп в win-hud-arduino нет (Plex-стримы/qBittorrent-торренты
# сюда не переехали) - оставлено пустым для совместимости с общим
# screens.py/templates.py, которые проверяют REPEATING_GROUPS.
REPEATING_GROUPS = ()
REPEATING_GROUP_MAX = {}


def group_count(group_name, context):
    """Оставлено для совместимости с общим screens.py - в win-hud-arduino
    повторяющихся групп нет, поэтому всегда 0 (экран такой группы никогда
    не будет создан через веб-интерфейс, т.к. в легенде такие переменные
    просто не появятся)."""
    return 0


def resolve(var_name, context, index=None):
    """Достать значение переменной. Возвращает None, если переменной нет
    в реестре, либо данных сейчас нет (например net2/диск2 не выбран)."""
    spec = VARIABLES.get(var_name)
    if spec is None:
        return None
    try:
        return spec["resolver"](context, index)
    except Exception:
        return None


def legend():
    """Для веб-интерфейса: список переменных с категорией (для группировки на
    /screens) и признаком repeating (в win-hud-arduino всегда False - см.
    REPEATING_GROUPS выше)."""
    return [
        {
            "name": name,
            "group": spec["group"],
            "category": spec.get("category", "Прочее"),
            "repeating": spec["group"] in REPEATING_GROUPS,
            "label": spec["label"],
        }
        for name, spec in VARIABLES.items()
    ]
