"""
history.py  (win-hud-arduino)

Короткая история числовых метрик (CPU/RAM/GPU/...) для мини-графиков
(спарклайнов) на OLED - см. cpu_graph/ram_graph/gpu_graph/... в variables.py
за тем, как это подключается к шаблонам, и pc_hud.py (metrics_main_loop) за
тем, откуда берутся сами значения (переиспользуются те же common_metrics,
что уже считаются для ленты - никакого нового источника данных не нужно).

Зачем (ts, value), а не просто список последних N значений: sparkline()
должен уметь ответить на вопрос "как менялась метрика за последние
window_seconds секунд" НЕЗАВИСИМО от того, как часто реально вызывался
record() - а POLL_INTERVAL (частота, с которой пишутся точки, см.
pc_hud.py) живая настройка и может измениться на лету. Если бы буфер хранил
только значения фиксированной длины, смена POLL_INTERVAL сдвигала бы
"что значит последние N секунд" без предупреждения. С таймстемпами это
просто окно по времени - resample_to_width() сам решает, сколько точек
куда попало.

Отрисовка - НЕ шрифтовыми символами (проверено на реальном железе - шрифты
u8g2, кроме крупного unifont, не содержат Block Elements, а unifont слишком
высокий для трёх обычных строк OLED). Вместо этого _BLOCKS ниже - control-
байты chr(1)..chr(8) (высота уровня 1-8), которые прошивка распознаёт ПО
ЗНАЧЕНИЮ БАЙТА и рисует примитивом drawVLine()/drawBox() поверх обычного
текста строки - см. drawLineWithBars() в прошивке. Эти байты физически не
встречаются в обычном ASCII/UTF-8/кириллическом тексте, поэтому отдельный
escape-символ не нужен - прошивка просто переключается между "печатать
текст" и "рисовать столбик" по диапазону байта. Пробел (0x20, _NO_DATA_CHAR
ниже) вне этого диапазона - печатается как обычный пробел, никакой особой
обработки на приёме не требует.

Для остального хост-кода (variables.py/templates.py/pc_hud.py) это всё
равно просто Python-строка фиксированной длины - обрезка/паддинг по
{cpu_graph:N} в templates.format_value() работает ровно как раньше, разница
только в том, ЧТО именно лежит внутри строки."""

import time
from collections import deque

# 8 уровней высоты столбика, от самого низкого (1) до полного (8) - НЕ
# печатные символы, а control-байты chr(1)..chr(8) (0x01-0x08). Прошивка
# ловит их по диапазону значения байта и рисует drawVLine()/drawBox() той
# высоты вместо печати глифа - см. докстринг модуля выше. Начинаем с 1, а
# не с 0 - 0x00 - это terminator C-строки, слать его посреди L1/L2/L3
# опасно (прошивка на стороне .ino может читать строку как null-terminated
# и просто обрежет всё после первого 0x00).
_BLOCKS = "".join(chr(level) for level in range(1, 9))  # chr(1)..chr(8)

# "Данных пока нет" (буфер только что создан / этот участок окна ещё не
# набрал ни одного сэмпла - например только что стартовало приложение) -
# обычный пробел (0x20) - вне диапазона control-байтов выше, поэтому
# прошивка просто печатает его как есть, без особой обработки. ОТДЕЛЬНЫЙ
# символ от _BLOCKS[0], т.к. "0%" - валидное значение метрики (например CPU
# реально простаивает), а "нет данных" - другое состояние, путать их нельзя
# (тот же принцип None vs 0, что и у остальных метрик проекта - см.
# variables.py).
_NO_DATA_CHAR = " "

DEFAULT_WIDTH = 8
MIN_WIDTH = 1
MAX_WIDTH = 16  # OLED-строка - 16 символов, больше графику отдавать бессмысленно

DEFAULT_WINDOW_SECONDS = 60.0  # "последние N секунд" - пока константа, не
                                 # настройка в /settings (см. обсуждение в
                                 # чате - можно вынести туда же паттерном,
                                 # что и peak_hold_seconds, отдельным шагом)

# Запас буфера с большим избытком относительно DEFAULT_WINDOW_SECONDS -
# реально нужен только на случай, если POLL_INTERVAL когда-нибудь резко
# уменьшат (сэмплы пишутся раз в POLL_INTERVAL, см. pc_hud.py). 600 точек
# при сэмплировании раз в секунду - 10 минут истории, с большим запасом
# даже для window_seconds заметно больше дефолтных 60.
_BUFFER_MAXLEN = 600


def _value_to_block(pct):
    """0-100 -> один символ из _BLOCKS. Округление к ближайшему уровню (не
    отбрасывание дробной части) - иначе значения чуть ниже границы уровня
    систематически показывались бы на уровень ниже, чем должны."""
    pct = max(0.0, min(100.0, pct))
    level = int(pct / 100.0 * (len(_BLOCKS) - 1) + 0.5)
    return _BLOCKS[level]


class MetricHistory:
    """
    Кольцевые буферы (ts, value) по каждой метрике (ключ - произвольная
    строка, см. GRAPH_METRIC_KEYS в variables.py за тем, какие ключи реально
    используются). Один инстанс на процесс, живёт в главном цикле
    (metrics_main_loop, pc_hud.py) - тот же принцип, что у peak_trackers/
    rotation/osd_manager там же: состояние между тиками, никакой персистентности
    на диск не нужно (график истории теряется при перезапуске приложения -
    осознанно, как и peak hold).
    """

    def __init__(self):
        self._buffers = {}  # metric_key -> deque[(ts, value)]

    def record(self, metric_key, value, now=None):
        """value - 0-100 (проценты), как и остальные common_metrics в
        pc_hud.py (та же шкала, что использует ledbar.py для ленты) - единая
        шкала для всех метрик упрощает _value_to_block() (не нужно знать
        per-метрику диапазон/единицы)."""
        now = now if now is not None else time.time()
        buf = self._buffers.setdefault(metric_key, deque(maxlen=_BUFFER_MAXLEN))
        buf.append((now, value))

    def sparkline(self, metric_key, width=DEFAULT_WIDTH, window_seconds=DEFAULT_WINDOW_SECONDS, now=None):
        """
        Строка из width символов - последние window_seconds секунд истории
        metric_key, разбитые на width равных по времени "корзин", в каждой -
        СРЕДНЕЕ значение попавших туда сэмплов. Пустая корзина (сэмплов не
        попало - например приложение только что запустилось, либо метрика
        ни разу не писалась под этим ключом) -> _NO_DATA_CHAR.

        Буфера для metric_key ещё нет вовсе (опечатка в ключе, либо ключ
        реально никогда не пишется) - пустой график целиком (тот же
        _NO_DATA_CHAR * width), а не исключение - resolve() в variables.py
        и так гасит любые исключения резолверов, но graph-резолверам
        удобнее получать предсказуемую строку фиксированной длины даже в
        вырожденном случае, а не думать об этом отдельно.
        """
        width = max(MIN_WIDTH, min(MAX_WIDTH, int(width)))
        now = now if now is not None else time.time()

        buf = self._buffers.get(metric_key)
        if not buf:
            return _NO_DATA_CHAR * width

        window_start = now - window_seconds
        bucket_seconds = window_seconds / width if width > 0 else window_seconds
        buckets = [[] for _ in range(width)]

        for ts, value in buf:
            if ts < window_start:
                continue
            if bucket_seconds <= 0:
                bucket_idx = width - 1
            else:
                bucket_idx = int((ts - window_start) / bucket_seconds)
                bucket_idx = max(0, min(width - 1, bucket_idx))
            buckets[bucket_idx].append(value)

        chars = []
        for bucket in buckets:
            if not bucket:
                chars.append(_NO_DATA_CHAR)
            else:
                chars.append(_value_to_block(sum(bucket) / len(bucket)))
        return "".join(chars)
