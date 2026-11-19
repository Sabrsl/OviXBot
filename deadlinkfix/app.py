"""API JSON + interface web (Flask). Toute l'API est protégée par le jeton administrateur."""
import hmac
import json
import os
import threading
import time
import uuid
from datetime import timedelta

from flask import Flask, jsonify, request, send_from_directory, session
from werkzeug.middleware.proxy_fix import ProxyFix

from . import config as C
from . import store
from .engine import Engine, in_schedule, make_diff, run_worker
from .wiki import Halt, WikiError

# Constants
BOT_REFRESH_INTERVAL = 120
JOB_TIMEOUT = 600
MAX_LOGIN_ATTEMPTS = 5
LOGIN_COOLDOWN_SECONDS = 300
MAX_TITLES_PER_REQUEST = 200
MAX_PROPOSALS_LIMIT = 200
MAX_LOG_LIMIT = 1000
DEFAULT_LOG_LIMIT = 200
MAX_QUEUE_ITEMS = 200
MAX_CATEGORY_DEPTH = 3
MAX_CATEGORY_SAMPLE = 15
MAX_CATEGORY_MEMBERS = 300
MIN_EDIT_INTERVAL = 10
MAX_BATCH_SIZE = 500
MAX_EDITS_PER_HOUR = 60
MAX_EDITS_PER_DAY = 500
MAX_SUMMARY_CHARS = 350
JOB_CLEANUP_KEEP = 20
# Nombre de proxys de confiance devant l'application (X-Forwarded-For). 0 = aucun (accès direct).
# Défaut 1 = comportement équivalent à l'ancien (derrière un proxy) ; mettre DRF_PROXY_HOPS=0 sans proxy.
PROXY_HOPS = int(os.environ.get("DRF_PROXY_HOPS", "1") or 0)

# Bornes (min, max) des réglages numériques
INT_LIMITS = {
    "batch_size": (1, 100), "recheck_days": (0, 3650), "max_refs": (0, MAX_BATCH_SIZE), "max_seconds": (30, 3600),
    "min_edit_interval": (MIN_EDIT_INTERVAL, 3600), "max_edits_per_hour": (1, MAX_EDITS_PER_HOUR),
    "max_edits_per_day": (1, MAX_EDITS_PER_DAY), "max_errors_before_pause": (1, 100),
    "category_chunk": (10, 500), "max_scan_per_fill": (100, 10000),
}
BOOL_KEYS = ("enabled", "autonomy_confirmed", "add_archive", "mark_broken", "random_pages")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "web", "dist")
app = Flask(__name__, static_folder=None)
if PROXY_HOPS > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=PROXY_HOPS)
app.secret_key = C.SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=os.environ.get("DRF_INSECURE_COOKIES") != "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12)
)
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
    ip = request.remote_addr or "?"
    now_t = time.time()
    for k in [k for k, v in _fails.items() if now_t - v[1] > LOGIN_COOLDOWN_SECONDS]:  # purge des anciennes entrées
        _fails.pop(k, None)
    f = _fails.get(ip, (0, 0))
    if f[0] >= MAX_LOGIN_ATTEMPTS and now_t - f[1] < LOGIN_COOLDOWN_SECONDS:
        return jsonify(error="trop de tentatives, réessayez dans quelques minutes"), 429
    if not C.ADMIN_TOKEN:
        return jsonify(error="Le serveur a été lancé sans DRF_ADMIN_TOKEN : utilisez run-local.bat (ou run-local.sh), ou définissez cette variable."), 503
    body = request.get_json(silent=True)
    tok = str((body.get("token") if isinstance(body, dict) else "") or "").strip()
    if C.ADMIN_TOKEN and hmac.compare_digest(tok, C.ADMIN_TOKEN):
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


_bot = {"info": None, "error": None, "at": 0.0, "busy": False}


def _refresh_bot():
    try:
        _bot["info"], _bot["error"] = eng().userinfo(), None
    except Exception as e:  # noqa
        _bot["info"], _bot["error"] = None, str(e)
    _bot["at"], _bot["busy"] = time.time(), False


def bot_info():
    """Infos du compte Wikipédia, SANS JAMAIS bloquer la réponse : la connexion se fait en arrière-plan (c'était la cause du
    « Chargement… » infini : /api/status attendait Wikipédia, avec nouvelles tentatives, avant de répondre).

    Avec DRF_DISABLE_BOT_REFRESH=1 : désactive les actualisations automatiques, retourne le cache existant.
    Sans DRF_DISABLE_BOT_REFRESH : rafraîchissement asynchrone en arrière-plan toutes les 120s."""
    if not (C.HAS_CREDENTIALS):
        return None, None
    if (
        not C.DISABLE_BOT_REFRESH
        and not _bot["busy"]
        and time.time() - _bot["at"] > BOT_REFRESH_INTERVAL
    ):
        _bot["busy"] = True
        threading.Thread(target=_refresh_bot, daemon=True).start()
    return _bot["info"], _bot["error"]


@app.get("/api/status")
def status():
    errs = []  # propre à la requête (plus de liste globale partagée)

    def safe(fn, default):
        try:
            return fn()
        except Exception as e:  # noqa  (base verrouillée, etc. : on répond quand même)
            errs.append(str(e))
            return default

    cfg = safe(store.config, dict(C.DEFAULT_CONFIG))
    info, err = bot_info()
    out = {
        "account": C.ACCOUNT, "account_kind": "awb" if C.IS_AWB_USER else "bot", "kill_url": C.KILL_URL, "talk_page": C.TALK_PAGE, "config": cfg, "halt": safe(lambda: store.get("halt"), None),
        "heartbeat": safe(lambda: store.get("heartbeat"), None), "now": store.now(),
        "edits_hour": safe(lambda: store.edits_since(3600), 0), "edits_day": safe(lambda: store.edits_since(86400), 0),
        "in_schedule": in_schedule(cfg),
        "queue": safe(lambda: store.q("SELECT COUNT(*) n FROM queue")[0]["n"], 0),
        "pending": safe(lambda: store.q("SELECT COUNT(*) n FROM proposals WHERE status='pending'")[0]["n"], 0),
        "saved_total": safe(lambda: store.q("SELECT COUNT(*) n FROM edits")[0]["n"], 0),
        "credentials": bool(C.HAS_CREDENTIALS),
        "bot": info,
    }
    if err:
        out["bot_error"] = err
    if errs:
        out["db_error"] = errs[0]
    out["effective_mode"] = eng().effective_mode(cfg, info) if info else ("supervised" if cfg["mode"] == "auto" else cfg["mode"])
    return jsonify(out)


def _clean_config(body):
    """Valide et borne un patch de configuration. Retourne (patch, None) ou (None, message d'erreur)."""
    if not isinstance(body, dict):
        return None, "corps invalide"
    out = dict(body)
    if out.get("mode") is None:
        out.pop("mode", None)
    elif out["mode"] not in ("dry", "supervised", "auto"):
        return None, "mode invalide"
    for k, (lo, hi) in INT_LIMITS.items():
        if k in out:
            try:
                out[k] = max(lo, min(hi, int(out[k])))
            except (TypeError, ValueError):
                return None, f"{k} invalide"
    for k in BOOL_KEYS:
        if k in out:
            out[k] = bool(out[k])
    if "categories" in out:
        cats = []
        for c in out["categories"] if isinstance(out["categories"], list) else []:
            if isinstance(c, dict) and isinstance(c.get("name"), str) and c["name"].strip():
                try:
                    d = int(c.get("depth", 0))
                except (TypeError, ValueError):
                    d = 0
                cats.append({"name": c["name"].strip(), "depth": max(0, min(MAX_CATEGORY_DEPTH, d))})
        out["categories"] = cats
    for k in ("titles", "rules_disabled"):
        if k in out:
            out[k] = [s.strip() for s in out[k] if isinstance(s, str) and s.strip()][:1000] if isinstance(out[k], list) else []
    if "schedule" in out:
        if not isinstance(out["schedule"], dict):
            out.pop("schedule")
        else:
            s = dict(out["schedule"])
            try:
                if "enabled" in s:
                    s["enabled"] = bool(s["enabled"])
                if "days" in s:
                    s["days"] = sorted({int(d) for d in s["days"] if 0 <= int(d) <= 6}) if isinstance(s["days"], list) else []
                if "start_hour" in s:
                    s["start_hour"] = max(0, min(23, int(s["start_hour"])))
                if "end_hour" in s:
                    s["end_hour"] = max(1, min(24, int(s["end_hour"])))
                if "cycle_pause" in s:
                    s["cycle_pause"] = max(0, min(86400, int(s["cycle_pause"])))
            except (TypeError, ValueError):
                return None, "planification invalide"
            out["schedule"] = s
    return out, None


@app.route("/api/config", methods=["GET", "PUT"])
def config():
    if request.method == "PUT":
        body, err = _clean_config(request.get_json(silent=True) or {})
        if err:
            return jsonify(error=err), 400
        store.save_config(body)
        store.log("CONFIG", "configuration modifiée")
    return jsonify(store.config())


@app.get("/api/log")
def get_log():
    try:
        n = max(1, min(int(request.args.get("limit", DEFAULT_LOG_LIMIT)), MAX_LOG_LIMIT))
    except ValueError:
        n = DEFAULT_LOG_LIMIT
    return jsonify(store.q("SELECT * FROM log ORDER BY id DESC LIMIT ?", (n,)))


@app.get("/api/proposals")
def proposals():
    st = request.args.get("status")
    sql = "SELECT id,title,status,created,decided,total,summary,note FROM proposals"  # hits/no_archive : seulement dans le détail
    if st:
        rows = store.q(sql + " WHERE status=? ORDER BY id DESC LIMIT ?", (st, MAX_PROPOSALS_LIMIT))
    else:
        rows = store.q(sql + " ORDER BY id DESC LIMIT ?", (MAX_PROPOSALS_LIMIT,))
    return jsonify(rows)


@app.get("/api/proposals/<int:pid>")
def proposal(pid):
    r = store.q("SELECT * FROM proposals WHERE id=?", (pid,))
    if not r:
        return jsonify(error="introuvable"), 404
    r = r[0]
    r.pop("new_text", None)
    r["hits"] = json.loads(r["hits"] or "[]")
    r["no_archive"] = json.loads(r["no_archive"] or "[]")
    return jsonify(r)


@app.post("/api/proposals/<int:pid>/approve")
def approve(pid):
    try:
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        cs = body.get("summary")
        cs = str(cs).strip()[:MAX_SUMMARY_CHARS] if isinstance(cs, str) and cs.strip() else None
        return jsonify(eng().save(pid, cs))
    except Halt as e:
        return jsonify(error=f"arrêt : {e}"), 409
    except WikiError as e:
        return jsonify(error=str(e)), 400


@app.post("/api/proposals/<int:pid>/reject")
def reject(pid):
    r = store.q("SELECT status FROM proposals WHERE id=?", (pid,))
    if not r:
        return jsonify(error="introuvable"), 404
    if r[0]["status"] not in ("pending", "dry"):
        return jsonify(error=f"statut {r[0]['status']} : rejet impossible"), 409
    store.x("UPDATE proposals SET status='rejected', decided=? WHERE id=? AND status IN ('pending','dry')", (store.now(), pid))
    return jsonify(ok=True)


@app.route("/api/queue", methods=["GET", "POST", "DELETE"])
def queue():
    if request.method == "POST":
        raw = (request.get_json(silent=True) or {}).get("titles", [])
        raw = raw if isinstance(raw, list) else []
        titles = [t.strip() for t in raw if isinstance(t, str) and t.strip()][:MAX_TITLES_PER_REQUEST]
        for t in titles:
            store.x("INSERT OR REPLACE INTO queue(title,source,manual,added) VALUES(?,?,1,?)", (t, "manuel", store.now()))
        store.log("FILE", f"{len(titles)} article(s) ajouté(s) à la file")
    if request.method == "DELETE":
        store.x("DELETE FROM queue")
    return jsonify(store.q("SELECT * FROM queue ORDER BY manual DESC, id LIMIT ?", (MAX_QUEUE_ITEMS,)))


@app.get("/api/categories/search")
def cat_search():
    try:
        return jsonify(eng().wiki.category_search(request.args.get("q", "")))
    except WikiError as e:
        return jsonify(error=str(e)), 502


@app.get("/api/categories/preview")
def cat_preview():
    name = request.args.get("name", "")
    try:
        depth = max(0, min(int(request.args.get("depth", 0)), MAX_CATEGORY_DEPTH))
    except ValueError:
        depth = 0
    try:
        info = eng().wiki.category_info(name)
        members = eng().wiki.category_members(name, depth, limit=MAX_CATEGORY_MEMBERS) if info["exists"] else []
        return jsonify(info=info, count=len(members), sample=members[:MAX_CATEGORY_SAMPLE])
    except WikiError as e:
        return jsonify(error=str(e)), 502


@app.post("/api/killswitch/check")
def ks_check():
    try:
        trig = eng().wiki.killswitch_check()
    except WikiError as e:
        return jsonify(error=str(e)), 502
    if trig:
        eng().apply_trigger(trig)
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


def _job_put(jid, data):
    store.put("job:" + jid, data)


def _run_test(jid, title):
    try:
        page, res = eng().analyse(title)
        if page is None:
            _job_put(jid, {"done": True, "skipped": res})
            return
        out = {"done": True, "analyzed": res.analyzed, "refs_total": res.refs_total, "dead": res.dead_found, "total": res.total,
               "offline": res.offline, "timed_out": res.timed_out, "no_archive": res.no_archive, "failures": res.failures, "hits": res.hits,
               "report": res.report, "skipped_refs": res.skipped, "summary": res.summary()}
        if res.changed:
            out["proposal_id"] = eng().make_proposal(page, res, "dry")
            out["diff"] = make_diff(page["text"], res.text)
        _job_put(jid, out)
    except Exception as e:  # noqa
        _job_put(jid, {"done": True, "error": f"{type(e).__name__}: {e}"})


@app.post("/api/test")
def test_article():
    title = (request.get_json(silent=True) or {}).get("title", "")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        return jsonify(error="titre requis"), 400
    jid = uuid.uuid4().hex[:12]
    store.x("DELETE FROM kv WHERE k LIKE 'job:%' AND k NOT IN (SELECT k FROM kv WHERE k LIKE 'job:%' ORDER BY rowid DESC LIMIT ?)", (JOB_CLEANUP_KEEP,))
    _job_put(jid, {"done": False, "started": time.time()})
    threading.Thread(target=_run_test, args=(jid, title), daemon=True).start()
    return jsonify(job=jid)


@app.get("/api/test/<jid>")
def test_result(jid):
    j = store.get("job:" + jid)
    if j is None:
        return jsonify(done=True, error="analyse introuvable")
    if not j.get("done") and time.time() - j.get("started", 0) > JOB_TIMEOUT:
        return jsonify(done=True, error="analyse interrompue (le processus a été redémarré ?)")
    return jsonify(j)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def spa(path):
    if path.startswith("api/"):
        return jsonify(error="route inconnue"), 404
    if path and os.path.isfile(os.path.join(DIST, path)):
        return send_from_directory(DIST, path)
    if not os.path.isfile(os.path.join(DIST, "index.html")):
        return "Interface non construite (web/dist manquant).", 503
    return send_from_directory(DIST, "index.html")


if os.environ.get("DRF_EMBED_WORKER") == "1":  # essai local : le worker tourne dans le même processus
    threading.Thread(target=run_worker, daemon=True).start()