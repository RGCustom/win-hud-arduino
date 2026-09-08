"""
variables.py  (win-hud-arduino)

Реестр переменных для OLED-шаблонов (L1/L2/L3) - портировано из проекта
shkaf-hud, но под метрики Windows-PC вместо Unraid/Tautulli/qBittorrent...
если не считать того, что Tautulli/qBittorrent сюда всё же вернулись (см.
ниже) - только уже не как метрики самого NAS, а как внешние интеграции с
любого PC, на котором крутится win-hud-arduino.

Ничего сам не собирает - работает поверх "context": обычного словаря с уже
готовыми данными, который раз в тик формирует главный скрипт (pc_hud.py) и
передаёт сюда. Здесь только словарь "имя переменной -> как её достать из
context", плюс легенда для веб-интерфейса (те же /api/variables, /screens,
что и в shkaf-hud - код screens_webui.py/templates.py переехал без правок).

Два вида переменных:
  - "scalar" - одно значение, всегда одно и то же (cpu_pct, gpu_temp_c и т.п.)
  - "stream"/"recent"/"qbt" - REPEATING-группы: экран, использующий
    переменную такой группы, автоматически размножается ротацией на N копий
    (по числу активных Plex-сеансов / недавно добавленного в Plex / активно
    скачивающихся торрентов ПРЯМО СЕЙЧАС) - см. group_count() и
    screens.build_active_screens() (общий с shkaf-hud движок, никаких правок
    там ради этого не понадобилось - он уже был готов к repeating-группам).
    Источники данных - metrics_tautulli.TautulliClient (stream/recent) и
    metrics_qbittorrent.QbittorrentClient (qbt); pc_hud.py раз в тик кладёт
    их списки в context как context["streams"] / context["recent"] /
    context["torrents"].

Изначально (первая версия win-hud-arduino) повторяющихся групп тут не было
вовсе - Media/qBittorrent пласт с shkaf-hud сюда не переезжал. Позже решили
всё же вернуть Plex (через Tautulli, а не напрямую через Plex API - готовая
статистика по библиотекам/сеансам/недавнему это сильно упрощает) и
qBittorrent - под них и появились "stream"/"recent"/"qbt" ниже.
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
#     # VU-метр - реальный уровень ИГРАЮЩЕГО звука (пики сигнала, через
#     # Core Audio IAudioMeterInformation) - НЕ то же самое, что volume_pct
#     # (системная громкость может быть 100%, а играть тихая запись, и
#     # наоборот). peak - по обоим каналам сразу; left/right - раздельно для
#     # стерео (для моно-источника оба равны peak). Обновляется каждый тик
#     # (не раз в POLL_INTERVAL) с плавным затуханием - см.
#     # metrics_windows.AudioController.read_vu().
#     "vu_peak_pct": int, "vu_left_pct": int, "vu_right_pct": int,
#
#     "keyboard_layout": str,   # глобальная системная раскладка, напр. "RU"/"EN"
#
#     "media_title": str|None, "media_artist": str|None,   # None, если сейчас
#                                # ничего не играет (в т.ч. на паузе) - см.
#                                # metrics_windows.MediaMonitor
#     "media_playing": str,     # "да"/"нет" - уже отформатировано
#
#     "top_process_name": str|None, "top_process_cpu_pct": float, "top_process_ram_pct": float,
#     "disk_io_read_mbps": float, "disk_io_write_mbps": float,
#
#     # --- Plex (через Tautulli, см. metrics_tautulli.TautulliClient) ---
#     "plex_movies": int|None, "plex_series": int|None, "plex_songs": int|None,
#     "plex_server_status": str,           # "online"/"offline"
#     "plex_transcode_count": int|None,
#     "plex_users_count": int|None,
#     "streams": [                          # repeating-группа "stream"
#         {"user": str, "title": str, "mode": str, "progress": int, "bandwidth": str},
#         ...
#     ],
#     "recent": [                           # repeating-группа "recent"
#         {"title": str, "code": str, "ago": str},
#         ...
#     ],
#
#     # --- qBittorrent (см. metrics_qbittorrent.QbittorrentClient) ---
#     "qbt_total_dl": str|None, "qbt_total_ul": str|None,
#     "qbt_ratio": float|None, "qbt_free_space_gb": float|None,
#     "qbt_count_all": int|None,
#     "torrents": [                         # repeating-группа "qbt"
#         {"name": str, "speed": str, "eta": str},
#         ...
#     ],
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


def _group_field(list_key, field):
    """
    Универсальный резолвер для REPEATING-групп (stream/recent/qbt).
    list_key - имя списка в context (context['streams']/['recent']/
    ['torrents']), field - какое поле взять из элемента списка ПО ИНДЕКСУ
    index. index сюда приходит от движка рендера (см. templates.render() ->
    screens.build_active_screens(), где index пробегает 0..group_count()-1) -
    именно index, а не сам резолвер, определяет, "какая копия" экрана сейчас
    рендерится.

    None, если index не передан (сюда не должно доходить для repeating-
    переменной при нормальной работе screens.py, но резолверы обязаны
    выдерживать любой вызов без падения - см. общий принцип "resolve()
    гасит исключения" ниже) либо вышел за пределы списка (список сократился
    между вызовом group_count() и рендером - маловероятно, но не должно
    падать).
    """

    def resolver(context, index=None):
        items = context.get(list_key) or []
        if index is None or index >= len(items):
            return None
        return items[index].get(field)

    return resolver


def _group_pos(list_key):
    """1-based позиция элемента внутри repeating-группы (stream_pos/
    recent_pos/qbt_pos) - вызывающему код на экране обычно нужен человеческий
    номер "2 из 3", а не 0-based index."""

    def resolver(context, index=None):
        items = context.get(list_key) or []
        if index is None or index >= len(items):
            return None
        return index + 1

    return resolver


def _group_total(list_key):
    """Общее число элементов repeating-группы (stream_count/recent_count/
    qbt_count) - ОДНО И ТО ЖЕ значение на каждой копии экрана, не зависит от
    index. None при пустом списке - но практического значения это не имеет:
    если список пуст, group_count() вернёт 0, цикл рендера в
    screens.build_active_screens() не выполнится ни разу, и эта ветка
    попросту не будет вызвана ни для одной копии экрана."""

    def resolver(context, index=None):
        items = context.get(list_key) or []
        if not items:
            return None
        return len(items)

    return resolver


# ---------------- реестр ----------------
#
# group: "scalar" для обычных переменных; "stream"/"recent"/"qbt" - см.
#         REPEATING_GROUPS/_GROUP_LIST_KEYS ниже
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
    "top_process_name":     {"label": "Топ-процесс: имя",             "group": "scalar", "category": "Система", "resolver": _scalar("top_process_name")},
    "top_process_cpu_pct":  {"label": "Топ-процесс: CPU, %",          "group": "scalar", "category": "Система", "resolver": _scalar("top_process_cpu_pct")},
    "top_process_ram_pct":  {"label": "Топ-процесс: RAM, %",          "group": "scalar", "category": "Система", "resolver": _scalar("top_process_ram_pct")},
    "disk_io_read_mbps":    {"label": "Диски: чтение, MB/s",          "group": "scalar", "category": "Система", "resolver": _scalar("disk_io_read_mbps")},
    "disk_io_write_mbps":   {"label": "Диски: запись, MB/s",          "group": "scalar", "category": "Система", "resolver": _scalar("disk_io_write_mbps")},

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
    "vu_peak_pct":       {"label": "VU: пик громкости (звук), %",  "group": "scalar", "category": "Аудио", "resolver": _scalar("vu_peak_pct")},
    "vu_left_pct":       {"label": "VU: левый канал, %",           "group": "scalar", "category": "Аудио", "resolver": _scalar("vu_left_pct")},
    "vu_right_pct":      {"label": "VU: правый канал, %",          "group": "scalar", "category": "Аудио", "resolver": _scalar("vu_right_pct")},

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

    # --- Plex (через Tautulli, см. metrics_tautulli.TautulliClient) - счётчики
    # библиотек/пользователей обновляются раз в минуту, plex_server_status ==
    # "offline", если Tautulli недоступен ИЛИ ещё не настроен в /settings
    # (пустые url/api-ключ) - экраны на этих переменных сами выпадут из
    # ротации через общий механизм build_active_screens(), если резолвер
    # вернёт None (пока Tautulli не настроен - вернёт None только
    # plex_transcode_count/plex_users_count, остальные - фиксированный
    # "offline"/0, т.к. это не None-типа поля, см. TautulliClient.read()) ---
    "plex_movies":           {"label": "Plex: фильмов в библиотеке",             "group": "scalar", "category": "Plex", "resolver": _scalar("plex_movies")},
    "plex_series":           {"label": "Plex: сериалов в библиотеке",            "group": "scalar", "category": "Plex", "resolver": _scalar("plex_series")},
    "plex_songs":            {"label": "Plex: исполнителей в музыкальной библиотеке", "group": "scalar", "category": "Plex", "resolver": _scalar("plex_songs")},
    "plex_server_status":    {"label": "Plex: статус сервера (online/offline)",  "group": "scalar", "category": "Plex", "resolver": _scalar("plex_server_status")},
    "plex_transcode_count":  {"label": "Plex: число транскодирующихся сеансов",  "group": "scalar", "category": "Plex", "resolver": _scalar("plex_transcode_count")},
    "plex_users_count":      {"label": "Plex: число разных зрителей сейчас",     "group": "scalar", "category": "Plex", "resolver": _scalar("plex_users_count")},

    # --- Plex: активные сеансы (REPEATING-группа "stream") - экран с этими
    # переменными автоматически размножается по числу активных сеансов прямо
    # сейчас (потолок - TautulliClient.ACTIVE_STREAMS_MAX) ---
    "stream_user":      {"label": "Сеанс: пользователь",                      "group": "stream", "category": "Plex", "resolver": _group_field("streams", "user")},
    "stream_title":     {"label": "Сеанс: название",                          "group": "stream", "category": "Plex", "resolver": _group_field("streams", "title")},
    "stream_mode":      {"label": "Сеанс: режим (D=Direct Play/Stream, T=Transcode)", "group": "stream", "category": "Plex", "resolver": _group_field("streams", "mode")},
    "stream_progress":  {"label": "Сеанс: прогресс просмотра, %",             "group": "stream", "category": "Plex", "resolver": _group_field("streams", "progress")},
    "stream_bandwidth": {"label": "Сеанс: битрейт потока",                     "group": "stream", "category": "Plex", "resolver": _group_field("streams", "bandwidth")},
    "stream_pos":       {"label": "Сеанс: номер по порядку",                  "group": "stream", "category": "Plex", "resolver": _group_pos("streams")},
    "stream_count":     {"label": "Сеанс: всего активных сеансов сейчас",     "group": "stream", "category": "Plex", "resolver": _group_total("streams")},

    # --- Plex: недавно добавленное (REPEATING-группа "recent") ---
    "recent_title": {"label": "Недавнее: название (для эпизода - имя сериала)", "group": "recent", "category": "Plex", "resolver": _group_field("recent", "title")},
    "recent_code":  {"label": "Недавнее: код (sNNeNN для эпизода / год для фильма)", "group": "recent", "category": "Plex", "resolver": _group_field("recent", "code")},
    "recent_ago":   {"label": "Недавнее: сколько времени назад добавлено",     "group": "recent", "category": "Plex", "resolver": _group_field("recent", "ago")},
    "recent_pos":   {"label": "Недавнее: номер по порядку",                    "group": "recent", "category": "Plex", "resolver": _group_pos("recent")},
    "recent_count": {"label": "Недавнее: всего элементов в списке",           "group": "recent", "category": "Plex", "resolver": _group_total("recent")},

    # --- qBittorrent (см. metrics_qbittorrent.QbittorrentClient) - ДВА сервера
    # (qbt1_*/qbt2_* в /settings), но переменные шаблонов ОДНИ на оба -
    # qbt_total_*/ratio/free_space/count_all это СУММА по обоим серверам, не
    # зависят от repeating-группы "qbt" ниже. qbt_total_dl/ul - ТЕКУЩАЯ
    # суммарная скорость (не "скачано за всё время"!), qbt_free_space_gb -
    # уже готовая строка с единицей ("123.4 GB"/"1.20 TB") ---
    "qbt_total_dl":      {"label": "qBittorrent: суммарная скорость скачивания", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_total_dl")},
    "qbt_total_ul":      {"label": "qBittorrent: суммарная скорость раздачи",    "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_total_ul")},
    "qbt_ratio":         {"label": "qBittorrent: общий рейтинг раздачи (оба сервера)", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_ratio")},
    "qbt_free_space_gb": {"label": "qBittorrent: свободно на диске (сумма по серверам)", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_free_space_gb")},
    "qbt_count_all":     {"label": "qBittorrent: торрентов всего (оба сервера)", "group": "scalar", "category": "qBittorrent", "resolver": _scalar("qbt_count_all")},

    # --- qBittorrent: активные торренты (REPEATING-группа "qbt") - С ОБОИХ
    # серверов вперемешку, отсортированы по убыванию скорости скачивания (см.
    # QbittorrentClient.read()) - "активные" тут - это qBittorrent-фильтр
    # filter=active (качается ИЛИ раздаётся), не только скачивающиеся ---
    "qbt_name":  {"label": "Торрент: имя",                        "group": "qbt", "category": "qBittorrent", "resolver": _group_field("torrents", "name")},
    "qbt_speed": {"label": "Торрент: скорость скачивания",        "group": "qbt", "category": "qBittorrent", "resolver": _group_field("torrents", "speed")},
    "qbt_eta":   {"label": "Торрент: ETA",                         "group": "qbt", "category": "qBittorrent", "resolver": _group_field("torrents", "eta")},
    "qbt_pos":   {"label": "Торрент: номер по порядку",            "group": "qbt", "category": "qBittorrent", "resolver": _group_pos("torrents")},
    "qbt_count": {"label": "Торрент: всего активных закачек сейчас","group": "qbt", "category": "qBittorrent", "resolver": _group_total("torrents")},
}

# Порядок категорий в легенде на /screens (buildLegend() в screens_webui.py -
# общий с shkaf-hud код, сортирует по этому списку, а не по алфавиту).
CATEGORY_ORDER = ["Система", "GPU", "Диски", "Сеть", "Аудио", "Медиа", "Plex", "qBittorrent"]

# Repeating-группы - экран, использующий переменную такой группы,
# автоматически размножается на N копий (см. group_count() ниже и докстринг
# модуля выше). _GROUP_LIST_KEYS сопоставляет имя группы с ключом списка в
# context - тот же список, что используют резолверы _group_field/_group_pos/
# _group_total выше, просто с явным именем на стороне group_count().
REPEATING_GROUPS = ("stream", "recent", "qbt")

_GROUP_LIST_KEYS = {
    "stream": "streams",
    "recent": "recent",
    "qbt": "torrents",
}

# Информационный потолок числа элементов на группу - фактическое ограничение
# применяется на стороне источника данных (TautulliClient.ACTIVE_STREAMS_MAX/
# RECENT_ADDED_COUNT, QbittorrentClient.ACTIVE_TORRENTS_MAX) - тут только для
# случаев, когда потолок нужно показать/учесть на стороне веб-интерфейса.
REPEATING_GROUP_MAX = {"stream": 6, "recent": 5, "qbt": 6}


def group_count(group_name, context):
    """
    Возвращает число элементов repeating-группы group_name ПРЯМО СЕЙЧАС -
    используется screens.build_active_screens() для развёртывания экрана в
    N копий (см. докстринг screens.py). 0, если group_name не repeating-
    группа (по историческим причинам - совместимость с общим screens.py) или
    в context ещё нет соответствующего списка (Tautulli/qBittorrent ещё не
    опрашивались ни разу - список просто отсутствует/пуст)."""
    list_key = _GROUP_LIST_KEYS.get(group_name)
    if not list_key:
        return 0
    return len(context.get(list_key) or [])


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
    /screens) и признаком repeating (True для stream/recent/qbt - см.
    REPEATING_GROUPS выше; на фронтенде отмечается отдельным бейджем, см.
    screens_webui.py buildLegend())."""
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
