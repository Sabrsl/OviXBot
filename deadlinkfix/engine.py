"""Orchestration : traitement d'un article, modes (simulation / supervisé / autonome), garde-fous et arrêt d'urgence."""
import difflib
import json
import random
import time
from datetime import datetime, timedelta, timezone

from . import config as C
from . import store
from .fixer import Fixer, Options, default_providers, is_eligible
from .linkcheck import CachingLinkChecker, HttpLinkChecker, bot_user_agent
from .wiki import Halt, Wiki, WikiError

KS_POLL = 20  # secondes entre deux vérifications de la page de discussion


def make_diff(old, new):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), "avant", "après", n=1))


class Engine:
    def __init__(self):
        self.wiki = Wiki()
        self.fixer = None    # DeadLinkFix (créé à la demande ; remplaçable dans les tests)
        self._ks_at = 0.0
        self._ui = None
        self._ui_at = 0.0

    # ------------------------------------------------------------ arrêt
    def set_halt(self, kind, reason, detail=None):
        h = {"type": kind, "reason": reason, "detail": detail, "ts": store.now()}
        store.put("halt", h)
        store.log("ARRET", f"{kind} — {reason}", level="error")
        return h

    def apply_trigger(self, trig):
        """Pose l'arrêt correspondant à un déclencheur de killswitch_check() (utilisé par guard et par l'API)."""
        if trig["type"] == "talk":
            return self.set_halt("killswitch", f"Arrêt d'urgence demandé par {trig.get('user') or 'inconnu'} ({trig['ts']})", trig)
        return self.set_halt("blocked", trig.get("reason") or "compte bloqué", trig)

    def guard(self, force=False, need_enabled=True):
        """Lève Halt si le robot doit s'arrêter. Vérifie la page de discussion toutes les KS_POLL secondes."""
        h = store.get("halt")
        if h:
            raise Halt(h["reason"])
        if need_enabled and not store.config()["enabled"]:
            raise Halt("robot désactivé")
        if force or time.time() - self._ks_at > KS_POLL:
            try:
                trig = self.wiki.killswitch_check()
            except WikiError as e:
                # impossible de vérifier = on ne modifie pas (sécurité par défaut)
                raise Halt(f"arrêt d'urgence invérifiable : {e}")
            self._ks_at = time.time()
            if trig:
                self.apply_trigger(trig)
                raise Halt("arrêt d'urgence")

    def userinfo(self):
        if self._ui is None or time.time() - self._ui_at > 300:
            self._ui = self.wiki.userinfo()
            self._ui_at = time.time()
        return self._ui

    def get_fixer(self):
        if self.fixer is None:
            chk = CachingLinkChecker(HttpLinkChecker(bot_user_agent(C.ACCOUNT)), ttl=1800)
            self.fixer = Fixer(chk, default_providers(bot_user_agent(C.ACCOUNT)), online=chk.online)
        return self.fixer

    def effective_mode(self, cfg, info=None):
        mode = cfg["mode"]
        if mode == "auto":
            try:
                is_bot = (info or self.userinfo())["is_bot"]
            except WikiError:
                is_bot = False
            # Compte robot autorisé explicitement : publie même sans drapeau bot.
            # Les comptes utilisateurs / AWB (sabrsl…) exigent toujours le drapeau bot.
            ok = cfg["autonomy_confirmed"] and (is_bot or not C.IS_AWB_USER)
            if not ok:
                return "supervised"  # jamais d'autonomie sans confirmation explicite (et drapeau bot pour les comptes utilisateurs)
        return mode

    def edit_budget(self, cfg):
        return min(cfg["max_edits_per_hour"] - store.edits_since(3600), cfg["max_edits_per_day"] - store.edits_since(86400))

    # ------------------------------------------------------------ traitement
    def options(self, cfg):
        return Options(max_links=int(cfg["max_refs"]) or 50, max_seconds=int(cfg["max_seconds"]),
                       add_archive=bool(cfg.get("add_archive", True)), mark_broken=bool(cfg.get("mark_broken", True)))

    def analyse(self, title, cfg=None):
        """Analyse un article sans rien écrire. Retourne (page, résultat) ou (None, raison)."""
        cfg = cfg or store.config()
        page = self.wiki.page(title)
        if page is None:
            return None, "inexistante"
        if not is_eligible(page["title"], page["text"]) or page["ns"] != 0 or page.get("model") != "wikitext":
            return None, "non éligible"
        if not self.wiki.allows_bot(page["text"]):
            return None, "exclusion {{nobots}}/{{bots}}"
        res = self.get_fixer().apply(page["text"], self.options(cfg), page["title"])
        # Enregistrer la traçabilité de l'analyse
        self._record_analysis(page, res)
        return page, res

    def _record_analysis(self, page, res):
        """Trace de l'analyse (base de données)."""
        data = {"analysis_id": res.analysis_id, "timestamp": res.analysis_timestamp, "article_title": page["title"], "article_revid": page["revid"],
                "links_checked": res.analyzed, "dead_found": res.dead_found, "total_changes": res.total, "report": res.report}
        store.x("INSERT OR REPLACE INTO analysis_log(id, title, revid, timestamp, data) VALUES(?,?,?,?,?)",
                (res.analysis_id, page["title"], page["revid"], res.analysis_timestamp, json.dumps(data, ensure_ascii=False)))

    def process(self, title, manual=False):
        cfg = store.config()
        page, res = self.analyse(title, cfg)
        if page is None:
            store.log("IGNORE", res, title)
            store.x("INSERT OR REPLACE INTO processed(title,checked,result) VALUES(?,?,?)", (title, store.now(), res))
            return res
        if res.offline:
            store.log("HORS_LIGNE", "pas de connexion Internet sortante : rien n'est écrit", title, "warn")
            return "offline"
        if res.timed_out:
            store.log("TEMPS_DEPASSE", "références restantes non analysées", title, "warn")
        for line in res.no_archive:
            store.log("SANS_ARCHIVE", line, title)
        for line in res.failures:
            store.log("ECHEC", line, title)
        store.x("INSERT OR REPLACE INTO processed(title,checked,result) VALUES(?,?,?)",
                (title, store.now(), "modifié" if res.changed else f"{res.dead_found} mort(s), rien à corriger"))
        if not res.changed:
            store.log("RIEN", f"{res.analyzed} lien(s) vérifié(s), {res.dead_found} mort(s), aucune correction", title)
            return "none"
        mode = self.effective_mode(cfg)
        pid = self.make_proposal(page, res, "dry" if mode == "dry" else "pending")
        if mode == "auto":
            self.save(pid)
        else:
            store.log("PROPOSITION", f"{res.total} lien(s) corrigé(s) — {mode}", title)
        return "changed"

    def make_proposal(self, page, res, status):
        summary = res.summary()
        return store.x(
            "INSERT INTO proposals(title,status,created,revid,basets,summary,total,diff,new_text,hits,no_archive) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (page["title"], status, store.now(), page["revid"], page["ts"], summary, res.total, make_diff(page["text"], res.text), res.text,
             json.dumps(res.hits, ensure_ascii=False), json.dumps(res.no_archive, ensure_ascii=False)))

    def save(self, pid, custom_summary=None):
        """Publie une proposition (après vérification de l'arrêt d'urgence, des quotas et de l'absence de modification concurrente)."""
        rows = store.q("SELECT * FROM proposals WHERE id=?", (pid,))
        if not rows:
            raise WikiError("proposition introuvable")
        p = rows[0]
        if p["status"] not in ("pending", "approved", "dry"):
            raise WikiError(f"statut {p['status']} : publication impossible")
        if not (C.HAS_CREDENTIALS):
            raise WikiError("aucun identifiant du compte configuré (DRF_BOT_USER / DRF_BOT_PASSWORD) : publication impossible")
        cfg = store.config()
        self.guard(force=True, need_enabled=False)  # publication manuelle : le kill switch reste prioritaire
        if self.edit_budget(cfg) <= 0:
            store.x("UPDATE proposals SET note=? WHERE id=?", ("quota atteint", pid))
            raise WikiError("quota de modifications atteint")
        cur = self.wiki.page(p["title"])
        if cur is None or cur["revid"] != p["revid"]:
            store.x("UPDATE proposals SET status='stale', decided=?, note=? WHERE id=?", (store.now(), "article modifié depuis l'analyse", pid))
            store.log("OBSOLETE", "article modifié entre-temps : proposition abandonnée", p["title"])
            return {"result": "stale"}
        try:
            summary = custom_summary or C.EDIT_SUMMARY
            ed = self.wiki.edit(p["title"], p["new_text"], summary, cur["ts"], cur["revid"], bot=self.userinfo()["is_bot"])
        except Halt as e:
            self.set_halt("wiki", str(e))
            raise
        except WikiError as e:
            store.x("UPDATE proposals SET status='error', decided=?, note=? WHERE id=?", (store.now(), str(e)[:300], pid))
            store.log("ERREUR", str(e), p["title"], "error")
            raise
        store.x("UPDATE proposals SET status='saved', decided=?, note=? WHERE id=?", (store.now(), f"rev {ed.get('newrevid')}", pid))
        store.record_edit(p["title"], ed.get("newrevid"), p["total"])
        store.log("MODIFIE", f"{p['total']} réf. — rev {ed.get('newrevid')}", p["title"])
        return {"result": "saved", "revid": ed.get("newrevid")}

    # ------------------------------------------------------------ sélection
    def _next_chunk(self, c, chunk_size):
        """Prochain lot d'articles d'une catégorie, en reprenant là où on s'était arrêté (état persistant)."""
        depth = int(c.get("depth", 0))
        key = f"cat_state:{c['name']}:{depth}"  # la profondeur fait partie de la clé : changer `depth` repart de zéro
        fresh_state = lambda: {"stack": [[c["name"], depth]], "cont": None, "seen": []}
        st = store.get(key) or fresh_state()
        wrapped = False
        while True:
            if not st["stack"]:
                if wrapped:
                    store.put(key, fresh_state())
                    return []
                wrapped, st = True, fresh_state()
            cat, d = st["stack"][-1]
            try:
                pages, subs, nxt = self.wiki.category_chunk(cat, st["cont"], chunk_size)
            except WikiError as e:
                if st["cont"] and "badcontinue" in str(e).lower():
                    # jeton périmé (catégorie remaniée) : on repart du début au lieu de bloquer la catégorie
                    store.log("ERREUR", f"jeton périmé pour {c['name']}, reprise au début", level="warn")
                    st = fresh_state()
                    store.put(key, st)
                    continue
                raise
            if d > 0:
                for s in subs:
                    if s not in st["seen"] and len(st["seen"]) < 2000:
                        st["seen"].append(s)
                        st["stack"].insert(0, [s, d - 1])
            st["cont"] = nxt
            if nxt is None:
                st["stack"].pop()
            if pages:
                store.put(key, st)
                return pages

    def fill_queue(self, cfg):
        n = int(cfg["batch_size"])
        chunk_size = int(cfg.get("category_chunk", 100))
        max_scan = int(cfg.get("max_scan_per_fill", 2000))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(cfg["recheck_days"]))).strftime("%Y-%m-%dT%H:%M:%SZ")
        seen = {r["title"] for r in store.q("SELECT title FROM processed WHERE checked > ?", (cutoff,))}
        seen |= {r["title"] for r in store.q("SELECT title FROM queue")}
        fresh = []

        def take(t, src):
            if t in seen:
                return False
            seen.add(t)
            fresh.append((t, src))
            return len(fresh) >= n

        done = False
        for t in cfg["titles"]:
            if take(t, "liste"):
                done = True
                break

        scanned = 0
        cats = list(cfg["categories"])
        random.shuffle(cats)
        for c in cats:
            if done or scanned >= max_scan:
                break
            while not done and scanned < max_scan:
                try:
                    members = self._next_chunk(c, chunk_size)
                except WikiError as e:
                    store.log("ERREUR", f"catégorie {c['name']} : {e}", level="warn")
                    break
                if not members:
                    break
                random.shuffle(members)
                scanned += len(members)
                for m in members:
                    if take(m, c["name"]):
                        done = True
                        break

        if cfg["random_pages"] and not fresh:
            for t in self.wiki.random_pages(n):
                if take(t, "hasard"):
                    break

        for t, src in fresh:
            store.x("INSERT OR IGNORE INTO queue(title,source,manual,added) VALUES(?,?,0,?)", (t, src, store.now()))
        return len(fresh)

    def next_titles(self, cfg):
        n = int(cfg["batch_size"])
        if len(store.q("SELECT 1 FROM queue LIMIT 1")) == 0:
            self.fill_queue(cfg)
        rows = store.q("SELECT id,title,manual FROM queue ORDER BY manual DESC, id LIMIT ?", (n,))
        return rows


def in_schedule(cfg, at=None):
    s = cfg["schedule"]
    if not s.get("enabled"):
        return True
    at = at or datetime.now(timezone.utc)
    return at.weekday() in s["days"] and int(s["start_hour"]) <= at.hour < int(s["end_hour"])


def run_worker():
    eng = Engine()
    errors = 0
    store.log("DEMARRAGE", "worker démarré")
    while True:
        try:
            store.put("heartbeat", store.now())
            cfg = store.config()
            if not cfg["enabled"] or store.get("halt"):
                time.sleep(10)
                continue
            if not in_schedule(cfg):
                time.sleep(60)
                continue
            if eng.edit_budget(cfg) <= 0 and eng.effective_mode(cfg) == "auto":
                store.log("QUOTA", "quota atteint, pause de 5 minutes")
                time.sleep(300)
                continue
            eng.guard(force=True)
            rows = eng.next_titles(cfg)
            if not rows:
                time.sleep(max(30, int(cfg["schedule"].get("cycle_pause", 60))))
                continue
            for r in rows:
                eng.guard()
                store.x("DELETE FROM queue WHERE id=?", (r["id"],))
                store.put("heartbeat", store.now())
                try:
                    eng.process(r["title"], bool(r["manual"]))
                    errors = 0
                except Halt:
                    raise
                except Exception as e:  # noqa
                    errors += 1
                    store.log("ERREUR", f"{type(e).__name__}: {e}", r["title"], "error")
                    if errors >= int(cfg["max_errors_before_pause"]):
                        eng.set_halt("errors", f"{errors} erreurs consécutives : pause automatique")
                        errors = 0  # après « Reprendre », on repart d'un compteur propre
                        raise Halt("erreurs")
                if eng.effective_mode(cfg) == "auto" and eng.edit_budget(cfg) <= 0:
                    break
            time.sleep(2)
        except Halt as h:
            store.log("PAUSE", str(h), level="warn")
            time.sleep(15)
        except Exception as e:  # noqa
            store.log("ERREUR", f"boucle : {type(e).__name__}: {e}", level="error")
            time.sleep(30)