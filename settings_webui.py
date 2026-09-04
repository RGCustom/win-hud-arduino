"""
settings_webui.py  (win-hud-arduino)

Страница /settings - портировано из shkaf-hud, но конструктор теперь на
ОДНУ ленту (вместо 4 независимых баров): режим (classic/center), метрика(и),
цвета, solid, peak hold, а также число диодов (leds_count - настройка, не
константа, см. ledbar.py). Плюс НОВЫЙ блок - настройки энкодера громкости:
шаг на клик, действие на клик кнопки, тайминг и цвета OSD-попапа.

Как и в shkaf-hud, вся серверная логика/состояние - в pc_hud.py (эндпойнты
/api/state, /api/mode, /api/assignment(_top), /api/colors(_top), /api/solid(_top),
/api/peak, /api/leds_count, /api/encoder - этот файл только читает/пишет
через них). Этот файл - чистая разметка + JS.
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
  select, input[type=number] { background:#101112; color:var(--text); border:1px solid var(--border);
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

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/">Sensors</a><a href="/settings" class="active">Settings</a><a href="/screens">OLED screens</a><a href="/flash">Flash</a></div>

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

  <footer>win-hud-arduino</footer>
</div>

<script>
const BAR_ID = "bar0";  // одна лента - один "бар" во внутреннем API (совместимость с shkaf-hud кодом)
let metricsMap = {};
let editingPeakHold = false, editingPeakFade = false, editingOsdHold = false;
let editingLedsCount = false, editingVolumeStep = false, editingWarningThreshold = false;

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
