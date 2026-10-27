"""Téléchargement HTTP thread-safe : cache, délai minimal par hôte, taille maximale, classification des échecs."""
import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

DEFAULT_UA = "OviXBot-DeadRefFix/2.0 (https://fr.wikipedia.org/wiki/Utilisateur:OviXBot; Toolforge tool ovixbot)"


class FetchResult:
    def __init__(self, ok=False, status=0, kind="other", final_url=None, body=None, error=None, content_type=""):
        self.ok, self.status, self.kind = ok, status, kind  # kind: none/http/dns/connect/timeout/ssl/unsupported/other
        self.final_url, self.body, self.error, self.content_type = final_url, body, error, content_type


def fail(msg, status=0, kind="other"):
    return FetchResult(False, status, kind, error=msg)


def _is_private(host):
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localdomain")):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified


def _resolves_private(host):
    """Protection SSRF : un nom public ne doit pas pointer vers une adresse interne."""
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return True
    except socket.gaierror:
        return False
    return False


def _decode(data, ctype):
    cs = None
    m = re.search(r"charset\s*=\s*[\"']?([\w-]+)", ctype or "", re.I)
    if m:
        cs = m.group(1)
    if cs is None:
        m = re.search(rb"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", data[:4096], re.I)
        if m:
            cs = m.group(1).decode("ascii", "ignore")
    enc = "utf-8"
    if cs:
        try:
            "".encode(cs)
            enc = cs
        except LookupError:
            enc = "utf-8"
    text = data.decode(enc, errors="replace")
    if cs is None and text.count("\ufffd") > 3:
        text = data.decode("cp1252", errors="replace")
    return text.lstrip("\ufeff")


class HttpFetcher:
    _host_lock = threading.Lock()
    _next_slot = {}

    def __init__(self, user_agent=DEFAULT_UA, timeout=12, max_bytes=1_500_000, min_delay=1.0, retry_delay=1.5):
        self.ua, self.timeout, self.max_bytes = user_agent, timeout, max_bytes
        self.min_delay, self.retry_delay = min_delay, retry_delay
        self._cache = {}
        self._lock = threading.Lock()
        self._local = threading.local()

    def _session(self):
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.max_redirects = 5
            s.headers.update({"User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
                              "Accept-Language": "fr,en;q=0.8"})
            self._local.s = s
        return s

    def get(self, url):
        with self._lock:
            if url in self._cache:
                return self._cache[url]
        r = self._download(url)
        with self._lock:
            self._cache[url] = r
        return r

    def get_fresh(self, url):
        time.sleep(self.retry_delay)
        r = self._download(url)
        with self._lock:
            self._cache[url] = r
        return r

    def prefetch(self, urls, parallelism=6):
        with self._lock:
            todo = list(dict.fromkeys(u for u in urls if u not in self._cache))
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=max(1, min(parallelism, 12))) as ex:
            list(ex.map(self._safe_get, todo))

    def _safe_get(self, u):
        try:
            self.get(u)
        except Exception:
            pass

    def _throttle(self, host):
        with self._host_lock:
            now = time.monotonic()
            nxt = max(self._next_slot.get(host, now), now)
            wait = nxt - now
            self._next_slot[host] = nxt + self.min_delay
        if wait > 0:
            time.sleep(wait)

    def _download(self, url):
        try:
            u = urlparse(url)
        except Exception:
            return fail("URL non valide")
        if u.scheme not in ("http", "https") or not u.hostname:
            return fail("URL non valide")
        host = u.hostname.lower()
        if _is_private(host):
            return fail("hôte local ignoré")
        self._throttle(host)
        try:
            resp = self._session().get(url, timeout=(self.timeout, self.timeout), stream=True, allow_redirects=True)
        except requests.exceptions.SSLError:
            return fail("erreur TLS", 0, "ssl")
        except (requests.exceptions.ConnectTimeout, requests.exceptions.ReadTimeout, requests.exceptions.Timeout):
            return fail("délai dépassé", 0, "timeout")
        except requests.exceptions.TooManyRedirects:
            return fail("trop de redirections", 0, "other")
        except requests.exceptions.ConnectionError as e:
            msg = str(e)
            if re.search(r"Name or service not known|NameResolution|getaddrinfo|nodename nor servname|No address associated|Temporary failure in name", msg, re.I):
                return fail("domaine introuvable (DNS)", 0, "dns")
            if re.search(r"refused|Errno 111|No route|unreachable|Errno 113|Errno 101", msg, re.I):
                return fail("connexion refusée", 0, "connect")
            if re.search(r"timed out", msg, re.I):
                return fail("délai dépassé", 0, "timeout")
            return fail(msg[:200], 0, "other")
        except Exception as e:  # noqa
            return fail(str(e)[:200])
        try:
            # SSRF : l'IP réellement jointe ne doit pas être interne (après redirections)
            final_host = urlparse(resp.url).hostname or host
            if final_host != host and (_is_private(final_host) or _resolves_private(final_host)):
                return fail("redirection vers un hôte local ignorée")
            if resp.status_code >= 400:
                return fail(f"HTTP {resp.status_code}", resp.status_code, "http")
            ct = resp.headers.get("Content-Type", "") or ""
            lct = ct.lower()
            if not any(k in lct for k in ("html", "xml", "json", "text/")):
                return fail("type non pris en charge (" + (ct.split(";")[0] or "inconnu") + ")", resp.status_code, "unsupported")
            buf = bytearray()
            for chunk in resp.iter_content(16384):
                buf += chunk
                if len(buf) >= self.max_bytes:
                    break
            return FetchResult(True, resp.status_code, "none", resp.url, _decode(bytes(buf), ct), None, ct)
        except requests.exceptions.Timeout:
            return fail("délai dépassé", 0, "timeout")
        except Exception as e:  # noqa
            return fail(str(e)[:200])
        finally:
            resp.close()
