"""
metrics_tautulli.py  (win-hud-arduino)

Интеграция с Tautulli - веб-приложением поверх Plex Media Server API,
отдающим уже готовую статистику по библиотекам/активным сеансам/недавно
добавленному. Адрес Tautulli и API-ключ - НАСТРОЙКИ в /settings
(settings.json), а не переменные окружения - тот же принцип, что и остальные
подключения проекта (serial_port/disk1_letter/net1_iface в pc_hud.py).

Логика форматирования (bandwidth/mode/recent_ago/recent_code) портирована
1-в-1 с УЖЕ РАБОТАЮЩЕГО на реальном железе кода опроса Tautulli из
сиблинг-проекта shkaf-hud - в частности:
  - mode - ОДНА буква "D"(irect play/stream)/"T"(ranscode), не текстовая
    метка - на 128px OLED текстовые "Direct Play"/"Transcode" просто не
    влезают рядом с остальным на строке;
  - bandwidth - формат "X.X Mbps"/"X Kbps" (с пробелом);
  - recent_ago - "5m"/"2h"/"3d" (латиница, короче кириллицы на экране);
  - recent_code - "sNNeNN" в нижнем регистре для эпизодов, год для фильмов.

Используется ТОЛЬКО стандартная библиотека (urllib/json) - без новых
зависимостей в requirements.txt.

Ничего не знает про Flask/serial/переменные шаблонов - чистый сбор данных,
вызывается из pc_hud.py (см. integrations_loop()) раз в
INTEGRATIONS_POLL_INTERVAL, НЕ в главном цикле - сеть может тормозить/быть
недоступна, а стек Tautulli-запросов не должен блокировать VU/BAR.

Tautulli HTTP API - один эндпоинт для всех команд:
    GET {base_url}/api/v2?apikey=<key>&cmd=<command>&<параметры>
    -> {"response": {"result": "success"|"error", "message": ..., "data": ...}}
См. https://github.com/Tautulli/Tautulli/wiki/Tautulli-API-Reference
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

REQUEST_TIMEOUT = 5.0  # секунд - то же значение, что и в проверенном коде shkaf-hud

# Плекс-сеансы/недавно добавленное - repeating-группы для OLED-экранов (см.
# variables.py/screens.py) - экран не резиновый, поэтому берём разумный
# потолок числа элементов, а не всё подряд.
ACTIVE_STREAMS_MAX = 6
RECENT_ADDED_COUNT = 5

# Недавно добавленное старше этого числа дней в repeating-группу "recent" не
# попадает - иначе при пустой библиотеке туда полез бы контент годовой
# давности, что не имеет отношения к "недавнему". Значение 1-в-1 с shkaf-hud
# (RECENT_MAX_AGE_DAYS). Не настройка в /settings (как и ACTIVE_STREAMS_MAX
# выше) - при необходимости меняется тут в коде.
RECENT_MAX_AGE_DAYS = 5

# Библиотеки (счётчики фильмов/сериалов/треков) меняются редко - обновляем
# раз в LIBRARY_REFRESH_SECONDS (значение 1-в-1 с shkaf-hud), а не на каждый
# read() (integrations_loop вызывает read() раз в INTEGRATIONS_POLL_INTERVAL,
# см. pc_hud.py) - незачем дёргать Tautulli API лишний раз ради практически
# неизменных между опросами данных.
LIBRARY_REFRESH_SECONDS = 300


def _format_bandwidth(kbps):
    """Tautulli отдаёт bandwidth сессии в Kbps - формат 1-в-1 с shkaf-hud."""
    kbps = kbps or 0
    if kbps <= 0:
        return "0 Kbps"
    mbps = kbps / 1000
    if mbps >= 1:
        return f"{mbps:.1f} Mbps"
    return f"{kbps:.0f} Kbps"


def _format_ago(added_at, now):
    """Секунды с момента добавления -> '5m'/'2h'/'3d' (латиница - короче
    кириллицы на узком OLED, формат 1-в-1 с shkaf-hud)."""
    secs = max(0, now - added_at)
    if secs < 3600:
        return f"{int(secs // 60)}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


class TautulliClient:
    """
    Обёртка над Tautulli HTTP API. base_url/api_key НЕ хранятся в __init__ -
    передаются в read() при каждом вызове, т.к. это живые настройки из
    /settings и могут поменяться в любой момент через веб-интерфейс (тот же
    паттерн, что cfg["serial_port"] в pc_hud.py).
    """

    def __init__(self):
        self._libraries_cache = {"plex_movies": 0, "plex_series": 0, "plex_songs": 0}
        self._last_library_fetch = 0.0

    def _get(self, base_url, api_key, cmd, **params):
        """Один вызов Tautulli API. Возвращает response["data"] при успехе,
        иначе None (сеть недоступна/неверный ключ/сервер выключен - ЛЮБАЯ
        причина сбоя тут не должна ронять фоновый поток, поэтому исключения
        гасятся тут же, а не пробрасываются вызывающему коду)."""
        params.update({"apikey": api_key, "cmd": cmd})
        url = base_url.rstrip("/") + "/api/v2?" + urllib.parse.urlencode(params)
        try:
            with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            return payload["response"]["data"]
        except Exception as e:
            print(f"[tautulli] запрос {cmd} не удался: {e}", flush=True)
            return None

    def read(self, base_url, api_key):
        """
        Возвращает dict:
            plex_movies, plex_series, plex_songs   - счётчики библиотек (int)
            plex_server_status                     - "online"/"offline"
            plex_transcode_count                    - int|None (None, если сервер недоступен)
            plex_users_count                        - int - ЧИСЛО РАЗНЫХ пользователей
                                                       В АКТИВНЫХ СЕАНСАХ ПРЯМО СЕЙЧАС
                                                       (1-в-1 с shkaf-hud - НЕ общее число
                                                       аккаунтов на сервере, get_users не
                                                       вызывается вообще)
            streams  - список dict для repeating-группы "stream" (см. variables.py):
                       {"user","title","mode","progress","bandwidth"}
            recent   - список dict для repeating-группы "recent":
                       {"title","code","ago"}

        Если base_url/api_key не заданы (Tautulli ещё не настроен в
        /settings) - возвращает "пустой" результат без единого сетевого
        запроса, чтобы не спамить логи ошибками на дефолтной пустой настройке.
        """
        empty = {
            "plex_movies": 0, "plex_series": 0, "plex_songs": 0,
            "plex_server_status": "offline", "plex_transcode_count": None,
            "plex_users_count": 0, "streams": [], "recent": [],
        }
        if not base_url or not api_key:
            return empty

        now = time.time()
        if now - self._last_library_fetch >= LIBRARY_REFRESH_SECONDS:
            self._refresh_libraries(base_url, api_key)
            self._last_library_fetch = now

        streams, server_online = self._get_activity(base_url, api_key)
        transcode_count = sum(1 for s in streams if s["mode"] == "T") if server_online else None
        # Разные пользователи В АКТИВНЫХ СЕАНСАХ ПРЯМО СЕЙЧАС - 1-в-1 с
        # shkaf-hud (не общее число аккаунтов на сервере).
        users_count = len({s["user"] for s in streams if s["user"]})

        return {
            "plex_movies": self._libraries_cache["plex_movies"],
            "plex_series": self._libraries_cache["plex_series"],
            "plex_songs": self._libraries_cache["plex_songs"],
            "plex_server_status": "online" if server_online else "offline",
            "plex_transcode_count": transcode_count,
            "plex_users_count": users_count,
            "streams": streams,
            "recent": self._get_recently_added(base_url, api_key, RECENT_ADDED_COUNT, RECENT_MAX_AGE_DAYS),
        }

    def _refresh_libraries(self, base_url, api_key):
        libs = self._get(base_url, api_key, "get_libraries")
        if libs is not None:
            movies = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "movie")
            series = sum(int(l.get("count", 0) or 0) for l in libs if l.get("section_type") == "show")
            # child_count у библиотеки-artist - суммарное число ТРЕКОВ (не
            # исполнителей) - если Tautulli его не отдал (старая версия API),
            # откатываемся на count (число исполнителей) как на менее точный,
            # но хоть какой-то показатель.
            songs = sum(
                int(l.get("child_count", 0) or l.get("count", 0) or 0)
                for l in libs if l.get("section_type") == "artist"
            )
            self._libraries_cache = {"plex_movies": movies, "plex_series": series, "plex_songs": songs}

    def _get_activity(self, base_url, api_key):
        """Возвращает (streams, ok). ok=False означает, что Tautulli сейчас
        недоступен (см. plex_server_status) - в отличие от ok=True с пустым
        streams, что означает 'сервер жив, просто никто не смотрит'."""
        data = self._get(base_url, api_key, "get_activity")
        if data is None:
            return [], False
        sessions = data.get("sessions", [])
        out = []
        for s in sessions[:ACTIVE_STREAMS_MAX]:
            transcode_decision = (s.get("transcode_decision") or "").lower()
            mode = "D" if transcode_decision in ("", "direct play", "copy") else "T"
            user = s.get("friendly_name") or s.get("user", "") or ""
            out.append({
                "title": s.get("full_title") or s.get("title", ""),
                "user": user,
                "progress": int(s.get("progress_percent", 0) or 0),
                "mode": mode,
                "bandwidth": _format_bandwidth(float(s.get("bandwidth") or 0)),
            })
        return out, True

    def _get_recently_added(self, base_url, api_key, count, max_age_days):
        # Запрашиваем с запасом (count*4, минимум 10) - часть элементов
        # отсеется фильтром по возрасту/типу медиа ниже, итоговых валидных
        # может оказаться меньше, чем "сырых" в ответе API.
        data = self._get(base_url, api_key, "get_recently_added", count=max(count * 4, 10))
        if data is None:
            return []
        items = data.get("recently_added", [])
        now = time.time()
        out = []
        for it in items:
            added_at = int(it.get("added_at", 0) or 0)
            if added_at <= 0:
                continue
            if (now - added_at) / 86400 > max_age_days:
                continue
            media_type = it.get("media_type")
            if media_type == "episode":
                season = int(it.get("parent_media_index", 0) or 0)
                episode = int(it.get("media_index", 0) or 0)
                code = f"s{season:02d}e{episode:02d}"
                title = it.get("grandparent_title") or it.get("title", "")
            elif media_type == "movie":
                code = str(it.get("year", "") or "")
                title = it.get("title", "")
            else:
                continue
            out.append({"ago": _format_ago(added_at, now), "code": code, "title": title})
            if len(out) >= count:
                break
        return out
