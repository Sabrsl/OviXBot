import os, sys, threading, http.server
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from deadlinkfix import archives as A, linkcheck as LC, urltools as U, wikiscan as W
from deadlinkfix.fixer import Fixer, Options, is_eligible

NOW = datetime(2026, 10, 26)
D = "http://old.example/x/page.html"
SNAP = "https://web.archive.org/web/20190601000000/" + D


class Chk:
    def __init__(self, m=None):
        self.m = m or {}
    def check(self, u):
        return self.m.get(u, LC.alive(u))
    def online(self):
        return True


class Prov:
    def __init__(self, ok=True):
        self.ok = ok
    def find_closest(self, u):
        return A.try_build("t", "https://web.archive.org/web/20190601000000/" + u, u) if self.ok else None


DEAD = {D: LC.dead(D, 404, "HTTP 404 confirmé")}
OPT = dict(host_delay=0)


def run(text, checks=DEAD, snap=True, **kw):
    return Fixer(Chk(checks), [Prov(snap)], lambda: NOW).apply(text, Options(**OPT, **kw))


def test_lien_web_archive_et_brise():
    r = run(f"A<ref>{{{{Lien web|url={D}|titre=T|consulté le=1 mai 2020}}}}</ref>")
    assert r.changed and f"archive-url={SNAP}" in r.text and "archive-date=1er juin 2019" in r.text and "brisé le=26 octobre 2026" in r.text
    assert "consulté le=1 mai 2020" in r.text and r.archived == 1 and r.marked == 1 and r.total == 1
    assert r.summary() == "Liens externes des références : 1 archive ajoutée, 1 lien brisé signalé"


def test_article_jamais_brise_le():
    r = run(f"<ref>{{{{Article|titre=T|lire en ligne={D}}}}}</ref>")
    assert "archiveurl=" in r.text and "archivedate=1er juin 2019" in r.text and "brisé le" not in r.text and r.marked == 0
    # archive déjà présente dans un Article : aucune modification (pas de « brisé le »)
    r2 = run(f"<ref>{{{{Article|lire en ligne={D}|archiveurl={SNAP}|archivedate=1er juin 2019}}}}</ref>")
    assert not r2.changed


def test_ouvrage_intact():
    assert not run(f"<ref>{{{{Ouvrage|lire en ligne={D}}}}}</ref>").changed


def test_vivant_et_indetermine_intacts():
    t = f"<ref>{{{{Lien web|url={D}}}}}</ref>"
    assert not run(t, {}).changed
    assert not run(t, {D: LC.unknown(D, 403, "HTTP 403")}).changed
    r = run(t, {D: LC.unknown(D, 0, "timeout")}); assert not r.changed and r.unknown == 1


def test_archive_presente_ajoute_brise_le():
    r = run(f"<ref>{{{{Lien web|url={D}|archive-url={SNAP}|archive-date=1er juin 2019}}}}</ref>")
    assert f"|url={D}|brisé le=26 octobre 2026|archive-url=" in r.text and r.archived == 0 and r.marked == 1


def test_archive_sans_date_ne_duplique_pas_lurl():
    r = run(f"<ref>{{{{Lien web|url={D}|archive-url={SNAP}}}}}</ref>")
    assert r.text.count("web.archive.org") == 1 and "archive-date=1er juin 2019" in r.text and "brisé le=" in r.text


def test_sans_capture():
    r = run(f"<ref>{{{{Lien web|url={D}}}}}</ref>", snap=False)
    assert not r.changed and r.dead_found == 1 and len(r.no_archive) == 1


def test_capture_dune_autre_page_refusee():
    class Bad:
        def find_closest(self, u):
            return A.try_build("t", "https://web.archive.org/web/20190601000000/http://old.example/autre", u)
    r = Fixer(Chk(DEAD), [Bad()], lambda: NOW).apply(f"<ref>{{{{Lien web|url={D}}}}}</ref>", Options(**OPT))
    assert not r.changed


def test_idempotent():
    r = run(f"<ref>{{{{Lien web|url={D}|titre=T}}}}</ref>")
    assert not run(r.text).changed


def test_deja_marque_et_brise_le_vide():
    base = f"<ref>{{{{Lien web|url={D}|archive-url={SNAP}|archive-date=1er juin 2019|"
    assert not run(base + "brisé le=1 mai 2025}}</ref>").changed
    assert not run(base + "dead-url=yes}}</ref>").changed
    assert run(base + "brisé le=}}</ref>").text.endswith("|brisé le=26 octobre 2026}}</ref>")


def test_ignores():
    for t in (f"<!--<ref>{{{{Lien web|url={D}}}}}</ref>-->", f"{{{{Lien web|url={D}}}}}", f"<ref>{{{{Lien web|url={D}|url={D}}}}}</ref>",
              f"<ref><nowiki>{{{{Lien web|url={D}}}}}</nowiki></ref>", f"<ref>{D}</ref>", "<ref>{{Lien web|url=http://localhost/x}}</ref>",
              "<ref>{{Lien web|url=https://web.archive.org/web/2019/http://a.fr}}</ref>"):
        assert not run(t).changed, t


def test_mise_en_forme_multiligne():
    r = run(f"<ref>{{{{Lien web\n | url = {D}\n | titre = T\n}}}}</ref>")
    assert f"| url = {D}\n |archive-url={SNAP}\n |archive-date=1er juin 2019\n |brisé le=26 octobre 2026\n | titre = T" in r.text


def test_limite_et_hors_ligne():
    t = "".join(f"<ref>{{{{Lien web|url=http://s{i}.example/p}}}}</ref>" for i in range(5))
    r = run(t, max_links=2)
    assert r.analyzed == 2 and r.skipped_over_limit == 3
    f = Fixer(Chk(DEAD), [Prov()], lambda: NOW, online=lambda: False)
    r = f.apply(f"<ref>{{{{Lien web|url={D}}}}}</ref>", Options(**OPT))
    assert r.offline and not r.changed


def test_eligible():
    assert is_eligible("Paris", "x") and not is_eligible("Utilisateur:X", "x") and not is_eligible("A", "#REDIRECTION [[B]]")


def test_urltools_et_wikiscan():
    assert U.same_resource("http://www.a.fr/x/index.html#f", "https://a.fr/x/")
    assert not U.same_resource("http://a.fr/x", "http://a.fr/y")
    assert not U.is_checkable(U.parse_http("http://192.168.1.2/x")) and U.is_checkable(U.parse_http("https://exemple.fr/x"))
    m = W.mask("a<!--{{Lien web|url=http://c.fr}}-->{{Lien web|url=http://b.fr}}")
    assert [t.name for t in W.find_templates(m, "a<!--{{Lien web|url=http://c.fr}}-->{{Lien web|url=http://b.fr}}")] == ["lien web"]


def test_wayback_json():
    w = A.WaybackProvider(fetch=lambda u: '{"archived_snapshots":{"closest":{"available":true,"status":"200","url":"http://web.archive.org/web/20190601000000/http://a.fr/x"}}}')
    s = w.find_closest("http://a.fr/x")
    assert s and s.archive_url.startswith("https://web.archive.org/web/2019") and s.date.year == 2019
    assert A.WaybackProvider(fetch=lambda u: '{"archived_snapshots":{}}').find_closest("http://a.fr/x") is None


class _H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path
        if p == "/ok": self.send_response(200)
        elif p == "/r": self.send_response(301); self.send_header("Location", "/ok")
        elif p == "/loop": self.send_response(302); self.send_header("Location", "/loop2")
        elif p == "/loop2": self.send_response(302); self.send_header("Location", "/loop")
        elif p == "/403": self.send_response(403)
        elif p == "/500": self.send_response(500)
        elif p == "/botonly404": self.send_response(404 if "DeadLinkFix" in self.headers.get("User-Agent", "") else 200)
        else: self.send_response(404)
        self.send_header("Content-Length", "0"); self.end_headers()


def test_http_checker_serveur_local():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    b = f"http://127.0.0.1:{srv.server_port}"
    c = LC.HttpLinkChecker(timeout=5, retries=0, allow_private=True)
    assert c.check(b + "/ok").state == LC.ALIVE
    r = c.check(b + "/r"); assert r.state == LC.ALIVE and r.redirects == 1
    assert c.check(b + "/nope").state == LC.DEAD
    assert c.check(b + "/botonly404").state == LC.ALIVE      # 404 non confirmé avec un autre User-Agent
    for p in ("/403", "/500", "/loop"):
        assert c.check(b + p).state == LC.UNKNOWN, p
    assert LC.HttpLinkChecker(retries=0).check(b + "/ok").state == LC.UNKNOWN   # hôte local : jamais contacté (SSRF)
    srv.shutdown()


def test_dns_inexistant_mort_seulement_si_en_ligne():
    c = LC.HttpLinkChecker(retries=0, online_probe=lambda: True)
    c._resolve = staticmethod(lambda h: LC._Probe(error="gaierror", dns=True))
    assert c.check("http://zzz.invalid/x").state == LC.DEAD
    c2 = LC.HttpLinkChecker(retries=0, online_probe=lambda: False)
    c2._resolve = staticmethod(lambda h: LC._Probe(error="gaierror", dns=True))
    assert c2.check("http://zzz.invalid/x").state == LC.UNKNOWN


if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("ok", k)
    print(n, "tests OK")
