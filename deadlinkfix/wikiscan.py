"""Analyse minimale et sûre du wikicode (port de WikiScan.cs) : masquage, balises <ref>, modèles et paramètres."""
import re

MASK = "\ue000"

PROTECTED = re.compile(
    r"<!--.*?(?:-->|$)|<nowiki\s*/>|<nowiki\b[^>]*>.*?(?:</nowiki\s*>|$)"
    r"|<(?P<t>pre|syntaxhighlight|source|code|math|templatedata)\b[^>]*>.*?(?:</(?P=t)\s*>|$)", re.S | re.I)
REF = re.compile(r"<ref\b[^>]*?(?<!/)>(?P<body>.*?)</ref\s*>", re.S | re.I)


class Param:
    __slots__ = ("name", "seg_start", "seg_end", "eq", "vstart", "vend", "value", "protected")


class Template:
    def __init__(self, start, end):
        self.name, self.start, self.end, self.params = None, start, end, []


def mask(text):
    """Remplace commentaires et zones nowiki/code par MASK, en conservant la longueur (positions identiques)."""
    chars = list(text)
    for m in PROTECTED.finditer(text):
        for i in range(m.start(), m.end()):
            chars[i] = MASK
    return "".join(chars)


def find_refs(masked):
    """Corps des <ref>…</ref> (positions début, fin)."""
    return [(m.start("body"), m.end("body")) for m in REF.finditer(masked)]


def normalize_name(s):
    t = re.sub(r"\s+", " ", s.replace("_", " ").strip().lower())
    for pre in ("modèle:", "template:"):
        if t.startswith(pre):
            return t[len(pre):].strip()
    return t


def find_templates(masked, original):
    """Modèles de premier niveau de `masked` ; `original` sert à lire les valeurs."""
    out, i, n = [], 0, len(masked)
    while i < n - 1:
        if masked[i] == "{" and masked[i + 1] == "{" and not (i + 2 < n and masked[i + 2] == "{"):
            end = _find_end(masked, i)
            if end < 0:
                break
            t = _parse(masked, original, i, end)
            if t is not None:
                out.append(t)
            i = end
        else:
            i += 1
    return out


def _find_end(s, start):
    depth, i, n = 0, start, len(s)
    while i < n - 1:
        if s[i] == "{" and s[i + 1] == "{":
            depth += 1
            i += 2
            continue
        if s[i] == "}" and s[i + 1] == "}":
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return -1


def _parse(m, orig, start, end):
    inner_start, inner_end = start + 2, end - 2
    t = Template(start, end)
    state = {"first": True, "seg": inner_start, "eq": -1}

    def close(seg_end):
        seg, eq = state["seg"], state["eq"]
        if state["first"]:
            t.name = normalize_name(orig[seg:seg_end])
            state["first"] = False
        elif eq >= 0:
            vs, ve = eq + 1, seg_end
            while vs < ve and orig[vs].isspace():
                vs += 1
            while ve > vs and orig[ve - 1].isspace():
                ve -= 1
            if vs == ve:
                vs = ve = eq + 1
            p = Param()
            p.name, p.seg_start, p.seg_end, p.eq = normalize_name(orig[seg:eq]), seg, seg_end, eq
            p.vstart, p.vend, p.value = vs, ve, orig[vs:ve]
            p.protected = MASK in m[vs:ve]
            t.params.append(p)

    brace = bracket = 0
    p = inner_start
    while p < inner_end:
        c = m[p]
        nx = m[p + 1] if p + 1 < inner_end else "\0"
        if c == "{" and nx == "{":
            brace += 1
            p += 1
        elif c == "}" and nx == "}":
            brace = max(0, brace - 1)
            p += 1
        elif c == "[" and nx == "[":
            bracket += 1
            p += 1
        elif c == "]" and nx == "]":
            bracket = max(0, bracket - 1)
            p += 1
        elif brace == 0 and bracket == 0:
            if c == "|":
                close(p)
                state["seg"], state["eq"] = p + 1, -1
            elif c == "=" and state["eq"] < 0 and not state["first"]:
                state["eq"] = p
        p += 1
    close(inner_end)
    return t if t.name is not None else None
