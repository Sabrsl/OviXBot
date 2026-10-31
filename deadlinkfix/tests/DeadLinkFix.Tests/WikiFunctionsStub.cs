// Doublure minimale de WikiFunctions.Plugin, uniquement pour compiler/tester le plugin sans AWB.
namespace WikiFunctions.Plugin
{
    public interface IAutoWikiBrowser { }
    public interface IProcessArticleEventArgs
    {
        string ArticleText { get; }
        string EditSummary { get; set; }
    }
    public interface IAWBPlugin
    {
        string Name { get; }
        string WikiName { get; }
        string ProcessArticle(IAutoWikiBrowser sender, IProcessArticleEventArgs e);
        void Initialise(IAutoWikiBrowser sender);
        void LoadSettings(object[] prefs);
        object[] SaveSettings();
        void Reset();
        void Nudge(out bool nudged);
        void Nudged(int nudges);
    }
}
