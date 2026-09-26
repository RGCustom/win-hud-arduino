"""
offline_webui.py  (win-hud-arduino)

Отдельная страница /offline - настройки офлайн-экрана платы (см. докстринг
"Offline-режим" в win_hud_arduino_firmware.ino за полным описанием
протокола/логики). Хост тут только источник конфига (settings.json, как и
остальной cfg) - реальная логика "показывать/не показывать" и рендер
офлайн-экрана живут на плате (EEPROM), т.к. в момент показа офлайн-экрана
самого хоста уже может не быть. pc_hud.py пересылает cfg["offline_*"] плате
через OFFCFG:/OFFL1-3: при каждом изменении/переподключении - см.
metrics_main_loop() и /api/offline там же.

В отличие от /screens (редактор ОБЫЧНЫХ экранов, полный реестр переменных
из variables.py, repeating-группы, условия показа, tier) - тут ВСЕГО ПЯТЬ
переменных (см. докстринг модуля в .ino про "меньшим количеством
переменных"), список захардкожен прямо тут, а не тянется из /api/variables:
переменные офлайн-экрана вычисляет САМА ПЛАТА (мягкие часы поверх millis(),
см. .ino), а не templates.py/variables.py на хосте - поэтому это не
подмножество общего реестра, а совсем отдельный, маленький список. Живой
предпросмотр (что реально покажет плата прямо сейчас) переиспользует ТОТ ЖЕ
/api/preview, что и /screens (см. screens_webui.py) - рендерит на хосте по
текущему context, это просто удобный ориентир "как выглядело бы сейчас",
сам рендер на плате не завязан на хост и работает независимо от него.
"""

from flask import request, jsonify, Response

OFFLINE_TOKENS = [
    ("time_now", "Время ЧЧ:ММ"),
    ("weekday_name", "День недели (Пн/Вт/...)"),
    ("date_now", "Дата ДД.ММ"),
    ("year_now", "Год ГГГГ"),
    ("uptime", "Аптайм Windows (последнее известное значение)"),
]

OFFLINE_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>win-hud-arduino - offline</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<link rel="icon" type="image/png" href="/favicon.png">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a; --panel: #1f2123; --border: #2c2e31;
    --text: #e6e6e6; --muted: #8a8d91; --accent: #ff8c2f; --danger: #e0483e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:24px 16px 60px; }
  .wrap { max-width:640px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; flex-wrap:wrap; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .global-card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
              padding:18px; margin-bottom:16px; }
  .global-card h2 { font-size:11px; color:var(--muted); margin:0 0 4px; font-weight:600;
                     text-transform:uppercase; letter-spacing:.03em; }
  .global-card .hint { font-size:11px; color:var(--muted); margin-bottom:14px; line-height:1.5; }

  .row { display:flex; align-items:center; gap:10px; margin-bottom:10px; flex-wrap:wrap; }
  .row label { font-size:12px; color:var(--muted); min-width:150px; }
  select, input[type=number], input[type=text], input[type=time] {
    background:#101112; color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:6px 8px; font-size:13px; flex:1; min-width:120px;
  }
  input[type=checkbox] { width:16px; height:16px; }

  .checkbox-row { display:flex; align-items:center; gap:8px; font-size:13px; color:var(--text); margin-bottom:14px; }

  .legend { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0 4px; }
  .legend-item { display:inline-block; background:#101112; border:1px solid var(--border); border-radius:5px;
                 padding:4px 8px; font-size:11px; font-family:monospace; cursor:pointer; color:var(--text); }
  .legend-item:hover { border-color:var(--accent); color:var(--accent); }

  .line-preview { font-size:12px; color:var(--accent); font-family:monospace; margin:4px 0 14px; min-height:16px; }
  .line-preview.err { color:var(--danger); }

  .note { font-size:11px; color:var(--muted); margin-top:6px; line-height:1.5; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/">Sensors</a><a href="/settings">Settings</a><a href="/screens">OLED screens</a><a href="/offline" class="active">Offline</a><a href="/flash">Flash</a></div>

  <div class="global-card">
    <h2>Offline-экран</h2>
    <div class="hint">Когда плата перестаёт получать данные от хоста (компьютер выключен/спит,
      win-hud-arduino закрыт, отвалился USB) - вместо погашенного экрана плата может сама
      показать простые часы/дату, используя собственное время, отсчитываемое от последней
      синхронизации. Настройки и три строки ниже хранятся в EEPROM САМОЙ ПЛАТЫ - она способна
      показывать офлайн-экран, даже когда хоста действительно больше нет рядом.</div>

    <div class="checkbox-row">
      <input type="checkbox" id="offline-enabled">
      <label for="offline-enabled">Показывать offline-экран</label>
    </div>

    <div class="row">
      <label>Через сколько минут молчания</label>
      <input type="number" id="offline-timeout" min="0.5" max="60" step="0.5" value="3">
    </div>
    <div class="note">Пока хост на связи, ниже видно окно активности офлайн-экрана и
      сами строки - но реально включится он только через это время после того, как связь
      с ПК пропадёт.</div>

    <div class="row" style="margin-top:14px">
      <label>Окно активности, с</label>
      <input type="time" id="offline-start" value="00:00">
    </div>
    <div class="row">
      <label>Окно активности, до</label>
      <input type="time" id="offline-end" value="23:59">
    </div>
    <div class="note">Офлайн-экран показывается, только если текущее время суток попадает в
      это окно - вне его плата просто гаснет, как и раньше ("не хочу, чтоб светился ночью").
      Если "до" меньше "с" - окно считается через полночь (например 22:00-08:00). Одинаковые
      значения - "весь день".</div>
  </div>

  <div class="global-card">
    <h2>Строки offline-экрана</h2>
    <div class="hint">Доступно всего пять переменных (плата считает их сама, без хоста) -
      кликни, чтобы вставить в поле. В отличие от обычных экранов на /screens, спецификаторы
      ширины ({var:N}) тут не поддерживаются, а текст не должен содержать символ "|".</div>
    <div class="legend" id="legend"></div>

    <label style="font-size:12px;color:var(--muted);display:block;margin:12px 0 4px">Строка 1</label>
    <input type="text" id="offline-l1" placeholder="{time_now}">
    <div class="line-preview" id="preview-l1"></div>

    <label style="font-size:12px;color:var(--muted);display:block;margin:0 0 4px">Строка 2</label>
    <input type="text" id="offline-l2" placeholder="{weekday_name} {date_now}">
    <div class="line-preview" id="preview-l2"></div>

    <label style="font-size:12px;color:var(--muted);display:block;margin:0 0 4px">Строка 3</label>
    <input type="text" id="offline-l3" placeholder="{uptime}">
    <div class="line-preview" id="preview-l3"></div>
    <div class="note">Предпросмотр считается на хосте по текущим данным - ориентир "как
      выглядело бы прямо сейчас". Сам офлайн-экран рендерит плата независимо от хоста, поэтому
      {uptime} там - замороженное последнее известное значение, а не живой счётчик.</div>
  </div>

  <footer>win-hud-arduino</footer>
</div>

<script>
let focusedField = null;
let editingTimeout = false, editingStart = false, editingEnd = false;
let editingL1 = false, editingL2 = false, editingL3 = false;

document.querySelectorAll('#offline-l1,#offline-l2,#offline-l3').forEach(el => {
  el.addEventListener('focus', () => focusedField = el);
});

function buildLegend() {
  const wrap = document.getElementById('legend');
  wrap.innerHTML = '';
  OFFLINE_TOKENS.forEach(([name, label]) => {
    const span = document.createElement('span');
    span.className = 'legend-item';
    span.textContent = '{' + name + '}';
    span.title = label;
    span.addEventListener('click', () => {
      const field = focusedField || document.getElementById('offline-l1');
      const pos = field.selectionStart || field.value.length;
      field.value = field.value.slice(0, pos) + '{' + name + '}' + field.value.slice(pos);
      field.dispatchEvent(new Event('input'));
      field.focus();
    });
    wrap.appendChild(span);
  });
}

function livePreview(inputEl, previewEl) {
  const tpl = inputEl.value;
  if (!tpl) { previewEl.textContent = ''; previewEl.classList.remove('err'); return; }
  fetch('/api/preview', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ template: tpl }) })
    .then(r => r.json())
    .then(res => {
      if (res.unknown_vars.length) {
        previewEl.textContent = 'Неизвестные переменные: ' + res.unknown_vars.join(', ');
        previewEl.classList.add('err');
      } else {
        previewEl.textContent = '\u2192 ' + (res.rendered || '(пусто)') + (res.all_resolved ? '' : '  (нет данных сейчас)');
        previewEl.classList.remove('err');
      }
    });
}

function debounceSave(el, flagSetter, sendFn) {
  el.addEventListener('input', () => flagSetter(true));
  el.addEventListener('change', () => { sendFn(); flagSetter(false); });
}

function sendOffline(partial) {
  fetch('/api/offline', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(partial) });
}

const enabledEl = document.getElementById('offline-enabled');
enabledEl.addEventListener('change', () => sendOffline({ enabled: enabledEl.checked }));

const timeoutEl = document.getElementById('offline-timeout');
debounceSave(timeoutEl, v => editingTimeout = v, () => sendOffline({ timeout_minutes: parseFloat(timeoutEl.value) }));

const startEl = document.getElementById('offline-start');
debounceSave(startEl, v => editingStart = v, () => sendOffline({ window_start: startEl.value }));

const endEl = document.getElementById('offline-end');
debounceSave(endEl, v => editingEnd = v, () => sendOffline({ window_end: endEl.value }));

['l1', 'l2', 'l3'].forEach(k => {
  const input = document.getElementById('offline-' + k);
  const preview = document.getElementById('preview-' + k);
  input.addEventListener('input', () => livePreview(input, preview));
  input.addEventListener('change', () => {
    const body = {}; body[k] = input.value;
    sendOffline(body);
  });
});

function render(s) {
  const cfg = s.cfg;
  enabledEl.checked = !!cfg.offline_enabled;
  if (!editingTimeout) timeoutEl.value = cfg.offline_timeout_minutes;
  if (!editingStart) startEl.value = cfg.offline_window_start || '00:00';
  if (!editingEnd) endEl.value = cfg.offline_window_end || '23:59';

  if (!editingL1 && document.activeElement !== document.getElementById('offline-l1')) {
    document.getElementById('offline-l1').value = cfg.offline_l1 || '';
  }
  if (!editingL2 && document.activeElement !== document.getElementById('offline-l2')) {
    document.getElementById('offline-l2').value = cfg.offline_l2 || '';
  }
  if (!editingL3 && document.activeElement !== document.getElementById('offline-l3')) {
    document.getElementById('offline-l3').value = cfg.offline_l3 || '';
  }
  ['l1','l2','l3'].forEach(k => livePreview(document.getElementById('offline-'+k), document.getElementById('preview-'+k)));
}

buildLegend();
fetch('/api/state').then(r => r.json()).then(render);

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }
</script>
</body></html>
"""


def register_offline_routes(app):
    import json as _json

    @app.route("/offline")
    def offline_page():
        # OFFLINE_TOKENS встраивается в страницу как JS-константа - тот же
        # список, что валидируется тут же (на будущее, если понадобится
        # сверка на сервере), один источник правды вместо дублирования
        # списка в HTML и в отдельном JS-файле.
        tokens_js = _json.dumps(OFFLINE_TOKENS, ensure_ascii=False)
        html = OFFLINE_PAGE_HTML.replace(
            "<script>",
            "<script>\nconst OFFLINE_TOKENS = " + tokens_js + ";",
            1,
        )
        return Response(html, mimetype="text/html")
