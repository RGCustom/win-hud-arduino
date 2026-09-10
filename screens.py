"""
screens.py  (win-hud-arduino)

Хранилище OLED-экранов + логика ротации - портировано из shkaf-hud. Движок
(CRUD/build_active_screens/RotationState) в основе не менялся - он уже был
написан общим - опирается на templates.template_group() и
variables.group_count(), а не на конкретные переменные проекта. Repeating-
группы (stream/recent/qbt, см. variables.REPEATING_GROUPS) в этом проекте
НЕ пусты (Plex/qBittorrent были возвращены - см. докстринг variables.py) -
ветка "экран в N копий" В ROTATION.PY ДОСТИЖИМА, если пользователь вручную
добавит на /screens экран, использующий stream_*/recent_*/qbt_* переменную
(см. README - по умолчанию таких экранов нет, DEFAULT_SCREENS ниже их не
содержит, но интеграции для этого предусмотрены).

Изменилось только:
  - DEFAULT_SCREENS - под новые переменные (CPU/RAM/GPU/диски/сеть/звук/
    раскладка вместо Cache/Array/Plex/qBittorrent)
  - CONFIG_DIR - дефолт под Windows (%APPDATA%\\win-hud-arduino), не /config
    докер-тома
  - НОВОЕ: приоритетная ротация (см. обсуждение в чате про личные/фоновые
    экраны, показывающиеся чаще обычных) - у каждого экрана появились два
    новых поля:
      "tier"         - "normal" (по умолчанию) | "personal" | "ambient".
                       Задаётся вручную в /screens (выпадающий список), НЕ
                       определяется автоматически по содержимому шаблона -
                       угадывание по переменным (например "если экран
                       использует media_title - значит personal") было бы
                       хрупким и неочевидным для пользователя. "personal" -
                       чаще (см. cfg["priority_boost_personal"] в
                       pc_hud.py) и с правом принудительно прервать текущий
                       показ (активация/смена контента); "ambient" - чаще
                       (cfg["priority_boost_ambient"]), но БЕЗ права
                       прерывания - просто получает более частые слоты в
                       обычной ротации. ИСКЛЮЧЕНИЕ: у копий repeating-группы
                       "stream" (Plex-сеансы) tier="ambient" МОЖЕТ быть
                       точечно повышен до "personal" ДЛЯ ОДНОЙ КОНКРЕТНОЙ
                       копии, если её "user" совпадает с настройкой
                       cfg["my_plex_user"] (/settings, карточка Tautulli) -
                       см. _stream_copy_tier() ниже. Остальные копии того
                       же экрана (чужие сеансы) остаются "ambient" как есть.
      "trigger_vars" - список имён переменных (подмножество тех, что уже
                       используются в l1/l2/l3 этого экрана), изменение
                       которых на УЖЕ ПОКАЗЫВАЕМОМ personal-экране должно
                       форсировать немедленное обновление содержимого и
                       продление его duration заново (например
                       ["media_title", "media_artist"] - смена трека, а не
                       ["stream_progress"] - которое меняется каждую
                       секунду и не должно дёргать интерфейс). Пусто по
                       умолчанию - тогда форс работает только на активацию
                       (экран появился/исчез из active), без реакции на
                       изменение контента внутри уже активного показа.
                       Не используется для tier="normal"/"ambient" - там
                       принудительных обновлений вообще нет, см. RotationState.
    Сама логика выбора следующего экрана с учётом tier - в RotationState
    ниже (не в build_active_screens() - она как строила плоский список
    активных экранов, так и строит, теперь ещё и с point-override tier для
    "stream" - разбивка на "дорожки" per-tier - уже ответственность
    RotationState, ей достаточно готового поля tier на каждом элементе active).
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

# Допустимые значения "tier" - см. докстринг модуля выше. "normal" - дефолт
# для новых экранов и для всех уже сохранённых screens.json от версии ДО
# этой настройки (см. backfill в load_screens() ниже).
SCREEN_TIERS = ("normal", "personal", "ambient")

DEFAULT_SCREENS = [
    {
        "id": "default-cpuram",
        "name": "CPU/RAM",
        "l1": "CPU {cpu_pct}%",
        "l2": "RAM {ram_pct}%",
        "l3": "{cpu_freq_mhz}MHz",
        "duration": 4.0,
        "enabled": True,
        "tier": "normal",
        "trigger_vars": [],
    },
    {
        "id": "default-gpu",
        "name": "GPU",
        "l1": "{gpu_name:16}",
        "l2": "GPU {gpu_pct}% {gpu_temp_c:.0f}C",
        "l3": "VRAM {gpu_vram_pct}%",
        "duration": 4.0,
        "enabled": True,
        "tier": "normal",
        "trigger_vars": [],
    },
    {
        "id": "default-disks",
        "name": "Disks",
        "l1": "Disk {disk1_letter}: {disk1_used_pct}%",
        "l2": "Disk {disk2_letter}: {disk2_used_pct}%",
        "l3": "Free {disk1_free_gb:.0f}GB",
        "duration": 4.0,
        "enabled": True,
        "tier": "normal",
        "trigger_vars": [],
    },
    {
        "id": "default-net1",
        "name": "Network",
        "l1": "{net1_name} {net1_speed}",
        "l2": "\u2193 {net1_rx}",
        "l3": "\u2191 {net1_tx}",
        "duration": 4.0,
        "enabled": True,
        "tier": "normal",
        "trigger_vars": [],
    },
    {
        "id": "default-audio",
        "name": "Audio",
        "l1": "Vol {volume_pct}%  {volume_muted}",
        "l2": "{audio_device_name:16}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
        "tier": "normal",
        "trigger_vars": [],
    },
    {
        # media_title/media_artist резолвятся в None, когда сейчас ничего не
        # играет (см. metrics_windows.MediaMonitor) - экран автоматически
        # выпадает из ротации через общий механизм build_active_screens()
        # ниже, отдельной логики "показывать только когда играет" тут нет.
        #
        # tier="personal" - живая демонстрация приоритетной ротации (см.
        # докстринг модуля выше): показывается чаще обычных экранов
        # (cfg["priority_boost_personal"] в pc_hud.py) и имеет право
        # прервать текущий показ, если появился (заиграла музыка) или
        # если сменился трек ПРЯМО во время его собственного показа -
        # trigger_vars ниже перечисляет именно те переменные, смена
        # которых означает "это другое событие, а не то же самое" (не
        # включаем media_playing - он не меняется без смены title/artist
        # в паре, был бы избыточен как триггер).
        "id": "default-nowplaying",
        "name": "Now Playing",
        "l1": "{media_title:16}",
        "l2": "{media_artist:16}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
        "tier": "personal",
        "trigger_vars": ["media_title", "media_artist"],
    },
    {
        "id": "default-clock",
        "name": "Clock",
        "l1": "{time_now}   [{keyboard_layout}]",
        "l2": "Uptime {uptime}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
        "tier": "normal",
        "trigger_vars": [],
    },
]


def _backfill_tier_fields(screen):
    """Дополняет ОДИН экран полями tier/trigger_vars, если их нет (файл
    screens.json сохранён версией до появления приоритетной ротации) -
    мутирует и возвращает тот же dict. tier валидируется на случай ручной
    правки файла руками/старого бага - невалидное значение откатывается
    на "normal", а не падает и не пропускает экран."""
    if screen.get("tier") not in SCREEN_TIERS:
        screen["tier"] = "normal"
    if not isinstance(screen.get("trigger_vars"), list):
        screen["trigger_vars"] = []
    return screen


def load_screens():
    try:
        with open(SCREENS_FILE) as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return [_backfill_tier_fields(s) for s in data]
    except Exception:
        pass
    return [dict(s) for s in DEFAULT_SCREENS]


def save_screens(screens):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SCREENS_FILE, "w") as f:
        json.dump(screens, f)


# ---------------- CRUD (portировано из shkaf-hud + tier/trigger_vars - НОВОЕ, см. докстринг модуля) ----------------

def _sanitize_tier(value, fallback="normal"):
    """Валидирует tier против SCREEN_TIERS - невалидное/отсутствующее
    значение откатывается на fallback, а не падает (та же терпимость к
    мусорному вводу, что и у остальных /api/* эндпоинтов в pc_hud.py -
    например api_mode() там же молча игнорирует значение не из BAR_MODES)."""
    return value if value in SCREEN_TIERS else fallback


def _sanitize_trigger_vars(value):
    """Список имён переменных для форс-обновления personal-экрана (см.
    докстринг модуля) - непустые строки, без дублей, порядок не важен для
    самой логики сравнения (используется как множество), но список
    сохраняем как есть (не set) ради стабильной сериализации в JSON."""
    if not isinstance(value, list):
        return []
    seen = []
    for v in value:
        if isinstance(v, str) and v and v not in seen:
            seen.append(v)
    return seen


def new_screen(name="New screen", l1="", l2="", l3="", duration=4.0, tier="normal", trigger_vars=None):
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "l1": l1, "l2": l2, "l3": l3,
        "duration": max(1.0, float(duration)),
        "enabled": True,
        "tier": _sanitize_tier(tier),
        "trigger_vars": _sanitize_trigger_vars(trigger_vars),
    }


def create_screen(screens, data):
    screen = new_screen(
        name=data.get("name", "New screen"),
        l1=data.get("l1", ""), l2=data.get("l2", ""), l3=data.get("l3", ""),
        duration=data.get("duration", 4.0),
        tier=data.get("tier", "normal"),
        trigger_vars=data.get("trigger_vars"),
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
            if "tier" in data:
                s["tier"] = _sanitize_tier(data["tier"], fallback=s.get("tier", "normal"))
            if "trigger_vars" in data:
                s["trigger_vars"] = _sanitize_trigger_vars(data["trigger_vars"])
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
        [{"screen_id": ..., "lines": [l1,l2,l3], "duration": float,
          "tier": "normal"|"personal"|"ambient", "trigger_vars": [str, ...]}, ...]
    tier/trigger_vars - см. докстринг модуля выше про приоритетную ротацию;
    копируются как есть из исходного screen-словаря (уже провалидированы при
    загрузке/сохранении - см. _backfill_tier_fields()/_sanitize_tier()), тут
    только подстраховка на случай мусора (см. tier not in SCREEN_TIERS ниже).

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

        tier = screen.get("tier", "normal")
        if tier not in SCREEN_TIERS:
            tier = "normal"
        trigger_vars = screen.get("trigger_vars") or []

        if not groups:
            r1, ok1 = templates.render(l1, context)
            r2, ok2 = templates.render(l2, context)
            r3, ok3 = templates.render(l3, context)
            if ok1 and ok2 and ok3:
                active.append({
                    "screen_id": screen["id"], "lines": [r1, r2, r3], "duration": screen["duration"],
                    "tier": tier, "trigger_vars": trigger_vars,
                })
            continue

        # Достижимая ветка (несмотря на комментарий "недостижимая" в
        # некоторых старых заметках проекта) - пользователь МОЖЕТ вручную
        # добавить экран на переменных repeating-группы (stream_*/recent_*/
        # qbt_*, см. README) через /screens, интеграции для этого
        # предусмотрены (metrics_tautulli.py/metrics_qbittorrent.py). tier/
        # trigger_vars относятся к ЭКРАНУ целиком - у всех N копий одно и то
        # же БАЗОВОЕ значение, ЗА ИСКЛЮЧЕНИЕМ point-override ниже для
        # group_name == "stream" (см. _stream_copy_tier()).
        group_name = next(iter(groups))
        count = variables.group_count(group_name, context)
        for idx in range(count):
            r1, ok1 = templates.render(l1, context, index=idx)
            r2, ok2 = templates.render(l2, context, index=idx)
            r3, ok3 = templates.render(l3, context, index=idx)
            if ok1 and ok2 and ok3:
                copy_tier = tier
                if group_name == "stream":
                    copy_tier = _stream_copy_tier(tier, idx, context)
                active.append({
                    "screen_id": f"{screen['id']}#{idx}",
                    "lines": [r1, r2, r3],
                    "duration": screen["duration"],
                    "tier": copy_tier, "trigger_vars": trigger_vars,
                })

    return active


def _stream_copy_tier(base_tier, idx, context):
    """Point-override tier ОДНОЙ конкретной копии repeating-группы "stream" -
    см. обсуждение в чате про "свой/чужой Plex-сеанс": если у пользователя
    заполнена настройка my_plex_user (/settings, карточка Tautulli) И она
    совпадает с полем "user" именно ЭТОГО сеанса (context["streams"][idx],
    см. metrics_tautulli.TautulliClient._get_activity() - там же friendly_name
    попадает в это поле) - апгрейдим tier ЭТОЙ КОНКРЕТНОЙ копии экрана до
    "personal" (тот же человек смотрит на этом же ПК, событие локальное).

    Апгрейд применяется, ТОЛЬКО если базовый tier экрана - "ambient" (это и
    есть рекомендованная настройка для экрана на stream-переменных - см.
    подсказку в /screens) - если админ явно поставил другой tier для всего
    экрана (например "normal" или уже "personal"), это осознанный выбор,
    point-override его не трогает. Чужие сеансы (user не совпал, или
    my_plex_user не заполнен) возвращают base_tier как есть - без изменений."""
    if base_tier != "ambient":
        return base_tier
    my_user = (context.get("my_plex_user") or "").strip()
    if not my_user:
        return base_tier
    streams = context.get("streams") or []
    if idx >= len(streams):
        return base_tier
    session_user = streams[idx].get("user") or ""
    if session_user == my_user:
        return "personal"
    return base_tier


# ---------------- ротация (НОВОЕ - три дорожки по tier, см. докстринг модуля) ----------------

class RotationState:
    """
    Живёт в памяти главного цикла (один инстанс на процесс, как и раньше) -
    продвигает текущий экран по его СОБСТВЕННОМУ duration (пользовательская
    настройка НИКОГДА не переопределяется этим классом - см. докстринг
    модуля: механизм управляет только тем, КАК ЧАСТО экран получает слот,
    а не тем, сколько секунд он висит на этом слоте), плюс:

      - три независимых "дорожки" (lanes) по tier - personal/ambient/normal -
        каждая крутится по кругу САМА ПО СЕБЕ (round-robin по screen_id
        внутри своей дорожки, не смешиваясь с другими) - см. _pick_in_lane();
      - slot_counter решает, чья дорожка получает ПЛАНОВЫЙ (не форсированный)
        слот на этот виток - см. _select_tier(): personal - каждый
        priority_boost_personal-й слот, ambient - каждый priority_boost_ambient-й,
        иначе - normal. Если "должная" дорожка на этот виток пуста - слот
        просто достаётся normal (или следующей непустой дорожке) - "должок"
        НЕ копится, ждём следующего совпадения по модулю (см. обсуждение -
        осознанно простое правило, без carry-over);
      - только personal-дорожка имеет право ФОРСИРОВАННО прервать текущий
        показ (см. current_lines() ниже) - при активации (экран появился в
        active, которого не было на прошлом тике) или при изменении его
        trigger_vars ПРЯМО во время собственного показа (в этом случае это
        НЕ смена текущего экрана, а продление duration с обновлённым
        содержимым - см. "elif cur_tier == 'personal'" ниже). ambient никогда
        не прерывает - только получает более частые ПЛАНОВЫЕ слоты;
      - если экран, который сейчас показывается, ИСЧЕЗ из active раньше
        истечения своего duration (событие закончилось/условие перестало
        выполняться) - уступаем место немедленно, не дожидаясь таймера, для
        ЛЮБОГО tier (не только personal) - иначе завис бы на устаревшем
        содержимом (см. обсуждение п.3.4 - тот же нюанс был скрытым багом и
        в старой index-модели, просто маскировался там переиндексацией).

    trigger_fingerprints - кэш последних значений trigger_vars на экран (по
    screen_id), нужен только personal-экранам с непустым trigger_vars -
    используется, чтобы отличить "контент сменился" (стоит форсировать
    обновление/продление) от "контент тот же" (ничего специально делать не
    нужно - строки и так перерисовываются каждый вызов, см. ниже).

    _prev_active_ids - screen_id, которые были активны на ПРЕДЫДУЩЕМ вызове -
    сравнение с текущим active даёт "какие personal-экраны только что
    активировались" (её не было раньше, появилась сейчас).
    """

    def __init__(self):
        self.lane_cursors = {"personal": None, "ambient": None, "normal": None}
        self.slot_counter = 0
        self.current = None  # {"screen_id","tier","lines","duration","started_at"} | None
        self.trigger_fingerprints = {}
        self._prev_active_ids = set()

    @staticmethod
    def _fingerprint(screen_id, trigger_vars_by_id, context):
        """None, если у экрана нет trigger_vars (форс только на активацию,
        см. докстринг класса) - иначе кортеж значений его trigger_vars ПРЯМО
        СЕЙЧАС, для сравнения с прошлым вызовом."""
        tvars = trigger_vars_by_id.get(screen_id) or []
        if not tvars:
            return None
        return tuple(variables.resolve(v, context) for v in tvars)

    def _select_tier(self, lanes, priority_boost_personal, priority_boost_ambient):
        """Чья дорожка получает ПЛАНОВЫЙ слот на этот виток - см. докстринг
        класса. Пустая "должная" дорожка молча уступает normal, а если и
        normal пуста - следующей непустой (personal, потом ambient) - до
        полностью пустого active() дело не доходит, этот случай отсекается
        раньше, в current_lines()."""
        personal_boost = max(1, int(priority_boost_personal))
        ambient_boost = max(1, int(priority_boost_ambient))

        if lanes["personal"] and self.slot_counter % personal_boost == 0:
            return "personal"
        if lanes["ambient"] and self.slot_counter % ambient_boost == 0:
            return "ambient"
        if lanes["normal"]:
            return "normal"
        if lanes["personal"]:
            return "personal"
        if lanes["ambient"]:
            return "ambient"
        return None

    def _pick_in_lane(self, tier, lane_ids):
        """Round-robin ВНУТРИ одной дорожки, независимо от других дорожек -
        курсор хранит screen_id (не индекс!), т.к. active пересобирается
        каждый вызов и порядковый индекс "поплыл" бы при любом изменении
        состава активных экранов между тиками (это и была скрытая слабость
        старой index-модели - см. докстринг класса)."""
        if not lane_ids:
            return None
        cursor = self.lane_cursors.get(tier)
        if cursor in lane_ids:
            idx = lane_ids.index(cursor)
            next_id = lane_ids[(idx + 1) % len(lane_ids)]
        else:
            # курсор не найден (первый вызов, либо прошлый экран этой
            # дорожки исчез из active) - начинаем дорожку заново с начала
            next_id = lane_ids[0]
        self.lane_cursors[tier] = next_id
        return next_id

    def current_lines(self, screens, context, now=None,
                       priority_boost_personal=2, priority_boost_ambient=4):
        now = now if now is not None else time.time()
        active = build_active_screens(screens, context)
        active_by_id = {a["screen_id"]: a for a in active}

        if not active:
            self.current = None
            self._prev_active_ids = set()
            return ["", "", ""]

        lanes = {"personal": [], "ambient": [], "normal": []}
        for item in active:
            tier = item.get("tier", "normal")
            if tier not in lanes:
                tier = "normal"
            lanes[tier].append(item["screen_id"])

        trigger_vars_by_id = {item["screen_id"]: item.get("trigger_vars") or [] for item in active}

        # ---- какие personal-экраны только что появились в active (не было
        # на прошлом вызове) - право форс-прерывания текущего показа ----
        newly_active_personal = [sid for sid in lanes["personal"] if sid not in self._prev_active_ids]
        self._prev_active_ids = set(active_by_id.keys())

        # ---- текущий показываемый экран: исчез / контент сменился / жив как есть ----
        if self.current is not None:
            cur_id = self.current["screen_id"]
            cur_tier = self.current["tier"]

            if cur_id not in active_by_id:
                # событие закончилось / условие видимости перестало
                # выполняться - уступаем место немедленно, не дожидаясь
                # истечения duration, независимо от tier (см. докстринг класса)
                self.current = None
            else:
                # экран всё ещё активен - строки перерисовываем в любом
                # случае (context мог поменяться в нетриггерных полях, см.
                # обсуждение п.3.1 - это просто обновление текста, не смена
                # слота и не продление таймера)
                self.current["lines"] = active_by_id[cur_id]["lines"]
                if cur_tier == "personal":
                    fp = self._fingerprint(cur_id, trigger_vars_by_id, context)
                    if fp is not None and self.trigger_fingerprints.get(cur_id) != fp:
                        # это другое событие (например, сменился трек), а не
                        # то же самое - форсируем полное продление duration,
                        # как будто экран показался заново
                        self.trigger_fingerprints[cur_id] = fp
                        self.current["started_at"] = now

        # ---- форс-активация: personal-экран появился, которого не было
        # (и это не тот, что уже показывается сейчас) ----
        force_target = None
        for sid in newly_active_personal:
            if self.current is None or self.current["screen_id"] != sid:
                force_target = sid
                break

        if force_target is not None:
            item = active_by_id[force_target]
            self.current = {
                "screen_id": force_target, "tier": "personal",
                "lines": item["lines"], "duration": item["duration"], "started_at": now,
            }
            self.trigger_fingerprints[force_target] = self._fingerprint(force_target, trigger_vars_by_id, context)
            self.lane_cursors["personal"] = force_target
            return self.current["lines"]

        # ---- обычное продолжение текущего показа (duration ещё не истёк) ----
        if self.current is not None and now - self.current["started_at"] < self.current["duration"]:
            return self.current["lines"]

        # ---- плановая смена: duration истёк (или self.current стал None
        # выше из-за исчезновения экрана) - выбираем следующий по алгоритму ----
        tier = self._select_tier(lanes, priority_boost_personal, priority_boost_ambient)
        self.slot_counter += 1
        if tier is None:
            # теоретически недостижимо (active непуст -> хотя бы одна
            # дорожка непуста), но не падаем на мусорном состоянии
            self.current = None
            return ["", "", ""]

        screen_id = self._pick_in_lane(tier, lanes[tier])
        item = active_by_id[screen_id]
        self.current = {
            "screen_id": screen_id, "tier": tier,
            "lines": item["lines"], "duration": item["duration"], "started_at": now,
        }
        if tier == "personal":
            self.trigger_fingerprints[screen_id] = self._fingerprint(screen_id, trigger_vars_by_id, context)
        return self.current["lines"]
