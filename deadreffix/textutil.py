"""Utilitaires texte, dates et modèles wiki (portage de TextUtil/Dates/Templates du plugin AWB)."""
import calendar
import html as _html
import re
from datetime import datetime, timedelta
from urllib.parse import urlparse

I = re.I
L = r"[^\W\d_]"  # lettre unicode

_BAD_TITLE = re.compile(r"^\s*(?:403|404|410|500|502|503|error|erreur|access denied|acc[èe]s refus[ée]|forbidden|just a moment|attention required|page not found|page introuvable|not found|document introuvable|are you a robot|robot check|captcha|please wait|one moment|sign in|log ?in|connexion|cookie|consent|before you continue|vérification|verification|security check|un instant)\b", I)
_BAD_ANYWHERE = re.compile(r"\b404\b|not found|page introuvable|introuvable|no longer (?:available|exists?)|n[’']existe plus|has been (?:removed|deleted)|domain (?:is )?for sale|this domain|domaine (?:est )?à vendre|account suspended|site (?:en )?maintenance|default web site|welcome to nginx|index of /|coming soon|bient[ôo]t disponible|page expir|lien (?:mort|bris[ée])", I)
_DEAD_TITLE = re.compile(r"\b404\b|\b410\b|not found|page introuvable|introuvable|no longer (?:available|exists?)|n[’']existe plus|has been (?:removed|deleted)|domain (?:is )?for sale|this domain (?:is|may be)|domaine (?:est )?à vendre|account suspended|page expir|cette page n[’']existe|this page (?:doesn[’']t|does not) exist|erreur 404|error 404|lien (?:mort|bris[ée])", I)
_BLOCK_TAGS = re.compile(r"</?(?:br|p|div|li|ul|ol|h[1-6]|tr|td|th|table|section|article|header|footer)\b[^>]*>", I)
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[\s\u00A0\u200B]+")
_GENERIC_AUTHOR = re.compile(r"^(?:admin|administrator|administrateur|webmaster|staff|team|[ée]quipe|redaction|rédaction|la r[ée]daction|unknown|inconnu|undefined|null|auteur|author)$", I)
_AUTHOR_NOISE = re.compile(r"^(?:partager|share|suivre|follow|lire|read|newsletter|commentaires?|comments?|abonn|subscribe|connexion|login|accueil|home|menu|contact|publi[ée]|mis à jour|updated|min\b|auteur$|author$|r[ée]daction|la r[ée]daction|plus d|voir|tous les|all posts|view|profile|profil)", I)
FR = {"le", "la", "les", "des", "du", "de", "et", "un", "une", "pour", "dans", "sur", "au", "aux", "est", "sont", "que", "qui", "avec", "par", "ce", "ces", "en"}
EN = {"the", "of", "and", "to", "in", "for", "is", "are", "with", "on", "at", "by", "from", "how", "what", "why", "your", "you"}


def _fix_mojibake(s):
    if "\u00C3" not in s and "\u00E2\u20AC" not in s and "\u00C2" not in s:
        return s
    try:
        t = s.encode("cp1252").decode("utf-8")
        return s if "\ufffd" in t else t
    except Exception:
        return s


def decode(s):
    if s is None:
        return None
    s = _html.unescape(s)
    if "&amp;" in s or "&#" in s:
        s = _html.unescape(s)
    s = _fix_mojibake(s)
    s = _BLOCK_TAGS.sub(" ", s)
    s = _TAGS.sub("", s)
    return _SPACES.sub(" ", s).strip()


def is_dead_title(t):
    t = decode(t)
    return bool(t) and bool(_DEAD_TITLE.search(t))


def is_bad_title(t):
    t = decode(t)
    return bool(t) and bool(_BAD_TITLE.search(t) or _BAD_ANYWHERE.search(t))


def norm(s):
    return "".join(c for c in (s or "").lower() if c.isalnum())


def host_of(url):
    try:
        h = urlparse(url).hostname
    except Exception:
        return None
    if not h:
        return None
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


def clean_title(title, site, host):
    title = decode(title)
    if not title:
        return None
    if _BAD_TITLE.search(title) or _BAD_ANYWHERE.search(title):
        return None
    n = norm(title)
    if len(n) < 2 or n == norm(host) or (len(norm(site)) > 0 and n == norm(site)):
        return None
    return title


def param(v):
    if v is None:
        return None
    v = _SPACES.sub(" ", v).strip()
    v = v.replace("{{", "&#123;&#123;").replace("}}", "&#125;&#125;").replace("[[", "&#91;&#91;").replace("]]", "&#93;&#93;")
    v = v.replace("|", "&#124;")
    return v or None


def url_param(u):
    return u.replace("|", "%7C").replace("{{", "%7B%7B").replace("}}", "%7D%7D").replace(" ", "%20")


def _title_case_name(a):
    if any(c.islower() for c in a) or " " not in a:
        return a
    out, up = [], True
    for c in a:
        out.append(c.upper() if up else c.lower())
        up = c in " -'’"
    return "".join(out)


def clean_author(a):
    a = decode(a)
    if not a:
        return None
    if "http" in a.lower() or "@" in a or "|" in a:
        return None
    a = re.sub(r"^(?:(?:par|by|publi[ée] par|[ée]crit par|text by|texte de)\s+|(?:auteur|author)\s*:\s*)", "", a, flags=I).strip(" ,;:-–")
    a = re.sub(r"\s*\([^)]*\)\s*$", "", a).strip()
    if len(a) < 2 or len(a) > 60 or _GENERIC_AUTHOR.match(a) or _AUTHOR_NOISE.match(a):
        return None
    if any(c.isdigit() for c in a) or len(a.split()) > 5:
        return None
    return _title_case_name(a)


def norm_lang(l):
    if not l:
        return None
    m = re.match(r"^([a-z]{2,3})(?:[-_].*)?$", l.strip().lower())
    return m.group(1) if m else None


def guess_lang(title, declared):
    lang = norm_lang(declared)
    fr = en = 0
    for w in re.findall(r"[^\W\d_']+", (title or "").lower()):
        fr += w in FR
        en += w in EN
    if fr >= 2 and en == 0:
        lang = "fr"
    elif en >= 2 and fr == 0:
        lang = "en"
    return None if lang in (None, "fr") else lang


# ------------------------------------------------------------------ dates
MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]
_MONTH_NAMES = ["janvier|janv|jan|january", "février|fevrier|févr|fevr|feb|february", "mars|mar|march", "avril|avr|apr|april", "mai|may",
                "juin|jun|june", "juillet|juil|jul|july", "août|aout|aug|august", "septembre|sept|sep|september", "octobre|oct|october",
                "novembre|nov|november", "décembre|decembre|déc|dec|december"]
MONTH_MAP = {n: i + 1 for i, g in enumerate(_MONTH_NAMES) for n in g.split("|")}
_ISO = re.compile(r"^\s*(?P<y>\d{4})(?:[-/](?P<m>\d{1,2})(?:[-/](?P<d>\d{1,2}))?)?(?:[T\s].*)?$")
_DMY = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})(?:\D.*)?$")
_TEXT_DMY = re.compile(r"(?P<d>\d{1,2})(?:er|st|nd|rd|th|e)?\.?\s+(?:de\s+|d['’])?(?P<m>" + L + r"{3,10})\.?,?\s+(?P<y>\d{4})", I)
_TEXT_MDY = re.compile(r"(?P<m>" + L + r"{3,10})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})", I)
_TEXT_MY = re.compile(r"^\s*(?P<m>" + L + r"{3,10})\.?,?\s+(?P<y>\d{4})\s*$", I)


def fr_date(y, m=0, d=0):
    if not y:
        return None
    if not m:
        return str(y)
    if not d:
        return f"{MONTHS[m - 1]} {y}"
    return f"{'1er' if d == 1 else d} {MONTHS[m - 1]} {y}"


def fmt_dt(dt):
    return fr_date(dt.year, dt.month, dt.day)


def _month(name):
    return MONTH_MAP.get(name.lower().rstrip("."), 0)


def _parse_text(s, day_first):
    s = s.strip()
    try:
        if re.fullmatch(r"\d{13}", s):
            e = datetime(1970, 1, 1) + timedelta(milliseconds=int(s))
            return e.year, e.month, e.day
        if re.fullmatch(r"\d{10}", s):
            e = datetime(1970, 1, 1) + timedelta(seconds=int(s))
            return e.year, e.month, e.day
    except Exception:
        return None
    c = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if c:
        return int(c[1]), int(c[2]), int(c[3])
    m = _DMY.match(s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        if a > 12:
            return y, b, a
        if b > 12:
            return y, a, b
        return (y, b, a) if day_first else (y, a, b)
    for t in _TEXT_DMY.finditer(s):
        mo = _month(t["m"])
        if mo:
            return int(t["y"]), mo, int(t["d"])
    for t in _TEXT_MDY.finditer(s):
        mo = _month(t["m"])
        if mo:
            return int(t["y"]), mo, int(t["d"])
    my = _TEXT_MY.match(s)
    if my and _month(my["m"]):
        return int(my["y"]), _month(my["m"]), 0
    return None


def parse_date(s, today, day_first=True):
    """Retourne (y, m, d) ou (0, 0, 0) si invalide."""
    empty = (0, 0, 0)
    if not s or not s.strip():
        return empty
    m = _ISO.match(s)
    if m:
        p = (int(m["y"]), int(m["m"] or 0), int(m["d"] or 0))
    else:
        p = _parse_text(s, day_first)
        if p is None:
            try:
                dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
                p = (dt.year, dt.month, dt.day)
            except Exception:
                return empty
    y, mo, d = p
    if y < 1900 or y > today.year or not 0 <= mo <= 12 or not 0 <= d <= 31 or (mo == 0 and d != 0):
        return empty
    if d > 0 and d > calendar.monthrange(y, mo)[1]:
        return empty
    full = datetime(y, mo or 1, d or 1)
    return p if full <= datetime(today.year, today.month, today.day) + timedelta(days=1) else empty


# ------------------------------------------------------------------ modèles wiki
class TParam:
    __slots__ = ("name", "value", "vstart", "vend", "named")


class TemplateInfo:
    def __init__(self):
        self.name = None
        self.params = []
        self.raw = ""

    def find(self, name):
        for p in self.params:
            if p.named and p.name.lower() == name.lower():
                return p
        return None

    def has_value(self, name):
        p = self.find(name)
        return p is not None and len(p.value) > 0


def find_top_level(s):
    res, depth, start, i = [], 0, -1, 0
    while i < len(s) - 1:
        if s[i] == "{" and s[i + 1] == "{":
            if depth == 0:
                start = i
            depth += 1
            i += 2
        elif s[i] == "}" and s[i + 1] == "}" and depth > 0:
            depth -= 1
            i += 2
            if depth == 0:
                res.append((start, i))
        else:
            i += 1
    return res


def _scan(seg, want):
    """Positions (hors modèles/liens imbriqués) du caractère `want` ; pour '=' seule la première."""
    dt = dl = 0
    i = 0
    pos = []
    while i < len(seg):
        c = seg[i]
        n = seg[i + 1] if i + 1 < len(seg) else ""
        if c == "{" and n == "{":
            dt += 1; i += 2; continue
        if c == "}" and n == "}" and dt > 0:
            dt -= 1; i += 2; continue
        if c == "[" and n == "[":
            dl += 1; i += 2; continue
        if c == "]" and n == "]" and dl > 0:
            dl -= 1; i += 2; continue
        if c == want and dt == 0 and dl == 0:
            pos.append(i)
            if want == "=":
                return pos
        i += 1
    return pos


def parse_template(raw):
    t = TemplateInfo()
    t.raw = raw
    if len(raw) < 4:
        return t
    inner = raw[2:-2]
    cuts = [-1] + _scan(inner, "|") + [len(inner)]
    segs = [(cuts[i] + 1, cuts[i + 1]) for i in range(len(cuts) - 1)]
    t.name = inner[segs[0][0]:segs[0][1]].strip()
    posn = 0
    for a, b in segs[1:]:
        seg = inner[a:b]
        eq = _scan(seg, "=")
        p = TParam()
        if eq:
            e = eq[0]
            p.named = True
            p.name = seg[:e].strip()
            p.value = seg[e + 1:].strip()
            p.vstart = 2 + a + e + 1
            p.vend = 2 + b
        else:
            posn += 1
            p.named = False
            p.name = str(posn)
            p.value = seg.strip()
            p.vstart = 2 + a
            p.vend = 2 + b
        t.params.append(p)
    return t


def set_value(raw, p, value):
    seg = raw[p.vstart:p.vend]
    lead = " " if seg[:1] == " " else ""
    trail = "\n" if "\n" in seg else ""
    return raw[:p.vstart] + lead + value + trail + raw[p.vend:]


def append_params(raw, add):
    if not add:
        return raw
    cut = len(raw) - 2
    while cut > 2 and raw[cut - 1].isspace():
        cut -= 1
    multi = "\n" in raw
    sep = "\n|" if multi else (" |" if " |" in raw else "|")
    return raw[:cut] + "".join(f"{sep}{k}={v}" for k, v in add) + raw[cut:]
