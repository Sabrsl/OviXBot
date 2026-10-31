using System;
using System.Collections.Generic;
using System.Text;
using System.Text.RegularExpressions;

namespace DeadLinkFix.Awb
{
    internal sealed class TemplateParam
    {
        public string Name;          // normalisé : minuscules, espaces simples, « _ » → espace
        public int SegStart;         // 1er caractère après le « | »
        public int SegEnd;           // fin du segment (avant le « | » suivant ou « }} »)
        public int EqIndex;          // position du « = » (toujours >= 0 ici : seuls les paramètres nommés sont conservés)
        public int ValueStart;       // valeur sans espaces de bordure
        public int ValueEnd;
        public string Value;         // texte ORIGINAL de la valeur (sans espaces de bordure)
        public bool HasProtected;    // la valeur contient un commentaire / nowiki masqué
    }

    internal sealed class ParsedTemplate
    {
        public string Name;          // normalisé
        public int Start;            // position du « {{ »
        public int End;              // position juste après « }} »
        public List<TemplateParam> Params = new List<TemplateParam>();
    }

    internal sealed class RefBlock
    {
        public int BodyStart;
        public int BodyEnd;
    }

    /// <summary>Analyse minimale et sûre du wikicode : masquage, balises ref, modèles et paramètres.</summary>
    internal static class WikiScan
    {
        public const char MaskChar = '\uE000';

        private static readonly Regex Protected = new Regex(
            @"<!--.*?(?:-->|$)|<nowiki\s*/>|<nowiki\b[^>]*>.*?(?:</nowiki\s*>|$)|<(?<t>pre|syntaxhighlight|source|code|math|templatedata)\b[^>]*>.*?(?:</\k<t>\s*>|$)",
            RegexOptions.Singleline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        private static readonly Regex RefRegex = new Regex(
            @"<ref\b[^>]*?(?<!/)>(?<body>.*?)</ref\s*>",
            RegexOptions.Singleline | RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

        /// <summary>Remplace commentaires et zones nowiki/code par MaskChar, en conservant la longueur (positions identiques).</summary>
        public static string Mask(string text)
        {
            var sb = new StringBuilder(text);
            foreach (Match m in Protected.Matches(text))
                for (int i = m.Index; i < m.Index + m.Length; i++)
                    sb[i] = MaskChar;
            return sb.ToString();
        }

        public static List<RefBlock> FindRefs(string masked)
        {
            var list = new List<RefBlock>();
            foreach (Match m in RefRegex.Matches(masked))
            {
                Group g = m.Groups["body"];
                list.Add(new RefBlock { BodyStart = g.Index, BodyEnd = g.Index + g.Length });
            }
            return list;
        }

        public static string NormalizeName(string s)
        {
            string t = s.Replace('_', ' ').Trim().ToLowerInvariant();
            t = Regex.Replace(t, @"\s+", " ");
            if (t.StartsWith("modèle:", StringComparison.Ordinal)) t = t.Substring(7).Trim();
            else if (t.StartsWith("template:", StringComparison.Ordinal)) t = t.Substring(9).Trim();
            return t;
        }

        /// <summary>Modèles de premier niveau de <paramref name="masked"/> ; <paramref name="original"/> sert à lire les valeurs.</summary>
        public static List<ParsedTemplate> FindTemplates(string masked, string original)
        {
            var list = new List<ParsedTemplate>();
            int i = 0;
            while (i < masked.Length - 1)
            {
                if (masked[i] == '{' && masked[i + 1] == '{' && !(i + 2 < masked.Length && masked[i + 2] == '{'))
                {
                    int end = FindEnd(masked, i);
                    if (end < 0) break;
                    ParsedTemplate t = Parse(masked, original, i, end);
                    if (t != null) list.Add(t);
                    i = end;
                }
                else i++;
            }
            return list;
        }

        private static int FindEnd(string s, int start)
        {
            int depth = 0, i = start;
            while (i < s.Length - 1)
            {
                if (s[i] == '{' && s[i + 1] == '{') { depth++; i += 2; continue; }
                if (s[i] == '}' && s[i + 1] == '}')
                {
                    depth--; i += 2;
                    if (depth == 0) return i;
                    continue;
                }
                i++;
            }
            return -1;
        }

        private static ParsedTemplate Parse(string m, string orig, int start, int end)
        {
            int innerStart = start + 2, innerEnd = end - 2;
            var t = new ParsedTemplate { Start = start, End = end };

            int depthBrace = 0, depthBracket = 0;
            int segStart = innerStart, eq = -1;
            bool first = true;

            Action<int> closeSegment = segEnd =>
            {
                if (first)
                {
                    t.Name = NormalizeName(orig.Substring(segStart, segEnd - segStart));
                    first = false;
                }
                else if (eq >= 0)
                {
                    string name = NormalizeName(orig.Substring(segStart, eq - segStart));
                    int vs = eq + 1, ve = segEnd;
                    while (vs < ve && char.IsWhiteSpace(orig[vs])) vs++;
                    while (ve > vs && char.IsWhiteSpace(orig[ve - 1])) ve--;
                    if (vs == ve) { vs = eq + 1; ve = eq + 1; }
                    string masked = m.Substring(vs, ve - vs);
                    t.Params.Add(new TemplateParam
                    {
                        Name = name, SegStart = segStart, SegEnd = segEnd, EqIndex = eq,
                        ValueStart = vs, ValueEnd = ve, Value = orig.Substring(vs, ve - vs),
                        HasProtected = masked.IndexOf(MaskChar) >= 0,
                    });
                }
            };

            for (int p = innerStart; p < innerEnd; p++)
            {
                char c = m[p];
                char n = p + 1 < innerEnd ? m[p + 1] : '\0';
                if (c == '{' && n == '{') { depthBrace++; p++; }
                else if (c == '}' && n == '}') { if (depthBrace > 0) depthBrace--; p++; }
                else if (c == '[' && n == '[') { depthBracket++; p++; }
                else if (c == ']' && n == ']') { if (depthBracket > 0) depthBracket--; p++; }
                else if (depthBrace == 0 && depthBracket == 0)
                {
                    if (c == '|')
                    {
                        closeSegment(p);
                        segStart = p + 1; eq = -1;
                    }
                    else if (c == '=' && eq < 0 && !first) eq = p;
                }
            }
            closeSegment(innerEnd);
            return t.Name == null ? null : t;
        }
    }
}
