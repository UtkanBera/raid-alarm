import os
import sys
import asyncio
import threading
import logging
import json
from datetime import datetime
from flask import Flask, jsonify, request, render_template_string
from twilio.rest import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Config file — Railway'de env var yoksa local config'den okur
# ---------------------------------------------------------------------------
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config():
    """Load config from file if it exists."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(data):
    """Save config to file."""
    existing = load_config()
    existing.update(data)
    with open(CONFIG_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    return existing


def get_config_value(key, default=None):
    """Get config value: env var first, then config file, then default."""
    val = os.environ.get(key)
    if val:
        return val
    cfg = load_config()
    return cfg.get(key, default)


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_state_lock = threading.Lock()

state = {
    "running": False,
    "connected": False,
    "alarm_count": 0,
    "last_alarm": None,
    "error": None,
    "log": [],
    # Config status
    "server_ip": get_config_value("RUSTPLUS_SERVER_IP"),
    "server_port": get_config_value("RUSTPLUS_SERVER_PORT"),
    "steam_id": get_config_value("RUSTPLUS_STEAM_ID"),
    "player_token": get_config_value("RUSTPLUS_PLAYER_TOKEN"),
    "entity_id": get_config_value("RUSTPLUS_ENTITY_ID"),
}

_rust_thread = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def add_log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    with _state_lock:
        state["log"].insert(0, {"time": ts, "msg": msg, "level": level})
        state["log"] = state["log"][:100]
    if level == "error":
        logging.error(msg)
    else:
        logging.info(msg)


def is_configured():
    """Check if all required Rust+ credentials are present."""
    return all([
        state.get("server_ip"),
        state.get("server_port"),
        state.get("steam_id"),
        state.get("player_token"),
        state.get("entity_id"),
    ])


def make_call():
    try:
        sid = get_config_value("TWILIO_SID")
        token = get_config_value("TWILIO_TOKEN")
        from_num = get_config_value("TWILIO_FROM")
        to_num = get_config_value("TWILIO_TO")

        if not all([sid, token, from_num, to_num]):
            add_log("Twilio credentials eksik!", "error")
            return

        client = Client(sid, token)
        twiml = (
            "<Response>"
            "<Say language='tr-TR' voice='alice'>"
            "Dikkat! Rust oyununda raid alarmi! Sismik sensor tetiklendi!"
            "</Say>"
            "<Pause length='1'/>"
            "<Say language='tr-TR' voice='alice'>"
            "Dikkat! Raid alarmi! Dikkat! Raid alarmi!"
            "</Say>"
            "</Response>"
        )
        call = client.calls.create(
            twiml=twiml,
            to=to_num,
            from_=from_num,
        )
        add_log(f"Arama baslatildi: {call.sid}")
    except Exception as exc:
        add_log(f"Twilio hatasi: {exc}", "error")


# ---------------------------------------------------------------------------
# Rust+ listener
# ---------------------------------------------------------------------------

async def rust_listener():
    from rustplus import RustSocket, EntityEvent

    ip = state["server_ip"]
    port = int(state["server_port"])
    steam_id = int(state["steam_id"])
    player_token = int(state["player_token"])
    entity_id = int(state["entity_id"])

    while state["running"]:
        socket = None
        try:
            socket = RustSocket(ip, port, steam_id, player_token)

            @socket.on_entity_event(entity_id)
            async def on_alarm(event: EntityEvent):
                if event.value:
                    with _state_lock:
                        state["alarm_count"] += 1
                        state["last_alarm"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    add_log("RAID ALARMI! Sismik sensor tetiklendi!", "alarm")
                    threading.Thread(target=make_call, daemon=True).start()
                else:
                    add_log("Sismik sensor sakinlesti.")

            await socket.connect()
            with _state_lock:
                state["connected"] = True
                state["error"] = None
            add_log("Rust+ sunucusuna baglandi.")

            while state["running"]:
                await asyncio.sleep(0.5)

        except Exception as exc:
            with _state_lock:
                state["connected"] = False
                state["error"] = str(exc)
            add_log(f"Baglanti hatasi: {exc}", "error")
            if state["running"]:
                add_log("5 saniye sonra yeniden baglaniliyor...")
                await asyncio.sleep(5)
        finally:
            with _state_lock:
                state["connected"] = False
            if socket:
                try:
                    await socket.disconnect()
                except Exception:
                    pass

    add_log("Listener durduruldu.")


def _run_rust_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(rust_listener())
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/start", methods=["POST"])
def api_start():
    global _rust_thread
    if not is_configured():
        return jsonify({"error": "Eksik ayarlar! Once tum bilgileri girin."}), 400
    if not state["running"]:
        with _state_lock:
            state["running"] = True
            state["alarm_count"] = 0
            state["error"] = None
        add_log("Alarm sistemi baslatildi.")
        _rust_thread = threading.Thread(target=_run_rust_loop, daemon=True)
        _rust_thread.start()
    return jsonify(_safe_state())


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with _state_lock:
        state["running"] = False
    add_log("Alarm sistemi durduruluyor...")
    return jsonify(_safe_state())


@app.route("/api/status")
def api_status():
    return jsonify(_safe_state())


@app.route("/api/test-call", methods=["POST"])
def api_test_call():
    threading.Thread(target=make_call, daemon=True).start()
    add_log("Test aramasi yapiliyor...")
    return jsonify({"ok": True})


@app.route("/api/save-config", methods=["POST"])
def api_save_config():
    """Save Rust+ connection credentials."""
    data = request.get_json(force=True)

    server_ip = data.get("server_ip", "").strip()
    server_port = data.get("server_port", "").strip()
    steam_id = data.get("steam_id", "").strip()
    player_token = data.get("player_token", "").strip()
    entity_id = data.get("entity_id", "").strip()

    if not all([server_ip, server_port, steam_id, player_token, entity_id]):
        return jsonify({"error": "Tum alanlar doldurulmali!"}), 400

    # Validate numeric fields
    try:
        int(server_port)
        int(steam_id)
        int(player_token)
        int(entity_id)
    except ValueError:
        return jsonify({"error": "Port, Steam ID, Player Token ve Entity ID sayisal olmali!"}), 400

    # Save to config file
    save_config({
        "RUSTPLUS_SERVER_IP": server_ip,
        "RUSTPLUS_SERVER_PORT": server_port,
        "RUSTPLUS_STEAM_ID": steam_id,
        "RUSTPLUS_PLAYER_TOKEN": player_token,
        "RUSTPLUS_ENTITY_ID": entity_id,
    })

    # Update state
    with _state_lock:
        state["server_ip"] = server_ip
        state["server_port"] = server_port
        state["steam_id"] = steam_id
        state["player_token"] = player_token
        state["entity_id"] = entity_id

    add_log("Baglanti bilgileri kaydedildi!")
    return jsonify(_safe_state())


@app.route("/api/save-twilio", methods=["POST"])
def api_save_twilio():
    """Save Twilio credentials."""
    data = request.get_json(force=True)

    twilio_sid = data.get("twilio_sid", "").strip()
    twilio_token = data.get("twilio_token", "").strip()
    twilio_from = data.get("twilio_from", "").strip()
    twilio_to = data.get("twilio_to", "").strip()

    if not all([twilio_sid, twilio_token, twilio_from, twilio_to]):
        return jsonify({"error": "Tum Twilio alanlari doldurulmali!"}), 400

    save_config({
        "TWILIO_SID": twilio_sid,
        "TWILIO_TOKEN": twilio_token,
        "TWILIO_FROM": twilio_from,
        "TWILIO_TO": twilio_to,
    })

    add_log("Twilio bilgileri kaydedildi!")
    return jsonify({"ok": True})


def _safe_state():
    with _state_lock:
        configured = is_configured()
        return {
            "running": state["running"],
            "connected": state["connected"],
            "alarm_count": state["alarm_count"],
            "last_alarm": state["last_alarm"],
            "error": state["error"],
            "configured": configured,
            "server_ip": state.get("server_ip") or "",
            "server_port": state.get("server_port") or "",
            "steam_id": state.get("steam_id") or "",
            "player_token": state.get("player_token") or "",
            "entity_id": state.get("entity_id") or "",
            "has_twilio": all([
                get_config_value("TWILIO_SID"),
                get_config_value("TWILIO_TOKEN"),
                get_config_value("TWILIO_FROM"),
                get_config_value("TWILIO_TO"),
            ]),
            "log": list(state["log"][:30]),
        }


# ---------------------------------------------------------------------------
# HTML — Premium Dark UI
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Raid Alarm — Rust+ Sismik Sensör İzleme</title>
  <meta name="description" content="Rust oyunu için raid alarm sistemi. Sismik sensör tetiklendiğinde telefon araması yapar."/>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #0a0e17;
      --bg-secondary: #111827;
      --bg-card: #1a1f36;
      --bg-card-hover: #222845;
      --border: #2a3050;
      --border-glow: rgba(249,115,22,0.2);
      --text-primary: #f1f5f9;
      --text-secondary: #94a3b8;
      --text-muted: #64748b;
      --accent: #f97316;
      --accent-glow: rgba(249,115,22,0.3);
      --green: #22c55e;
      --green-glow: rgba(34,197,94,0.3);
      --red: #ef4444;
      --red-glow: rgba(239,68,68,0.3);
      --yellow: #eab308;
      --yellow-glow: rgba(234,179,8,0.3);
      --blue: #3b82f6;
      --blue-glow: rgba(59,130,246,0.3);
      --purple: #8b5cf6;
      --purple-glow: rgba(139,92,246,0.3);
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      background: var(--bg-primary);
      color: var(--text-primary);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      min-height: 100vh;
      padding: 16px;
      line-height: 1.5;
    }

    /* Background effect */
    body::before {
      content: '';
      position: fixed;
      top: 0; left: 0;
      width: 100%; height: 100%;
      background: radial-gradient(ellipse at 50% 0%, rgba(249,115,22,0.08) 0%, transparent 60%);
      pointer-events: none;
      z-index: 0;
    }

    .container {
      max-width: 480px;
      margin: 0 auto;
      position: relative;
      z-index: 1;
    }

    /* Header */
    .header {
      text-align: center;
      margin-bottom: 24px;
      padding: 20px 0 16px;
    }
    .header h1 {
      font-size: 1.6rem;
      font-weight: 800;
      background: linear-gradient(135deg, var(--accent), #fb923c);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: 3px;
      text-transform: uppercase;
      margin-bottom: 4px;
    }
    .header .subtitle {
      font-size: 0.75rem;
      color: var(--text-muted);
      letter-spacing: 1px;
    }

    /* Cards */
    .card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      margin-bottom: 16px;
      transition: border-color 0.3s ease;
      backdrop-filter: blur(10px);
    }
    .card:hover {
      border-color: var(--border-glow);
    }
    .card-title {
      font-size: 0.7rem;
      font-weight: 600;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 1.5px;
      margin-bottom: 14px;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    /* Status indicators */
    .status-grid {
      display: grid;
      gap: 12px;
    }
    .status-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .dot {
      width: 10px; height: 10px;
      border-radius: 50%;
      flex-shrink: 0;
      transition: all 0.3s ease;
    }
    .dot.green { background: var(--green); box-shadow: 0 0 10px var(--green-glow); }
    .dot.red { background: var(--red); box-shadow: 0 0 10px var(--red-glow); }
    .dot.yellow { background: var(--yellow); box-shadow: 0 0 10px var(--yellow-glow); }
    .dot.pulse { animation: pulse 2s infinite; }
    @keyframes pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50% { opacity: 0.6; transform: scale(1.2); }
    }
    .status-label { font-size: 0.82rem; color: var(--text-secondary); min-width: 90px; }
    .status-value { font-size: 0.88rem; font-weight: 600; }

    /* Alarm counter */
    .alarm-section { text-align: center; padding: 10px 0; }
    .alarm-count {
      font-size: 3rem;
      font-weight: 800;
      background: linear-gradient(135deg, var(--accent), #fb923c);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      line-height: 1;
    }
    .alarm-label {
      color: var(--text-muted);
      font-size: 0.75rem;
      margin-top: 4px;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .alarm-last {
      color: var(--text-secondary);
      font-size: 0.78rem;
      margin-top: 8px;
    }

    /* Buttons */
    .btn-group { display: grid; gap: 10px; }
    .btn-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }

    .btn {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      padding: 14px 20px;
      border: none;
      border-radius: 12px;
      font-size: 0.92rem;
      font-weight: 700;
      cursor: pointer;
      letter-spacing: 0.5px;
      transition: all 0.2s ease;
      font-family: 'Inter', sans-serif;
      position: relative;
      overflow: hidden;
    }
    .btn:active { transform: scale(0.97); }
    .btn:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }

    .btn-start {
      background: linear-gradient(135deg, #16a34a, #22c55e);
      color: #fff;
      box-shadow: 0 4px 15px var(--green-glow);
    }
    .btn-start:hover:not(:disabled) { box-shadow: 0 6px 25px var(--green-glow); }

    .btn-stop {
      background: linear-gradient(135deg, #dc2626, #ef4444);
      color: #fff;
      box-shadow: 0 4px 15px var(--red-glow);
    }
    .btn-stop:hover:not(:disabled) { box-shadow: 0 6px 25px var(--red-glow); }

    .btn-test {
      background: linear-gradient(135deg, #2563eb, #3b82f6);
      color: #fff;
      font-size: 0.82rem;
      padding: 12px;
      box-shadow: 0 4px 15px var(--blue-glow);
    }

    .btn-config {
      background: linear-gradient(135deg, #7c3aed, #8b5cf6);
      color: #fff;
      font-size: 0.82rem;
      padding: 12px;
      box-shadow: 0 4px 15px var(--purple-glow);
    }

    .btn-save {
      background: linear-gradient(135deg, #16a34a, #22c55e);
      color: #fff;
      box-shadow: 0 4px 15px var(--green-glow);
    }

    /* Config panel */
    .config-panel {
      display: none;
      margin-top: 12px;
      padding-top: 16px;
      border-top: 1px solid var(--border);
    }
    .config-panel.visible { display: block; }

    .form-group {
      margin-bottom: 12px;
    }
    .form-group label {
      display: block;
      font-size: 0.75rem;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 4px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .form-group input {
      width: 100%;
      padding: 10px 14px;
      background: var(--bg-primary);
      border: 1px solid var(--border);
      border-radius: 8px;
      color: var(--text-primary);
      font-size: 0.88rem;
      font-family: 'Inter', sans-serif;
      transition: border-color 0.2s;
      outline: none;
    }
    .form-group input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 2px var(--accent-glow);
    }
    .form-group input::placeholder {
      color: var(--text-muted);
    }
    .form-hint {
      font-size: 0.68rem;
      color: var(--text-muted);
      margin-top: 3px;
    }

    /* Alert box */
    .alert-box {
      padding: 12px 16px;
      border-radius: 10px;
      font-size: 0.82rem;
      margin-bottom: 14px;
      display: none;
    }
    .alert-box.error {
      background: rgba(239,68,68,0.1);
      border: 1px solid rgba(239,68,68,0.3);
      color: #fca5a5;
      display: block;
    }
    .alert-box.success {
      background: rgba(34,197,94,0.1);
      border: 1px solid rgba(34,197,94,0.3);
      color: #86efac;
      display: block;
    }
    .alert-box.info {
      background: rgba(59,130,246,0.1);
      border: 1px solid rgba(59,130,246,0.3);
      color: #93c5fd;
      display: block;
    }

    /* Info banner */
    .info-banner {
      background: rgba(139,92,246,0.08);
      border: 1px solid rgba(139,92,246,0.25);
      border-radius: 12px;
      padding: 14px 16px;
      margin-bottom: 16px;
      font-size: 0.78rem;
      color: var(--text-secondary);
      line-height: 1.6;
    }
    .info-banner strong { color: var(--purple); }
    .info-banner code {
      background: rgba(0,0,0,0.3);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.74rem;
      color: var(--accent);
    }

    /* Log list */
    .log-list {
      list-style: none;
      max-height: 280px;
      overflow-y: auto;
      scrollbar-width: thin;
      scrollbar-color: var(--border) transparent;
    }
    .log-list::-webkit-scrollbar { width: 4px; }
    .log-list::-webkit-scrollbar-track { background: transparent; }
    .log-list::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }

    .log-list li {
      font-size: 0.75rem;
      padding: 6px 0;
      border-bottom: 1px solid rgba(42,48,80,0.5);
      display: flex;
      gap: 10px;
      transition: background 0.2s;
    }
    .log-list li:hover { background: rgba(255,255,255,0.02); }
    .log-time { color: var(--text-muted); flex-shrink: 0; font-family: monospace; font-size: 0.72rem; }
    .log-msg.alarm { color: var(--red); font-weight: 700; }
    .log-msg.error { color: var(--yellow); }
    .log-msg.info { color: var(--text-secondary); }

    /* Tabs */
    .tab-bar {
      display: flex;
      gap: 4px;
      margin-bottom: 14px;
    }
    .tab-btn {
      flex: 1;
      padding: 8px;
      border: 1px solid var(--border);
      background: transparent;
      color: var(--text-muted);
      font-size: 0.72rem;
      font-weight: 600;
      font-family: 'Inter', sans-serif;
      border-radius: 8px;
      cursor: pointer;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      transition: all 0.2s;
    }
    .tab-btn.active {
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }

    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* Responsive */
    @media (min-width: 600px) {
      body { padding: 24px; }
      .container { max-width: 520px; }
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>&#128680; Raid Alarm</h1>
      <div class="subtitle">Rust+ Sismik Sensör İzleme Sistemi</div>
    </div>

    <!-- Config banner — configured olunca gizlenir -->
    <div id="config-banner" class="info-banner" style="display:none">
      &#9888;&#65039; <strong>Ayarlar eksik!</strong> Sistemi başlatmak için önce bağlantı bilgilerini girin.<br>
      Token almak için bilgisayarında çalıştır: <code>npx @liamcottle/rustplus.js fcm-register</code>
    </div>

    <!-- Status Card -->
    <div class="card" id="status-card">
      <div class="card-title">&#128994; Sistem Durumu</div>
      <div class="status-grid">
        <div class="status-row">
          <div class="dot" id="dot-config"></div>
          <span class="status-label">Ayarlar:</span>
          <span class="status-value" id="txt-config">—</span>
        </div>
        <div class="status-row">
          <div class="dot" id="dot-running"></div>
          <span class="status-label">Sistem:</span>
          <span class="status-value" id="txt-running">—</span>
        </div>
        <div class="status-row">
          <div class="dot" id="dot-conn"></div>
          <span class="status-label">Rust+:</span>
          <span class="status-value" id="txt-conn">—</span>
        </div>
        <div class="status-row">
          <div class="dot" id="dot-twilio"></div>
          <span class="status-label">Twilio:</span>
          <span class="status-value" id="txt-twilio">—</span>
        </div>
      </div>
      <div id="error-box" class="alert-box"></div>
    </div>

    <!-- Alarm Counter -->
    <div class="card">
      <div class="alarm-section">
        <div class="alarm-count" id="txt-count">0</div>
        <div class="alarm-label">Toplam Alarm</div>
        <div class="alarm-last" id="txt-last">Henüz alarm yok</div>
      </div>
    </div>

    <!-- Control Buttons -->
    <div class="card">
      <div class="btn-group">
        <div class="btn-row">
          <button class="btn btn-start" id="btn-start" onclick="doStart()">&#9654; BAŞLAT</button>
          <button class="btn btn-stop" id="btn-stop" onclick="doStop()">&#9632; DURDUR</button>
        </div>
        <div class="btn-row">
          <button class="btn btn-test" id="btn-test" onclick="doTest()">&#128222; Test Araması</button>
          <button class="btn btn-config" id="btn-config" onclick="toggleConfig()">&#9881; Ayarlar</button>
        </div>
      </div>
    </div>

    <!-- Config Panel -->
    <div class="card" id="config-card" style="display:none">
      <div class="card-title">&#9881; Bağlantı Ayarları</div>

      <div class="tab-bar">
        <button class="tab-btn active" onclick="switchTab('rustplus')">Rust+</button>
        <button class="tab-btn" onclick="switchTab('twilio')">Twilio</button>
        <button class="tab-btn" onclick="switchTab('help')">Yardım</button>
      </div>

      <!-- Rust+ Tab -->
      <div class="tab-content active" id="tab-rustplus">
        <div id="config-msg" class="alert-box"></div>

        <div class="form-group">
          <label>Sunucu IP</label>
          <input type="text" id="inp-ip" placeholder="123.45.67.89"/>
        </div>
        <div class="form-group">
          <label>Sunucu Port</label>
          <input type="text" id="inp-port" placeholder="28082" value="28082"/>
          <div class="form-hint">Rust+ app port (genellikle 28082)</div>
        </div>
        <div class="form-group">
          <label>Steam ID</label>
          <input type="text" id="inp-steamid" placeholder="76561198..."/>
        </div>
        <div class="form-group">
          <label>Player Token</label>
          <input type="text" id="inp-token" placeholder="-1234567890"/>
          <div class="form-hint">Negatif sayı olabilir</div>
        </div>
        <div class="form-group">
          <label>Entity ID (Sismik Sensör)</label>
          <input type="text" id="inp-entity" placeholder="12345678"/>
        </div>
        <button class="btn btn-save" onclick="saveConfig()" style="margin-top:4px">&#128190; Kaydet</button>
      </div>

      <!-- Twilio Tab -->
      <div class="tab-content" id="tab-twilio">
        <div id="twilio-msg" class="alert-box"></div>
        <div class="form-group">
          <label>Account SID</label>
          <input type="text" id="inp-tsid" placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"/>
        </div>
        <div class="form-group">
          <label>Auth Token</label>
          <input type="password" id="inp-ttoken" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"/>
        </div>
        <div class="form-group">
          <label>Arayan Numara</label>
          <input type="text" id="inp-tfrom" placeholder="+1..."/>
        </div>
        <div class="form-group">
          <label>Aranacak Numara</label>
          <input type="text" id="inp-tto" placeholder="+90..."/>
        </div>
        <button class="btn btn-save" onclick="saveTwilio()" style="margin-top:4px">&#128190; Kaydet</button>
      </div>

      <!-- Help Tab -->
      <div class="tab-content" id="tab-help">
        <div class="info-banner" style="margin:0">
          <strong>Token Nasıl Alınır?</strong><br><br>
          1. Bilgisayarında terminal aç<br>
          2. Çalıştır: <code>npx @liamcottle/rustplus.js fcm-register</code><br>
          3. Chrome açılacak, Steam ile giriş yap<br>
          4. Sonra çalıştır: <code>npx @liamcottle/rustplus.js fcm-listen</code><br>
          5. Rust oyununda ESC → Rust+ → Pair with Server<br>
          6. Terminalde gelen bilgileri buraya yapıştır<br><br>
          <strong>Entity ID Nasıl Bulunur?</strong><br>
          Sismik sensörü bir Smart Alarm'a bağla, Rust+ uygulamasından ID'yi oku
        </div>
      </div>
    </div>

    <!-- Logs -->
    <div class="card">
      <div class="card-title">&#128220; Kayıtlar</div>
      <ul class="log-list" id="log-list"></ul>
    </div>
  </div>

  <script>
    let configVisible = false;

    async function fetchStatus() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();
        update(d);
      } catch(e) {}
    }

    function update(d) {
      // Config banner
      const banner = document.getElementById('config-banner');
      banner.style.display = d.configured ? 'none' : 'block';

      // Config status
      document.getElementById('dot-config').className = 'dot ' + (d.configured ? 'green' : 'yellow');
      document.getElementById('txt-config').textContent = d.configured ? 'Hazır' : 'Eksik';

      // Running status
      document.getElementById('dot-running').className = 'dot ' + (d.running ? 'green pulse' : 'red');
      document.getElementById('txt-running').textContent = d.running ? 'Aktif' : 'Durdu';

      // Connection status
      document.getElementById('dot-conn').className = 'dot ' + (d.connected ? 'green' : (d.running ? 'yellow pulse' : 'red'));
      document.getElementById('txt-conn').textContent = d.connected ? 'Bağlı' : (d.running ? 'Bağlanıyor...' : 'Bağlı Değil');

      // Twilio status
      document.getElementById('dot-twilio').className = 'dot ' + (d.has_twilio ? 'green' : 'yellow');
      document.getElementById('txt-twilio').textContent = d.has_twilio ? 'Hazır' : 'Eksik';

      // Alarm counter
      document.getElementById('txt-count').textContent = d.alarm_count;
      document.getElementById('txt-last').textContent = d.last_alarm ? 'Son alarm: ' + d.last_alarm : 'Henüz alarm yok';

      // Error box
      const eb = document.getElementById('error-box');
      if (d.error) {
        eb.className = 'alert-box error';
        eb.textContent = d.error;
      } else {
        eb.className = 'alert-box';
        eb.textContent = '';
      }

      // Logs
      const ll = document.getElementById('log-list');
      ll.innerHTML = '';
      (d.log || []).forEach(function(e) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="log-time">' + e.time + '</span><span class="log-msg ' + e.level + '">' + e.msg + '</span>';
        ll.appendChild(li);
      });

      // Buttons
      document.getElementById('btn-start').disabled = d.running || !d.configured;
      document.getElementById('btn-stop').disabled = !d.running;

      // Fill config fields if not already filled by user
      if (d.server_ip && !document.getElementById('inp-ip').value) document.getElementById('inp-ip').value = d.server_ip;
      if (d.server_port && !document.getElementById('inp-port').value) document.getElementById('inp-port').value = d.server_port;
      if (d.steam_id && !document.getElementById('inp-steamid').value) document.getElementById('inp-steamid').value = d.steam_id;
      if (d.player_token && !document.getElementById('inp-token').value) document.getElementById('inp-token').value = d.player_token;
      if (d.entity_id && !document.getElementById('inp-entity').value) document.getElementById('inp-entity').value = d.entity_id;
    }

    function toggleConfig() {
      configVisible = !configVisible;
      document.getElementById('config-card').style.display = configVisible ? 'block' : 'none';
    }

    function switchTab(tab) {
      document.querySelectorAll('.tab-btn').forEach(function(b, i) {
        b.classList.toggle('active', ['rustplus','twilio','help'][i] === tab);
      });
      document.querySelectorAll('.tab-content').forEach(function(c) {
        c.classList.remove('active');
      });
      document.getElementById('tab-' + tab).classList.add('active');
    }

    async function saveConfig() {
      const msgEl = document.getElementById('config-msg');
      const data = {
        server_ip: document.getElementById('inp-ip').value,
        server_port: document.getElementById('inp-port').value,
        steam_id: document.getElementById('inp-steamid').value,
        player_token: document.getElementById('inp-token').value,
        entity_id: document.getElementById('inp-entity').value,
      };
      try {
        const r = await fetch('/api/save-config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(data)
        });
        const res = await r.json();
        if (r.ok) {
          msgEl.className = 'alert-box success';
          msgEl.textContent = 'Ayarlar kaydedildi!';
          update(res);
        } else {
          msgEl.className = 'alert-box error';
          msgEl.textContent = res.error || 'Hata olustu!';
        }
      } catch (e) {
        msgEl.className = 'alert-box error';
        msgEl.textContent = 'Baglanti hatasi!';
      }
      setTimeout(function(){ msgEl.className = 'alert-box'; }, 4000);
    }

    async function saveTwilio() {
      const msgEl = document.getElementById('twilio-msg');
      const data = {
        twilio_sid: document.getElementById('inp-tsid').value,
        twilio_token: document.getElementById('inp-ttoken').value,
        twilio_from: document.getElementById('inp-tfrom').value,
        twilio_to: document.getElementById('inp-tto').value,
      };
      try {
        const r = await fetch('/api/save-twilio', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(data)
        });
        const res = await r.json();
        if (r.ok) {
          msgEl.className = 'alert-box success';
          msgEl.textContent = 'Twilio ayarlari kaydedildi!';
        } else {
          msgEl.className = 'alert-box error';
          msgEl.textContent = res.error || 'Hata olustu!';
        }
      } catch (e) {
        msgEl.className = 'alert-box error';
        msgEl.textContent = 'Baglanti hatasi!';
      }
      setTimeout(function(){ msgEl.className = 'alert-box'; }, 4000);
    }

    async function doStart() {
      document.getElementById('btn-start').disabled = true;
      try {
        const r = await fetch('/api/start', {method:'POST'});
        const d = await r.json();
        if (r.ok) update(d);
        else alert(d.error || 'Hata!');
      } catch(e) { alert('Baglanti hatasi!'); }
    }

    async function doStop() {
      document.getElementById('btn-stop').disabled = true;
      const r = await fetch('/api/stop', {method:'POST'});
      update(await r.json());
    }

    async function doTest() {
      const btn = document.getElementById('btn-test');
      btn.disabled = true;
      btn.textContent = '⏳ Aranıyor...';
      await fetch('/api/test-call', {method:'POST'});
      setTimeout(function(){
        btn.disabled = false;
        btn.textContent = '📞 Test Araması';
      }, 3000);
    }

    // Auto-open config if not configured
    fetchStatus().then(function() {
      setTimeout(function() {
        const banner = document.getElementById('config-banner');
        if (banner.style.display !== 'none') {
          configVisible = true;
          document.getElementById('config-card').style.display = 'block';
        }
      }, 500);
    });

    setInterval(fetchStatus, 3000);
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Startup log
# ---------------------------------------------------------------------------
add_log("Uygulama baslatildi.")

if is_configured():
    add_log("Tum ayarlar hazir. BASLAT butonuna basabilirsiniz.")
else:
    missing = []
    if not state.get("server_ip"): missing.append("Server IP")
    if not state.get("server_port"): missing.append("Server Port")
    if not state.get("steam_id"): missing.append("Steam ID")
    if not state.get("player_token"): missing.append("Player Token")
    if not state.get("entity_id"): missing.append("Entity ID")
    add_log(f"Eksik ayarlar: {', '.join(missing)}", "error")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
