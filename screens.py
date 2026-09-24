"""
screens.py  (win-hud-arduino)

Хранилище OLED-экранов + логика ротации - портировано из shkaf-hud. Движок
(CRUD/build_active_screens/RotationState) опирается на templates.template_group()
и variables.group_count(), а не на конкретные переменные проекта. Repeating-
группы (stream/recent/qbt/mon, см. variables.REPEATING_GROUPS) достижимы: для
мониторинга (mon_*) - сразу из коробки (см. "default-monitoring"/
"default-alert" в DEFAULT_SCREENS), для Plex/qBittorrent - если пользователь
добавит на /screens экран с соответствующими переменными.

Поля экрана, связанные с приоритетной ротацией и условиями показа:

  "tier"         - "normal" (по умолчанию) | "priority" | "ambient".
                   Задаётся вручную в /screens, НЕ определяется автоматически
                   по содержимому шаблона (угадывание по переменным было бы
                   хрупким). "priority" (ранее "personal" - переименовано,
                   старые значения в screens.json переезжают сами, см.
                   _TIER_ALIASES) - показывается чаще (cfg["boost_priority"]
                   в pc_hud.py) и с правом принудительно прервать текущий
                   показ (экран появился в ротации или сменился контент);
                   "ambient" - чаще (cfg["boost_ambient"]), но БЕЗ права
                   прерывания. ИСКЛЮЧЕНИЕ: у копий repeating-группы "stream"
                   tier="ambient" может быть точечно повышен до "priority"
                   для ОДНОЙ копии, если её "user" совпадает с
                   cfg["my_plex_user"] - см. _stream_copy_tier().

  "trigger_vars" - имена переменных, СМЕНА значения которых на УЖЕ показываемом
                   priority-экране форсирует обновление и продление его
                   duration (например ["media_title", "media_artist"] -
                   смена трека). Это реакция на ИЗМЕНЕНИЕ; порог значения
                   выражается через "conditions" ниже.

  "conditions"   - НОВОЕ. Список ПОРОГОВЫХ условий показа (логика И):
                       {"var": "cpu_pct", "op": ">", "value": 25.0,
                        "for_s": 0.0, "hold_s": 0.0}
                   Это ЖЁСТКИЙ ФИЛЬТР: пока хоть одно условие не выполнено,
                   экран не участвует в ротации вовсе (так же, как экран с
                   None-переменной, см. build_active_screens()). Когда
                   условия начинают выполняться, экран появляется в active -
                   и если он priority, RotationState форсированно прерывает
                   текущий показ (тот же механизм "newly active", что у Now
                   Playing и алерта мониторинга); для normal/ambient он просто
                   входит в обычную ротацию. Только числовые переменные (см.
                   variables.NUMERIC_UNITS); значение None ("нет данных")
                   считается невыполненным условием.
                     for_s  - условие должно выполняться N секунд ПОДРЯД,
                              прежде чем сработать (отсекает короткие всплески);
                     hold_s - после того как условие перестало выполняться,
                              экран остаётся ещё N секунд (защита от дребезга
                              на границе порога).
                   Состояние for_s/hold_s хранит ConditionTracker внутри
                   RotationState. Оно обновляется при каждом вызове
                   current_lines(), т.е. с шагом POLL_INTERVAL главного цикла
                   (по умолчанию 1 с) - значения for_s/hold_s меньше этого
                   шага фактически округляются до него.

Миграции при загрузке screens.json (результат сразу сохраняется на диск):
  - tier "personal" -> "priority";
  - экраны на top_process_* без поля "conditions" получают условие
    top_process_cpu_pct >= <прежний порог из settings.json>: раньше экран
    скрывал сам TopProcessMonitor по настройке top_process_min_cpu_pct (она
    убирается из /settings), теперь это обычное условие экрана.
"""

import json
import operator
import os
import time
import uuid

import templates
import variables

# На Windows %APPDATA% всегда есть (обычно C:\Users\<user>\AppData\Roaming) -
# берём его как базу для конфига. CONFIG_DIR можно переопределить переменной
# окружения.
_DEFAULT_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "win-hud-arduino")
CONFIG_DIR = os.environ.get("CONFIG_DIR", _DEFAULT_CONFIG_DIR)
SCREENS_FILE = os.path.join(CONFIG_DIR, "screens.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")  # только для разовой миграции порога топ-процесса

# Допустимые значения "tier" - см. докстринг модуля.
SCREEN_TIERS = ("normal", "priority", "ambient")

# Прежние названия tier -> текущие. Применяется при загрузке/валидации, так что
# старый screens.json и старые клиенты (если такие остались) не ломаются.
_TIER_ALIASES = {"personal": "priority"}

# Операторы пороговых условий (ASCII в JSON, в интерфейсе рисуются как > < ≥ ≤).
CONDITION_OPS = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
}
CONDITION_MAX_SECONDS = 3600.0  # потолок for_s/hold_s

_TOP_PROCESS_VARS = ("top_process_name", "top_process_cpu_pct", "top_process_ram_pct")
_LEGACY_TOP_PROCESS_MIN_CPU_PCT = 25.0  # прежний дефолт настройки, если settings.json недоступен

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
        "conditions": [],
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
        "conditions": [],
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
        "conditions": [],
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
        "conditions": [],
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
        "conditions": [],
    },
    {
        # media_title/media_artist резолвятся в None, когда сейчас ничего не
        # играет (см. metrics_windows.MediaMonitor) - экран выпадает из
        # ротации через общий механизм build_active_screens().
        #
        # tier="priority": показывается чаще и может прервать текущий показ,
        # если появился (заиграла музыка) или сменился трек во время своего
        # показа - trigger_vars перечисляет переменные, смена которых означает
        # "это другое событие" (media_playing не включаем - он не меняется без
        # смены title/artist).
        "id": "default-nowplaying",
        "name": "Now Playing",
        "l1": "{media_title:16}",
        "l2": "{media_artist:16}",
        "l3": "",
        "duration": 4.0,
        "enabled": True,
        "tier": "priority",
        "trigger_vars": ["media_title", "media_artist"],
        "conditions": [],
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
        "conditions": [],
    },
    {
        # Мониторинг ресурсов (см. metrics_ping.py) - repeating-группа "mon",
        # экран сам размножается по числу целей из /settings (0 целей = экран
        # просто отсутствует).
        #
        # НАМЕРЕННО не используется mon_latency_ms: для offline-целей он None,
        # а любая None-переменная гасит копию экрана - упавший ресурс выпал бы
        # из ротации ровно тогда, когда его статус нужнее всего.
        "id": "default-monitoring",
        "name": "Мониторинг",
        "l1": "{mon_label:16}",
        "l2": "{mon_status:16}",
        "l3": "{mon_since:16}",
        "duration": 4.0,
        "enabled": True,
        "tier": "ambient",
        "trigger_vars": [],
        "conditions": [],
    },
    {
        # Алерт мониторинга - НЕВИДИМ, пока mon_down_names резолвится в None
        # (все ресурсы живы). mon_down_names - скаляр, поэтому у экрана ровно
        # одна копия со всеми именами через запятую.
        #
        # tier="priority" + trigger_vars=["mon_down_names"]: как только хоть
        # один ресурс падает, экран появляется в active и RotationState
        # форсированно прерывает текущий показ; если список упавших меняется
        # во время показа - показ продлевается с новым содержимым.
        "id": "default-monitoring-alert",
        "name": "Алерт мониторинга",
        "l1": "\u26a0 Недоступно:",
        "l2": "{mon_down_names:16}",
        "l3": "",
        "duration": 5.0,
        "enabled": True,
        "tier": "priority",
        "trigger_vars": ["mon_down_names"],
        "conditions": [],
    },
]


# ---------------- валидация ----------------

def _normalize_tier(value):
    """Значение tier с учётом _TIER_ALIASES, либо None, если оно невалидно."""
    value = _TIER_ALIASES.get(value, value)
    return value if value in SCREEN_TIERS else None


def _sanitize_tier(value, fallback="normal"):
    """Невалидное/отсутствующее значение откатывается на fallback, а не
    падает (та же терпимость к мусорному вводу, что у остальных /api/*)."""
    return _normalize_tier(value) or fallback


def _sanitize_trigger_vars(value):
    """Непустые строки без дублей; список (не set) ради стабильного JSON."""
    if not isinstance(value, list):
        return []
    seen = []
    for v in value:
        if isinstance(v, str) and v and v not in seen:
            seen.append(v)
    return seen


def _to_seconds(value):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    if f != f:  # NaN
        return 0.0
    return round(max(0.0, min(CONDITION_MAX_SECONDS, f)), 1)


def _sanitize_conditions(value):
    """Список условий -> только валидные записи. Отбрасываются: не-словари,
    неизвестные/нечисловые переменные, неизвестный оператор, нечисловой
    порог. for_s/hold_s приводятся к 0..CONDITION_MAX_SECONDS."""
    if not isinstance(value, list):
        return []
    out = []
    for c in value:
        if not isinstance(c, dict):
            continue
        var = c.get("var")
        op = c.get("op")
        if not isinstance(var, str) or not variables.is_numeric(var):
            continue
        if op not in CONDITION_OPS:
            continue
        try:
            threshold = float(c.get("value"))
        except (TypeError, ValueError):
            continue
        if threshold != threshold or threshold in (float("inf"), float("-inf")):
            continue
        out.append({
            "var": var, "op": op, "value": threshold,
            "for_s": _to_seconds(c.get("for_s")), "hold_s": _to_seconds(c.get("hold_s")),
        })
    return out


def _backfill_screen_fields(screen):
    """Дополняет/нормализует ОДИН экран (tier/trigger_vars/conditions) -
    файл мог быть сохранён более старой версией. Мутирует и возвращает тот же dict."""
    screen["tier"] = _normalize_tier(screen.get("tier")) or "normal"
    if not isinstance(screen.get("trigger_vars"), list):
        screen["trigger_vars"] = []
    screen["conditions"] = _sanitize_conditions(screen.get("conditions"))
    return screen


# ---------------- миграции ----------------

def _legacy_top_process_threshold():
    """Прежняя настройка top_process_min_cpu_pct из settings.json (она убирается
    из /settings) - нужна один раз, чтобы перенести её в условие экрана."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        return float(saved.get("top_process_min_cpu_pct", _LEGACY_TOP_PROCESS_MIN_CPU_PCT))
    except Exception:
        return _LEGACY_TOP_PROCESS_MIN_CPU_PCT


def _migrate_top_process_conditions(screens):
    """Экраны на top_process_* БЕЗ поля "conditions" (файл старого формата) -
    добавляем top_process_cpu_pct >= прежний порог. Должно вызываться ДО
    _backfill_screen_fields(), которая заводит пустой "conditions"."""
    threshold = None
    for s in screens:
        if not isinstance(s, dict) or "conditions" in s:
            continue
        used = set()
        for key in ("l1", "l2", "l3"):
            used |= set(templates.used_variables(s.get(key, "")))
        if used & set(_TOP_PROCESS_VARS):
            if threshold is None:
                threshold = _legacy_top_process_threshold()
            s["conditions"] = [{
                "var": "top_process_cpu_pct", "op": ">=", "value": threshold,
                "for_s": 0.0, "hold_s": 0.0,
            }]


def load_screens():
    try:
        with open(SCREENS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            before = json.dumps(data, sort_keys=True)
            _migrate_top_process_conditions(data)
            result = [_backfill_screen_fields(s) for s in data]
            if json.dumps(result, sort_keys=True) != before:
                # что-то мигрировало/дополнилось - фиксируем сразу, иначе
                # разовая миграция порога потеряла бы источник (settings.json
                # перестанет хранить top_process_min_cpu_pct)
                try:
                    save_screens(result)
                except OSError:
                    pass
            return result
    except Exception:
        pass
    return [json.loads(json.dumps(s)) for s in DEFAULT_SCREENS]


def save_screens(screens):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SCREENS_FILE, "w", encoding="utf-8") as f:
        json.dump(screens, f, ensure_ascii=False)


# ---------------- CRUD ----------------

def new_screen(name="New screen", l1="", l2="", l3="", duration=4.0, tier="normal",
               trigger_vars=None, conditions=None):
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "l1": l1, "l2": l2, "l3": l3,
        "duration": max(1.0, float(duration)),
        "enabled": True,
        "tier": _sanitize_tier(tier),
        "trigger_vars": _sanitize_trigger_vars(trigger_vars),
        "conditions": _sanitize_conditions(conditions),
    }


def create_screen(screens, data):
    screen = new_screen(
        name=data.get("name", "New screen"),
        l1=data.get("l1", ""), l2=data.get("l2", ""), l3=data.get("l3", ""),
        duration=data.get("duration", 4.0),
        tier=data.get("tier", "normal"),
        trigger_vars=data.get("trigger_vars"),
        conditions=data.get("conditions"),
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
            if "conditions" in data:
                s["conditions"] = _sanitize_conditions(data["conditions"])
            return screens, s
    return screens, None


def delete_screen(screens, screen_id):
    return [s for s in screens if s["id"] != screen_id]


def reorder_screens(screens, id_order):
    by_id = {s["id"]: s for s in screens}
    reordered = [by_id[i] for i in id_order if i in by_id]
    missing = [s for s in screens if s["id"] not in id_order]
    return reordered + missing


# ---------------- пороговые условия ----------------

def _condition_raw(cond, context, index):
    """Выполняется ли условие ПРЯМО СЕЙЧАС (без for_s/hold_s). Нет данных
    (None/не число) - условие невыполнено."""
    value = variables.to_number(variables.resolve(cond["var"], context, index))
    if value is None:
        return False
    return CONDITION_OPS[cond["op"]](value, cond["value"])


class ConditionTracker:
    """
    Состояние for_s/hold_s для условий экранов. Живёт в RotationState (один
    инстанс на процесс), между вызовами build_active_screens() - как и
    остальные "стейты между тиками" (PeakHold, RotationState).

    Ключ состояния - (ключ экрана, номер условия): ключ экрана это screen_id
    либо "screen_id#idx" для копии repeating-экрана. begin()/end() окружают
    один проход build_active_screens(): состояние тех ключей, что в этом
    проходе не проверялись (экран отключён/удалён, условие отредактировано,
    копия исчезла), сбрасывается - при следующем появлении отсчёт for_s
    начнётся заново.

    Правила одного условия:
      - сейчас выполняется: запоминаем момент начала непрерывной серии
        ("since") и последний момент выполнения ("last_true"); как только
        серия длится >= for_s - условие "сработало" (met);
      - сейчас не выполняется: серия рвётся; уже сработавшее условие остаётся
        met, пока с last_true не прошло hold_s, потом гаснет;
      - уже сработавшее условие, снова ставшее true во время удержания,
        остаётся met без повторного ожидания for_s.
    """

    def __init__(self):
        self._states = {}
        self._seen = set()

    def begin(self):
        self._seen = set()

    def end(self):
        for key in list(self._states.keys()):
            if key not in self._seen:
                del self._states[key]

    def check(self, screen_key, conditions, context, index, now):
        """True, если ВСЕ условия экрана сработали. Пустой список - True.
        Не прерывается на первом невыполненном - состояние каждого условия
        должно обновляться на каждом проходе."""
        all_met = True
        for i, cond in enumerate(conditions):
            state_key = (screen_key, i)
            self._seen.add(state_key)
            st = self._states.setdefault(state_key, {"since": None, "met": False, "last_true": None})

            if _condition_raw(cond, context, index):
                if st["since"] is None:
                    st["since"] = now
                st["last_true"] = now
                if now - st["since"] >= cond.get("for_s", 0.0):
                    st["met"] = True
            else:
                st["since"] = None
                if st["met"] and now - st["last_true"] >= cond.get("hold_s", 0.0):
                    st["met"] = False

            if not st["met"]:
                all_met = False
        return all_met


def _conditions_pass(tracker, screen_key, conditions, context, index, now):
    """С трекером - полные правила (for_s/hold_s); без него - только текущее
    значение (например при разовом вызове build_active_screens() без ротации)."""
    if not conditions:
        return True
    if tracker is not None:
        return tracker.check(screen_key, conditions, context, index, now)
    return all(_condition_raw(c, context, index) for c in conditions)


# ---------------- рендер активного списка ----------------

def build_active_screens(screens, context, tracker=None, now=None):
    """
    Возвращает список готовых к показу экранов:
        [{"screen_id": ..., "lines": [l1,l2,l3], "duration": float,
          "tier": "normal"|"priority"|"ambient", "trigger_vars": [str, ...]}, ...]

    tracker (ConditionTracker) и now - для пороговых условий с for_s/hold_s
    (см. докстринг модуля); RotationState передаёт свой трекер на каждом
    вызове. Без трекера условия проверяются по текущему значению.

    Экран попадает в active, только если: включён, все его условия
    выполнены, и ВСЕ переменные в l1/l2/l3 резолвятся (не None).

    Для каждого конкретного экрана допускается не более одной repeating-группы
    (смешивать в одном шаблоне stream_*/mon_*/... не поддерживается - неясно,
    по какому из счётчиков размножать копии). Группу определяют только
    шаблоны строк, не условия: условие на переменную группы у экрана без
    этой группы в шаблонах не выполнится (index не передаётся, резолвер даёт
    None).

    КАК ДЕЛАТЬ "экран не включается, если ...": экран выпадает из ротации,
    если ХОТЬ ОДНА переменная его l1/l2/l3 резолвится в None (см. ok1/ok2/ok3),
    либо не выполнено пороговое условие. НЕ пишите условия видимости здесь -
    пусть resolver в variables.py или источник данных в context кладёт None,
    когда данных "нет по смыслу", либо задайте условие в самом экране.
    """
    now = now if now is not None else time.time()
    active = []
    if tracker is not None:
        tracker.begin()

    for screen in screens:
        if not screen.get("enabled", True):
            continue

        l1, l2, l3 = screen.get("l1", ""), screen.get("l2", ""), screen.get("l3", "")
        groups = set()
        for tpl in (l1, l2, l3):
            groups |= templates.template_group(tpl)

        if len(groups) > 1:
            continue

        tier = _normalize_tier(screen.get("tier")) or "normal"
        trigger_vars = screen.get("trigger_vars") or []
        conditions = screen.get("conditions") or []

        if not groups:
            if not _conditions_pass(tracker, screen["id"], conditions, context, None, now):
                continue
            r1, ok1 = templates.render(l1, context)
            r2, ok2 = templates.render(l2, context)
            r3, ok3 = templates.render(l3, context)
            if ok1 and ok2 and ok3:
                active.append({
                    "screen_id": screen["id"], "lines": [r1, r2, r3], "duration": screen["duration"],
                    "tier": tier, "trigger_vars": trigger_vars,
                })
            continue

        # repeating-группа: N копий экрана. tier/trigger_vars/conditions
        # общие для всех копий; условия проверяются для КАЖДОЙ копии со своим
        # index (условие на mon_latency_ms > 100 - по задержке именно этой
        # цели). ЗА ИСКЛЮЧЕНИЕМ point-override tier для группы "stream" (см.
        # _stream_copy_tier()).
        group_name = next(iter(groups))
        count = variables.group_count(group_name, context)
        for idx in range(count):
            copy_key = f"{screen['id']}#{idx}"
            if not _conditions_pass(tracker, copy_key, conditions, context, idx, now):
                continue
            r1, ok1 = templates.render(l1, context, index=idx)
            r2, ok2 = templates.render(l2, context, index=idx)
            r3, ok3 = templates.render(l3, context, index=idx)
            if ok1 and ok2 and ok3:
                copy_tier = tier
                if group_name == "stream":
                    copy_tier = _stream_copy_tier(tier, idx, context)
                active.append({
                    "screen_id": copy_key,
                    "lines": [r1, r2, r3],
                    "duration": screen["duration"],
                    "tier": copy_tier, "trigger_vars": trigger_vars,
                })

    if tracker is not None:
        tracker.end()
    return active


def _stream_copy_tier(base_tier, idx, context):
    """Point-override tier ОДНОЙ копии repeating-группы "stream": если
    заполнен my_plex_user (/settings, карточка Tautulli) и совпадает с "user"
    именно ЭТОГО сеанса - копия становится "priority" (тот же человек смотрит
    на этом же ПК, событие локальное).

    Апгрейд только если базовый tier экрана - "ambient" (рекомендованная
    настройка для экранов на stream-переменных); явно выбранный другой tier
    point-override не трогает. Чужие сеансы возвращают base_tier как есть."""
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
        return "priority"
    return base_tier


# ---------------- ротация (три дорожки по tier) ----------------

class RotationState:
    """
    Живёт в памяти главного цикла (один инстанс на процесс) - продвигает
    текущий экран по его СОБСТВЕННОМУ duration (пользовательская настройка
    никогда не переопределяется: механизм управляет только тем, КАК ЧАСТО
    экран получает слот), плюс:

      - три независимых "дорожки" (lanes) по tier - priority/ambient/normal -
        каждая крутится по кругу САМА ПО СЕБЕ (round-robin по screen_id
        внутри дорожки) - см. _pick_in_lane();
      - slot_counter решает, чья дорожка получает ПЛАНОВЫЙ слот: priority -
        каждый boost_priority-й, ambient - каждый boost_ambient-й, иначе
        normal (см. _select_tier()). Если "должная" дорожка пуста, слот
        достаётся следующей непустой - "должок" не копится;
      - только priority-дорожка имеет право ФОРСИРОВАННО прервать текущий
        показ: при активации (экран появился в active - в том числе потому,
        что сработали его пороговые условия) или при изменении его
        trigger_vars прямо во время показа (тогда это не смена экрана, а
        продление duration с новым содержимым). ambient не прерывает никогда;
      - если показываемый экран ИСЧЕЗ из active раньше истечения duration
        (событие закончилось / условия перестали выполняться и hold_s
        истёк) - уступаем место немедленно, для ЛЮБОГО tier.

    conditions - ConditionTracker (см. выше) с состоянием for_s/hold_s.
    trigger_fingerprints - кэш последних значений trigger_vars по screen_id
    (нужен priority-экранам с непустым trigger_vars). _prev_active_ids -
    screen_id, активные на ПРЕДЫДУЩЕМ вызове: сравнение с текущим active даёт
    "какие priority-экраны только что активировались".
    """

    def __init__(self):
        self.lane_cursors = {"priority": None, "ambient": None, "normal": None}
        self.slot_counter = 0
        self.current = None  # {"screen_id","tier","lines","duration","started_at"} | None
        self.trigger_fingerprints = {}
        self.conditions = ConditionTracker()
        self._prev_active_ids = set()

    @staticmethod
    def _fingerprint(screen_id, trigger_vars_by_id, context):
        """None, если у экрана нет trigger_vars - иначе кортеж значений его
        trigger_vars ПРЯМО СЕЙЧАС, для сравнения с прошлым вызовом."""
        tvars = trigger_vars_by_id.get(screen_id) or []
        if not tvars:
            return None
        return tuple(variables.resolve(v, context) for v in tvars)

    def _select_tier(self, lanes, boost_priority, boost_ambient):
        """Чья дорожка получает ПЛАНОВЫЙ слот на этот виток. Пустая "должная"
        дорожка молча уступает normal, а если и normal пуста - следующей
        непустой (priority, потом ambient)."""
        priority_boost = max(1, int(boost_priority))
        ambient_boost = max(1, int(boost_ambient))

        if lanes["priority"] and self.slot_counter % priority_boost == 0:
            return "priority"
        if lanes["ambient"] and self.slot_counter % ambient_boost == 0:
            return "ambient"
        if lanes["normal"]:
            return "normal"
        if lanes["priority"]:
            return "priority"
        if lanes["ambient"]:
            return "ambient"
        return None

    def _pick_in_lane(self, tier, lane_ids):
        """Round-robin ВНУТРИ одной дорожки - курсор хранит screen_id (не
        индекс!), т.к. active пересобирается каждый вызов и порядковый индекс
        "поплыл" бы при любом изменении состава активных экранов."""
        if not lane_ids:
            return None
        cursor = self.lane_cursors.get(tier)
        if cursor in lane_ids:
            idx = lane_ids.index(cursor)
            next_id = lane_ids[(idx + 1) % len(lane_ids)]
        else:
            # курсор не найден (первый вызов, либо прошлый экран дорожки
            # исчез из active) - начинаем дорожку заново
            next_id = lane_ids[0]
        self.lane_cursors[tier] = next_id
        return next_id

    def current_lines(self, screens, context, now=None,
                       boost_priority=2, boost_ambient=4,
                       priority_boost_personal=None, priority_boost_ambient=None):
        # priority_boost_personal/priority_boost_ambient - прежние имена
        # параметров (до переименования tier "personal" -> "priority"), приняты
        # как алиасы, чтобы pc_hud.py прежней версии продолжал работать до
        # своего обновления.
        if priority_boost_personal is not None:
            boost_priority = priority_boost_personal
        if priority_boost_ambient is not None:
            boost_ambient = priority_boost_ambient

        now = now if now is not None else time.time()
        active = build_active_screens(screens, context, tracker=self.conditions, now=now)
        active_by_id = {a["screen_id"]: a for a in active}

        if not active:
            self.current = None
            self._prev_active_ids = set()
            return ["", "", ""]

        lanes = {"priority": [], "ambient": [], "normal": []}
        for item in active:
            tier = item.get("tier", "normal")
            if tier not in lanes:
                tier = "normal"
            lanes[tier].append(item["screen_id"])

        trigger_vars_by_id = {item["screen_id"]: item.get("trigger_vars") or [] for item in active}

        # ---- какие priority-экраны только что появились в active (не было
        # на прошлом вызове) - право форс-прерывания текущего показа ----
        newly_active_priority = [sid for sid in lanes["priority"] if sid not in self._prev_active_ids]
        self._prev_active_ids = set(active_by_id.keys())

        # ---- текущий показываемый экран: исчез / контент сменился / жив как есть ----
        if self.current is not None:
            cur_id = self.current["screen_id"]
            cur_tier = self.current["tier"]

            if cur_id not in active_by_id:
                # событие закончилось / условия перестали выполняться -
                # уступаем место немедленно, независимо от tier
                self.current = None
            else:
                # экран всё ещё активен - строки перерисовываем в любом
                # случае (это обновление текста, не смена слота и не
                # продление таймера)
                self.current["lines"] = active_by_id[cur_id]["lines"]
                if cur_tier == "priority":
                    fp = self._fingerprint(cur_id, trigger_vars_by_id, context)
                    if fp is not None and self.trigger_fingerprints.get(cur_id) != fp:
                        # другое событие (сменился трек, изменился список
                        # упавших ресурсов) - форсируем продление duration
                        self.trigger_fingerprints[cur_id] = fp
                        self.current["started_at"] = now

        # ---- форс-активация: priority-экран появился (и это не тот, что
        # уже показывается сейчас) ----
        force_target = None
        for sid in newly_active_priority:
            if self.current is None or self.current["screen_id"] != sid:
                force_target = sid
                break

        if force_target is not None:
            item = active_by_id[force_target]
            self.current = {
                "screen_id": force_target, "tier": "priority",
                "lines": item["lines"], "duration": item["duration"], "started_at": now,
            }
            self.trigger_fingerprints[force_target] = self._fingerprint(force_target, trigger_vars_by_id, context)
            self.lane_cursors["priority"] = force_target
            return self.current["lines"]

        # ---- обычное продолжение текущего показа (duration ещё не истёк) ----
        if self.current is not None and now - self.current["started_at"] < self.current["duration"]:
            return self.current["lines"]

        # ---- плановая смена: duration истёк (или self.current стал None
        # из-за исчезновения экрана) - выбираем следующий по алгоритму ----
        tier = self._select_tier(lanes, boost_priority, boost_ambient)
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
        if tier == "priority":
            self.trigger_fingerprints[screen_id] = self._fingerprint(screen_id, trigger_vars_by_id, context)
        return self.current["lines"]
