"""
flash_webui.py  (win-hud-arduino)

Страница /flash - загрузка .hex прошивки и её заливка на Arduino Pro Micro
прямо из процесса win-hud-arduino, с построчным логом avrdude, который
стримится в браузер по мере поступления. Портировано из shkaf-hud без
изменений логики - весь код здесь работает через flash.py (уже адаптирован
под COM-порты Windows), сам этот файл ничего платформозависимого не делает.

Подключается к уже существующему Flask-приложению вызовом:

    register_flash_routes(app, serial_port, flashing_event)

serial_port     - COM-порт платы: строка ИЛИ функция-геттер без аргументов
                   (см. подробности в register_flash_routes ниже) - тот же
                   порт, что использует главный цикл pc_hud.py.
flashing_event  - threading.Event, общий с главным циклом: пока установлен,
                   главный цикл не открывает/не пишет в serial-порт платы
                   (см. pc_hud.py) - чтобы два процесса не дрались за один и
                   тот же порт одновременно. То же самое, что нужно было бы
                   и для serial-читающего потока энкодера (ENC:/BTN:) -
                   он тоже должен уважать flashing_event, см. pc_hud.py.

Что изменилось относительно shkaf-hud: только брендинг и иконки (локальные
/favicon.png, /icon.png - см. пояснение в screens_webui.py).
"""

import os
import threading

from flask import request, jsonify, Response

import flash

HEX_UPLOAD_PATH = os.path.join(os.environ.get("TEMP", "/tmp"), "win-hud-arduino-flash.hex")

_flash_lock = threading.Lock()  # не даёт запустить вторую прошивку параллельно
_current_cancel_event = None  # cancel_event активной прошивки - None, если сейчас ничего не льётся
_cancel_lock = threading.Lock()  # защищает _current_cancel_event от гонки между /api/flash и /api/flash/cancel


FLASH_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>win-hud-arduino - прошивка</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<link rel="icon" type="image/png" href="/favicon.png">
<style>
  * { box-sizing: border-box; }
  :root {
    --bg: #17181a; --panel: #1f2123; --border: #2c2e31;
    --text: #e6e6e6; --muted: #8a8d91; --accent: #ff8c2f; --danger: #e0483e; --ok: #3ecf6e;
  }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
         margin:0; padding:24px 16px 60px; }
  .wrap { max-width:640px; margin:0 auto; }
  .brand { display:flex; align-items:center; gap:10px; margin-bottom:4px; }
  .brand .dot { width:9px; height:9px; border-radius:50%; background:var(--accent); box-shadow:0 0 8px var(--accent); }
  h1 { font-size:19px; font-weight:600; margin:0; }
  .nav { display:flex; gap:16px; margin:14px 0 24px; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:22px; margin-bottom:18px; }
  .card h2 { font-size:11px; color:var(--muted); margin:0 0 16px; font-weight:600; }

  .drop { border:1px dashed var(--border); border-radius:10px; padding:24px; text-align:center;
          color:var(--muted); font-size:13px; cursor:pointer; }
  .drop.hasfile { color:var(--text); border-color:var(--accent); }
  input[type=file] { display:none; }

  .btn { border:none; border-radius:8px; padding:11px 20px; font-size:14px; font-weight:600;
         cursor:pointer; margin-top:16px; width:100%; }
  .btn.primary { background:var(--accent); color:#151515; }
  .btn.primary:disabled { background:#4a3a28; color:#8a8d91; cursor:not-allowed; }
  .btn.danger { background:#3a1f1c; color:#ffb3ab; border:1px solid var(--danger); }

  .status { font-size:13px; margin-top:12px; min-height:18px; }
  .status.ok { color:var(--ok); }
  .status.err { color:var(--danger); }

  .log { background:#101112; border:1px solid var(--border); border-radius:8px; padding:12px;
         font-family:monospace; font-size:12px; color:#9fd3a0; white-space:pre-wrap;
         word-break:break-all; max-height:340px; overflow-y:auto; margin-top:16px; display:none; }

  .warn { font-size:12px; color:var(--muted); margin-top:10px; line-height:1.5; }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/">Sensors</a><a href="/settings">Settings</a><a href="/screens">OLED screens</a><a href="/flash" class="active">Flash</a></div>

  <div class="card">
    <h2>ПРОШИВКА ARDUINO</h2>

    <div class="drop" id="drop">Выбери .hex файл (или перетащи сюда)</div>
    <input type="file" id="file-input" accept=".hex">

    <button class="btn primary" id="flash-btn" disabled>Прошить</button>
    <button class="btn danger" id="cancel-btn" style="display:none">Отменить прошивку</button>

    <div class="status" id="status"></div>
    <div class="log" id="log"></div>

    <div class="warn">
      Во время прошивки основной обмен с платой (лента/OLED/энкодер) приостанавливается
      и возобновится автоматически после перезагрузки платы. Не отключай Pro Micro
      и не закрывай win-hud-arduino, пока идёт заливка.
    </div>
  </div>

  <footer>win-hud-arduino</footer>
</div>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('file-input');
const flashBtn = document.getElementById('flash-btn');
const cancelBtn = document.getElementById('cancel-btn');
const statusEl = document.getElementById('status');
const logEl = document.getElementById('log');

cancelBtn.addEventListener('click', () => {
  cancelBtn.disabled = true;
  fetch('/api/flash/cancel', { method: 'POST' });
});

let selectedFile = null;

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('dragover', e => { e.preventDefault(); });
drop.addEventListener('drop', e => {
  e.preventDefault();
  if (e.dataTransfer.files.length) setFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) setFile(fileInput.files[0]);
});

function setFile(f) {
  selectedFile = f;
  drop.textContent = f.name;
  drop.classList.add('hasfile');
  flashBtn.disabled = false;
}

flashBtn.addEventListener('click', async () => {
  if (!selectedFile) return;
  flashBtn.disabled = true;
  cancelBtn.style.display = 'block';
  cancelBtn.disabled = false;
  statusEl.textContent = 'Загружаю и прошиваю...';
  statusEl.className = 'status';
  logEl.style.display = 'block';
  logEl.textContent = '';

  const formData = new FormData();
  formData.append('hex', selectedFile);

  try {
    const resp = await fetch('/api/flash', { method: 'POST', body: formData });
    if (resp.status === 409) {
      statusEl.textContent = 'Прошивка уже идёт - подожди её завершения.';
      statusEl.className = 'status err';
      flashBtn.disabled = false;
      cancelBtn.style.display = 'none';
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let done = false;
    let lastLine = '';

    while (!done) {
      const result = await reader.read();
      done = result.done;
      if (result.value) {
        const chunk = decoder.decode(result.value, { stream: true });
        logEl.textContent += chunk;
        logEl.scrollTop = logEl.scrollHeight;
        const lines = chunk.split('\\n').filter(Boolean);
        if (lines.length) lastLine = lines[lines.length - 1];
      }
    }

    if (lastLine.trim() === 'OK') {
      statusEl.textContent = 'Готово - прошивка залита.';
      statusEl.className = 'status ok';
    } else {
      statusEl.textContent = 'Ошибка прошивки - смотри лог выше.';
      statusEl.className = 'status err';
    }
  } catch (e) {
    statusEl.textContent = 'Сетевая ошибка: ' + e;
    statusEl.className = 'status err';
  }

  flashBtn.disabled = false;
  cancelBtn.style.display = 'none';
});
</script>
</body></html>
"""


def register_flash_routes(app, serial_port, flashing_event):
    """
    serial_port - COM-порт платы. Может быть:
      - строкой (фиксированный порт, как в shkaf-hud)
      - функцией без аргументов, возвращающей АКТУАЛЬНЫЙ порт из settings.json
        на момент вызова - нужно, т.к. в win-hud-arduino порт настраивается
        в вебе (/api/serial_port, см. pc_hud.py) и может поменяться уже
        после старта процесса, в отличие от shkaf-hud, где SERIAL_PORT
        читался один раз из переменной окружения при запуске контейнера.
    """

    def _resolve_port():
        return serial_port() if callable(serial_port) else serial_port

    @app.route("/flash")
    def flash_page():
        return Response(FLASH_PAGE_HTML, mimetype="text/html")

    @app.route("/api/flash", methods=["POST"])
    def api_flash():
        if "hex" not in request.files:
            return jsonify({"error": "файл не передан"}), 400

        uploaded = request.files["hex"]
        uploaded.save(HEX_UPLOAD_PATH)

        if not _flash_lock.acquire(blocking=False):
            return jsonify({"error": "прошивка уже идёт"}), 409

        cancel_event = threading.Event()
        global _current_cancel_event
        with _cancel_lock:
            _current_cancel_event = cancel_event

        port = _resolve_port()

        def generate():
            flashing_event.set()
            try:
                for line in flash.flash(HEX_UPLOAD_PATH, port, cancel_event=cancel_event):
                    yield line + "\n"
            except flash.FlashError as e:
                yield f"ОШИБКА: {e}\n"
            except Exception as e:
                yield f"НЕОЖИДАННАЯ ОШИБКА: {e}\n"
            finally:
                flashing_event.clear()
                with _cancel_lock:
                    global _current_cancel_event
                    _current_cancel_event = None
                _flash_lock.release()
                try:
                    os.remove(HEX_UPLOAD_PATH)
                except OSError:
                    pass

        return Response(generate(), mimetype="text/plain")

    @app.route("/api/flash/cancel", methods=["POST"])
    def api_flash_cancel():
        with _cancel_lock:
            if _current_cancel_event is None:
                return jsonify({"error": "сейчас ничего не льётся"}), 409
            _current_cancel_event.set()
        return jsonify({"ok": True})
