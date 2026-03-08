import os
import sys
import asyncio
import threading
import logging
import json
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from twilio.rest import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
state = {
    "running": False,
    "connected": False,
    "alarm_count": 0,
    "last_alarm": None,
    "error": None,
    "log": [],
    # FCM pairing
    "paired": False,
    "steam_id": os.environ.get("RUSTPLUS_STEAM_ID"),
    "player_token": os.environ.get("RUSTPLUS_PLAYER_TOKEN"),
    "pairing_url": None,
}

_rust_thread = None
_fcm_thread = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def add_log(msg, level="info"):
    ts = datetime.now().strftime("%H:%M:%S")
    state["log"].insert(0, {"time": ts, "msg": msg, "level": level})
    state["log"] = state["log"][:100]
    if level == "error":
        logging.error(msg)
    else:
        logging.info(msg)


def make_call():
    try:
        client = Client(os.environ["TWILIO_SID"], os.environ["TWILIO_TOKEN"])
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
            to=os.environ["TWILIO_TO"],
            from_=os.environ["TWILIO_FROM"],
        )
        add_log(f"Arama baslatildi: {call.sid}")
    except Exception as exc:
        add_log(f"Twilio hatasi: {exc}", "error")


# ---------------------------------------------------------------------------
# FCM Listener — pairing bildirimi bekler, steamId + playerToken alır
# ---------------------------------------------------------------------------

def _start_fcm_listener():
    android_id     = os.environ.get("FCM_ANDROID_ID")
    security_token = os.environ.get("FCM_SECURITY_TOKEN")
    fcm_token      = os.environ.get("FCM_TOKEN")

    if not all([android_id, security_token, fcm_token]):
        add_log("FCM credentials eksik, listener baslatilmadi.", "error")
        return

    # Pairing URL'sini state'e yaz
    state["pairing_url"] = (
        f"https://companion-rust.facepunch.com/api/push/link?fcm_token={fcm_token}"
    )

    # Eğer zaten steam_id + player_token varsa listener'a gerek yok
    if state["steam_id"] and state["player_token"]:
        state["paired"] = True
        add_log("Steam ID ve Player Token zaten mevcut, eslestirme atlanıyor.")
        return

    add_log("FCM dinleyici baslatildi. Eslestirme bekleniyor...")

    try:
        # push_receiver user site-packages altında
        sys.path.insert(0, __import__("site").getusersitepackages())
        from push_receiver.push_receiver import PushReceiver

        credentials = {
            "gcm": {
                "androidId": android_id,
                "securityToken": security_token,
            }
        }

        def on_notification(obj, notification, data_message):
            try:
                payload = {}
                if isinstance(notification, dict):
                    payload.update(notification)
                if data_message:
                    for kv in getattr(data_message, "app_data", []):
                        payload[kv.key] = kv.value

                add_log(f"FCM bildirimi alindi: {list(payload.keys())}")

                steam_id     = payload.get("steamId")     or payload.get("steam_id")
                player_token = payload.get("playerToken") or payload.get("player_token")

                if steam_id and player_token:
                    state["steam_id"]     = steam_id
                    state["player_token"] = player_token
                    state["paired"]       = True
                    add_log(f"Eslestirme tamamlandi! Steam ID: {steam_id}", "alarm")
                    add_log("Artik BASLAT butonuna basabilirsiniz.")

            except Exception as exc:
                add_log(f"FCM bildirim hatasi: {exc}", "error")

        receiver = PushReceiver(credentials=credentials)
        receiver.listen(callback=on_notification)  # blocking

    except Exception as exc:
        add_log(f"FCM listener hatasi: {exc}", "error")


# ---------------------------------------------------------------------------
# Rust+ listener
# ---------------------------------------------------------------------------

async def rust_listener():
    from rustplus import RustSocket, EntityEvent

    ip           = os.environ["RUSTPLUS_SERVER_IP"]
    port         = int(os.environ["RUSTPLUS_SERVER_PORT"])
    steam_id     = int(state["steam_id"])
    player_token = int(state["player_token"])
    entity_id    = int(os.environ["RUSTPLUS_ENTITY_ID"])

    while state["running"]:
        socket = None
        try:
            socket = RustSocket(ip, port, steam_id, player_token)

            @socket.on_entity_event(entity_id)
            async def on_alarm(event: EntityEvent):
                if event.value:
                    state["alarm_count"] += 1
                    state["last_alarm"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    add_log("RAID ALARMI! Sismik sensor tetiklendi!", "alarm")
                    threading.Thread(target=make_call, daemon=True).start()
                else:
                    add_log("Sismik sensor sakinlesti.")

            await socket.connect()
            state["connected"] = True
            state["error"] = None
            add_log("Rust+ sunucusuna baglandi.")

            while state["running"]:
                await asyncio.sleep(0.5)

        except Exception as exc:
            state["connected"] = False
            state["error"] = str(exc)
            add_log(f"Baglanti hatasi: {exc}", "error")
            if state["running"]:
                add_log("5 saniye sonra yeniden baglaniliyor...")
                await asyncio.sleep(5)
        finally:
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
    if not state["steam_id"] or not state["player_token"]:
        return jsonify({"error": "Once eslestirme yapilmali! Pairing URL'yi kullanin."}), 400
    if not state["running"]:
        state["running"] = True
        state["alarm_count"] = 0
        state["error"] = None
        add_log("Alarm sistemi baslatildi.")
        _rust_thread = threading.Thread(target=_run_rust_loop, daemon=True)
        _rust_thread.start()
    return jsonify(_safe_state())


@app.route("/api/stop", methods=["POST"])
def api_stop():
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


def _safe_state():
    return {
        "running":      state["running"],
        "connected":    state["connected"],
        "alarm_count":  state["alarm_count"],
        "last_alarm":   state["last_alarm"],
        "error":        state["error"],
        "paired":       state["paired"],
        "steam_id":     state["steam_id"],
        "pairing_url":  state["pairing_url"],
        "log":          state["log"][:30],
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Raid Alarm</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding:16px}
    h1{text-align:center;font-size:1.4rem;margin-bottom:20px;color:#f0883e;letter-spacing:2px;text-transform:uppercase}
    .card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:18px;margin-bottom:14px}
    .status-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
    .dot{width:12px;height:12px;border-radius:50%;flex-shrink:0}
    .dot.green{background:#3fb950;box-shadow:0 0 8px #3fb950}
    .dot.red{background:#f85149;box-shadow:0 0 8px #f85149}
    .dot.yellow{background:#e3b341;box-shadow:0 0 8px #e3b341}
    .label{font-size:0.85rem;color:#8b949e}
    .value{font-size:1rem;font-weight:600}
    .alarm-count{font-size:2.5rem;font-weight:700;text-align:center;color:#f0883e;line-height:1}
    .alarm-label{text-align:center;color:#8b949e;font-size:0.8rem;margin-top:4px}
    .btn{display:block;width:100%;padding:16px;border:none;border-radius:10px;font-size:1.1rem;font-weight:700;cursor:pointer;letter-spacing:1px;transition:opacity .15s;margin-bottom:10px}
    .btn:active{opacity:.75}
    .btn-start{background:#238636;color:#fff}
    .btn-stop{background:#da3633;color:#fff}
    .btn-test{background:#1f6feb;color:#fff;font-size:.9rem;padding:12px}
    .btn-pair{background:#6e40c9;color:#fff;font-size:.9rem;padding:12px;text-decoration:none;display:block;text-align:center;border-radius:10px;font-weight:700;margin-bottom:10px}
    .btn:disabled{opacity:.4;cursor:default}
    .log-list{list-style:none;max-height:260px;overflow-y:auto}
    .log-list li{font-size:0.78rem;padding:5px 0;border-bottom:1px solid #21262d;display:flex;gap:8px}
    .log-time{color:#8b949e;flex-shrink:0}
    .log-msg.alarm{color:#f85149;font-weight:700}
    .log-msg.error{color:#e3b341}
    .log-msg.info{color:#e6edf3}
    .pairing-box{background:#161b22;border:1px solid #6e40c9;border-radius:8px;padding:12px;margin-bottom:14px}
    .pairing-box p{font-size:.82rem;color:#8b949e;margin-bottom:8px}
    .error-box{background:#2d1212;border:1px solid #f8514966;border-radius:8px;padding:10px;font-size:0.82rem;color:#ffa198;margin-top:8px;display:none}
  </style>
</head>
<body>
  <h1>&#128680; Raid Alarm</h1>

  <!-- Pairing kutusu — paired olunca gizlenir -->
  <div class="pairing-box" id="pairing-box" style="display:none">
    <p>&#128279; Steam hesabını bağlamak için aşağıdaki butona bas:</p>
    <a id="pair-link" href="#" target="_blank" class="btn-pair">&#128279; Steam ile Eşleştir</a>
    <p style="font-size:.75rem;color:#6e7681">Giriş yaptıktan sonra Rust oyununda sunucuya bağlan → Rust+ → Pair with Server</p>
  </div>

  <div class="card">
    <div class="status-row">
      <div class="dot" id="dot-paired"></div>
      <span class="label">Eşleştirme:</span>
      <span class="value" id="txt-paired">—</span>
    </div>
    <div class="status-row">
      <div class="dot" id="dot-running"></div>
      <span class="label">Sistem:</span>
      <span class="value" id="txt-running">—</span>
    </div>
    <div class="status-row">
      <div class="dot" id="dot-conn"></div>
      <span class="label">Rust+ Bağlantı:</span>
      <span class="value" id="txt-conn">—</span>
    </div>
    <div id="error-box" class="error-box"></div>
  </div>

  <div class="card">
    <div class="alarm-count" id="txt-count">0</div>
    <div class="alarm-label">Toplam Alarm</div>
    <div style="text-align:center;color:#8b949e;font-size:.78rem;margin-top:6px" id="txt-last">—</div>
  </div>

  <div class="card">
    <button class="btn btn-start" id="btn-start" onclick="doStart()">&#9654; BAŞLAT</button>
    <button class="btn btn-stop"  id="btn-stop"  onclick="doStop()">&#9632; DURDUR</button>
    <button class="btn btn-test"  id="btn-test"  onclick="doTest()">&#128222; Test Araması Yap</button>
  </div>

  <div class="card">
    <div style="font-size:.8rem;color:#8b949e;margin-bottom:8px">Kayıtlar</div>
    <ul class="log-list" id="log-list"></ul>
  </div>

  <script>
    async function fetchStatus() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();
        update(d);
      } catch(e) {}
    }

    function update(d) {
      // Pairing kutusu
      const pbox = document.getElementById('pairing-box');
      if (!d.paired && d.pairing_url) {
        pbox.style.display = 'block';
        document.getElementById('pair-link').href = d.pairing_url;
      } else {
        pbox.style.display = 'none';
      }

      document.getElementById('dot-paired').className = 'dot ' + (d.paired ? 'green' : 'yellow');
      document.getElementById('txt-paired').textContent = d.paired
        ? ('Bağlı' + (d.steam_id ? ' (' + d.steam_id + ')' : ''))
        : 'Eşleştirilmedi';

      document.getElementById('dot-running').className = 'dot ' + (d.running ? 'green' : 'red');
      document.getElementById('txt-running').textContent = d.running ? 'Aktif' : 'Durdu';

      document.getElementById('dot-conn').className = 'dot ' + (d.connected ? 'green' : (d.running ? 'yellow' : 'red'));
      document.getElementById('txt-conn').textContent = d.connected ? 'Bağlı' : (d.running ? 'Bağlanıyor...' : 'Bağlı Değil');

      document.getElementById('txt-count').textContent = d.alarm_count;
      document.getElementById('txt-last').textContent = d.last_alarm ? 'Son alarm: ' + d.last_alarm : 'Henüz alarm yok';

      const eb = document.getElementById('error-box');
      if (d.error) { eb.style.display='block'; eb.textContent = d.error; }
      else { eb.style.display='none'; }

      const ll = document.getElementById('log-list');
      ll.innerHTML = '';
      (d.log || []).forEach(function(e) {
        const li = document.createElement('li');
        li.innerHTML = '<span class="log-time">' + e.time + '</span><span class="log-msg ' + e.level + '">' + e.msg + '</span>';
        ll.appendChild(li);
      });

      document.getElementById('btn-start').disabled = d.running || !d.paired;
      document.getElementById('btn-stop').disabled = !d.running;
    }

    async function doStart() {
      document.getElementById('btn-start').disabled = true;
      const r = await fetch('/api/start', {method:'POST'});
      update(await r.json());
    }
    async function doStop() {
      document.getElementById('btn-stop').disabled = true;
      const r = await fetch('/api/stop', {method:'POST'});
      update(await r.json());
    }
    async function doTest() {
      document.getElementById('btn-test').disabled = true;
      document.getElementById('btn-test').textContent = 'Aranıyor...';
      await fetch('/api/test-call', {method:'POST'});
      setTimeout(function(){
        document.getElementById('btn-test').disabled = false;
        document.getElementById('btn-test').textContent = '📞 Test Araması Yap';
      }, 3000);
    }

    fetchStatus();
    setInterval(fetchStatus, 2000);
  </script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Startup — FCM listener'ı arka planda başlat
# ---------------------------------------------------------------------------

def _start_background_services():
    global _fcm_thread
    _fcm_thread = threading.Thread(target=_start_fcm_listener, daemon=True)
    _fcm_thread.start()


_start_background_services()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
