import os
import asyncio
import threading
import logging
from datetime import datetime
from flask import Flask, jsonify, render_template_string
from twilio.rest import Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = Flask(__name__)

state = {
    "running": False,
    "connected": False,
    "alarm_count": 0,
    "last_alarm": None,
    "error": None,
    "log": [],
}

_thread = None


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
    """Twilio ile telefon araması yap."""
    try:
        client = Client(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )
        twiml = (
            "<Response>"
            "<Say language='tr-TR' voice='alice'>"
            "Dikkat! Rust oyununda raid alarmı! Sismik sensör tetiklendi!"
            "</Say>"
            "<Pause length='1'/>"
            "<Say language='tr-TR' voice='alice'>"
            "Dikkat! Raid alarmı! Dikkat! Raid alarmı!"
            "</Say>"
            "</Response>"
        )
        call = client.calls.create(
            twiml=twiml,
            to=os.environ["TWILIO_TO_NUMBER"],
            from_=os.environ["TWILIO_FROM_NUMBER"],
        )
        add_log(f"Arama başlatıldı: {call.sid}")
    except Exception as exc:
        add_log(f"Twilio hatası: {exc}", "error")


# ---------------------------------------------------------------------------
# Rust+ listener (async, ayrı thread'de çalışır)
# ---------------------------------------------------------------------------

async def rust_listener():
    from rustplus import RustSocket, EntityEvent  # noqa: import burada — Railway ortamında

    ip = os.environ["RUSTPLUS_SERVER_IP"]
    port = int(os.environ["RUSTPLUS_SERVER_PORT"])
    steam_id = int(os.environ["RUSTPLUS_STEAM_ID"])
    player_token = int(os.environ["RUSTPLUS_PLAYER_TOKEN"])
    entity_id = int(os.environ["RUSTPLUS_ENTITY_ID"])

    while state["running"]:
        socket = None
        try:
            socket = RustSocket(ip, port, steam_id, player_token)

            @socket.on_entity_event(entity_id)
            async def on_alarm(event: EntityEvent):
                if event.value:
                    state["alarm_count"] += 1
                    state["last_alarm"] = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                    add_log("RAID ALARMI! Sismik sensör tetiklendi!", "alarm")
                    threading.Thread(target=make_call, daemon=True).start()
                else:
                    add_log("Sismik sensör sakinlesti.")

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


def _run_loop():
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
    global _thread
    if not state["running"]:
        state["running"] = True
        state["alarm_count"] = 0
        state["error"] = None
        add_log("Alarm sistemi baslatildi.")
        _thread = threading.Thread(target=_run_loop, daemon=True)
        _thread.start()
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
        "running": state["running"],
        "connected": state["connected"],
        "alarm_count": state["alarm_count"],
        "last_alarm": state["last_alarm"],
        "error": state["error"],
        "log": state["log"][:30],
    }


# ---------------------------------------------------------------------------
# HTML arayüzü
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
    .btn{display:block;width:100%;padding:16px;border:none;border-radius:10px;font-size:1.1rem;font-weight:700;cursor:pointer;letter-spacing:1px;transition:opacity .15s}
    .btn:active{opacity:.75}
    .btn-start{background:#238636;color:#fff;margin-bottom:10px}
    .btn-stop{background:#da3633;color:#fff;margin-bottom:10px}
    .btn-test{background:#1f6feb;color:#fff;font-size:.9rem;padding:12px}
    .btn:disabled{opacity:.4;cursor:default}
    .log-list{list-style:none;max-height:260px;overflow-y:auto}
    .log-list li{font-size:0.78rem;padding:5px 0;border-bottom:1px solid #21262d;display:flex;gap:8px}
    .log-time{color:#8b949e;flex-shrink:0}
    .log-msg.alarm{color:#f85149;font-weight:700}
    .log-msg.error{color:#e3b341}
    .log-msg.info{color:#e6edf3}
    .error-box{background:#2d1212;border:1px solid #f8514966;border-radius:8px;padding:10px;font-size:0.82rem;color:#ffa198;margin-top:8px;display:none}
  </style>
</head>
<body>
  <h1>&#128680; Raid Alarm</h1>

  <div class="card">
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
    <button class="btn btn-start" id="btn-start" onclick="doStart()">▶ BAŞLAT</button>
    <button class="btn btn-stop"  id="btn-stop"  onclick="doStop()">■ DURDUR</button>
    <button class="btn btn-test"  id="btn-test"  onclick="doTest()">&#128222; Test Araması Yap</button>
  </div>

  <div class="card">
    <div style="font-size:.8rem;color:#8b949e;margin-bottom:8px">Kayıtlar</div>
    <ul class="log-list" id="log-list"></ul>
  </div>

  <script>
    let _running = false;

    async function fetchStatus() {
      try {
        const r = await fetch('/api/status');
        const d = await r.json();
        update(d);
      } catch(e) {}
    }

    function update(d) {
      _running = d.running;

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

      document.getElementById('btn-start').disabled = d.running;
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
