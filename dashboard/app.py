import threading
import io
import json
import os
from flask import Flask, render_template, jsonify, send_file, request
from flask_socketio import SocketIO

import os as _os
app = Flask(__name__)
app.config["SECRET_KEY"] = "scorm-sim-2024"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")
OWNER_ID = _os.environ.get("OWNER_ID", "default")

_state = None
_run_simulator = None
_custom_schedule = {}
_db = None  # Supabase DB instance

def init_dashboard(state, db=None):
    global _state, _db
    _state = state
    _db = db

def set_simulator_runner(fn):
    global _run_simulator
    _run_simulator = fn

def push_update():
    if _state:
        try:
            socketio.emit("state_update", {
                "accounts": _state.get_all(),
                "logs":     _state.get_logs(200),
                "schedule": _state.get_schedule(),
                "running":  _state.is_running()
            })
        except:
            pass

def _load_config():
    env_cfg = os.environ.get("SCORM_CONFIG")
    if env_cfg:
        return json.loads(env_cfg)
    with open("config.json") as f:
        return json.load(f)

# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({"status": "ok", "running": _state.is_running() if _state else False})

@app.route("/api/state")
def get_state():
    if not _state:
        return jsonify({"error": "not ready"})

    accounts = _state.get_all()

    # If state is empty, load from Supabase/config directly
    if not accounts and _db and _db.enabled:
        from modules.proxy import ProxyManager
        sb_accounts = _db.get_accounts(owner_id=OWNER_ID)
        accounts = [{
            "email":        a["email"],
            "name":         a.get("name", a["email"].split("@")[0]),
            "status":       "queued",
            "status_label": "Ready",
            "proxy":        a.get("proxy_country", "in"),
            "modules_done": [],
            "progress":     0
        } for a in sb_accounts]

    return jsonify({
        "accounts": accounts,
        "logs":     _state.get_logs(200),
        "schedule": _state.get_schedule(),
        "running":  _state.is_running(),
        "paused":   _state.is_paused()
    })

@app.route("/api/config")
def get_config():
    try:
        cfg = _load_config()
        # Get accounts: config + Supabase
        accounts = list(cfg.get("accounts", []))
        if _db and _db.enabled:
            sb_accounts = _db.get_accounts(owner_id=OWNER_ID)
            existing = {a["email"] for a in accounts}
            for a in sb_accounts:
                if a["email"] not in existing:
                    accounts.append(a)
        return jsonify({
            "accounts": accounts,
            "courses": cfg["courses"],
            "proxy_countries": list(cfg.get("proxy", {}).get("servers", {}).keys())
        })
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    if _db and _db.enabled:
        return jsonify(_db.get_accounts(owner_id=OWNER_ID))
    return jsonify([])

@app.route("/api/accounts", methods=["POST"])
def add_account():
    data = request.json
    email    = data.get("email", "")
    password = data.get("password", "")
    proxy    = data.get("proxy_country", "in")
    name     = data.get("name", email.split("@")[0])
    modules  = data.get("modules", [])

    if not email or not password:
        return jsonify({"ok": False, "error": "Email and password required"})

    if _db and _db.enabled:
        ok = _db.add_account(email, password, proxy, name, modules, owner_id=OWNER_ID)
        if ok:
            cfg = _load_config()
            _db.init_progress(email, cfg["courses"])
            # Add to live state so dashboard shows immediately
            if _state:
                _state.update(email,
                    name=name or email.split("@")[0],
                    proxy=proxy,
                    status="queued",
                    status_label="Ready"
                )
                push_update()
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "Failed to add (may already exist)"})

    # Fallback: local file
    path = "accounts.json"
    accounts = []
    if os.path.exists(path):
        with open(path) as f:
            accounts = json.load(f)
    if any(a["email"] == email for a in accounts):
        return jsonify({"ok": False, "error": "Email already exists"})
    accounts.append({"email":email,"password":password,"proxy_country":proxy,"name":name,"modules":modules})
    with open(path, "w") as f:
        json.dump(accounts, f, indent=2)
    return jsonify({"ok": True})

@app.route("/api/accounts/<email>", methods=["DELETE"])
def delete_account(email):
    # Delete from Supabase
    if _db and _db.enabled:
        _db.delete_account(email)

    # Delete from local accounts.json
    path = "accounts.json"
    if os.path.exists(path):
        with open(path) as f:
            accounts = json.load(f)
        accounts = [a for a in accounts if a["email"] != email]
        with open(path, "w") as f:
            json.dump(accounts, f, indent=2)

    # Delete local cookie file
    import glob
    safe = email.replace("@","_").replace(".","_")
    for f_path in glob.glob(f"cookies_{safe}*.json"):
        try:
            os.remove(f_path)
        except:
            pass

    # Remove from live state + push update to dashboard
    if _state:
        _state.remove_account(email)
        push_update()

    return jsonify({"ok": True})

@app.route("/api/schedule", methods=["POST"])
def save_schedule():
    global _custom_schedule
    data = request.json
    _custom_schedule = data.get("schedule", {})
    count = sum(len(v) for v in _custom_schedule.values())
    return jsonify({"ok": True, "saved": count})

@app.route("/api/control/start_one", methods=["POST"])
def start_one():
    """Start simulation for a single account"""
    if not _run_simulator:
        return jsonify({"ok": False, "error": "simulator not ready"})
    data  = request.json or {}
    email = data.get("email", "")
    if not email:
        return jsonify({"ok": False, "error": "email required"})

    sched = _custom_schedule if _custom_schedule else None

    def run_single():
        try:
            _run_simulator(custom_schedule=sched, single_email=email)
        except Exception as e:
            if _state:
                _state.log(f"Error starting {email}: {e}", "error")
                push_update()

    t = threading.Thread(target=run_single, daemon=True)
    t.start()

    if _state:
        _state.log(f"▶️ Starting: {email}", "success")
        push_update()
    return jsonify({"ok": True})

@app.route("/api/control/<action>", methods=["POST"])
def control(action):
    if not _state:
        return jsonify({"error": "not ready"})
    if action == "start":
        if not _state.is_running():
            _state.start()
            sched = _custom_schedule if _custom_schedule else None
            t = threading.Thread(
                target=_run_simulator,
                kwargs={"custom_schedule": sched},
                daemon=True
            )
            t.start()
            _state.log("▶️ Simulator started!", "success")
    elif action == "pause":
        _state.pause()
        _state.log("⏸️ Paused", "warning")
    elif action == "resume":
        _state.resume()
        _state.log("▶️ Resumed", "success")
    elif action == "stop":
        _state.stop()
        _state.log("🛑 Stopped", "error")
    push_update()
    return jsonify({"ok": True, "action": action})

@app.route("/api/export")
def export_csv():
    if not _state:
        return jsonify({"error": "not ready"})
    csv_data = _state.export_csv()
    return send_file(
        io.BytesIO(csv_data.encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="scorm_report.csv"
    )

def run_dashboard(host="0.0.0.0", port=5000):
    socketio.run(app, host=host, port=port, debug=False,
                 allow_unsafe_werkzeug=True, log_output=False)
