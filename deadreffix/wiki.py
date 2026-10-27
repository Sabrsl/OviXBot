"""Client de l'API MediaWiki : connexion, lecture, catégories, modification, arrêt d'urgence."""
import re
import time
from datetime import datetime, timezone

import requests

from . import config as C
from . import store


class WikiError(Exception):
    pass


class Halt(WikiError):
    """Condition d'arrêt (blocage, perte du statut, maxlag persistant…)."""


class Wiki:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = C.USER_AGENT
        self.logged = False
        self.csrf = None
        self._last_edit = 0.0

    # --------------------------------------------------------- bas niveau
    def call(self, params, post=False, retries=4):
        p = dict(params, format="json", formatversion="2", maxlag="5")
        for attempt in range(retries):
            try:
                r = (self.s.post if post else self.s.get)(C.API, **({"data": p} if post else {"params": p}), timeout=30)
            except requests.RequestException as e:
                time.sleep(2 * (attempt + 1))
                if attempt == retries - 1:
                    raise WikiError(f"réseau : {e}")
                continue
            if r.status_code in (429, 503):
                time.sleep(int(r.headers.get("Retry-After", "10")) + 1)
                continue
            r.raise_for_status()
            j = r.json()
            err = j.get("error")
            if err and err.get("code") == "maxlag":
                time.sleep(min(int(r.headers.get("Retry-After", "5")) + 1, 30))
                continue
            if err:
                raise WikiError(f"{err.get('code')}: {err.get('info')}")
            return j
        raise WikiError("maxlag/limitation persistants")

    def login(self):
        if C.OAUTH_TOKEN:
            self.s.headers["Authorization"] = "Bearer " + C.OAUTH_TOKEN
            self.logged = True
            return
        if not C.BOT_USER or not C.BOT_PASSWORD:
            raise WikiError("identifiants manquants (DRF_BOT_USER / DRF_BOT_PASSWORD ou DRF_OAUTH_TOKEN)")
        tok = self.call({"action": "query", "meta": "tokens", "type": "login"})["query"]["tokens"]["logintoken"]
        r = self.call({"action": "login", "lgname": C.BOT_USER, "lgpassword": C.BOT_PASSWORD, "lgtoken": tok}, post=True)
        if r.get("login", {}).get("result") != "Success":
            raise WikiError("connexion refusée : " + str(r.get("login", {}).get("reason", "?")))
        self.logged = True

    def ensure(self):
        if not self.logged:
            self.login()

    def token(self):
        self.ensure()
        if not self.csrf:
            self.csrf = self.call({"action": "query", "meta": "tokens"})["query"]["tokens"]["csrftoken"]
        return self.csrf

    def userinfo(self):
        self.ensure()
        u = self.call({"action": "query", "meta": "userinfo", "uiprop": "groups|blockinfo|hasmsg|rights"})["query"]["userinfo"]
        return {"name": u.get("name"), "groups": u.get("groups", []), "blocked": "blockid" in u, "block_reason": u.get("blockreason"),
                "hasmsg": bool(u.get("messages")), "is_bot": "bot" in u.get("groups", []), "anon": bool(u.get("anon"))}

    # --------------------------------------------------------- lecture
    def page(self, title):
        j = self.call({"action": "query", "prop": "revisions", "titles": title, "rvprop": "ids|timestamp|content|contentmodel",
                       "rvslots": "main"})
        pg = j["query"]["pages"][0]
        if pg.get("missing"):
            return None
        rv = pg["revisions"][0]
        return {"title": pg["title"], "ns": pg["ns"], "revid": rv["revid"], "ts": rv["timestamp"], "text": rv["slots"]["main"]["content"],
                "model": rv["slots"]["main"].get("contentmodel")}

    def category_members(self, cat, depth=0, limit=500, _seen=None):
        """Articles (ns 0) d'une catégorie, avec sous-catégories jusqu'à `depth`."""
        if not cat.lower().startswith(("catégorie:", "category:")):
            cat = "Catégorie:" + cat
        seen = _seen if _seen is not None else set()
        if cat in seen:
            return []
        seen.add(cat)
        out, sub, cont = [], [], {}
        while len(out) < limit:
            j = self.call(dict({"action": "query", "list": "categorymembers", "cmtitle": cat, "cmtype": "page|subcat", "cmlimit": "500"}, **cont))
            for m in j["query"]["categorymembers"]:
                (out if m["ns"] == 0 else sub).append(m["title"])
            if "continue" not in j:
                break
            cont = j["continue"]
        if depth > 0:
            for sc in sub[:50]:
                if len(out) >= limit:
                    break
                out += self.category_members(sc, depth - 1, limit - len(out), seen)
        return out[:limit]

    def category_search(self, prefix):
        j = self.call({"action": "query", "list": "prefixsearch", "pssearch": prefix, "psnamespace": "14", "pslimit": "10"})
        return [{"title": r["title"]} for r in j["query"]["prefixsearch"]]

    def category_info(self, cat):
        if not cat.lower().startswith(("catégorie:", "category:")):
            cat = "Catégorie:" + cat
        j = self.call({"action": "query", "prop": "categoryinfo", "titles": cat})
        pg = j["query"]["pages"][0]
        ci = pg.get("categoryinfo") or {}
        return {"title": pg["title"], "exists": not pg.get("missing"), "pages": ci.get("pages", 0), "subcats": ci.get("subcats", 0)}

    def random_pages(self, n):
        j = self.call({"action": "query", "list": "random", "rnnamespace": "0", "rnlimit": str(min(n, 50)), "rnfilterredir": "nonredirects"})
        return [r["title"] for r in j["query"]["random"]]

    # --------------------------------------------------------- exclusion {{nobots}}
    def allows_bot(self, text):
        for m in re.finditer(r"\{\{\s*(nobots|bots)\s*(\|[^}]*)?\}\}", text, re.I):
            name, args = m.group(1).lower(), (m.group(2) or "")
            if name == "nobots":
                return False
            a = args.lower().replace(" ", "")
            me = C.ACCOUNT.lower()
            if "|allow=none" in a:
                return False
            if "|deny=all" in a:
                return False
            d = re.search(r"\|deny=([^|]*)", a)
            if d and me in d.group(1).split(","):
                return False
            al = re.search(r"\|allow=([^|]*)", a)
            if al and "all" not in al.group(1).split(",") and me not in al.group(1).split(","):
                return False
        return True

    # --------------------------------------------------------- écriture
    def edit(self, title, text, summary, basetimestamp, baserevid, bot=True):
        wait = store.config()["min_edit_interval"] - (time.time() - self._last_edit)
        if wait > 0:
            time.sleep(wait)
        p = {"action": "edit", "title": title, "text": text, "summary": summary, "basetimestamp": basetimestamp, "baserevid": str(baserevid),
             "nocreate": "1", "token": self.token(), "assert": "user"}
        if bot:
            p["bot"] = "1"
            p["minor"] = "1"
        try:
            j = self.call(p, post=True)
        except WikiError as e:
            msg = str(e)
            if re.search(r"blocked|assertuserfailed|assertbotfailed|permissiondenied|readonly", msg, re.I):
                raise Halt(msg)
            raise
        self._last_edit = time.time()
        ed = j.get("edit", {})
        if ed.get("result") != "Success":
            raise WikiError("échec : " + str(ed))
        return ed

    # --------------------------------------------------------- arrêt d'urgence
    def killswitch_check(self):
        """Retourne None, ou un dict décrivant le déclencheur (nouvelle section « Arrêt d'urgence » sur la page de discussion
        postérieure à la dernière reprise manuelle, ou blocage du compte)."""
        resumed = store.get("resumed_at") or "1970-01-01T00:00:00Z"
        # Vérifier les résumés des 50 dernières révisions
        j = self.call({"action": "query", "prop": "revisions", "titles": C.TALK_PAGE, "rvprop": "ids|timestamp|user|comment|tags", "rvlimit": "50"})
        pg = j["query"]["pages"][0]
        me = C.ACCOUNT.lower()
        for r in pg.get("revisions", []) or []:
            if r["timestamp"] <= resumed:
                break
            if (r.get("user") or "").lower() == me:
                continue
            if re.search(r"arr[êe]t d.urgence|kill.?switch", r.get("comment", ""), re.I):
                return {"type": "talk", "revid": r["revid"], "user": r.get("user"), "ts": r["timestamp"], "comment": r.get("comment", "")}
        # Vérifier le contenu de la page de discussion pour BOT-CONTROL: STOP
        try:
            content = self.call({"action": "query", "prop": "revisions", "titles": C.TALK_PAGE, "rvprop": "content", "rvslots": "main", "rvlimit": "1"})
            content_pg = content["query"]["pages"][0]
            if not content_pg.get("missing"):
                page_content = content_pg["revisions"][0]["slots"]["main"]["content"]
                if re.search(r"BOT-CONTROL\s*:\s*STOP", page_content, re.I):
                    last_rev = pg.get("revisions", [{}])[0] if pg.get("revisions") else {}
                    return {"type": "talk", "revid": last_rev.get("revid"), "user": last_rev.get("user"), "ts": last_rev.get("timestamp", store.now()), "comment": "BOT-CONTROL: STOP détecté dans le contenu"}
        except Exception:
            pass
        if self.logged:
            ui = self.userinfo()
            if ui["blocked"]:
                return {"type": "blocked", "reason": ui.get("block_reason") or "compte bloqué", "ts": store.now()}
        return None
