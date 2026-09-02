"""
screens_webui.py  (win-hud-arduino)

Отдельная страница /screens - редактор OLED-экранов. Портировано из
shkaf-hud практически без изменений логики: движок (CRUD/preview/legend)
работает через variables.py/templates.py/screens.py, которые уже полностью
адаптированы под win-hud-arduino - самому этому файлу конкретные переменные
проекта не важны.

Что изменилось относительно shkaf-hud:
  - брендинг (заголовок, favicon)
  - иконки - ЛОКАЛЬНЫЕ пути (/favicon.png, /icon.png), а не жёсткие ссылки
    на raw.githubusercontent.com/RGCustom/shkaf-hud. win-hud-arduino - не
    докер-образ из публичного репозитория, а локальное Windows-приложение,
    поэтому иконки разумнее раздавать самим процессом (см. pc_hud.py -
    там нужно будет добавить роуты /favicon.png и /icon.png, отдающие файлы
    из папки assets/ рядом со скриптом - те же PNG, что использует и
    трей-иконка pystray)
  - порядок категорий легенды - под variables.CATEGORY_ORDER этого проекта
    (Система/GPU/Диски/Сеть/Аудио/Медиа, вместо Система/Диски.../Media/qBittorrent)

get_context - функция без аргументов, возвращающая текущий context (тот же
словарь, что build_active_screens ожидает) - нужна для живого превью при
редактировании шаблона. Подключается вызовом register_screens_routes(app, get_context).
"""

import threading

from flask import request, jsonify, Response

import screens
import templates
import variables

_lock = threading.Lock()
_screens = screens.load_screens()


SCREENS_PAGE_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>win-hud-arduino - экраны</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#ff8c2f">
<link rel="icon" type="image/png" href="/favicon.png">
<link rel="apple-touch-icon" href="/icon.png">
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
  .nav { display:flex; gap:16px; margin:14px 0 24px; }
  .nav a { color:var(--muted); text-decoration:none; font-size:13px; padding:6px 0; border-bottom:2px solid transparent; }
  .nav a.active { color:var(--text); border-bottom-color:var(--accent); }

  .card { background:var(--panel); border:1px solid var(--border); border-radius:14px;
          padding:18px; margin-bottom:16px; }

  .screen-row { display:flex; align-items:center; gap:10px; padding:10px 8px; border-radius:8px;
                border:1px solid var(--border); margin-bottom:8px; background:#191a1c; cursor:grab; }
  .screen-row.dragging { opacity:.4; }
  .screen-row .handle { color:var(--muted); font-size:16px; }
  .screen-row .info { flex:1; min-width:0; }
  .screen-row .name { font-size:14px; }
  .screen-row .preview { font-size:11px; color:var(--muted); font-family:monospace; white-space:nowrap;
                          overflow:hidden; text-overflow:ellipsis; }
  .screen-row input[type=checkbox] { width:16px; height:16px; }
  .screen-row .dur { width:52px; background:#101112; color:var(--text); border:1px solid var(--border);
                      border-radius:6px; padding:4px; font-size:12px; text-align:center; }
  .screen-row button { background:none; border:none; color:var(--muted); cursor:pointer; font-size:15px; padding:4px 6px; }
  .screen-row button:hover { color:var(--text); }

  #add-btn { background:var(--accent); color:#151515; border:none; border-radius:8px;
             padding:10px 18px; font-weight:600; cursor:pointer; font-size:14px; margin-top:6px; }

  .modal-bg { display:none; position:fixed; inset:0; background:rgba(0,0,0,.6); align-items:center;
              justify-content:center; z-index:10; padding:16px; }
  .modal-bg.show { display:flex; }
  .modal { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:22px;
           width:100%; max-width:520px; max-height:90vh; overflow-y:auto; }
  .modal h2 { font-size:16px; margin:0 0 16px; }
  .modal label { font-size:12px; color:var(--muted); display:block; margin:12px 0 4px; }
  .modal input[type=text], .modal input[type=number] {
    width:100%; background:#101112; color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:8px 10px; font-size:14px; font-family:monospace;
  }
  .line-preview { font-size:12px; color:var(--accent); font-family:monospace; margin-top:4px; min-height:16px; }
  .line-preview.err { color:var(--danger); }
  .modal-actions { display:flex; justify-content:space-between; gap:10px; margin-top:20px; }
  .modal-actions .left { display:flex; gap:10px; }
  .btn { border:none; border-radius:6px; padding:8px 16px; font-size:13px; cursor:pointer; }
  .btn.primary { background:var(--accent); color:#151515; font-weight:600; }
  .btn.secondary { background:#101112; color:var(--text); border:1px solid var(--border); }
  .btn.danger { background:#3a1f1c; color:#ffb3ab; border:1px solid var(--danger); }

  .legend { margin-top:16px; }
  .legend-group { margin-bottom:14px; }
  .legend-group .gtitle { font-size:11px; color:var(--muted); text-transform:uppercase; margin-bottom:6px;
                           letter-spacing:.02em; }
  .legend-item { display:inline-block; background:#101112; border:1px solid var(--border); border-radius:5px;
                 padding:3px 7px; margin:2px 3px 2px 0; font-size:11px; font-family:monospace; cursor:pointer;
                 color:var(--text); }
  .legend-item:hover { border-color:var(--accent); color:var(--accent); }

  footer { text-align:center; color:var(--border); font-size:11px; margin-top:20px; }
</style></head>
<body>
<div class="wrap">
  <div class="brand"><span class="dot"></span><h1>win-hud-arduino</h1></div>
  <div class="nav"><a href="/">Sensors</a><a href="/settings">Settings</a><a href="/screens" class="active">OLED screens</a><a href="/flash">Flash</a></div>

  <div class="card">
    <div id="screen-list"></div>
    <button id="add-btn">+ Добавить экран</button>
  </div>

  <footer>win-hud-arduino</footer>
</div>

<div class="modal-bg" id="modal-bg">
  <div class="modal">
    <h2 id="modal-title">Экран</h2>
    <input type="hidden" id="edit-id">

    <label>Название (только для интерфейса)</label>
    <input type="text" id="edit-name" placeholder="CPU/RAM">

    <label>Время показа, сек</label>
    <input type="number" id="edit-duration" min="1" step="0.5" value="4">

    <label>Строка 1</label>
    <input type="text" id="edit-l1" placeholder="CPU {cpu_pct}%">
    <div class="line-preview" id="preview-l1"></div>

    <label>Строка 2</label>
    <input type="text" id="edit-l2" placeholder="GPU {gpu_pct}%">
    <div class="line-preview" id="preview-l2"></div>

    <label>Строка 3</label>
    <input type="text" id="edit-l3" placeholder="{time_now}">
    <div class="line-preview" id="preview-l3"></div>

    <div class="legend" id="legend"></div>

    <div class="modal-actions">
      <div class="left">
        <button class="btn primary" id="save-btn">Сохранить</button>
        <button class="btn secondary" id="cancel-btn">Отмена</button>
      </div>
      <button class="btn danger" id="delete-btn" style="display:none">Удалить</button>
    </div>
  </div>
</div>

<script>
let screensCache = [];
let draggedId = null;
let focusedField = null;

document.querySelectorAll('#edit-l1,#edit-l2,#edit-l3').forEach(el => {
  el.addEventListener('focus', () => focusedField = el);
});

function loadScreens() {
  fetch('/api/screens').then(r => r.json()).then(data => {
    screensCache = data;
    renderList();
  });
}

function renderList() {
  const list = document.getElementById('screen-list');
  list.innerHTML = '';
  screensCache.forEach(s => {
    const row = document.createElement('div');
    row.className = 'screen-row';
    row.draggable = true;
    row.dataset.id = s.id;

    row.addEventListener('dragstart', () => { draggedId = s.id; row.classList.add('dragging'); });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));
    row.addEventListener('dragover', e => e.preventDefault());
    row.addEventListener('drop', e => {
      e.preventDefault();
      if (!draggedId || draggedId === s.id) return;
      const fromIdx = screensCache.findIndex(x => x.id === draggedId);
      const toIdx = screensCache.findIndex(x => x.id === s.id);
      const [moved] = screensCache.splice(fromIdx, 1);
      screensCache.splice(toIdx, 0, moved);
      renderList();
      saveOrder();
    });

    const handle = document.createElement('div');
    handle.className = 'handle';
    handle.textContent = '\u2261';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = s.enabled;
    cb.addEventListener('change', () => {
      fetch('/api/screens/' + s.id, { method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ enabled: cb.checked }) });
    });

    const info = document.createElement('div');
    info.className = 'info';
    const name = document.createElement('div');
    name.className = 'name';
    name.textContent = s.name;
    const preview = document.createElement('div');
    preview.className = 'preview';
    preview.textContent = [s.l1, s.l2, s.l3].filter(Boolean).join('  |  ');
    info.appendChild(name);
    info.appendChild(preview);

    const dur = document.createElement('input');
    dur.type = 'number';
    dur.className = 'dur';
    dur.min = 1; dur.step = 0.5;
    dur.value = s.duration;
    dur.addEventListener('change', () => {
      fetch('/api/screens/' + s.id, { method: 'PUT', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ duration: parseFloat(dur.value) || 4 }) });
    });

    const editBtn = document.createElement('button');
    editBtn.textContent = '\u270e';
    editBtn.addEventListener('click', () => openModal(s));

    row.appendChild(handle);
    row.appendChild(cb);
    row.appendChild(info);
    row.appendChild(dur);
    row.appendChild(editBtn);
    list.appendChild(row);
  });
}

function saveOrder() {
  fetch('/api/screens/reorder', { method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ order: screensCache.map(s => s.id) }) });
}

function buildLegend() {
  fetch('/api/variables').then(r => r.json()).then(vars => {
    const categories = {};
    vars.forEach(v => { (categories[v.category] = categories[v.category] || []).push(v); });

    // Порядок категорий - как в variables.CATEGORY_ORDER этого проекта
    // (Система/GPU/Диски/Сеть/Аудио/Медиа); всё, чего там почему-то нет - в конец.
    const order = ['Система', 'GPU', 'Диски', 'Сеть', 'Аудио', 'Медиа'];
    const orderedCats = [...order.filter(c => categories[c]), ...Object.keys(categories).filter(c => !order.includes(c))];

    const legend = document.getElementById('legend');
    legend.innerHTML = '';
    orderedCats.forEach(cat => {
      const items = categories[cat];
      const div = document.createElement('div');
      div.className = 'legend-group';
      const title = document.createElement('div');
      title.className = 'gtitle';
      title.textContent = cat;
      div.appendChild(title);
      items.forEach(v => {
        const span = document.createElement('span');
        // В win-hud-arduino повторяющихся переменных нет (variables.REPEATING_GROUPS
        // пуст) - бейдж repeating из shkaf-hud тут не нужен, но проверка на
        // v.repeating оставлена ради совместимости, если он появится позже.
        span.className = 'legend-item' + (v.repeating ? ' repeating' : '');
        span.textContent = '{' + v.name + '}';
        span.title = v.label;
        span.addEventListener('click', () => {
          const field = focusedField || document.getElementById('edit-l1');
          const pos = field.selectionStart || field.value.length;
          field.value = field.value.slice(0, pos) + '{' + v.name + '}' + field.value.slice(pos);
          field.dispatchEvent(new Event('input'));
          field.focus();
        });
        div.appendChild(span);
      });
      legend.appendChild(div);
    });
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

['l1','l2','l3'].forEach(k => {
  const input = document.getElementById('edit-' + k);
  const preview = document.getElementById('preview-' + k);
  input.addEventListener('input', () => livePreview(input, preview));
});

function openModal(s) {
  document.getElementById('modal-title').textContent = s ? 'Редактировать экран' : 'Новый экран';
  document.getElementById('edit-id').value = s ? s.id : '';
  document.getElementById('edit-name').value = s ? s.name : '';
  document.getElementById('edit-duration').value = s ? s.duration : 4;
  document.getElementById('edit-l1').value = s ? s.l1 : '';
  document.getElementById('edit-l2').value = s ? s.l2 : '';
  document.getElementById('edit-l3').value = s ? s.l3 : '';
  document.getElementById('delete-btn').style.display = s ? 'inline-block' : 'none';
  ['l1','l2','l3'].forEach(k => livePreview(document.getElementById('edit-'+k), document.getElementById('preview-'+k)));
  document.getElementById('modal-bg').classList.add('show');
}

function closeModal() {
  document.getElementById('modal-bg').classList.remove('show');
}

document.getElementById('add-btn').addEventListener('click', () => openModal(null));
document.getElementById('cancel-btn').addEventListener('click', closeModal);

document.getElementById('save-btn').addEventListener('click', () => {
  const id = document.getElementById('edit-id').value;
  const body = {
    name: document.getElementById('edit-name').value || 'Screen',
    l1: document.getElementById('edit-l1').value,
    l2: document.getElementById('edit-l2').value,
    l3: document.getElementById('edit-l3').value,
    duration: parseFloat(document.getElementById('edit-duration').value) || 4,
  };
  const req = id
    ? fetch('/api/screens/' + id, { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
    : fetch('/api/screens', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  req.then(() => { closeModal(); loadScreens(); });
});

document.getElementById('delete-btn').addEventListener('click', () => {
  const id = document.getElementById('edit-id').value;
  if (!confirm('Удалить экран?')) return;
  fetch('/api/screens/' + id, { method: 'DELETE' }).then(() => { closeModal(); loadScreens(); });
});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

buildLegend();
loadScreens();
</script>
</body></html>
"""

MANIFEST_JSON = {
    "name": "win-hud-arduino",
    "short_name": "win-hud-arduino",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#17181a",
    "theme_color": "#ff8c2f",
    "icons": [
        {
            "src": "/icon.png",
            "sizes": "512x512",
            "type": "image/png",
        }
    ],
}

SW_JS = """
// win-hud-arduino service worker - минимальный, только для установки как PWA.
// Данные всегда живые (network-first), офлайн-кэш тут не имеет особого смысла.
self.addEventListener('install', e => self.skipWaiting());
self.addEventListener('activate', e => self.clients.claim());
self.addEventListener('fetch', e => {
  e.respondWith(fetch(e.request).catch(() => new Response('offline', {status: 503})));
});
"""


def register_screens_routes(app, get_context):
    import json as _json

    @app.route("/screens")
    def screens_page():
        return Response(SCREENS_PAGE_HTML, mimetype="text/html")

    @app.route("/manifest.json")
    def manifest():
        return Response(_json.dumps(MANIFEST_JSON), mimetype="application/manifest+json")

    @app.route("/sw.js")
    def service_worker():
        return Response(SW_JS, mimetype="application/javascript")

    @app.route("/api/variables")
    def api_variables():
        return jsonify(variables.legend())

    @app.route("/api/preview", methods=["POST"])
    def api_preview():
        body = request.get_json(force=True)
        tpl = body.get("template", "")
        unknown = templates.validate_template(tpl)
        if unknown:
            return jsonify({"rendered": "", "all_resolved": False, "unknown_vars": unknown})
        rendered, ok = templates.render(tpl, get_context(), index=0)
        return jsonify({"rendered": rendered, "all_resolved": ok, "unknown_vars": []})

    @app.route("/api/screens", methods=["GET"])
    def api_screens_list():
        with _lock:
            return jsonify(_screens)

    @app.route("/api/screens", methods=["POST"])
    def api_screens_create():
        body = request.get_json(force=True)
        with _lock:
            new_list, screen = screens.create_screen(_screens, body)
            _screens[:] = new_list
            screens.save_screens(_screens)
            return jsonify(screen)

    @app.route("/api/screens/<screen_id>", methods=["PUT"])
    def api_screens_update(screen_id):
        body = request.get_json(force=True)
        with _lock:
            new_list, screen = screens.update_screen(_screens, screen_id, body)
            _screens[:] = new_list
            screens.save_screens(_screens)
            if screen is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(screen)

    @app.route("/api/screens/<screen_id>", methods=["DELETE"])
    def api_screens_delete(screen_id):
        with _lock:
            _screens[:] = screens.delete_screen(_screens, screen_id)
            screens.save_screens(_screens)
            return jsonify({"ok": True})

    @app.route("/api/screens/reorder", methods=["POST"])
    def api_screens_reorder():
        body = request.get_json(force=True)
        order = body.get("order", [])
        with _lock:
            _screens[:] = screens.reorder_screens(_screens, order)
            screens.save_screens(_screens)
            return jsonify({"ok": True})


def get_screens():
    """Для главного цикла - актуальный список экранов (с учётом правок из веб-интерфейса)."""
    with _lock:
        return list(_screens)
