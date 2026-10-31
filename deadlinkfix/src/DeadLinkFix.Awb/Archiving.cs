using System;
using System.Globalization;
using System.Net;
using System.Net.Http;
using System.Text.RegularExpressions;

namespace DeadLinkFix.Awb
{
    public sealed class ArchiveSnapshot
    {
        public string ArchiveUrl { get; private set; }
        public string OriginalUrl { get; private set; }
        public DateTime DateUtc { get; private set; }
        public string Provider { get; private set; }

        public ArchiveSnapshot(string archiveUrl, string originalUrl, DateTime dateUtc, string provider)
        {
            ArchiveUrl = archiveUrl; OriginalUrl = originalUrl; DateUtc = dateUtc; Provider = provider;
        }
    }

    public interface IArchiveProvider
    {
        string Name { get; }
        /// <summary>Renvoie null si aucune capture VALIDÉE (même domaine, même lien). Ne doit jamais lever d'exception.</summary>
        ArchiveSnapshot FindClosest(string url);
    }

    internal static class ArchiveTools
    {
        // .../web/20200312101010/http://example.com/x   |   .../20200312101010/http://example.com/x   |   .../web/20200312101010id_/http://...
        private static readonly Regex SnapshotUrl = new Regex(
            @"^https?://[^/]+/(?:web/)?(?<ts>\d{14})(?:[a-z]{2}_)?/(?<orig>https?:/{1,2}.+)$",
            RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);

        /// <summary>Valide qu'une URL de capture correspond bien à l'URL demandée (domaine ET lien).</summary>
        public static ArchiveSnapshot TryBuild(string provider, string snapshotUrl, string requestedUrl)
        {
            if (string.IsNullOrWhiteSpace(snapshotUrl)) return null;
            Match m = SnapshotUrl.Match(snapshotUrl.Trim());
            if (!m.Success) return null;

            string orig = m.Groups["orig"].Value;
            // Certaines captures écrasent « // » en « / ».
            orig = Regex.Replace(orig, @"^(https?):/(?!/)", "$1://", RegexOptions.IgnoreCase);

            if (!UrlTools.SameResource(orig, requestedUrl)) return null;

            DateTime d;
            if (!DateTime.TryParseExact(m.Groups["ts"].Value, "yyyyMMddHHmmss", CultureInfo.InvariantCulture,
                    DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out d))
                return null;
            if (d > DateTime.UtcNow.AddDays(2)) return null;

            return new ArchiveSnapshot(snapshotUrl.Trim(), orig, d, provider);
        }

        public static string ForceHttps(string url)
        {
            if (url != null && url.StartsWith("http://", StringComparison.OrdinalIgnoreCase))
            {
                Uri u;
                if (Uri.TryCreate(url, UriKind.Absolute, out u) &&
                    (u.Host.EndsWith("archive.org", StringComparison.OrdinalIgnoreCase) ||
                     u.Host.EndsWith("archive.ph", StringComparison.OrdinalIgnoreCase) ||
                     u.Host.EndsWith("archive.today", StringComparison.OrdinalIgnoreCase) ||
                     u.Host.EndsWith("archive.is", StringComparison.OrdinalIgnoreCase)))
                    return "https" + url.Substring(4);
            }
            return url;
        }

        private static HttpClient _http;
        private static readonly object HttpLock = new object();

        public static HttpClient Http
        {
            get
            {
                lock (HttpLock)
                {
                    if (_http == null)
                    {
                        _http = new HttpClient(new HttpClientHandler { AllowAutoRedirect = true, AutomaticDecompression = DecompressionMethods.GZip | DecompressionMethods.Deflate })
                        {
                            Timeout = TimeSpan.FromSeconds(25),
                        };
                        _http.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent", HttpLinkChecker.BotUserAgent);
                    }
                    return _http;
                }
            }
        }

        public static string HttpGetString(string url)
        {
            try
            {
                using (HttpResponseMessage r = Http.GetAsync(url).ConfigureAwait(false).GetAwaiter().GetResult())
                {
                    if (!r.IsSuccessStatusCode) return null;
                    return r.Content.ReadAsStringAsync().ConfigureAwait(false).GetAwaiter().GetResult();
                }
            }
            catch (Exception) { return null; }
        }
    }

    /// <summary>Internet Archive (API « available »).</summary>
    public sealed class WaybackProvider : IArchiveProvider
    {
        private static readonly Regex Closest = new Regex("\"closest\"\\s*:\\s*\\{(?<b>.*?)\\}", RegexOptions.Singleline | RegexOptions.CultureInvariant);
        private static readonly Regex FUrl = new Regex("\"url\"\\s*:\\s*\"(?<v>[^\"]+)\"", RegexOptions.CultureInvariant);
        private static readonly Regex FStatus = new Regex("\"status\"\\s*:\\s*\"(?<v>\\d+)\"", RegexOptions.CultureInvariant);
        private static readonly Regex FAvail = new Regex("\"available\"\\s*:\\s*true", RegexOptions.CultureInvariant);

        private readonly Func<string, string> _fetch;

        public string ApiBase { get; set; }
        public string Name { get { return "Internet Archive"; } }

        public WaybackProvider() : this(null) { }

        /// <param name="fetch">Récupère le corps d'une URL (null si échec). Par défaut : HTTP réel.</param>
        public WaybackProvider(Func<string, string> fetch)
        {
            _fetch = fetch ?? ArchiveTools.HttpGetString;
            ApiBase = "https://archive.org/wayback/available?url=";
        }

        public ArchiveSnapshot FindClosest(string url)
        {
            try
            {
                string json = _fetch(ApiBase + Uri.EscapeDataString(url));
                return ParseResponse(json, url);
            }
            catch (Exception) { return null; }
        }

        internal ArchiveSnapshot ParseResponse(string json, string requestedUrl)
        {
            if (string.IsNullOrEmpty(json)) return null;
            Match c = Closest.Match(json);
            if (!c.Success) return null;
            string body = c.Groups["b"].Value;

            if (!FAvail.IsMatch(body)) return null;
            Match st = FStatus.Match(body);
            if (!st.Success || st.Groups["v"].Value != "200") return null;
            Match u = FUrl.Match(body);
            if (!u.Success) return null;

            string snap = ArchiveTools.ForceHttps(u.Groups["v"].Value.Replace("\\/", "/"));
            return ArchiveTools.TryBuild(Name, snap, requestedUrl);
        }
    }

    /// <summary>Service compatible Memento (archive.ph, etc.) : le TimeGate renvoie la capture la plus proche via « Location ».</summary>
    public sealed class MementoProvider : IArchiveProvider
    {
        private readonly string _name;
        private readonly string _timegateBase;
        private readonly Func<string, string> _resolveLocation;

        public string Name { get { return _name; } }

        public MementoProvider(string name, string timegateBase) : this(name, timegateBase, null) { }

        /// <param name="resolveLocation">Renvoie l'en-tête Location du TimeGate (null si échec).</param>
        public MementoProvider(string name, string timegateBase, Func<string, string> resolveLocation)
        {
            _name = name;
            _timegateBase = timegateBase;
            _resolveLocation = resolveLocation ?? DefaultResolve;
        }

        public ArchiveSnapshot FindClosest(string url)
        {
            try
            {
                string loc = _resolveLocation(_timegateBase + url);
                if (string.IsNullOrEmpty(loc)) return null;
                return ArchiveTools.TryBuild(_name, ArchiveTools.ForceHttps(loc), url);
            }
            catch (Exception) { return null; }
        }

        private static string DefaultResolve(string timegateUrl)
        {
            try
            {
                using (var handler = new HttpClientHandler { AllowAutoRedirect = false })
                using (var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(25) })
                using (var req = new HttpRequestMessage(HttpMethod.Get, timegateUrl))
                {
                    req.Headers.TryAddWithoutValidation("User-Agent", HttpLinkChecker.BotUserAgent);
                    using (HttpResponseMessage r = client.SendAsync(req, HttpCompletionOption.ResponseHeadersRead).ConfigureAwait(false).GetAwaiter().GetResult())
                    {
                        int code = (int)r.StatusCode;
                        if (code < 300 || code >= 400 || r.Headers.Location == null) return null;
                        return r.Headers.Location.IsAbsoluteUri ? r.Headers.Location.AbsoluteUri : null;
                    }
                }
            }
            catch (Exception) { return null; }
        }
    }
}
