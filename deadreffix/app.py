"""API JSON + interface web (Flask). Toute l'API est protégée par le jeton administrateur."""
import hmac
import json
import os
import threading
import time
import uuid
from datetime import timedelta

from flask import Flask, jsonify, request, send_from_directory, session

from . import config as C
from . import store
from .engine import Engine, in_schedule, make_diff, run_worker
from .wiki import Halt, WikiError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "web", "dist")
app = Flask(__name__, static_folder=None)
app.secret_key = C.SECRET
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict", SESSION_COOKIE_SECURE=os.environ.get("DRF_INSECURE_COOKIES") != "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
_engine = None
_fails = {}


def eng():
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def authed():
    return bool(C.ADMIN_TOKEN) and session.get("ok") is True


@app.before_request
def protect():
    p = request.path
    if not p.startswith("/api/") or p in ("/api/login",):
        return None
    if not authed():
        return jsonify(error="non authentifié"), 401
    if request.method in ("POST", "PUT", "DELETE") and not request.is_json:
        return jsonify(error="JSON requis"), 415
    return None


@app.after_request
def headers(r):
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "same-origin"
    r.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
    return r


@app.get("/health")
def health():
    return "ok"


@app.post("/api/login")
def login():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "?").split(",")[0]
    f = _fails.get(ip, (0, 0))
    if f[0] >= 5 and time.time() - f[1] < 300:
        return jsonify(error="trop de tentatives, réessayez dans quelques minutes"), 429
    if not C.ADMIN_TOKEN:
        return jsonify(error="Le serveur a été lancé sans DRF_ADMIN_TOKEN : utilisez run-local.bat (ou run-local.sh), ou définissez cette variable."), 503
    tok = (request.get_json(silent=True) or {}).get("token", "").strip()
    if C.ADMIN_TOKEN and hmac.compare_digest(str(tok), C.ADMIN_TOKEN):
        session.permanent = True
        session["ok"] = True
        _fails.pop(ip, None)
        return jsonify(ok=True)
    _fails[ip] = (f[0] + 1, time.time())
    time.sleep(1)
    return jsonify(error="jeton invalide"), 401


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(ok=True)


@app.get("/api/status")
def status():
    cfg = store.config()
    hb = store.get("heartbeat")
    out = {
        "account": C.ACCOUNT, "kill_url": C.KILL_URL, "talk_page": C.TALK_PAGE, "config": cfg, "halt": store.get("halt"), "heartbeat": hb,
        "now": store.now(), "edits_hour": store.edits_since(3600), "edits_day": store.edits_since(86400), "in_schedule": in_schedule(cfg),
        "queue": store.q("SELECT COUNT(*) n FROM queue")[0]["n"],
        "pending": store.q("SELECT COUNT(*) n FROM proposals WHERE status='pending'")[0]["n"],
        "saved_total": store.q("SELECT COUNT(*) n FROM edits")[0]["n"],
        "credentials": bool(C.OAUTH_TOKEN or (C.BOT_USER and C.BOT_PASSWORD)),
    }
    try:
        ui = eng().userinfo() if out["credentials"] else None
        out["bot"] = ui
        out["effective_mode"] = eng().effective_mode(cfg) if ui else cfg["mode"] if cfg["mode"] != "auto" else "supervised"
    except WikiError as e:
        out["bot_error"] = str(e)
        out["effective_mode"] = "supervised" if cfg["mode"] == "auto" else cfg["mode"]
    return jsonify(out)


@app.route("/api/config", methods=["GET", "PUT"])
def config():
    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        if body.get("mode") not in (None, "dry", "supervised", "auto"):
            return jsonify(error="mode invalide"), 400
        for k in ("batch_size", "recheck_days", "max_refs", "max_seconds", "min_edit_interval", "max_edits_per_hour", "max_edits_per_day", "max_errors_before_pause"):
            if k in body:
                body[k] = max(0, int(body[k]))
        body["min_edit_interval"] = max(10, body.get("min_edit_interval", 15)) if "min_edit_interval" in body else None
        if body["min_edit_interval"] is None:
            body.pop("min_edit_interval")
        if "max_edits_per_hour" in body:
            body["max_edits_per_hour"] = min(body["max_edits_per_hour"], 60)
        if "max_edits_per_day" in body:
            body["max_edits_per_day"] = min(body["max_edits_per_day"], 500)
        store.save_config(body)
        store.log("CONFIG", "configuration modifiée")
    return jsonify(store.config())


@app.get("/api/log")
def get_log():
    n = min(int(request.args.get("limit", 200)), 1000)
    return jsonify(store.q("SELECT * FROM log ORDER BY id DESC LIMIT ?", (n,)))


@app.get("/api/proposals")
def proposals():
    st = request.args.get("status")
    sql = "SELECT id,title,status,created,decided,total,summary,note,hits,no_archive FROM proposals"
    rows = store.q(sql + (" WHERE status=?" if st else "") + " ORDER BY id DESC LIMIT 200", (st,) if st else ())
    for r in rows:
        r["hits"] = json.loads(r["hits"] or "[]")
        r["no_archive"] = json.loads(r["no_archive"] or "[]")
    return jsonify(rows)


@app.get("/api/proposals/<int:pid>")
def proposal(pid):
    r = store.q("SELECT * FROM proposals WHERE id=?", (pid,))
    if not r:
        return jsonify(error="introuvable"), 404
    r = r[0]
    r.pop("new_text", None)
    r["hits"] = json.loads(r["hits"] or "[]")
    return jsonify(r)


@app.post("/api/proposals/<int:pid>/approve")
def approve(pid):
    try:
        body = request.get_json(silent=True) or {}
        custom_summary = body.get("summary")
        return jsonify(eng().save(pid, custom_summary))
    except Halt as e:
        return jsonify(error=f"arrêt : {e}"), 409
    except WikiError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/proposals/<int:pid>/reject")
def reject(pid):
    store.x("UPDATE proposals SET status='rejected', decided=? WHERE id=? AND status IN ('pending','dry')", (store.now(), pid))
    return jsonify(ok=True)


@app.route("/api/queue", methods=["GET", "POST", "DELETE"])
def queue():
    if request.method == "POST":
        titles = [t.strip() for t in (request.get_json(silent=True) or {}).get("titles", []) if t and t.strip()][:200]
        for t in titles:
            store.x("INSERT OR REPLACE INTO queue(title,source,manual,added) VALUES(?,?,1,?)", (t, "manuel", store.now()))
        store.log("FILE", f"{len(titles)} article(s) ajouté(s) à la file")
    if request.method == "DELETE":
        store.x("DELETE FROM queue")
    return jsonify(store.q("SELECT * FROM queue ORDER BY manual DESC, id LIMIT 200"))


@app.get("/api/categories/search")
def cat_search():
    try:
        return jsonify(eng().wiki.category_search(request.args.get("q", "")))
    except WikiError as e:
        return jsonify(error=str(e)), 502


@app.get("/api/categories/preview")
def cat_preview():
    name, depth = request.args.get("name", ""), min(int(request.args.get("depth", 0)), 3)
    try:
        info = eng().wiki.category_info(name)
        members = eng().wiki.category_members(name, depth, limit=300) if info["exists"] else []
        return jsonify(info=info, count=len(members), sample=members[:15])
    except WikiError as e:
        return jsonify(error=str(e)), 502


@app.post("/api/killswitch/check")
def ks_check():
    try:
        trig = eng().wiki.killswitch_check()
    except WikiError as e:
        return jsonify(error=str(e)), 502
    if trig:
        eng().set_halt("killswitch" if trig["type"] == "talk" else "blocked", trig.get("user") and f"Arrêt d'urgence demandé par {trig['user']}" or trig["reason"], trig)
    return jsonify(triggered=bool(trig), trigger=trig)


@app.post("/api/stop")
def stop():
    eng().set_halt("manual", "arrêt manuel depuis le tableau de bord")
    return jsonify(ok=True)


@app.post("/api/resume")
def resume():
    if not (request.get_json(silent=True) or {}).get("confirm"):
        return jsonify(error="confirmation requise"), 400
    store.put("resumed_at", store.now())
    store.put("halt", None)
    store.log("REPRISE", "reprise validée manuellement")
    return jsonify(ok=True)


_jobs = {}


def _run_test(jid, title):
    try:
        page, res = eng().analyse(title)
        if page is None:
            _jobs[jid] = {"done": True, "skipped": res}
            return
        out = {"done": True, "analyzed": res.analyzed, "refs_total": res.refs_total, "dead": res.dead_found, "total": res.total,
               "offline": res.offline, "timed_out": res.timed_out, "no_archive": res.no_archive, "failures": res.failures, "hits": res.hits,
               "report": res.report, "skipped_refs": res.skipped, "summary": res.summary()}
        if res.changed:
            out["proposal_id"] = eng().make_proposal(page, res, "dry")
            out["diff"] = make_diff(page["text"], res.text)
        _jobs[jid] = out
    except Exception as e:  # noqa
        _jobs[jid] = {"done": True, "error": str(e)}


@app.post("/api/test")
def test_article():
    title = (request.get_json(silent=True) or {}).get("title", "").strip()
    if not title:
        return jsonify(error="titre requis"), 400
    jid = uuid.uuid4().hex[:12]
    for k in list(_jobs)[:-20]:
        _jobs.pop(k, None)
    _jobs[jid] = {"done": False}
    threading.Thread(target=_run_test, args=(jid, title), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/test/<jid>")
def test_result(jid):
    return jsonify(_jobs.get(jid, {"done": True, "error": "analyse introuvable"}))


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path):
    if path and os.path.isfile(os.path.join(DIST, path)):
        return send_from_directory(DIST, path)
    if not os.path.isfile(os.path.join(DIST, "index.html")):
        return "Interface non construite (web/dist manquant).", 503
    return send_from_directory(DIST, "index.html")


if os.environ.get("DRF_EMBED_WORKER") == "1":  # essai local : le worker tourne dans le même processus
    threading.Thread(target=run_worker, daemon=True).start()
