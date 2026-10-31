using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Text;
using System.Text.RegularExpressions;

namespace DeadLinkFix.Awb
{
    /// <summary>Outils d'analyse d'URL : comparaison de domaines, forme canonique, indices de correspondance.</summary>
    public static class UrlTools
    {
        // Suffixes publics à deux niveaux (liste volontairement courte : sert uniquement à déterminer le « domaine »).
        private static readonly HashSet<string> TwoLevelSuffixes = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "com.au", "net.au", "org.au", "co.nz", "co.jp", "co.za",
            "com.br", "com.mx", "com.ar", "com.tr", "com.cn", "co.in", "co.kr", "com.sg", "com.hk", "com.tw",
            "com.ma", "co.ma", "gov.ma", "org.ma", "ac.ma", "com.dz", "com.tn", "gov.tn", "com.eg", "gov.eg",
            "com.ng", "gov.ng", "org.ng", "co.ke", "go.ke", "or.ke", "co.tz", "go.tz", "co.ug", "co.zw",
            "com.gh", "gov.gh", "com.cm", "gov.cm", "com.cd", "co.cd", "co.rw", "com.rw", "com.sn", "org.sn",
            "gouv.sn", "gouv.ci", "gouv.bj", "gouv.ml", "gouv.bf", "gouv.ne", "gouv.tg", "gouv.ga", "gouv.cd",
            "gouv.cg", "gouv.mg", "gouv.km", "gouv.dj", "gouv.mr", "gouv.gn", "gouv.td", "gouv.cf", "gouv.ht",
            "asso.fr", "com.fr", "gouv.qc.ca", "co.il", "org.il", "com.ua", "com.pl", "com.pt", "com.es", "com.ve",
        };

        // Domaines d'hébergement mutualisé : deux sous-domaines n'y désignent PAS le même site.
        private static readonly HashSet<string> SharedHostingDomains = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            "blogspot.com", "blogspot.fr", "wordpress.com", "github.io", "gitlab.io", "herokuapp.com", "netlify.app",
            "vercel.app", "pages.dev", "wixsite.com", "weebly.com", "tumblr.com", "medium.com", "over-blog.com",
            "over-blog.fr", "over-blog.org", "canalblog.com", "free.fr", "sites.google.com", "googleusercontent.com",
            "azurewebsites.net", "appspot.com", "web.app", "firebaseapp.com", "unblog.fr", "skyrock.com",
            "jimdofree.com", "jimdo.com", "yolasite.com", "livejournal.com", "hautetfort.com", "e-monsite.com",
            "webnode.fr", "000webhostapp.com", "onrender.com", "fly.dev", "glitch.me", "repl.co", "notion.site",
            "substack.com", "bandcamp.com", "deviantart.com", "hubpages.com", "ovh.net", "amazonaws.com",
            "cloudfront.net", "wixstudio.io", "framer.website", "webflow.io", "carrd.co", "mystrikingly.com",
        };

        private static readonly string[] ArchiveHosts =
        {
            "archive.org", "archive.ph", "archive.is", "archive.today", "archive.li", "archive.fo", "archive.md",
            "archive.vn", "webcitation.org", "archive-it.org", "arquivo.pt", "perma.cc", "webarchive.org.uk",
        };

        private static readonly Regex IndexFile = new Regex(
            @"/(?:index|default|home)\.(?:html?|php\d?|aspx?|jsp|shtml|cfm)$",
            RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);

        private static readonly Regex SlugExtension = new Regex(
            @"\.(?:html?|php\d?|aspx?|jsp|shtml|cfm|pdf)$",
            RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);

        private static readonly Regex DigitRun = new Regex(@"\d{5,}", RegexOptions.CultureInvariant);

        private static readonly Regex ErrorMarker = new Regex(
            @"(?:^|[/_\-?=&.])(?:404|erreur|error|not-?found|page-?not-?found|page-?introuvable|introuvable|oops|removed|expired)(?:[/_\-?=&.]|$)",
            RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);

        private static readonly Regex AccessWallMarker = new Regex(
            @"(?:^|[/_\-?=&.])(?:login|log-in|signin|sign-in|connexion|identification|consent|captcha|cookies?|paywall|subscribe|abonnement|auth|sso)(?:[/_\-?=&.]|$)",
            RegexOptions.CultureInvariant | RegexOptions.IgnoreCase);

        public static bool TryParseHttp(string url, out Uri uri)
        {
            uri = null;
            if (string.IsNullOrWhiteSpace(url)) return false;
            Uri u;
            if (!Uri.TryCreate(url.Trim(), UriKind.Absolute, out u)) return false;
            if (u.Scheme != Uri.UriSchemeHttp && u.Scheme != Uri.UriSchemeHttps) return false;
            if (string.IsNullOrEmpty(u.Host)) return false;
            uri = u;
            return true;
        }

        public static string HostNoWww(Uri u)
        {
            string h = u.Host.ToLowerInvariant();
            if (h.StartsWith("www.", StringComparison.Ordinal) && h.Length > 4) h = h.Substring(4);
            return h;
        }

        public static string RegistrableDomain(string host)
        {
            IPAddress ip;
            if (IPAddress.TryParse(host.Trim('[', ']'), out ip)) return host;
            string[] labels = host.Split('.');
            if (labels.Length <= 2) return host;
            string lastTwo = labels[labels.Length - 2] + "." + labels[labels.Length - 1];
            int take = TwoLevelSuffixes.Contains(lastTwo) ? 3 : 2;
            if (labels.Length < take) return host;
            return string.Join(".", labels.Skip(labels.Length - take));
        }

        /// <summary>Même site : même hôte (www ignoré) ou même domaine enregistrable hors hébergement mutualisé.</summary>
        public static bool SameSite(Uri a, Uri b)
        {
            string ha = HostNoWww(a), hb = HostNoWww(b);
            if (ha == hb) return true;
            string ra = RegistrableDomain(ha), rb = RegistrableDomain(hb);
            if (!string.Equals(ra, rb, StringComparison.OrdinalIgnoreCase)) return false;
            return !SharedHostingDomains.Contains(ra);
        }

        public static string SafeUnescape(string s)
        {
            if (string.IsNullOrEmpty(s)) return string.Empty;
            try { return Uri.UnescapeDataString(s); }
            catch (Exception) { return s; }
        }

        public static string CanonicalPath(Uri u)
        {
            string path = SafeUnescape(u.AbsolutePath).ToLowerInvariant();
            path = IndexFile.Replace(path, "/");
            return path.TrimEnd('/');
        }

        public static string CanonicalQuery(Uri u)
        {
            string q = u.Query;
            if (string.IsNullOrEmpty(q) || q == "?") return string.Empty;
            return SafeUnescape(q).ToLowerInvariant();
        }

        /// <summary>Forme canonique : sans schéma, sans « www. », sans fragment, sans slash final ni index.html, en minuscules.</summary>
        public static string Canonical(Uri u)
        {
            string port = u.IsDefaultPort ? string.Empty : ":" + u.Port.ToString(CultureInfo.InvariantCulture);
            return HostNoWww(u) + port + CanonicalPath(u) + CanonicalQuery(u);
        }

        public static string Canonical(string url)
        {
            Uri u;
            return TryParseHttp(url, out u) ? Canonical(u) : (url ?? string.Empty).Trim().ToLowerInvariant();
        }

        /// <summary>Deux URL désignent la même ressource (schéma, www, fragment, slash final ignorés).</summary>
        public static bool SameResource(string a, string b)
        {
            Uri ua, ub;
            if (!TryParseHttp(a, out ua) || !TryParseHttp(b, out ub)) return false;
            return string.Equals(Canonical(ua), Canonical(ub), StringComparison.Ordinal);
        }

        public static bool IsRoot(Uri u)
        {
            return CanonicalPath(u).Length == 0 && CanonicalQuery(u).Length == 0;
        }

        /// <summary>Dernier segment de chemin normalisé (sans extension, sans accents, alphanumérique minuscule).</summary>
        public static string SlugKey(Uri u)
        {
            string[] segs = SafeUnescape(u.AbsolutePath).Split(new[] { '/' }, StringSplitOptions.RemoveEmptyEntries);
            for (int i = segs.Length - 1; i >= 0; i--)
            {
                string s = SlugExtension.Replace(segs[i], string.Empty);
                string key = StripToAlnum(s);
                if (key.Length == 0) continue;
                if (key == "index" || key == "default" || key == "home" || key == "accueil") continue;
                return key;
            }
            return string.Empty;
        }

        private static string StripToAlnum(string s)
        {
            string d = s.Normalize(NormalizationForm.FormD);
            var sb = new StringBuilder(d.Length);
            foreach (char c in d)
            {
                if (CharUnicodeInfo.GetUnicodeCategory(c) == UnicodeCategory.NonSpacingMark) continue;
                if (char.IsLetterOrDigit(c)) sb.Append(char.ToLowerInvariant(c));
            }
            return sb.ToString();
        }

        /// <summary>Identifiants numériques (≥ 5 chiffres) présents dans le chemin ou la requête.</summary>
        public static HashSet<string> NumericIds(Uri u)
        {
            var set = new HashSet<string>(StringComparer.Ordinal);
            foreach (Match m in DigitRun.Matches(SafeUnescape(u.AbsolutePath) + SafeUnescape(u.Query)))
                set.Add(m.Value);
            return set;
        }

        public static bool LooksLikeErrorPage(Uri u)
        {
            return ErrorMarker.IsMatch(SafeUnescape(u.AbsolutePath) + SafeUnescape(u.Query));
        }

        public static bool LooksLikeAccessWall(Uri u)
        {
            return AccessWallMarker.IsMatch(u.Host + SafeUnescape(u.AbsolutePath) + SafeUnescape(u.Query));
        }

        public static bool IsArchiveHost(Uri u)
        {
            string h = u.Host.ToLowerInvariant();
            foreach (string a in ArchiveHosts)
                if (h == a || h.EndsWith("." + a, StringComparison.Ordinal)) return true;
            return false;
        }

        /// <summary>Faux pour les hôtes locaux / privés / mono-label et pour les services d'archive.</summary>
        public static bool IsCheckable(Uri u)
        {
            string h = u.Host.ToLowerInvariant();
            if (h == "localhost" || h.EndsWith(".local", StringComparison.Ordinal) ||
                h.EndsWith(".localhost", StringComparison.Ordinal) || h.EndsWith(".internal", StringComparison.Ordinal))
                return false;

            IPAddress ip;
            if (IPAddress.TryParse(h.Trim('[', ']'), out ip))
            {
                if (IPAddress.IsLoopback(ip)) return false;
                byte[] b = ip.GetAddressBytes();
                if (b.Length == 4)
                {
                    if (b[0] == 10 || b[0] == 0) return false;
                    if (b[0] == 192 && b[1] == 168) return false;
                    if (b[0] == 172 && b[1] >= 16 && b[1] <= 31) return false;
                    if (b[0] == 169 && b[1] == 254) return false;
                }
            }
            else if (h.IndexOf('.') < 0)
            {
                return false;
            }
            return !IsArchiveHost(u);
        }

        /// <summary>Caractères qui casseraient la syntaxe d'un paramètre de modèle.</summary>
        public static bool IsWikiSafe(string url)
        {
            if (string.IsNullOrEmpty(url) || url.Length > 2000) return false;
            foreach (char c in url)
            {
                if (char.IsWhiteSpace(c) || char.IsControl(c)) return false;
                if (c == '|' || c == '{' || c == '}' || c == '[' || c == ']' || c == '<' || c == '>' || c == '"') return false;
            }
            return true;
        }
    }
}
