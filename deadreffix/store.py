"""Stockage SQLite (état, configuration, file d'attente, propositions, journal)."""
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

from . import config as C

_lock = threading.RLock()
_conn = None


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(os.path.join(C.DATA_DIR, "ovixbot.db"), timeout=30, check_same_thread=False, isolation_level=None)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA busy_timeout=30000")
            _conn.execute("PRAGMA journal_mode=DELETE")  # WAL non supporté sur NFS (Toolforge)
            _conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT UNIQUE, source TEXT, manual INTEGER DEFAULT 0, added TEXT);
            CREATE TABLE IF NOT EXISTS processed(title TEXT PRIMARY KEY, checked TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, status TEXT, created TEXT, decided TEXT,
              revid INTEGER, basets TEXT, summary TEXT, total INTEGER, diff TEXT, new_text TEXT, hits TEXT, no_archive TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS edits(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, epoch REAL, title TEXT, revid INTEGER, total INTEGER);
            CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, page TEXT, event TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS analysis_log(id TEXT PRIMARY KEY, title TEXT, revid INTEGER, timestamp TEXT, data TEXT);
            """)
        return _conn


def q(sql, args=()):
    with _lock:
        return [dict(r) for r in db().execute(sql, args).fetchall()]


def x(sql, args=()):
    with _lock:
        cur = db().execute(sql, args)
        return cur.lastrowid


def get(key, default=None):
    r = q("SELECT v FROM kv WHERE k=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r[0]["v"])
    except Exception:
        return default


def put(key, value):
    x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value)))


def config():
    cfg = dict(C.DEFAULT_CONFIG)
    cfg.update(get("config", {}) or {})
    # Sécurité : robot désactivé si identifiants de publication absents
    if not (C.OAUTH_TOKEN or (C.BOT_USER and C.BOT_PASSWORD)):
        cfg["enabled"] = False
    return cfg


def save_config(new):
    cfg = config()
    for k in C.DEFAULT_CONFIG:
        if k in new:
            cfg[k] = new[k]
    put("config", cfg)
    return cfg


def log(event, detail="", page="", level="info"):
    x("INSERT INTO log(ts,level,page,event,detail) VALUES(?,?,?,?,?)", (now(), level, page, event, str(detail)[:2000]))
    x("DELETE FROM log WHERE id < (SELECT MAX(id) FROM log) - 5000")


def edits_since(seconds):
    return q("SELECT COUNT(*) n FROM edits WHERE epoch > ?", (time.time() - seconds,))[0]["n"]


def record_edit(title, revid, total):
    x("INSERT INTO edits(ts,epoch,title,revid,total) VALUES(?,?,?,?,?)", (now(), time.time(), title, revid, total))
