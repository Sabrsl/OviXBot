using System;
using System.IO;
using System.Net;
using System.Reflection;
using WikiFunctions.Plugin;

namespace DeadLinkFix.Awb
{
    /// <summary>
    /// Plugin AWB : détecte et répare les liens externes morts, UNIQUEMENT dans les références
    /// ({{Lien web}}, {{Ouvrage}}, {{Article}}). Toute la logique est dans <see cref="DeadLinkFixEngine"/>
    /// (sans dépendance AWB, testable seule). Réglages optionnels : DeadLinkFix.ini à côté de la DLL.
    /// </summary>
    public class DeadLinkFixPlugin : IAWBPlugin
    {
        private readonly DeadLinkFixEngine _engine;

        public DeadLinkFixPlugin()
        {
            try
            {
                ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072;   // TLS 1.2
                if (ServicePointManager.DefaultConnectionLimit < 32) ServicePointManager.DefaultConnectionLimit = 32;
            }
            catch (Exception) { }

            string dir = string.Empty;
            try { dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location) ?? string.Empty; } catch (Exception) { }
            DeadLinkFixOptions options = DeadLinkFixOptionsLoader.LoadFile(Path.Combine(dir, "DeadLinkFix.ini"));

            var checker = new CachingLinkChecker(
                new HttpLinkChecker(TimeSpan.FromSeconds(options.TimeoutSeconds)), TimeSpan.FromMinutes(30));

            var providers = new System.Collections.Generic.List<IArchiveProvider> { new WaybackProvider() };
            if (options.UseArchivePh) providers.Add(new MementoProvider("archive.today", "https://archive.ph/timegate/"));

            _engine = new DeadLinkFixEngine(checker, providers, options, null);
        }

        internal DeadLinkFixPlugin(DeadLinkFixEngine engine) { _engine = engine; }

        public string Name => "DeadLinkFix";
        public string WikiName => string.Empty;

        public string ProcessArticle(IAutoWikiBrowser sender, IProcessArticleEventArgs e)
        {
            string text = e.ArticleText;
            if (string.IsNullOrEmpty(text)) return text;

            try
            {
                DeadLinkFixResult result = _engine.Fix(text);
                if (!result.Changed) return text;

                e.EditSummary = DeadLinkFixEngine.BuildSummary(result);
                return result.Text;
            }
            catch (Exception)
            {
                // Jamais d'exception vers AWB : en cas de problème, l'article reste intact.
                return text;
            }
        }

        public void Initialise(IAutoWikiBrowser sender) { }
        public void LoadSettings(object[] prefs) { }
        public object[] SaveSettings() { return new object[0]; }
        public void Reset() { }
        public void Nudge(out bool nudged) { nudged = false; }
        public void Nudged(int nudges) { }
    }
}
