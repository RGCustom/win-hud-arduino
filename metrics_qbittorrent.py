"""
metrics_qbittorrent.py  (win-hud-arduino)

Сбор данных с qBittorrent для repeating-группы "qbt" (см. variables.py:
qbt_name/qbt_speed/qbt_eta/qbt_pos/qbt_count) и глобальных переменных
qbt_total_dl/qbt_total_ul/qbt_count_all/qbt_ratio/qbt_free_space_gb.

Авторизация - через API-ключ (qBittorrent >= 5.2.0 / WebAPI >= 2.14.1):
STATELESS, без логина/пароля и без кук - просто заголовок
"Authorization: Bearer <ключ>" на каждый запрос (ключ генерируется в самом
qBittorrent: Preferences -> WebUI -> API Key -> Generate). Это сильно проще
классической cookie-сессии (нет login-запроса, нет протухания сессии,
нечего перевызывать при 403) - портировано 1-в-1 с УЖЕ РАБОТАЮЩЕГО на
реальном железе кода из сиблинг-проекта shkaf-hud (qbittorrent.py), только
на stdlib urllib вместо requests (без новых зависимостей в requirements.txt)
и с поддержкой ДВУХ серверов вместо одного (см. QbittorrentClient.read()
ниже) - у Konstantin два инстанса qBittorrent с разными IP и разными
API-ключами, а переменные шаблонов (qbt_name/qbt_total_dl/...) при этом
ОДНИ НА ВСЕХ - см. пояснение по объединению в докстринге read().

Адрес и API-ключ каждого сервера - НАСТРОЙКИ в /settings (settings.json),
плоские ключи qbt1_url/qbt1_api_key/qbt2_url/qbt2_api_key - тот же паттерн,
что net1_iface/net2_iface/disk1_letter/disk2_letter в pc_hud.py.

Ничего не знает про Flask/serial/переменные шаблонов - чистый сбор данных,
вызывается из pc_hud.py (см. integrations_loop()) раз в
INTEGRATIONS_POLL_INTERVAL, НЕ в главном цикле - сеть может тормозить/быть
недоступна, а сетевые запросы не должны блокировать VU/BAR (см. подробное
обоснование у INTEGRATIONS_POLL_INTERVAL в pc_hud.py).
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import variables

REQUEST_TIMEOUT = 5.0  # секунд - то же значение, что и в проверенном коде shkaf-hud

# Что считать "бесконечность/неизвестно" в eta - служебное значение самого
# qBittorrent (100 дней в секундах), 1-в-1 с shkaf-hud.
_ETA_UNKNOWN = 8640000


def _qbt_get(base_url, api_key, path, **params):
    """Один GET-запрос к одному серверу qBittorrent. Возвращает распарсенный
    JSON либо None (сеть недоступна/неверный ключ/сервер выключен - ЛЮБАЯ
    причина сбоя тут не должна ронять фоновый поток, поэтому исключения
    гасятся тут же, а не пробрасываются вызывающему коду)."""
    url = base_url.rstrip("/") + "/api/v2/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[qbt] запрос {path} не удался: {e}", flush=True)
        return None


def _human_rate(bps):
    """'1234' (B/s) -> '1.2 KB/s' и т.п. - общий форматтер скорости, 1-в-1
    с shkaf-hud: используется и для одного торрента, и для суммарной по всем."""
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024:
            return f"{bps:.0f} {unit}" if unit == "B/s" else f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} TB/s"


def _format_qbt_speed(dlspeed, upspeed):
    """Скорость с указанием направления - качаем (↓) или раздаём (↑)."""
    if dlspeed > 0:
        return f"\u2193 {_human_rate(dlspeed)}"
    if upspeed > 0:
        return f"\u2191 {_human_rate(upspeed)}"
    return "0 B/s"


def _format_qbt_eta(eta_seconds, dlspeed):
    """qBittorrent отдаёт eta=8640000 (100 дней) как 'бесконечность/неизвестно'."""
    if dlspeed <= 0:
        return "раздача"
    if eta_seconds is None or eta_seconds < 0 or eta_seconds >= _ETA_UNKNOWN:
        return "?"
    h, rem = divmod(int(eta_seconds), 3600)
    m, _sec = divmod(rem, 60)
    return f"{h}ч {m}м" if h > 0 else f"{m}м"


def _format_free_space(bytes_val):
    """Свободное место на диске загрузок (server_state.free_space_on_disk)."""
    if bytes_val is None or bytes_val < 0:
        return "?"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1000:
        return f"{gb / 1024:.2f} TB"
    return f"{gb:.1f} GB"


class QbittorrentClient:
    """
    Обёртка над qBittorrent WebUI API - для ОДНОГО ИЛИ НЕСКОЛЬКИХ серверов
    сразу. servers передаётся в read() при каждом вызове (живые настройки из
    /settings, тот же паттерн, что и TautulliClient.read()) как список
    dict {"url": str, "api_key": str} - серверы с пустым url/api_key
    пропускаются молча (интеграция для конкретного слота выключена).

    Полностью stateless - Bearer-токен не требует ни логина, ни хранения
    cookie/сессии между вызовами, поэтому в отличие от TautulliClient тут
    нет вообще никакого состояния между read() - можно было бы обойтись
    свободными функциями, но класс сохранён ради единообразия с остальными
    источниками интеграций в проекте.
    """

    def read(self, servers):
        """
        servers: [{"url": str, "api_key": str}, ...] - см. докстринг класса.

        Возвращает dict:
            qbt_total_dl, qbt_total_ul  - СУММАРНАЯ ТЕКУЩАЯ скорость по ВСЕМ
                                           серверам (dl_info_speed/up_info_speed
                                           из sync/maindata, а НЕ "скачано за
                                           всё время" - 1-в-1 с shkaf-hud)
            qbt_ratio                   - float, суммарный аплоад/суммарный
                                           даунлоад ПО ВСЕМ серверам разом
            qbt_free_space_gb           - строка "123.4 GB"/"1.20 TB" - СУММА
                                           свободного места по серверам (см.
                                           докстринг модуля - у Konstantin два
                                           сервера, тоталы объединяются в одни
                                           и те же переменные)
            qbt_count_all                - int - ВСЕГО торрентов на ВСЕХ серверах
            torrents - список dict для repeating-группы "qbt" (см. variables.py):
                       {"name", "speed", "eta"} - активные торренты СО ВСЕХ
                       серверов вперемешку, отсортированные по убыванию
                       скорости скачивания, до variables.REPEATING_GROUP_MAX["qbt"] штук
        """
        empty = {
            "qbt_total_dl": "0 B/s", "qbt_total_ul": "0 B/s",
            "qbt_ratio": 0.0, "qbt_free_space_gb": "?",
            "qbt_count_all": 0, "torrents": [],
        }
        active_servers = [s for s in (servers or []) if s.get("url") and s.get("api_key")]
        if not active_servers:
            return empty

        total_dlspeed = total_upspeed = 0
        total_free_space = None  # None, пока ни с одного сервера не пришло валидное значение
        total_count_all = 0
        total_downloaded = total_uploaded = 0
        raw_active_torrents = []  # сырые dict из torrents/info?filter=active со ВСЕХ серверов

        for server in active_servers:
            base_url, api_key = server["url"], server["api_key"]

            maindata = _qbt_get(base_url, api_key, "sync/maindata")
            if maindata is not None:
                server_state = maindata.get("server_state", {})
                total_dlspeed += server_state.get("dl_info_speed", 0) or 0
                total_upspeed += server_state.get("up_info_speed", 0) or 0
                free_space = server_state.get("free_space_on_disk")
                if free_space is not None and free_space >= 0:
                    total_free_space = (total_free_space or 0) + free_space

            all_torrents = _qbt_get(base_url, api_key, "torrents/info")
            if all_torrents is not None:
                total_count_all += len(all_torrents)
                total_downloaded += sum(t.get("downloaded", 0) or 0 for t in all_torrents)
                total_uploaded += sum(t.get("uploaded", 0) or 0 for t in all_torrents)

            active = _qbt_get(base_url, api_key, "torrents/info", filter="active", sort="dlspeed", reverse="true")
            if active is not None:
                raw_active_torrents.extend(active)

        ratio = round(total_uploaded / total_downloaded, 2) if total_downloaded > 0 else 0.0

        limit = variables.REPEATING_GROUP_MAX["qbt"]
        raw_active_torrents.sort(key=lambda t: t.get("dlspeed", 0) or 0, reverse=True)
        torrents = []
        for t in raw_active_torrents[:limit]:
            dlspeed = t.get("dlspeed", 0) or 0
            torrents.append({
                "name": t.get("name", "?"),
                "speed": _format_qbt_speed(dlspeed, t.get("upspeed", 0) or 0),
                "eta": _format_qbt_eta(t.get("eta"), dlspeed),
            })

        return {
            "qbt_total_dl": _human_rate(total_dlspeed),
            "qbt_total_ul": _human_rate(total_upspeed),
            "qbt_ratio": ratio,
            "qbt_free_space_gb": _format_free_space(total_free_space),
            "qbt_count_all": total_count_all,
            "torrents": torrents,
        }
