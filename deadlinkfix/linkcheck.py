"""Vérification HTTP des liens (port de LinkChecking.cs).

Principes : redirections suivies à la main ; SEULS 404/410 (confirmés avec un second User-Agent) et « domaine inexistant »
(hors panne de connexion locale) sont « mort » ; tout le reste (401/403/429/5xx, timeout, TLS, boucle, hors-ligne) est
« indéterminé » : jamais de modification.
"""
import ipaddress
import socket
import threading
import time
from urllib.parse import urljoin

import requests

from .urltools import parse_http

ALIVE, DEAD, UNKNOWN = "alive", "dead", "unknown"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")
_NXDOMAIN = {getattr(socket, n) for n in ("EAI_NONAME", "EAI_NODATA") if hasattr(socket, n)}


def bot_user_agent(account="Sabrsl"):
    return f"Mozilla/5.0 (compatible; DeadLinkFix/1.0; +https://fr.wikipedia.org/wiki/Utilisateur:{account})"


class LinkCheck:
    __slots__ = ("url", "state", "status", "final_url", "redirects", "reason")

    def __init__(self, url, state, status=0, final_url=None, redirects=0, reason=""):
        self.url, self.state, self.status, self.final_url, self.redirects, self.reason = url, state, status, final_url, redirects, reason


def alive(url, final_url=None, redirects=0, status=200):
    return LinkCheck(url, ALIVE, status, final_url or url, redirects, "OK")


def dead(url, status, reason):
    return LinkCheck(url, DEAD, status, None, 0, reason)


def unknown(url, status, reason):
    return LinkCheck(url, UNKNOWN, status, None, 0, reason)


class _Probe:
    def __init__(self, status=0, final_url=None, hops=0, error=None, problem=None, dns=False):
        self.status, self.final_url, self.hops, self.error, self.problem, self.dns = status, final_url, hops, error, problem, dns


class HttpLinkChecker:
    """Thread-safe ; ne lève jamais d'exception."""

    def __init__(self, user_agent=None, timeout=20, max_redirects=8, retries=1, retry_delay=2.0, online_probe=None, allow_private=False):
        self.ua = user_agent or bot_user_agent()
        self.timeout, self.max_redirects, self.retries, self.retry_delay = timeout, max_redirects, retries, retry_delay
        self.allow_private = allow_private  # tests uniquement : jamais en production (protection SSRF)
        self._online_probe = online_probe or self._default_online
        self._lock = threading.Lock()
        self._online = (0.0, True)

    # ------------------------------------------------------------ réseau local
    def online(self):
        return self._online_probe()

    def _default_online(self):
        with self._lock:
            ts, val = self._online
            if time.time() - ts < 60:
                return val
        try:
            ok = bool(socket.getaddrinfo("www.wikipedia.org", 443))
        except OSError:
            ok = False
        with self._lock:
            self._online = (time.time(), ok)
        return ok

    @staticmethod
    def _resolve(host):
        """None si l'hôte est public ; sinon une _Probe décrivant le problème (DNS, hôte privé)."""
        try:
            infos = socket.getaddrinfo(host.strip("[]"), None, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            return _Probe(error="gaierror", dns=e.errno in _NXDOMAIN)
        except (OSError, UnicodeError):
            return _Probe(error="resolve")
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0].split("%")[0])
            except ValueError:
                continue
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified or ip.is_reserved or ip.is_multicast:
                return _Probe(problem="hôte local ou privé")
        return None

    # ------------------------------------------------------------ API
    def check(self, url):
        try:
            u = parse_http(url)
            if u is None:
                return unknown(url, 0, "URL non HTTP(S)")
            p = None
            for attempt in range(self.retries + 1):
                p = self._run(u.absolute, self.ua)
                if not self._transient(p):
                    break
                if attempt < self.retries and self.retry_delay > 0:
                    time.sleep(self.retry_delay)
            return self._interpret(url, u, p)
        except Exception as e:  # noqa
            return unknown(url, 0, "exception : " + type(e).__name__)

    def _interpret(self, url, u, p):
        if p.error:
            if p.dns:
                try:
                    on = bool(self.online())
                except Exception:  # noqa
                    on = False
                return dead(url, 0, "domaine introuvable (DNS)") if on else unknown(url, 0, "DNS en échec mais pas de connexion Internet")
            return unknown(url, 0, "erreur réseau : " + str(p.error))
        if p.problem:
            return unknown(url, p.status, p.problem)
        if 200 <= p.status < 300:
            return alive(url, p.final_url, p.hops, p.status)
        if p.status in (404, 410):
            p2 = self._run(u.absolute, BROWSER_UA)  # certains sites répondent 404 aux robots
            if not p2.error and not p2.problem:
                if p2.status in (404, 410):
                    return dead(url, p2.status, f"HTTP {p2.status} confirmé")
                if 200 <= p2.status < 300:
                    return alive(url, p2.final_url, p2.hops, p2.status)
            return unknown(url, p.status, f"HTTP {p.status} non confirmé")
        return unknown(url, p.status, f"HTTP {p.status}")

    @staticmethod
    def _transient(p):
        if p.error:
            return True
        if p.problem:
            return False
        return p.status in (408, 429) or p.status >= 500

    def _run(self, start_url, user_agent):
        current, visited, hops = start_url, {start_url}, 0
        headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
                   "Accept-Language": "fr,en;q=0.8"}
        with requests.Session() as s:
            while True:
                cu = parse_http(current)
                if cu is None:
                    return _Probe(hops=hops, problem="URL de redirection invalide")
                bad = None if self.allow_private else self._resolve(cu.host)
                if bad is not None:
                    bad.hops = hops
                    return bad
                try:
                    r = s.get(current, headers=headers, allow_redirects=False, stream=True, timeout=self.timeout)
                except requests.RequestException as e:
                    return _Probe(hops=hops, error=type(e).__name__)
                try:
                    code = r.status_code
                    if 300 <= code < 400 and code != 304:
                        loc = r.headers.get("Location")
                        if not loc:
                            return _Probe(status=code, hops=hops, problem="redirection sans en-tête Location")
                        nxt = urljoin(current, loc)
                        nu = parse_http(nxt)
                        if nu is None:
                            return _Probe(status=code, hops=hops, problem="redirection vers un schéma non HTTP")
                        hops += 1
                        if hops > self.max_redirects:
                            return _Probe(status=code, hops=hops, problem="trop de redirections")
                        if nu.absolute in visited:
                            return _Probe(status=code, hops=hops, problem="boucle de redirections")
                        visited.add(nu.absolute)
                        current = nu.absolute
                        continue
                    return _Probe(status=code, final_url=current, hops=hops)
                finally:
                    r.close()


class CachingLinkChecker:
    """Cache mémoire (vivant/mort uniquement)."""

    def __init__(self, inner, ttl=1800, clock=time.time):
        self.inner, self.ttl, self.clock = inner, ttl, clock
        self._cache, self._lock = {}, threading.Lock()

    def online(self):
        return self.inner.online() if hasattr(self.inner, "online") else True

    def check(self, url):
        with self._lock:
            hit = self._cache.get(url)
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        r = self.inner.check(url)
        if r is not None and r.state != UNKNOWN:
            with self._lock:
                if len(self._cache) > 5000:
                    self._cache.clear()
                self._cache[url] = (self.clock(), r)
        return r
