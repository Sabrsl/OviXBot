"""Extraction des métadonnées d'une page (titre, auteur, date, site…) — portage de Html.cs."""
import html as _html
import json
import re
from urllib.parse import urlparse

from . import textutil as T

RX = re.I | re.S
EMPTY = (0, 0, 0)
L = T.L

_META = re.compile(r"<meta\b[^>]*>", RX)
_ATTR = re.compile(r"""([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']+))""", RX)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", RX)
_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", RX)
_HTML_LANG = re.compile(r"""<html[^>]*?\slang\s*=\s*["']?([A-Za-z_-]+)""", RX)
_JSONLD = re.compile(r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""", RX)
_TIME = re.compile(r"<time\b(?P<a>[^>]*)>", RX)
_SCRIPT = re.compile(r"<(?:script|style|noscript|svg|nav|footer)\b.*?</(?:script|style|noscript|svg|nav|footer)>|<!--.*?-->", RX)
_ANYTAG = re.compile(r"<[^>]+>", RX)
_REL_AUTHOR = re.compile(r"""<a\b[^>]*\brel\s*=\s*["'][^"']*\bauthor\b[^"']*["'][^>]*>(.*?)</a>""", RX)
_ITEMPROP = re.compile(r"""<(?P<t>span|div|a|p|li|address)\b[^>]*itemprop\s*=\s*["']author["'][^>]*>(?P<c>.*?)</(?P=t)>""", RX)
_BYLINE = re.compile(r"""<(?P<t>span|div|p|a|address|li)\b[^>]*class\s*=\s*["'][^"']*(?:byline|author|auteur|signature|writer)[^"']*["'][^>]*>(?P<c>.*?)</(?P=t)>""", RX)
_URL_DATE = re.compile(r"/((?:19|20)\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?=[/._-]|$)|/((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?=[-_/])")
_DATE_EXPR = (r"(?:\d{1,2}(?:er|st|nd|rd|th)?\.?\s+(?:de\s+|d['’])?" + L + r"{3,10}\.?,?\s+\d{4}|" + L +
              r"{3,10}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}[/.]\d{1,2}[/.]\d{4}|\d{4}-\d{2}-\d{2})")
_WEEKDAY = r"(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+)?"
_VISIBLE_DATE = re.compile(r"(?:publi[ée]e?|mis(?:e)? en ligne|post[ée]e?|[ée]crit(?:e)?|paru(?:e)?|published|posted|date de publication|publication date|first published)\s*(?:le|on|:|-|–)?\s*(?:le\s+)?" + _WEEKDAY + r"(?P<d>" + _DATE_EXPR + ")", RX)
_AUTHOR_SPLIT = re.compile(r"\s*;\s*|\s+(?:et|and|und|y)\s+|\s+&\s+|\s*/\s*", re.I)
_AUTHOR_TAIL = re.compile(r"\s+(?:[-–—|•·]|le\s+\d|publi|mis à jour|updated|\d{1,2}[/.]\d{1,2}[/.]|\d{1,2}\s+" + L + r"{3,9}\s+\d{4})", re.I)
_NAME_SHAPE = re.compile(r"^[A-ZÀ-ÖØ-Þ](?:[^\W\d_]|['’.\-])*(?:\s+[^\W\d_](?:[^\W\d_]|['’.\-])*){1,4}$")

STRONG_DATE = ["article:published_time", "og:article:published_time", "og:published_time", "citation_publication_date", "citation_online_date",
               "citation_date", "dc.date.issued", "dcterms.issued", "dc.date.created", "dcterms.created", "datepublished", "parsely-pub-date",
               "sailthru.date", "publication_date", "article:published", "article.published", "date_published", "cxenseparse:recs:publishtime", "dc.date", "dcterms.date"]
WEAK_DATE = ["date", "pubdate", "publishdate", "publish_date", "release_date", "created"]
AUTHOR_KEYS = ["author", "dc.creator", "dcterms.creator", "article:author", "og:article:author", "parsely-author", "sailthru.author", "byl", "twitter:creator", "creator", "article.author"]


class PageMeta:
    def __init__(self):
        self.title = self.site_name = self.lang = self.journal = self.volume = self.issue = None
        self.first_page = self.last_page = self.issn = self.doi = self.publisher = self.jsonld_type = None
        self.titles = []
        self.title_tag_raw = None
        self.authors = []
        self.date = EMPTY
        self.upload = EMPTY
        self.is_scholarly = False

    def best_title(self, host, site_override=None):
        site = site_override or self.site_name
        ns = T.norm(site)
        for t in self.titles:
            if T.is_bad_title(t):
                return None
        cleaned = []
        for t in self.titles:
            c = T.clean_title(t, site, host)
            if c is None or len(c) < 4:
                continue
            if ns and T.norm(c) == ns:
                continue
            cleaned.append(c)
        if not cleaned:
            return None
        best = next((o for o in cleaned if "|" not in o), cleaned[0])
        nb = T.norm(best)
        if len(best) >= 100:
            tag_clean = T.clean_title(self.title_tag_raw, site, host) if self.title_tag_raw else None
            for o in cleaned:
                if o == tag_clean:
                    continue
                no = T.norm(o)
                if len(no) > len(nb) and no.startswith(nb) and len(o) <= 250:
                    best, nb = o, no
                    break
        return best


def _metas(html):
    res = []
    for m in _META.finditer(html):
        key = content = None
        for a in _ATTR.finditer(m.group(0)):
            n = a.group(1).lower()
            v = next((g for g in a.groups()[1:] if g is not None), "")
            if n in ("property", "name", "itemprop") and key is None:
                key = v.lower()
            elif n == "content":
                content = v
        if key is not None and content is not None:
            res.append((key, _html.unescape(content)))
    return res


def _one(metas, *keys):
    for k in keys:
        for key, v in metas:
            if key == k and v.strip():
                return v.strip()
    return None


def _all(metas, key):
    return [v.strip() for k, v in metas if k == key and v.strip()]


def _flatten(o, into):
    if isinstance(o, list):
        for x in o:
            _flatten(x, into)
    elif isinstance(o, dict):
        into.append(o)
        if "@graph" in o:
            _flatten(o["@graph"], into)


def _s(o):
    if isinstance(o, str):
        return o if o.strip() else None
    if isinstance(o, (int, float)) and not isinstance(o, bool):
        return str(o)
    return None


def _g(o, k):
    return o.get(k) if isinstance(o, dict) else None


def _type_of(d):
    t = d.get("@type")
    if isinstance(t, list):
        return ",".join(str(x) for x in t).lower()
    return (_s(t) or "").lower()


def _score(d):
    t = _type_of(d)
    if "scholarlyarticle" in t: s = 6
    elif "article" in t or "blogposting" in t or "report" in t: s = 5
    elif "videoobject" in t: s = 4
    elif "webpage" in t or "itempage" in t: s = 2
    else: s = 1
    return s + (d.get("datePublished") is not None) + (d.get("author") is not None)


def _deref(o, ids):
    if isinstance(o, dict):
        i = _s(o.get("@id"))
        if i and _s(o.get("name")) is None and i in ids:
            return ids[i]
    return o


def _names(o, into, ids, depth=0):
    if depth > 4:
        return
    o = _deref(o, ids)
    s = _s(o)
    if s:
        into.append(s)
        return
    if isinstance(o, list):
        for x in o:
            _names(x, into, ids, depth + 1)
        return
    n = _s(_g(o, "name"))
    if n:
        into.append(n)
        return
    given, fam = _s(_g(o, "givenName")), _s(_g(o, "familyName"))
    if fam:
        into.append(((given + " " if given else "") + fam).strip())


def _name_of(o, ids):
    l = []
    _names(o, l, ids)
    return l[0] if l else None


def _words(s):
    return len(s.split())


def _split_authors(a):
    a = T.decode(a)
    if not a:
        return []
    tmp = []
    parts = _AUTHOR_SPLIT.split(a)
    for part in parts:
        cs = part.split(",")
        if len(cs) >= 3:
            tmp += [c.strip() for c in cs]
        elif len(cs) == 2:
            l, r = cs[0].strip(), cs[1].strip()
            if not l or not r:
                tmp.append(part.strip(" ,"))
                continue
            wl, wr = _words(l), _words(r)
            if wl >= 2 and wr >= 2: tmp += [l, r]
            elif wl == 1 and wr == 1: tmp.append(r + " " + l)
            elif wl >= 2 and r[0].islower(): tmp.append(l)
            else: tmp.append(part.strip())
        else:
            tmp.append(part.strip())
    if any(t and _words(t) < 2 and len(tmp) > 1 for t in tmp) and len(parts) > 1 and "," not in a:
        return [a]
    return [t for t in tmp if t]


def _clean_authors(raw, site):
    res, ns = [], T.norm(site)
    for r in raw:
        for piece in _split_authors(r):
            c = T.clean_author(piece)
            if c is None or (ns and T.norm(c) == ns):
                continue
            if not any(x.lower() == c.lower() for x in res):
                res.append(c)
            if len(res) >= 5:
                return res
    return res


def _html_authors(html):
    res = []

    def add(inner):
        t = T.decode(inner)
        if not t or len(t) > 120:
            return
        t = re.sub(r"^(?:par|by|auteur|author|[ée]crit par|publi[ée] par|texte(?: de)?|signature)\s*:?\s+", "", t, flags=re.I).strip()
        tail = _AUTHOR_TAIL.search(t)
        if tail:
            t = t[:tail.start()].strip()
        if len(t) > 60 or not _NAME_SHAPE.match(t):
            return
        if t not in res:
            res.append(t)

    for rx, grp, lim in ((_REL_AUTHOR, 1, 3), (_ITEMPROP, "c", 3), (_BYLINE, "c", 4)):
        if res:
            break
        for n, m in enumerate(rx.finditer(html)):
            add(m.group(grp))
            if n + 1 >= lim:
                break
    return res


def _first_date(values, today, day_first, weak):
    for v in values:
        d = T.parse_date(v, today, day_first)
        if d == EMPTY:
            continue
        if weak and d[2] > 0 and (d[0], d[1], d[2]) == (today.year, today.month, today.day):
            continue
        return d
    return EMPTY


def _time_tag_dates(html):
    res = []
    for m in _TIME.finditer(html):
        a = m.group("a")
        dt, cls, ip = None, "", ""
        for at in _ATTR.finditer(a):
            n = at.group(1).lower()
            v = next((g for g in at.groups()[1:] if g is not None), "")
            if n == "datetime": dt = v
            elif n == "class": cls = v.lower()
            elif n == "itemprop": ip = v.lower()
        if dt is None:
            continue
        pub = "datepublished" in ip or "pubdate" in a.lower() or re.search(r"publi|posted|entry-date|post-date|article[_-]?date|date[_-]?pub|created|datepublished", cls)
        upd = re.search(r"updat|modif", cls) or "datemodified" in ip
        if pub and not upd:
            res.append(dt)
    return res


def _url_date(url):
    if not url:
        return None
    try:
        path = urlparse(url).path
    except Exception:
        return None
    m = _URL_DATE.search(path)
    if not m:
        return None
    return f"{m[1]}-{m[2]}-{m[3]}" if m[1] else f"{m[4]}-{m[5]}-{m[6]}"


def _visible_text(html):
    t = _SCRIPT.sub(" ", html)
    t = _ANYTAG.sub(" ", t)
    t = T.decode(t) or ""
    return t[:40000]


def extract(html, today, url=None):
    m = PageMeta()
    if not html:
        return m
    metas = _metas(html)
    nodes = []
    for sm in _JSONLD.finditer(html):
        try:
            _flatten(json.loads(_html.unescape(sm.group(1)).strip()), nodes)
        except Exception:
            pass
    ids = {}
    for d in nodes:
        i = _s(d.get("@id"))
        if i and i not in ids:
            ids[i] = d
    best, best_score = None, -1
    for d in nodes:
        if _s(d.get("headline")) is None and _s(d.get("name")) is None:
            continue
        sc = _score(d)
        if sc > best_score:
            best, best_score = d, sc
    ld_title = ld_date = ld_upload = ld_pub = ld_lang = None
    ld_authors = []
    if best is not None:
        m.jsonld_type = _type_of(best)
        ld_title = _s(best.get("headline")) or _s(best.get("name"))
        ld_date = _s(best.get("datePublished")) or _s(best.get("dateCreated"))
        ld_upload = _s(best.get("uploadDate"))
        _names(best.get("author"), ld_authors, ids)
        if not ld_authors:
            _names(best.get("creator"), ld_authors, ids)
        ld_pub = _name_of(best.get("publisher"), ids) or _name_of(best.get("isPartOf"), ids)
        ld_lang = _name_of(best.get("inLanguage"), ids)
        if "scholarlyarticle" in m.jsonld_type:
            m.is_scholarly = True
    if ld_date is None or not ld_authors:
        for d in nodes:
            t = _type_of(d)
            if not ("article" in t or "posting" in t):
                continue
            if ld_date is None:
                ld_date = _s(d.get("datePublished"))
            if not ld_authors:
                _names(d.get("author"), ld_authors, ids)

    if ld_title:
        m.titles.append(ld_title)
    for k in ("og:title", "twitter:title", "citation_title", "dc.title", "dcterms.title"):
        v = _one(metas, k)
        if v:
            m.titles.append(v)
    h1 = _H1.search(html)
    if h1:
        m.titles.append(h1.group(1))
    tt = _TITLE.search(html)
    if tt:
        m.titles.append(tt.group(1))
        m.title_tag_raw = tt.group(1)
    if m.titles:
        m.title = m.titles[0]

    m.site_name = T.decode(_one(metas, "og:site_name", "application-name") or ld_pub or _one(metas, "apple-mobile-web-app-title"))
    m.publisher = T.decode(_one(metas, "citation_publisher", "dc.publisher") or ld_pub)

    authors = _clean_authors(ld_authors, m.site_name)
    if not authors:
        authors = _clean_authors(_all(metas, "citation_author"), m.site_name)
    if not authors:
        for k in AUTHOR_KEYS:
            authors = _clean_authors(_all(metas, k), m.site_name)
            if authors:
                break
    if not authors:
        authors = _clean_authors(_html_authors(html), m.site_name)
    m.authors = authors

    hl = _HTML_LANG.search(html)
    m.lang = (T.norm_lang(ld_lang) or T.norm_lang(_one(metas, "og:locale", "citation_language", "dc.language", "language"))
              or (T.norm_lang(hl.group(1)) if hl else None))
    day_first = m.lang != "en"

    strong = ([ld_date] if ld_date else [])
    for k in STRONG_DATE:
        strong += _all(metas, k)
    date = _first_date(strong, today, day_first, False)
    if date == EMPTY:
        date = _first_date(_time_tag_dates(html), today, day_first, False)
    if date == EMPTY:
        ud = _url_date(url)
        if ud:
            date = _first_date([ud], today, day_first, True)
    if date == EMPTY:
        weak = []
        for k in WEAK_DATE:
            weak += _all(metas, k)
        date = _first_date(weak, today, day_first, True)
    if date == EMPTY:
        vm = _VISIBLE_DATE.search(_visible_text(html))
        if vm:
            date = _first_date([vm.group("d")], today, day_first, True)
    m.date = date
    up = ld_upload or _one(metas, "uploaddate", "video:release_date", "og:video:release_date")
    m.upload = _first_date([up] if up else [], today, day_first, False)

    m.journal = T.decode(_one(metas, "citation_journal_title", "prism.publicationname"))
    m.volume = _one(metas, "citation_volume", "prism.volume")
    m.issue = _one(metas, "citation_issue", "prism.number")
    m.first_page = _one(metas, "citation_firstpage", "prism.startingpage")
    m.last_page = _one(metas, "citation_lastpage", "prism.endingpage")
    m.issn = _one(metas, "citation_issn", "prism.issn")
    m.doi = _one(metas, "citation_doi", "dc.identifier.doi")
    if m.doi:
        m.doi = re.sub(r"^(?:doi:|https?://(?:dx\.)?doi\.org/)", "", m.doi, flags=re.I).strip()
    if m.journal or (m.doi and _one(metas, "citation_title")):
        m.is_scholarly = True
    return m
