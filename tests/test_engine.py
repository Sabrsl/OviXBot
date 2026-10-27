import os, sys, tempfile
os.environ["DRF_DATA_DIR"] = tempfile.mkdtemp()
os.environ["DRF_ADMIN_TOKEN"] = "secret"
os.environ["DRF_BOT_USER"] = "u@b"; os.environ["DRF_BOT_PASSWORD"] = "p"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
sys.path.insert(0, os.path.dirname(__file__))
from deadreffix import store, engine as E
from deadreffix.wiki import Wiki, Halt
from test_core import Fake, P404, S, D, NOW
from deadreffix.checker import Checker

REVS = []
class FW(Wiki):
    edits = []
    def __init__(self):
        super().__init__(); self.logged = True
    def call(self, p, post=False, retries=4):
        if p.get("prop") == "revisions" and p["titles"].startswith("Discussion utilisateur"):
            return {"query": {"pages": [{"revisions": REVS}]}}
        if p.get("meta") == "userinfo":
            return {"query": {"userinfo": {"name": "OviXBot", "groups": ["bot"]}}}
        raise AssertionError(p)
    def page(self, t):
        return {"title": t, "ns": 0, "revid": 5, "ts": "2026-01-01T00:00:00Z", "text": f"X<ref>{D}</ref>", "model": "wikitext"}
    def edit(self, title, text, summary, ts, rev, bot=True):
        self.edits.append((title, summary)); return {"result": "Success", "newrevid": 6}

def mk(mode):
    e = E.Engine(); e.wiki = FW(); e.fetcher = Fake(P404, S)
    e.fetcher.prefetch = lambda *a, **k: None
    store.save_config({"enabled": True, "mode": mode, "autonomy_confirmed": True, "min_edit_interval": 0, "max_edits_per_hour": 20, "max_edits_per_day": 150}); store.x("DELETE FROM edits")
    store.put("halt", None); FW.edits.clear(); REVS.clear(); e._ks_at = 0
    return e

def patch_checker():
    import deadreffix.engine as m
    orig = m.Checker
    m.Checker = lambda f: Checker(f, lambda: NOW)

def test_dry_never_writes():
    patch_checker(); e = mk("dry"); e.process("Paris")
    assert not FW.edits and store.q("SELECT status FROM proposals ORDER BY id DESC")[0]["status"] == "dry"

def test_auto_writes():
    e = mk("auto"); e.process("Paris")
    assert FW.edits and "OviXBot" in FW.edits[0][1]

def test_supervised_then_approve():
    e = mk("supervised"); e.process("Paris2")
    pid = store.q("SELECT id FROM proposals ORDER BY id DESC")[0]["id"]
    assert not FW.edits; e.save(pid); assert FW.edits

def test_killswitch_blocks():
    e = mk("auto")
    REVS.append({"revid": 9, "timestamp": "2099-01-01T00:00:00Z", "user": "Quelqu'un", "comment": "/* Arrêt d'urgence */ Kill switch STOP"})
    try:
        e.process("Paris3"); e.guard(force=True); assert False
    except Halt: pass
    assert store.get("halt")["type"] == "killswitch" and not FW.edits

def test_resume_ignores_old():
    e = mk("auto")
    REVS.append({"revid": 9, "timestamp": "2020-01-01T00:00:00Z", "user": "Quelqu'un", "comment": "/* Arrêt d'urgence */ x"})
    store.put("resumed_at", "2021-01-01T00:00:00Z")
    e.guard(force=True)   # ne lève pas

def test_own_edit_ignored():
    e = mk("auto")
    REVS.append({"revid": 9, "timestamp": "2099-01-01T00:00:00Z", "user": "OviXBot", "comment": "/* Arrêt d'urgence */ réponse"})
    e.guard(force=True)

def test_killswitch_unverifiable_halts():
    e = mk("auto")
    from deadreffix.wiki import WikiError
    e.wiki.killswitch_check = lambda: (_ for _ in ()).throw(WikiError("réseau"))
    try: e.guard(force=True); assert False
    except Halt: pass

def test_quota():
    e = mk("auto"); store.save_config({"max_edits_per_hour": 1})
    store.x("DELETE FROM edits"); store.record_edit("a", 1, 1)
    assert e.edit_budget(store.config()) <= 0

def test_nobots():
    w = Wiki()
    assert not w.allows_bot("{{nobots}}") and not w.allows_bot("{{bots|deny=all}}") and w.allows_bot("{{bots|deny=AWB}}") and not w.allows_bot("{{bots|deny=OviXBot}}")

def test_api_auth():
    import deadreffix.app as A
    c = A.app.test_client()
    assert c.get("/api/status").status_code == 401
    assert c.post("/api/login", json={"token": "secret"}).status_code == 200
    assert c.put("/api/config", json={"mode": "x"}).status_code == 400
    assert c.get("/api/log").status_code == 200

def test_manual_publish_of_dry():
    import deadreffix.config as C
    C.BOT_USER, C.BOT_PASSWORD = "u@b", "p"
    e = mk("dry"); store.save_config({"enabled": False}); e.process("Manuel")
    pid = store.q("SELECT id FROM proposals ORDER BY id DESC")[0]["id"]
    assert store.q("SELECT status FROM proposals WHERE id=?", (pid,))[0]["status"] == "dry" and not FW.edits
    assert e.save(pid)["result"] == "saved" and FW.edits   # publication manuelle possible même robot désactivé

def test_manual_publish_blocked_by_killswitch():
    import deadreffix.config as C
    C.BOT_USER, C.BOT_PASSWORD = "u@b", "p"
    e = mk("dry"); e.process("Manuel2")
    pid = store.q("SELECT id FROM proposals ORDER BY id DESC")[0]["id"]
    REVS.append({"revid": 9, "timestamp": "2099-01-01T00:00:00Z", "user": "X", "comment": "/* Arrêt d'urgence */"})
    try: e.save(pid); assert False
    except Halt: pass
    assert not FW.edits

def test_report_and_skipped():
    from test_core import run
    r = run(f"<ref>{D}</ref><ref>Dupont, texte http://libre.example/a/b, 2010</ref><ref>{{{{Ouvrage|url=http://o.example/x}}}}</ref>", P404, S)
    assert r.report and r.report[0]["state"] == "mort" and "web.archive.org" in r.report[0]["archive"]
    assert len(r.skipped) == 2 and r.refs_total == 3

def test_async_test_endpoint():
    import time, deadreffix.app as A
    A.eng().wiki = FW(); A.eng().fetcher = Fake(P404, S); A.eng().fetcher.prefetch = lambda *a, **k: None
    c = A.app.test_client(); c.post("/api/login", json={"token": "secret"})
    jid = c.post("/api/test", json={"title": "Paris"}).get_json()["job"]
    for _ in range(50):
        j = c.get("/api/test/" + jid).get_json()
        if j["done"]: break
        time.sleep(0.1)
    assert j.get("proposal_id") and j["report"] and j["diff"], j

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("ok", k)
    print(n, "tests OK")
