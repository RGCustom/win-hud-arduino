"""
templates.py  (win-hud-arduino)

Парсинг и рендер шаблонов строк L1/L2/L3. Портировано из shkaf-hud без
изменений логики - модуль работает через variables.resolve()/variables.VARIABLES
и ничего не знает о том, какие конкретно переменные в реестре (Windows/GPU/
звук здесь, вместо Tautulli/qBittorrent там) - вся специфика живёт в
variables.py, этот файл её не касается.

Синтаксис:
    Обычный текст, {имя_переменной} для подстановки значения.
    {имя_переменной:N}     - обрезать/дополнить пробелами СПРАВА до ровно N
                              символов (берутся ПЕРВЫЕ N символов значения)
    {имя_переменной:-N}    - обрезать/дополнить пробелами СЛЕВА до ровно N
                              символов (берутся ПОСЛЕДНИЕ N символов значения) -
                              удобно для "хвоста" длинных значений, например
                              {audio_device_name:-16} покажет конец названия
                              устройства вместо начала
    {имя_переменной:.2f}   - любой другой спецификатор идёт напрямую в Python format()

Примеры (под переменные win-hud-arduino):
    "CPU {cpu_pct}% GPU {gpu_pct}%"
    "Vol {volume_pct}% {volume_muted}"
    "{keyboard_layout} {time_now}"
    "{audio_device_name:-16}"   - последние 16 символов имени устройства

Если хоть одна переменная в шаблоне не резолвится (None - например net2/диск2
не выбран) - render() возвращает all_resolved=False, чтобы screens.py мог
решить не показывать такой экран.
"""

import re

import variables

TOKEN_RE = re.compile(r"\{([a-zA-Z0-9_]+)(?::([^}]*))?\}")


def parse_template(template: str):
    """Возвращает список токенов: ('text', str) или ('var', name, spec|None)."""
    tokens = []
    pos = 0
    for m in TOKEN_RE.finditer(template):
        if m.start() > pos:
            tokens.append(("text", template[pos:m.start()]))
        name = m.group(1)
        spec = m.group(2)
        tokens.append(("var", name, spec))
        pos = m.end()
    if pos < len(template):
        tokens.append(("text", template[pos:]))
    return tokens


def format_value(value, spec):
    if spec is None or spec == "":
        return str(value)
    if spec.isdigit():
        # {var:N} - обрезка/дополнение СПРАВА: первые N символов, остаток
        # (если короче) дополняется пробелами справа (ljust).
        n = int(spec)
        s = str(value)
        return s[:n] if len(s) > n else s.ljust(n)
    if spec.startswith("-") and spec[1:].isdigit():
        # {var:-N} - обрезка/дополнение СЛЕВА: последние N символов, остаток
        # (если короче) дополняется пробелами слева (rjust). Симметрично
        # {var:N} выше - удобно для "хвоста" длинных значений (например
        # окончание audio_device_name, если начало малоинформативно).
        n = int(spec[1:])
        s = str(value)
        return s[-n:] if len(s) > n else s.rjust(n)
    try:
        return format(value, spec)
    except (ValueError, TypeError):
        return str(value)


def render(template: str, context: dict, index=None):
    """Рендерит шаблон. Возвращает (готовая_строка, all_resolved: bool)."""
    if not template:
        return "", True

    tokens = parse_template(template)
    out = []
    all_resolved = True

    for tok in tokens:
        if tok[0] == "text":
            out.append(tok[1])
        else:
            _, name, spec = tok
            value = variables.resolve(name, context, index)
            if value is None:
                all_resolved = False
                out.append("")
            else:
                out.append(format_value(value, spec))

    return "".join(out), all_resolved


def used_variables(template: str):
    """Список имён переменных, встречающихся в шаблоне - для валидации в веб-интерфейсе."""
    return [tok[1] for tok in parse_template(template) if tok[0] == "var"]


def validate_template(template: str):
    """Список опечаток - переменных, которых нет в реестре. Пустой список = всё ок."""
    return [name for name in used_variables(template) if name not in variables.VARIABLES]


def template_group(template: str):
    """
    Определяет, к какой "повторяющейся" группе относится шаблон, либо None,
    если шаблон использует только скалярные переменные. В win-hud-arduino
    variables.REPEATING_GROUPS пуст (см. variables.py) - эта функция всегда
    будет возвращать пустой set(), т.к. в реестре просто нет переменных с
    group != "scalar". Оставлено без изменений ради совместимости со
    screens.py - логика там уже готова к "экрану без повторяющихся групп",
    отдельно ветку под этот случай выделять не пришлось.
    """
    groups = set()
    for name in used_variables(template):
        spec = variables.VARIABLES.get(name)
        if spec and spec["group"] != "scalar":
            groups.add(spec["group"])
    return groups
