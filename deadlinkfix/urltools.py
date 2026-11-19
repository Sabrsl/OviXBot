"""Outils d'URL (port de UrlTools.cs) : comparaison de domaines, forme canonique, indices de correspondance."""
import ipaddress
import re
import unicodedata
from urllib.parse import unquote, urlsplit

TWO_LEVEL_SUFFIXES = {
    'co.uk', 'org.uk', 'ac.uk', 'gov.uk', 'me.uk', 'com.au', 'net.au', 'org.au', 'co.nz', 'co.jp', 'co.za',
    'com.br', 'com.mx', 'com.ar', 'com.tr', 'com.cn', 'co.in', 'co.kr', 'com.sg', 'com.hk', 'com.tw',
    'com.ma', 'co.ma', 'gov.ma', 'org.ma', 'ac.ma', 'com.dz', 'com.tn', 'gov.tn', 'com.eg', 'gov.eg',
    'com.ng', 'gov.ng', 'org.ng', 'co.ke', 'go.ke', 'or.ke', 'co.tz', 'go.tz', 'co.ug', 'co.zw', 'com.gh',
    'gov.gh', 'com.cm', 'gov.cm', 'com.cd', 'co.cd', 'co.rw', 'com.rw', 'com.sn', 'org.sn', 'gouv.sn',
    'gouv.ci', 'gouv.bj', 'gouv.ml', 'gouv.bf', 'gouv.ne', 'gouv.tg', 'gouv.ga', 'gouv.cd', 'gouv.cg',
    'gouv.mg', 'gouv.km', 'gouv.dj', 'gouv.mr', 'gouv.gn', 'gouv.td', 'gouv.cf', 'gouv.ht', 'asso.fr',
    'com.fr', 'gouv.qc.ca', 'co.il', 'org.il', 'com.ua', 'com.pl', 'com.pt', 'com.es', 'com.ve',
}

# Hébergement mutualisé : deux sous-domaines n'y désignent PAS le même site.
SHARED_HOSTING = {
    'blogspot.com', 'blogspot.fr', 'wordpress.com', 'github.io', 'gitlab.io', 'herokuapp.com', 'netlify.app',
    'vercel.app', 'pages.dev', 'wixsite.com', 'weebly.com', 'tumblr.com', 'medium.com', 'over-blog.com',
    'over-blog.fr', 'over-blog.org', 'canalblog.com', 'free.fr', 'sites.google.com', 'googleusercontent.com',
    'azurewebsites.net', 'appspot.com', 'web.app', 'firebaseapp.com', 'unblog.fr', 'skyrock.com',
    'jimdofree.com', 'jimdo.com', 'yolasite.com', 'livejournal.com', 'hautetfort.com', 'e-monsite.com',
    'webnode.fr', '000webhostapp.com', 'onrender.com', 'fly.dev', 'glitch.me', 'repl.co', 'notion.site',
    'substack.com', 'bandcamp.com', 'deviantart.com', 'hubpages.com', 'ovh.net', 'amazonaws.com',
    'cloudfront.net', 'wixstudio.io', 'framer.website', 'webflow.io', 'carrd.co', 'mystrikingly.com',
}

ARCHIVE_HOSTS = (
    'archive.org', 'archive.ph', 'archive.is', 'archive.li', 'archive.fo', 'archive.md',
    'archive.vn', 'webcitation.org', 'archive-it.org', 'arquivo.pt', 'perma.cc', 'webarchive.org.uk',
)

INDEX_FILE = re.compile(r"/(?:index|default|home)\.(?:html?|php\d?|aspx?|jsp|shtml|cfm)$", re.I)
SLUG_EXT = re.compile(r"\.(?:html?|php\d?|aspx?|jsp|shtml|cfm|pdf)$", re.I)
DIGIT_RUN = re.compile(r"\d{5,}")
HOST_OK = re.compile(r"^[\w.\-:\[\]%]+$")


class Url:
    __slots__ = ("scheme", "host", "port", "path", "query", "absolute")

    def __init__(self, scheme, host, port, path, query, absolute):
        self.scheme, self.host, self.port, self.path, self.query, self.absolute = scheme, host, port, path, query, absolute

    @property
    def default_port(self):
        return self.port is None or (self.scheme, self.port) in (("http", 80), ("https", 443))


def parse_http(url):
    """Retourne un Url, ou None si ce n'est pas une URL http(s) absolue valide."""
    if not url or not url.strip():
        return None
    s = url.strip()
    try:
        p = urlsplit(s)
        port = p.port
        host = (p.hostname or "").lower()
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not host or not HOST_OK.match(host):
        return None
    return Url(p.scheme.lower(), host, port, p.path or "/", ("?" + p.query) if p.query else "", p.geturl())


def host_no_www(u):
    h = u.host
    return h[4:] if h.startswith("www.") and len(h) > 4 else h


def registrable_domain(host):
    try:
        ipaddress.ip_address(host.strip("[]"))
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    take = 3 if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 2
    return ".".join(labels[-take:]) if len(labels) >= take else host


def same_site(a, b):
    ha, hb = host_no_www(a), host_no_www(b)
    if ha == hb:
        return True
    ra, rb = registrable_domain(ha), registrable_domain(hb)
    return ra == rb and ra not in SHARED_HOSTING


def safe_unescape(s):
    try:
        return unquote(s or "", errors="strict")
    except Exception:  # noqa
        return s or ""


def canonical_path(u):
    path = INDEX_FILE.sub("/", safe_unescape(u.path).lower())
    return path.rstrip("/")


def canonical_query(u):
    return "" if u.query in ("", "?") else safe_unescape(u.query).lower()


def canonical(u):
    """Sans schéma, sans « www. », sans fragment, sans slash final ni index.html, en minuscules."""
    port = "" if u.default_port else ":%d" % u.port
    return host_no_www(u) + port + canonical_path(u) + canonical_query(u)


def same_resource(a, b):
    ua, ub = parse_http(a), parse_http(b)
    return bool(ua and ub and canonical(ua) == canonical(ub))


def _alnum(s):
    d = unicodedata.normalize("NFD", s)
    return "".join(c.lower() for c in d if unicodedata.category(c) != "Mn" and c.isalnum())


def slug_key(u):
    segs = [x for x in safe_unescape(u.path).split("/") if x]
    for seg in reversed(segs):
        key = _alnum(SLUG_EXT.sub("", seg))
        if key and key not in ("index", "default", "home", "accueil"):
            return key
    return ""


def numeric_ids(u):
    return set(DIGIT_RUN.findall(safe_unescape(u.path) + safe_unescape(u.query)))


def is_archive_host(u):
    h = u.host
    return any(h == a or h.endswith("." + a) for a in ARCHIVE_HOSTS)


def is_checkable(u):
    """Faux pour les hôtes locaux / privés / mono-label et pour les services d'archive."""
    h = u.host
    if h == "localhost" or h.endswith((".local", ".localhost", ".internal")):
        return False
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved:
            return False
    except ValueError:
        if "." not in h:
            return False
    return not is_archive_host(u)
