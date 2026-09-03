#!/usr/bin/env python3
"""
pc_hud.py  (win-hud-arduino)

Главный скрипт - аналог shkaf_stats_bridge.py, но под Windows-хост и с двумя
принципиальными отличиями от shkaf-hud:

  1. Serial - ДВУСТОРОННИЙ. Плата не только принимает BAR/BRI/CON/L1-3, но и
     сама шлёт события энкодера (ENC:/BTN:, см. protocol.parse_incoming_line).
     Вместо отдельного потока-читателя главный цикл крутится с маленьким
     тиком (TICK_INTERVAL, по умолчанию 100мс) и на каждой итерации сначала
     неблокирующе вычитывает всё, что накопилось в порту, потом (не на
     каждой итерации, а раз в POLL_INTERVAL) пересчитывает "медленные"
     метрики (CPU/RAM/GPU/диски/сеть/OLED-экраны). BAR-пиксели пересчитываются
     и шлются КАЖДЫЙ тик - чтобы OSD громкости откликался на вращение
     энкодера быстро, а не раз в секунду. Один поток = один писатель в
     serial - никаких блокировок между чтением и записью не требуется.

  2. Запуск - трей-иконка (pystray), а не голый процесс в Docker. Flask и
     основной цикл метрик крутятся в фоновых потоках, сама трей-иконка
     блокирует главный поток (так требует pystray на Windows).

Зависимости (requirements.txt):
    pyserial, flask, psutil, pynvml, pycaw, comtypes, pywin32, pystray, pillow
"""

import copy
import json
import os
import sys
import threading
import time
import webbrowser

import serial
from flask import Flask, request, jsonify, Response, send_file

import variables
import templates
import screens
import screens_webui
import settings_webui
import protocol
import ledbar
import metrics_windows
import flash
import flash_webui

SCRIPT_VERSION = "2026-08-31-1"

CONTAINER_START_TIME = time.time()

# ---------------- КОНФИГ ----------------

BAUD = int(os.environ.get("BAUD", "115200"))

# NET_MAX_MBPS - что считать 100% на LED-метрике "net" (использует тот же
# интерфейс, что выбран для OLED-экрана Network 1 - net1_iface в settings,
# отдельного "LED-only" интерфейса не заводим, чтобы не плодить настройки).
NET_MAX_MBPS = float(os.environ.get("NET_MAX_MBPS", "300"))

POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))       # медленные метрики (CPU/RAM/GPU/диски/сеть/экраны)
TICK_INTERVAL = float(os.environ.get("TICK_INTERVAL", "0.04"))        # частота главного цикла (BAR/serial-чтение) -
                                                                        # 25 Гц; понижено с прежних 0.1с (10 Гц) - для
                                                                        # VU-эквалайзера (см. read_vu() в
                                                                        # metrics_windows.py) 10 Гц ощущались
                                                                        # ступенчато. Если при большом числе
                                                                        # светодиодов упрётесь в пропускную
                                                                        # способность serial (115200 бод) - можно
                                                                        # поднять обратно через переменную окружения
                                                                        # TICK_INTERVAL=0.06 и т.п., без правки кода.
FULL_RESYNC_SECONDS = float(os.environ.get("FULL_RESYNC_SECONDS", "30"))

WEB_PORT = int(os.environ.get("WEB_PORT", "8189"))

_DEFAULT_CONFIG_DIR = os.path.join(os.environ.get("APPDATA", "."), "win-hud-arduino")
CONFIG_DIR = os.environ.get("CONFIG_DIR", _DEFAULT_CONFIG_DIR)
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
ICON_PATH = os.path.join(ASSETS_DIR, "icon.png")
FAVICON_PATH = os.path.join(ASSETS_DIR, "favicon.png")

# ---------------- дефолтные настройки ----------------
# Лента одна, но внутренний ключ "bar0" сохранён ради совместимости с уже
# написанным settings_webui.py/JS (тот же формат {bar0: value} на все API).

DEFAULT_COLORS = {"bar0": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"}}
DEFAULT_COLORS_TOP = copy.deepcopy(DEFAULT_COLORS)
DEFAULT_ASSIGNMENT = {"bar0": "cpu"}
DEFAULT_ASSIGNMENT_TOP = dict(DEFAULT_ASSIGNMENT)
DEFAULT_SOLID = {"bar0": False}
DEFAULT_SOLID_TOP = {"bar0": False}
DEFAULT_MODE = {"bar0": "classic"}
DEFAULT_PEAK = {"bar0": {"enabled": False, "style": "hold"}}
DEFAULT_BRIGHTNESS = 15
DEFAULT_PEAK_HOLD_SECONDS = 2.0
DEFAULT_PEAK_FADE_SECONDS = 1.5

DEFAULT_ENCODER = {
    "volume_step_pct": 2,          # % громкости на один "клик" вращения
    "click_action": "mute_toggle",  # mute_toggle | switch_device (последнее - заглушка, см. metrics_windows.py)
    "osd_hold_seconds": 3.0,       # сколько держится OSD после последнего вращения/клика
    "mute_color": "FF0000",
    "warning_color": "FFA500",
    "warning_threshold_pct": 95,
    "volume_colors": {"c1": "00FF42", "c2": "FFF600", "c3": "FF0000"},
}

BAR_METRICS = {
    "cpu": "CPU",
    "ram": "RAM",
    "gpu": "GPU загрузка",
    "gpu_vram": "GPU VRAM",
    "disk1": "Диск 1, %",
    "disk2": "Диск 2, %",
    "net": "NET (Network 1, для LED)",
    # VU-метр - реальный уровень играющего звука (пики сигнала), НЕ системная
    # громкость (volume_pct) - см. metrics_windows.AudioController.read_vu().
    # peak - общий пик по всем каналам сразу (удобно для classic-режима);
    # left/right - для честного стерео в center-режиме (низ=left, верх=right).
    "vu_peak": "VU: пик громкости (звук)",
    "vu_left": "VU: левый канал",
    "vu_right": "VU: правый канал",
}

CLICK_ACTIONS = ("mute_toggle", "switch_device")

DEFAULT_SETTINGS = {
    "colors": DEFAULT_COLORS,
    "colors_top": DEFAULT_COLORS_TOP,
    "assignment": DEFAULT_ASSIGNMENT,
    "assignment_top": DEFAULT_ASSIGNMENT_TOP,
    "mode": DEFAULT_MODE,
    "brightness": DEFAULT_BRIGHTNESS,
    "solid": DEFAULT_SOLID,
    "solid_top": DEFAULT_SOLID_TOP,
    "peak": DEFAULT_PEAK,
    "peak_hold_seconds": DEFAULT_PEAK_HOLD_SECONDS,
    "peak_fade_seconds": DEFAULT_PEAK_FADE_SECONDS,
    "contrast": 255,
    "leds_count": 30,
    # Частота главного цикла (VU/BAR-обновления, чтение serial), в секундах -
    # см. также TICK_INTERVAL (env var) в шапке файла. Дефолт тут = значению
    # TICK_INTERVAL на момент старта - т.е. пока настройку никто не трогал
    # через /settings, поведение то же, что было раньше (управлялось только
    # переменной окружения). Как только пользователь один раз сохранит
    # значение через /api/tick_interval, оно осядет в settings.json и с
    # этого момента будет ПЕРЕВЕШИВАТЬ переменную окружения при каждом
    # следующем запуске (см. load_settings() - saved-значение всегда в
    # приоритете над DEFAULT_SETTINGS). Это осознанный компромисс: раз
    # настройка живёт в /settings как обычный слайдер, она должна вести
    # себя как остальные - переживать перезапуски независимо от env.
    "tick_interval": TICK_INTERVAL,
    "serial_port": "",
    "net1_iface": "",
    "net2_iface": "",
    "disk1_letter": "",
    "disk2_letter": "",
    "encoder": DEFAULT_ENCODER,
}


def load_settings():
    """Рекурсивный merge с дефолтами - без изменений логики относительно
    shkaf-hud (двухуровневый merge для dict-of-dict уже покрывает и
    encoder.volume_colors, т.к. это тоже плоский dict на верхнем уровне
    вложенности - см. DEFAULT_ENCODER)."""
    try:
        with open(SETTINGS_FILE) as f:
            saved = json.load(f)
    except Exception:
        saved = {}

    cfg = copy.deepcopy(DEFAULT_SETTINGS)
    for key, default_val in DEFAULT_SETTINGS.items():
        if key not in saved:
            continue
        saved_val = saved[key]
        if isinstance(default_val, dict):
            if isinstance(saved_val, dict):
                for sub_key, sub_val in saved_val.items():
                    if sub_key in cfg[key] and isinstance(cfg[key][sub_key], dict) and isinstance(sub_val, dict):
                        cfg[key][sub_key].update(sub_val)
                    elif sub_key in cfg[key] and not isinstance(cfg[key][sub_key], dict):
                        cfg[key][sub_key] = sub_val
        else:
            cfg[key] = saved_val
    return cfg


def save_settings(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(cfg, f)


state_lock = threading.Lock()
state = {
    "bar": {"mode": "classic", "pixels": [], "pct_bottom": 0, "pct_top": None, "osd_active": False},
    "cfg": load_settings(),
    "serial_connected": False,
    "oled_lines": ["", "", ""],
}

_last_context = {}
_context_lock = threading.Lock()

flashing_event = threading.Event()

gpu_monitor = metrics_windows.GpuMonitor()
audio_controller = metrics_windows.AudioController()
media_monitor = metrics_windows.MediaMonitor()


def get_context():
    with _context_lock:
        return dict(_last_context)


# ---------------- форматтеры (аналог shkaf-hud) ----------------

def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_rate(bytes_delta, dt):
    if dt <= 0 or bytes_delta < 0:
        return "0Kbps"
    bits_per_sec = bytes_delta * 8 / dt
    mbps = bits_per_sec / 1_000_000
    if mbps >= 1:
        return f"{mbps:.1f}Mbps"
    kbps = bits_per_sec / 1000
    return f"{kbps:.0f}Kbps"


def format_bytes_total(bytes_val):
    if bytes_val is None or bytes_val < 0:
        return "0MB"
    gb = bytes_val / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f}GB"
    mb = bytes_val / (1024 ** 2)
    return f"{mb:.0f}MB"


def format_speed_mbps(mbps):
    if not mbps:
        return "?"
    if mbps >= 1000:
        return f"{mbps / 1000:g}Gbit"
    return f"{mbps}Mbit"


# ---------------- assets (иконка трея/favicon - генерируются, если отсутствуют) ----------------

def _ensure_assets():
    """Если assets/icon.png нет (например первый запуск из исходников, а не
    из готового дистрибутива) - рисуем простую иконку через Pillow, чтобы
    трею и /favicon.png/ /icon.png было что раздавать без внешних файлов."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    if os.path.isfile(ICON_PATH) and os.path.isfile(FAVICON_PATH):
        return
    try:
        from PIL import Image, ImageDraw
        size = 256
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, size - 8, size - 8), fill=(255, 140, 47, 255))
        draw.ellipse((size * 0.28, size * 0.28, size * 0.72, size * 0.72), fill=(23, 24, 26, 255))
        img.save(ICON_PATH)
        img.resize((32, 32)).save(FAVICON_PATH)
    except Exception as e:
        print(f"[assets] не удалось сгенерировать иконку: {e}", flush=True)


# ---------------- serial ----------------

def try_open_serial(port):
    if not port:
        return None
    try:
        s = serial.Serial(port, BAUD, timeout=0)  # timeout=0 - неблокирующее чтение
        time.sleep(2)
        with state_lock:
            state["serial_connected"] = True
        print(f"[serial] connected: {port}", flush=True)
        return s
    except (serial.SerialException, OSError):
        with state_lock:
            state["serial_connected"] = False
        return None


# ---------------- энкодер: применение событий ----------------

def apply_encoder_delta(delta, cfg):
    step = cfg["encoder"]["volume_step_pct"]
    audio_controller.set_volume_relative(delta * step)


def apply_button_click(cfg):
    action = cfg["encoder"]["click_action"]
    if action == "mute_toggle":
        audio_controller.toggle_mute()
    elif action == "switch_device":
        audio_controller.switch_output_device()


# ---------------- веб-интерфейс (Sensors) ----------------

app = Flask(__name__)

SENSORS_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>win-hud-arduino</title>
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
  .wrap { max-width:560px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; flex-wrap:wrap; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .banner { display:none; background:#3a2418; border:1px solid var(--danger); color:#ffb3ab;
            border-radius:10px; padding:12px 16px; margin-bottom:18px; font-size:13px;
            align-items:center; gap:10px; }
  .banner.show { display:flex; }
  .banner .b-dot { width:8px; height:8px; border-radius:50%; background:var(--danger); flex-shrink:0; }

  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:22px; margin-bottom:18px; }
  .card h2 { font-size:11px; color:var(--muted); margin:0 0 18px; font-weight:600; }

  .strip-track { width:100%; height:36px; background:#101112; border-radius:6px;
                 display:flex; flex-direction:row; overflow:hidden; border:1px solid var(--border);
                 padding:2px; gap:1px; }
  .led-px { flex:1 1 auto; min-width:1px; border-radius:1px; background:#101112; transition:background .15s; }
  .label { font-size:12px; color:var(--muted); text-align:center; margin-top:8px; }
  .label b { color:var(--text); font-size:13px; }
  .osd-badge { display:none; background:var(--accent); color:#151515; font-size:10px; font-weight:700;
               border-radius:4px; padding:2px 6px; margin-left:8px; }
  .osd-badge.show { display:inline-block; }

  .brightness-row, .field-row { display:flex; align-items:center; gap:10px; margin-top:12px; font-size:13px; }
  .brightness-row label, .field-row label { color:var(--muted); min-width:110px; }
  .brightness-row input[type=range] { flex:1; }
  .brightness-row .val { min-width:36px; text-align:right; color:var(--text); }

  select.iface { background:#101112; color:var(--text); border:1px solid var(--border);
                  border-radius:6px; font-size:12px; padding:5px 6px; width:100%; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/" class="active">Sensors</a><a href="/settings">Settings</a><a href="/screens">OLED screens</a><a href="/flash">Flash</a></div>

  <div class="banner" id="banner"><span class="b-dot"></span>
    Pro Micro не подключена - лента и OLED не обновляются, метрики продолжают собираться</div>

  <div class="card">
    <h2>ЛЕНТА <span class="osd-badge" id="osd-badge">VOLUME OSD</span></h2>
    <div class="strip-track" id="pixels-strip"></div>
    <div class="label"><b><span id="val-strip"></span></b></div>
    <div class="brightness-row">
      <label>Яркость</label>
      <input type="range" id="brightness" min="0" max="100" value="15">
      <span class="val" id="brightness-val">15%</span>
    </div>
  </div>

  <div class="card">
    <h2>OLED (текущий экран)</h2>
    <div style="background:#000;color:#7fd8ff;font-family:monospace;font-size:18px;padding:16px;border-radius:8px;line-height:1.5" id="oled"></div>
    <div class="brightness-row">
      <label>Контраст</label>
      <input type="range" id="contrast" min="0" max="255" value="255">
      <span class="val" id="contrast-val">255</span>
    </div>
  </div>

  <div class="card">
    <h2>ПОДКЛЮЧЕНИЕ</h2>
    <div class="field-row"><label>COM-порт платы</label><select class="iface" id="serial-port"></select></div>
  </div>

  <div class="card">
    <h2>ДИСКИ (для экранов и метрики ленты)</h2>
    <div class="field-row"><label>Диск 1</label><select class="iface" id="disk1-letter"><option value="">(не выбран)</option></select></div>
    <div class="field-row"><label>Диск 2</label><select class="iface" id="disk2-letter"><option value="">(не выбран)</option></select></div>
  </div>

  <div class="card">
    <h2>СЕТЕВЫЕ ИНТЕРФЕЙСЫ (для экранов и метрики ленты "net")</h2>
    <div class="field-row"><label>Network 1</label><select class="iface" id="net1-iface"></select></div>
    <div class="field-row"><label>Network 2</label><select class="iface" id="net2-iface"><option value="">(не выбран)</option></select></div>
  </div>

  <footer>win-hud-arduino</footer>
</div>

<script>
let editingBrightness = false, editingContrast = false, editingSelects = false;
let selectsPopulated = false, pixelsBuilt = false, lastLedsCount = 0;

const brightnessEl = document.getElementById("brightness");
brightnessEl.addEventListener("input", () => {
  editingBrightness = true;
  document.getElementById("brightness-val").textContent = brightnessEl.value + "%";
});
brightnessEl.addEventListener("change", () => {
  fetch("/api/brightness", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(brightnessEl.value) }) }).then(() => editingBrightness = false);
});

const contrastEl = document.getElementById("contrast");
contrastEl.addEventListener("input", () => {
  editingContrast = true;
  document.getElementById("contrast-val").textContent = contrastEl.value;
});
contrastEl.addEventListener("change", () => {
  fetch("/api/contrast", { method: "POST", headers: {"Content-Type":"application/json"},
    body: JSON.stringify({ value: parseInt(contrastEl.value) }) }).then(() => editingContrast = false);
});

function fillSelect(sel, options, current, allowEmpty) {
  sel.innerHTML = allowEmpty ? '<option value="">(не выбран)</option>' : "";
  options.forEach(name => {
    const o = document.createElement("option");
    o.value = name; o.textContent = name;
    if (name === current) o.selected = true;
    sel.appendChild(o);
  });
}

function populateSelects(s) {
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

function buildPixelGrid(ledsCount) {
  const track = document.getElementById("pixels-strip");
  track.innerHTML = "";
  for (let i = 0; i < ledsCount; i++) {
    const sq = document.createElement("div");
    sq.className = "led-px";
    sq.id = "px-" + i;
    track.appendChild(sq);
  }
  pixelsBuilt = true;
  lastLedsCount = ledsCount;
}

function refresh() {
  fetch("/api/state").then(r => r.json()).then(s => {
    document.getElementById("banner").classList.toggle("show", !s.serial_connected);
    if (!selectsPopulated) populateSelects(s);
    if (!pixelsBuilt || lastLedsCount !== s.leds_count) buildPixelGrid(s.leds_count);

    const bar = s.bar;
    bar.pixels.forEach((hex, i) => {
      const px = document.getElementById("px-" + i);
      if (px) px.style.background = "#" + hex;
    });
    const label = bar.mode === "center" ? (bar.pct_bottom + "% / " + bar.pct_top + "%") : (bar.pct_bottom + "%");
    document.getElementById("val-strip").textContent = label;
    document.getElementById("osd-badge").classList.toggle("show", bar.osd_active);

    if (!editingBrightness) {
      brightnessEl.value = s.cfg.brightness;
      document.getElementById("brightness-val").textContent = s.cfg.brightness + "%";
    }
    if (!editingContrast) {
      contrastEl.value = s.cfg.contrast;
      document.getElementById("contrast-val").textContent = s.cfg.contrast;
    }

    document.getElementById("oled").innerHTML = s.oled_lines.map(l => l || "&nbsp;").join("<br>");
  });
}

if ('serviceWorker' in navigator) { navigator.serviceWorker.register('/sw.js').catch(() => {}); }

setInterval(refresh, 500);
refresh();
</script>
</body></html>
"""


@app.route("/")
def index():
    return Response(SENSORS_PAGE_HTML, mimetype="text/html")


@app.route("/favicon.png")
def favicon():
    return send_file(FAVICON_PATH, mimetype="image/png")


@app.route("/icon.png")
def icon_png():
    return send_file(ICON_PATH, mimetype="image/png")


@app.route("/api/state")
def api_state():
    with state_lock:
        out = dict(state)
        out["metrics"] = BAR_METRICS
        out["click_actions"] = CLICK_ACTIONS
        out["available_interfaces"] = metrics_windows.list_network_interfaces()
        out["available_disks"] = metrics_windows.list_disk_letters()
        out["available_ports"] = sorted(flash.list_com_ports())
        out["leds_count"] = state["cfg"]["leds_count"]
        return jsonify(out)


@app.route("/api/colors", methods=["POST"])
def api_colors():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            for stop in ("c1", "c2", "c3"):
                if stop in body["bar0"]:
                    state["cfg"]["colors"]["bar0"][stop] = body["bar0"][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/colors_top", methods=["POST"])
def api_colors_top():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            for stop in ("c1", "c2", "c3"):
                if stop in body["bar0"]:
                    state["cfg"]["colors_top"]["bar0"][stop] = body["bar0"][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/assignment", methods=["POST"])
def api_assignment():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body and body["bar0"] in BAR_METRICS:
            state["cfg"]["assignment"]["bar0"] = body["bar0"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/assignment_top", methods=["POST"])
def api_assignment_top():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body and body["bar0"] in BAR_METRICS:
            state["cfg"]["assignment_top"]["bar0"] = body["bar0"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/mode", methods=["POST"])
def api_mode():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body and body["bar0"] in ("classic", "center"):
            state["cfg"]["mode"]["bar0"] = body["bar0"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/solid", methods=["POST"])
def api_solid():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            state["cfg"]["solid"]["bar0"] = bool(body["bar0"])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/solid_top", methods=["POST"])
def api_solid_top():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            state["cfg"]["solid_top"]["bar0"] = bool(body["bar0"])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/brightness", methods=["POST"])
def api_brightness():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["brightness"] = max(0, min(100, int(body.get("value", state["cfg"]["brightness"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/contrast", methods=["POST"])
def api_contrast():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["contrast"] = max(0, min(255, int(body.get("value", state["cfg"]["contrast"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/peak", methods=["POST"])
def api_peak():
    body = request.get_json(force=True)
    with state_lock:
        if "bar0" in body:
            entry = body["bar0"]
            if "enabled" in entry:
                state["cfg"]["peak"]["bar0"]["enabled"] = bool(entry["enabled"])
            if "style" in entry and entry["style"] in ("hold", "fade"):
                state["cfg"]["peak"]["bar0"]["style"] = entry["style"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/peak_timing", methods=["POST"])
def api_peak_timing():
    body = request.get_json(force=True)
    with state_lock:
        if "hold_seconds" in body:
            state["cfg"]["peak_hold_seconds"] = round(max(0.0, min(10.0, float(body["hold_seconds"]))), 1)
        if "fade_seconds" in body:
            state["cfg"]["peak_fade_seconds"] = round(max(0.0, min(10.0, float(body["fade_seconds"]))), 1)
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/leds_count", methods=["POST"])
def api_leds_count():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["leds_count"] = max(1, min(300, int(body.get("value", state["cfg"]["leds_count"]))))
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/tick_interval", methods=["POST"])
def api_tick_interval():
    """Частота главного цикла (VU/лента/serial) - см. tick_interval в
    DEFAULT_SETTINGS выше и использование в главном цикле ниже. Границы
    0.02с (50 Гц) - 0.5с (2 Гц): нижняя - чтобы не заспамить serial-порт
    при большом leds_count (см. предупреждение в README про пропускную
    способность 115200 бод), верхняя - чтобы настройка не превращала ленту
    в полностью неотзывчивую по ошибке."""
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["tick_interval"] = round(
            max(0.02, min(0.5, float(body.get("value", state["cfg"]["tick_interval"])))), 3
        )
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/serial_port", methods=["POST"])
def api_serial_port():
    body = request.get_json(force=True)
    with state_lock:
        state["cfg"]["serial_port"] = body.get("value", state["cfg"]["serial_port"])
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/disks", methods=["POST"])
def api_disks():
    body = request.get_json(force=True)
    with state_lock:
        if "disk1_letter" in body:
            state["cfg"]["disk1_letter"] = body["disk1_letter"]
        if "disk2_letter" in body:
            state["cfg"]["disk2_letter"] = body["disk2_letter"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/net-ifaces", methods=["POST"])
def api_net_ifaces():
    body = request.get_json(force=True)
    with state_lock:
        if "net1_iface" in body:
            state["cfg"]["net1_iface"] = body["net1_iface"]
        if "net2_iface" in body:
            state["cfg"]["net2_iface"] = body["net2_iface"]
        save_settings(state["cfg"])
    return jsonify({"ok": True})


@app.route("/api/encoder", methods=["POST"])
def api_encoder():
    body = request.get_json(force=True)
    with state_lock:
        enc = state["cfg"]["encoder"]
        if "volume_step_pct" in body:
            enc["volume_step_pct"] = max(1, min(20, int(body["volume_step_pct"])))
        if "click_action" in body and body["click_action"] in CLICK_ACTIONS:
            enc["click_action"] = body["click_action"]
        if "osd_hold_seconds" in body:
            enc["osd_hold_seconds"] = round(max(0.5, min(10.0, float(body["osd_hold_seconds"]))), 1)
        if "mute_color" in body:
            enc["mute_color"] = body["mute_color"].upper()
        if "warning_color" in body:
            enc["warning_color"] = body["warning_color"].upper()
        if "warning_threshold_pct" in body:
            enc["warning_threshold_pct"] = max(50, min(100, int(body["warning_threshold_pct"])))
        if "volume_colors" in body:
            for stop in ("c1", "c2", "c3"):
                if stop in body["volume_colors"]:
                    enc["volume_colors"][stop] = body["volume_colors"][stop].upper()
        save_settings(state["cfg"])
    return jsonify({"ok": True})


screens_webui.register_screens_routes(app, get_context)
settings_webui.register_settings_routes(app)
flash_webui.register_flash_routes(app, lambda: state["cfg"]["serial_port"], flashing_event)


def run_web():
    app.run(host="127.0.0.1", port=WEB_PORT, use_reloader=False)


# ---------------- главный цикл метрик + serial ----------------

def metrics_main_loop(stop_event):
    print(f"[win-hud-arduino] metrics loop starting, version {SCRIPT_VERSION}", flush=True)

    # COM (comtypes/pycaw) должен быть инициализирован В ЭТОМ ПОТОКЕ до
    # первого обращения к audio_controller - иначе pycaw падает с
    # "Не был произведен вызов CoInitialize" (COM per-thread, а этот цикл
    # живёт в отдельном от главного потоке). См. metrics_windows.init_com_for_thread().
    metrics_windows.init_com_for_thread()

    with state_lock:
        state["cfg"] = load_settings()

    ser = None
    last_reconnect_attempt = 0.0

    prev_net_iface = {"net1": None, "net2": None}
    prev_net_counters = {"net1": (None, None), "net2": (None, None)}
    net_base_counters = {"net1": (None, None), "net2": (None, None)}

    peak_trackers = {"bottom": ledbar.PeakHold(), "top": ledbar.PeakHold()}

    rotation = screens.RotationState()
    proto = protocol.ProtocolState(full_resync_seconds=FULL_RESYNC_SECONDS)

    common_metrics = {
        "cpu": 0.0, "ram": 0.0, "gpu": 0.0, "gpu_vram": 0.0, "disk1": 0.0, "disk2": 0.0, "net": 0.0,
        "vu_peak": 0.0, "vu_left": 0.0, "vu_right": 0.0,
    }
    audio_state = {"volume_pct": 0, "volume_muted": "нет", "audio_device_name": "N/A"}
    media_state = {"media_title": None, "media_artist": None, "media_playing": "нет"}
    lines = ["", "", ""]

    osd_active = False
    osd_until = 0.0

    last_metrics_tick = 0.0
    # last_vu_time - ОТДЕЛЬНЫЙ от last_metrics_tick таймер: VU обновляется
    # каждый тик (TICK_INTERVAL, ~100мс), а не раз в POLL_INTERVAL (1с) как
    # остальные метрики - иначе индикатор реального звука будет заметно
    # дёрганым/с лагом. dt между тиками нужен AudioController.read_vu() для
    # плавного затухания пика (см. VU_RELEASE_SECONDS в metrics_windows.py).
    last_vu_time = time.time()
    read_buffer = ""

    while not stop_event.is_set():
        loop_t0 = time.time()
        now = loop_t0

        with state_lock:
            cfg = copy.deepcopy(state["cfg"])

        # ---- (пере)подключение / отключение на время прошивки ----
        if flashing_event.is_set():
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
            last_reconnect_attempt = now
        elif ser is None and now - last_reconnect_attempt > 5:
            ser = try_open_serial(cfg["serial_port"])
            if ser is not None:
                proto.reset()
            last_reconnect_attempt = now

        # ---- неблокирующее чтение входящих строк (ENC:/BTN:) ----
        if ser is not None and not flashing_event.is_set():
            try:
                waiting = ser.in_waiting
                if waiting:
                    read_buffer += ser.read(waiting).decode("utf-8", errors="ignore")
                    while "\n" in read_buffer:
                        line, read_buffer = read_buffer.split("\n", 1)
                        event = protocol.parse_incoming_line(line)
                        if event is None:
                            continue
                        kind, value = event
                        if kind == "encoder":
                            apply_encoder_delta(value, cfg)
                        elif kind == "button":
                            apply_button_click(cfg)
                        # любое событие энкодера/кнопки - показать OSD громкости
                        audio_state = audio_controller.read_state()
                        osd_active = True
                        osd_until = now + cfg["encoder"]["osd_hold_seconds"]
            except (serial.SerialException, OSError):
                print("[serial] read failed, will reconnect", flush=True)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
                last_reconnect_attempt = now

        # ---- медленные метрики: раз в POLL_INTERVAL ----
        if now - last_metrics_tick >= POLL_INTERVAL:
            dt = now - last_metrics_tick if last_metrics_tick else POLL_INTERVAL
            last_metrics_tick = now

            cpu_pct, cpu_pct_core_max, cpu_freq_mhz = metrics_windows.read_cpu_stats()
            ram_pct, ram_used_gb, ram_total_gb = metrics_windows.read_ram_stats()
            gpu_stats = gpu_monitor.read()

            disk1 = metrics_windows.read_disk_usage(cfg["disk1_letter"])
            disk2 = metrics_windows.read_disk_usage(cfg["disk2_letter"])

            disks_ctx = {}
            if cfg["disk1_letter"] and disk1:
                disks_ctx[cfg["disk1_letter"]] = disk1
            if cfg["disk2_letter"] and disk2:
                disks_ctx[cfg["disk2_letter"]] = disk2

            # сеть по слотам net1/net2 (для OLED) + метрика "net" для ленты
            # (использует net1 - отдельного LED-only интерфейса не заводим)
            net_ctx = {"net1": None, "net2": None}
            net_pct = 0.0
            for slot in ("net1", "net2"):
                iface = cfg[f"{slot}_iface"]
                if iface != prev_net_iface[slot]:
                    prev_net_counters[slot] = (None, None)
                    net_base_counters[slot] = (None, None)
                    prev_net_iface[slot] = iface
                if not iface:
                    continue
                rx, tx = metrics_windows.read_iface_counters(iface)
                if rx is None:
                    continue
                prev_rx, prev_tx = prev_net_counters[slot]
                rx_str = format_rate(rx - prev_rx, dt) if prev_rx is not None else "0Kbps"
                tx_str = format_rate(tx - prev_tx, dt) if prev_tx is not None else "0Kbps"

                if prev_rx is not None and dt > 0 and slot == "net1":
                    mbps = (rx - prev_rx) * 8 / 1_000_000 / dt
                    net_pct = max(0.0, min(100.0, mbps / NET_MAX_MBPS * 100.0))

                base_rx, base_tx = net_base_counters[slot]
                if base_rx is None:
                    base_rx, base_tx = rx, tx
                total_rx_str = format_bytes_total(rx - base_rx)
                total_tx_str = format_bytes_total(tx - base_tx)

                prev_net_counters[slot] = (rx, tx)
                net_base_counters[slot] = (base_rx, base_tx)

                net_ctx[slot] = {
                    "name": iface,
                    "speed": format_speed_mbps(metrics_windows.read_iface_speed_mbps(iface)),
                    "rx": rx_str, "tx": tx_str,
                    "total_rx": total_rx_str, "total_tx": total_tx_str,
                }

            audio_state = audio_controller.read_state()
            media_state = media_monitor.read()
            keyboard_layout = metrics_windows.get_keyboard_layout()

            common_metrics = {
                "cpu": cpu_pct, "ram": ram_pct,
                "gpu": gpu_stats["gpu_pct"], "gpu_vram": gpu_stats["gpu_vram_pct"],
                "disk1": disk1["used_pct"] if disk1 else 0.0,
                "disk2": disk2["used_pct"] if disk2 else 0.0,
                "net": net_pct,
            }

            context = {
                "cpu_pct": round(cpu_pct), "cpu_pct_core_max": round(cpu_pct_core_max),
                "cpu_freq_mhz": cpu_freq_mhz,
                "ram_pct": round(ram_pct), "ram_used_gb": ram_used_gb, "ram_total_gb": ram_total_gb,
                "gpu_name": gpu_stats["gpu_name"], "gpu_pct": round(gpu_stats["gpu_pct"]),
                "gpu_temp_c": gpu_stats["gpu_temp_c"],
                "gpu_vram_used_gb": gpu_stats["gpu_vram_used_gb"], "gpu_vram_total_gb": gpu_stats["gpu_vram_total_gb"],
                "gpu_vram_pct": round(gpu_stats["gpu_vram_pct"]), "gpu_power_w": gpu_stats["gpu_power_w"],
                "disk_slots": {"disk1_letter": cfg["disk1_letter"], "disk2_letter": cfg["disk2_letter"]},
                "disks": disks_ctx,
                "net": net_ctx,
                "uptime": format_duration(time.time() - _boot_time()),
                "container_uptime": format_duration(now - CONTAINER_START_TIME),
                "time_now": time.strftime("%H:%M"),
                "volume_pct": audio_state["volume_pct"], "volume_muted": audio_state["volume_muted"],
                "audio_device_name": audio_state["audio_device_name"],
                # VU (реальный уровень звука) для OLED-шаблонов - берём уже
                # посчитанное значение из common_metrics (обновляется каждый
                # тик ниже по циклу, см. блок "VU" после медленных метрик) -
                # отдельный COM-вызов тут не нужен, лаг не больше одного тика
                # (~TICK_INTERVAL), для текстового экрана это незаметно.
                "vu_peak_pct": round(common_metrics.get("vu_peak", 0.0)),
                "vu_left_pct": round(common_metrics.get("vu_left", 0.0)),
                "vu_right_pct": round(common_metrics.get("vu_right", 0.0)),
                "keyboard_layout": keyboard_layout,
                "media_title": media_state["media_title"],
                "media_artist": media_state["media_artist"],
                "media_playing": media_state["media_playing"],
            }
            with _context_lock:
                _last_context.clear()
                _last_context.update(context)

            current_screens = screens_webui.get_screens()
            lines = rotation.current_lines(current_screens, context, now=now)
            with state_lock:
                state["oled_lines"] = lines

        # ---- VU (реальный уровень звука): каждый тик, НЕ раз в POLL_INTERVAL -
        # иначе индикатор ощутимо дёргается/лагает при интервале в секунду.
        # dt считаем по факту прошедшего времени между итерациями (а не
        # "теоретический" TICK_INTERVAL) - на случай, если предыдущая
        # итерация подвисла на serial write/read. Пишем в common_metrics
        # ПОСЛЕ блока медленных метрик выше - там common_metrics иногда
        # переприсваивается целиком, и vu-ключи иначе терялись бы до
        # следующего POLL_INTERVAL.
        vu_dt = now - last_vu_time
        last_vu_time = now
        try:
            vu_state = audio_controller.read_vu(dt=vu_dt if vu_dt > 0 else cfg.get("tick_interval", TICK_INTERVAL))
        except Exception as e:
            # Страховка: на реальном запуске необработанное исключение
            # именно отсюда (AttributeError из-за неполного объявления
            # IAudioMeterInformation в pycaw - см. metrics_windows.py) убило
            # ВЕСЬ поток metrics_main_loop целиком, а не только VU - экран
            # переставал обновляться вообще (CPU/RAM/лента/OLED - всё
            # замирало). read_vu() теперь сама не должна бросать исключения,
            # но эта обвязка - защита именно от того, чтобы ЛЮБАЯ будущая
            # ошибка в чтении звука не могла повторить тот же сценарий.
            print(f"[audio] read_vu() unexpected error, VU отключён на этот тик: {e}", flush=True)
            vu_state = {"vu_peak_pct": 0.0, "vu_left_pct": 0.0, "vu_right_pct": 0.0}
        common_metrics["vu_peak"] = vu_state["vu_peak_pct"]
        common_metrics["vu_left"] = vu_state["vu_left_pct"]
        common_metrics["vu_right"] = vu_state["vu_right_pct"]

        # ---- лента: OSD громкости ИЛИ обычная метрика (каждый тик) ----
        leds_count = cfg["leds_count"]

        if osd_active and now < osd_until:
            enc = cfg["encoder"]
            pixels = ledbar.compute_volume_osd_pixels(
                audio_state["volume_pct"],
                enc["volume_colors"]["c1"], enc["volume_colors"]["c2"], enc["volume_colors"]["c3"],
                muted=(audio_state["volume_muted"] == "да"),
                mute_color=enc["mute_color"], warning_color=enc["warning_color"],
                warning_threshold_pct=enc["warning_threshold_pct"],
                leds_per_bar=leds_count,
            )
            bar_state = {"mode": "volume_osd", "pixels": pixels,
                         "pct_bottom": audio_state["volume_pct"], "pct_top": audio_state["volume_pct"],
                         "osd_active": True}
        else:
            osd_active = False
            bar_mode = cfg["mode"]["bar0"]
            peak_info = cfg["peak"]["bar0"]
            peak_enabled = peak_info["enabled"]

            peak_trackers["bottom"].set_style(peak_info["style"])
            peak_trackers["bottom"].set_timings(cfg["peak_hold_seconds"], cfg["peak_fade_seconds"])

            pct_bottom = round(common_metrics.get(cfg["assignment"]["bar0"], 0))
            bottom_peak = peak_trackers["bottom"].update(pct_bottom, now)

            if bar_mode == "center":
                peak_trackers["top"].set_style(peak_info["style"])
                peak_trackers["top"].set_timings(cfg["peak_hold_seconds"], cfg["peak_fade_seconds"])

                pct_top = round(common_metrics.get(cfg["assignment_top"]["bar0"], 0))
                top_peak = peak_trackers["top"].update(pct_top, now)

                pixels = ledbar.compute_bar_pixels_center(
                    pct_bottom, pct_top,
                    cfg["colors"]["bar0"]["c1"], cfg["colors"]["bar0"]["c2"], cfg["colors"]["bar0"]["c3"], cfg["solid"]["bar0"],
                    cfg["colors_top"]["bar0"]["c1"], cfg["colors_top"]["bar0"]["c2"], cfg["colors_top"]["bar0"]["c3"], cfg["solid_top"]["bar0"],
                    leds_per_bar=leds_count,
                    peak_pct_bottom=bottom_peak if peak_enabled else None,
                    peak_pct_top=top_peak if peak_enabled else None,
                )
                bar_state = {"mode": "center", "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": pct_top, "osd_active": False}
            else:
                pixels = ledbar.compute_bar_pixels(
                    pct_bottom, cfg["colors"]["bar0"]["c1"], cfg["colors"]["bar0"]["c2"], cfg["colors"]["bar0"]["c3"], cfg["solid"]["bar0"],
                    leds_per_bar=leds_count,
                    peak_pct=bottom_peak if peak_enabled else None,
                )
                bar_state = {"mode": "classic", "pixels": pixels, "pct_bottom": pct_bottom, "pct_top": None, "osd_active": False}

        with state_lock:
            state["bar"] = bar_state

        # ---- собрать и отправить serial-строку ----
        proto_values = {
            "BAR": protocol.pack_bar_pixels(pixels),
            "BRI": str(cfg["brightness"]),
            "CON": str(cfg["contrast"]),
            "L1": lines[0], "L2": lines[1], "L3": lines[2],
        }
        line_to_send = proto.build(proto_values, now=now)

        if ser is not None and not flashing_event.is_set() and line_to_send is not None:
            try:
                ser.write((line_to_send + "\n").encode("utf-8"))
            except (serial.SerialException, OSError):
                print("[serial] write failed, will reconnect", flush=True)
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                with state_lock:
                    state["serial_connected"] = False
                last_reconnect_attempt = now

        elapsed = time.time() - loop_t0
        # cfg["tick_interval"] - живая настройка из /settings (см.
        # /api/tick_interval и DEFAULT_SETTINGS выше), а не статическая
        # TICK_INTERVAL - cfg уже перечитывается из state в начале КАЖДОЙ
        # итерации цикла (см. "cfg = copy.deepcopy(state[\"cfg\"])" в самом
        # начале while), поэтому смена значения в /settings подхватывается
        # на следующем же тике, без перезапуска pc_hud.py. TICK_INTERVAL
        # (env var) используется только как дефолт при самом первом запуске
        # (см. DEFAULT_SETTINGS) - .get() тут на случай уже сохранённого
        # settings.json от версии ДО этой настройки (там ключа ещё нет).
        tick_interval = cfg.get("tick_interval", TICK_INTERVAL)
        stop_event.wait(timeout=max(0.0, tick_interval - elapsed))

    if ser is not None:
        try:
            ser.close()
        except Exception:
            pass
    print("[win-hud-arduino] metrics loop stopped", flush=True)


def _boot_time():
    import psutil
    return psutil.boot_time()


# ---------------- трей-иконка ----------------

def build_tray_icon(stop_event):
    import pystray
    from PIL import Image

    image = Image.open(ICON_PATH)

    def on_open(icon, item):
        webbrowser.open(f"http://127.0.0.1:{WEB_PORT}/")

    def on_quit(icon, item):
        stop_event.set()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть панель", on_open, default=True),
        pystray.MenuItem("Выход", on_quit),
    )
    return pystray.Icon("win-hud-arduino", image, "win-hud-arduino", menu)


def main():
    print(f"[win-hud-arduino] starting, version {SCRIPT_VERSION}", flush=True)
    _ensure_assets()

    stop_event = threading.Event()
    threading.Thread(target=run_web, daemon=True).start()
    threading.Thread(target=metrics_main_loop, args=(stop_event,), daemon=True).start()

    icon = build_tray_icon(stop_event)
    icon.run()  # блокирует главный поток, пока не нажмут "Выход"

    stop_event.set()
    time.sleep(0.3)
    gpu_monitor.shutdown()
    print("[win-hud-arduino] stopped", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
