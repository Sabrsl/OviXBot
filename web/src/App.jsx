import React, { useCallback, useEffect, useRef, useState } from 'react'

async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    method: opts.method || 'GET', credentials: 'same-origin',
    headers: opts.body ? { 'Content-Type': 'application/json' } : (opts.method && opts.method !== 'GET' ? { 'Content-Type': 'application/json' } : {}),
    body: opts.body ? JSON.stringify(opts.body) : (opts.method && opts.method !== 'GET' ? '{}' : undefined),
  })
  const j = await r.json().catch(() => ({}))
  if (r.status === 401 && path !== '/login') { const e = new Error('auth'); e.auth = true; throw e }
  if (!r.ok) throw new Error(j.error || 'Erreur ' + r.status)
  return j
}

const fmt = (s) => (s ? new Date(s).toLocaleString('fr-FR') : '—')
const ago = (s) => { if (!s) return 'jamais'; const d = (Date.now() - new Date(s)) / 1000; return d < 90 ? 'à l\'instant' : d < 3600 ? Math.round(d / 60) + ' min' : d < 86400 ? Math.round(d / 3600) + ' h' : Math.round(d / 86400) + ' j' }
const MODES = {
  dry: ['Simulation', 'Analyse et propose, n\'écrit jamais sur Wikipédia.'],
  supervised: ['Supervisé', 'Chaque modification attend votre validation dans « Propositions ».'],
  auto: ['Autonome', 'Publie seul, dans les quotas. Exige le drapeau bot et une confirmation explicite.'],
}

function Diff({ text }) {
  return <pre className="diff">{(text || '').split('\n').map((l, i) => <span key={i} className={l.startsWith('+') && !l.startsWith('+++') ? 'a' : l.startsWith('-') && !l.startsWith('---') ? 'd' : 'h'}>{l || ' '}</span>)}</pre>
}

function Login({ onOk }) {
  const [t, setT] = useState(''); const [err, setErr] = useState('')
  const go = async (e) => { e.preventDefault(); try { await api('/login', { method: 'POST', body: { token: t } }); onOk() } catch (x) { setErr(x.message) } }
  return <form className="card login" onSubmit={go}>
    <h2>OviXBot</h2><p className="mut">Accès réservé. Saisissez le jeton administrateur.</p>
    <input type="password" autoFocus value={t} onChange={(e) => setT(e.target.value)} placeholder="Jeton administrateur" />
    {err && <p style={{ color: 'var(--bad)' }}>{err}</p>}
    <p><button className="btn pri" type="submit">Se connecter</button></p>
  </form>
}

function Dashboard({ st, reload, go }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const cfg = st.config, halt = st.halt
  const hbAge = st.heartbeat ? (Date.now() - new Date(st.heartbeat)) / 1000 : 1e9
  const act = async (fn) => { setBusy(true); setMsg(''); try { await fn() } catch (e) { setMsg(e.message) } setBusy(false); reload() }
  return <>
    <h2>Tableau de bord</h2>
    {halt && <div className="alert bad kill"><b>ROBOT ARRÊTÉ</b> — {halt.reason} <span className="mut">({fmt(halt.ts)})</span>
      <div className="small mut" style={{ margin: '6px 0' }}>Vérifiez la raison de l'arrêt (page de discussion, journal) avant de reprendre.</div>
      <button className="btn" disabled={busy} onClick={() => confirm('Reprendre l\'activité du robot ? Les demandes d\'arrêt antérieures seront ignorées.') && act(() => api('/resume', { method: 'POST', body: { confirm: true } }))}>Reprendre manuellement</button></div>}
    {hbAge >= 120 && <div className="alert warn">Le worker n'est pas lancé : aucun article n'est traité automatiquement (l'analyse manuelle fonctionne). En local, utilisez run-local ; sur Toolforge, la tâche ovixbot-worker.</div>}
    {!st.credentials && <div className="alert warn">Identifiants du compte absents (variables DRF_BOT_USER / DRF_BOT_PASSWORD ou DRF_OAUTH_TOKEN) : lecture seule, aucune modification possible.</div>}
    {st.bot_error && <div className="alert warn">Connexion à Wikipédia : {st.bot_error}</div>}
    {cfg.mode === 'auto' && st.effective_mode !== 'auto' && <div className="alert warn">Mode autonome demandé mais non actif : il faut le drapeau bot sur le compte et la case de confirmation cochée. Fonctionnement en mode supervisé.</div>}
    <div className="grid" style={{ marginBottom: 16 }}>
      <div className="stat"><b>{halt ? 'Arrêté' : cfg.enabled ? 'Actif' : 'Désactivé'}</b><span>État du robot</span></div>
      <div className="stat"><b>{MODES[st.effective_mode]?.[0] || '—'}</b><span>Mode effectif</span></div>
      <div className="stat"><b>{st.edits_hour}/{cfg.max_edits_per_hour}</b><span>Modifs cette heure</span></div>
      <div className="stat"><b>{st.edits_day}/{cfg.max_edits_per_day}</b><span>Modifs aujourd'hui</span></div>
      <div className="stat"><b>{st.pending}</b><span>Propositions en attente</span></div>
      <div className="stat"><b>{st.queue}</b><span>Articles en file</span></div>
      <div className="stat"><b>{hbAge < 120 ? '● en ligne' : '○ hors ligne'}</b><span>Worker ({ago(st.heartbeat)})</span></div>
      <div className="stat"><b>{st.saved_total}</b><span>Modifications publiées</span></div>
    </div>
    <div className="card"><h3>Commandes</h3>
      <div className="row">
        <button className={'btn ' + (cfg.enabled ? '' : 'ok')} disabled={busy || !!halt} onClick={() => act(() => api('/config', { method: 'PUT', body: { enabled: !cfg.enabled } }))}>{cfg.enabled ? 'Désactiver' : 'Activer le robot'}</button>
        <button className="btn bad" disabled={busy} onClick={() => confirm('Arrêt immédiat du robot ?') && act(() => api('/stop', { method: 'POST' }))}>Arrêt d'urgence local</button>
        <button className="btn" onClick={() => go('security')}>Sécurité & kill switch →</button>
      </div>
      {msg && <p style={{ color: 'var(--bad)' }}>{msg}</p>}
      <p className="mut small">Planification : {cfg.schedule.enabled ? `jours ${cfg.schedule.days.length}/7, ${cfg.schedule.start_hour}h–${cfg.schedule.end_hour}h UTC ${st.in_schedule ? '(dans la plage)' : '(hors plage)'}` : 'en continu'}.</p>
    </div>
    <TestBox st={st} reload={reload} />
  </>
}

function TestBox({ st, reload }) {
  const [t, setT] = useState(''); const [r, setR] = useState(null); const [busy, setBusy] = useState(false); const [msg, setMsg] = useState(''); const [customSummary, setCustomSummary] = useState('')
  const run = async () => {
    setBusy(true); setR(null); setMsg(''); setCustomSummary('')
    try {
      const { job } = await api('/test', { method: 'POST', body: { title: t } })
      for (;;) { await new Promise((x) => setTimeout(x, 1500)); const j = await api('/test/' + job); if (j.done) { setR(j); setCustomSummary(j.summary); break } }
    } catch (e) { setR({ error: e.message }) }
    setBusy(false); reload()
  }
  const publish = async () => {
    if (!confirm('Publier cette correction sur Wikipédia maintenant ?')) return
    setMsg('Publication…')
    try {
      const body = customSummary !== r.summary ? { summary: customSummary } : {}
      const x = await api(`/proposals/${r.proposal_id}/approve`, { method: 'POST', body })
      setMsg(x.result === 'saved' ? 'Publié (révision ' + x.revid + ').' : 'L\'article a changé entre-temps : relancez l\'analyse.')
      reload()
    } catch (e) { setMsg(e.message) }
  }
  const col = { mort: 'var(--bad)', vivant: 'var(--ok)', 'indéterminé': 'var(--warn)' }
  return <div className="card"><h3>Analyser un article</h3>
    <div className="small mut" style={{ marginBottom: 8 }}>Compte bot : <b>{st.account || 'non configuré'}</b></div>
    <div className="row"><input className="grow" type="text" value={t} onChange={(e) => setT(e.target.value)} placeholder="Titre exact de l'article" onKeyDown={(e) => e.key === 'Enter' && t && !busy && run()} />
      <button className="btn pri" disabled={busy || !t} onClick={run}>{busy ? 'Analyse en cours…' : 'Analyser'}</button></div>
    <p className="mut small">L'analyse suit vos réglages (règles, nombre de références) et n'écrit rien : la publication est un bouton à part. Comptez 1 s par lien (politesse envers les sites).</p>
    {r?.error && <p style={{ color: 'var(--bad)' }}>{r.error}</p>}
    {r?.skipped && <p className="mut">Article ignoré : {r.skipped}</p>}
    {r && !r.error && !r.skipped && <>
      <p><b>{r.refs_total}</b> balise(s) ref · <b>{r.analyzed}</b> avec lien vérifiable · <b>{r.dead}</b> mort(s) · <b>{r.total}</b> correction(s){r.offline && <span style={{ color: 'var(--bad)' }}> · Wayback injoignable : rien analysé (vérifiez la connexion Internet)</span>}{r.timed_out && ' · temps dépassé, analyse partielle'}</p>
      {r.report?.length > 0 && <table><thead><tr><th>Lien vérifié</th><th>État</th><th>Détail / archive</th></tr></thead><tbody>
        {r.report.map((x, i) => <tr key={i}><td className="small" style={{ wordBreak: 'break-all' }}>{x.url}</td><td style={{ color: col[x.state] }}>{x.state}</td><td className="small mut" style={{ wordBreak: 'break-all' }}>{x.reason || ''}{x.archive ? ' → ' + x.archive : ''}</td></tr>)}</tbody></table>}
      {r.skipped_refs?.length > 0 && <details style={{ marginTop: 10 }}><summary className="small mut">{r.skipped_refs.length} référence(s) avec URL non traitée(s)</summary>{r.skipped_refs.map((x, i) => <div className="small mut" key={i} style={{ wordBreak: 'break-all' }}>{x.url} — {x.why}</div>)}</details>}
      {r.diff ? <><h3 style={{ marginTop: 14 }}>Modification proposée</h3>
        <div className="card" style={{ marginBottom: 12, padding: 12, background: 'var(--bg-soft)' }}>
          <p className="small"><b>Résumé envoyé à Wikipédia :</b></p>
          <input type="text" value={customSummary} onChange={(e) => setCustomSummary(e.target.value)} style={{ fontFamily: 'monospace', background: 'var(--bg)', padding: 8, borderRadius: 4, width: '100%', marginBottom: 8, fontSize: '12px' }} />
          <p className="small mut" style={{ fontFamily: 'monospace', background: 'var(--bg)', padding: 8, borderRadius: 4, wordBreak: 'break-word', fontSize: '12px' }}>{customSummary || r.summary} ([[Utilisateur:{st.account}|{st.account}]])</p>
        </div>
        <Diff text={r.diff} />
        <div className="row" style={{ marginTop: 12 }}>
          <button className="btn ok" disabled={!st.credentials} onClick={publish}>Publier sur Wikipédia</button>
          {!st.credentials && <span className="mut small">Identifiants du compte absents : publication impossible.</span>}
          {msg && <span>{msg}</span>}</div></> : <p className="mut">Aucune correction à proposer pour cet article.</p>}
    </>}
  </div>
}

function Proposals({ reload }) {
  const [rows, setRows] = useState([]); const [sel, setSel] = useState(null); const [f, setF] = useState('pending'); const [msg, setMsg] = useState(''); const [customSummary, setCustomSummary] = useState('')
  const load = useCallback(async () => setRows(await api('/proposals' + (f ? '?status=' + f : ''))), [f])
  useEffect(() => { load() }, [load])
  const open = async (id) => { const s = await api('/proposals/' + id); setSel(s); setCustomSummary(s.summary) }
  const decide = async (kind) => {
    setMsg('')
    try {
      const body = kind === 'approve' && customSummary !== sel.summary ? { summary: customSummary } : {}
      const r = await api(`/proposals/${sel.id}/${kind}`, { method: 'POST', body })
      if (r.result === 'stale') setMsg('Article modifié depuis l\'analyse : proposition abandonnée.'); else setSel(null)
    } catch (e) { setMsg(e.message) }
    load(); reload()
  }
  return <>
    <h2>Propositions</h2>
    <div className="row" style={{ marginBottom: 12 }}>{[['pending', 'En attente'], ['dry', 'Simulation'], ['saved', 'Publiées'], ['rejected', 'Rejetées'], ['stale', 'Obsolètes'], ['error', 'Erreurs'], ['', 'Toutes']].map(([k, l]) =>
      <button key={k} className={'btn' + (f === k ? ' pri' : '')} onClick={() => { setF(k); setSel(null) }}>{l}</button>)}</div>
    {sel && <div className="card"><div className="row"><h3 className="grow" style={{ margin: 0 }}>{sel.title} <span className={'badge b-' + sel.status}>{sel.status}</span></h3>
      <a className="btn" target="_blank" rel="noreferrer" href={'https://fr.wikipedia.org/wiki/' + encodeURIComponent(sel.title)}>Voir l'article</a>
      <button className="btn" onClick={() => setSel(null)}>Fermer</button></div>
      <div className="card" style={{ marginBottom: 12, padding: 12, background: 'var(--bg-soft)' }}>
        <p className="small"><b>Résumé envoyé à Wikipédia :</b></p>
        {(sel.status === 'pending' || sel.status === 'dry') ? (
          <input type="text" value={customSummary} onChange={(e) => setCustomSummary(e.target.value)} style={{ fontFamily: 'monospace', background: 'var(--bg)', padding: 8, borderRadius: 4, width: '100%', marginBottom: 8, fontSize: '12px' }} />
        ) : (
          <p className="small" style={{ fontFamily: 'monospace', background: 'var(--bg)', padding: 8, borderRadius: 4, wordBreak: 'break-word', fontSize: '12px' }}>{sel.summary}</p>
        )}
      </div>
      <Diff text={sel.diff} />
      {(sel.status === 'pending' || sel.status === 'dry') && <div className="row" style={{ marginTop: 12 }}><button className="btn ok" onClick={() => decide('approve')}>{sel.status === 'dry' ? 'Publier sur Wikipédia' : 'Valider et publier'}</button><button className="btn bad" onClick={() => decide('reject')}>Rejeter</button></div>}
      {msg && <p style={{ color: 'var(--warn)' }}>{msg}</p>}</div>}
    <div className="card"><table><thead><tr><th>Article</th><th>Statut</th><th>Réf.</th><th>Date</th><th>Note</th></tr></thead><tbody>
      {rows.map((r) => <tr key={r.id} className="click" onClick={() => open(r.id)}><td>{r.title}</td><td><span className={'badge b-' + r.status}>{r.status}</span></td><td>{r.total}</td><td>{fmt(r.created)}</td><td className="mut small">{r.note || ''}</td></tr>)}
      {rows.length === 0 && <tr><td colSpan="5" className="mut">Aucune proposition.</td></tr>}</tbody></table></div>
  </>
}

function Articles({ st, reload }) {
  const cfg = st.config
  const [cats, setCats] = useState(cfg.categories); const [q, setQ] = useState(''); const [sugg, setSugg] = useState([]); const [depth, setDepth] = useState(1)
  const [prev, setPrev] = useState(null); const [titles, setTitles] = useState(''); const [queue, setQueue] = useState([]); const [msg, setMsg] = useState('')
  const tm = useRef()
  const loadQ = useCallback(async () => setQueue(await api('/queue')), [])
  useEffect(() => { loadQ() }, [loadQ])
  useEffect(() => { clearTimeout(tm.current); if (q.length < 2) { setSugg([]); return } tm.current = setTimeout(async () => { try { setSugg(await api('/categories/search?q=' + encodeURIComponent(q.startsWith('Cat') ? q : 'Catégorie:' + q))) } catch { } }, 300) }, [q])
  const save = async (patch) => { await api('/config', { method: 'PUT', body: patch }); reload() }
  const addCat = async (name) => { const n = cats.some((c) => c.name === name) ? cats : [...cats, { name, depth }]; setCats(n); setQ(''); setSugg([]); await save({ categories: n }) }
  const rm = async (name) => { const n = cats.filter((c) => c.name !== name); setCats(n); await save({ categories: n }) }
  const preview = async (c) => { setPrev({ loading: true, name: c.name }); try { setPrev({ ...(await api(`/categories/preview?name=${encodeURIComponent(c.name)}&depth=${c.depth}`)), name: c.name }) } catch (e) { setPrev({ error: e.message, name: c.name }) } }
  const enqueue = async () => { const l = titles.split('\n').map((s) => s.trim()).filter(Boolean); if (!l.length) return; setQueue(await api('/queue', { method: 'POST', body: { titles: l } })); setTitles(''); setMsg(l.length + ' article(s) ajouté(s) en priorité.'); reload() }
  return <>
    <h2>Catégories & articles</h2>
    <div className="card"><h3>Catégories sources</h3>
      <div className="chips" style={{ marginBottom: 10 }}>{cats.map((c) => <span className="chip" key={c.name}>{c.name.replace(/^Catégorie:/, '')} · prof. {c.depth}
        <button title="Aperçu" onClick={() => preview(c)}>👁</button><button title="Retirer" onClick={() => rm(c.name)}>✕</button></span>)}
        {cats.length === 0 && <span className="mut">Aucune catégorie. Sans catégorie, seuls la liste manuelle et le mode « au hasard » alimentent le robot.</span>}</div>
      <div className="row"><input className="grow" type="text" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Rechercher une catégorie (ex. Histoire de France)" />
        <select style={{ width: 150 }} value={depth} onChange={(e) => setDepth(+e.target.value)}>{[0, 1, 2, 3].map((d) => <option key={d} value={d}>Sous-cat. : {d}</option>)}</select></div>
      {sugg.length > 0 && <div className="chips" style={{ marginTop: 8 }}>{sugg.map((s) => <button className="btn" key={s.title} onClick={() => addCat(s.title)}>+ {s.title.replace(/^Catégorie:/, '')}</button>)}</div>}
      {prev && <div style={{ marginTop: 12 }} className="small">{prev.loading ? 'Chargement…' : prev.error ? <span style={{ color: 'var(--bad)' }}>{prev.error}</span> : <><b>{prev.name}</b> : {prev.count} article(s){prev.count >= 300 ? '+' : ''} — ex. {prev.sample.join(' · ')}</>}</div>}
      <label><input type="checkbox" checked={cfg.random_pages} onChange={(e) => save({ random_pages: e.target.checked })} /> Compléter avec des articles au hasard quand les catégories sont épuisées</label>
    </div>
    <div className="card"><h3>Sélection</h3>
      <div className="grid">
        <div><label>Articles par cycle</label><input type="number" min="1" max="100" defaultValue={cfg.batch_size} onBlur={(e) => save({ batch_size: +e.target.value })} /></div>
        <div><label>Ne pas ré-analyser avant (jours)</label><input type="number" min="0" defaultValue={cfg.recheck_days} onBlur={(e) => save({ recheck_days: +e.target.value })} /></div>
        <div><label>Références par article (0 = toutes)</label><input type="number" min="0" defaultValue={cfg.max_refs} onBlur={(e) => save({ max_refs: +e.target.value })} /></div>
      </div>
      <p className="mut small">Seul l'espace principal est traité ; redirections, pages avec {'{{nobots}}'} / {'{{bots|deny=…}}'} exclues.</p></div>
    <div className="card"><h3>Ajouter des articles à la main (traités en priorité)</h3>
      <textarea rows="4" value={titles} onChange={(e) => setTitles(e.target.value)} placeholder="Un titre par ligne" />
      <div className="row" style={{ marginTop: 8 }}><button className="btn pri" onClick={enqueue}>Ajouter à la file</button>
        <button className="btn" onClick={async () => { setQueue(await api('/queue', { method: 'DELETE' })); reload() }}>Vider la file</button>{msg && <span className="mut">{msg}</span>}</div>
      <p className="small mut">File d'attente : {queue.length} article(s){queue.length ? ' — ' + queue.slice(0, 8).map((x) => x.title).join(', ') + (queue.length > 8 ? '…' : '') : ''}</p></div>
  </>
}

const DAYS = ['L', 'M', 'M', 'J', 'V', 'S', 'D']
function Scheduler({ st, reload }) {
  const [c, setC] = useState(st.config); const [ok, setOk] = useState('')
  const set = (k, v) => setC((o) => ({ ...o, [k]: v }))
  const sch = (k, v) => setC((o) => ({ ...o, schedule: { ...o.schedule, [k]: v } }))
  const KEYS = ['mode', 'autonomy_confirmed', 'schedule', 'min_edit_interval', 'max_edits_per_hour', 'max_edits_per_day', 'max_errors_before_pause', 'max_seconds', 'rules_disabled']
  const save = async () => { try { await api('/config', { method: 'PUT', body: Object.fromEntries(KEYS.map((k) => [k, c[k]])) }); setOk('Enregistré.'); reload() } catch (e) { setOk(e.message) } }
  return <>
    <h2>Planificateur & modes</h2>
    <div className="card"><h3>Mode de fonctionnement</h3>
      <div className="mode">{Object.entries(MODES).map(([k, [n, d]]) => <div key={k} className={c.mode === k ? 'on' : ''} onClick={() => set('mode', k)}><b>{n}</b><span>{d}</span></div>)}</div>
      {c.mode === 'auto' && <div className="alert warn" style={{ marginTop: 12 }}>
        <label style={{ margin: 0, color: 'inherit' }}><input type="checkbox" checked={c.autonomy_confirmed} onChange={(e) => set('autonomy_confirmed', e.target.checked)} /> Je confirme que le compte dispose du drapeau bot (tâche approuvée) et j'autorise les modifications sans validation.</label>
        <div className="small">Statut du compte : {st.bot ? (st.bot.is_bot ? 'drapeau bot présent ✓' : 'PAS de drapeau bot — le mode autonome restera inactif') : 'non connecté'}</div></div>}</div>
    <div className="card"><h3>Planification (UTC)</h3>
      <label><input type="checkbox" checked={c.schedule.enabled} onChange={(e) => sch('enabled', e.target.checked)} /> Limiter à une plage horaire</label>
      {c.schedule.enabled && <><div className="days" style={{ margin: '10px 0' }}>{DAYS.map((d, i) => <button key={i} className={'btn' + (c.schedule.days.includes(i) ? ' on' : '')} onClick={() => sch('days', c.schedule.days.includes(i) ? c.schedule.days.filter((x) => x !== i) : [...c.schedule.days, i])}>{d}</button>)}</div>
        <div className="grid"><div><label>Début (heure)</label><input type="number" min="0" max="23" value={c.schedule.start_hour} onChange={(e) => sch('start_hour', +e.target.value)} /></div>
          <div><label>Fin (heure)</label><input type="number" min="1" max="24" value={c.schedule.end_hour} onChange={(e) => sch('end_hour', +e.target.value)} /></div>
          <div><label>Pause entre cycles (s)</label><input type="number" min="30" value={c.schedule.cycle_pause} onChange={(e) => sch('cycle_pause', +e.target.value)} /></div></div></>}</div>
    <div className="card"><h3>Quotas & garde-fous</h3><div className="grid">
      <div><label>Intervalle min. entre modifs (s, ≥ 10)</label><input type="number" min="10" value={c.min_edit_interval} onChange={(e) => set('min_edit_interval', +e.target.value)} /></div>
      <div><label>Max modifs / heure (≤ 60)</label><input type="number" min="1" max="60" value={c.max_edits_per_hour} onChange={(e) => set('max_edits_per_hour', +e.target.value)} /></div>
      <div><label>Max modifs / jour (≤ 500)</label><input type="number" min="1" max="500" value={c.max_edits_per_day} onChange={(e) => set('max_edits_per_day', +e.target.value)} /></div>
      <div><label>Pause après N erreurs consécutives</label><input type="number" min="1" value={c.max_errors_before_pause} onChange={(e) => set('max_errors_before_pause', +e.target.value)} /></div>
      <div><label>Durée max par article (s)</label><input type="number" min="30" value={c.max_seconds} onChange={(e) => set('max_seconds', +e.target.value)} /></div></div></div>
    <div className="card"><h3>Règles de détection</h3>
      {[['DRF01', 'Erreur HTTP (404, 410, 5xx…)'], ['DRF02', 'Soft 404 / redirection vers l\'accueil'], ['DRF03', 'Domaine injoignable'], ['DRF04', 'URL brute → {{Lien web}} / {{Article}}'], ['DRF05', 'Ajout des paramètres d\'archive aux modèles existants'], ['DRF06', 'Compléter les champs manquants'], ['DRF07', 'Titres contenant « | » (écrits &#124;)']].map(([id, l]) =>
        <label key={id} style={{ margin: '4px 0', color: 'var(--tx)' }}><input type="checkbox" checked={!c.rules_disabled.includes(id)} onChange={(e) => set('rules_disabled', e.target.checked ? c.rules_disabled.filter((x) => x !== id) : [...c.rules_disabled, id])} /> <b>{id}</b> {l}</label>)}</div>
    <div className="row"><button className="btn pri" onClick={save}>Enregistrer</button>{ok && <span className="mut">{ok}</span>}</div>
  </>
}

function Security({ st, reload }) {
  const [r, setR] = useState(null); const [busy, setBusy] = useState(false)
  const check = async () => { setBusy(true); try { setR(await api('/killswitch/check', { method: 'POST' })) } catch (e) { setR({ error: e.message }) } setBusy(false); reload() }
  return <>
    <h2>Sécurité & arrêt d'urgence</h2>
    <div className="card kill"><h3>Kill switch communautaire</h3>
      <p>N'importe quel contributeur peut arrêter le robot en ouvrant une nouvelle section « Arrêt d'urgence » sur <a href={'https://fr.wikipedia.org/wiki/' + st.talk_page.replace(/ /g, '_')} target="_blank" rel="noreferrer">{st.talk_page}</a>. Le robot vérifie cette page toutes les 20 secondes et avant chaque modification ; à la détection il s'arrête et <b>ne reprend que sur validation manuelle</b> depuis ce tableau de bord.</p>
      <div className="row"><a className="btn bad" href={st.kill_url} target="_blank" rel="noreferrer">Déclencher le kill switch (page wiki)</a>
        <button className="btn" disabled={busy} onClick={check}>{busy ? 'Vérification…' : 'Vérifier maintenant'}</button></div>
      {r?.error && <p style={{ color: 'var(--bad)' }}>{r.error}</p>}{r && !r.error && <p>{r.triggered ? '⚠ Arrêt d\'urgence détecté.' : '✓ Aucun arrêt demandé.'}</p>}</div>
    <div className="card"><h3>Mécanismes de protection actifs</h3><ul style={{ margin: 0, paddingLeft: 18 }}>
      <li><b>Arrêt automatique</b> : kill switch, blocage du compte, 5 erreurs consécutives, vérification du kill switch impossible (réseau → on ne modifie pas).</li>
      <li><b>Autonomie conditionnée</b> : le mode autonome exige le drapeau bot ET une confirmation explicite ; sinon retour au mode supervisé.</li>
      <li><b>Quotas</b> : modifications par heure et par jour, intervalle minimal, <code>maxlag=5</code>, respect de <code>Retry-After</code>.</li>
      <li><b>Conflits</b> : publication refusée si l'article a changé depuis l'analyse (<code>baserevid</code> + <code>basetimestamp</code>, <code>nocreate</code>).</li>
      <li><b>Exclusions</b> : espace principal uniquement, <code>{'{{nobots}}'}</code>/<code>{'{{bots}}'}</code> respectés, aucune modification si Wayback est injoignable.</li>
      <li><b>Réseau</b> : User-Agent identifiant le compte, 1 requête/s/hôte, blocage des adresses internes (SSRF), 1,5 Mo max par page.</li>
      <li><b>Accès</b> : tableau de bord protégé par jeton, cookies HttpOnly/SameSite=Strict, limitation des tentatives.</li></ul></div>
    <div className="card"><h3>Compte</h3><p>Compte : <b>{st.account}</b> — {st.bot ? <>connecté comme {st.bot.name} · groupes : {st.bot.groups.join(', ') || '—'} {st.bot.blocked && <span className="badge b-bad">bloqué</span>} {st.bot.hasmsg && <span className="badge b-warn">nouveau message</span>}</> : 'non connecté'}</p></div>
  </>
}

function Journal() {
  const [rows, setRows] = useState([]); const [auto, setAuto] = useState(true)
  const load = useCallback(async () => setRows(await api('/log?limit=300')), [])
  useEffect(() => { load(); if (!auto) return; const t = setInterval(load, 8000); return () => clearInterval(t) }, [load, auto])
  return <><h2>Journal</h2><div className="row" style={{ marginBottom: 10 }}><button className="btn" onClick={load}>Actualiser</button><label style={{ margin: 0 }}><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto</label></div>
    <div className="card"><table><thead><tr><th>Date</th><th>Événement</th><th>Article</th><th>Détail</th></tr></thead><tbody>{rows.map((r) =>
      <tr key={r.id}><td className="small mut" style={{ whiteSpace: 'nowrap' }}>{fmt(r.ts)}</td><td><span className={'badge ' + (r.level === 'error' ? 'b-bad' : r.level === 'warn' ? 'b-warn' : '')}>{r.event}</span></td><td>{r.page}</td><td className="small mut">{r.detail}</td></tr>)}</tbody></table></div></>
}

const TABS = [['dash', 'Tableau de bord'], ['props', 'Propositions'], ['arts', 'Catégories & articles'], ['sched', 'Planificateur'], ['security', 'Sécurité'], ['log', 'Journal']]

export default function App() {
  const [authed, setAuthed] = useState(null); const [st, setSt] = useState(null); const [tab, setTab] = useState(() => {
    const hash = window.location.hash.slice(1)
    return TABS.some(([k]) => k === hash) ? hash : 'dash'
  }); const [err, setErr] = useState('')
  const reload = useCallback(async () => { try { setSt(await api('/status')); setAuthed(true); setErr('') } catch (e) { if (e.auth) setAuthed(false); else setErr(e.message) } }, [])
  useEffect(() => { reload(); const t = setInterval(reload, 6000); return () => clearInterval(t) }, [reload])
  useEffect(() => { window.location.hash = tab }, [tab])
  if (authed === false) return <Login onOk={() => { setAuthed(true); reload() }} />
  if (!st) return <p style={{ padding: 24 }} className="mut">{err || 'Chargement…'}</p>
  return <div className="app">
    <nav><h1>OviXBot<small>DeadRefFix · Toolforge</small></h1>
      {TABS.map(([k, l]) => <button key={k} className={tab === k ? 'on' : ''} onClick={() => setTab(k)}>{l}{k === 'props' && st.pending > 0 ? ` (${st.pending})` : ''}{k === 'security' && st.halt ? ' ⚠' : ''}</button>)}
      <div className="sp" /><button onClick={async () => { await api('/logout', { method: 'POST' }); setAuthed(false) }}>Déconnexion</button></nav>
    <main>
      {tab === 'dash' && <Dashboard st={st} reload={reload} go={setTab} />}
      {tab === 'props' && <Proposals reload={reload} />}
      {tab === 'arts' && <Articles key={JSON.stringify(st.config.categories)} st={st} reload={reload} />}
      {tab === 'sched' && <Scheduler st={st} reload={reload} />}
      {tab === 'security' && <Security st={st} reload={reload} />}
      {tab === 'log' && <Journal />}
    </main></div>
}
