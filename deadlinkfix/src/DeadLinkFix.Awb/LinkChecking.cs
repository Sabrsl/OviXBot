using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Threading;

namespace DeadLinkFix.Awb
{
    public enum LinkState
    {
        /// <summary>La ressource répond (2xx, éventuellement après redirections).</summary>
        Alive,
        /// <summary>Erreur PERMANENTE confirmée (404/410 confirmés, domaine inexistant).</summary>
        Dead,
        /// <summary>Indéterminé : erreur temporaire, blocage anti-robot, timeout, hors-ligne... Jamais de modification.</summary>
        Unknown,
    }

    public sealed class LinkCheckResult
    {
        public string Url { get; private set; }
        public LinkState State { get; private set; }
        /// <summary>Dernier code HTTP obtenu (0 si aucune réponse).</summary>
        public int StatusCode { get; private set; }
        /// <summary>URL finale après redirections (renseignée si State == Alive).</summary>
        public string FinalUrl { get; private set; }
        public int RedirectCount { get; private set; }
        public string Reason { get; private set; }

        public LinkCheckResult(string url, LinkState state, int statusCode, string finalUrl, int redirectCount, string reason)
        {
            Url = url; State = state; StatusCode = statusCode; FinalUrl = finalUrl; RedirectCount = redirectCount; Reason = reason;
        }

        public static LinkCheckResult Alive(string url, string finalUrl, int redirects)
        {
            return new LinkCheckResult(url, LinkState.Alive, 200, finalUrl ?? url, redirects, "OK");
        }
        public static LinkCheckResult Dead(string url, int status, string reason)
        {
            return new LinkCheckResult(url, LinkState.Dead, status, null, 0, reason);
        }
        public static LinkCheckResult Unknown(string url, int status, string reason)
        {
            return new LinkCheckResult(url, LinkState.Unknown, status, null, 0, reason);
        }
    }

    public interface ILinkChecker
    {
        /// <summary>Ne doit jamais lever d'exception. Doit être thread-safe.</summary>
        LinkCheckResult Check(string url);
    }

    /// <summary>
    /// Vérificateur HTTP. Principes :
    ///  - redirections suivies à la main (on veut l'URL finale et la chaîne) ;
    ///  - SEULS 404/410 (confirmés avec un second User-Agent) et « domaine inexistant » (hors panne locale) sont « Dead » ;
    ///  - tout le reste (401/403/429/5xx, timeout, TLS, boucle, hors-ligne) est « Unknown ».
    /// </summary>
    public sealed class HttpLinkChecker : ILinkChecker, IDisposable
    {
        public const string BotUserAgent = "Mozilla/5.0 (compatible; DeadLinkFix/1.0; +https://fr.wikipedia.org/wiki/Utilisateur:Sabrsl)";
        public const string BrowserUserAgent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

        private readonly HttpClient _client;
        private static readonly object OnlineLock = new object();
        private static DateTime _onlineCheckedAt = DateTime.MinValue;
        private static bool _onlineValue;

        public int MaxRedirects { get; set; }
        public int Retries { get; set; }
        public TimeSpan RetryDelay { get; set; }
        /// <summary>Sert à distinguer « domaine inexistant » d'une panne de connexion locale.</summary>
        public Func<bool> OnlineProbe { get; set; }

        public HttpLinkChecker() : this(TimeSpan.FromSeconds(20)) { }

        public HttpLinkChecker(TimeSpan timeout)
        {
            var handler = new HttpClientHandler
            {
                AllowAutoRedirect = false,
                UseCookies = true,
                CookieContainer = new CookieContainer(),
                AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate,
            };
            _client = new HttpClient(handler) { Timeout = timeout };
            MaxRedirects = 8;
            Retries = 1;
            RetryDelay = TimeSpan.FromSeconds(2);
            OnlineProbe = DefaultOnlineProbe;
        }

        public void Dispose() { _client.Dispose(); }

        private sealed class Probe
        {
            public int Status;
            public string FinalUrl;
            public int Hops;
            public Exception Error;
            public string Problem;      // boucle, trop de redirections, Location absente...
        }

        public LinkCheckResult Check(string url)
        {
            try
            {
                Uri u;
                if (!UrlTools.TryParseHttp(url, out u))
                    return LinkCheckResult.Unknown(url, 0, "URL non HTTP(S)");

                Probe p = null;
                for (int attempt = 0; attempt <= Retries; attempt++)
                {
                    p = Run(u.AbsoluteUri, BotUserAgent);
                    if (!IsTransient(p)) break;
                    if (attempt < Retries && RetryDelay > TimeSpan.Zero) Thread.Sleep(RetryDelay);
                }
                return Interpret(url, u, p);
            }
            catch (Exception ex)
            {
                return LinkCheckResult.Unknown(url, 0, "exception : " + ex.GetType().Name);
            }
        }

        private LinkCheckResult Interpret(string url, Uri u, Probe p)
        {
            if (p.Error != null)
            {
                if (IsNameResolutionFailure(p.Error))
                {
                    bool online;
                    try { online = OnlineProbe == null || OnlineProbe(); } catch (Exception) { online = false; }
                    return online
                        ? LinkCheckResult.Dead(url, 0, "domaine introuvable (DNS)")
                        : LinkCheckResult.Unknown(url, 0, "DNS en échec mais pas de connexion Internet");
                }
                return LinkCheckResult.Unknown(url, 0, "erreur réseau : " + p.Error.GetType().Name);
            }
            if (p.Problem != null) return LinkCheckResult.Unknown(url, p.Status, p.Problem);

            if (p.Status >= 200 && p.Status < 300)
                return LinkCheckResult.Alive(url, p.FinalUrl, p.Hops);

            if (p.Status == 404 || p.Status == 410)
            {
                // Confirmation avec un autre User-Agent : certains sites répondent 404 aux robots.
                Probe p2 = Run(u.AbsoluteUri, BrowserUserAgent);
                if (p2.Error == null && p2.Problem == null)
                {
                    if (p2.Status == 404 || p2.Status == 410)
                        return LinkCheckResult.Dead(url, p2.Status, "HTTP " + p2.Status + " confirmé");
                    if (p2.Status >= 200 && p2.Status < 300)
                        return LinkCheckResult.Alive(url, p2.FinalUrl, p2.Hops);
                }
                return LinkCheckResult.Unknown(url, p.Status, "HTTP " + p.Status + " non confirmé");
            }

            return LinkCheckResult.Unknown(url, p.Status, "HTTP " + p.Status);
        }

        private bool IsTransient(Probe p)
        {
            if (p.Error != null) return true;
            if (p.Problem != null) return false;
            return p.Status == 408 || p.Status == 429 || p.Status >= 500;
        }

        private Probe Run(string startUrl, string userAgent)
        {
            string current = startUrl;
            var visited = new HashSet<string>(StringComparer.Ordinal) { current };
            int hops = 0;

            while (true)
            {
                HttpResponseMessage resp;
                try
                {
                    using (var req = new HttpRequestMessage(HttpMethod.Get, current))
                    {
                        req.Headers.TryAddWithoutValidation("User-Agent", userAgent);
                        req.Headers.TryAddWithoutValidation("Accept", "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8");
                        req.Headers.TryAddWithoutValidation("Accept-Language", "fr,en;q=0.8");
                        resp = _client.SendAsync(req, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false).GetAwaiter().GetResult();
                    }
                }
                catch (Exception ex)
                {
                    return new Probe { Error = ex, Hops = hops };
                }

                using (resp)
                {
                    int code = (int)resp.StatusCode;
                    if (code >= 300 && code < 400 && code != 304)
                    {
                        Uri loc = resp.Headers.Location;
                        if (loc == null)
                            return new Probe { Status = code, Hops = hops, Problem = "redirection sans en-tête Location" };

                        Uri next;
                        try { next = loc.IsAbsoluteUri ? loc : new Uri(new Uri(current), loc); }
                        catch (Exception) { return new Probe { Status = code, Hops = hops, Problem = "Location invalide" }; }

                        if (next.Scheme != Uri.UriSchemeHttp && next.Scheme != Uri.UriSchemeHttps)
                            return new Probe { Status = code, Hops = hops, Problem = "redirection vers un schéma non HTTP" };

                        hops++;
                        string nextUrl = next.AbsoluteUri;
                        if (hops > MaxRedirects)
                            return new Probe { Status = code, Hops = hops, Problem = "trop de redirections" };
                        if (!visited.Add(nextUrl))
                            return new Probe { Status = code, Hops = hops, Problem = "boucle de redirections" };
                        current = nextUrl;
                        continue;
                    }
                    return new Probe { Status = code, FinalUrl = current, Hops = hops };
                }
            }
        }

        /// <summary>Vrai si l'exception (ou une cause) signifie « nom de domaine inexistant » (et non « DNS indisponible »).</summary>
        public static bool IsNameResolutionFailure(Exception ex)
        {
            for (Exception e = ex; e != null; e = e.InnerException)
            {
                var we = e as WebException;
                if (we != null && we.Status == WebExceptionStatus.NameResolutionFailure) return true;
                var se = e as SocketException;
                if (se != null && (se.SocketErrorCode == SocketError.HostNotFound || se.SocketErrorCode == SocketError.NoData))
                    return true;
            }
            return false;
        }

        private static bool DefaultOnlineProbe()
        {
            lock (OnlineLock)
            {
                if ((DateTime.UtcNow - _onlineCheckedAt) < TimeSpan.FromSeconds(60)) return _onlineValue;
                bool ok;
                try { ok = Dns.GetHostEntry("www.wikipedia.org").AddressList.Length > 0; }
                catch (Exception) { ok = false; }
                _onlineValue = ok;
                _onlineCheckedAt = DateTime.UtcNow;
                return ok;
            }
        }
    }

    /// <summary>Cache mémoire (Alive/Dead uniquement) : évite de revérifier la même URL d'un article à l'autre.</summary>
    public sealed class CachingLinkChecker : ILinkChecker
    {
        private readonly ILinkChecker _inner;
        private readonly TimeSpan _ttl;
        private readonly Func<DateTime> _now;
        private readonly ConcurrentDictionary<string, KeyValuePair<DateTime, LinkCheckResult>> _cache =
            new ConcurrentDictionary<string, KeyValuePair<DateTime, LinkCheckResult>>(StringComparer.Ordinal);

        public CachingLinkChecker(ILinkChecker inner, TimeSpan ttl) : this(inner, ttl, () => DateTime.UtcNow) { }

        public CachingLinkChecker(ILinkChecker inner, TimeSpan ttl, Func<DateTime> now)
        {
            if (inner == null) throw new ArgumentNullException("inner");
            _inner = inner; _ttl = ttl; _now = now;
        }

        public LinkCheckResult Check(string url)
        {
            KeyValuePair<DateTime, LinkCheckResult> hit;
            if (_cache.TryGetValue(url, out hit) && (_now() - hit.Key) < _ttl) return hit.Value;

            LinkCheckResult r = _inner.Check(url);
            if (r != null && r.State != LinkState.Unknown)
                _cache[url] = new KeyValuePair<DateTime, LinkCheckResult>(_now(), r);
            return r;
        }
    }
}
