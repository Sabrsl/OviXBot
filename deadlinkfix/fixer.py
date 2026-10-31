"""Moteur DeadLinkFix (port Python de DeadLinkFixEngine.cs) : remplace l'ancienne logique DeadRefFix.

Philosophie : vérifier d'abord, réparer ensuite.
  * lien vivant ou indéterminé (403, 429, 5xx, timeout…) → rien ;
  * lien CONFIRMÉ mort (404/410 confirmés, domaine inexistant) → recherche d'une archive validée (même ressource) ;
  * archive ajoutée / déjà présente → « brisé le » (UNIQUEMENT dans {{Lien web}}, jamais dans {{Article}} ni {{Ouvrage}}) ;
  * « consulté le » n'est jamais lu, ajouté ni modifié.
"""
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import groupby

from . import archives as ARC
from . import linkcheck as LC
from . import urltools as U
from . import wikiscan as W

MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
NON_MAIN = re.compile(
    r"^\s*(?:Discussion|Utilisateur(?:rice)?|Discussion utilisateur(?:rice)?|Wikipédia|Discussion Wikipédia|Fichier|Discussion fichier|"
    r"MediaWiki|Discussion MediaWiki|Modèle|Discussion modèle|Aide|Discussion aide|Catégorie|Discussion catégorie|Portail|"
    r"Discussion Portail|Projet|Discussion Projet|Référence|Discussion référence|Module|Discussion module|Gadget|Discussion gadget|"
    r"Définition de gadget|Discussion définition de gadget|Sujet|Spécial|Média|Wikipedia|User|Talk|File|Template|Category|Help|Project)\s*:", re.I)
REDIRECT = re.compile(r"^\s*#(?:REDIRECTION|REDIRECT)\b", re.I)
KEEP, REPAIR, DEAD, UNKNOWN = "keep", "repair", "dead", "unknown"


def is_eligible(title, text):
    if not text:
        return False
    if title and NON_MAIN.match(title):
        return False
    return not REDIRECT.match(text)


def french_date(d):
    return ("1er" if d.day == 1 else str(d.day)) + f" {MONTHS[d.month - 1]} {d.year}"


class Options:
    def __init__(self, **kw):
        self.max_links = 50          # liens distincts vérifiés par article
        self.max_parallelism = 6     # hôtes vérifiés en parallèle (une file séquentielle par hôte)
        self.max_seconds = 240       # budget de temps par article
        self.host_delay = 0.5        # politesse entre deux requêtes vers le même hôte
        self.fix_http_to_https = False
        self.repair_redirects = False
        self.add_archive = True
        self.mark_broken = True
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError("option inconnue : " + k)
            setattr(self, k, v)


class Spec:
    def __init__(self, name, url_params, arc_url=(), arc_date=(), mark_broken=False):
        self.name, self.url_params, self.arc_url, self.arc_date, self.mark_broken = name, url_params, arc_url, arc_date, mark_broken
        self.supports_archive = bool(arc_url)
        self.default_arc_url = arc_url[0] if arc_url else None
        self.default_arc_date = arc_date[0] if arc_date else None


# « brisé le » n'existe que dans {{Lien web}} : {{Article}} reçoit archiveurl/archivedate mais JAMAIS « brisé le ».
SPECS = {s.name: s for s in (
    Spec("lien web", ("url",), ("archive-url", "archiveurl"), ("archive-date", "archivedate"), mark_broken=True),
    Spec("ouvrage", ("lire en ligne", "présentation en ligne", "écouter en ligne")),
    Spec("article", ("lire en ligne",), ("archiveurl", "archive-url"), ("archivedate", "archive-date")),
)}


class Decision:
    def __init__(self, kind, reason, new_url=None):
        self.kind, self.reason, self.new_url, self.snapshot = kind, reason, new_url, None


def analyze(url, r, opt):
    """Résultat de vérification → décision (réparation de redirection uniquement si prouvée)."""
    if r is None:
        return Decision(UNKNOWN, "pas de résultat")
    if r.state == LC.UNKNOWN:
        return Decision(UNKNOWN, r.reason)
    if r.state == LC.DEAD:
        return Decision(DEAD, r.reason)
    if r.redirects > 0 and r.final_url:
        orig, final = U.parse_http(url), U.parse_http(r.final_url)
        if orig and final:
            if opt.fix_http_to_https and orig.scheme == "http" and final.scheme == "https" and U.canonical(orig) == U.canonical(final):
                return Decision(REPAIR, "http → https identique", r.final_url)
            if opt.repair_redirects and U.same_site(orig, final):
                a, b = U.slug_key(orig), U.slug_key(final)
                if len(a) >= 6 and a == b:
                    return Decision(REPAIR, "redirection même slug", r.final_url)
                if any(len(i) >= 5 and i in U.numeric_ids(final) for i in U.numeric_ids(orig)):
                    return Decision(REPAIR, "redirection même identifiant", r.final_url)
                if U.canonical_path(orig) == U.canonical_path(final) and U.canonical_query(orig) == U.canonical_query(final):
                    return Decision(REPAIR, "redirection même chemin", r.final_url)
    return Decision(KEEP, "lien vivant")


class Site:
    __slots__ = ("offset", "t", "spec", "up", "url", "body")

    def has_archive(self):
        return any(p.name in self.spec.arc_url and p.value for p in self.t.params)

    def has_archive_date(self):
        return any(p.name in self.spec.arc_date and p.value for p in self.t.params)

    def is_marked(self):
        for p in self.t.params:
            if p.name == "brisé le" and p.value:
                return True
            if p.name == "dead-url" and p.value.lower() in ("yes", "oui", "true"):
                return True
        return False

    def archive_url_value(self):
        for p in self.t.params:
            if p.name in self.spec.arc_url and p.value:
                return p.value
        return ""


class Result:
    def __init__(self, text):
        self.original = self.text = text
        self.hits, self.failures, self.no_archive, self.report, self.skipped = [], [], [], [], []
        self.analyzed = self.refs_total = self.dead_found = self.total = 0
        self.repaired = self.archived = self.marked = self.dead_unresolved = self.unknown = self.skipped_over_limit = 0
        self.offline = self.timed_out = False
        self.analysis_id = uuid.uuid4().hex[:12]
        self.analysis_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def changed(self):
        return self.text != self.original

    def summary(self):
        if not self.changed:
            return ""
        return "bot réparation de liens morts - test v2"


class _Edit:
    __slots__ = ("start", "end", "seq", "text")

    def __init__(self, start, end, seq, text):
        self.start, self.end, self.seq, self.text = start, end, seq, text


class Fixer:
    def __init__(self, checker, providers=(), clock=None, online=None):
        self.checker, self.providers = checker, list(providers)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.online = online

    # ------------------------------------------------------------------ collecte
    def _collect(self, text, masked, res):
        sites = []
        refs = W.find_refs(masked)
        res.refs_total = len(refs)
        for bs, be in refs:
            body, mbody = text[bs:be], masked[bs:be]
            for t in W.find_templates(mbody, body):
                spec = SPECS.get(t.name)
                if spec is None:
                    continue
                for pname in spec.url_params:
                    ms = [p for p in t.params if p.name == pname]
                    if len(ms) != 1:      # absent ou ambigu (doublon) : on ne touche à rien
                        continue
                    p = ms[0]
                    if not p.value:
                        continue
                    why = None
                    if p.protected:
                        why = "commentaire ou nowiki dans la valeur"
                    elif "{{" in p.value:
                        why = "modèle dans l'URL"
                    elif any(c.isspace() for c in p.value):
                        why = "espace dans l'URL"
                    else:
                        u = U.parse_http(p.value)
                        if u is None:
                            why = "URL non HTTP(S)"
                        elif not U.is_checkable(u):
                            why = "hôte local ou service d'archive"
                    if why:
                        res.skipped.append({"url": p.value[:200], "why": why})
                        continue
                    s = Site()
                    s.offset, s.t, s.spec, s.up, s.url, s.body = bs, t, spec, p, p.value, body
                    sites.append(s)
        return sites

    # ------------------------------------------------------------------ évaluation d'une URL
    def _evaluate(self, url, opt, want_archive):
        try:
            try:
                r = self.checker.check(url)
            except Exception as e:  # noqa
                r = LC.unknown(url, 0, "exception : " + type(e).__name__)
            d = analyze(url, r, opt)
            # Archive cherchée SEULEMENT si le lien est CONFIRMÉ mort (jamais sur un lien vivant ou indéterminé).
            if d.kind == DEAD and want_archive and (opt.add_archive or opt.mark_broken):
                for p in self.providers:
                    try:
                        snap = p.find_closest(url)
                    except Exception:  # noqa
                        snap = None
                    if snap is not None and U.same_resource(snap.original_url, url):
                        d.snapshot = snap
                        break
            return d
        except Exception as e:  # noqa
            return Decision(UNKNOWN, "erreur interne : " + type(e).__name__)

    # ------------------------------------------------------------------ point d'entrée
    def apply(self, text, options=None, title=None):
        opt = options or Options()
        res = Result(text)
        if "<ref" not in text.lower():
            return res
        masked = W.mask(text)
        sites = self._collect(text, masked, res)
        if not sites:
            return res

        all_urls = list(dict.fromkeys(s.url for s in sites))
        urls = all_urls[:max(1, int(opt.max_links))]
        res.skipped_over_limit = len(all_urls) - len(urls)
        for u in all_urls[len(urls):]:
            res.report.append({"url": u, "state": "ignoré", "reason": f"plus de {opt.max_links} liens"})
        res.analyzed = len(urls)
        if self.online is not None and not self.online():
            res.offline = True
            return res

        # L'archive n'est utile que si un modèle n'en a pas, ou n'a pas de date récupérable.
        want_archive = set()
        for s in sites:
            if s.spec.supports_archive and (not s.has_archive() or (not s.has_archive_date() and not ARC.snapshot_date(s.archive_url_value()))):
                want_archive.add(s.url)
            elif s.spec.mark_broken and not s.has_archive():
                want_archive.add(s.url)

        decisions, deadline, lock = {}, time.monotonic() + max(10, int(opt.max_seconds)), threading.Lock()
        by_host = {}
        for u in urls:
            pu = U.parse_http(u)
            by_host.setdefault(U.host_no_www(pu) if pu else u, []).append(u)

        def work(group):
            for i, u in enumerate(group):
                if time.monotonic() > deadline:
                    res.timed_out = True
                    return
                if i and opt.host_delay:
                    time.sleep(opt.host_delay)
                d = self._evaluate(u, opt, u in want_archive)
                with lock:
                    decisions[u] = d

        with ThreadPoolExecutor(max_workers=max(1, int(opt.max_parallelism))) as ex:
            list(ex.map(work, by_host.values()))

        for u in urls:
            d = decisions.get(u)
            if d is None:
                res.report.append({"url": u, "state": "ignoré", "reason": "temps dépassé"})
                continue
            state = {REPAIR: "corrigé", DEAD: "mort", UNKNOWN: "indéterminé"}.get(d.kind, "vivant")
            res.report.append({"url": u, "state": state, "reason": d.reason + (f" → {d.new_url}" if d.new_url else ""),
                               "archive": d.snapshot.archive_url if d.snapshot else None})
            if d.kind == UNKNOWN:
                res.unknown += 1
                if len(res.failures) < 25:
                    res.failures.append(f"{u} — {d.reason}")
            if d.kind == DEAD:
                res.dead_found += 1

        edits, seq, touched, dead_counted = [], [0], set(), set()
        today = french_date(self.clock())

        def add(start, end, txt):
            edits.append(_Edit(start, end, seq[0], txt))
            seq[0] += 1

        for s in sites:
            d = decisions.get(s.url)
            if d is None:
                continue
            before = len(edits)
            if d.kind == REPAIR and d.new_url:
                add(s.offset + s.up.vstart, s.offset + s.up.vend, d.new_url)
                res.repaired += 1
                res.hits.append({"url": s.url, "action": "lien corrigé", "detail": d.new_url})
            if d.kind == DEAD:
                has_arc = s.has_archive()
                if s.spec.supports_archive and opt.add_archive:
                    if has_arc and not s.has_archive_date():
                        dt = ARC.snapshot_date(s.archive_url_value())   # date lue dans l'archive DÉJÀ présente (jamais dupliquer l'URL)
                        if dt:
                            self._add_archive_params(s, None, french_date(dt), add)
                            res.archived += 1
                            res.hits.append({"url": s.url, "action": "date d'archive ajoutée", "detail": french_date(dt)})
                    elif not has_arc and d.snapshot is not None:
                        self._add_archive_params(s, d.snapshot.archive_url, french_date(d.snapshot.date), add)
                        has_arc = True
                        res.archived += 1
                        res.hits.append({"url": s.url, "action": "archive ajoutée", "detail": d.snapshot.archive_url})
                if has_arc and opt.mark_broken and s.spec.mark_broken and not s.is_marked():
                    empty = next((p for p in s.t.params if p.name == "brisé le"), None)
                    if empty is not None:
                        add(s.offset + empty.vstart, s.offset + empty.vstart, today)
                    else:
                        add(s.offset + s.up.vend, s.offset + s.up.vend, self._sep(s) + "|brisé le=" + today)
                    res.marked += 1
                    res.hits.append({"url": s.url, "action": "brisé le", "detail": today})
                elif not has_arc and s.url not in dead_counted:
                    dead_counted.add(s.url)
                    res.dead_unresolved += 1
                    res.no_archive.append(f"{s.url} — lien mort, aucune capture validée")
            if len(edits) > before:
                touched.add(id(s))

        if not edits:
            return res
        out = text
        for e in sorted(edits, key=lambda e: (e.start, e.seq), reverse=True):
            out = out[:e.start] + e.text + out[e.end:]
        res.text, res.total = out, len(touched)
        return res

    # ------------------------------------------------------------------ insertion des paramètres d'archive
    @staticmethod
    def _sep(s):
        """Espaces / retours à la ligne qui précèdent le « | » du paramètre URL : réutilisés pour garder la mise en forme."""
        pipe = s.up.seg_start - 1
        k = pipe - 1
        while k >= 0 and s.body[k].isspace():
            k -= 1
        return s.body[k + 1:pipe]

    def _add_archive_params(self, s, arc_url, arc_date, add):
        appends = []
        pu = next((p for p in s.t.params if p.name in s.spec.arc_url), None)
        pd = next((p for p in s.t.params if p.name in s.spec.arc_date), None)
        if arc_url is not None:
            if pu is not None:
                if not pu.value:
                    add(s.offset + pu.vstart, s.offset + pu.vstart, arc_url)
            else:
                appends.append(s.spec.default_arc_url + "=" + arc_url)
        if pd is not None:
            if not pd.value:
                add(s.offset + pd.vstart, s.offset + pd.vstart, arc_date)
        else:
            appends.append(s.spec.default_arc_date + "=" + arc_date)
        if appends:
            pre = self._sep(s)
            add(s.offset + s.up.vend, s.offset + s.up.vend, "".join(pre + "|" + a for a in appends))


def default_providers(user_agent, use_archive_ph=True):
    p = [ARC.WaybackProvider(user_agent=user_agent)]
    if use_archive_ph:
        p.append(ARC.MementoProvider(user_agent=user_agent))
    return p
