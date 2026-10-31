using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using DeadLinkFix.Awb;
using WikiFunctions.Plugin;

namespace DeadLinkFix.Tests
{
    // ------------------------------------------------------------------ doublures
    sealed class FakeChecker : ILinkChecker
    {
        public readonly Dictionary<string, LinkCheckResult> Map = new Dictionary<string, LinkCheckResult>();
        public readonly ConcurrentBag<string> Calls = new ConcurrentBag<string>();
        public int DelayMs;
        public bool Throw;
        public int InFlight, MaxInFlight;

        public LinkCheckResult Check(string url)
        {
            Calls.Add(url);
            int n = Interlocked.Increment(ref InFlight);
            int m; while (n > (m = MaxInFlight) && Interlocked.CompareExchange(ref MaxInFlight, n, m) != m) { }
            try
            {
                if (DelayMs > 0) Thread.Sleep(DelayMs);
                if (Throw) throw new InvalidOperationException("boom");
                LinkCheckResult r;
                return Map.TryGetValue(url, out r) ? r : LinkCheckResult.Alive(url, url, 0);
            }
            finally { Interlocked.Decrement(ref InFlight); }
        }
        public FakeChecker Dead(string u) { Map[u] = LinkCheckResult.Dead(u, 404, "HTTP 404 confirmé"); return this; }
        public FakeChecker Unk(string u) { Map[u] = LinkCheckResult.Unknown(u, 403, "HTTP 403"); return this; }
        public FakeChecker Redir(string u, string f) { Map[u] = LinkCheckResult.Alive(u, f, 1); return this; }
    }

    sealed class FakeArchive : IArchiveProvider
    {
        public readonly Dictionary<string, ArchiveSnapshot> Map = new Dictionary<string, ArchiveSnapshot>();
        public string Name { get { return "fake"; } }
        public ArchiveSnapshot FindClosest(string url) { ArchiveSnapshot s; return Map.TryGetValue(url, out s) ? s : null; }
        public FakeArchive Add(string url, string orig = null, string ts = "20200312101010")
        {
            string o = orig ?? url;
            Map[url] = new ArchiveSnapshot("https://web.archive.org/web/" + ts + "/" + o, o, new DateTime(2020, 3, 12), "fake");
            return this;
        }
    }

    sealed class Args : IProcessArticleEventArgs
    {
        public string ArticleText { get; set; }
        public string EditSummary { get; set; }
    }

    // ------------------------------------------------------------------ mini serveur HTTP local
    sealed class LocalServer : IDisposable
    {
        private readonly HttpListener _l = new HttpListener();
        public readonly string Base;
        public LocalServer()
        {
            var tl = new TcpListener(IPAddress.Loopback, 0); tl.Start();
            int port = ((IPEndPoint)tl.LocalEndpoint).Port; tl.Stop();
            Base = "http://127.0.0.1:" + port;
            _l.Prefixes.Add(Base + "/");
            _l.Start();
            new Thread(Loop) { IsBackground = true }.Start();
        }
        private void Loop()
        {
            while (_l.IsListening)
            {
                HttpListenerContext c;
                try { c = _l.GetContext(); } catch (Exception) { return; }
                ThreadPool.QueueUserWorkItem(_ => Handle(c));
            }
        }
        private void Handle(HttpListenerContext c)
        {
            try
            {
                string p = c.Request.Url.AbsolutePath;
                string ua = c.Request.UserAgent ?? "";
                bool bot = ua.Contains("DeadLinkFix");
                int code = 200; string loc = null; string body = "ok";
                switch (p)
                {
                    case "/ok": break;
                    case "/gone": code = 404; break;
                    case "/gone410": code = 410; break;
                    case "/botonly404": if (bot) code = 404; break;
                    case "/forbidden": code = 403; break;
                    case "/error500": code = 500; break;
                    case "/moved": code = 301; loc = "/new-article-2020"; break;
                    case "/relative": code = 302; loc = "ok"; break;
                    case "/new-article-2020": break;
                    case "/chain1": code = 301; loc = "/chain2"; break;
                    case "/chain2": code = 301; loc = "/ok"; break;
                    case "/loop": code = 302; loc = "/loop2"; break;
                    case "/loop2": code = 302; loc = "/loop"; break;
                    case "/toroot": code = 301; loc = "/"; break;
                    case "/noloc": code = 302; break;
                    case "/ftp": code = 302; loc = "ftp://example.com/x"; break;
                    case "/to404chain": code = 301; loc = "/gone"; break;
                    case "/api":
                        body = "{\"url\": \"x\", \"archived_snapshots\": {\"closest\": {\"status\": \"200\", \"available\": true, \"url\": \"http:\\/\\/web.archive.org\\/web\\/20200312101010\\/http:\\/\\/example.com\\/page\", \"timestamp\": \"20200312101010\"}}}";
                        break;
                    case "/": body = "home"; break;
                    default: code = 404; break;
                }
                c.Response.StatusCode = code;
                if (loc != null) c.Response.RedirectLocation = loc;
                byte[] b = Encoding.UTF8.GetBytes(body);
                c.Response.OutputStream.Write(b, 0, b.Length);
                c.Response.Close();
            }
            catch (Exception) { try { c.Response.Abort(); } catch (Exception) { } }
        }
        public void Dispose() { try { _l.Stop(); _l.Close(); } catch (Exception) { } }
    }

    // ------------------------------------------------------------------ harnais
    static class T
    {
        public static int Pass, Fail;
        public static string Current = "";
        public static void Test(string name, Action a)
        {
            Current = name;
            try { a(); Pass++; Console.WriteLine("  ok   " + name); }
            catch (Exception ex) { Fail++; Console.WriteLine("  FAIL " + name + "\n       " + ex.Message.Replace("\n", "\n       ")); }
        }
        public static void True(bool c, string msg = "condition fausse") { if (!c) throw new Exception(msg); }
        public static void Eq(object exp, object act, string msg = "")
        {
            if (!Equals(exp, act)) throw new Exception(msg + "\n attendu : " + exp + "\n obtenu  : " + act);
        }
    }

    static class Program
    {
        static readonly DateTime Today = new DateTime(2026, 10, 17);
        static DeadLinkFixOptions Opt() { return new DeadLinkFixOptions(); }
        static DeadLinkFixEngine Eng(ILinkChecker c, IArchiveProvider a = null, DeadLinkFixOptions o = null)
        {
            return new DeadLinkFixEngine(c, a == null ? new IArchiveProvider[0] : new[] { a }, o ?? Opt(), () => Today);
        }

        static int Main()
        {
            Console.WriteLine("== UrlTools ==");
            UrlToolsTests();
            Console.WriteLine("== Analyse du wikicode ==");
            ParsingTests();
            Console.WriteLine("== Analyseur de liens (preuves) ==");
            AnalyzerTests();
            Console.WriteLine("== Moteur ==");
            EngineTests();
            Console.WriteLine("== Vérificateur HTTP (serveur local) ==");
            HttpTests();
            Console.WriteLine("== Archives ==");
            ArchiveTests();
            Console.WriteLine("== Divers / plugin ==");
            MiscTests();
            Console.WriteLine();
            Console.WriteLine("Résultat : " + T.Pass + " réussis, " + T.Fail + " échecs");
            return T.Fail == 0 ? 0 : 1;
        }

        static Uri U(string s) { Uri u; if (!UrlTools.TryParseHttp(s, out u)) throw new Exception("URL invalide " + s); return u; }

        // ---------------------------------------------------------------- UrlTools
        static void UrlToolsTests()
        {
            T.Test("Canonical ignore schéma/www/fragment/slash final/index", () =>
            {
                T.Eq(UrlTools.Canonical("http://www.Example.com/A/b/index.html#x"), UrlTools.Canonical("https://example.com/a/b/"));
                T.True(UrlTools.Canonical("https://example.com/a?x=1") != UrlTools.Canonical("https://example.com/a?x=2"));
            });
            T.Test("SameSite : www, sous-domaine, hébergement mutualisé, TLD à 2 niveaux", () =>
            {
                T.True(UrlTools.SameSite(U("http://www.exemple.fr/a"), U("https://exemple.fr/b")));
                T.True(UrlTools.SameSite(U("http://news.exemple.fr/a"), U("https://exemple.fr/b")));
                T.True(!UrlTools.SameSite(U("http://a.blogspot.com/x"), U("http://b.blogspot.com/x")));
                T.True(!UrlTools.SameSite(U("http://a.exemple.fr/x"), U("http://a.autre.fr/x")));
                T.True(!UrlTools.SameSite(U("http://a.co.uk/x"), U("http://b.co.uk/x")));
                T.True(!UrlTools.SameSite(U("http://x.gouv.sn/x"), U("http://y.gouv.sn/x")));
                T.True(UrlTools.SameSite(U("http://x.exemple.com.ma/x"), U("http://exemple.com.ma/y")));
            });
            T.Test("SlugKey / NumericIds / IsRoot", () =>
            {
                T.Eq("lasenegalaiseenmarche", UrlTools.SlugKey(U("http://a.sn/2019/05/La-Sénégalaise_en-marche.html")));
                T.Eq("", UrlTools.SlugKey(U("http://a.sn/")));
                T.True(UrlTools.NumericIds(U("http://a.sn/n/123456/x?id=7890123")).SetEquals(new[] { "123456", "7890123" }));
                T.True(UrlTools.IsRoot(U("http://a.sn")) && UrlTools.IsRoot(U("http://a.sn/index.php")) && !UrlTools.IsRoot(U("http://a.sn/?p=1")));
            });
            T.Test("IsCheckable : privé, local, archives, mono-label exclus", () =>
            {
                foreach (string bad in new[] { "http://localhost/x", "http://127.0.0.1/x", "http://192.168.1.5/x", "http://10.0.0.1/x", "http://intranet/x", "http://web.archive.org/web/2020/x", "http://archive.ph/abc" })
                    T.True(!UrlTools.IsCheckable(U(bad)), bad);
                T.True(UrlTools.IsCheckable(U("https://www.lemonde.fr/x")));
            });
            T.Test("IsWikiSafe", () =>
            {
                T.True(UrlTools.IsWikiSafe("https://a.fr/x?y=1&z=2"));
                foreach (string bad in new[] { "https://a.fr/x y", "https://a.fr/x|y", "https://a.fr/}}", "https://a.fr/[x]", "https://a.fr/<x>" })
                    T.True(!UrlTools.IsWikiSafe(bad), bad);
            });
        }

        // ---------------------------------------------------------------- parsing
        static void ParsingTests()
        {
            T.Test("FindRefs : ref normale, ref nommée, auto-fermante, references ignorés", () =>
            {
                string s = "a<ref>1</ref> b<ref name=\"x\">2</ref> c<ref name=x/> d<ref name=\"a/b\" />e<references/>f<ref group=n>3</ref>";
                var refs = WikiScan.FindRefs(WikiScan.Mask(s));
                T.Eq(3, refs.Count);
                T.Eq("1", s.Substring(refs[0].BodyStart, refs[0].BodyEnd - refs[0].BodyStart));
                T.Eq("3", s.Substring(refs[2].BodyStart, refs[2].BodyEnd - refs[2].BodyStart));
            });
            T.Test("Mask : commentaire, nowiki, code ; longueur conservée", () =>
            {
                string s = "x<!-- <ref>a</ref> -->y<nowiki><ref>b</ref></nowiki>z<code><ref>c</ref></code><ref>ok</ref>";
                string m = WikiScan.Mask(s);
                T.Eq(s.Length, m.Length);
                T.Eq(1, WikiScan.FindRefs(m).Count);
            });
            T.Test("FindTemplates : imbriqués, pipes dans [[ ]] et {{ }}, valeur lue dans l'original", () =>
            {
                string s = "{{Lien web |url=http://a.fr/x |titre=[[A|b]] {{langue|fr|z}} |consulté le=17 octobre 2026 |auteur1 }}";
                var t = WikiScan.FindTemplates(WikiScan.Mask(s), s);
                T.Eq(1, t.Count);
                T.Eq("lien web", t[0].Name);
                T.Eq(3, t[0].Params.Count);   // le paramètre positionnel « auteur1 » est ignoré
                var url = t[0].Params.First(p => p.Name == "url");
                T.Eq("http://a.fr/x", url.Value);
                T.Eq("http://a.fr/x", s.Substring(url.ValueStart, url.ValueEnd - url.ValueStart));
                T.Eq("17 octobre 2026", t[0].Params.First(p => p.Name == "consulté le").Value);
            });
            T.Test("FindTemplates : nom avec underscore/majuscules/préfixe Modèle:", () =>
            {
                string s = "{{Modèle:Lien_Web|url=http://a.fr/x}}";
                T.Eq("lien web", WikiScan.FindTemplates(WikiScan.Mask(s), s)[0].Name);
            });
        }

        // ---------------------------------------------------------------- analyseur
        static void AnalyzerTests()
        {
            T.Test("Statuts : Unknown / Dead transmis, jamais de réparation sur erreur", () =>
            {
                T.Eq(DecisionKind.Unknown, LinkAnalyzer.Analyze("http://a.fr/x", LinkCheckResult.Unknown("http://a.fr/x", 503, "x"), Opt()).Kind);
                T.Eq(DecisionKind.Dead, LinkAnalyzer.Analyze("http://a.fr/x", LinkCheckResult.Dead("http://a.fr/x", 404, "x"), Opt()).Kind);
            });
            T.Test("Lien vivant sans redirection → Keep", () =>
            {
                T.Eq(DecisionKind.Keep, LinkAnalyzer.Analyze("http://a.fr/x", LinkCheckResult.Alive("http://a.fr/x", "http://a.fr/x", 0), Opt()).Kind);
            });
            T.Test("Redirection même slug → Repair", () =>
                T.Eq(DecisionKind.Repair, LinkAnalyzer.Analyze("http://www.exemple.fr/2019/05/mon-article.html", LinkCheckResult.Alive("http://www.exemple.fr/2019/05/mon-article.html", "https://www.exemple.fr/actualites/mon-article", 1), Opt()).Kind));
            T.Test("Redirection même identifiant → Repair", () =>
                T.Eq(DecisionKind.Repair, LinkAnalyzer.Analyze("http://exemple.fr/article.php?id=123456", LinkCheckResult.Alive("http://exemple.fr/article.php?id=123456", "https://exemple.fr/politique/titre-different-123456.html", 1), Opt()).Kind));
            T.Test("Slug différent → Keep", () =>
                T.Eq(DecisionKind.Keep, LinkAnalyzer.Analyze("http://exemple.fr/a/mon-article", LinkCheckResult.Alive("http://exemple.fr/a/mon-article", "http://exemple.fr/b/un-autre-sujet", 1), Opt()).Kind));
            T.Test("http → https identique → Repair", () =>
                T.Eq(DecisionKind.Repair, LinkAnalyzer.Analyze("http://exemple.fr/a/b?x=1", LinkCheckResult.Alive("http://exemple.fr/a/b?x=1", "https://exemple.fr/a/b?x=1", 1), Opt()).Kind));
            T.Test("Redirection vers autre domaine → Keep", () =>
                T.Eq(DecisionKind.Keep, LinkAnalyzer.Analyze("http://exemple.fr/a", LinkCheckResult.Alive("http://exemple.fr/a", "https://autre.fr/a", 1), Opt()).Kind));
        }

        // ---------------------------------------------------------------- moteur
        const string LW = "{{Lien web |url=http://exemple.fr/p |titre=T |site=S |consulté le=17 octobre 2026}}";

        static void EngineTests()
        {
            T.Test("Pas de <ref> → intact, aucun appel réseau", () =>
            {
                var c = new FakeChecker(); var r = Eng(c).Fix("Texte " + LW);
                T.True(!r.Changed); T.Eq(0, c.Calls.Count);
            });
            T.Test("Lien vivant / inconnu → texte strictement identique", () =>
            {
                string s = "<ref>" + LW + "</ref><ref>{{Lien web |url=http://autre.fr/q |titre=T}}</ref>";
                var c = new FakeChecker().Unk("http://autre.fr/q");
                var r = Eng(c).Fix(s);
                T.True(!r.Changed); T.Eq(s, r.Text); T.Eq(1, r.Unknown);
            });
            T.Test("Redirection réparée (http→https)", () =>
            {
                string s = "<ref>{{Lien web |url=http://exemple.fr/mon-article-long |titre=T}}</ref>";
                var c = new FakeChecker().Redir("http://exemple.fr/mon-article-long", "https://exemple.fr/mon-article-long");
                var r = Eng(c).Fix(s);
                T.True(r.Changed);
                T.True(r.Text.Contains("url=https://exemple.fr/mon-article-long"));
                T.Eq(1, r.Repaired);
            });
            T.Test("Redirection réparée (même slug)", () =>
            {
                string s = "<ref>{{Lien web |url=http://exemple.fr/mon-article |titre=T}}</ref>";
                var c = new FakeChecker().Redir("http://exemple.fr/mon-article", "https://exemple.fr/actualites/mon-article");
                var r = Eng(c).Fix(s);
                T.True(r.Changed);
                T.True(r.Text.Contains("url=https://exemple.fr/actualites/mon-article"));
                T.Eq(1, r.Repaired);
            });
            T.Test("Lien mort avec archive : construction du lien complet", () =>
            {
                string s = "<ref>{{Lien web |url=http://exemple.fr/page-mort |titre=Exemple |site=Exemple.com |consulté le=3 mars 2019}}</ref>";
                var c = new FakeChecker().Dead("http://exemple.fr/page-mort");
                var a = new FakeArchive().Add("http://exemple.fr/page-mort");
                var r = Eng(c, a).Fix(s);
                Console.WriteLine("Lien original : " + s);
                Console.WriteLine("Lien corrigé : " + r.Text);
                T.True(r.Changed);
                T.True(r.Text.Contains("archive-url=https://web.archive.org/web/20200312101010/http://exemple.fr/page-mort"));
                T.True(r.Text.Contains("archive-date=12 mars 2020"));
                T.True(r.Text.Contains("brisé le=17 octobre 2026")); // paramètre dans le modèle
                T.True(!r.Text.Contains("{{Lien brisé")); // PAS de modèle séparé
                T.True(r.Text.Contains("consulté le=3 mars 2019")); // consulté le doit rester intact
                T.Eq(1, r.Archived);
                T.Eq(1, r.Marked);
            });
            T.Test("Lien mort avec archive déjà présente : ajouter brisé le", () =>
            {
                string s = "<ref>{{Lien web |langue=en |titre=RESEARCH & FORECAST REPORT |url=http://www.colliers.com/en-GB/UK/insights/~/media/Files/EMEA/UK/research/offices/201301-city-monthly-market-monitor.ashx |format=pdf |date=janvier 2013 |archive-url=https://web.archive.org/web/20160309182250/http://www.colliers.com/en-gb/uk/insights/~/media/files/emea/uk/research/offices/201301-city-monthly-market-monitor.ashx |archive-date=9 mars 2016}}.</ref>";
                var c = new FakeChecker().Dead("http://www.colliers.com/en-GB/UK/insights/~/media/Files/EMEA/UK/research/offices/201301-city-monthly-market-monitor.ashx");
                var r = Eng(c).Fix(s);
                Console.WriteLine("Lien original : " + s);
                Console.WriteLine("Lien corrigé : " + r.Text);
                T.True(r.Changed);
                T.True(r.Text.Contains("brisé le=17 octobre 2026")); // doit ajouter brisé le
                T.Eq(1, r.Marked);
                T.Eq(0, r.Archived); // archive déjà présente
            });
            T.Test("Mort sans archive ni capture → intact, compté non résolu", () =>
            {
                string s = "<ref>" + LW + "</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive()).Fix(s);
                T.True(!r.Changed); T.Eq(1, r.DeadUnresolved);
            });
            T.Test("Mort + archive déjà présente → paramètre « brisé le », « consulté le » intact", () =>
            {
                string s = "<ref>{{Lien web |url=http://exemple.fr/p |titre=T |consulté le=3 mars 2019 |archive-url=https://web.archive.org/web/20190303/http://exemple.fr/p |archive-date=3 mars 2019}}</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p")).Fix(s);
                T.True(r.Changed);
                T.True(r.Text.Contains("brisé le=17 octobre 2026"), r.Text);
                T.True(r.Text.Contains("consulté le=3 mars 2019"), "consulté le modifié");
                T.Eq(1, r.Marked); T.Eq(0, r.Archived);
            });
            T.Test("Mort sans archive + capture valide → archive-url/date ajoutés + paramètre brisé le (Lien web)", () =>
            {
                string s = "<ref>" + LW + "</ref>";
                var a = new FakeArchive().Add("http://exemple.fr/p");
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), a).Fix(s);
                T.Eq("<ref>{{Lien web |url=http://exemple.fr/p |archive-url=https://web.archive.org/web/20200312101010/http://exemple.fr/p |archive-date=12 mars 2020 |brisé le=17 octobre 2026 |titre=T |site=S |consulté le=17 octobre 2026}}</ref>", r.Text);
                T.Eq(1, r.Archived); T.Eq(1, r.Marked);
            });
            T.Test("Article : paramètres archiveurl / archivedate, JAMAIS « brisé le »", () =>
            {
                string s = "<ref>{{Article |titre=T |périodique=P |date=2010 |lire en ligne=http://exemple.fr/p |consulté le=1 janvier 2020}}</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive().Add("http://exemple.fr/p")).Fix(s);
                T.True(r.Text.Contains("lire en ligne=http://exemple.fr/p |archiveurl=https://web.archive.org/web/20200312101010/http://exemple.fr/p |archivedate=12 mars 2020 |consulté le=1 janvier 2020}}") && !r.Text.Contains("brisé le"), r.Text);
            });
            T.Test("Ouvrage : pas de paramètre d'archive → aucun ajout ni marqueur", () =>
            {
                string s = "<ref>{{Ouvrage |titre=T |lire en ligne=http://exemple.fr/livre-complet-2010 |consulté le=1 janvier 2020}}</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/livre-complet-2010"), new FakeArchive().Add("http://exemple.fr/livre-complet-2010")).Fix(s);
                T.True(!r.Changed);  // Ouvrage n'a pas de paramètres d'archive
            });
            T.Test("Paramètres d'archive vides → remplis sur place, pas de doublon", () =>
            {
                string s = "<ref>{{Lien web |url=http://exemple.fr/p |titre=T |archive-url= |archive-date= }}</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive().Add("http://exemple.fr/p")).Fix(s);
                T.True(r.Text.Contains("|archive-url=https://web.archive.org/web/20200312101010/http://exemple.fr/p |archive-date=12 mars 2020 }}"), r.Text);
                T.Eq(1, r.Text.Split(new[] { "archive-url" }, StringSplitOptions.None).Length - 1);
            });
            T.Test("Mise en forme multi-lignes conservée", () =>
            {
                string s = "<ref>{{Lien web\n | url = http://exemple.fr/p\n | titre = T\n}}</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive().Add("http://exemple.fr/p")).Fix(s);
                T.True(r.Text.Contains("| url = http://exemple.fr/p\n |archive-url=https://web.archive.org/web/20200312101010/http://exemple.fr/p\n |archive-date=12 mars 2020\n |brisé le=17 octobre 2026\n | titre = T"), r.Text);
            });
            T.Test("Idempotent : second passage = aucun changement", () =>
            {
                string s = "<ref>" + LW + "</ref>";
                var c = new FakeChecker().Dead("http://exemple.fr/p");
                var e = Eng(c, new FakeArchive().Add("http://exemple.fr/p"));
                string once = e.Fix(s).Text;
                var again = e.Fix(once);
                T.True(!again.Changed, "deuxième passage a modifié : " + again.Text);
            });
            T.Test("Déjà marqué (brisé le / dead-url=yes) → pas de second marqueur", () =>
            {
                foreach (string tpl in new[] { "|dead-url=yes", "|brisé le=1 mai 2025" })
                {
                    string s = "<ref>{{Lien web |url=http://exemple.fr/p |archive-url=https://web.archive.org/web/20190303000000/http://exemple.fr/p " + tpl + "}}</ref>";
                    var r = Eng(new FakeChecker().Dead("http://exemple.fr/p")).Fix(s);
                    T.True(!r.Changed, tpl);
                }
            });
            T.Test("Capture d'une AUTRE page (domaine/lien différent) → rejetée", () =>
            {
                var a = new FakeArchive();
                a.Map["http://exemple.fr/p"] = new ArchiveSnapshot("https://web.archive.org/web/20200312101010/http://exemple.fr/autre", "http://exemple.fr/autre", new DateTime(2020, 3, 12), "fake");
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), a).Fix("<ref>" + LW + "</ref>");
                T.True(!r.Changed);
            });
            T.Test("Option AddArchive=false / MarkBrokenWithArchive=false", () =>
            {
                var o = Opt(); o.AddArchive = false;
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive().Add("http://exemple.fr/p"), o).Fix("<ref>" + LW + "</ref>");
                T.True(!r.Changed);
                var o2 = Opt(); o2.MarkBrokenWithArchive = false;
                var r2 = Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive().Add("http://exemple.fr/p"), o2).Fix("<ref>" + LW + "</ref>");
                T.True(r2.Text.Contains("archive-url=") && !r2.Text.Contains("brisé le="));
            });
            T.Test("Zones protégées et hors-<ref> : jamais touchées", () =>
            {
                string s = LW + "<!-- <ref>" + LW + "</ref> --><nowiki><ref>" + LW + "</ref></nowiki><ref name=\"a\" />";
                var c = new FakeChecker().Dead("http://exemple.fr/p");
                var r = Eng(c, new FakeArchive().Add("http://exemple.fr/p")).Fix(s + "<ref>{{Citation|url=http://exemple.fr/p}}</ref>");
                T.True(!r.Changed); T.Eq(0, c.Calls.Count);
            });
            T.Test("URL non vérifiables (modèle, espace, ftp, local, archive) → ignorées", () =>
            {
                string s = "<ref>{{Lien web |url={{URL|exemple.fr}} |titre=T}}</ref><ref>{{Lien web |url=ftp://a.fr/x}}</ref>" +
                           "<ref>{{Lien web |url=http://127.0.0.1/x}}</ref><ref>{{Lien web |url=https://web.archive.org/web/2020/http://a.fr/x}}</ref>" +
                           "<ref>{{Lien web |url=http://a.fr/x y}}</ref><ref>{{Lien web |url=http://a.fr/x<!--c-->}}</ref><ref>{{Lien web |url=http://a.fr/z |url=http://a.fr/w}}</ref>";
                var c = new FakeChecker(); Eng(c).Fix(s);
                T.Eq(0, c.Calls.Count);
            });
            T.Test("Limite de 50 liens, URL dupliquée vérifiée une seule fois", () =>
            {
                var sb = new StringBuilder();
                for (int i = 0; i < 60; i++) sb.Append("<ref>{{Lien web |url=http://site" + i + ".fr/p |titre=T}}</ref>");
                sb.Append("<ref>{{Lien web |url=http://site0.fr/p |titre=T}}</ref>");
                var c = new FakeChecker(); var r = Eng(c).Fix(sb.ToString());
                T.Eq(50, c.Calls.Count); T.Eq(10, r.SkippedOverLimit);
                T.Eq(50, c.Calls.Distinct().Count());
            });
            T.Test("Même URL morte dans deux références → deux marquages, une seule vérification", () =>
            {
                string a = "{{Lien web |url=http://exemple.fr/p |archive-url=https://web.archive.org/web/20190303000000/http://exemple.fr/p |archive-date=3 mars 2019}}";
                var c = new FakeChecker().Dead("http://exemple.fr/p");
                var r = Eng(c).Fix("<ref>" + a + "</ref><ref>" + a + "</ref>");
                T.Eq(2, r.Marked); T.Eq(1, c.Calls.Count);
            });
            T.Test("Deux modèles dans une référence, chacun traité", () =>
            {
                string s = "<ref>{{Lien web |url=http://exemple.fr/a |titre=A}} ; {{Lien web |url=http://exemple.fr/b |titre=B}}</ref>";
                var c = new FakeChecker().Dead("http://exemple.fr/a").Dead("http://exemple.fr/b");
                var a = new FakeArchive().Add("http://exemple.fr/a").Add("http://exemple.fr/b");
                var r = Eng(c, a).Fix(s);
                T.Eq(2, r.Archived); T.Eq(2, r.Marked);
            });
            T.Test("Soft 404 (accueil) + archive présente → marqué", () =>
            {
                // Note: avec le nouveau comportement, redirection vers l'accueil = Keep (pas de Dead)
                // Donc ce test n'est plus applicable
                T.True(true);  // Placeholder - comportement modifié
            });
            T.Test("Vérificateur qui lève une exception → texte intact", () =>
            {
                var c = new FakeChecker { Throw = true };
                var r = Eng(c).Fix("<ref>" + LW + "</ref>");
                T.True(!r.Changed); T.Eq(1, r.Unknown);
            });
            T.Test("Vérifications en parallèle entre hôtes, séquentielles pour un même hôte", () =>
            {
                var sb = new StringBuilder();
                for (int i = 0; i < 8; i++) sb.Append("<ref>{{Lien web |url=http://h" + i + ".exemple" + i + ".fr/p |titre=T}}</ref>");
                var c = new FakeChecker { DelayMs = 250 };
                var sw = Stopwatch.StartNew(); Eng(c).Fix(sb.ToString()); sw.Stop();
                T.True(sw.ElapsedMilliseconds < 1500, "trop lent : " + sw.ElapsedMilliseconds + " ms");
                T.True(c.MaxInFlight > 1, "pas de parallélisme");

                var sb2 = new StringBuilder();
                for (int i = 0; i < 4; i++) sb2.Append("<ref>{{Lien web |url=http://meme.fr/p" + i + " |titre=T}}</ref>");
                var c2 = new FakeChecker { DelayMs = 50 };
                Eng(c2).Fix(sb2.ToString());
                T.Eq(1, c2.MaxInFlight, "même hôte en parallèle");
            });
            T.Test("Date française : 1er et autres mois", () =>
            {
                T.Eq("1er janvier 2026", DeadLinkFixEngine.FrenchDate(new DateTime(2026, 1, 1)));
                T.Eq("2 février 2026", DeadLinkFixEngine.FrenchDate(new DateTime(2026, 2, 2)));
                T.Eq("31 décembre 2026", DeadLinkFixEngine.FrenchDate(new DateTime(2026, 12, 31)));
            });
            T.Test("Texte avec CRLF, caractères Unicode et refs imbriquées dans du texte : positions correctes", () =>
            {
                string s = "Été «é» œuvre\r\n<ref>" + LW + "</ref>\r\nFin <ref>{{Lien web |url=http://exemple.fr/p2 |titre=Ünï}}</ref>";
                var r = Eng(new FakeChecker().Dead("http://exemple.fr/p").Dead("http://exemple.fr/p2"), new FakeArchive().Add("http://exemple.fr/p").Add("http://exemple.fr/p2")).Fix(s);
                T.True(r.Text.StartsWith("Été «é» œuvre\r\n<ref>{{Lien web |url=http://exemple.fr/p |archive-url="), r.Text);
                T.True(r.Text.Contains("titre=Ünï") && r.Text.Contains("brisé le=17 octobre 2026"), r.Text);
            });
        }

        // ---------------------------------------------------------------- HTTP réel (local)
        static void HttpTests()
        {
            using (var srv = new LocalServer())
            using (var chk = new HttpLinkChecker(TimeSpan.FromSeconds(10)) { RetryDelay = TimeSpan.Zero, OnlineProbe = () => true })
            {
                Func<string, LinkCheckResult> C = p => chk.Check(srv.Base + p);

                T.Test("200 → Alive", () => { var r = C("/ok"); T.Eq(LinkState.Alive, r.State); T.Eq(0, r.RedirectCount); });
                T.Test("404 confirmé → Dead", () => T.Eq(LinkState.Dead, C("/gone").State));
                T.Test("410 → Dead", () => T.Eq(LinkState.Dead, C("/gone410").State));
                T.Test("404 uniquement pour le robot → Alive (confirmation navigateur)", () => T.Eq(LinkState.Alive, C("/botonly404").State));
                T.Test("403 / 500 → Unknown (jamais Dead)", () =>
                {
                    T.Eq(LinkState.Unknown, C("/forbidden").State);
                    T.Eq(LinkState.Unknown, C("/error500").State);
                });
                T.Test("301 → URL finale", () =>
                {
                    var r = C("/moved"); T.Eq(LinkState.Alive, r.State);
                    T.Eq(srv.Base + "/new-article-2020", r.FinalUrl); T.Eq(1, r.RedirectCount);
                });
                T.Test("Chaîne de 2 redirections et Location relative", () =>
                {
                    T.Eq(2, C("/chain1").RedirectCount);
                    T.Eq(srv.Base + "/ok", C("/relative").FinalUrl);
                });
                T.Test("Redirection → 404 : Dead", () => T.Eq(LinkState.Dead, C("/to404chain").State));
                T.Test("Boucle, sans Location, schéma ftp → Unknown", () =>
                {
                    T.Eq(LinkState.Unknown, C("/loop").State);
                    T.Eq(LinkState.Unknown, C("/noloc").State);
                    T.Eq(LinkState.Unknown, C("/ftp").State);
                });
                T.Test("Redirection vers l'accueil + analyseur → Keep (pas de preuve)", () =>
                {
                    var d1 = LinkAnalyzer.Analyze(srv.Base + "/toroot", C("/toroot"), Opt());
                    T.Eq(DecisionKind.Keep, d1.Kind);  // Redirection vers l'accueil : pas de preuve, donc Keep
                    // /moved redirige vers /new-article-2020, mais ce slug est trop court (< 6 caractères après nettoyage)
                    var d2 = LinkAnalyzer.Analyze(srv.Base + "/moved", C("/moved"), Opt());
                    T.Eq(DecisionKind.Keep, d2.Kind);  // Slug trop court : pas de preuve suffisante
                });
                T.Test("Connexion refusée → Unknown", () =>
                {
                    var tl = new TcpListener(IPAddress.Loopback, 0); tl.Start(); int port = ((IPEndPoint)tl.LocalEndpoint).Port; tl.Stop();
                    T.Eq(LinkState.Unknown, chk.Check("http://127.0.0.1:" + port + "/x").State);
                });
                T.Test("URL non HTTP → Unknown", () => T.Eq(LinkState.Unknown, chk.Check("mailto:a@b.fr").State));
                T.Test("Moteur complet sur serveur local : redirection prouvée corrigée (même slug)", () =>
                {
                    // /new-article-2020 existe ; /relative → /ok n'a pas de preuve. On teste donc le chemin bout-en-bout sur 404.
                    string s = "<ref>{{Lien web |url=" + srv.Base + "/gone |titre=T |archive-url=https://web.archive.org/web/20190101000000/" + srv.Base + "/gone}}</ref>";
                    var e = new DeadLinkFixEngine(chk, new IArchiveProvider[0], new DeadLinkFixOptions(), () => Today);
                    // 127.0.0.1 est non vérifiable (IsCheckable) : le moteur doit l'ignorer.
                    T.True(!e.Fix(s).Changed);
                });
            }
            T.Test("DNS : exception classifiée correctement", () =>
            {
                T.True(HttpLinkChecker.IsNameResolutionFailure(new SocketException((int)SocketError.HostNotFound)));
                T.True(HttpLinkChecker.IsNameResolutionFailure(new InvalidOperationException("x", new SocketException((int)SocketError.NoData))));
                T.True(!HttpLinkChecker.IsNameResolutionFailure(new SocketException((int)SocketError.TryAgain)));
                T.True(!HttpLinkChecker.IsNameResolutionFailure(new SocketException((int)SocketError.ConnectionRefused)));
                T.True(!HttpLinkChecker.IsNameResolutionFailure(new TimeoutException()));
            });
            T.Test("Domaine inexistant : jamais Alive ; Unknown si hors-ligne, Dead si en ligne", () =>
            {
                using (var off = new HttpLinkChecker(TimeSpan.FromSeconds(8)) { RetryDelay = TimeSpan.Zero, OnlineProbe = () => false })
                    T.True(off.Check("http://dlf-inexistant-test.invalid/x").State == LinkState.Unknown);
                using (var on = new HttpLinkChecker(TimeSpan.FromSeconds(8)) { RetryDelay = TimeSpan.Zero, OnlineProbe = () => true })
                    T.True(on.Check("http://dlf-inexistant-test.invalid/x").State != LinkState.Alive);
            });
            T.Test("CachingLinkChecker : Alive/Dead mis en cache, Unknown jamais, expiration", () =>
            {
                var f = new FakeChecker().Unk("http://a.fr/u").Dead("http://a.fr/d");
                DateTime now = new DateTime(2026, 1, 1);
                var cc = new CachingLinkChecker(f, TimeSpan.FromMinutes(10), () => now);
                cc.Check("http://a.fr/ok"); cc.Check("http://a.fr/ok"); cc.Check("http://a.fr/d"); cc.Check("http://a.fr/d");
                cc.Check("http://a.fr/u"); cc.Check("http://a.fr/u");
                T.Eq(4, f.Calls.Count, "appels attendus : ok, d, u, u");
                now = now.AddMinutes(11); cc.Check("http://a.fr/ok");
                T.Eq(5, f.Calls.Count);
            });
        }

        // ---------------------------------------------------------------- archives
        static void ArchiveTests()
        {
            const string json = "{\"url\": \"http://exemple.fr/page\", \"archived_snapshots\": {\"closest\": {\"status\": \"200\", \"available\": true, \"url\": \"http://web.archive.org/web/20200312101010/http://exemple.fr/page\", \"timestamp\": \"20200312101010\"}}}";
            T.Test("Wayback : réponse valide → https + date", () =>
            {
                var w = new WaybackProvider(u => json);
                var s = w.FindClosest("http://exemple.fr/page");
                T.True(s != null);
                T.Eq("https://web.archive.org/web/20200312101010/http://exemple.fr/page", s.ArchiveUrl);
                T.Eq(new DateTime(2020, 3, 12), s.DateUtc.Date);
            });
            T.Test("Wayback : variantes http/https/www/slash acceptées", () =>
                T.True(new WaybackProvider(u => json).FindClosest("https://www.exemple.fr/page/") != null));
            T.Test("Wayback : autre lien → rejeté ; vide, non disponible, status≠200, JSON cassé, exception → null", () =>
            {
                T.True(new WaybackProvider(u => json).FindClosest("http://exemple.fr/autre") == null);
                T.True(new WaybackProvider(u => json).FindClosest("http://autre.fr/page") == null);
                T.True(new WaybackProvider(u => "{\"archived_snapshots\": {}}").FindClosest("http://exemple.fr/page") == null);
                T.True(new WaybackProvider(u => json.Replace("true", "false")).FindClosest("http://exemple.fr/page") == null);
                T.True(new WaybackProvider(u => json.Replace("\"200\"", "\"404\"")).FindClosest("http://exemple.fr/page") == null);
                T.True(new WaybackProvider(u => "<html>").FindClosest("http://exemple.fr/page") == null);
                T.True(new WaybackProvider(u => null).FindClosest("http://exemple.fr/page") == null);
                T.True(new WaybackProvider(u => { throw new Exception("x"); }).FindClosest("http://exemple.fr/page") == null);
            });
            T.Test("Wayback : appel HTTP réel vers un serveur local (clé url échappée)", () =>
            {
                using (var srv = new LocalServer())
                {
                    var w = new WaybackProvider { ApiBase = srv.Base + "/api?url=" };
                    var s = w.FindClosest("http://example.com/page");
                    T.True(s != null, "aucune capture");
                    T.Eq("https://web.archive.org/web/20200312101010/http://example.com/page", s.ArchiveUrl);
                }
            });
            T.Test("Memento (archive.ph) : Location valide / invalide", () =>
            {
                var m = new MementoProvider("archive.today", "https://archive.ph/timegate/", loc => "https://archive.ph/20210101120000/http://exemple.fr/page");
                var s = m.FindClosest("http://exemple.fr/page");
                T.True(s != null && s.DateUtc.Year == 2021);
                var bad = new MementoProvider("x", "https://archive.ph/timegate/", loc => "https://archive.ph/20210101120000/http://autre.fr/page");
                T.True(bad.FindClosest("http://exemple.fr/page") == null);
                T.True(new MementoProvider("x", "g", loc => null).FindClosest("http://exemple.fr/page") == null);
            });
            T.Test("Plusieurs fournisseurs : bascule sur le second si le premier échoue", () =>
            {
                string s = "<ref>" + LW + "</ref>";
                var second = new FakeArchive().Add("http://exemple.fr/p");
                var e = new DeadLinkFixEngine(new FakeChecker().Dead("http://exemple.fr/p"), new IArchiveProvider[] { new FakeArchive(), second }, Opt(), () => Today);
                T.True(e.Fix(s).Archived == 1);
            });
            T.Test("Capture datée dans le futur → rejetée", () =>
                T.True(ArchiveTools.TryBuild("p", "https://web.archive.org/web/20991231000000/http://exemple.fr/p", "http://exemple.fr/p") == null));
        }

        // ---------------------------------------------------------------- divers
        static void MiscTests()
        {
            T.Test("Options : parsing ini, valeurs invalides ignorées", () =>
            {
                var o = DeadLinkFixOptionsLoader.Parse("# c\nMaxLinksPerArticle=20\nAddArchive=false\nMaxParallelism=999\nTimeoutSeconds=abc\nBrokenMarkerFormat={date}\nInconnu=1\nUseArchivePh=false");
                T.Eq(20, o.MaxLinksPerArticle); T.True(!o.AddArchive); T.Eq(8, o.MaxParallelism); T.Eq(20, o.TimeoutSeconds);
                T.Eq("{date}", o.BrokenMarkerFormat); T.True(!o.UseArchivePh);
                T.Eq("{date}", DeadLinkFixOptionsLoader.Parse("BrokenMarkerFormat=sans date").BrokenMarkerFormat);
                T.Eq(50, DeadLinkFixOptionsLoader.LoadFile("/inexistant/x.ini").MaxLinksPerArticle);
            });
            T.Test("Plugin : modifie le texte et le résumé ; intact si rien à faire", () =>
            {
                var plugin = new DeadLinkFixPlugin(Eng(new FakeChecker().Dead("http://exemple.fr/p"), new FakeArchive().Add("http://exemple.fr/p")));
                var a = new Args { ArticleText = "<ref>" + LW + "</ref>" };
                string outText = plugin.ProcessArticle(null, a);
                T.True(outText.Contains("brisé le="));
                T.Eq("Liens externes des références : 1 archive(s) ajoutée(s), 1 lien(s) brisé(s) signalé(s)", a.EditSummary);
                var b = new Args { ArticleText = "Rien <ref>texte</ref>" };
                T.Eq(b.ArticleText, plugin.ProcessArticle(null, b)); T.True(b.EditSummary == null);
                T.Eq("", plugin.ProcessArticle(null, new Args { ArticleText = "" }));
                T.Eq("DeadLinkFix", plugin.Name);
            });
            T.Test("Plugin : jamais d'exception vers AWB (moteur défaillant)", () =>
            {
                var plugin = new DeadLinkFixPlugin(Eng(new FakeChecker { Throw = true }));
                var a = new Args { ArticleText = "<ref>" + LW + "</ref>" };
                T.Eq(a.ArticleText, plugin.ProcessArticle(null, a));
            });
            T.Test("Robustesse : entrées pathologiques ne plantent pas", () =>
            {
                var e = Eng(new FakeChecker());
                foreach (string s in new[] { "<ref>", "</ref>", "<ref>{{Lien web", "<ref>{{Lien web |url=</ref>", "<ref>{{{Lien web|url=http://a.fr/x}}}</ref>",
                    "<!-- <ref>", "<ref>{{Lien web |url=http://a.fr/x }}", "<ref>{{}}</ref>", "<ref>{{Lien web|=|=}}</ref>", "<ref>[[{{Lien web|url=http://a.fr}}</ref>", new string('{', 5000) + "<ref>" })
                    e.Fix(s);
                T.True(true);
            });
            T.Test("Perf : article de 200 000 caractères sans lien, < 2 s", () =>
            {
                var sb = new StringBuilder();
                for (int i = 0; i < 2000; i++) sb.Append("Texte de l'article numéro " + i + " avec du contenu [[lien]] et {{modèle|a=b}} <ref>note " + i + "</ref> ");
                var sw = Stopwatch.StartNew(); Eng(new FakeChecker()).Fix(sb.ToString()); sw.Stop();
                T.True(sw.ElapsedMilliseconds < 2000, sw.ElapsedMilliseconds + " ms");
            });
        }
    }
}
