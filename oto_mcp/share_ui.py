"""UI web NAVIGABLE d'un projet partagé — face navigateur des sous-domaines par projet
(`<slug>.share.oto.cx`, ADR 0032). **Lecture seule**, rendue SERVER-SIDE (lisible par un
humain ET par un agent via WebFetch, contrairement à l'ex-partage chiffré SPA `/p/p`). Le
MCP (agir) reste au path `/mcp` ; ici on ne fait que CONSULTER.

C'est aussi le **canal de démonstration / acquisition** : la page doit « claquer » (hero
« brancher dans Claude », carte « Ajouter à mon Oto », connecteurs présentés avec logo +
description au survol + lien, tableaux confortables à explorer).

Quatre pages, toutes gatées par l'appartenance au projet (fail-closed) :
- `/`               index : brief + hero MCP + connecteurs + liens procédures / tableaux / docs
- `/procedures/<id>`  prose d'une procédure liée (markdown sûr)
- `/data/<id>`        lignes d'un tableau lié (table riche : recherche + tri + filtres), gaté `secret`
- `/docs/<id>`        page Documents (liée au projet ou de son arbre)

Auto-portée (tokens Otomata inline + Google Fonts), aucune dépendance front. Tout contenu
utilisateur est ÉCHAPPÉ ; la prose markdown est rendue en mode sûr (`html=False` → HTML brut
échappé, `javascript:` neutralisé), même posture que `public_doc_page`.

`build_page` fait les lectures DB (SYNC) → l'appeler dans un threadpool (le serveur est
mono-loop, cf. CLAUDE.md §PERF). Il rend `(html, status)`, ou `(None, 0)` quand le path n'est
PAS une route UI (→ le dispatch retombe sur le MCP : `/mcp`, `/.well-known/*`…).
"""
from __future__ import annotations

import html
import json
import os
import re
from typing import Optional

from markdown_it import MarkdownIt

from . import brand

# Rendu CommonMark SÛR (html=False = HTML brut échappé, pas exécuté), réutilisé (stateless).
_MD = MarkdownIt("commonmark", {"html": False})

# Plafond de lignes affichées par page de tableau (pagination par `?offset=`).
_DATA_PAGE = 100

# Deep-link « Ajouter à mon Oto » (le dashboard gère le login puis le fork/récupération).
# Env-driven (cutover ADR 0040 : prod dashboard ≠ preprod ; ne pas figer sur .oto.ninja).
_DASHBOARD = os.environ.get("OTO_DASHBOARD_URL", "https://dashboard.oto.ninja").rstrip("/")

# Favicon Otomata (mark canonique) — source unique dans `brand.py`.
_FAVICON_LINK = brand.FAVICON_LINK


# ── Shell HTML charté (mêmes tokens que public_doc_page) ──────────────────────
def _shell(*, title: str, inner: str, home_url: Optional[str] = None,
           wide: bool = False, extra_head: str = "", extra_body: str = "") -> str:
    safe_title = html.escape(title or "Projet")
    crumb = (f'<a class=back href="{html.escape(home_url)}">← Retour au projet</a>'
             if home_url else "")
    wrap_cls = "wrap wide" if wide else "wrap"
    return f"""<!DOCTYPE html>
<html lang=fr><head>
<meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>{safe_title} · Oto</title>
{_FAVICON_LINK}
<meta name=description content="Projet partagé via Oto.">
<meta name=robots content="noindex">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=Hanken+Grotesk:wght@400..700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  :root{{--bg:#fefcf5;--surface:#fff;--paper2:#f4ecd2;--paper3:#faf5e6;--ink:#2c2112;--ink-soft:#4a3a23;
    --mute:#6c5e44;--faint:#8a7b5c;--hair:#dccfa8;--hair-soft:#ede1bd;--primary:#f0b41e;
    --primary-soft:#fbe7a8;--primary-ink:#5a3b03;--accent:#2a87d8;--ink-deep:#241a0e;
    --shadow-card:0 1px 2px rgba(44,33,18,.04),0 8px 24px -12px rgba(44,33,18,.14)}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:'Hanken Grotesk',system-ui,sans-serif;
    line-height:1.6;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:820px;margin:0 auto;padding:44px 24px 40px}}
  .wrap.wide{{max-width:1180px}}
  .eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);font-weight:600}}
  h1{{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:34px;line-height:1.15;
    margin:8px 0 4px;letter-spacing:-.01em}}
  .back{{display:inline-block;margin-bottom:18px;color:var(--accent);text-decoration:none;font-size:13.5px}}
  .back:hover{{text-decoration:underline}}
  .lede{{color:var(--ink-soft);font-size:16px;margin:6px 0 8px}}
  .lede p{{margin:.5em 0}}
  .card{{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:22px 26px;
    margin:20px 0;box-shadow:var(--shadow-card)}}
  .card h2{{font-family:'Bricolage Grotesque',sans-serif;font-size:15px;margin:0 0 12px;font-weight:700;
    letter-spacing:.01em}}
  .card h2 .muted{{color:var(--mute);font-weight:600}}
  .nav{{list-style:none;margin:0;padding:0;display:grid;gap:8px}}
  .nav a{{display:flex;align-items:center;gap:10px;text-decoration:none;color:var(--ink);
    background:var(--paper3);border:1px solid var(--hair);border-radius:10px;padding:11px 14px;font-size:14.5px;
    transition:border-color .12s,transform .12s}}
  .nav a:hover{{border-color:var(--primary);transform:translateX(2px)}}
  .nav .k{{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--primary-ink);
    background:var(--primary-soft);border-radius:5px;padding:2px 7px;white-space:nowrap;text-transform:uppercase;letter-spacing:.03em}}
  .nav .arrow{{margin-left:auto;color:var(--faint)}}
  .empty{{color:var(--mute);font-size:14px}}
  article{{color:var(--ink-soft);font-size:15.5px}}
  article h1,article h2,article h3{{font-family:'Bricolage Grotesque',sans-serif;color:var(--ink)}}
  article h2{{font-size:22px;margin:1.4em 0 .4em}} article h3{{font-size:18px;margin:1.2em 0 .3em}}
  article p{{margin:.7em 0}} article a{{color:var(--accent)}}
  article ul,article ol{{margin:.6em 0;padding-left:22px}} article li{{margin:.25em 0}}
  article code{{font-family:'JetBrains Mono',monospace;font-size:.88em;background:var(--paper2);border-radius:5px;padding:1px 5px}}
  article pre{{background:var(--paper2);border:1px solid var(--hair);border-radius:10px;padding:14px 16px;overflow-x:auto}}
  article pre code{{background:none;padding:0}}
  article blockquote{{margin:.8em 0;padding:.2em 16px;border-left:3px solid var(--hair);color:var(--mute)}}

  /* ── Hero « brancher » (carte MCP URL + Ajouter à mon Oto) ── */
  .hero{{background:linear-gradient(135deg,#fff 0%,var(--paper3) 100%);border:1px solid var(--hair);
    border-radius:16px;padding:24px 26px;margin:22px 0;box-shadow:var(--shadow-card)}}
  .hero h2{{font-family:'Bricolage Grotesque',sans-serif;font-size:17px;margin:0 0 4px;font-weight:700}}
  .hero .sub{{color:var(--ink-soft);font-size:14px;margin:0 0 16px}}
  .url{{display:flex;gap:8px;align-items:center;background:var(--ink-deep);border-radius:11px;
    padding:11px 12px 11px 15px;font-family:'JetBrains Mono',monospace;font-size:13px;word-break:break-all}}
  .url code{{flex:1;color:#f6ecc9}}
  .url .badge{{color:#c7b48a;font-size:10.5px;text-transform:uppercase;letter-spacing:.08em;
    padding-right:4px;white-space:nowrap}}
  .btn{{font-family:inherit;font-size:12.5px;font-weight:700;cursor:pointer;border:1px solid var(--primary);
    background:var(--primary);color:var(--primary-ink);border-radius:9px;padding:8px 14px;white-space:nowrap;
    text-decoration:none;display:inline-flex;align-items:center;gap:6px;transition:filter .12s}}
  .btn:hover{{filter:brightness(1.05)}}
  .btn.ghost{{background:transparent;color:var(--ink);border-color:var(--hair)}}
  .btn.ghost:hover{{border-color:var(--primary);background:var(--paper3)}}
  .btn.copy{{background:#3a2c17;color:#f6ecc9;border-color:#3a2c17;padding:7px 11px}}
  .cta-row{{display:flex;gap:10px;align-items:center;margin-top:14px;flex-wrap:wrap}}
  .cta-row .hint{{color:var(--faint);font-size:12.5px}}

  /* ── Connecteurs (chips avec logo + tooltip + lien) ── */
  .conns{{display:flex;flex-wrap:wrap;gap:8px}}
  .conn{{position:relative;display:inline-flex;align-items:center;gap:8px;text-decoration:none;color:var(--ink);
    background:var(--paper3);border:1px solid var(--hair);border-radius:999px;padding:6px 13px 6px 7px;
    font-size:13.5px;font-weight:600;transition:border-color .12s,transform .12s}}
  .conn:hover{{border-color:var(--primary);transform:translateY(-1px)}}
  .conn .logo{{width:22px;height:22px;border-radius:6px;object-fit:contain;background:#fff;
    border:1px solid var(--hair-soft);flex:none}}
  .conn .mono{{width:22px;height:22px;border-radius:6px;flex:none;display:grid;place-items:center;
    font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:12px;color:var(--primary-ink);
    background:var(--primary-soft)}}
  .conn .n{{line-height:1}} .conn .cnt{{color:var(--faint);font-weight:500;font-size:12px}}
  /* tooltip pur CSS (title accessible en fallback) */
  .conn[data-tip]:hover::after{{content:attr(data-tip);position:absolute;left:0;top:calc(100% + 8px);
    z-index:20;width:max-content;max-width:280px;background:var(--ink-deep);color:#f6ecc9;
    font-size:12px;font-weight:400;line-height:1.45;padding:9px 12px;border-radius:9px;
    box-shadow:0 10px 30px -8px rgba(44,33,18,.4);white-space:normal;pointer-events:none}}
  .conn[data-tip]:hover::before{{content:"";position:absolute;left:16px;top:calc(100% + 2px);z-index:21;
    border:6px solid transparent;border-bottom-color:var(--ink-deep);pointer-events:none}}
  .toolchips{{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}}
  .toolchip{{font-family:'JetBrains Mono',monospace;font-size:11px;background:var(--paper2);
    color:var(--mute);border-radius:6px;padding:2px 8px}}

  /* ── Tableau riche (recherche + tri + filtres) ── */
  .dtoolbar{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0 14px}}
  .search{{flex:1;min-width:200px;display:flex;align-items:center;gap:8px;background:var(--surface);
    border:1px solid var(--hair);border-radius:10px;padding:8px 12px}}
  .search input{{flex:1;border:none;outline:none;background:none;font-family:inherit;font-size:14px;color:var(--ink)}}
  .search svg{{flex:none;color:var(--faint)}}
  .count{{color:var(--mute);font-size:13px;white-space:nowrap}}
  .tablewrap{{overflow-x:auto;border:1px solid var(--hair);border-radius:12px;box-shadow:var(--shadow-card);background:var(--surface)}}
  table{{border-collapse:separate;border-spacing:0;font-size:13.5px;width:100%}}
  th,td{{border-bottom:1px solid var(--hair-soft);padding:9px 14px;text-align:left;vertical-align:top;
    min-width:110px;max-width:420px}}
  td.rich{{min-width:210px}}
  thead th{{background:var(--paper2);position:sticky;top:0;z-index:2}}
  /* Cellule : hauteur BORNÉE (lignes uniformes) ; les cellules qui débordent sont
     fondues en bas (masque bg-agnostique posé par JS sur `.clamped`). */
  td .cell{{display:block;max-height:150px;overflow:hidden;overflow-wrap:anywhere}}
  td .cell.clamped{{-webkit-mask-image:linear-gradient(180deg,#000 72%,transparent);
    mask-image:linear-gradient(180deg,#000 72%,transparent)}}
  /* JSON lisible : dict → clé/valeur, liste d'objets → blocs, liste de scalaires → puces. */
  .kv{{display:grid;gap:3px}}
  .kvrow{{display:grid;grid-template-columns:auto 1fr;gap:9px;align-items:baseline}}
  .kvk{{font-family:'JetBrains Mono',monospace;font-size:10px;text-transform:uppercase;
    letter-spacing:.03em;color:var(--faint);white-space:nowrap}}
  .kvv{{color:var(--ink-soft);min-width:0;overflow-wrap:anywhere}}
  .kvv a{{color:var(--accent)}}
  .jlist{{display:flex;flex-direction:column;gap:9px}}
  .jitem{{padding-left:10px;border-left:2px solid var(--hair-soft)}}
  .chips{{display:flex;flex-wrap:wrap;gap:4px}}
  .chip{{background:var(--paper2);border-radius:6px;padding:1px 8px;font-size:12px;color:var(--ink-soft)}}
  th .thlabel{{display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;
    font-family:'JetBrains Mono',monospace;font-size:11px;text-transform:uppercase;letter-spacing:.04em;
    color:var(--mute);white-space:nowrap}}
  th .thlabel:hover{{color:var(--ink)}}
  th .sort{{color:var(--faint);font-size:10px}}
  th.asc .sort::after{{content:"▲";color:var(--primary-ink)}}
  th.desc .sort::after{{content:"▼";color:var(--primary-ink)}}
  th .sort::after{{content:"↕"}}
  th input.colf{{margin-top:6px;width:100%;border:1px solid var(--hair);border-radius:6px;padding:4px 7px;
    font-family:inherit;font-size:12px;background:var(--surface);color:var(--ink)}}
  th input.colf::placeholder{{color:var(--faint)}}
  tbody tr:nth-child(even) td{{background:var(--paper3)}}
  tbody tr:hover td{{background:var(--primary-soft)}}
  tbody tr:last-child td{{border-bottom:none}}
  .norows td{{color:var(--mute);text-align:center;padding:22px}}
  .pager{{display:flex;justify-content:space-between;align-items:center;margin-top:14px;font-size:13.5px}}
  .pager a{{color:var(--accent);text-decoration:none;font-weight:600}} .pager a:hover{{text-decoration:underline}}
  .pager span{{color:var(--mute)}}

  footer{{margin-top:34px;padding-top:16px;border-top:1px solid var(--hair);font-size:12.5px;color:var(--mute)}}
  footer a{{color:var(--accent);text-decoration:none}}
  @media (max-width:560px){{h1{{font-size:28px}} .wrap{{padding:32px 16px 32px}}}}
{extra_head}</style></head>
<body><div class="{wrap_cls}">
{crumb}
{inner}
  <footer>Partagé via <a href="https://oto.cx">Oto</a> — la boîte à outils d'automatisation pour agents IA.</footer>
</div>{extra_body}</body></html>"""


def _nav_section(title: str, items: list[dict]) -> str:
    """Une carte « section » avec une liste de liens navigables (ou rien si vide)."""
    if not items:
        return ""
    lis = "".join(
        f'<a href="{html.escape(it["href"])}">'
        f'<span class=k>{html.escape(it["kind"])}</span>'
        f'<span class=n>{html.escape(it["label"])}</span>'
        f'<span class=arrow>→</span></a>'
        for it in items
    )
    return f'<div class=card><h2>{html.escape(title)}</h2><div class=nav>{lis}</div></div>'


def _hero_connect(connect_url: str, add_url: Optional[str]) -> str:
    """Hero « brancher dans Claude/Mistral » : l'URL MCP publique + copie + CTA
    « Ajouter à mon Oto » (deep-link dashboard — login géré côté dashboard)."""
    if not connect_url:
        return ""
    url = html.escape(connect_url)
    add_btn = (
        f'<a class="btn" href="{html.escape(add_url)}" target="_blank" rel="noopener">'
        '<span>＋</span> Ajouter à mon Oto</a>' if add_url else "")
    hint = ('<span class=hint>déjà client Oto ? récupère ce projet dans ton espace</span>'
            if add_url else "")
    return (
        '<div class=hero>'
        '<h2>Brancher ce projet dans Claude ou Mistral</h2>'
        '<p class=sub>Colle cette URL comme connecteur MCP — tu obtiens les outils de '
        'ce projet, prêts à l\'emploi.</p>'
        '<div class=url><span class=badge>MCP</span>'
        f'<code id=u>{url}</code>'
        '<button class="btn copy" onclick="navigator.clipboard.writeText('
        'document.getElementById(\'u\').textContent).then(()=>{this.textContent=\'copié ✓\'})">'
        'copier</button></div>'
        f'<div class=cta-row>{add_btn}{hint}</div>'
        '</div>')


def _connectors_card(connectors: list[dict]) -> str:
    """Carte « connecteurs » : une pastille par connecteur (logo/monogramme + nom +
    nb d'outils), tooltip = description, clic = fiche marketplace du dashboard."""
    if not connectors:
        return ""
    pills = []
    for c in connectors:
        logo = (f'<img class=logo src="{html.escape(c["logo"])}" alt="" loading=lazy '
                'onerror="this.style.display=\'none\'">' if c.get("logo")
                else f'<span class=mono>{html.escape(c["mono"])}</span>')
        cnt = (f'<span class=cnt>· {c["tool_count"]}</span>'
               if c.get("tool_count") else "")
        tip = html.escape(c.get("description") or "")
        pills.append(
            f'<a class=conn href="{html.escape(c["href"])}" target="_blank" rel="noopener" '
            f'data-tip="{tip}" title="{tip}">'
            f'{logo}<span class=n>{html.escape(c["label"])}</span>{cnt}</a>')
    return ('<div class=card><h2>Connecteurs <span class=muted>· ce que ce projet '
            'sait faire</span></h2>'
            f'<div class=conns>{"".join(pills)}</div></div>')


# ── Rendus de page ────────────────────────────────────────────────────────────
def render_index(*, name: str, brief_md: str, procedures: list[dict], tables: list[dict],
                 docs: list[dict], connect_url: str, connectors: Optional[list[dict]] = None,
                 add_url: Optional[str] = None, loose_tools: Optional[list[str]] = None) -> str:
    brief_html = (f'<div class=card><article>{_MD.render(brief_md)}</article></div>'
                  if (brief_md or "").strip()
                  else '<p class="empty">Projet partagé, en lecture seule.</p>')
    sections = (
        _nav_section("Procédures", [
            {"href": f"/procedures/{p['id']}", "kind": "procédure", "label": p["label"]}
            for p in procedures])
        + _nav_section("Tableaux", [
            {"href": f"/data/{t['id']}", "kind": "tableau", "label": t["label"]}
            for t in tables])
        + _nav_section("Documents", [
            {"href": f"/docs/{d['id']}", "kind": "doc", "label": d["label"]}
            for d in docs])
    )
    if not sections:
        sections = ('<div class=card><p class="empty">Ce projet n\'expose encore aucune '
                    'procédure, tableau ni document.</p></div>')
    conns = _connectors_card(connectors or [])
    # Outils sans connecteur reconnu (open-data/maison) : chips discrètes, sans lien.
    loose = ""
    if loose_tools:
        chips = "".join(f"<span class=toolchip>{html.escape(t)}</span>" for t in loose_tools)
        loose = (f'<div class=card><h2>Autres outils</h2><div class=toolchips>{chips}</div></div>')
    inner = (f'  <div class=eyebrow>Projet partagé · Oto</div>\n'
             f'  <h1>{html.escape(name or "Projet")}</h1>\n'
             f'  {brief_html}\n'
             f'  {_hero_connect(connect_url, add_url)}\n'
             f'  {conns}\n'
             f'  {sections}\n  {loose}')
    return _shell(title=name, inner=inner)


def render_prose(*, name: str, title: str, body_md: str, kind_label: str) -> str:
    body_html = _MD.render(body_md or "")
    inner = (f'  <div class=eyebrow>{html.escape(kind_label)} · {html.escape(name or "Projet")}</div>\n'
             f'  <h1>{html.escape(title or kind_label)}</h1>\n'
             f'  <div class=card><article>{body_html}</article></div>')
    return _shell(title=title, inner=inner, home_url="/")


def render_data(*, name: str, namespace: str, columns: list[str], rows: list[dict],
                total: int, offset: int) -> str:
    if columns and rows:
        head_cells = []
        for i, c in enumerate(columns):
            head_cells.append(
                f'<th data-col="{i}"><div class=thlabel onclick="otoSort({i})">'
                f'<span>{html.escape(c)}</span><span class=sort></span></div>'
                f'<input class=colf placeholder="filtrer…" oninput="otoFilter()" data-col="{i}"></th>')
        head = "".join(head_cells)
        body_rows = []
        for r in rows:
            data = r.get("data") or {}
            cells = "".join(_cell_td(data.get(c)) for c in columns)
            body_rows.append(f"<tr>{cells}</tr>")
        table = (
            '<div class=dtoolbar>'
            '<label class=search>'
            '<svg width=16 height=16 viewBox="0 0 24 24" fill=none stroke=currentColor stroke-width=2>'
            '<circle cx=11 cy=11 r=8></circle><path d="m21 21-4.3-4.3"></path></svg>'
            '<input id=q placeholder="Rechercher dans le tableau…" oninput="otoFilter()"></label>'
            f'<span class=count id=cnt>{len(rows)} lignes</span></div>'
            f'<div class=tablewrap><table id=dt><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body_rows)}</tbody></table></div>')
        script = _DATA_SCRIPT
    else:
        table = '<div class=card><p class="empty">Ce tableau est vide.</p></div>'
        script = ""
    start = offset + 1 if rows else 0
    end = offset + len(rows)
    pager_bits = [f"<span>{start}–{end} sur {total}</span>"]
    if offset > 0:
        pager_bits.insert(0, f'<a href="?offset={max(0, offset - _DATA_PAGE)}">← précédent</a>')
    if end < total:
        pager_bits.append(f'<a href="?offset={offset + _DATA_PAGE}">suivant →</a>')
    pager = f'<div class=pager>{"".join(pager_bits)}</div>' if total else ""
    inner = (f'  <div class=eyebrow>Tableau · {html.escape(name or "Projet")}</div>\n'
             f'  <h1>{html.escape(namespace)}</h1>\n  {table}\n  {pager}')
    return _shell(title=namespace, inner=inner, home_url="/", wide=True, extra_body=script)


def render_not_found(*, name: str = "") -> str:
    inner = ('  <div class=eyebrow>Oto</div>\n  <h1>Introuvable</h1>\n'
             '  <div class=card><p class="empty">Cette page n\'existe pas ou n\'est plus '
             'partagée dans ce projet.</p></div>')
    return _shell(title="Introuvable", inner=inner, home_url="/")


# JS de tableau : recherche globale + filtres par colonne + tri 3 états. Opère sur le
# DOM déjà rendu (aucune donnée dupliquée) ; pas de dépendance externe. Le compteur
# reflète les lignes visibles / total de la page.
_DATA_SCRIPT = """<script>
(function(){
  var tb=document.querySelector('#dt tbody');
  if(!tb)return;
  // Fondu bas des cellules dont le contenu déborde la hauteur bornée (masque CSS).
  [].forEach.call(document.querySelectorAll('#dt .cell'),function(c){
    if(c.scrollHeight-c.clientHeight>2)c.classList.add('clamped');
  });
  var rows=[].slice.call(tb.rows);
  var cnt=document.getElementById('cnt');
  var sortCol=-1, sortDir=0;
  window.otoFilter=function(){
    var q=(document.getElementById('q').value||'').toLowerCase();
    var cf=[].slice.call(document.querySelectorAll('input.colf'));
    var vis=0;
    rows.forEach(function(tr){
      var cells=tr.cells, ok=true;
      if(q){ ok=[].slice.call(cells).some(function(td){return td.textContent.toLowerCase().indexOf(q)>-1;}); }
      if(ok){ for(var i=0;i<cf.length;i++){ var v=(cf[i].value||'').toLowerCase(); if(v){ var td=cells[+cf[i].dataset.col];
        if(!td||td.textContent.toLowerCase().indexOf(v)<0){ok=false;break;} } } }
      tr.style.display=ok?'':'none'; if(ok)vis++;
    });
    if(cnt)cnt.textContent=vis+(vis===rows.length?' lignes':' / '+rows.length+' lignes');
  };
  window.otoSort=function(col){
    var ths=document.querySelectorAll('#dt thead th');
    if(sortCol===col){ sortDir=sortDir===1?-1:(sortDir===-1?0:1); } else { sortCol=col; sortDir=1; }
    ths.forEach(function(th){th.classList.remove('asc','desc');});
    var num=function(s){var n=parseFloat(String(s).replace(/[^0-9.\\-]/g,''));return isNaN(n)?null:n;};
    if(sortDir===0){ sortCol=-1; rows.forEach(function(tr){tb.appendChild(tr);}); return; }
    ths[col].classList.add(sortDir===1?'asc':'desc');
    var sorted=rows.slice().sort(function(a,b){
      var x=a.cells[col].textContent.trim(), y=b.cells[col].textContent.trim();
      var nx=num(x), ny=num(y), r;
      if(nx!==null&&ny!==null){ r=nx-ny; } else { r=x.localeCompare(y,'fr',{numeric:true}); }
      return sortDir===1?r:-r;
    });
    sorted.forEach(function(tr){tb.appendChild(tr);});
  };
})();
</script>"""


def _cell(v: object) -> str:
    """Représentation TEXTE d'une valeur de cellule (dict/list → JSON compact court).
    Sert au `title` de survol (contenu complet) et à la recherche/tri côté DOM."""
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
        return s if len(s) <= 300 else s[:297] + "…"
    return str(v)


_URL_RE = re.compile(r"^https?://\S+$", re.I)


def _cell_td(v: object) -> str:
    """Une cellule `<td>` : contenu rendu (structuré pour le JSON), enveloppé dans un
    `.cell` à hauteur bornée. `title` = valeur texte complète (survol). Les valeurs
    dict/list obtiennent la classe `rich` (colonne un peu plus large)."""
    inner = _cell_html(v)
    rich = isinstance(v, (dict, list)) and v not in (None, "", [], {})
    full = _cell(v)
    title = f' title="{html.escape(full)}"' if (rich or len(full) > 60) else ""
    cls = "cell rich-cell" if rich else "cell"
    td_cls = ' class="rich"' if rich else ""
    return f'<td{td_cls}><div class="{cls}"{title}>{inner}</div></td>'


def _cell_html(v: object) -> str:
    """Rendu HTML d'une valeur : scalaire/URL en texte, dict en clé/valeur, liste de
    scalaires en puces, liste d'objets en blocs empilés. Tout est échappé."""
    if v is None or v == "":
        return ""
    if isinstance(v, dict):
        return _kv_html(v)
    if isinstance(v, list):
        if not v:
            return ""
        if all(not isinstance(x, (dict, list)) for x in v):
            chips = "".join(f"<span class=chip>{html.escape(_short(x, 48))}</span>"
                            for x in v if x not in (None, ""))
            return f"<div class=chips>{chips}</div>" if chips else ""
        blocks = "".join(
            f"<div class=jitem>{_kv_html(x) if isinstance(x, dict) else _scalar_html(x)}</div>"
            for x in v)
        return f"<div class=jlist>{blocks}</div>"
    return _scalar_html(v)


def _scalar_html(v: object) -> str:
    """Scalaire → texte échappé ; une URL http(s) → lien (label tronqué)."""
    s = "" if v is None else str(v)
    if not s:
        return ""
    if _URL_RE.match(s):
        label = s if len(s) <= 44 else s[:41] + "…"
        return (f'<a href="{html.escape(s)}" target="_blank" rel="noopener nofollow">'
                f'{html.escape(label)}</a>')
    return html.escape(s)


def _kv_html(d: dict) -> str:
    """dict → liste clé/valeur lisible (remplace le JSON brut char-wrappé)."""
    rows = []
    for k, val in d.items():
        if val in (None, "", [], {}):
            continue
        rows.append(f"<div class=kvrow><span class=kvk>{html.escape(str(k))}</span>"
                    f"<span class=kvv>{_flat_val(val)}</span></div>")
    return f"<div class=kv>{''.join(rows)}</div>" if rows else ""


def _flat_val(v: object) -> str:
    """Valeur imbriquée dans une ligne KV : dict/list aplatis sur une ligne courte."""
    if isinstance(v, dict):
        parts = [f"{html.escape(str(k))}: {html.escape(_short(vv))}"
                 for k, vv in v.items() if vv not in (None, "")]
        return " · ".join(parts)
    if isinstance(v, list):
        if all(not isinstance(x, (dict, list)) for x in v):
            return html.escape(", ".join(_short(x) for x in v if x not in (None, "")))
        return f"{len(v)} éléments"
    return _scalar_html(v)


def _short(v: object, n: int = 80) -> str:
    s = "" if v is None else str(v)
    return s if len(s) <= n else s[: n - 1] + "…"


# ── Connecteurs dérivés des tools exposés (tooltip + logo + lien marketplace) ──
def _connectors_from_tools(tools: list[str]) -> tuple[list[dict], list[str]]:
    """Groupe les tools par connecteur (namespace) et enrichit chacun (logo, description,
    lien fiche). Retourne `(connectors, loose_tools)` : `loose_tools` = les tools dont le
    namespace n'est pas un connecteur reconnu (open-data/maison sans fiche). Défensif —
    tout import/lookup échouant retombe sur une présentation nue."""
    if not tools:
        return [], []
    try:
        from . import providers
        from .tool_visibility import namespace_of
    except Exception:  # noqa: BLE001
        return [], list(tools)

    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for t in tools:
        try:
            ns = namespace_of(t)
        except Exception:  # noqa: BLE001
            ns = ""
        if ns not in groups:
            groups[ns] = []
            order.append(ns)
        groups[ns].append(t)

    connectors: list[dict] = []
    loose: list[str] = []
    for ns in order:
        con = None
        try:
            con = providers.connector_for_namespace(ns)
        except Exception:  # noqa: BLE001
            con = None
        if con is None:
            loose.extend(groups[ns])
            continue
        try:
            logo = con.logo_url_for()
        except Exception:  # noqa: BLE001
            logo = None
        label = getattr(con, "name", ns) or ns
        connectors.append({
            "name": con.name,
            "label": label,
            "mono": (label[:1] or "•").upper(),
            "logo": logo,
            "description": (getattr(con, "description", "") or "").strip(),
            "tool_count": len(groups[ns]),
            "href": f"{_DASHBOARD}/connectors?tab=marketplace&connector={con.name}",
        })
    return connectors, loose


def _tableau_entries(project: dict, links: list) -> list[dict]:
    """Tableaux liés au projet, résolus en `{id, label}` — accepte un lien référencé par
    ID numérique OU par NOM (liens legacy d'avant la normalisation nom→id, même contrat
    que `tools.datastore._anon_project_tableau_ns_ids`). Un lien par nom est résolu contre
    le datastore de l'org/user propriétaire du projet ; namespace introuvable ⇒ lien ignoré
    (jamais de 404 dur sur l'index). Sans ça la page web ne montrait QUE les liens par id."""
    from . import db
    owner_type = str(project.get("owner_type") or "org")
    owner_id = str(project.get("owner_id") or "")
    out: list[dict] = []
    for l in links:
        if l.get("target_type") != "tableau":
            continue
        ref = str(l.get("target_ref") or "").strip()
        if not ref:
            continue
        if ref.isdigit():
            out.append({"id": int(ref),
                        "label": l.get("label") or l.get("namespace") or f"#{ref}"})
        elif owner_id:
            ns = db.get_datastore_namespace(owner_type, owner_id, ref)
            if ns:
                out.append({"id": int(ns["id"]),
                            "label": l.get("label") or l.get("namespace") or ref})
    return out


# ── Routeur (lectures DB SYNC → appeler en threadpool) ────────────────────────
def build_page(project: dict, path: str, *, offset: int = 0,
               connect_url: str = "") -> tuple[Optional[str], int]:
    """Rend la page UI pour ce (projet, path), ou `(None, 0)` si le path n'est PAS une
    route UI (le dispatch retombe alors sur le MCP). Fail-closed : une entité non liée au
    projet → 404, jamais une lecture hors périmètre. Les TABLEAUX (datastore, lecture seule)
    ne sont navigables que sur un partage `secret` (mode « partage de projet ») ; le flag
    `mcp_expose_datastore`, lui, gate les OUTILS `data_*` MCP, pas cette vue humaine."""
    from . import db, org_store

    pid = int(project["id"])
    p = (path or "/").rstrip("/") or "/"
    # Tableaux navigables (lecture seule) uniquement sur un partage `secret` : l'owner a
    # lié le tableau ET publié le projet en partage → consentement explicite. `anonymous`
    # (endpoint-outil listé publiquement) ne montre pas les lignes du datastore.
    show_data = (project.get("mcp_access") == "secret")

    if p == "/":
        links = db.list_project_links(pid)
        procedures = [
            {"id": int(l["target_ref"]), "label": l.get("label") or l.get("title") or f"#{l['target_ref']}"}
            for l in links
            if l.get("target_type") == "procedure" and str(l.get("target_ref", "")).isdigit()]
        tables = (_tableau_entries(project, links) if show_data else [])
        # Docs : les pages de l'arbre du projet. (Le lien `doc` — pointeur manuel vers
        # une page d'un autre projet — a été retiré, lot 3 chantier 0.4.)
        docs = [{"id": int(d["id"]), "label": d.get("title") or f"#{d['id']}"}
                for d in db.list_docs_for_project(pid)]
        connectors, loose = _connectors_from_tools(list(project.get("mcp_tools") or []))
        # « Ajouter à mon Oto » : deep-link dashboard (login + fork/récupération gérés là-bas).
        slug = project.get("mcp_slug")
        add_url = f"{_DASHBOARD}/import?slug={slug}" if slug else None
        return render_index(
            name=project.get("name") or "", brief_md=project.get("brief_md") or "",
            procedures=procedures, tables=tables, docs=docs, connect_url=connect_url,
            connectors=connectors, add_url=add_url, loose_tools=loose), 200

    parts = p.strip("/").split("/")
    if len(parts) == 2 and parts[1].isdigit():
        section, rid = parts[0], int(parts[1])
        links = db.list_project_links(pid)

        if section == "procedures":
            allowed = {int(l["target_ref"]) for l in links
                       if l.get("target_type") == "procedure" and str(l.get("target_ref", "")).isdigit()}
            instr = org_store.get_instruction_by_id(rid) if rid in allowed else None
            if not instr:
                return render_not_found(), 404
            return render_prose(name=project.get("name") or "", title=instr.get("title") or "",
                                body_md=instr.get("body_md") or "", kind_label="Procédure"), 200

        if section == "data":
            allowed = {t["id"] for t in _tableau_entries(project, links)}
            ns = db.get_datastore_namespace_by_id(rid) if (show_data and rid in allowed) else None
            if not ns:
                return render_not_found(), 404
            total = db.datastore_count_rows(rid)
            rows = db.datastore_list_rows(rid, offset=max(0, offset), limit=_DATA_PAGE)
            columns = _derive_columns(ns.get("schema"), rows)
            return render_data(name=project.get("name") or "", namespace=ns.get("namespace") or "tableau",
                               columns=columns, rows=rows, total=total, offset=max(0, offset)), 200

        if section == "docs":
            doc = db.get_doc_by_id(rid)
            # Autorisé si le doc appartient à CE projet (héritage d'accès). Le lien
            # `doc` cross-projet a été retiré (lot 3 chantier 0.4).
            if not doc or int(doc.get("project_id") or 0) != pid:
                return render_not_found(), 404
            return render_prose(name=project.get("name") or "", title=doc.get("title") or "",
                                body_md=doc.get("body_md") or "", kind_label="Document"), 200

        return render_not_found(), 404

    return None, 0


def _derive_columns(schema: object, rows: list[dict]) -> list[str]:
    """Colonnes du tableau : celles du schéma typé si présent, sinon l'union des clés des
    rows (ordre de première apparition). Défensif — le schéma peut manquer/varier."""
    if isinstance(schema, dict):
        fields = schema.get("fields") or schema.get("columns")
        if isinstance(fields, list) and fields:
            cols = [f.get("name") if isinstance(f, dict) else f for f in fields]
            cols = [c for c in cols if isinstance(c, str) and c]
            if cols:
                return cols
    cols: list[str] = []
    for r in rows:
        for k in (r.get("data") or {}).keys():
            if k not in cols:
                cols.append(k)
    return cols
