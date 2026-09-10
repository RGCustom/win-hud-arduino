"""
osd.py  (win-hud-arduino)

Единая OSD-очередь popup'ов (громкость / раскладка клавиатуры / смена
аудио-устройства вывода) - см. обсуждение в чате про унификацию. Раньше
громкость была ЕДИНСТВЕННЫМ таким popup'ом, реализованным прямой парой
переменных osd_active/osd_until внутри metrics_main_loop (pc_hud.py) с
одной жёстко зашитой веткой if/else. Когда выяснилось, что раскладка
клавиатуры (боль пользователя №1 - "хочу чтоб прям визуально было") и смена
аудио-устройства нуждаются в ТОМ ЖЕ механизме (полная подмена OLED на
короткое время + опционально лента) - плодить вторую/третью такую же пару
переменных и второй/третий if в главном цикле означало бы дублирование
логики. Вместо этого - одна очередь с приоритетом и общим кулдауном, типы
регистрируются декларативно в OSD_TYPES ниже.

Источники триггеров у разных типов - РАЗНЫЕ по природе, и это осознанно не
унифицируется (унифицируется не источник, а то, что в итоге попадает в
очередь через push()):
  - "volume" - дискретное событие С ПЛАТЫ (ENC:/BTN:, см.
               protocol.parse_incoming_line/apply_encoder_delta и
               apply_button_click в pc_hud.py - они и зовут push("volume", ...)
               на каждое событие энкодера, независимо от того, изменилось ли
               реально значение громкости, см. код там же).
  - "layout"  - изменение ЗНАЧЕНИЯ context["keyboard_layout"] МЕЖДУ ТИКАМИ
               (watched-value diff, не событие) - см. metrics_main_loop в
               pc_hud.py, с обязательным фильтром по foreground_pid (см.
               metrics_windows.read_keyboard_state()), чтобы не путать
               реальную смену языка с alt-tab между окнами с разной
               per-window раскладкой Windows.
  - "device"  - тот же watched-value diff по audio_device_name, без фильтра
               по окну (переключение устройства вывода не связано с фокусом
               окна).

Приоритет прерывания (поле "priority" в OSD_TYPES, больше = важнее, см.
OsdManager.push()): layout (3) > volume (2) > device (1) - раскладка бьёт по
главной боли пользователя, поэтому имеет право прервать уже показываемую
громкость/попап устройства немедленно; device - самый низкоприоритетный,
это скорее "к сведению", чем что-то срочное.

Ничего не знает про Flask/serial/сам протокол посылки на плату - чистая
логика очереди + рендереры типов возвращают уже готовые OLED-строки и
(опционально) LED-пиксели. Дальше это уходит в ту же сборку BAR:/L1-3:, что
и обычная ротация экранов (см. metrics_main_loop) - для остального пайплайна
OsdManager неотличим по интерфейсу от screens.RotationState, оба в итоге
просто отдают "что показать прямо сейчас".
"""

from collections import deque

import ledbar


def _center_line(text, width=16):
    """Центрирует текст пробелами под ширину OLED-строки (16 символов) - тот
    же приём центрирования, что и _center_oled_line() в pc_hud.py (см. там
    за обоснованием width=16). Продублировано тут, а не импортировано из
    pc_hud.py - osd.py и так импортируется ИЗ pc_hud.py, обратный импорт
    создал бы цикл. Длинные значения обрезаются, а не скроллятся - так же,
    как и у существующего popup'а громкости."""
    text = str(text)
    if len(text) >= width:
        return text[:width]
    pad = width - len(text)
    left = pad // 2
    return " " * left + text + " " * (pad - left)


def _render_volume(payload, cfg, leds_count):
    """payload: {"volume_pct": int, "muted": bool}. Логика 1-в-1 с прежним
    инлайн-блоком volume OSD в metrics_main_loop - просто переехала сюда без
    изменений (см. ledbar.compute_volume_osd_pixels за деталями геометрии -
    расходится от центра ленты в обе стороны, тревожный цвет при mute/почти
    максимуме)."""
    enc = cfg["encoder"]
    pixels = ledbar.compute_volume_osd_pixels(
        payload["volume_pct"],
        enc["volume_colors"]["c1"], enc["volume_colors"]["c2"], enc["volume_colors"]["c3"],
        muted=payload["muted"],
        mute_color=enc["mute_color"], warning_color=enc["warning_color"],
        warning_threshold_pct=enc["warning_threshold_pct"],
        leds_per_bar=leds_count,
    )
    osd_line = "MUTE" if payload["muted"] else f"Vol {payload['volume_pct']}%"
    lines = ["", _center_line(osd_line), ""]
    return lines, pixels


def _render_layout(payload, cfg, leds_count):
    """payload: {"layout": str}. Крупно показывает код раскладки на средней
    строке OLED + перекрашивает ВСЮ ленту сплошным цветом по
    cfg["layout_colors"] (код -> hex, "_default" - fallback для языков без
    отдельной записи) - см. обоснование в чате: текст на OLED боковым
    зрением не поймать, а вспышку цветом на ленте под монитором - да.

    Сплошной цвет получаем тем же приёмом, что и mute/warning в
    _render_volume (c1=c2=c3=цвет через compute_bar_pixels_flat) - при
    одинаковых трёх стопах результат не зависит от pct, поэтому pct тут
    произвольный (100.0), см. ledbar.compute_bar_pixels_flat()."""
    layout = payload.get("layout") or "??"
    colors = cfg.get("layout_colors") or {}
    color = colors.get(layout) or colors.get("_default") or "808080"
    pixels = ledbar.compute_bar_pixels_flat(100.0, color, color, color, leds_per_bar=leds_count)
    lines = ["", _center_line(layout), ""]
    return lines, pixels


def _render_device(payload, cfg, leds_count):
    """payload: {"device_name": str}. Только OLED-текст - лента НЕ
    подменяется (pixels=None - вызывающий код в pc_hud.py в этом случае
    оставляет пиксели от обычной ротации/метрики нетронутыми, см. докстринг
    OsdManager.render() ниже). Смена устройства вывода - не настолько
    срочное событие, чтобы перекрашивать всю ленту, в отличие от
    layout/volume (см. обсуждение в чате)."""
    name = payload.get("device_name") or "N/A"
    lines = [_center_line("Audio device:"), _center_line(name), ""]
    return lines, None


# ---------------- реестр типов ----------------
# priority - больше = важнее, побеждает при одновременном срабатывании (см.
# OsdManager.push()). hold_seconds_key - какое поле cfg смотреть за
# длительностью показа ИМЕННО этого типа - длительности НЕ унифицируются
# (у каждого типа своя настройка в /settings, см. докстринг модуля).
OSD_TYPES = {
    "layout": {"priority": 3, "hold_seconds_key": "layout_hold_seconds", "render": _render_layout},
    "volume": {"priority": 2, "hold_seconds_key": None, "render": _render_volume},
    "device": {"priority": 1, "hold_seconds_key": "device_hold_seconds", "render": _render_device},
}


def _hold_seconds(osd_type, cfg):
    """"volume" - особый случай: его hold_seconds исторически лежит в
    cfg["encoder"]["osd_hold_seconds"] (эта настройка существовала ДО
    появления единой очереди) - не переносим и не дублируем её в плоский
    cfg[...] ключ ради обратной совместимости с уже сохранёнными на дисках
    пользователей settings.json (иначе значение "потерялось" бы при апгрейде,
    откатившись на дефолт)."""
    if osd_type == "volume":
        return cfg["encoder"]["osd_hold_seconds"]
    key = OSD_TYPES[osd_type]["hold_seconds_key"]
    return cfg.get(key, 2.0)


class OsdManager:
    """
    Единая очередь popup'ов - см. докстринг модуля. Живёт в памяти главного
    цикла, один инстанс на процесс (тот же принцип, что и RotationState в
    screens.py - НЕ смешивается с ней, см. обсуждение в чате про два яруса:
    пока OsdManager что-то показывает, RotationState полностью на паузе, её
    таймеры/курсоры не двигаются - ровно так же, как раньше с volume OSD).
    """

    _PENDING_MAXLEN = 3  # защита от неограниченного роста при шторме
                          # однотипных/разнотипных событий - в реальности
                          # кулдаун ниже почти всегда не даст очереди вырасти

    def __init__(self):
        self._current = None       # {"type":, "payload":, "until":} | None
        self._pending = deque()    # [{"type":, "payload":}, ...]
        self._last_trigger_time = 0.0

    def push(self, osd_type, payload, now, cfg):
        """Добавить/заменить/поставить в очередь - см. правила:
          - повтор ТОГО ЖЕ типа, что уже показывается сейчас, ВСЕГДА проходит
            немедленно (продлевает текущий popup новым payload/hold_seconds) -
            кулдаун ниже к этому случаю не применяется (см. комментарий в
            коде - иначе быстрое вращение энкодера отставало бы от реальной
            громкости);
          - общий кулдаун cfg["osd_cooldown_seconds"] - между срабатываниями
            РАЗНЫХ типов (защита от дребезга источника, см. обсуждение
            п.3.3/4 в чате про быстрые чередования аудио-устройства/
            спецсимволов). Применяется ДАЖЕ к более приоритетному типу -
            иначе кулдаун было бы легко обойти, просто чередуя два типа
            событий подряд;
          - если сейчас ничего не показывается - новый popup становится
            текущим немедленно;
          - если что-то уже показывается и новый ПРИОРИТЕТНЕЕ - текущий
            ЗАМЕНЯЕТСЯ целиком (НЕ довешивается в pending - прерванный
            popup не "доигрывает" остаток, см. обсуждение в чате), новый
            получает свою полную hold_seconds с этого момента;
          - если новый НЕ приоритетнее текущего - встаёт в очередь (FIFO,
            ограниченную _PENDING_MAXLEN)."""
        new_priority = OSD_TYPES[osd_type]["priority"]

        # Повтор ТОГО ЖЕ типа, что уже показывается сейчас - НЕ считается
        # новым "срабатыванием" для целей общего кулдауна ниже. Кулдаун
        # существует для защиты от дребезга ЧУЖОГО источника, прерывающего
        # текущий popup (быстрые чередования устройства вывода/раскладки -
        # см. докстринг класса про п.3.3/4) - он не должен душить частые
        # легитимные повторы ОДНОГО И ТОГО ЖЕ типа. Вращение энкодера шлёт
        # ENC: примерно раз в ENCODER_FLUSH_INTERVAL_MS (~30мс, см. .ino) -
        # apply_encoder_delta() в pc_hud.py меняет системную громкость на
        # КАЖДОЕ такое событие немедленно, но если гасить сам ПОКАЗ (payload
        # popup'а) общим кулдауном 0.5с по умолчанию, до ленты/OLED долетало
        # бы лишь ~2 обновления в секунду вместо ~30 - визуально "крутилка
        # тормозит", хотя реальная громкость меняется мгновенно. Продлеваем
        # текущий popup новым payload/hold_seconds без каких-либо проверок -
        # он и так уже активен, прерывать/заменять тут нечего.
        if self._current is not None and self._current["type"] == osd_type:
            self._current["payload"] = payload
            self._current["until"] = now + _hold_seconds(osd_type, cfg)
            return

        cooldown = cfg.get("osd_cooldown_seconds", 0.5)
        if now - self._last_trigger_time < cooldown:
            return
        self._last_trigger_time = now

        if self._current is None:
            self._current = {"type": osd_type, "payload": payload, "until": now + _hold_seconds(osd_type, cfg)}
            return

        current_priority = OSD_TYPES[self._current["type"]]["priority"]
        if new_priority > current_priority:
            self._current = {"type": osd_type, "payload": payload, "until": now + _hold_seconds(osd_type, cfg)}
            return

        if len(self._pending) < self._PENDING_MAXLEN:
            self._pending.append({"type": osd_type, "payload": payload})

    def tick(self, now, cfg):
        """Вызывается КАЖДЫЙ тик главного цикла, ДО расчёта ленты/OLED (см.
        metrics_main_loop в pc_hud.py) - продвигает очередь по времени и
        возвращает {"type":, "payload":} текущего активного popup'а, либо
        None, если сейчас ничего не показывается (тогда работает обычная/
        приоритетная ротация экранов, см. screens.RotationState)."""
        if self._current is not None and now >= self._current["until"]:
            if self._pending:
                nxt = self._pending.popleft()
                self._current = {
                    "type": nxt["type"], "payload": nxt["payload"],
                    "until": now + _hold_seconds(nxt["type"], cfg),
                }
            else:
                self._current = None

        if self._current is None:
            return None
        return {"type": self._current["type"], "payload": self._current["payload"]}

    def render(self, osd_type, payload, cfg, leds_count):
        """Вызывается pc_hud.py СРАЗУ после tick(), с её результатом.
        Возвращает (oled_lines: [l1, l2, l3], led_pixels: list[str] | None).
        led_pixels=None значит "не подменять ленту" (см. _render_device
        выше) - вызывающий код должен в этом случае оставить пиксели от
        обычной ротации/текущей метрики нетронутыми, а не гасить ленту."""
        return OSD_TYPES[osd_type]["render"](payload, cfg, leds_count)
