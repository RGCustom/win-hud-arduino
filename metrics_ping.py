"""
metrics_ping.py  (win-hud-arduino)

Мониторинг произвольных ресурсов (IP/домен, опционально порт) - "жив ли
роутер/NAS/VPN-эндпоинт" и т.п. См. обсуждение в чате: пользователь заранее
не знает, сколько ресурсов будет мониторить - поэтому это REPEATING-группа
для OLED-шаблонов ("mon", см. variables.py), а не фиксированные слоты типа
disk1/disk2 - список целей задаётся и меняется в /settings (cfg["mon_targets"]),
экран с {mon_label}/{mon_status}/... сам размножается по текущему числу
настроенных целей, тем же уже готовым механизмом, что и Plex-сеансы
(build_active_screens() в screens.py - никаких изменений там не понадобилось).

Опрашивается ОТДЕЛЬНЫМ фоновым потоком в pc_hud.py (monitor_loop, по
аналогии с integrations_loop), НЕ главным циклом - см. обоснование у
INTEGRATIONS_POLL_INTERVAL в pc_hud.py: сетевая проверка с таймаутом не
должна блокировать VU/BAR на TICK_INTERVAL. Интервал у пинга концептуально
другой (минуты, не секунды) - отдельная настройка cfg["ping_interval_seconds"],
отдельный поток, не смешивается с Tautulli/qBittorrent.

---- Способ проверки (на КАЖДУЮ цель независимо, см. target["port"]) ----

  - host БЕЗ port -> ICMP ping через системный ping.exe (subprocess) -
    НЕ raw-сокет, поэтому админ-права не нужны (та же логика, что у
    остальных внешних инструментов проекта - avrdude, тоже просто внешний
    процесс). Статус смотрим ТОЛЬКО по returncode (0 = успех) - никогда не
    парсим текстовый вывод: он локализован (на русской Windows "Превышен
    интервал ожидания" вместо "Request timed out") и был бы хрупким, в то
    время как returncode одинаков независимо от языка ОС.

  - host С port -> TCP-коннект (socket.connect_ex) - полезно для ресурсов,
    которые сами блокируют ICMP (частый случай у облачных
    балансировщиков/VPN), а важен конкретный сервис (веб-морда на 443,
    SSH на 22 и т.п.). Чистый stdlib, без внешнего процесса.

  ВАЖНО про latency_ms при ICMP: это время выполнения ВСЕГО вызова
  subprocess (запуск процесса + сам ping + его завершение), а НЕ "чистый"
  RTT из вывода ping.exe (мы его принципиально не парсим, см. выше) - то
  есть значение чуть завышено относительно настоящего сетевого времени
  (накладные расходы на запуск процесса, обычно единицы-десятки мс на
  современной машине). Для OLED-индикатора "быстро/медленно" этого более
  чем достаточно - если нужна точная RTT, используйте TCP-проверку (там
  latency_ms - честное время самого socket.connect(), без обвязки процесса).

---- Гистерезис (защита от дребезга - одна потерянная посылка не должна
     мгновенно перекрашивать статус и дёргать OSD/экран-алерт) ----

  cfg["ping_fail_threshold"]/["ping_recover_threshold"] - сколько ПОДРЯД
  идущих проверок нужно, прежде чем реально сменить status:
    - offline требует fail_threshold провалов подряд (дефолт 2 - один
      потерянный пакет ещё не авария)
    - online требует recover_threshold успехов подряд (дефолт 1 - "ожил"
      не настолько рискованно поспешить признать, как ложное "упал")
  ПЕРВАЯ проверка цели (state.status is None) - статус выставляется сразу
  по результату, без ожидания порога, иначе первые fail_threshold-1 тиков
  после запуска приложения показывали бы "неизвестно" молча.

Состояние (PingMonitor._states) хранится ПО id ЦЕЛИ, а не по host/индексу
в списке - id стабилен при переименовании/правке host существующей цели в
/settings (см. CRUD в pc_hud.py), а порядок/название меняться могут.
Состояние удалённых из /settings целей чистится каждый read() - тот же
принцип, что и у TopProcessMonitor._procs в metrics_windows.py.

Ничего не знает про Flask/шаблоны/протокол - чистый сбор данных, тот же
контракт, что у TautulliClient/QbittorrentClient (.read(...) -> dict,
готовый к прямому merge в context в pc_hud.py).
"""

import socket
import subprocess
import sys
import time

REQUEST_TIMEOUT_DEFAULT_MS = 800

# Подавляет мелькание чёрного консольного окна при запуске ping.exe -
# критично именно для этого проекта: сборка PyInstaller идёт с --windowed
# (см. build.ps1/README), и БЕЗ этого флага каждый subprocess.run() ниже на
# долю секунды показывал бы пользователю консольное окно поверх остальных
# приложений - subprocess по умолчанию наследует поведение родителя не
# полностью в этом отношении на Windows. На не-Windows платформах модуль всё
# равно не имеет смысла (весь проект - Windows-only, см. README), но флаг
# сделан условным, чтобы файл хотя бы импортировался без AttributeError при
# случайном запуске тестов на другой ОС.
_WINDOWS_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _check_tcp(host, port, timeout_ms):
    """(ok: bool, latency_ms: int|None). latency_ms - честное время самого
    socket.connect() (см. докстринг модуля про разницу с ICMP-веткой)."""
    timeout_s = max(0.05, timeout_ms / 1000.0)
    t0 = time.perf_counter()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            result = s.connect_ex((host, port))
    except (socket.gaierror, OSError):
        return False, None
    latency_ms = round((time.perf_counter() - t0) * 1000)
    ok = result == 0
    return ok, (latency_ms if ok else None)


def _check_icmp(host, timeout_ms):
    """(ok: bool, latency_ms: int|None) - см. докстринг модуля за тем, что
    именно измеряет latency_ms тут (не чистый RTT, время всего процесса)."""
    timeout_ms = max(1, int(timeout_ms))
    cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_ms / 1000.0 + 2.0,  # запас поверх -w на случай
                                                  # медленного старта самого
                                                  # процесса ping.exe
            creationflags=_WINDOWS_NO_WINDOW,
        )
    except (subprocess.SubprocessError, OSError):
        return False, None
    latency_ms = round((time.perf_counter() - t0) * 1000)
    ok = proc.returncode == 0
    return ok, (latency_ms if ok else None)


def _format_since(ts, now):
    """Секунды с момента последней смены статуса -> '5m'/'2h'/'3d' - тот же
    формат/приём, что и _format_ago() в metrics_tautulli.py (латиница
    короче кириллицы на узком OLED)."""
    secs = max(0, now - ts)
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


class _TargetState:
    """Состояние ОДНОЙ цели между вызовами read() - см. докстринг модуля
    про гистерезис за смыслом полей."""
    __slots__ = ("status", "consecutive_fail", "consecutive_success", "latency_ms", "last_change_ts")

    def __init__(self):
        self.status = None  # None = ни разу не проверяли; иначе bool
        self.consecutive_fail = 0
        self.consecutive_success = 0
        self.latency_ms = None
        self.last_change_ts = 0.0


class PingMonitor:
    """Обёртка с состоянием между вызовами read() (гистерезис по каждой
    цели) - один инстанс на процесс, живёт в фоновом потоке monitor_loop
    (pc_hud.py), тот же принцип, что и TautulliClient/QbittorrentClient."""

    def __init__(self):
        self._states = {}  # target id -> _TargetState

    def read(self, targets, timeout_ms=REQUEST_TIMEOUT_DEFAULT_MS,
              fail_threshold=2, recover_threshold=1):
        """
        targets: [{"id": str, "label": str, "host": str, "port": int|None}, ...]
                 - живая настройка из /settings (cfg["mon_targets"]), см.
                 докстринг модуля. Записи без id/host пропускаются молча
                 (например незаполненная "новая" строка в редакторе).

        Возвращает dict:
            mon               - repeating-группа для variables.py:
                                 [{"label","host","status","latency_ms","since"}, ...]
                                 status - "online"/"offline" (строка, не bool -
                                 напрямую подставляется в OLED-шаблон)
            mon_down_count    - int, сколько целей сейчас offline (0, если
                                 все живы ИЛИ целей не настроено вовсе)
            mon_down_names    - строка с именами упавших через ", ", ЛИБО
                                 None, если всё живо - см. обсуждение в чате:
                                 именно на этом None построен авто-показ/
                                 авто-скрытие алерт-экрана в screens.py
                                 (тот же общий механизм build_active_screens(),
                                 что у disk2/media Now Playing - любая None-
                                 переменная в шаблоне гасит экран).
        """
        now = time.time()
        mon = []
        down_names = []
        seen_ids = set()

        for t in (targets or []):
            tid = t.get("id")
            host = (t.get("host") or "").strip()
            if not tid or not host:
                continue
            seen_ids.add(tid)
            label = t.get("label") or host
            port = t.get("port")

            state = self._states.setdefault(tid, _TargetState())

            if port:
                ok, latency_ms = _check_tcp(host, int(port), timeout_ms)
            else:
                ok, latency_ms = _check_icmp(host, timeout_ms)

            if ok:
                state.consecutive_success += 1
                state.consecutive_fail = 0
            else:
                state.consecutive_fail += 1
                state.consecutive_success = 0

            prev_status = state.status
            if state.status is None:
                # первая проверка этой цели - см. докстринг модуля, статус
                # выставляется сразу, гистерезис тут не применяется
                state.status = ok
            elif ok and state.consecutive_success >= max(1, recover_threshold):
                state.status = True
            elif not ok and state.consecutive_fail >= max(1, fail_threshold):
                state.status = False
            # иначе - "переходное" состояние ещё не набрало нужного числа
            # подряд идущих проверок, status остаётся как был

            if state.status != prev_status:
                state.last_change_ts = now
            if ok:
                state.latency_ms = latency_ms

            mon.append({
                "label": label,
                "host": host,
                "status": "online" if state.status else "offline",
                # latency_ms имеет смысл только когда цель реально online -
                # "последняя успешная задержка на мёртвой цели" вводила бы в
                # заблуждение (выглядело бы как живой пинг), поэтому None
                "latency_ms": state.latency_ms if state.status else None,
                "since": _format_since(state.last_change_ts, now) if state.last_change_ts else "",
            })
            if not state.status:
                down_names.append(label)

        # чистим состояние удалённых из /settings целей - тот же приём, что
        # и TopProcessMonitor._procs в metrics_windows.py, иначе словарь
        # бесконечно растёт за время долгой работы приложения
        for tid in list(self._states.keys()):
            if tid not in seen_ids:
                del self._states[tid]

        return {
            "mon": mon,
            "mon_down_count": len(down_names),
            "mon_down_names": ", ".join(down_names) if down_names else None,
        }
