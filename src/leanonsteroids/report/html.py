"""Rapport HTML autonome : photo annotée ↔ transcription ↔ étapes ↔ Lean ↔ verdict.

Un clic sur une ligne de la photo (ou sur une étape) met en évidence tous les
éléments liés. Le rendu des formules utilise KaTeX (cdnjs).
"""

from __future__ import annotations

import base64
import html
import json
import mimetypes
from pathlib import Path

from ..schemas import RunResult, Verdict

STATUS_LABEL = {
    "verifie_elementaire": ("vérifiée", "ok"),
    "verifie_theoreme": ("vérifiée (théorème cité)", "ok"),
    "verifie_agent": ("vraie, saut logique", "warn"),
    "refute": ("réfutée", "bad"),
    "non_verifie": ("non vérifiée", "warn"),
    "hypothese": ("hypothèse", "neutral"),
    "definition": ("définition", "neutral"),
    "non_formalise": ("non formalisée", "muted"),
    "erreur_formalisation": ("formalisation invalide", "bad"),
}
VERDICT_CLASS = {Verdict.verified: "ok", Verdict.error: "bad", Verdict.review: "warn"}

CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1d1b;--muted:#6b6b66;--line:#e3e2dc;--ok:#1f7a4d;--okbg:#e5f4ec;
--bad:#b3261e;--badbg:#fbe9e7;--warn:#9a6700;--warnbg:#fff4d6;--neutral:#3a5a8c;--neutralbg:#e8eef8;--hl:#ffe58a;
--code:#f1f0ec;--accent:#3a5a8c}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--panel:#1f1f1d;--ink:#ececE8;
--muted:#a3a39c;--line:#34342f;--ok:#6fcf97;--okbg:#17301f;--bad:#f28b82;--badbg:#3a1d1a;--warn:#f2c14e;
--warnbg:#332a12;--neutral:#8fb0e6;--neutralbg:#1b2638;--hl:#5c4d12;--code:#262623;--accent:#8fb0e6}}
:root[data-theme="dark"]{--bg:#141413;--panel:#1f1f1d;--ink:#ececE8;--muted:#a3a39c;--line:#34342f;--ok:#6fcf97;
--okbg:#17301f;--bad:#f28b82;--badbg:#3a1d1a;--warn:#f2c14e;--warnbg:#332a12;--neutral:#8fb0e6;--neutralbg:#1b2638;
--hl:#5c4d12;--code:#262623;--accent:#8fb0e6}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",Roboto,sans-serif}
header{padding:20px 16px 8px;max-width:1400px;margin:auto}h1{font-size:22px;margin:0 0 4px}
h2{font-size:17px;margin:0 0 10px}.sub{color:var(--muted);font-size:13px}
main{max-width:1400px;margin:auto;padding:0 16px 40px;display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:16px}
@media (max-width:900px){main{grid-template-columns:1fr}.sticky{position:static!important}}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px;overflow-x:auto}
.sticky{position:sticky;top:12px}
.photo{position:relative}.photo img{width:100%;display:block;border-radius:6px}
.photo svg{position:absolute;inset:0;width:100%;height:100%}
.box{fill:transparent;stroke-width:3;cursor:pointer;vector-effect:non-scaling-stroke}
.box.ok{stroke:var(--ok)}.box.bad{stroke:var(--bad)}.box.warn{stroke:var(--warn)}.box.neutral{stroke:var(--neutral)}
.box.muted{stroke:var(--muted);stroke-dasharray:4 3}.box.hl{fill:rgba(255,200,0,.25);stroke-width:4}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;white-space:nowrap}
.ok{color:var(--ok)}.badge.ok{background:var(--okbg)}.bad{color:var(--bad)}.badge.bad{background:var(--badbg)}
.warn{color:var(--warn)}.badge.warn{background:var(--warnbg)}.neutral{color:var(--neutral)}.badge.neutral{background:var(--neutralbg)}
.muted{color:var(--muted)}.badge.muted{background:var(--code)}
.verdict{font-size:20px;padding:6px 14px}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border-bottom:1px solid var(--line);padding:6px 8px;
text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase}
tr.hl td{background:var(--hl)}tr[data-lines]{cursor:pointer}
pre{background:var(--code);padding:8px;border-radius:6px;overflow-x:auto;font-size:12.5px;margin:6px 0 0;white-space:pre}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
details summary{cursor:pointer;color:var(--accent);font-size:13px}
ul{margin:4px 0;padding-left:20px}.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;font-size:14px}
.kv div:nth-child(odd){color:var(--muted)}.small{font-size:12.5px}
.toggle{float:right;background:none;border:1px solid var(--line);color:var(--ink);border-radius:6px;padding:2px 8px;cursor:pointer}
"""

JS = """
function hl(lines){document.querySelectorAll('.hl').forEach(e=>e.classList.remove('hl'));
 lines.forEach(l=>{document.querySelectorAll('[data-line="'+l+'"]').forEach(e=>e.classList.add('hl'));
 document.querySelectorAll('tr[data-lines]').forEach(r=>{if(r.dataset.lines.split(' ').includes(l))r.classList.add('hl')})})}
document.querySelectorAll('[data-lines]').forEach(e=>e.addEventListener('click',()=>hl(e.dataset.lines.split(' '))));
document.querySelectorAll('.box').forEach(e=>e.addEventListener('click',()=>hl([e.dataset.line])));
document.getElementById('theme').addEventListener('click',()=>{const r=document.documentElement;
 const d=r.dataset.theme==='dark'||(!r.dataset.theme&&matchMedia('(prefers-color-scheme: dark)').matches);r.dataset.theme=d?'light':'dark'});
document.addEventListener('DOMContentLoaded',()=>{if(window.renderMathInElement)renderMathInElement(document.body,
 {delimiters:[{left:'$',right:'$',display:false},{left:'\\\\(',right:'\\\\)',display:false},{left:'\\\\[',right:'\\\\]',display:true}],throwOnError:false})});
"""


def e(s: object) -> str:
    return html.escape(str(s), quote=True)


def _img_data(path: str) -> str:
    p = Path(path)
    if not p.exists():
        return ""
    mime = mimetypes.guess_type(p.name)[0] or "image/webp"
    if p.suffix.lower() == ".webp":
        mime = "image/webp"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


def render_html(r: RunResult, out_dir: Path | None = None) -> str:
    st_by = {c.step_id: c for c in r.lean.steps}
    line_class: dict[str, str] = {}
    for s in r.structure.steps:
        c = st_by.get(s.id)
        cls = STATUS_LABEL.get(c.status, ("", "muted"))[1] if c else "muted"
        for ref in s.source:
            prev = line_class.get(ref.line_id)
            rank = {"bad": 4, "warn": 3, "ok": 2, "neutral": 1, "muted": 0}
            if prev is None or rank[cls] > rank[prev]:
                line_class[ref.line_id] = cls
    for u in r.transcription.uncertainties:
        if u.blocking:
            line_class[u.line_id] = "warn"

    # Photo(s)
    photos = []
    for pg in r.transcription.pages:
        boxes = []
        for ln in r.transcription.lines:
            if ln.page != pg.page:
                continue
            x0, y0, x1, y1 = ln.bbox
            cls = line_class.get(ln.id, "muted")
            boxes.append(f'<rect class="box {cls}" data-line="{e(ln.id)}" x="{x0}" y="{y0}" width="{x1 - x0}" '
                         f'height="{y1 - y0}" rx="6"><title>{e(ln.id)} — {e(ln.text)}</title></rect>')
        photos.append(
            f'<div class="photo"><img alt="Copie, page {pg.page}" src="{_img_data(pg.path)}">'
            f'<svg viewBox="0 0 {pg.width} {pg.height}" preserveAspectRatio="none">{"".join(boxes)}</svg></div>'
            f'<div class="sub">Page {pg.page} — {e(Path(pg.path).name)} — sha256 {e(pg.sha256[:12])}…</div>')

    v = r.verdict
    vcls = VERDICT_CLASS[v.verdict]
    verdict_html = (
        f'<section><h2>Verdict <span class="badge verdict {vcls}">{e(v.verdict.value)}</span></h2>'
        + "<ul>" + "".join(f"<li>{e(x)}</li>" for x in v.reasons) + "</ul>"
        + (("<h3 class='small warn'>Points bloquants</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in v.blocking_issues)
            + "</ul>") if v.blocking_issues else "")
        + (("<details><summary>Remarques (" + str(len(v.remarks)) + ")</summary><ul>"
            + "".join(f"<li>{e(x)}</li>" for x in v.remarks) + "</ul></details>") if v.remarks else "")
        + "</section>")

    fb = r.feedback
    fb_html = ('<section><h2>Retour proposé à l’élève</h2>'
               f'<p><strong>{e(fb.summary)}</strong></p>'
               + _list("Points forts", fb.points_forts) + _list("À corriger", fb.points_a_corriger)
               + _list("Rédaction", fb.conseils_redaction)
               + _list("Pour une rédaction parfaite", fb.pour_une_redaction_parfaite)
               + (f'<details><summary>Note pour le correcteur</summary><pre>{e(fb.note_pour_correcteur)}</pre></details>'
                  if fb.note_pour_correcteur else "")
               + f'<div class="sub">Rédigé par : {e(fb.generated_by)}</div></section>')

    # Transcription
    rows = []
    for ln in r.transcription.lines:
        conf_cls = "ok" if ln.confidence >= 0.8 else ("warn" if ln.confidence >= 0.5 else "bad")
        engines = "".join(f"<div class='small muted'>{e(k)} : {e(val)}</div>"
                          for k, val in ln.engine_readings.items() if val != ln.text)
        rows.append(f'<tr data-lines="{e(ln.id)}"><td><code>{e(ln.id)}</code></td><td>{e(ln.text)}{engines}</td>'
                    f'<td><span class="badge {conf_cls}">{ln.confidence:.2f}</span></td><td class="small">{e(ln.status.value)}</td></tr>')
    tr_html = ('<section><h2>Transcription</h2>'
               f'<div class="sub">{e(r.transcription.provenance)}</div>'
               f'<div class="sub">Moteurs : {e(", ".join(f"{x.engine} ({x.model}, {x.mode})" for x in r.transcription.engines))}</div>'
               '<table><tr><th>Ligne</th><th>Texte lu</th><th>Confiance</th><th>État</th></tr>' + "".join(rows) + "</table></section>")

    # Incertitudes
    urows = []
    for u in r.transcription.uncertainties:
        cls = "bad" if u.blocking else "ok"
        reads = " / ".join(f"<code>{e(x.text)}</code> ({x.score:.2f})" for x in u.readings)
        urows.append(f'<tr data-lines="{e(u.line_id)}"><td><code>{e(u.id)}</code><br><span class="small">{e(u.line_id)}</span></td>'
                     f'<td>{reads}<div class="small muted">{e(u.reason)}</div></td>'
                     f'<td><span class="badge {cls}">{"bloquante" if u.blocking else "non bloquante"}</span>'
                     f'{"<div class=small>dépend du contexte</div>" if u.context_dependent else ""}</td>'
                     f'<td class="small">{e(u.resolution or "")}</td></tr>')
    unc_html = ('<section><h2>Incertitudes de lecture</h2><table><tr><th>Id</th><th>Lectures</th><th>Effet</th>'
                '<th>Analyse de sensibilité</th></tr>' + "".join(urows) + "</table></section>") if urows else ""

    # Étapes
    fid_by: dict[str, list] = {}
    for c in r.fidelity:
        for sid in c.step_id.split(","):
            fid_by.setdefault(sid, []).append(c)
    srows = []
    for s in r.structure.steps:
        c = st_by.get(s.id)
        label, cls = STATUS_LABEL.get(c.status, ("?", "muted")) if c else ("?", "muted")
        lines = " ".join(sorted({x.line_id for x in s.source}))
        f = next((x for x in r.formalization.steps if x.step_id == s.id), None)
        extra = []
        if c and c.closed_by and c.status != "non_formalise":
            extra.append(f"fermée par <code>{e(c.closed_by)}</code>")
        if c and c.counterexample:
            extra.append(f"<span class='bad'>contre-exemple : {e(c.counterexample)}</span>")
        if c and c.independent_of_deps and s.depends_on:
            extra.append("vraie sans ses dépendances")
        fids = "".join(f"<div class='small'>{'✔' if x.ok else ('✘' if x.ok is False else '•')} "
                       f"{e(x.kind)} : {e(x.detail)}</div>" for x in fid_by.get(s.id, []))
        lean_code = f"<details><summary>Lean</summary><pre><code>{e(c.lean_snippet)}</code></pre></details>" if c and c.lean_snippet else ""
        origin = {"code": "traduit par le code", "agent": "traduit par un agent", "mémoire": "repris de la mémoire",
                  "": ""}.get(f.origin, f.origin) if f else ""
        claim = (f"<div class='small'><code>{e(f.claim or f.def_body or f.not_formalized_reason or '')}</code>"
                 f"{' <span class=muted>· ' + e(origin) + '</span>' if origin else ''}</div>") if f else ""
        msgs = "".join(f"<pre class='small'>{e(m.text[:600])}</pre>" for m in (c.messages if c and c.status != 'refute' else [])[:2])
        srows.append(
            f'<tr data-lines="{e(lines)}"><td><code>{e(s.id)}</code><div class="small muted">{e(s.kind)}'
            f'{" · implicite" if s.implicit else ""}{" · " + e(s.scope) if s.scope != "global" else ""}</div></td>'
            f'<td>${e(s.statement)}$'
            f'{"<div class=small>justification : " + e(s.justification) + "</div>" if s.justification else ""}'
            f'{"<div class=small>dépend de : " + e(", ".join(s.depends_on)) + "</div>" if s.depends_on else ""}'
            f'{claim}{lean_code}{msgs}</td>'
            f'<td><span class="badge {cls}">{e(label)}</span><div class="small">{"<br>".join(extra)}</div></td>'
            f'<td>{fids}</td></tr>')
    steps_html = ('<section><h2>Étapes formalisées</h2><div class="sub">Cliquer une étape surligne les lignes de la copie. '
                  'Chaque étape est un théorème Lean dont les seules hypothèses sont ses dépendances déclarées.</div>'
                  '<table><tr><th>Étape</th><th>Affirmation (copie) et formalisation</th><th>Lean</th><th>Fidélité</th></tr>'
                  + "".join(srows) + "</table></section>")

    # Lean
    L = r.lean
    run = L.run
    ax = "".join(f"<div><code>{e(k)}</code></div><div class='small'>{e(', '.join(v) or 'aucun')}</div>"
                 for k, v in L.axioms.items() if k.endswith(("assemblage", "accord_enonce")))
    lean_html = ('<section><h2>Résultat Lean</h2><div class="kv">'
                 f'<div>Version</div><div>{e(run.lean_version if run else "—")}</div>'
                 f'<div>Isolation</div><div>{e(run.sandbox if run else "—")} · {run.seconds if run else 0:.1f} s'
                 f'{" · DÉLAI DÉPASSÉ" if run and run.timed_out else ""}</div>'
                 f'<div>Fragments refusés</div><div>{len(L.policy_violations)}</div>'
                 f'<div>Assemblage</div><div>{_yes(L.assembly_ok)}</div>'
                 f'<div>Accord avec l’énoncé</div><div>{_yes(L.statement_match)}</div>'
                 f'<div>Axiomes</div><div>{_yes(L.axioms_ok)} (autorisés : propext, Classical.choice, Quot.sound)</div>'
                 f'{ax}</div>'
                 + "".join(f"<div class='bad small'>{e(v.where)} : {e(v.token)} — {e(v.detail)}</div>" for v in L.policy_violations)
                 + (f"<details><summary>Fichier Lean complet</summary><pre><code>{e(_read(out_dir, 'Copie.lean'))}</code></pre></details>"
                    if out_dir else "")
                 + "</section>")

    cost_html = ""
    if r.costs is not None:
        c = r.costs
        rows = "".join(
            f"<tr><td>{e(x.stage)}</td><td><code>{e(x.engine)}</code></td><td>{x.input_tokens}</td>"
            f"<td>{x.output_tokens}</td><td>{'cache' if x.cached else ''}{(str(x.images) + ' image') if x.images else ''}</td>"
            f"<td>{'—' if x.cost_usd is None else f'{x.cost_usd:.4f} $'}</td></tr>" for x in c.entries)
        cost_html = ('<section><h2>Coût de la correction</h2><div class="kv">'
                     f'<div>Total</div><div><strong>{c.total_usd:.4f} $</strong>'
                     f'{"" if c.complete else " <span class=warn>(certains prix inconnus)</span>"}</div>'
                     f'<div>Appels aux modèles</div><div>{c.llm_calls} (+ {c.cached_calls} servis par le cache)</div>'
                     f'<div>Lean (local)</div><div>{c.lean_seconds:.1f} s de calcul, gratuit</div></div>'
                     + (('<table><tr><th>Étape</th><th>Moteur</th><th>Entrée</th><th>Sortie</th><th></th><th>Coût</th></tr>'
                         + rows + "</table>") if c.entries else
                        '<div class="sub">Aucun appel à un modèle pour cette copie.</div>')
                     + "</section>")
    others = [c for c in r.fidelity if c.kind in ("couverture", "structure") or c.step_id not in {s.id for s in r.structure.steps}]
    fid_html = ""
    if others:
        fid_html = ('<section><h2>Contrôles de fidélité (hors étapes)</h2><table><tr><th>Objet</th><th>Contrôle</th><th>Résultat</th></tr>'
                    + "".join(f"<tr data-lines='{e(c.step_id)}'><td><code>{e(c.step_id)}</code></td><td>{e(c.kind)}</td>"
                              f"<td><span class='badge {'ok' if c.ok else ('bad' if c.ok is False else 'muted')}'>"
                              f"{'ok' if c.ok else ('échec' if c.ok is False else 'n/a')}</span> {e(c.detail)}</td></tr>"
                              for c in others)
                    + "</table></section>")
    stmt = f'<div class="sub">Énoncé : {e(r.exercise.statement_latex)}</div>'
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Correction Lean On Steroids</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.css">
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/katex.min.js"></script>
<script defer src="https://cdnjs.cloudflare.com/ajax/libs/KaTeX/0.16.9/contrib/auto-render.min.js"></script>
<style>{CSS}</style></head><body>
<header><button class="toggle" id="theme">thème</button><h1>Correction — {e(r.exercise.exercise_id)}</h1>{stmt}
<div class="sub">Formalisation Lean de l’énoncé : <code>{e(r.exercise.lean_statement)}</code> — validée par : {e(r.exercise.validated_by or "NON VALIDÉE")}</div></header>
<main><div><div class="sticky">{''.join(photos)}</div></div>
<div>{verdict_html}{fb_html}{steps_html}{unc_html}{tr_html}{lean_html}{cost_html}{fid_html}</div></main>
<script>{JS}</script></body></html>"""


def _list(title: str, items: list[str]) -> str:
    if not items:
        return ""
    return f"<h3 class='small'>{e(title)}</h3><ul>" + "".join(f"<li>{e(x)}</li>" for x in items) + "</ul>"


def _yes(b: bool) -> str:
    return '<span class="badge ok">oui</span>' if b else '<span class="badge bad">non</span>'


def _read(out_dir: Path | None, name: str) -> str:
    if out_dir and (out_dir / name).exists():
        return (out_dir / name).read_text(encoding="utf-8")
    return ""


def dump_json(r: RunResult) -> str:
    return json.dumps(r.model_dump(mode="json"), ensure_ascii=False, indent=1)
