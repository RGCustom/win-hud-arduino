"""
settings_webui.py  (win-hud-arduino)

Страница /settings - портировано из shkaf-hud, но конструктор теперь на
ОДНУ ленту (вместо 4 независимых баров): режим (classic/center), метрика(и),
цвета, solid, peak hold, а также число диодов (leds_count - настройка, не
константа, см. ledbar.py). Плюс НОВЫЙ блок - настройки энкодера громкости:
шаг на клик, действие на клик кнопки, тайминг и цвета OSD-попапа. И ЕЩЁ
НОВЫЕ блоки - подключение к Tautulli (Plex) и к двум серверам qBittorrent
(см. metrics_tautulli.py/metrics_qbittorrent.py) - просто адрес/API-ключ(и),
опрашиваются отдельным фоновым потоком (integrations_loop в pc_hud.py), не
главным циклом. И ЕЩЁ ОДИН НОВЫЙ блок - мониторинг произвольных ресурсов
(ping/TCP-порт, см. metrics_ping.py) - список целей неизвестной заранее
длины (см. обсуждение в чате: "не знаю сколько ресурсов буду мониторить"),
поэтому это динамический список строк (добавить/удалить), а не фиксированные
поля - опрашивается ещё одним отдельным фоновым потоком (monitor_loop в
pc_hud.py) со своим, гораздо более редким интервалом (минуты, не секунды).

ПЕРЕЕХАЛО СЮДА С /  (см. обсуждение в чате): выбор COM-порта платы, дисков
(disk1_letter/disk2_letter) и сетевых интерфейсов (net1_iface/net2_iface) -
это настройки "один раз выбрал и забыл", их место рядом с остальным
конфигом, а не на главном экране (/) с живыми показаниями ленты/OLED.
Карточка "Подключение" ниже читает те же /api/serial_port, /api/disks,
/api/net-ifaces эндпоинты в pc_hud.py, что и раньше использовала страница /
- сам бэкенд не менялся.

Как и в shkaf-hud, вся серверная логика/состояние - в pc_hud.py (эндпойнты
/api/state, /api/mode, /api/assignment(_top), /api/colors(_top), /api/solid(_top),
/api/peak, /api/leds_count, /api/encoder, /api/tautulli, /api/qbittorrent,
/api/monitor_targets, /api/ping_settings, /api/serial_port, /api/disks,
/api/net-ifaces - этот файл только читает/пишет через них). Этот файл -
чистая разметка + JS.
"""

from flask import Response

SETTINGS_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>win-hud-arduino - настройки</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a; --panel: #1f2123; --border: #2c2e31;
    --text: #e6e6e6; --muted: #8a8d91; --accent: #ff8c2f; --danger: #e0483e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:24px 16px 60px; }
  .wrap { max-width:720px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; flex-wrap:wrap; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .bar-card, .global-card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
              padding:18px; margin-bottom:16px; }
  .bar-card h2 { font-size:13px; margin:0 0 16px; font-weight:600; display:flex; align-items:center; gap:8px; }
  .global-card h2 { font-size:11px; color:var(--muted); margin:0 0 4px; font-weight:600;
                     text-transform:uppercase; letter-spacing:.03em; }
  .global-card .hint { font-size:11px; color:var(--muted); margin-bottom:14px; line-height:1.5; }

  .row { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }
  .row label { font-size:12px; color:var(--muted); min-width:130px; }
  select, input[type=number], input[type=text] { background:#101112; color:var(--text); border:1px solid var(--border);
           border-radius:6px; padding:6px 8px; font-size:13px; flex:1; min-width:120px; }
  input[type=color] { width:26px; height:26px; border:none; background:none; border-radius:6px; cursor:pointer; padding:0; }
  input[type=checkbox] { width:16px; height:16px; }

  .half { border-left:2px solid var(--border); padding-left:12px; margin-top:4px; margin-bottom:10px; }
  .half-title { font-size:11px; color:var(--accent); text-transform:uppercase; letter-spacing:.03em; margin-bottom:8px; }

  .colors { display:flex; gap:6px; }
  .checkbox-row { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--muted); margin-bottom:10px; }

  .peak-row { border-top:1px solid var(--border); margin-top:12px; padding-top:12px; }

  .slider-row { display:flex; align-items:center; gap:10px; margin-top:10px; font-size:13px; }
  .slider-row label { color:var(--muted); min-width:170px; }
  .slider-row input[type=range] { flex:1; }
  .slider-row .val { min-width:52px; text-align:right; color:var(--text); font-variant-numeric:tabular-nums; }

  .note { font-size:11px; color:var(--muted); margin-top:6px; line-height:1.5; }

  /* ---- Мониторинг ресурсов: динамический список целей (см. metrics_ping.py) ---- */
  .mon-row { display:flex; align-items:center; gap:8px; margin-bottom:8px; }
  .mon-row input[type=text] { flex:2; min-width:0; }
  .mon-row input[type=text].mon-host { flex:3; }
  .mon-row input[type=number] { flex:none; width:110px; min-width:0; }
  .mon-del-btn { background:none; border:1px solid var(--border); color:var(--muted); border-radius:6px;
                 padding:6px 10px; cursor:pointer; font-size:13px; flex:none; }
  .mon-del-btn:hover { color:var(--danger); border-color:var(--danger); }
  #mon-target-add { background:var(--accent); color:#151515; border:none; border-radius:8px;
                    padding:8px 16px; font-weight:600; cursor:pointer; font-size:13px; margin-top:4px; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/">Sensors</a><a href="/settings" class="active">Settings</a><a href="/screens">OLED screens</a><a href="/flash">Flash</a></div>

  <!-- ---- Подключение (ПЕРЕЕХАЛО с Sensors, см. докстринг модуля выше) ---- -->
  <div class="global-card">
    <h2>Подключение</h2>
    <div class="hint">COM-порт платы - список читается прямо с системы при загрузке страницы
      (см. Диспетчер устройств -> Порты (COM и LPT), если плата не появилась в списке).</div>
    <div class="row">
      <label>COM-порт платы</label>
      <select id="serial-port"></select>
    </div>
  </div>

  <div class="global-card">
    <h2>Диски (для экранов и метрики ленты)</h2>
    <div class="row"><label>Диск 1</label><select id="disk1-letter"><option value="">(не выбран)</option></select></div>
    <div class="row"><label>Диск 2</label><select id="disk2-letter"><option value="">(не выбран)</option></select></div>
  </div>

  <div class="global-card">
    <h2>Сетевые интерфейсы (для экранов и метрики ленты "net")</h2>
    <div class="row"><label>Network 1</label><select id="net1-iface"></select></div>
    <div class="row"><label>Network 2</label><select id="net2-iface"><option value="">(не выбран)</option></select></div>
  </div>

  <!-- ---- Лента: число диодов (настройка, не константа - см. ledbar.py) ---- -->
  <div class="global-card">
    <h2>Лента</h2>
    <div class="hint">Число физических диодов на ленте - меняется тут, а не в коде.
      После изменения нужна повторная калибровка LED_MAP в прошивке (см. README, команда CAL).</div>
    <div class="row">
      <label>Диодов на ленте</label>
      <input type="number" id="leds-count" min="1" max="300" value="30">
    </div>

    <div class="checkbox-row">
      <input type="checkbox" id="leds-reverse">
      <label for="leds-reverse">Реверс ленты (если подключена/повёрнута задом наперёд)</label>
    </div>
    <div class="note">Переворачивает порядок диодов на лету, без пересборки прошивки -
      альтернатива калибровке LED_MAP, если выяснилось, что вся лента целиком светится
      "не в ту сторону" (начало и конец градиента поменялись местами).</div>

    <div class="slider-row">
      <label>Частота опроса (VU/лента)</label>
      <input type="range" id="tick-interval-ms" min="20" max="500" step="5" value="40">
      <span class="val" id="tick-interval-ms-val">40мс (~25Гц)</span>
    </div>
    <div class="note">Как часто опрашивается звук (VU-метр) и отправляется обновление на ленту по serial.
      Ниже - отзывчивее (лучше для VU-эквалайзера), но больше нагрузка на serial-порт, особенно
      при большом числе диодов выше. Если при малых значениях лента начнёт подтормаживать/дёргаться -
      увеличьте это число.</div>
  </div>

  <!-- ---- Энкодер громкости ---- -->
  <div class="global-card">
    <h2>Энкодер громкости</h2>
    <div class="hint">Вращение энкодера временно переключает ленту в OSD-показ громкости
      (расходится от центра), потом возврат к обычной метрике ниже.</div>

    <div class="row">
      <label>Шаг на "клик" вращения</label>
      <input type="number" id="volume-step" min="1" max="20" value="2">
      <span style="color:var(--muted);font-size:12px">% громкости за один щелчок энкодера</span>
    </div>

    <div class="row">
      <label>Действие на клик кнопки</label>
      <select id="encoder-click-action">
        <option value="mute_toggle">Mute / Unmute</option>
        <option value="switch_device">Переключить устройство вывода (скоро)</option>
      </select>
    </div>

    <div class="slider-row">
      <label>OSD держится после вращения, сек</label>
      <input type="range" id="osd-hold-seconds" min="0.5" max="10" step="0.5" value="3">
      <span class="val" id="osd-hold-seconds-val">3.0с</span>
    </div>

    <div class="row" style="margin-top:14px">
      <label>Цвета громкости</label>
      <div class="colors" id="volume-colors"></div>
    </div>
    <div class="row">
      <label>Цвет при mute</label>
      <div class="colors"><input type="color" id="volume-mute-color" value="#FF0000"></div>
    </div>
    <div class="row">
      <label>Цвет "почти максимум"</label>
      <div class="colors"><input type="color" id="volume-warning-color" value="#FFA500"></div>
    </div>
    <div class="row">
      <label>Порог "почти максимум", %</label>
      <input type="number" id="volume-warning-threshold" min="50" max="100" value="95">
    </div>
    <div class="note">Цвет громкости/mute/warning переопределяет обычный градиент ленты только
      во время OSD-попапа - на основную метрику ниже не влияет.</div>
  </div>

  <!-- ---- OSD: раскладка клавиатуры / устройство вывода (НОВОЕ) ---- -->
  <div class="global-card">
    <h2>OSD: раскладка клавиатуры / устройство вывода</h2>
    <div class="hint">Смена раскладки клавиатуры (в том же окне, без alt-tab) и смена
      устройства вывода звука временно замещают OLED коротким попапом - тем же механизмом,
      что и громкость выше (единая очередь, см. осд.py). Раскладка имеет приоритет
      над громкостью и устройством - прерывает их немедленно, если они показывались
      в этот момент. У раскладки дополнительно перекрашивается вся лента сплошным цветом
      (см. цвета ниже) - у устройства вывода лента не трогается, только текст.</div>

    <div class="slider-row">
      <label>Раскладка держится, сек</label>
      <input type="range" id="layout-hold-seconds" min="0.3" max="5" step="0.1" value="1.2">
      <span class="val" id="layout-hold-seconds-val">1.2с</span>
    </div>
    <div class="slider-row">
      <label>Устройство держится, сек</label>
      <input type="range" id="device-hold-seconds" min="0.5" max="10" step="0.5" value="2.0">
      <span class="val" id="device-hold-seconds-val">2.0с</span>
    </div>
    <div class="slider-row">
      <label>Общий кулдаун между попапами, сек</label>
      <input type="range" id="osd-cooldown-seconds" min="0" max="5" step="0.1" value="0.5">
      <span class="val" id="osd-cooldown-seconds-val">0.5с</span>
    </div>
    <div class="note">Кулдаун - минимальный интервал между ЛЮБЫМИ двумя срабатываниями
      OSD (любого типа - громкость/раскладка/устройство), защита от дребезга источника.</div>

    <div class="row" style="margin-top:14px">
      <label>Цвета раскладки</label>
    </div>
    <div id="layout-colors-rows"></div>
    <div class="note">Цвет, в который перекрашивается вся лента на время показа раскладки -
      "по умолчанию" используется для языков без отдельной настройки ниже.</div>
  </div>

  <!-- ---- Приоритетная ротация экранов (НОВОЕ) ---- -->
  <div class="global-card">
    <h2>Приоритетная ротация экранов</h2>
    <div class="hint">"Личные" экраны (например Now Playing - см. tier в редакторе /screens)
      показывают чаще обычных и могут прервать текущий показ, если появились или сменился
      их контент (например трек). "Фоновые" (Plex/qBittorrent) показываются чаще обычных,
      но без права прерывания - просто получают более частые слоты. Число ниже - "каждый
      N-й слот" ротации достаётся этой дорожке, если на неё сейчас есть что показать.</div>
    <div class="row">
      <label>Личные - каждый N-й слот</label>
      <input type="number" id="priority-boost-personal" min="1" max="20" value="2">
    </div>
    <div class="row">
      <label>Фоновые - каждый N-й слот</label>
      <input type="number" id="priority-boost-ambient" min="1" max="20" value="4">
    </div>
  </div>

  <!-- ---- Топ-процесс по CPU (НОВОЕ) ---- -->
  <div class="global-card">
    <h2>Топ-процесс (CPU)</h2>
    <div class="hint">Порог загрузки CPU, ниже которого экран "top_process_name" не показывается
      (см. {top_process_name}/{top_process_cpu_pct}/{top_process_ram_pct} на /screens) - без порога
      в топе постоянно мелькали бы лёгкие фоновые процессы. psutil считает загрузку НЕ нормализованной
      по числу потоков (сумма по ядрам) - на многопоточном CPU один полностью загруженный поток даёт
      лишь несколько процентов, поэтому нужное значение сильно зависит от конкретного железа.</div>
    <div class="slider-row">
      <label>Порог показа, %</label>
      <input type="range" id="top-process-min-cpu" min="0" max="100" step="1" value="25">
      <span class="val" id="top-process-min-cpu-val">25%</span>
    </div>
  </div>

  <!-- ---- Peak hold - общие тайминги (как в shkaf-hud) ---- -->
  <div class="global-card">
    <h2>Peak hold — общие тайминги</h2>
    <div class="hint">Применяется, если Peak hold включён в карточке ленты ниже.</div>
    <div class="slider-row">
      <label>Держится (hold), сек</label>
      <input type="range" id="peak-hold-seconds" min="0" max="10" step="0.1" value="2.0">
      <span class="val" id="peak-hold-seconds-val">2.0с</span>
    </div>
    <div class="slider-row">
      <label>Затухает (fade), сек</label>
      <input type="range" id="peak-fade-seconds" min="0" max="10" step="0.1" value="1.5">
      <span class="val" id="peak-fade-seconds-val">1.5с</span>
    </div>
  </div>

  <!-- ---- Сама лента: режим/метрика/цвета/solid/peak (как один bar0 из shkaf-hud) ---- -->
  <div id="bar-container"></div>

  <!-- ---- Tautulli (Plex) ---- -->
  <div class="global-card">
    <h2>Tautulli (Plex)</h2>
    <div class="hint">Адрес Tautulli и API-ключ (в самом Tautulli: Settings -> Web Interface -> API,
      там же кнопка показать/сгенерировать ключ). Сеансы/библиотека/недавно добавленное
      опрашиваются ОТДЕЛЬНЫМ фоновым потоком раз в несколько секунд, не главным циклом ленты -
      пусто здесь = интеграция выключена, экраны/переменные Plex просто не резолвятся.</div>
    <div class="row">
      <label>Адрес (URL)</label>
      <input type="text" id="tautulli-url" placeholder="http://192.168.1.10:8181">
    </div>
    <div class="row">
      <label>API-ключ</label>
      <input type="password" id="tautulli-api-key" placeholder="API key" autocomplete="off">
    </div>
    <div class="row">
      <label>Мой Plex-логин</label>
      <input type="text" id="my-plex-user" placeholder="(необязательно) ваш friendly name в Plex">
    </div>
    <div class="note">Если заполнено - сеанс с этим пользователем на экранах, использующих
      stream_* переменные (см. /screens), считается "личным" (tier=personal, показывается чаще
      и может прервать текущий показ), а не "фоновым" - см. обсуждение "свой/чужой Plex-сеанс".
      Работает, только если сам экран настроен как tier="ambient" (рекомендуемая настройка для
      таких экранов). Пусто - ВСЕ Plex-сеансы считаются фоновыми.</div>
  </div>

  <!-- ---- qBittorrent (два сервера, объединяются в одни переменные) ---- -->
  <div class="global-card">
    <h2>qBittorrent</h2>
    <div class="hint">API-ключ генерируется в самом qBittorrent (Preferences -> WebUI -> API Key ->
      Generate, нужен qBittorrent >= 5.2.0) - логин/пароль не требуются. Данные с обоих серверов
      объединяются в одни и те же переменные шаблонов (qbt_total_dl/qbt_name и т.п.) - пустой
      слот означает, что этот сервер выключен.</div>

    <div class="half">
      <div class="half-title">Сервер 1</div>
      <div class="row"><label>Адрес (URL)</label><input type="text" id="qbt1-url" placeholder="http://192.168.1.11:8080"></div>
      <div class="row"><label>API-ключ</label><input type="password" id="qbt1-api-key" placeholder="API key" autocomplete="off"></div>
    </div>
    <div class="half">
      <div class="half-title">Сервер 2</div>
      <div class="row"><label>Адрес (URL)</label><input type="text" id="qbt2-url" placeholder="http://192.168.1.12:8080"></div>
      <div class="row"><label>API-ключ</label><input type="password" id="qbt2-api-key" placeholder="API key" autocomplete="off"></div>
    </div>
  </div>

  <!-- ---- Мониторинг ресурсов (ping/TCP, НОВОЕ) ---- -->
  <div class="global-card">
    <h2>Мониторинг ресурсов</h2>
    <div class="hint">Произвольные ресурсы (роутер, NAS, VPN, сайт - что угодно с IP или
      доменным именем) - раз в заданный интервал проверяются на доступность: обычный ICMP
      ping, если порт не указан, либо TCP-подключение к порту, если он задан (полезно для
      ресурсов, которые сами блокируют ICMP). Доступны на /screens как {mon_label}/{mon_status}/
      {mon_latency_ms}/{mon_since} - экран с ними сам размножается по числу целей ниже, плюс
      готовые экраны "Мониторинг" (по очереди) и "Алерт" (мгновенно всплывает при падении)
      уже есть среди дефолтных на /screens.</div>

    <div id="mon-targets-rows"></div>
    <button id="mon-target-add">+ Добавить ресурс</button>

    <div class="slider-row" style="margin-top:18px">
      <label>Интервал проверки, сек</label>
      <input type="range" id="ping-interval" min="5" max="600" step="5" value="120">
      <span class="val" id="ping-interval-val">120с</span>
    </div>
    <div class="row">
      <label>Таймаут проверки, мс</label>
      <input type="number" id="ping-timeout" min="50" max="10000" step="50" value="800">
    </div>
    <div class="row">
      <label>Провалов подряд -> offline</label>
      <input type="number" id="ping-fail-threshold" min="1" max="10" value="2">
    </div>
    <div class="row">
      <label>Успехов подряд -> online</label>
      <input type="number" id="ping-recover-threshold" min="1" max="10" value="1">
    </div>
    <div class="note">Гистерезис защищает от ложных срабатываний на один потерянный пакет -
      статус ресурса меняется, только когда указанное число проверок подряд дало одинаковый
      результат (при "1" смена происходит по первой же проверке).</div>
  </div>

  <footer>win-hud-arduino</footer>
</div>

<script>
const BAR_ID = "bar0";  // одна лента - один "бар" во внутреннем API (совместимость с shkaf-hud кодом)
let metricsMap = {};
let editingPeakHold = false, editingPeakFade = false, editingOsdHold = false;
let editingLedsCount = false, editingVolumeStep = false, editingWarningThreshold = false;
let editingTautulliUrl = false, editingTautulliApiKey = false, editingMyPlexUser = false;
let editingQbt1Url = false, editingQbt1ApiKey = false, editingQbt2Url = false, editingQbt2ApiKey = false;
let editingLayoutHold = false, editingDeviceHold = false, editingOsdCooldown = false;
let editingPriorityPersonal = false, editingPriorityAmbient = false;
let editingPingInterval = false, editingPingTimeout = false, editingPingFail = false, editingPingRecover = false;
let editingTopProcessMinCpu = false;
let layoutColorsBuilt = false;

// ---- Подключение / диски / сеть (ПЕРЕЕХАЛО с Sensors, см. докстринг модуля) ----
// Заполняется ОДИН РАЗ на загрузку страницы (см. render() ниже) - в отличие
// от живой ленты/OLED на Sensors, эти списки (порты/диски/интерфейсы)
// меняются редко, отдельный поллинг тут не нужен.
let selectsPopulated = false;

function fillSelect(sel, options, current, allowEmpty) {
  sel.innerHTML = allowEmpty ? '<option value="">(не выбран)</option>' : "";
  options.forEach(name => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    if (name === current) o.selected = true;
    sel.appendChild(o);
  });
}

function populateConnectionSelects(s) {
  fillSelect(document.getElementById("serial-port"), s.available_ports, s.cfg.serial_port, false);
  fillSelect(document.getElementById("disk1-letter"), s.available_disks, s.cfg.disk1_letter, true);
  fillSelect(document.getElementById("disk2-letter"), s.available_disks, s.cfg.disk2_letter, true);
  fillSelect(document.getElementById("net1-iface"), s.available_interfaces, s.cfg.net1_iface, false);
  fillSelect(document.getElementById("net2-iface"), s.available_interfaces, s.cfg.net2_iface, true);
  selectsPopulated = true;

  document.getElementById("serial-port").addEventListener("change", e => {
    fetch("/api/serial_port", { method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ value: e.target.value }) });
  });
  ["disk1-letter", "disk2-letter"].forEach(id => {
    document.getElementById(id).addEventListener("change", () => {
      fetch("/api/disks", { method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          disk1_letter: document.getElementById("disk1-letter").value,
          disk2_letter: document.getElementById("disk2-letter").value,
        }) });
    });
  });
  ["net1-iface", "net2-iface"].forEach(id => {
    document.getElementById(id).addEventListener("change", () => {
      fetch("/api/net-ifaces", { method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({
          net1_iface: document.getElementById("net1-iface").value,
          net2_iface: document.getElementById("net2-iface").value,
        }) });
    });
  });
}

function debounceSave(el, flagSetter, sendFn) {
  el.addEventListener("input", () => flagSetter(true));
  el.addEventListener("change", () => { sendFn(); flagSetter(false); });
}

const ledsCountEl = document.getElementById("leds-count");
debounceSave(ledsCountEl, v => editingLedsCount = v, () => {
  fetch("/api/leds_count", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(ledsCountEl.value) }) });
});

const ledsReverseEl = document.getElementById("leds-reverse");
ledsReverseEl.addEventListener("change", () => {
  fetch("/api/leds_reverse", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: ledsReverseEl.checked }) });
});

const tickIntervalEl = document.getElementById("tick-interval-ms");
const tickIntervalValEl = document.getElementById("tick-interval-ms-val");
let editingTickInterval = false;
function formatTickInterval(ms) {
  return ms + "мс (~" + Math.round(1000 / ms) + "Гц)";
}
// Подпись обновляем на КАЖДОЕ движение ползунка (input), а не только на
// отпускание (change) - иначе число мс/Гц не успевает за пальцем/мышью, в
// отличие от peak-hold/peak-fade слайдеров ниже (там это не так критично,
// а тут пользователь настраивает "на глаз" по отзывчивости).
tickIntervalEl.addEventListener("input", () => {
  editingTickInterval = true;
  tickIntervalValEl.textContent = formatTickInterval(parseInt(tickIntervalEl.value));
});
tickIntervalEl.addEventListener("change", () => {
  fetch("/api/tick_interval", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(tickIntervalEl.value) / 1000 }) });
  editingTickInterval = false;
});

const volumeStepEl = document.getElementById("volume-step");
const encoderClickEl = document.getElementById("encoder-click-action");
const osdHoldEl = document.getElementById("osd-hold-seconds");
const osdHoldValEl = document.getElementById("osd-hold-seconds-val");
const muteColorEl = document.getElementById("volume-mute-color");
const warningColorEl = document.getElementById("volume-warning-color");
const warningThresholdEl = document.getElementById("volume-warning-threshold");

function sendEncoderSettings(partial) {
  fetch("/api/encoder", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(partial) });
}

debounceSave(volumeStepEl, v => editingVolumeStep = v, () => sendEncoderSettings({ volume_step_pct: parseInt(volumeStepEl.value) }));
encoderClickEl.addEventListener("change", () => sendEncoderSettings({ click_action: encoderClickEl.value }));

osdHoldEl.addEventListener("input", () => {
  editingOsdHold = true;
  osdHoldValEl.textContent = parseFloat(osdHoldEl.value).toFixed(1) + "с";
});
osdHoldEl.addEventListener("change", () => {
  sendEncoderSettings({ osd_hold_seconds: parseFloat(osdHoldEl.value) });
  editingOsdHold = false;
});

muteColorEl.addEventListener("change", () => sendEncoderSettings({ mute_color: muteColorEl.value.slice(1).toUpperCase() }));
warningColorEl.addEventListener("change", () => sendEncoderSettings({ warning_color: warningColorEl.value.slice(1).toUpperCase() }));
debounceSave(warningThresholdEl, v => editingWarningThreshold = v, () => sendEncoderSettings({ warning_threshold_pct: parseInt(warningThresholdEl.value) }));

// ---- OSD: раскладка клавиатуры / устройство вывода ----
function sendOsd(partial) {
  fetch("/api/osd", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(partial) });
}

const layoutHoldEl = document.getElementById("layout-hold-seconds");
const layoutHoldValEl = document.getElementById("layout-hold-seconds-val");
layoutHoldEl.addEventListener("input", () => {
  editingLayoutHold = true;
  layoutHoldValEl.textContent = parseFloat(layoutHoldEl.value).toFixed(1) + "с";
});
layoutHoldEl.addEventListener("change", () => {
  sendOsd({ layout_hold_seconds: parseFloat(layoutHoldEl.value) });
  editingLayoutHold = false;
});

const deviceHoldEl = document.getElementById("device-hold-seconds");
const deviceHoldValEl = document.getElementById("device-hold-seconds-val");
deviceHoldEl.addEventListener("input", () => {
  editingDeviceHold = true;
  deviceHoldValEl.textContent = parseFloat(deviceHoldEl.value).toFixed(1) + "с";
});
deviceHoldEl.addEventListener("change", () => {
  sendOsd({ device_hold_seconds: parseFloat(deviceHoldEl.value) });
  editingDeviceHold = false;
});

const osdCooldownEl = document.getElementById("osd-cooldown-seconds");
const osdCooldownValEl = document.getElementById("osd-cooldown-seconds-val");
osdCooldownEl.addEventListener("input", () => {
  editingOsdCooldown = true;
  osdCooldownValEl.textContent = parseFloat(osdCooldownEl.value).toFixed(1) + "с";
});
osdCooldownEl.addEventListener("change", () => {
  sendOsd({ osd_cooldown_seconds: parseFloat(osdCooldownEl.value) });
  editingOsdCooldown = false;
});

// Известные коды раскладки (см. metrics_windows._PRIMARY_LANG_NAMES) +
// "_default" - fallback-цвет для языков без отдельной настройки. Строится
// ОДИН РАЗ (layoutColorsBuilt) - создание/пересоздание input[type=color]
// на каждый refresh() сбрасывало бы фокус/незакоммиченный выбор пользователя,
// тот же принцип, что и populateConnectionSelects()/selectsPopulated выше.
const KNOWN_LAYOUT_CODES = ["_default", "EN", "RU"];
function buildLayoutColorsRows(colors) {
  const wrap = document.getElementById("layout-colors-rows");
  wrap.innerHTML = "";
  KNOWN_LAYOUT_CODES.forEach(code => {
    const row = document.createElement("div");
    row.className = "row";
    const label = document.createElement("label");
    label.textContent = code === "_default" ? "По умолчанию" : code;
    row.appendChild(label);
    const colorsWrap = document.createElement("div");
    colorsWrap.className = "colors";
    const inp = document.createElement("input");
    inp.type = "color";
    inp.value = "#" + (colors[code] || "808080");
    inp.addEventListener("change", () => {
      const body = { layout_colors: {} };
      body.layout_colors[code] = inp.value.slice(1).toUpperCase();
      sendOsd(body);
    });
    colorsWrap.appendChild(inp);
    row.appendChild(colorsWrap);
    wrap.appendChild(row);
  });
  layoutColorsBuilt = true;
}

// ---- Приоритетная ротация экранов ----
function sendPriorityBoost(partial) {
  fetch("/api/priority_boost", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(partial) });
}
const priorityPersonalEl = document.getElementById("priority-boost-personal");
const priorityAmbientEl = document.getElementById("priority-boost-ambient");
debounceSave(priorityPersonalEl, v => editingPriorityPersonal = v,
  () => sendPriorityBoost({ priority_boost_personal: parseInt(priorityPersonalEl.value) }));
debounceSave(priorityAmbientEl, v => editingPriorityAmbient = v,
  () => sendPriorityBoost({ priority_boost_ambient: parseInt(priorityAmbientEl.value) }));

// ---- Tautulli (Plex) ----
function sendTautulli(partial) {
  fetch("/api/tautulli", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(partial) });
}
const tautulliUrlEl = document.getElementById("tautulli-url");
const tautulliApiKeyEl = document.getElementById("tautulli-api-key");
const myPlexUserEl = document.getElementById("my-plex-user");
debounceSave(tautulliUrlEl, v => editingTautulliUrl = v, () => sendTautulli({ url: tautulliUrlEl.value }));
debounceSave(tautulliApiKeyEl, v => editingTautulliApiKey = v, () => sendTautulli({ api_key: tautulliApiKeyEl.value }));
debounceSave(myPlexUserEl, v => editingMyPlexUser = v, () => sendTautulli({ my_plex_user: myPlexUserEl.value }));

// ---- qBittorrent (два сервера, один эндпоинт на оба - см. /api/qbittorrent в pc_hud.py) ----
function sendQbt(partial) {
  fetch("/api/qbittorrent", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(partial) });
}
const qbt1UrlEl = document.getElementById("qbt1-url");
const qbt1ApiKeyEl = document.getElementById("qbt1-api-key");
const qbt2UrlEl = document.getElementById("qbt2-url");
const qbt2ApiKeyEl = document.getElementById("qbt2-api-key");
debounceSave(qbt1UrlEl, v => editingQbt1Url = v, () => sendQbt({ qbt1_url: qbt1UrlEl.value }));
debounceSave(qbt1ApiKeyEl, v => editingQbt1ApiKey = v, () => sendQbt({ qbt1_api_key: qbt1ApiKeyEl.value }));
debounceSave(qbt2UrlEl, v => editingQbt2Url = v, () => sendQbt({ qbt2_url: qbt2UrlEl.value }));
debounceSave(qbt2ApiKeyEl, v => editingQbt2ApiKey = v, () => sendQbt({ qbt2_api_key: qbt2ApiKeyEl.value }));

// ---- Мониторинг ресурсов (ping/TCP, НОВОЕ - см. metrics_ping.py) ----
// monTargets - ЕДИНСТВЕННЫЙ источник правды в браузере на время сессии этой
// страницы (в отличие от полей выше, тут НЕТ отдельного editing-флага на
// каждую строку - settings_webui.py вообще не переопрашивает /api/state
// периодически, см. единственный fetch() в самом низу файла, поэтому нет
// риска, что сервер "перезатрёт" то, что человек как раз печатает). id
// генерируется КЛИЕНТСКИ при добавлении новой строки (genMonId()) - тот же
// id уходит на сервер и там же сохраняется (see _sanitize_mon_targets() в
// pc_hud.py - принимает готовый id, если он есть) - это позволяет НЕ
// синхронизировать локальный список с ответом сервера после каждого
// сохранения: ответ игнорируется, локальный monTargets всегда авторитетен,
// а id остаётся стабильным между сохранениями (важно - от него зависит
// состояние гистерезиса на бэкенде, см. metrics_ping.PingMonitor._states).
let monTargets = [];
let monTargetsLoaded = false;

function genMonId() {
  return "m" + Math.random().toString(36).slice(2, 10);
}

function saveMonTargets() {
  fetch("/api/monitor_targets", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ targets: monTargets }) });
  // Ответ намеренно игнорируется - см. комментарий у monTargets выше.
  // Сервер сам отбросит строки с пустым host (см. _sanitize_mon_targets()) -
  // это нормально, пока пользователь ещё не дописал адрес: локальная копия
  // при этом не меняется, недописанная строка не исчезает из-под курсора.
}

function renderMonTargets() {
  const wrap = document.getElementById("mon-targets-rows");
  wrap.innerHTML = "";
  monTargets.forEach((t, idx) => {
    const row = document.createElement("div");
    row.className = "mon-row";

    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.placeholder = "Название (Роутер, NAS...)";
    labelInput.value = t.label || "";
    labelInput.addEventListener("change", () => { t.label = labelInput.value; saveMonTargets(); });

    const hostInput = document.createElement("input");
    hostInput.type = "text";
    hostInput.className = "mon-host";
    hostInput.placeholder = "IP или домен";
    hostInput.value = t.host || "";
    hostInput.addEventListener("change", () => { t.host = hostInput.value; saveMonTargets(); });

    const portInput = document.createElement("input");
    portInput.type = "number";
    portInput.placeholder = "порт (необяз.)";
    portInput.min = 1; portInput.max = 65535;
    portInput.value = t.port || "";
    portInput.addEventListener("change", () => {
      t.port = portInput.value ? parseInt(portInput.value) : null;
      saveMonTargets();
    });

    const delBtn = document.createElement("button");
    delBtn.className = "mon-del-btn";
    delBtn.textContent = "\u2715";
    delBtn.title = "Удалить";
    delBtn.addEventListener("click", () => {
      monTargets.splice(idx, 1);
      renderMonTargets();
      saveMonTargets();
    });

    row.appendChild(labelInput);
    row.appendChild(hostInput);
    row.appendChild(portInput);
    row.appendChild(delBtn);
    wrap.appendChild(row);
  });
}

document.getElementById("mon-target-add").addEventListener("click", () => {
  // Новая строка ещё БЕЗ host - saveMonTargets() тут не вызывается (нечего
  // сохранять, сервер бы всё равно её отбросил, см. _sanitize_mon_targets()) -
  // сохранение случится само на первое реальное изменение любого поля этой
  // строки (см. addEventListener("change", ...) выше).
  monTargets.push({ id: genMonId(), label: "", host: "", port: null });
  renderMonTargets();
});

function sendPingSettings(partial) {
  fetch("/api/ping_settings", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify(partial) });
}
const pingIntervalEl = document.getElementById("ping-interval");
const pingIntervalValEl = document.getElementById("ping-interval-val");
const pingTimeoutEl = document.getElementById("ping-timeout");
const pingFailEl = document.getElementById("ping-fail-threshold");
const pingRecoverEl = document.getElementById("ping-recover-threshold");

pingIntervalEl.addEventListener("input", () => {
  editingPingInterval = true;
  pingIntervalValEl.textContent = pingIntervalEl.value + "с";
});
pingIntervalEl.addEventListener("change", () => {
  sendPingSettings({ ping_interval_seconds: parseFloat(pingIntervalEl.value) });
  editingPingInterval = false;
});
debounceSave(pingTimeoutEl, v => editingPingTimeout = v, () => sendPingSettings({ ping_timeout_ms: parseInt(pingTimeoutEl.value) }));
debounceSave(pingFailEl, v => editingPingFail = v, () => sendPingSettings({ ping_fail_threshold: parseInt(pingFailEl.value) }));
debounceSave(pingRecoverEl, v => editingPingRecover = v, () => sendPingSettings({ ping_recover_threshold: parseInt(pingRecoverEl.value) }));

// ---- Топ-процесс по CPU (порог показа, см. metrics_windows.TopProcessMonitor) ----
const topProcessMinCpuEl = document.getElementById("top-process-min-cpu");
const topProcessMinCpuValEl = document.getElementById("top-process-min-cpu-val");
topProcessMinCpuEl.addEventListener("input", () => {
  editingTopProcessMinCpu = true;
  topProcessMinCpuValEl.textContent = topProcessMinCpuEl.value + "%";
});
topProcessMinCpuEl.addEventListener("change", () => {
  fetch("/api/top_process_settings", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ top_process_min_cpu_pct: parseFloat(topProcessMinCpuEl.value) }) });
  editingTopProcessMinCpu = false;
});

function renderVolumeColors(colors) {
  const wrap = document.getElementById("volume-colors");
  wrap.innerHTML = "";
  ["c1", "c2", "c3"].forEach(stop => {
    const inp = document.createElement("input");
    inp.type = "color";
    inp.value = "#" + colors[stop];
    inp.addEventListener("change", () => {
      const body = {}; body[stop] = inp.value.slice(1).toUpperCase();
      sendEncoderSettings({ volume_colors: body });
    });
    wrap.appendChild(inp);
  });
}

const peakHoldEl = document.getElementById("peak-hold-seconds");
const peakHoldValEl = document.getElementById("peak-hold-seconds-val");
peakHoldEl.addEventListener("input", () => {
  editingPeakHold = true;
  peakHoldValEl.textContent = parseFloat(peakHoldEl.value).toFixed(1) + "с";
});
peakHoldEl.addEventListener("change", () => {
  fetch("/api/peak_timing", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ hold_seconds: parseFloat(peakHoldEl.value) }) })
    .then(() => editingPeakHold = false);
});

const peakFadeEl = document.getElementById("peak-fade-seconds");
const peakFadeValEl = document.getElementById("peak-fade-seconds-val");
peakFadeEl.addEventListener("input", () => {
  editingPeakFade = true;
  peakFadeValEl.textContent = parseFloat(peakFadeEl.value).toFixed(1) + "с";
});
peakFadeEl.addEventListener("change", () => {
  fetch("/api/peak_timing", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ fade_seconds: parseFloat(peakFadeEl.value) }) })
    .then(() => editingPeakFade = false);
});

function colorRow(prefix, colors, onChange) {
  const wrap = document.createElement("div");
  wrap.className = "row";
  const label = document.createElement("label");
  label.textContent = "Цвета";
  wrap.appendChild(label);
  const colorsWrap = document.createElement("div");
  colorsWrap.className = "colors";
  ["c1", "c2", "c3"].forEach(stop => {
    const inp = document.createElement("input");
    inp.type = "color";
    inp.value = "#" + colors[stop];
    inp.addEventListener("change", () => {
      const body = {}; body[BAR_ID] = {}; body[BAR_ID][stop] = inp.value.slice(1);
      fetch(onChange, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
    });
    colorsWrap.appendChild(inp);
  });
  wrap.appendChild(colorsWrap);
  return wrap;
}

function metricSelect(labelText, currentValue, endpoint) {
  const wrap = document.createElement("div");
  wrap.className = "row";
  const label = document.createElement("label");
  label.textContent = labelText;
  wrap.appendChild(label);
  const sel = document.createElement("select");
  Object.entries(metricsMap).forEach(([id, name]) => {
    const opt = document.createElement("option");
    opt.value = id; opt.textContent = name;
    if (id === currentValue) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => {
    const body = {}; body[BAR_ID] = sel.value;
    fetch(endpoint, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  });
  wrap.appendChild(sel);
  return wrap;
}

function solidCheckbox(labelText, checked, endpoint) {
  const wrap = document.createElement("div");
  wrap.className = "checkbox-row";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = checked;
  const id = "solid-" + endpoint.replace(/\\W/g, "");
  cb.id = id;
  cb.addEventListener("change", () => {
    const body = {}; body[BAR_ID] = cb.checked;
    fetch(endpoint, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  });
  const lab = document.createElement("label");
  lab.htmlFor = id;
  lab.textContent = labelText;
  wrap.appendChild(cb);
  wrap.appendChild(lab);
  return wrap;
}

function renderBar(cfg) {
  const card = document.createElement("div");
  card.className = "bar-card";

  const title = document.createElement("h2");
  title.textContent = "Метрика по умолчанию (когда энкодер не трогали)";
  card.appendChild(title);

  const modeRow = document.createElement("div");
  modeRow.className = "row";
  const modeLabel = document.createElement("label");
  modeLabel.textContent = "Режим";
  modeRow.appendChild(modeLabel);
  const modeSel = document.createElement("select");
  [
    ["classic", "Classic (слева направо)"],
    ["center", "Center (от центра в обе стороны)"],
    ["edges", "Edges (от краёв к центру)"],
    ["flat", "Flat (вся лента одним цветом)"],
  ].forEach(([val, text]) => {
    const opt = document.createElement("option");
    opt.value = val; opt.textContent = text;
    if (val === cfg.mode[BAR_ID]) opt.selected = true;
    modeSel.appendChild(opt);
  });
  modeRow.appendChild(modeSel);
  card.appendChild(modeRow);

  const topBlock = document.createElement("div");
  topBlock.className = "half";
  const topTitle = document.createElement("div");
  topTitle.className = "half-title";
  topTitle.textContent = "Правая половина";
  topBlock.appendChild(topTitle);
  topBlock.appendChild(metricSelect("Метрика", cfg.assignment_top[BAR_ID], "/api/assignment_top"));
  topBlock.appendChild(colorRow("top", cfg.colors_top[BAR_ID], "/api/colors_top"));
  topBlock.appendChild(solidCheckbox("Цвет на 100%", cfg.solid_top[BAR_ID], "/api/solid_top"));
  card.appendChild(topBlock);

  const bottomBlock = document.createElement("div");
  const bottomTitle = document.createElement("div");
  bottomTitle.className = "half-title";
  bottomBlock.appendChild(bottomTitle);
  bottomBlock.appendChild(metricSelect("Метрика", cfg.assignment[BAR_ID], "/api/assignment"));
  bottomBlock.appendChild(colorRow("", cfg.colors[BAR_ID], "/api/colors"));
  bottomBlock.appendChild(solidCheckbox("Цвет на 100%", cfg.solid[BAR_ID], "/api/solid"));
  card.appendChild(bottomBlock);

  function applyModeVisibility(mode) {
    // center и edges - оба двухполосные режимы (см. ledbar.py) - у обоих
    // показываем блок "правая половина" и подписываем нижний блок как
    // "левая половина"; classic/flat - однометричные, там второй половины
    // нет вообще, а нижний блок - единственная метрика без подписи.
    const twoHalves = mode === "center" || mode === "edges";
    bottomTitle.textContent = twoHalves ? "Левая половина" : "";
    topBlock.style.display = twoHalves ? "block" : "none";
    // Peak hold (точка недавнего максимума) не имеет смысла в flat-режиме -
    // там нет позиции вдоль ленты, куда её ставить (см. докстринг
    // ledbar.compute_bar_pixels_flat() и ветку "flat" в главном цикле
    // pc_hud.py, где peak_enabled для этого режима не используется).
    peakRow.style.display = mode === "flat" ? "none" : "block";
  }

  modeSel.addEventListener("change", () => {
    applyModeVisibility(modeSel.value);
    const body = {}; body[BAR_ID] = modeSel.value;
    fetch("/api/mode", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  });

  const peakRow = document.createElement("div");
  peakRow.className = "peak-row";

  const peakToggle = document.createElement("div");
  peakToggle.className = "checkbox-row";
  const peakCb = document.createElement("input");
  peakCb.type = "checkbox";
  peakCb.checked = cfg.peak[BAR_ID].enabled;
  peakCb.id = "peak-toggle";
  const peakLab = document.createElement("label");
  peakLab.htmlFor = "peak-toggle";
  peakLab.textContent = "Peak hold (точка недавнего максимума)";
  peakToggle.appendChild(peakCb);
  peakToggle.appendChild(peakLab);
  peakRow.appendChild(peakToggle);

  const styleRow = document.createElement("div");
  styleRow.className = "row";
  const styleLabel = document.createElement("label");
  styleLabel.textContent = "Стиль";
  styleRow.appendChild(styleLabel);
  const styleSel = document.createElement("select");
  [["hold", "Держится и гаснет"], ["fade", "Плавно затухает"]].forEach(([val, text]) => {
    const opt = document.createElement("option");
    opt.value = val; opt.textContent = text;
    if (val === cfg.peak[BAR_ID].style) opt.selected = true;
    styleSel.appendChild(opt);
  });
  styleRow.appendChild(styleSel);
  peakRow.appendChild(styleRow);
  card.appendChild(peakRow);
  applyModeVisibility(cfg.mode[BAR_ID]);

  function sendPeak(partial) {
    const body = {}; body[BAR_ID] = partial;
    fetch("/api/peak", { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify(body) });
  }
  peakCb.addEventListener("change", () => sendPeak({ enabled: peakCb.checked }));
  styleSel.addEventListener("change", () => sendPeak({ style: styleSel.value }));

  return card;
}

function render(state) {
  metricsMap = state.metrics;

  if (!selectsPopulated) populateConnectionSelects(state);

  if (!editingLedsCount) ledsCountEl.value = state.cfg.leds_count;
  ledsReverseEl.checked = !!state.cfg.leds_reverse;

  if (!editingTickInterval) {
    // state.cfg.tick_interval хранится в СЕКУНДАХ на сервере (см.
    // DEFAULT_SETTINGS/TICK_INTERVAL в pc_hud.py) - на слайдере показываем
    // мс, конвертация туда-обратно только тут и в обработчике change выше.
    const ms = Math.round(state.cfg.tick_interval * 1000);
    tickIntervalEl.value = ms;
    tickIntervalValEl.textContent = formatTickInterval(ms);
  }

  if (!editingVolumeStep) volumeStepEl.value = state.cfg.encoder.volume_step_pct;
  encoderClickEl.value = state.cfg.encoder.click_action;
  if (!editingOsdHold) {
    osdHoldEl.value = state.cfg.encoder.osd_hold_seconds;
    osdHoldValEl.textContent = parseFloat(state.cfg.encoder.osd_hold_seconds).toFixed(1) + "с";
  }
  muteColorEl.value = "#" + state.cfg.encoder.mute_color;
  warningColorEl.value = "#" + state.cfg.encoder.warning_color;
  if (!editingWarningThreshold) warningThresholdEl.value = state.cfg.encoder.warning_threshold_pct;
  renderVolumeColors(state.cfg.encoder.volume_colors);

  if (!editingTautulliUrl) tautulliUrlEl.value = state.cfg.tautulli_url;
  if (!editingTautulliApiKey) tautulliApiKeyEl.value = state.cfg.tautulli_api_key;
  if (!editingMyPlexUser) myPlexUserEl.value = state.cfg.my_plex_user || "";
  if (!editingQbt1Url) qbt1UrlEl.value = state.cfg.qbt1_url;
  if (!editingQbt1ApiKey) qbt1ApiKeyEl.value = state.cfg.qbt1_api_key;
  if (!editingQbt2Url) qbt2UrlEl.value = state.cfg.qbt2_url;
  if (!editingQbt2ApiKey) qbt2ApiKeyEl.value = state.cfg.qbt2_api_key;

  if (!monTargetsLoaded) {
    // Загружается ОДИН РАЗ на старте страницы - дальше monTargets живёт
    // только в браузере (см. подробный комментарий у объявления monTargets
    // выше про то, почему ответ /api/monitor_targets потом игнорируется).
    monTargets = (state.cfg.mon_targets || []).map(t => ({ ...t }));
    renderMonTargets();
    monTargetsLoaded = true;
  }
  if (!editingPingInterval) {
    pingIntervalEl.value = state.cfg.ping_interval_seconds;
    pingIntervalValEl.textContent = Math.round(state.cfg.ping_interval_seconds) + "с";
  }
  if (!editingPingTimeout) pingTimeoutEl.value = state.cfg.ping_timeout_ms;
  if (!editingPingFail) pingFailEl.value = state.cfg.ping_fail_threshold;
  if (!editingPingRecover) pingRecoverEl.value = state.cfg.ping_recover_threshold;

  if (!editingTopProcessMinCpu) {
    topProcessMinCpuEl.value = state.cfg.top_process_min_cpu_pct;
    topProcessMinCpuValEl.textContent = Math.round(state.cfg.top_process_min_cpu_pct) + "%";
  }

  if (!editingLayoutHold) {
    layoutHoldEl.value = state.cfg.layout_hold_seconds;
    layoutHoldValEl.textContent = parseFloat(state.cfg.layout_hold_seconds).toFixed(1) + "с";
  }
  if (!editingDeviceHold) {
    deviceHoldEl.value = state.cfg.device_hold_seconds;
    deviceHoldValEl.textContent = parseFloat(state.cfg.device_hold_seconds).toFixed(1) + "с";
  }
  if (!editingOsdCooldown) {
    osdCooldownEl.value = state.cfg.osd_cooldown_seconds;
    osdCooldownValEl.textContent = parseFloat(state.cfg.osd_cooldown_seconds).toFixed(1) + "с";
  }
  if (!layoutColorsBuilt) buildLayoutColorsRows(state.cfg.layout_colors || {});

  if (!editingPriorityPersonal) priorityPersonalEl.value = state.cfg.priority_boost_personal;
  if (!editingPriorityAmbient) priorityAmbientEl.value = state.cfg.priority_boost_ambient;

  if (!editingPeakHold) {
    peakHoldEl.value = state.cfg.peak_hold_seconds;
    peakHoldValEl.textContent = parseFloat(state.cfg.peak_hold_seconds).toFixed(1) + "с";
  }
  if (!editingPeakFade) {
    peakFadeEl.value = state.cfg.peak_fade_seconds;
    peakFadeValEl.textContent = parseFloat(state.cfg.peak_fade_seconds).toFixed(1) + "с";
  }

  const container = document.getElementById("bar-container");
  container.innerHTML = "";
  container.appendChild(renderBar(state.cfg));
}

fetch("/api/state").then(r => r.json()).then(render);

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }
</script>
</body></html>
"""


def register_settings_routes(app):
    @app.route("/settings")
    def settings_page():
        return Response(SETTINGS_PAGE_HTML, mimetype="text/html")
