"""Fournisseurs d'archives (port de Archiving.cs) : Internet Archive (Wayback) et Memento (archive.ph)."""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from .urltools import same_resource

SNAPSHOT = re.compile(r"^https?://[^/]+/(?:web/)?(?P<ts>\d{14})(?:[a-z]{2}_)?/(?P<orig>https?:/{1,2}.+)$", re.I)
ARCHIVE_SUFFIXES = ("archive.org", "archive.ph", "archive.today", "archive.is")


class Snapshot:
    __slots__ = ("archive_url", "original_url", "date", "provider")

    def __init__(self, archive_url, original_url, date, provider):
        self.archive_url, self.original_url, self.date, self.provider = archive_url, original_url, date, provider


def snapshot_date(archive_url):
    """Date (UTC) lue dans une URL de capture, ou None."""
    m = SNAPSHOT.match((archive_url or "").strip())
    if not m:
        return None
    try:
        d = datetime.strptime(m.group("ts"), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return d if d <= datetime.utcnow() + timedelta(days=2) else None


def try_build(provider, snapshot_url, requested_url):
    """Valide qu'une URL de capture correspond bien à l'URL demandée (domaine ET lien)."""
    if not snapshot_url or not snapshot_url.strip():
        return None
    m = SNAPSHOT.match(snapshot_url.strip())
    if not m:
        return None
    orig = re.sub(r"^(https?):/(?!/)", r"\1://", m.group("orig"), flags=re.I)  # certaines captures écrasent « // »
    if not same_resource(orig, requested_url):
        return None
    d = snapshot_date(snapshot_url)
    return Snapshot(snapshot_url.strip(), orig, d, provider) if d else None


def force_https(url):
    if url and url.lower().startswith("http://"):
        host = url[7:].split("/", 1)[0].lower()
        if host.endswith(ARCHIVE_SUFFIXES):
            return "https" + url[4:]
    return url


def _http_get(url, ua, timeout=25):
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=timeout)
        return r.text if r.ok else None
    except requests.RequestException:
        return None


def _http_location(url, ua, timeout=25):
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=timeout, allow_redirects=False)
        if 300 <= r.status_code < 400:
            loc = r.headers.get("Location", "")
            return loc if loc.startswith("http") else None
    except requests.RequestException:
        pass
    return None


class WaybackProvider:
    name = "Internet Archive"

    def __init__(self, fetch=None, user_agent="DeadLinkFix/1.0"):
        self._fetch = fetch or (lambda u: _http_get(u, user_agent))
        self.api = "https://archive.org/wayback/available?url="

    def find_closest(self, url):
        try:
            return self.parse(self._fetch(self.api + quote(url, safe="")), url)
        except Exception:  # noqa
            return None

    def parse(self, body, requested):
        if not body:
            return None
        c = ((json.loads(body).get("archived_snapshots") or {}).get("closest")) or {}
        if not c.get("available") or str(c.get("status")) != "200" or not c.get("url"):
            return None
        return try_build(self.name, force_https(c["url"].replace("\\/", "/")), requested)


class MementoProvider:
    def __init__(self, name="archive.today", timegate="https://archive.ph/timegate/", resolve=None, user_agent="DeadLinkFix/1.0"):
        self.name, self.timegate = name, timegate
        self._resolve = resolve or (lambda u: _http_location(u, user_agent))

    def find_closest(self, url):
        try:
            loc = self._resolve(self.timegate + url)
            return try_build(self.name, force_https(loc), url) if loc else None
        except Exception:  # noqa
            return None
