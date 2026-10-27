"""Détection des liens morts dans les <ref> et ajout de l'archive Wayback — portage de DeadRefChecker.cs."""
import json
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote, urlparse

from . import htmlmeta
from . import textutil as T

RX = re.S | re.I
EMPTY = (0, 0, 0)

RULES = [
    ("DRF01", "Lien mort : erreur HTTP (404, 410, 5xx…, hors 401/403/407/429)", True),
    ("DRF02", "Lien mort : « soft 404 » (titre d'erreur ou redirection vers l'accueil)", True),
    ("DRF03", "Lien mort : domaine injoignable (DNS / connexion refusée)", True),
    ("DRF04", "URL brute dans une <ref> → {{Lien web}} / {{Article}} avec archive", True),
    ("DRF05", "{{Lien web}} / {{Article}} existants : ajouter brisé le, archive-url, archive-date", True),
    ("DRF06", "Compléter les champs manquants d'un {{Lien web}} depuis l'archive", True),
    ("DRF07", "Titre contenant « | » : l'écrire &#124; (sinon la référence est ignorée)", False),
]

URL_BR = r'https?://[^\s\]<>|"{}]+'
URL_BARE = r'https?://[^\s\]<>|"{}]*[^\s\]<>|"{}.,;:!?)]'
PING_URL = "https://archive.org/wayback/available?url=wikipedia.org"

REF_RX = re.compile(r"<ref(?P<attrs>(?:\s[^>]*?)?)(?<!/)>(?P<body>.*?)</ref\s*>", RX)
BODY_RX = re.compile(
    r"^\s*(?:\[\s*(?P<u>" + URL_BR + r")(?:\s+(?P<l>[^\]\n]*?))?\s*\]|(?P<u2>" + URL_BARE + r"))"
    r"(?:[\s,;.]*\(?\s*[Cc]onsult[ée]e?\s+le\s+(?P<cd>[^)\n]{4,30}?)\s*\)?)?[\s.]*$")
CONSULTED_OK = re.compile(r"^(?:\d{1,2}(?:er)?\s+[^\W\d_]+\s+\d{4}|\d{4}-\d{2}-\d{2})$")
DEAD_TEMPLATE = re.compile(
    r"\{\{\s*(?:lien[ _](?:bris[ée]|mort|cass[ée]|archiv[ée]?|web[ _]bris[ée])|webarchive|dead[ _]?link|wayback|bris[ée]|archive[ _]?(?:link|url))\b|Wikip[ée]dia:Liens[ _]bris[ée]s", re.I)
DEAD_PARAMS = ["brisé le", "brisé", "lien brisé", "archive-url", "archive-date", "archiveurl", "archivedate", "archive url", "archive date",
               "lien archive", "date d'archive", "date archive", "url archive", "archive", "format archive"]
SKIP_HOSTS = ["facebook.com", "fb.com", "twitter.com", "x.com", "instagram.com", "tiktok.com", "linkedin.com", "web.archive.org",
              "archive.is", "archive.today", "archive.ph", "wikipedia.org", "wikimedia.org", "wikidata.org", "wikisource.org", "wiktionary.org"]

NON_MAIN = re.compile(
    r"^\s*(?:Discussion|Utilisateur(?:rice)?|Discussion utilisateur(?:rice)?|Wikipédia|Discussion Wikipédia|Fichier|Discussion fichier|MediaWiki|Discussion MediaWiki|Modèle|Discussion modèle|Aide|Discussion aide|Catégorie|Discussion catégorie|Portail|Discussion Portail|Projet|Discussion Projet|Référence|Discussion Référence|Module|Discussion module|Gadget|Discussion gadget|Définition de gadget|Discussion définition de gadget|Sujet|Spécial|Média|Wikipedia|User|Talk|File|Template|Category|Help|Project)\s*:", re.I)
REDIRECT = re.compile(r"^\s*#(?:REDIRECTION|REDIRECT)\b", re.I)


def is_eligible(title, text):
    if not text:
        return False
    if title and NON_MAIN.match(title):
        return False
    return not REDIRECT.match(text)


class Options:
    def __init__(self, **kw):
        self.disabled_rules = {"DRF07"}
        self.max_refs = 0
        self.max_seconds = 240
        self.parallelism = 6
        self.emit_empty = False
        self.terminal_period = True
        for k, v in kw.items():
            setattr(self, k, v)


class Result:
    def __init__(self, text):
        self.text = text
        self.hits, self.failures, self.no_archive = [], [], []
        self.analyzed = self.dead_found = self.total = 0
        self.offline = self.timed_out = False
        self.report, self.skipped, self.refs_total = [], [], 0
        # Traçabilité détaillée
        self.analysis_id = None
        self.analysis_timestamp = None
        self.url_analysis = {}  # Pour chaque URL : état, raison, archive vérifiée, date archive
        self.template_conversions = []  # Conversions de modèles effectuées
        self.dead_links_checked = []  # Liens marqués brisés vérifiés
        self.archives_verified = []  # Archives existantes vérifiées
        self.false_positives = []  # Liens marqués brisés mais vivants

    @property
    def changed(self):
        return self.total > 0

    def summary(self):
        if self.total > 0:
            return "Bot réparation de liens morts"
        else:
            return "Bot signalement de liens morts"


class _Masker:
    COMMENTS = re.compile(r"<!--.*?-->", re.S)
    BLOCKS = re.compile(r"<(?P<n>nowiki|pre|code|math|chem|source|syntaxhighlight|score|gallery|imagemap|timeline|poem|templatedata|hiero|mapframe|maplink|graph)\b[^>]*(?<!/)>.*?</(?P=n)\s*>", RX)
    TOKEN = re.compile("\uE000(\\d+)\uE001")

    def __init__(self):
        self.tokens = []

    def _store(self, m):
        self.tokens.append(m.group(0))
        return f"\uE000{len(self.tokens) - 1}\uE001"

    def mask(self, text):
        return self.BLOCKS.sub(self._store, self.COMMENTS.sub(self._store, text))

    def restore(self, text):
        for _ in range(20):
            new = self.TOKEN.sub(lambda m: self.tokens[int(m.group(1))], text)
            if new == text:
                break
            text = new
        return text


class _Item:
    def __init__(self):
        self.bare = False
        self.url = self.label = self.consulted = None
        self.start = self.end = 0
        self.raw = self.kind = None
        self.t = None
        self.has_dead_template = False
        self.partial_archive = False
        self.full_archive = False
        self.existing_archive_url = None
        self.existing_archive_date = None


def _is_skipped(host):
    return any(host == s or host.endswith("." + s) for s in SKIP_HOSTS)


def _is_dead_status(s):
    return 400 <= s <= 599 and s not in (401, 403, 407, 429)


def _home_or_error_redirect(orig, final):
    try:
        a, b = urlparse(orig), urlparse(final)
    except Exception:
        return False
    if not a.netloc or not b.netloc:
        return False
    if len(a.path.strip("/")) >= 3 and len(b.path.strip("/")) == 0 and a.hostname.replace("www.", "") == b.hostname.replace("www.", ""):
        return True
    return bool(re.search(r"/(?:404|error|erreur|not-?found|page-?introuvable)(?:/|\.|$)", b.path, re.I))


class _Ctx:
    def __init__(self, opts, fetcher, now, res):
        self.o, self.f, self.now, self.r = opts, fetcher, now, res
        self.started = time.monotonic()
        self.dead, self.reason, self.archives, self.rep = {}, {}, {}, {}

    def on(self, rid):
        return rid not in self.o.disabled_rules

    def time_up(self):
        return self.o.max_seconds > 0 and time.monotonic() - self.started > self.o.max_seconds

    def hit(self, rid, label, before, after):
        h = next((x for x in self.r.hits if x["id"] == rid and x["label"] == label), None)
        if h is None:
            h = {"id": rid, "label": label, "count": 0, "examples": []}
            self.r.hits.append(h)
        h["count"] += 1
        if len(h["examples"]) < 3:
            h["examples"].append(_cut(before) + " → " + _cut(after))

    def fail(self, url, why):
        f = f"{url} — {why}"
        if f not in self.r.failures:
            self.r.failures.append(f)


def _cut(s):
    s = s.replace("\n", " ")
    return s[:90] + "…" if len(s) > 90 else s


class Checker:
    def __init__(self, fetcher, clock=None):
        self.f = fetcher
        self.clock = clock or datetime.now

    # ------------------------------------------------------------ point d'entrée
    def apply(self, text, options=None, article_title=None):
        o = options or Options()
        res = Result(text)
        # Initialiser la traçabilité
        res.analysis_id = f"{self.clock().strftime('%Y%m%d%H%M%S')}-{hash(text) % 10000:04d}"
        res.analysis_timestamp = self.clock().isoformat()
        if article_title:
            res.article_title = article_title
        if not text or "\uE000" in text or "\uE001" in text:
            return res
        low = text.lower()
        if "http" not in low or "<ref" not in low:
            return res
        c = _Ctx(o, self.f, self.clock(), res)
        mk = _Masker()
        work = mk.mask(text)

        refs, analysed = [], 0
        for m in REF_RX.finditer(work):
            items = self._items(m.group("body"))
            if not items:
                self._note_skipped(res, m.group("body"))
            if items:
                if o.max_refs > 0 and analysed >= o.max_refs:
                    break
                analysed += 1
            refs.append((m, items))
        res.analyzed = analysed
        res.refs_total = len(refs)
        if analysed == 0:
            return res

        ping = self.f.get(PING_URL)
        if not ping.ok or not ping.body or "archived_snapshots" not in ping.body:
            res.offline = True
            return res

        urls = list(dict.fromkeys(it.url for _, items in refs for it in items))
        if hasattr(self.f, "prefetch"):
            self.f.prefetch(urls, o.parallelism)

        out, pos = [], 0
        for m, items in refs:
            out.append(work[pos:m.start()])
            pos = m.end()
            if not items or c.time_up():
                if items:
                    res.timed_out = True
                out.append(m.group(0))
                continue
            out.append(self._process_ref(c, m, items))
        out.append(work[pos:])
        res.report = list(c.rep.values())
        work = mk.restore("".join(out))
        for h in res.hits:
            h["examples"] = [mk.restore(e) for e in h["examples"]]
        if res.total == 0 or work == text:
            res.hits, res.total, res.text = [], 0, text
        else:
            res.text = work
        return res

    @staticmethod
    def _note_skipped(res, body):
        mu = re.search(r'https?://[^\s\]<>|"{}]+', body)
        if not mu or len(res.skipped) >= 60:
            return
        # Ne plus marquer comme "déjà marquée (lien brisé / archive)" car ils seront maintenant traités
        why = "ignorée : lien dans du texte libre, modèle non géré ({{Ouvrage}}…) ou site exclu (réseaux sociaux, Wayback, Wikimédia)"
        res.skipped.append({"url": mu.group(0), "why": why})

    # ------------------------------------------------------------ extraction
    def _items(self, body):
        items = []
        b = BODY_RX.match(body)
        if b:
            it = _Item()
            it.bare = True
            it.url = (b.group("u") or b.group("u2")).replace("&amp;", "&")
            label = b.group("l")
            if label and label.strip():
                label = T.decode(label.strip())
                if len(label) >= 4 and not label.lower().startswith("http"):
                    it.label = label
            if b.group("cd") and CONSULTED_OK.match(b.group("cd").strip()):
                it.consulted = b.group("cd").strip()
            host = T.host_of(it.url)
            if host and not _is_skipped(host):
                items.append(it)
            return items
        from .textutil import find_top_level, parse_template
        for s, e in find_top_level(body):
            raw = body[s:e]
            t = parse_template(raw)
            n = (t.name or "").replace("_", " ").strip().lower()
            kind = "web" if n == "lien web" else ("article" if n == "article" else None)
            has_dead_template = DEAD_TEMPLATE.search(raw)
            # Vérifier les paramètres d'archive
            has_archive_url = t.has_value("archive-url") or t.has_value("archiveurl") or t.has_value("url archive")
            has_archive_date = t.has_value("archive-date") or t.has_value("archivedate") or t.has_value("date archive")
            # Si c'est un template de lien brisé, on l'extrait pour vérification
            if has_dead_template and not kind:
                # Template de lien brisé (pas Lien web/Article)
                url = None
                for pn in ("url", "lien", "url du lien"):
                    p = t.find(pn)
                    if p and re.match(r"^https?://", p.value, re.I):
                        url = p.value.replace("&amp;", "&")
                        break
                if url is None:
                    continue
                host = T.host_of(url)
                if not host or _is_skipped(host):
                    continue
                it = _Item()
                it.url, it.start, it.end, it.raw, it.t = url, s, e, raw, t
                it.kind = "dead_template"
                it.has_dead_template = True
                it.partial_archive = has_archive_url or has_archive_date
                it.full_archive = has_archive_url and has_archive_date
                if has_archive_url:
                    p = t.find("archive-url") or t.find("archiveurl") or t.find("url archive")
                    if p and p.value:
                        it.existing_archive_url = p.value.replace("&amp;", "&")
                if has_archive_date:
                    p = t.find("archive-date") or t.find("archivedate") or t.find("date archive")
                    if p and p.value:
                        it.existing_archive_date = p.value
                items.append(it)
                continue
            # Templates {{Lien web}} et {{Article}}
            if kind is None:
                continue
            url = None
            for pn in ("url", "lire en ligne"):
                p = t.find(pn)
                if p and re.match(r"^https?://", p.value, re.I):
                    url = p.value.replace("&amp;", "&")
                    break
            if url is None:
                continue
            host = T.host_of(url)
            if not host or _is_skipped(host):
                continue
            it = _Item()
            it.url, it.start, it.end, it.raw, it.t, it.kind = url, s, e, raw, t, kind
            cp = t.find("consulté le")
            if cp and cp.value:
                it.consulted = cp.value
            it.has_dead_template = bool(has_dead_template)
            it.partial_archive = has_archive_url or has_archive_date
            it.full_archive = has_archive_url and has_archive_date
            # Récupérer l'archive existante si présente
            if has_archive_url:
                p = t.find("archive-url") or t.find("archiveurl") or t.find("url archive")
                if p and p.value:
                    it.existing_archive_url = p.value.replace("&amp;", "&")
            if has_archive_date:
                p = t.find("archive-date") or t.find("archivedate") or t.find("date archive")
                if p and p.value:
                    it.existing_archive_date = p.value
            items.append(it)
        return items

    # ------------------------------------------------------------ détection
    def _classify(self, c, f, url):
        if not f.ok:
            if f.kind == "http" and _is_dead_status(f.status):
                return "hard", f"HTTP {f.status}"
            if f.kind in ("dns", "connect"):
                return "domain", f.error
            return "unknown", f.error
        if f.final_url and _home_or_error_redirect(url, f.final_url):
            return "soft", "redirigé vers l'accueil ou une page d'erreur"
        m = htmlmeta.extract(f.body, c.now, url)
        for t in m.titles:
            if T.is_dead_title(t):
                return "soft", f"page d'erreur (« {T.decode(t)} »)"
        return "alive", None

    def _dead(self, c, url):
        if url in c.dead:
            return c.dead[url]
        k, reason = self._classify(c, c.f.get(url), url)
        if k == "domain" or (k == "hard" and reason and reason.startswith("HTTP 5")):
            k, reason = self._classify(c, c.f.get_fresh(url), url)
        c.dead[url], c.reason[url] = k, reason
        c.rep[url] = {"url": url, "state": {"alive": "vivant", "unknown": "indéterminé"}.get(k, "mort"), "reason": reason, "archive": None}
        if k in ("hard", "soft", "domain"):
            c.r.dead_found += 1
        return k

    @staticmethod
    def _kind_enabled(c, k):
        return (k == "hard" and c.on("DRF01")) or (k == "soft" and c.on("DRF02")) or (k == "domain" and c.on("DRF03"))

    # ------------------------------------------------------------ archive Wayback
    def _verify_archive(self, c, archive_url, original_url):
        """Vérifie si une archive existante est valide et accessible."""
        try:
            f = c.f.get(archive_url)
            if not f.ok:
                return False
            # Vérifier que l'archive ne redirige pas vers une page d'erreur
            if f.final_url and _home_or_error_redirect(original_url, f.final_url):
                return False
            # Vérifier que le contenu n'est pas une page d'erreur Wayback
            m = htmlmeta.extract(f.body, c.now, original_url)
            title = m.best_title(T.host_of(original_url))
            if not title or re.search(r"wayback machine|internet archive|archive\.org|file not found|got an error|timed out", title, re.I):
                return False
            return True
        except Exception:
            return False

    def _get_archive(self, c, url, consulted):
        ts0 = None
        if consulted:
            y, mo, d = T.parse_date(consulted, c.now)
            if y and d > 0:
                ts0 = f"{y:04d}{mo:02d}{d:02d}"
        key = url + "|" + (ts0 or "")
        if key in c.archives:
            return c.archives[key]
        c.archives[key] = None
        api = c.f.get("https://archive.org/wayback/available?url=" + quote(url, safe="") + (f"&timestamp={ts0}" if ts0 else ""))
        if not api.ok:
            return None
        try:
            closest = json.loads(api.body)["archived_snapshots"]["closest"]
        except Exception:
            return None
        if not isinstance(closest, dict) or closest.get("available") is not True or str(closest.get("status")) != "200":
            return None
        ts = str(closest.get("timestamp") or "")
        if not re.fullmatch(r"\d{8,14}", ts):
            return None
        snap = str(closest.get("url") or "")
        if "web.archive.org/web/" not in snap.lower():
            return None
        snap = re.sub(r"^http://", "https://", snap, flags=re.I)
        y, mo, d = ts[:4], ts[4:6], ts[6:8]
        if T.parse_date(f"{y}-{mo}-{d}", c.now) == EMPTY:
            return None
        pg = c.f.get(f"https://web.archive.org/web/{ts}id_/{url}")
        if not pg.ok:
            return None
        fm = re.search(r"/web/\d+(?:id_|if_)?/(https?://.+)$", pg.final_url or "", re.I)
        if fm and _home_or_error_redirect(url, fm.group(1)):
            return None
        m = htmlmeta.extract(pg.body, c.now, url)
        title = m.best_title(T.host_of(url))
        if not title or re.search(r"wayback machine|internet archive|archive\.org", title, re.I):
            return None
        a = {"url": snap, "date": f"{d}/{mo}/{y}", "meta": m}
        c.archives[key] = a
        return a

    # ------------------------------------------------------------ traitement
    def _process_ref(self, c, m, items):
        body = m.group("body")
        new = body
        if len(items) == 1 and items[0].bare:
            tpl = self._process_bare(c, items[0])
            if tpl:
                new = tpl + ("." if c.o.terminal_period else "")
        else:
            for it in reversed(items):
                if it.bare:
                    continue
                if it.kind == "dead_template":
                    rep = self._process_dead_template(c, it)
                else:
                    rep = self._process_template(c, it)
                if rep:
                    new = new[:it.start] + rep + new[it.end:]
        if new == body:
            return m.group(0)
        return "<ref" + m.group("attrs") + ">" + new + "</ref>"

    def _confirmed(self, c, it):
        # Si le lien a déjà une archive complète, vérifier sa validité
        if it.full_archive and it.existing_archive_url:
            # Vérifier si l'archive existante est valide
            archive_valid = self._verify_archive(c, it.existing_archive_url, it.url)
            c.r.archives_verified.append({
                "url": it.url,
                "archive_url": it.existing_archive_url,
                "archive_date": it.existing_archive_date,
                "valid": archive_valid,
                "timestamp": c.now.isoformat()
            })
            if it.url in c.rep:
                c.rep[it.url]["archive_verified"] = archive_valid
                c.rep[it.url]["existing_archive"] = it.existing_archive_url
            if archive_valid:
                # Archive valide, rien à faire
                return None
            # Archive invalide, chercher une nouvelle
            a = self._get_archive(c, it.url, it.consulted)
            if it.url in c.rep:
                c.rep[it.url]["archive"] = a["url"] if a else "aucune archive valide (remplacement)"
            return a
        # Si le lien est déjà marqué brisé sans archive complète, on vérifie s'il est vraiment mort
        # et on cherche une archive valide pour compléter
        if it.has_dead_template and not it.partial_archive:
            # Vérifier si le lien est vraiment mort
            k = self._dead(c, it.url)
            c.r.dead_links_checked.append({
                "url": it.url,
                "state": k,
                "reason": c.reason.get(it.url),
                "timestamp": c.now.isoformat()
            })
            if k not in ("hard", "soft", "domain"):
                # Le lien n'est pas mort, mais marqué comme tel
                c.r.false_positives.append({
                    "url": it.url,
                    "reason": c.reason.get(it.url, "vivant"),
                    "timestamp": c.now.isoformat()
                })
                if it.url in c.rep:
                    c.rep[it.url]["state"] = "vivant (faux positif)"
                    c.rep[it.url]["reason"] = f"marqué brisé mais {c.reason.get(it.url, 'vivant')}"
                return None
            # Le lien est mort, chercher une archive
            a = self._get_archive(c, it.url, it.consulted)
            if it.url in c.rep:
                c.rep[it.url]["archive"] = a["url"] if a else "aucune archive valide"
            if a is None:
                line = f"{it.url} — {c.reason.get(it.url) or 'mort'} : aucune archive valide"
                if line not in c.r.no_archive:
                    c.r.no_archive.append(line)
            return a
        # Cas normal : vérifier si mort puis chercher archive
        k = self._dead(c, it.url)
        if not self._kind_enabled(c, k):
            return None
        a = self._get_archive(c, it.url, it.consulted)
        if it.url in c.rep:
            c.rep[it.url]["archive"] = a["url"] if a else "aucune archive valide"
        if a is None:
            line = f"{it.url} — {c.reason.get(it.url) or 'mort'} : aucune archive valide"
            if line not in c.r.no_archive:
                c.r.no_archive.append(line)
        return a

    @staticmethod
    def _has_any(t, *names):
        return any(t.has_value(n) for n in names)

    @staticmethod
    def _remove_dead_template(body):
        """Supprime les modèles de lien brisé redondants après ajout des paramètres d'archive."""
        # Ne supprime que les modèles qui ne sont pas imbriqués dans {{Lien web}} ou {{Article}}
        # Car ceux-là sont convertis en paramètres, pas supprimés
        from .textutil import find_top_level
        modified = body
        for s, e in reversed(list(find_top_level(body))):
            raw = body[s:e]
            t = T.parse_template(raw)
            n = (t.name or "").replace("_", " ").strip().lower()
            # Supprimer uniquement les modèles de lien brisé purs (pas Lien web/Article)
            if DEAD_TEMPLATE.search(raw) and n not in ("lien web", "article"):
                modified = modified[:s] + modified[e:]
        return modified

    def _process_dead_template(self, c, it):
        """Convertit un template de lien brisé pur en {{Lien web}} avec archive."""
        if not c.on("DRF04"):
            return None
        a = self._confirmed(c, it)
        if a is None:
            return None
        m = a["meta"]
        host = T.host_of(it.url)
        # Construire un {{Lien web}} à partir du template de lien brisé
        params = []
        # URL
        params.append(f"|url={T.url_param(it.url)}")
        # Titre
        title = m.best_title(host)
        if title:
            params.append(f"|titre={T.param(title)}")
        # Archive
        params.append(f"|brisé le={T.fmt_dt(c.now)}")
        params.append(f"|archive-url={T.url_param(a['url'])}")
        params.append(f"|archive-date={a['date']}")
        # Site
        site = T.param(m.site_name) or host
        if site:
            params.append(f"|site={site}")
        # Langue
        lang = T.guess_lang(title, m.lang)
        if lang:
            params.append(f"|langue={lang}")
        # Auteurs
        if m.authors:
            for i, au in enumerate(m.authors):
                params.append(f"|auteur{i + 1}={T.param(au)}")
        # Date de publication
        if m.date != EMPTY:
            params.append(f"|date={T.fr_date(*m.date)}")
        new_template = "{{Lien web" + "".join(params) + "}}"
        c.hit("DRF04", "Lien brisé → Lien web", it.raw, new_template)
        c.r.total += 1
        c.r.template_conversions.append({
            "url": it.url,
            "kind": "dead_template_to_lien_web",
            "before": it.raw[:200] + "..." if len(it.raw) > 200 else it.raw,
            "after": new_template[:200] + "..." if len(new_template) > 200 else new_template,
            "added_params": ["url", "titre", "archive-url", "archive-date", "site"],
            "timestamp": c.now.isoformat()
        })
        return new_template

    def _process_template(self, c, it):
        if not c.on("DRF05"):
            return None
        a = self._confirmed(c, it)
        if a is None:
            return None
        t = it.t
        add, fill = [], []

        def put(name, value):
            if value is None:
                return
            ex = t.find(name)
            if ex is None:
                add.append((name, value))
            elif not ex.value:
                fill.append((ex, value))

        now = c.now
        if c.on("DRF06"):
            m = a["meta"]
            host = T.host_of(it.url)
            if it.kind == "web":
                has_titre = self._has_any(t, "titre", "title")
                has_auteur = self._has_any(t, "auteur", "auteur1", "nom", "nom1", "prénom", "prénom1", "auteur institutionnel", "auteurs", "responsabilité")
                has_date = self._has_any(t, "date", "année", "jour", "mois")
                has_site = self._has_any(t, "site", "périodique", "éditeur")
                raw_t = None if has_titre else m.best_title(host)
                pipe = raw_t is not None and "|" in raw_t and not c.on("DRF07")
                title = None if pipe else T.param(raw_t)
                if not has_auteur:
                    if len(m.authors) == 1:
                        put("auteur", T.param(m.authors[0]))
                    else:
                        for i, au in enumerate(m.authors):
                            put(f"auteur{i + 1}", T.param(au))
                if not has_titre:
                    put("titre", title)
                if not has_date:
                    put("date", T.fr_date(*m.date))
                if not has_site:
                    put("site", T.param(m.site_name) or host)
                if (add or fill) and not t.has_value("langue"):
                    tp = t.find("titre")
                    put("langue", T.guess_lang(title or (tp.value if tp else None), m.lang))
        put("brisé le", T.fmt_dt(now))
        put("archive-url", T.url_param(a["url"]))
        put("archive-date", a["date"])

        res = it.raw
        for p, v in sorted(fill, key=lambda kv: -kv[0].vstart):
            res = T.set_value(res, p, v)
        res = T.append_params(res, add)
        # Supprimer les modèles de lien brisé redondants après ajout des paramètres d'archive
        res = self._remove_dead_template(res)
        if res == it.raw:
            return None
        c.hit("DRF05", "Lien web" if it.kind == "web" else "Article", it.raw, res)
        c.r.total += 1
        # Enregistrer la conversion dans la traçabilité
        c.r.template_conversions.append({
            "url": it.url,
            "kind": it.kind,
            "before": it.raw[:200] + "..." if len(it.raw) > 200 else it.raw,
            "after": res[:200] + "..." if len(res) > 200 else res,
            "added_params": [n for n, v in add],
            "timestamp": c.now.isoformat()
        })
        return res

    def _process_bare(self, c, it):
        if not c.on("DRF04"):
            return None
        a = self._confirmed(c, it)
        if a is None:
            return None
        m = a["meta"]
        host = T.host_of(it.url)
        raw_title = None if it.label else m.best_title(host)
        if raw_title is not None and "|" in raw_title and not c.on("DRF07"):
            c.fail(it.url, "titre contenant « | » : ignoré (règle DRF07 désactivée)")
            return None
        title = T.param(it.label) if it.label else T.param(raw_title)
        if title is None:
            c.fail(it.url, "titre introuvable dans l'archive")
            return None
        lang = T.guess_lang(title, m.lang)
        cons = it.consulted or T.fmt_dt(c.now)
        now_s = T.fmt_dt(c.now)
        arch = [("brisé le", now_s), ("archive-url", T.url_param(a["url"])), ("archive-date", a["date"])]
        if m.is_scholarly:
            p = [("langue", lang)]
            p += self._authors(m.authors, "auteur1", False)
            pages = None if not m.first_page else (m.first_page if not m.last_page else f"{m.first_page}-{m.last_page}")
            p += [("titre", title), ("périodique", T.param(m.journal)), ("volume", T.param(m.volume)), ("numéro", T.param(m.issue)),
                  ("date", T.fr_date(*m.date)), ("pages", T.param(pages)), ("issn", T.param(m.issn)),
                  ("lire en ligne", T.url_param(it.url))] + arch
            p.append(("id", "{{doi|" + T.url_param(m.doi) + "}}" if m.doi else None))
            tpl, label = self._render("Article", p, c.o.emit_empty), "Article"
        else:
            p = [("langue", lang)]
            p += self._authors(m.authors, "auteur", True)
            p += [("titre", title), ("url", T.url_param(it.url)), ("date", T.fr_date(*m.date)),
                  ("site", T.param(m.site_name) or host)] + arch
            tpl, label = self._render("Lien web", p, c.o.emit_empty), "Lien web"
        c.hit("DRF04", label, it.url, tpl)
        c.r.total += 1
        return tpl

    @staticmethod
    def _authors(authors, single, single_if_one):
        a = [v for v in (T.param(x) for x in authors) if v]
        if not a:
            return [(single, None)]
        if len(a) == 1 and single_if_one:
            return [(single, a[0])]
        return [(f"auteur{i + 1}", v) for i, v in enumerate(a)]

    @staticmethod
    def _render(name, parts, emit_empty):
        return "{{" + name + "".join(f" |{k}={v or ''}" for k, v in parts if v or emit_empty) + "}}"
