# app.py — BotHost VPS for Koyeb
import os, sys, time, signal, resource, subprocess, threading, shutil
from datetime import datetime
import sqlite3
import requests
from flask import Flask, request, jsonify, Response, render_template_string

PORT = int(os.environ.get("PORT", 8000))
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
API_KEY = os.environ.get("VPS_API_KEY", "change-me")
ADMIN_KEY = os.environ.get("ADMIN_KEY", API_KEY)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BOTS_ROOT = os.path.join(APP_DIR, "bots")
DB_PATH = os.path.join(APP_DIR, "bothost.db")
os.makedirs(BOTS_ROOT, exist_ok=True)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            user_email TEXT,
            provider TEXT,
            ip TEXT,
            status TEXT DEFAULT 'stopped',
            pid INTEGER,
            auto_restart INTEGER DEFAULT 1,
            memory_limit_mb INTEGER DEFAULT 256,
            restarts INTEGER DEFAULT 0,
            created_at TEXT,
            started_at TEXT,
            last_crash_at TEXT,
            last_exit_code INTEGER
        );
    """)
    c.commit()
    c.close()

def tg_send(text):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=8,
        )
    except Exception as e:
        print(f"[tg] {e}")

class Runner:
    def __init__(self):
        self.procs = {}
        self.lock = threading.Lock()
        threading.Thread(target=self.monitor_loop, daemon=True).start()

    def bot_dir(self, bid):
        d = os.path.join(BOTS_ROOT, str(bid))
        os.makedirs(d, exist_ok=True)
        return d

    def _limits(self, mem_mb):
        try:
            if mem_mb and mem_mb > 0:
                b = mem_mb * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (b, b))
            resource.setrlimit(resource.RLIMIT_NOFILE, (512, 512))
            resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
        except Exception:
            pass

    def start(self, bid):
        with self.lock:
            e = self.procs.get(bid)
            if e and e.poll() is None:
                return False, "already running"
            c = db()
            bot = c.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
            c.close()
            if not bot:
                return False, "not found"
            wd = self.bot_dir(bid)
            entry = os.path.join(wd, bot["filename"])
            if not os.path.exists(entry):
                return False, "file missing"
            log_path = os.path.join(wd, "stdout.log")
            log_f = open(log_path, "ab", buffering=0)
            log_f.write(f"\n=== start {datetime.now().isoformat()} ===\n".encode())
            if bot["filename"].endswith(".py"):
                cmd = [sys.executable, "-u", bot["filename"]]
            elif bot["filename"].endswith(".js"):
                cmd = ["node", bot["filename"]]
            else:
                log_f.close()
                return False, "unsupported extension"
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            mem = bot["memory_limit_mb"] or 256
            try:
                p = subprocess.Popen(
                    cmd, cwd=wd, env=env,
                    stdout=log_f, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    preexec_fn=lambda: self._limits(mem),
                    start_new_session=True,
                )
            except Exception as ex:
                log_f.close()
                return False, str(ex)
            self.procs[bid] = p
            c = db()
            c.execute("UPDATE bots SET status='running', pid=?, started_at=? WHERE id=?",
                      (p.pid, datetime.now().isoformat(), bid))
            c.commit()
            c.close()
            return True, None

    def stop(self, bid):
        with self.lock:
            p = self.procs.get(bid)
            if not p or p.poll() is not None:
                self.procs.pop(bid, None)
                c = db()
                c.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bid,))
                c.commit()
                c.close()
                return True, None
            try:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    p.terminate()
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except Exception:
                        p.kill()
                    p.wait(timeout=5)
            except Exception as ex:
                return False, str(ex)
            self.procs.pop(bid, None)
            c = db()
            c.execute("UPDATE bots SET status='stopped', pid=NULL WHERE id=?", (bid,))
            c.commit()
            c.close()
            return True, None

    def restart(self, bid):
        self.stop(bid)
        time.sleep(0.5)
        return self.start(bid)

    def monitor_loop(self):
        while True:
            try:
                time.sleep(5)
                with self.lock:
                    dead = [b for b, p in self.procs.items() if p.poll() is not None]
                    for bid in dead:
                        p = self.procs.pop(bid)
                        code = p.returncode
                        c = db()
                        bot = c.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone()
                        if not bot:
                            c.close()
                            continue
                        name = bot["filename"]
                        email = bot["user_email"] or "guest"
                        if bot["auto_restart"] and bot["status"] == "running":
                            c.execute(
                                "UPDATE bots SET status='restarting', last_crash_at=?, "
                                "last_exit_code=?, restarts=restarts+1 WHERE id=?",
                                (datetime.now().isoformat(), code, bid),
                            )
                            c.commit()
                            c.close()
                            tg_send(
                                f"⚠️ <b>Bot crashed — restarting</b>\n"
                                f"📄 <code>{name}</code>\n"
                                f"👤 <code>{email}</code>\n"
                                f"💥 Exit: <code>{code}</code>"
                            )
                            time.sleep(1.5)
                            self.start(bid)
                        else:
                            c.execute(
                                "UPDATE bots SET status='crashed', pid=NULL, "
                                "last_crash_at=?, last_exit_code=? WHERE id=?",
                                (datetime.now().isoformat(), code, bid),
                            )
                            c.commit()
                            c.close()
                            tg_send(
                                f"🔴 <b>Bot stopped</b>\n"
                                f"📄 <code>{name}</code>\n"
                                f"💥 Exit: <code>{code}</code>"
                            )
            except Exception as e:
                print(f"[monitor] {e}")

runner = Runner()

app = Flask(__name__)
init_db()

def auth_ok():
    return request.headers.get("X-Api-Key", "") == API_KEY

def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0].strip()

@app.route("/api/deploy", methods=["POST"])
def api_deploy():
    if not auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    data = request.get_json(silent=True) or {}
    filename = (data.get("filename") or "bot.py").strip()
    code = data.get("code") or ""
    email = (data.get("email") or "guest").strip()
    provider = (data.get("provider") or "?").strip()
    mem = int(data.get("memory_limit_mb") or 256)
    ar = 1 if data.get("auto_restart", True) else 0
    if not code:
        return jsonify({"ok": False, "error": "missing code"}), 400
    if len(code) > 5 * 1024 * 1024:
        return jsonify({"ok": False, "error": "too large"}), 413
    safe = "".join(ch for ch in filename if ch.isalnum() or ch in "._-")
    if not safe.endswith(".py"):
        safe += ".py"
    c = db()
    cur = c.execute(
        "INSERT INTO bots (filename, user_email, provider, ip, memory_limit_mb, "
        "auto_restart, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (safe, email, provider, client_ip(), mem, ar, "stopped", datetime.now().isoformat()),
    )
    bid = cur.lastrowid
    c.commit()
    c.close()
    wd = runner.bot_dir(bid)
    with open(os.path.join(wd, safe), "w", encoding="utf-8") as f:
        f.write(code)
    ok, err = runner.start(bid)
    if ok:
        tg_send(
            f"✅ <b>Bot deployed and running</b>\n"
            f"━━━━━━━━━━━━━━━\n"
            f"🆔 ID: <code>{bid}</code>\n"
            f"📄 <code>{safe}</code>\n"
            f"👤 <code>{email}</code>\n"
            f"🔐 {provider}\n"
            f"💾 <code>{mem} MB</code>\n"
            f"♻️ Auto-restart: <b>{'ON' if ar else 'OFF'}</b>\n"
            f"🕒 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
    else:
        tg_send(f"❌ <b>Deploy failed</b>\n🆔 <code>{bid}</code>\n📄 <code>{safe}</code>\n⚠️ <code>{err}</code>")
    return jsonify({"ok": ok, "bot_id": bid, "filename": safe,
                    "status": "running" if ok else "failed", "error": err})

@app.route("/api/stop/<int:bid>", methods=["POST"])
def api_stop(bid):
    if not auth_ok():
        return jsonify({"ok": False}), 401
    ok, err = runner.stop(bid)
    return jsonify({"ok": ok, "error": err})

@app.route("/api/restart/<int:bid>", methods=["POST"])
def api_restart(bid):
    if not auth_ok():
        return jsonify({"ok": False}), 401
    ok, err = runner.restart(bid)
    return jsonify({"ok": ok, "error": err})

@app.route("/api/list")
def api_list():
    if not auth_ok():
        return jsonify({"ok": False}), 401
    c = db()
    rows = c.execute("SELECT * FROM bots ORDER BY id DESC LIMIT 200").fetchall()
    c.close()
    return jsonify({"ok": True, "bots": [dict(r) for r in rows]})

@app.route("/api/logs/<int:bid>")
def api_logs(bid):
    if not auth_ok():
        return jsonify({"ok": False}), 401
    p = os.path.join(BOTS_ROOT, str(bid), "stdout.log")
    if not os.path.exists(p):
        return "no logs", 200, {"Content-Type": "text/plain"}
    sz = os.path.getsize(p)
    with open(p, "rb") as f:
        f.seek(max(0, sz - 65536))
        data = f.read()
    return Response(data, mimetype="text/plain")

@app.route("/health")
def health():
    return jsonify({"ok": True, "time": datetime.now().isoformat()})

ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>BotHost VPS</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui;background:#0a0a0f;color:#e6e6ef;margin:0;padding:20px}
h1{font-size:20px;margin-bottom:16px}
.row{background:#12121a;border:1px solid #1f1f2e;border-radius:10px;padding:14px;margin-bottom:10px}
.meta{font-size:13px;line-height:1.55;margin-bottom:8px}
.name{font-weight:600;color:#c7d2fe}
.badge{padding:3px 9px;border-radius:6px;font-size:12px;font-weight:600}
.running{background:#065f46;color:#6ee7b7}.stopped{background:#374151;color:#cbd5e1}
.crashed{background:#7f1d1d;color:#fecaca}.restarting{background:#78350f;color:#fde68a}
.btns form{display:inline}
button{background:#1f2937;color:#e6e6ef;border:1px solid #374151;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;margin-left:6px}
button.danger{background:#7f1d1d;border-color:#991b1b}
a{color:#93c5fd;text-decoration:none;margin-right:8px;font-size:12px}
.muted{color:#808099;font-size:12px}
</style></head><body>
<h1>BotHost VPS · admin</h1>
<p class="muted">Total: {{ n }}</p>
{% for b in bots %}<div class="row">
<div class="meta"><span class="name">#{{ b.id }} · {{ b.filename }}</span>
<span class="badge {{ b.status }}">{{ b.status }}</span></div>
<div class="muted">{{ b.user_email }} · {{ b.ip }} · {{ b.memory_limit_mb }}MB · restarts {{ b.restarts }}</div>
<div class="muted">started: {{ b.started_at or '-' }} · crash: {{ b.last_crash_at or '-' }}</div>
<div style="margin-top:8px">
<a href="/admin/logs/{{ b.id }}?k={{ k }}">logs</a>
<form method="POST" action="/admin/start/{{ b.id }}?k={{ k }}"><button>start</button></form>
<form method="POST" action="/admin/stop/{{ b.id }}?k={{ k }}"><button>stop</button></form>
<form method="POST" action="/admin/restart/{{ b.id }}?k={{ k }}"><button>restart</button></form>
<form method="POST" action="/admin/delete/{{ b.id }}?k={{ k }}" onsubmit="return confirm('Delete?')"><button class="danger">delete</button></form>
</div></div>{% endfor %}</body></html>"""

def _admin_ok():
    return request.args.get("k", "") == ADMIN_KEY

@app.route("/admin")
def admin_home():
    if not _admin_ok():
        return "403", 403
    c = db()
    rows = c.execute("SELECT * FROM bots ORDER BY id DESC LIMIT 200").fetchall()
    c.close()
    return render_template_string(ADMIN_HTML, bots=[dict(r) for r in rows], k=ADMIN_KEY, n=len(rows))

@app.route("/admin/start/<int:bid>", methods=["POST"])
def admin_start(bid):
    if not _admin_ok():
        return "403", 403
    runner.start(bid)
    return ("", 302, {"Location": f"/admin?k={ADMIN_KEY}"})

@app.route("/admin/stop/<int:bid>", methods=["POST"])
def admin_stop(bid):
    if not _admin_ok():
        return "403", 403
    runner.stop(bid)
    return ("", 302, {"Location": f"/admin?k={ADMIN_KEY}"})

@app.route("/admin/restart/<int:bid>", methods=["POST"])
def admin_restart(bid):
    if not _admin_ok():
        return "403", 403
    runner.restart(bid)
    return ("", 302, {"Location": f"/admin?k={ADMIN_KEY}"})

@app.route("/admin/delete/<int:bid>", methods=["POST"])
def admin_delete(bid):
    if not _admin_ok():
        return "403", 403
    runner.stop(bid)
    c = db()
    c.execute("DELETE FROM bots WHERE id=?", (bid,))
    c.commit()
    c.close()
    shutil.rmtree(os.path.join(BOTS_ROOT, str(bid)), ignore_errors=True)
    return ("", 302, {"Location": f"/admin?k={ADMIN_KEY}"})

@app.route("/admin/logs/<int:bid>")
def admin_logs(bid):
    if not _admin_ok():
        return "403", 403
    p = os.path.join(BOTS_ROOT, str(bid), "stdout.log")
    if not os.path.exists(p):
        return "no logs", 200, {"Content-Type": "text/plain"}
    with open(p, "rb") as f:
        return Response(f.read(), mimetype="text/plain")

def restore():
    c = db()
    rows = c.execute("SELECT id FROM bots WHERE status IN ('running','restarting')").fetchall()
    c.close()
    for r in rows:
        try:
            runner.start(r["id"])
        except Exception as e:
            print(f"[restore] {r['id']}: {e}")

if __name__ == "__main__":
    restore()
    print(f"BotHost VPS starting on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
