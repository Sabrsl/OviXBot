import json, sys, os
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from deadreffix.checker import Checker, Options, is_eligible
from deadreffix.fetcher import FetchResult, fail

NOW = datetime(2026, 10, 26)
ARCH_HTML = '<html lang="fr"><head><meta property="og:title" content="Un bel article"><meta property="og:site_name" content="Le Journal"><meta property="article:published_time" content="2019-05-04"><meta name="author" content="Jean Dupont"></head></html>'


class Fake:
    def __init__(self, pages, snaps=None, online=True):
        self.pages, self.snaps, self.online = pages, snaps or {}, online
    def get(self, url):
        if url.startswith("https://archive.org/wayback/available?url=wikipedia.org"):
            return FetchResult(True, 200, "none", url, '{"archived_snapshots":{}}') if self.online else fail("off")
        if url.startswith("https://archive.org/wayback/available"):
            for u, ts in self.snaps.items():
                if u.replace(":", "%3A").replace("/", "%2F") in url:
                    return FetchResult(True, 200, "none", url, json.dumps({"archived_snapshots": {"closest": {"available": True, "status": "200", "timestamp": ts, "url": f"http://web.archive.org/web/{ts}/{u}"}}}))
            return FetchResult(True, 200, "none", url, '{"archived_snapshots":{}}')
        if url.startswith("https://web.archive.org/web/"):
            return FetchResult(True, 200, "none", url, ARCH_HTML)
        return self.pages.get(url, fail("DNS", 0, "dns"))
    get_fresh = get


def run(text, pages, snaps, **kw):
    return Checker(Fake(pages, snaps, kw.pop("online", True)), lambda: NOW).apply(text, Options(**kw))

D = "http://old.example/x/page.html"
S = {D: "20190601000000"}
P404 = {D: fail("HTTP 404", 404, "http")}

def test_bare_dead():
    r = run(f"A<ref>{D}</ref>", P404, S)
    assert r.changed and "{{Lien web" in r.text and "archive-url=https://web.archive.org/web/20190601000000/" in r.text
    assert "brisé le=26 octobre 2026" in r.text and "titre=Un bel article" in r.text and "auteur=Jean Dupont" in r.text

def test_alive_untouched():
    t = f"A<ref>{D}</ref>"
    r = run(t, {D: FetchResult(True, 200, "none", D, "<html><title>Hello world page</title></html>")}, S)
    assert not r.changed and r.text == t

def test_template_existing():
    r = run("<ref>{{Lien web|url=%s|titre=T|consulté le=3 mars 2020}}</ref>" % D, P404, S)
    assert r.changed and "archive-date=01/06/2019" in r.text and "titre=T" in r.text

def test_no_archive():
    r = run(f"<ref>{D}</ref>", P404, {})
    assert not r.changed and r.no_archive

def test_403_ignored():
    r = run(f"<ref>{D}</ref>", {D: fail("HTTP 403", 403, "http")}, S)
    assert not r.changed

def test_offline():
    r = run(f"<ref>{D}</ref>", P404, S, online=False)
    assert r.offline and not r.changed

def test_idempotent():
    r = run(f"<ref>{D}</ref>", P404, S)
    r2 = run(r.text, P404, S)
    assert not r2.changed

def test_outside_ref():
    t = f"== Liens externes ==\n* {D}\n<ref>x</ref>"
    assert not run(t, P404, S).changed

def test_nowiki_comment():
    t = f"<!-- <ref>{D}</ref> -->"
    assert not run(t, P404, S).changed

def test_eligible():
    assert is_eligible("Paris", "texte") and not is_eligible("Discussion:Paris", "x") and not is_eligible("Paris", "#REDIRECTION [[X]]")

if __name__ == "__main__":
    n = 0
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f(); n += 1; print("ok", k)
    print(n, "tests OK")
