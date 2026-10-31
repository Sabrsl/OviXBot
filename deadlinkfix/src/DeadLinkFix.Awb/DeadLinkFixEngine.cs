using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading.Tasks;

// Ce fichier ne dépend PAS d'AWB (WikiFunctions) : il peut être testé seul.
// C# 7.3 maximum (compatible .NET Framework 4.8.1 / MSBuild d'origine).

namespace DeadLinkFix.Awb
{
    /// <summary>Réglages. Philosophie : uniquement ajouter une archive validée si le lien est mort.</summary>
    public sealed class DeadLinkFixOptions
    {
        public int MaxLinksPerArticle { get; set; }
        public int MaxParallelism { get; set; }
        public int TimeoutSeconds { get; set; }
        /// <summary>« http://x » → « https://x » quand le serveur redirige uniquement vers la version https identique.</summary>
        public bool FixHttpToHttps { get; set; }
        /// <summary>Corrige l'URL quand une redirection mène au même site ET au même document (preuve slug / identifiant / chemin).</summary>
        public bool RepairRedirects { get; set; }
        /// <summary>Ajoute archive-url / archive-date (capture validée) quand le lien est mort et qu'aucune archive n'est renseignée.</summary>
        /// <remarks>Désactivé par défaut : le plugin se concentre uniquement sur la réparation des liens morts et le marquage avec brisé le.</remarks>
        public bool AddArchive { get; set; }
        /// <summary>Ajoute le marqueur « brisé le » quand le lien est mort et qu'une archive est présente.</summary>
        public bool MarkBrokenWithArchive { get; set; }
        /// <summary>Marqueur inséré après le modèle. {date} = date du constat. Ne touche jamais à « consulté le ».</summary>
        public string BrokenMarkerFormat { get; set; }
        public bool UseArchivePh { get; set; }

        public DeadLinkFixOptions()
        {
            MaxLinksPerArticle = 50;
            MaxParallelism = 8;
            TimeoutSeconds = 20;
            FixHttpToHttps = false;  // Désactivé : ne corrige pas http→https
            RepairRedirects = false;  // Désactivé : ne corrige pas les redirections
            AddArchive = true;  // Activé : ajouter archive quand lien mort
            MarkBrokenWithArchive = true;  // Activé : marquer comme brisé quand archive ajoutée
            BrokenMarkerFormat = "{date}";  // Format pour le paramètre brisé le (juste la date)
            UseArchivePh = true;
        }
    }

    /// <summary>Lit un fichier « clé=valeur » (# = commentaire). Toute clé inconnue ou valeur invalide est ignorée.</summary>
    public static class DeadLinkFixOptionsLoader
    {
        public static DeadLinkFixOptions Parse(string content)
        {
            var o = new DeadLinkFixOptions();
            if (string.IsNullOrEmpty(content)) return o;
            foreach (string raw in content.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries))
            {
                string line = raw.Trim();
                if (line.Length == 0 || line[0] == '#' || line[0] == ';') continue;
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                string k = line.Substring(0, eq).Trim().ToLowerInvariant();
                string v = line.Substring(eq + 1).Trim();
                int n; bool b;
                switch (k)
                {
                    case "maxlinksperarticle": if (int.TryParse(v, out n) && n >= 1 && n <= 500) o.MaxLinksPerArticle = n; break;
                    case "maxparallelism": if (int.TryParse(v, out n) && n >= 1 && n <= 64) o.MaxParallelism = n; break;
                    case "timeoutseconds": if (int.TryParse(v, out n) && n >= 3 && n <= 120) o.TimeoutSeconds = n; break;
                    case "fixhttptohttps": if (bool.TryParse(v, out b)) o.FixHttpToHttps = b; break;
                    case "repairredirects": if (bool.TryParse(v, out b)) o.RepairRedirects = b; break;
                    case "addarchive": if (bool.TryParse(v, out b)) o.AddArchive = b; break;
                    case "markbrokenwitharchive": if (bool.TryParse(v, out b)) o.MarkBrokenWithArchive = b; break;
                    case "usearchiveph": if (bool.TryParse(v, out b)) o.UseArchivePh = b; break;
                    case "brokenmarkerformat": if (v.Length > 0 && v.IndexOf("{date}", StringComparison.Ordinal) >= 0) o.BrokenMarkerFormat = v; break;
                }
            }
            return o;
        }

        public static DeadLinkFixOptions LoadFile(string path)
        {
            try { return File.Exists(path) ? Parse(File.ReadAllText(path, Encoding.UTF8)) : new DeadLinkFixOptions(); }
            catch (Exception) { return new DeadLinkFixOptions(); }
        }
    }

    public enum DecisionKind { Keep, Repair, Dead, Unknown }

    public sealed class LinkDecision
    {
        public DecisionKind Kind { get; private set; }
        public string NewUrl { get; private set; }
        public string Reason { get; private set; }
        public ArchiveSnapshot Snapshot { get; set; }

        private LinkDecision(DecisionKind k, string newUrl, string reason) { Kind = k; NewUrl = newUrl; Reason = reason; }
        public static LinkDecision Keep(string r) { return new LinkDecision(DecisionKind.Keep, null, r); }
        public static LinkDecision Unknown(string r) { return new LinkDecision(DecisionKind.Unknown, null, r); }
        public static LinkDecision Dead(string r) { return new LinkDecision(DecisionKind.Dead, null, r); }
        public static LinkDecision Repair(string newUrl, string r) { return new LinkDecision(DecisionKind.Repair, newUrl, r); }
    }

    /// <summary>Transforme un résultat de vérification en décision. Répare les redirections quand elles sont prouvées.</summary>
    public static class LinkAnalyzer
    {
        public static LinkDecision Analyze(string url, LinkCheckResult r, DeadLinkFixOptions opt)
        {
            if (r == null) return LinkDecision.Unknown("pas de résultat");
            if (r.State == LinkState.Unknown) return LinkDecision.Unknown(r.Reason);
            if (r.State == LinkState.Dead) return LinkDecision.Dead(r.Reason);

            // Lien vivant : vérifier s'il y a une redirection réparable
            if (r.State == LinkState.Alive && r.RedirectCount > 0 && r.FinalUrl != null)
            {
                Uri orig, final;
                if (!UrlTools.TryParseHttp(url, out orig) || !UrlTools.TryParseHttp(r.FinalUrl, out final))
                    return LinkDecision.Keep("lien vivant");

                // http → https strictement identique
                if (opt.FixHttpToHttps)
                {
                    if (orig.Scheme == Uri.UriSchemeHttp && final.Scheme == Uri.UriSchemeHttps)
                    {
                        string origCanon = UrlTools.Canonical(orig);
                        string finalCanon = UrlTools.Canonical(final);
                        if (origCanon == finalCanon)
                            return LinkDecision.Repair(r.FinalUrl, "http → https identique");
                    }
                }

                // Redirection vers même domaine et même document
                if (opt.RepairRedirects)
                {
                    if (UrlTools.SameSite(orig, final))
                    {
                        // Preuve par slug (≥ 6 caractères)
                        string origSlug = UrlTools.SlugKey(orig);
                        string finalSlug = UrlTools.SlugKey(final);
                        if (origSlug.Length >= 6 && origSlug == finalSlug)
                            return LinkDecision.Repair(r.FinalUrl, "redirection même slug");

                        // Preuve par identifiant numérique (≥ 5 chiffres)
                        var origIds = UrlTools.NumericIds(orig);
                        var finalIds = UrlTools.NumericIds(final);
                        foreach (string id in origIds)
                        {
                            if (id.Length >= 5 && finalIds.Contains(id))
                                return LinkDecision.Repair(r.FinalUrl, "redirection même identifiant");
                        }

                        // Preuve par chemin identique (sans query/fragment)
                        if (UrlTools.CanonicalPath(orig) == UrlTools.CanonicalPath(final) &&
                            UrlTools.CanonicalQuery(orig) == UrlTools.CanonicalQuery(final))
                            return LinkDecision.Repair(r.FinalUrl, "redirection même chemin");
                    }
                }
            }

            return LinkDecision.Keep("lien vivant");
        }
    }

    public sealed class LinkReport
    {
        public string Url { get; set; }
        public string Outcome { get; set; }   // OK, Inconnu, Corrigé, Mort, Ignoré (limite)
        public string Detail { get; set; }
    }

    public sealed class DeadLinkFixResult
    {
        public string Text { get; internal set; }
        public bool Changed { get; internal set; }
        public int Repaired { get; internal set; }
        public int Archived { get; internal set; }
        public int Marked { get; internal set; }
        public int DeadUnresolved { get; internal set; }
        public int Unknown { get; internal set; }
        public int SkippedOverLimit { get; internal set; }
        public int LinksChecked { get; internal set; }
        public IList<LinkReport> Reports { get; internal set; }
    }

    internal sealed class TemplateSpec
    {
        public string Name;
        public string[] UrlParams;
        public string[] ArchiveUrlParams;      // vide : le modèle n'a pas de paramètres d'archive
        public string[] ArchiveDateParams;
        public string DefaultArchiveUrl;
        public string DefaultArchiveDate;
        /// <summary>« brisé le » n'existe que dans {{Lien web}} : jamais ajouté dans {{Article}} / {{Ouvrage}}.</summary>
        public bool MarkBroken;
        public bool SupportsArchive { get { return ArchiveUrlParams.Length > 0; } }
    }

    public sealed class DeadLinkFixEngine
    {
        private static readonly TemplateSpec[] Specs =
        {
            new TemplateSpec { Name = "lien web", UrlParams = new[] { "url" },
                ArchiveUrlParams = new[] { "archive-url", "archiveurl" }, ArchiveDateParams = new[] { "archive-date", "archivedate" },
                DefaultArchiveUrl = "archive-url", DefaultArchiveDate = "archive-date", MarkBroken = true },
            new TemplateSpec { Name = "ouvrage", UrlParams = new[] { "lire en ligne", "présentation en ligne", "écouter en ligne" },
                ArchiveUrlParams = new string[0], ArchiveDateParams = new string[0] },
            new TemplateSpec { Name = "article", UrlParams = new[] { "lire en ligne" },
                ArchiveUrlParams = new[] { "archiveurl", "archive-url" }, ArchiveDateParams = new[] { "archivedate", "archive-date" },
                DefaultArchiveUrl = "archiveurl", DefaultArchiveDate = "archivedate" },
        };

        private static readonly string[] Months = { "janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre" };

        private readonly ILinkChecker _checker;
        private readonly IList<IArchiveProvider> _providers;
        private readonly DeadLinkFixOptions _opt;
        private readonly Func<DateTime> _clock;

        public DeadLinkFixEngine(ILinkChecker checker, IEnumerable<IArchiveProvider> providers, DeadLinkFixOptions options, Func<DateTime> clock)
        {
            if (checker == null) throw new ArgumentNullException("checker");
            _checker = checker;
            _providers = (providers ?? new IArchiveProvider[0]).ToList();
            _opt = options ?? new DeadLinkFixOptions();
            _clock = clock ?? (() => DateTime.Now);
        }

        public static string FrenchDate(DateTime d)
        {
            return (d.Day == 1 ? "1er" : d.Day.ToString(CultureInfo.InvariantCulture)) + " " + Months[d.Month - 1] + " " + d.Year.ToString(CultureInfo.InvariantCulture);
        }

        private sealed class Site
        {
            public int BodyOffset;
            public ParsedTemplate T;
            public TemplateSpec Spec;
            public TemplateParam UrlParam;
            public string Url;
            public string Body;
        }

        private sealed class Edit
        {
            public int Start, End, Seq;
            public string NewText;
        }

        public DeadLinkFixResult Fix(string text)
        {
            if (text == null) throw new ArgumentNullException("text");
            var result = new DeadLinkFixResult { Text = text, Reports = new List<LinkReport>() };
            if (text.IndexOf("<ref", StringComparison.OrdinalIgnoreCase) < 0) return result;

            string masked = WikiScan.Mask(text);
            var sites = CollectSites(text, masked);
            if (sites.Count == 0) return result;

            // URL distinctes, dans l'ordre d'apparition, limitées.
            var all = new List<string>();
            var seen = new HashSet<string>(StringComparer.Ordinal);
            foreach (Site s in sites) if (seen.Add(s.Url)) all.Add(s.Url);
            var urls = all.Take(Math.Max(1, _opt.MaxLinksPerArticle)).ToList();
            var allowed = new HashSet<string>(urls, StringComparer.Ordinal);
            result.SkippedOverLimit = all.Count - urls.Count;
            foreach (string skipped in all.Skip(urls.Count))
                result.Reports.Add(new LinkReport { Url = skipped, Outcome = "Ignoré (limite)", Detail = "plus de " + _opt.MaxLinksPerArticle + " liens" });
            result.LinksChecked = urls.Count;

            // Vérification parallèle : une file séquentielle par hôte (politesse), plusieurs hôtes en parallèle.
            // L'archive n'est cherchée que si le lien est CONFIRMÉ mort (philosophie : vérifier d'abord, réparer ensuite).
            var decisions = new ConcurrentDictionary<string, LinkDecision>(StringComparer.Ordinal);
            var groups = urls.GroupBy(HostOf).ToList();
            Parallel.ForEach(groups, new ParallelOptions { MaxDegreeOfParallelism = Math.Max(1, _opt.MaxParallelism) }, g =>
            {
                foreach (string u in g) decisions[u] = Evaluate(u);
            });

            // Rapport par URL.
            foreach (string u in urls)
            {
                LinkDecision d = decisions[u];
                result.Reports.Add(new LinkReport
                {
                    Url = u,
                    Outcome = d.Kind == DecisionKind.Repair ? "Corrigé" : d.Kind == DecisionKind.Dead ? "Mort" : d.Kind == DecisionKind.Unknown ? "Inconnu" : "OK",
                    Detail = d.Reason + (d.Kind == DecisionKind.Repair ? " → " + d.NewUrl : string.Empty),
                });
                if (d.Kind == DecisionKind.Unknown) result.Unknown++;
            }

            // Édition : positions absolues, appliquées de la fin vers le début.
            var edits = new List<Edit>();
            int seq = 0;
            string today = FrenchDate(_clock());
            var deadCounted = new HashSet<string>(StringComparer.Ordinal);

            foreach (Site s in sites)
            {
                LinkDecision d;
                if (!decisions.TryGetValue(s.Url, out d)) continue;

                // Réparation des URL (redirections prouvées, http→https)
                if (d.Kind == DecisionKind.Repair && d.NewUrl != null)
                {
                    edits.Add(new Edit { Start = s.BodyOffset + s.UrlParam.ValueStart, End = s.BodyOffset + s.UrlParam.ValueEnd, NewText = d.NewUrl, Seq = seq++ });
                    result.Repaired++;
                }

                if (d.Kind == DecisionKind.Dead)
                {
                    bool hasArchive = HasArchive(s);
                    bool hasArchiveDate = HasArchiveDate(s);

                    // Si archive-url existe mais PAS archive-date → chercher l'archive pour obtenir la date
                    if (hasArchive && !hasArchiveDate && _opt.AddArchive && s.Spec.SupportsArchive && d.Snapshot != null)
                    {
                        AddArchiveEdits(s, d.Snapshot, edits, ref seq);
                        hasArchiveDate = true;
                        result.Archived++;
                    }
                    // Si PAS d'archive mais snapshot trouvé → ajouter archive-url et archive-date
                    else if (!hasArchive && _opt.AddArchive && s.Spec.SupportsArchive && d.Snapshot != null)
                    {
                        AddArchiveEdits(s, d.Snapshot, edits, ref seq);
                        hasArchive = true;
                        hasArchiveDate = true;
                        result.Archived++;
                    }

                    // Marquer comme brisé seulement si une archive est présente (déjà existante ou nouvellement ajoutée).
                    if (hasArchive && _opt.MarkBrokenWithArchive && s.Spec.MarkBroken && !IsMarked(s))
                    {
                        // Ajouter le paramètre brisé le directement dans le modèle
                        TemplateParam brokenParam = s.T.Params.FirstOrDefault(p => p.Name == "brisé le");
                        if (brokenParam != null && brokenParam.Value.Length == 0)
                        {
                            // Paramètre vide : remplir sur place
                            edits.Add(new Edit { Start = s.BodyOffset + brokenParam.ValueStart, End = s.BodyOffset + brokenParam.ValueStart, NewText = today, Seq = seq++ });
                        }
                        else if (brokenParam == null)
                        {
                            // Paramètre absent : ajouter après le dernier paramètre
                            string prefix = SeparatorBefore(s.Body, s.UrlParam);
                            edits.Add(new Edit { Start = s.BodyOffset + s.UrlParam.ValueEnd, End = s.BodyOffset + s.UrlParam.ValueEnd, NewText = prefix + "|brisé le=" + today, Seq = seq++ });
                        }
                        result.Marked++;
                    }
                    else if (!hasArchive && deadCounted.Add(s.Url)) result.DeadUnresolved++;
                }
            }

            if (edits.Count == 0) return result;

            var sb = new StringBuilder(text);
            foreach (Edit e in edits.OrderByDescending(x => x.Start).ThenByDescending(x => x.Seq))
            {
                sb.Remove(e.Start, e.End - e.Start);
                sb.Insert(e.Start, e.NewText);
            }
            result.Text = sb.ToString();
            result.Changed = !string.Equals(result.Text, text, StringComparison.Ordinal);
            return result;
        }

        public static string BuildSummary(DeadLinkFixResult r)
        {
            if (r == null || !r.Changed) return string.Empty;
            var parts = new List<string>();
            if (r.Repaired > 0)
            {
                if (r.Repaired == 1)
                    parts.Add("1 lien corrigé");
                else
                    parts.Add(r.Repaired + " liens corrigés");
            }
            if (r.Archived > 0)
            {
                if (r.Archived == 1)
                    parts.Add("1 archive ajoutée");
                else
                    parts.Add(r.Archived + " archives ajoutées");
            }
            if (r.Marked > 0)
            {
                if (r.Marked == 1)
                    parts.Add("1 lien brisé signalé");
                else
                    parts.Add(r.Marked + " liens brisés signalés");
            }
            return "Liens externes des références : " + string.Join(", ", parts);
        }

        // ---------------------------------------------------------------- collecte

        private List<Site> CollectSites(string text, string masked)
        {
            var sites = new List<Site>();
            foreach (RefBlock rb in WikiScan.FindRefs(masked))
            {
                string body = text.Substring(rb.BodyStart, rb.BodyEnd - rb.BodyStart);
                string mbody = masked.Substring(rb.BodyStart, rb.BodyEnd - rb.BodyStart);
                foreach (ParsedTemplate t in WikiScan.FindTemplates(mbody, body))
                {
                    TemplateSpec spec = Specs.FirstOrDefault(x => x.Name == t.Name);
                    if (spec == null) continue;
                    foreach (string pname in spec.UrlParams)
                    {
                        var matches = t.Params.Where(q => q.Name == pname).ToList();
                        if (matches.Count != 1) continue;           // absent ou ambigu (doublon) : on ne touche à rien
                        TemplateParam p = matches[0];
                        if (p.HasProtected || p.Value.Length == 0) continue;
                        if (p.Value.IndexOf("{{", StringComparison.Ordinal) >= 0) continue;
                        if (p.Value.Any(char.IsWhiteSpace)) continue;
                        Uri u;
                        if (!UrlTools.TryParseHttp(p.Value, out u) || !UrlTools.IsCheckable(u)) continue;
                        sites.Add(new Site { BodyOffset = rb.BodyStart, T = t, Spec = spec, UrlParam = p, Url = p.Value, Body = body });
                    }
                }
            }
            return sites;
        }

        private static string HostOf(string url)
        {
            Uri u;
            return UrlTools.TryParseHttp(url, out u) ? UrlTools.HostNoWww(u) : url;
        }

        private static bool HasArchive(Site s)
        {
            return s.T.Params.Any(p => s.Spec.ArchiveUrlParams.Contains(p.Name) && p.Value.Length > 0);
        }

        private static bool HasArchiveDate(Site s)
        {
            return s.T.Params.Any(p => s.Spec.ArchiveDateParams.Contains(p.Name) && p.Value.Length > 0);
        }

        private static bool IsMarked(Site s)
        {
            foreach (TemplateParam p in s.T.Params)
            {
                if (p.Name == "brisé le") return true;
                if (p.Name == "dead-url" && (p.Value.Equals("yes", StringComparison.OrdinalIgnoreCase) ||
                                             p.Value.Equals("oui", StringComparison.OrdinalIgnoreCase) ||
                                             p.Value.Equals("true", StringComparison.OrdinalIgnoreCase))) return true;
            }
            return false;
        }

        // ---------------------------------------------------------------- évaluation d'une URL

        private LinkDecision Evaluate(string url)
        {
            try
            {
                LinkCheckResult r;
                try { r = _checker.Check(url); }
                catch (Exception ex) { r = LinkCheckResult.Unknown(url, 0, "exception : " + ex.GetType().Name); }

                LinkDecision d = LinkAnalyzer.Analyze(url, r, _opt);
                // Philosophie : chercher une archive SEULEMENT si le lien est CONFIRMÉ mort.
                // Jamais d'archive sur un lien vivant ou inconnu.
                if (d.Kind == DecisionKind.Dead && _opt.AddArchive)
                {
                    foreach (IArchiveProvider p in _providers)
                    {
                        ArchiveSnapshot snap = null;
                        try { snap = p.FindClosest(url); } catch (Exception) { }
                        if (snap != null && UrlTools.SameResource(snap.OriginalUrl, url)) { d.Snapshot = snap; break; }
                    }
                }
                // Même si AddArchive=false, chercher l'archive si le lien est mort pour pouvoir ajouter brisé le quand archive-url existe sans date
                else if (d.Kind == DecisionKind.Dead && _opt.MarkBrokenWithArchive)
                {
                    foreach (IArchiveProvider p in _providers)
                    {
                        ArchiveSnapshot snap = null;
                        try { snap = p.FindClosest(url); } catch (Exception) { }
                        if (snap != null && UrlTools.SameResource(snap.OriginalUrl, url)) { d.Snapshot = snap; break; }
                    }
                }
                return d;
            }
            catch (Exception ex)
            {
                return LinkDecision.Unknown("erreur interne : " + ex.GetType().Name);
            }
        }

        // ---------------------------------------------------------------- insertion des paramètres d'archive

        private void AddArchiveEdits(Site s, ArchiveSnapshot snap, List<Edit> edits, ref int seq)
        {
            string arcUrl = snap.ArchiveUrl;
            string arcDate = FrenchDate(snap.DateUtc);
            var appends = new List<string>();

            TemplateParam emptyUrl = s.T.Params.FirstOrDefault(p => s.Spec.ArchiveUrlParams.Contains(p.Name));
            TemplateParam emptyDate = s.T.Params.FirstOrDefault(p => s.Spec.ArchiveDateParams.Contains(p.Name));

            if (emptyUrl != null) edits.Add(new Edit { Start = s.BodyOffset + emptyUrl.ValueStart, End = s.BodyOffset + emptyUrl.ValueStart, NewText = arcUrl, Seq = seq++ });
            else appends.Add(s.Spec.DefaultArchiveUrl + "=" + arcUrl);

            if (emptyDate != null)
            {
                if (emptyDate.Value.Length == 0)
                    edits.Add(new Edit { Start = s.BodyOffset + emptyDate.ValueStart, End = s.BodyOffset + emptyDate.ValueStart, NewText = arcDate, Seq = seq++ });
                // date déjà renseignée : on la respecte.
            }
            else appends.Add(s.Spec.DefaultArchiveDate + "=" + arcDate);

            if (appends.Count > 0)
            {
                string prefix = SeparatorBefore(s.Body, s.UrlParam);
                var sb = new StringBuilder();
                foreach (string a in appends) sb.Append(prefix).Append('|').Append(a);
                edits.Add(new Edit { Start = s.BodyOffset + s.UrlParam.ValueEnd, End = s.BodyOffset + s.UrlParam.ValueEnd, NewText = sb.ToString(), Seq = seq++ });
            }
        }

        /// <summary>Espaces / retours à la ligne qui précèdent le « | » du paramètre : réutilisés pour garder la mise en forme.</summary>
        private static string SeparatorBefore(string body, TemplateParam p)
        {
            int pipe = p.SegStart - 1;
            int k = pipe - 1;
            while (k >= 0 && char.IsWhiteSpace(body[k])) k--;
            return body.Substring(k + 1, pipe - (k + 1));
        }
    }
}
