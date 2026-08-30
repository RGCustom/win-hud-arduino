"""
protocol.py  (win-hud-arduino)

ИСХОДЯЩИЙ протокол (хост -> плата) - портирован из shkaf-hud практически
без изменений, там ProtocolState.build() и так был полностью общим (работает
с произвольным dict полей, никакого захардкоженного числа баров). Единственная
смысловая разница: вместо BAR1..BAR4 теперь один "BAR" - строка из
leds_per_bar (настройка в /settings, см. ledbar.py) пикселей RRGGBB подряд.

    BAR   - готовый цвет КАЖДОГО светодиода ленты, посчитанный на хосте
             (ledbar.py): leds_per_bar пикселей x RRGGBB подряд без разделителей.
    BRI   - яркость 0-100
    CON   - контраст OLED 0-255 (u8g2.setContrast)
    L1-L3 - три строки OLED (уже отрендеренные шаблонизатором)

Строка шлётся только если что-то реально изменилось с прошлого тика (см.
ProtocolState.build()); раз в FULL_RESYNC_SECONDS - полное состояние целиком,
на случай перезагрузки/потери синхронизации платой.

ВХОДЯЩИЙ протокол (плата -> хост) - НОВОЕ по сравнению с shkaf-hud, где
плата вообще ничего не отправляла обратно. Причина - энкодер громкости живёт
на плате, хосту нужно узнавать о его вращении/клике по serial:

    ENC:<+N|-N>   - энкодер повернулся на N "детентов" с прошлого отчёта
                     (N - целое, положительное = по часовой/громче, см.
                     калибровку направления в прошивке). Прошивка сама
                     дебaунсит/аккумулирует между отправками - хосту всегда
                     приходит уже готовое целое число шагов.
    BTN:CLICK     - однократный клик кнопки энкодера (дебaunс - на плате;
                     хост получает ровно одно сообщение на один клик, не
                     обрабатывает длительность нажатия).

Каждое входящее сообщение - одна строка, парсится независимо по мере
поступления (see pc_hud.py: отдельный поток читает serial построчно и
складывает разобранные события в очередь для главного цикла). Никакой
привязки к тику POLL_INTERVAL - события энкодера должны быть отработаны
как можно быстрее для отзывчивой OSD-индикации громкости.
"""

import time


def pack_bar_pixels(pixels):
    """
    pixels: список из leds_per_bar строк 'RRGGBB' (без '#'), в порядке от
    первого физического светодиода ленты до последнего.

    Просто склеивает их подряд без разделителей - на приёме (parseHex6 в
    .ino) каждый кусок фиксированной длины 6 символов, парсер сам режет
    строку на шестёрки, зная общее число диодов из своего NUM_LEDS.

    -> '00FF0000FF0040FF0080FF00FFFF00FF8000...' (6 * leds_per_bar символов)
    """
    for px in pixels:
        if len(px) != 6:
            raise ValueError(f"pack_bar_pixels: пиксель должен быть 6 hex-символов (RRGGBB), получено {px!r}")
    return "".join(pixels)


class ProtocolState:
    """Исходящий протокол (хост -> плата) - без изменений относительно
    shkaf-hud, см. докстринг модуля."""

    def __init__(self, full_resync_seconds=30):
        self.last = {}
        self.last_full_sync = 0.0
        self.full_resync_seconds = full_resync_seconds

    def build(self, values: dict, now=None):
        """
        values: {"BAR": "...", "BRI": "15", "CON": "255",
                 "L1": "...", "L2": "...", "L3": "..."}
        Возвращает готовую строку для serial (без \\n) или None, если слать нечего.
        """
        now = now if now is not None else time.time()
        force_full = (now - self.last_full_sync) >= self.full_resync_seconds

        changed = {}
        for k, v in values.items():
            if force_full or self.last.get(k) != v:
                changed[k] = v

        self.last.update(values)
        if force_full:
            self.last_full_sync = now

        if not changed:
            return None

        return "|".join(f"{k}:{v}" for k, v in changed.items())

    def reset(self):
        """Форсировать полную пересылку на следующем build() - например,
        сразу после переподключения платы."""
        self.last = {}
        self.last_full_sync = 0.0


def parse_incoming_line(line: str):
    """
    Разбирает ОДНУ строку, пришедшую от платы, в событие для главного цикла.

    Возвращает:
        ("encoder", delta: int)  - энкодер повернулся на delta шагов
        ("button", "click")      - клик кнопки энкодера
        None                      - пустая строка, мусор в порту при
                                    подключении/отладочный вывод прошивки,
                                    или нераспознанный формат - вызывающий
                                    код просто игнорирует такие строки, а не
                                    падает (serial - не идеальный канал, шум
                                    на подключении - обычное дело).
    """
    line = line.strip()
    if not line:
        return None

    if line.startswith("ENC:"):
        try:
            delta = int(line[4:])
        except ValueError:
            return None
        if delta == 0:
            return None
        return ("encoder", delta)

    if line == "BTN:CLICK":
        return ("button", "click")

    return None
