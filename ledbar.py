"""
ledbar.py  (win-hud-arduino)

Расчёт цвета КАЖДОГО светодиода ленты на хосте - плата ничего не считает,
только раскладывает уже готовый массив цветов по своим физическим диодам
(см. LED_MAP в .ino). Портировано из проекта shkaf-hud почти без изменений -
сама математика градиента не завязана на конкретное железо.

ГЛАВНОЕ ОТЛИЧИЕ ОТ shkaf-hud: там LEDS_PER_BAR - константа модуля, жёстко
совпадающая с прошивкой (4 бара по 12 диодов, число зашито и в .ino, и тут).
В win-hud-arduino железо - ОДНА лента переменной длины (~30 диодов на
старте), и число диодов - НАСТРОЙКА в /settings (settings.json), а не
константа кода. LEDS_PER_BAR ниже оставлен только как fallback-дефолт для
прямых вызовов/самотеста - в реальной работе pc_hud.py всегда передаёт
leds_per_bar явно, беря его из cfg["leds_count"].

Также: в shkaf-hud было 4 независимых бара (bar0..bar3), в win-hud-arduino
бар один - но все функции ниже как были рассчитаны на ОДНУ полосу диодов
произвольной длины, так и остались. Вызывающий код (pc_hud.py) просто зовёт
их один раз за тик, а не четыре.

Режимы:

  - "classic" - обычный градиент снизу вверх (для горизонтальной ленты
    "снизу вверх" читается как "слева направо" - направление физическое,
    определяется только порядком в LED_MAP на плате):
    compute_bar_pixels(pct, c1, c2, c3, solid, ...)

  - "center" - лента растёт от центра в обе стороны, у каждой половины
    свой % заполнения/градиент/solid:
    compute_bar_pixels_center(pct_bottom, pct_top, ...)

  - "volume_osd" - НЕ отдельный режим здесь, а частный случай center с
    ОДИНАКОВЫМ pct и ОДИНАКОВЫМИ цветами на обе половины (см. комментарий
    "Зеркальность" в shkaf-hud - там та же идея). Это ровно то, что нужно
    для индикации громкости: значение одно (volume_pct), а рисуется оно
    симметрично от центра ленты в обе стороны. Обёрнуто в отдельную функцию
    compute_volume_osd_pixels() ниже - чисто для читаемости вызывающего
    кода в pc_hud.py (не пришлось дублировать pct/цвета дважды на вызов).
    Переключение "обычная метрика <-> OSD громкости" - НЕ логика этого
    файла, а стейт-машина с таймером в главном цикле (см. pc_hud.py):
    ledbar.py просто считает пиксели для того, что ему передали на этот тик.

  - Peak hold - независимая "точка недавнего максимума", включается поверх
    ЛЮБОГО режима (см. класс PeakHold ниже) - логика не изменилась
    относительно shkaf-hud.

Логика градиента внутри одной "полосы" диодов (solid-чекбокс "цвет на 100%"):

  - solid = False: обычный 3-стопный градиент по всей длине полосы, всегда
    c1 (начало) -> c2 (середина) -> c3 (конец), независимо от текущего pct.
  - solid = True:
      - pct < 100: градиент только между c1 и c2 (2 стопа) - c3 не участвует.
      - pct >= 100: вся заполненная полоса заливается сплошным c3.
  - Уровни за пределами заполненности (level >= lit) - чёрные (погашены),
    кроме случая, когда на этот уровень как раз легла точка peak hold.
"""

LEDS_PER_BAR = 30  # fallback-дефолт для прямых вызовов/самотеста - в работе
                    # всегда переопределяется cfg["leds_count"] из settings.json

# Дефолтные тайминги peak hold - используются, если у ленты в settings.json
# ещё нет hold_seconds/fade_seconds (старый конфиг) или значение не задано.
PEAK_HOLD_SECONDS = 2.0   # style="hold": сколько точка стоит неподвижно, прежде чем погаснуть
PEAK_FADE_SECONDS = 1.5   # style="fade": за сколько точка плавно съезжает вниз к текущему pct


def _hex_to_rgb(hex_str):
    h = hex_str.strip().lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _rgb_to_hex(rgb):
    r, g, b = (max(0, min(255, round(v))) for v in rgb)
    return f"{r:02X}{g:02X}{b:02X}"


def _blend(rgb_a, rgb_b, amount):
    """amount: 0.0 (чистый rgb_a) .. 1.0 (чистый rgb_b)."""
    amount = max(0.0, min(1.0, amount))
    return tuple(a + (b - a) * amount for a, b in zip(rgb_a, rgb_b))


def _gradient_pixels(pct, c1_hex, c2_hex, c3_hex, solid, count):
    """
    Общее ядро градиента на ПОЛОСЕ произвольной длины count (для classic-режима
    count = вся лента; для center-режима/volume OSD count = длина одной
    половины). Индекс 0 = "начало" полосы (там, где pct=0% ничего не горит,
    а рост идёт от него), индекс count-1 = "конец" (там же, где c3 при pct=100%).

    Возвращает список из count строк 'RRGGBB' (без '#', верхний регистр).
    """
    pct = max(0.0, min(100.0, pct))
    lit = round(pct / 100.0 * count)
    lit = max(0, min(count, lit))

    c1, c2, c3 = _hex_to_rgb(c1_hex), _hex_to_rgb(c2_hex), _hex_to_rgb(c3_hex)

    pixels = []
    for level in range(count):
        if level >= lit:
            pixels.append("000000")
            continue

        if solid and pct >= 100:
            pixels.append(_rgb_to_hex(c3))
            continue

        frac = level / (count - 1) if count > 1 else 0.0

        if solid:
            # галка стоит, но ещё не 100% - только c1 -> c2 по всей длине
            rgb = _blend(c1, c2, frac)
        else:
            # обычный 3-стопный градиент: c1 -> c2 -> c3
            if frac <= 0.5:
                rgb = _blend(c1, c2, frac / 0.5)
            else:
                rgb = _blend(c2, c3, (frac - 0.5) / 0.5)

        pixels.append(_rgb_to_hex(rgb))

    return pixels


def _apply_peak(pixels, peak_pct, color_hex, count):
    """Перекрашивает ОДИН диод (тот, что соответствует уровню peak_pct) в
    color_hex - поверх уже посчитанного градиента. Не мутирует исходный список."""
    idx = round(peak_pct / 100.0 * count) - 1
    idx = max(0, min(count - 1, idx))
    out = list(pixels)
    out[idx] = color_hex.strip().lstrip("#").upper()
    return out


def compute_bar_pixels(pct, c1_hex, c2_hex, c3_hex, solid, leds_per_bar=LEDS_PER_BAR, peak_pct=None):
    """
    Classic-режим: обычный градиент по всей длине ленты.
    peak_pct - опционально 0-100 - если задано, поверх градиента подсвечивается
    точка недавнего максимума цветом c3.

    Возвращает список из leds_per_bar строк 'RRGGBB'.
    """
    pixels = _gradient_pixels(pct, c1_hex, c2_hex, c3_hex, solid, leds_per_bar)
    if peak_pct is not None:
        pixels = _apply_peak(pixels, peak_pct, c3_hex, leds_per_bar)
    return pixels


def compute_bar_pixels_center(
    pct_bottom, pct_top,
    b_c1, b_c2, b_c3, b_solid,
    t_c1, t_c2, t_c3, t_solid,
    leds_per_bar=LEDS_PER_BAR,
    peak_pct_bottom=None, peak_pct_top=None,
):
    """
    Center-режим: лента разбита на две половины, каждая растёт от центра к
    своему краю независимо (своя метрика/цвета/solid/peak на каждую).
    "bottom"/"top" - исторические имена из shkaf-hud (там это буквально
    низ/верх вертикального бара); в win-hud-arduino это левая/правая
    половина горизонтальной ленты - смысл тот же, физическое направление
    определяется LED_MAP на плате.

    bottom_count = leds_per_bar // 2, top_count = остаток - если leds_per_bar
    нечётный, лишний диод достаётся "верхней"/правой половине.

    Возвращает ПОЛНЫЙ список из leds_per_bar строк 'RRGGBB' (порядок - как
    ожидает протокол/прошивка).
    """
    bottom_count = leds_per_bar // 2
    top_count = leds_per_bar - bottom_count

    bottom_half = _gradient_pixels(pct_bottom, b_c1, b_c2, b_c3, b_solid, bottom_count)
    if peak_pct_bottom is not None:
        bottom_half = _apply_peak(bottom_half, peak_pct_bottom, b_c3, bottom_count)
    bottom_half = list(reversed(bottom_half))

    top_half = _gradient_pixels(pct_top, t_c1, t_c2, t_c3, t_solid, top_count)
    if peak_pct_top is not None:
        top_half = _apply_peak(top_half, peak_pct_top, t_c3, top_count)

    return bottom_half + top_half


def compute_volume_osd_pixels(volume_pct, c1_hex, c2_hex, c3_hex, leds_per_bar=LEDS_PER_BAR):
    """
    Пиксели для OSD-попапа громкости: одно значение (volume_pct), рисуется
    СИММЕТРИЧНО от центра ленты в обе стороны - частный случай
    compute_bar_pixels_center() с одинаковым pct и одинаковыми цветами на
    обе половины (см. пояснение "Зеркальность" в докстринге модуля выше).

    Без peak hold и без solid-режима - для OSD это лишнее: попап и так живёт
    считанные секунды (см. таймер в pc_hud.py), пик держать незачем.

    Возвращает список из leds_per_bar строк 'RRGGBB'.
    """
    return compute_bar_pixels_center(
        volume_pct, volume_pct,
        c1_hex, c2_hex, c3_hex, False,
        c1_hex, c2_hex, c3_hex, False,
        leds_per_bar=leds_per_bar,
    )


class PeakHold:
    """
    Отслеживает "недавний максимум" одного канала (вся лента в classic-режиме,
    либо одна половина в center-режиме) во времени - для VU-meter-style точки.
    Стейт живёт в инстансе, вызывающий код (главный цикл) раз в тик зовёт
    update(pct, now). Логика идентична shkaf-hud - см. комментарии там же.

    style="hold" - точка держится hold_seconds неподвижно, потом гаснет разом.
    style="fade" - точка плавно линейно едет вниз к текущему pct за fade_seconds.
    """

    def __init__(self, style="hold", hold_seconds=PEAK_HOLD_SECONDS, fade_seconds=PEAK_FADE_SECONDS):
        self.set_style(style)
        self.hold_seconds = hold_seconds
        self.fade_seconds = fade_seconds
        self.peak_pct = 0.0
        self.peak_time = 0.0

    def set_style(self, style):
        self.style = style if style in ("hold", "fade") else "hold"

    def set_timings(self, hold_seconds=None, fade_seconds=None):
        if hold_seconds is not None:
            self.hold_seconds = max(0.0, min(10.0, hold_seconds))
        if fade_seconds is not None:
            self.fade_seconds = max(0.0, min(10.0, fade_seconds))

    def update(self, pct, now):
        """Возвращает peak_pct (0-100) для отрисовки точки, либо None, если
        отдельную точку сейчас рисовать не нужно."""
        pct = max(0.0, min(100.0, pct))

        if pct >= self.peak_pct:
            self.peak_pct = pct
            self.peak_time = now
            return None

        elapsed = now - self.peak_time

        if self.style == "hold":
            if elapsed >= self.hold_seconds:
                self.peak_pct = pct
                return None
            return self.peak_pct

        if self.fade_seconds <= 0 or elapsed >= self.fade_seconds:
            self.peak_pct = pct
            return None
        frac = elapsed / self.fade_seconds
        current = self.peak_pct - (self.peak_pct - pct) * frac
        return current if current > pct else None
