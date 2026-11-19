"""Stockage SQLite (état, configuration, file d'attente, propositions, journal)."""
import copy
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import config as C

_lock = threading.RLock()
_conn = None
_ino = None


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path():
    return os.path.join(C.DATA_DIR, "ovixbot.db")


def _stat_ino():
    try:
        return os.stat(_path()).st_ino
    except OSError:
        return None


def _reset():
    global _conn, _ino
    try:
        if _conn is not None:
            _conn.close()
    except Exception:
        pass
    _conn = None
    _ino = None


def db():
    global _conn, _ino
    with _lock:
        if _conn is not None and _stat_ino() != _ino:  # fichier remplacé/renommé
            _reset()
        if _conn is None:
            os.makedirs(C.DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(_path(), timeout=8, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA busy_timeout=8000")
            _conn.execute("PRAGMA journal_mode=DELETE")
            _conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT UNIQUE, source TEXT, manual INTEGER DEFAULT 0, added TEXT);
            CREATE TABLE IF NOT EXISTS processed(title TEXT PRIMARY KEY, checked TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, status TEXT, created TEXT, decided TEXT,
              revid INTEGER, basets TEXT, summary TEXT, total INTEGER, diff TEXT, new_text TEXT, hits TEXT, no_archive TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS edits(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL, title TEXT, revid INTEGER, total INTEGER);
            CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, page TEXT, event TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS analysis_log(id TEXT PRIMARY KEY, title TEXT, revid INTEGER, timestamp TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS idx_processed_checked ON processed(checked);
            CREATE INDEX IF NOT EXISTS idx_edits_epoch ON edits(epoch);
            CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status, id);
            """)
            _ino = _stat_ino()
        return _conn


def _retry(fn):
    """Une seule nouvelle tentative après reconnexion si la base est devenue inutilisable."""
    try:
        return fn()
    except sqlite3.OperationalError as e:
        if "readonly" not in str(e) and "disk I/O" not in str(e):
            raise
        _reset()
        return fn()


def q(sql, args=()):
    with _lock:
        return _retry(lambda: [dict(r) for r in db().execute(sql, args).fetchall()])


def x(sql, args=()):
    with _lock:
        return _retry(lambda: db().execute(sql, args).lastrowid)


def get(key, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r[0]["v"])
    except Exception:
        try:  # valeur illisible : on la signale (sans récursion dans log())
            x("INSERT INTO log(ts,level,page,event,detail) VALUES(?,?,?,?,?)",
              (now(), "warn", "", "KV_ILLISIBLE", f"clé {key} : JSON invalide, valeur ignorée"))
        except Exception:
            pass
        return default


def put(key, value):
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))


def _merge_schedule(base, new):
    s = dict(base)
    s.update({k: v for k, v in (new or {}).items() if v is not None})
    return s


def config():
    cfg = copy.deepcopy(C.DEFAULT_CONFIG)
    saved = get("config", {}) or {}
    cfg.update(saved)
    cfg["schedule"] = _merge_schedule(C.DEFAULT_CONFIG["schedule"], saved.get("schedule"))
    return cfg


def save_config(new):
    with _lock:  # lecture-fusion-écriture atomique (plusieurs PUT rapprochés ne se perdent plus)
        cfg = config()
        for k in C.DEFAULT_CONFIG:
            if k in new:
                if k == "schedule":
                    cfg[k] = _merge_schedule(cfg["schedule"], new[k])
                else:
                    cfg[k] = new[k]
        if cfg.get("mode") != "auto":
            cfg["autonomy_confirmed"] = False  # la confirmation ne survit pas à la sortie du mode autonome
        put("config", cfg)
        return cfg


def log(event, detail="", page="", level="info"):
    rid = x("INSERT INTO log(ts,level,page,event,detail) VALUES(?,?,?,?,?)", (now(), level, page, event, str(detail)[:2000]))
    if rid and rid % 100 == 0:  # purge périodique (au lieu d'une à chaque appel)
        x("DELETE FROM log WHERE id < (SELECT MAX(id) FROM log) - 5000")


def edits_since(seconds):
    return q("SELECT COUNT(*) n FROM edits WHERE epoch > ?", (time.time() - seconds,))[0]["n"]


def record_edit(title, revid, total):
    x("INSERT INTO edits(ts,epoch,title,revid,total) VALUES(?,?,?,?,?)", (now(), time.time(), title, revid, total))