"""Page de partage PUBLIQUE d'un doc — `dashboard.oto.ninja/p/d/<token>`, rendue
SERVER-SIDE (otomata-private, gap « pages de partage non lisibles par un agent »).

Le viewer historique était une route SPA (Vue) : un `WebFetch` sans exécution JS n'y
voyait qu'un shell vide → **illisible par un agent**. On ne peut pas distinguer un agent
d'un navigateur par l'entête `Accept` (les deux demandent `text/html`), donc la seule voie
robuste = servir une page qui **contient** déjà le contenu, pour tout le monde — même patron
que `share_ui` pour les projets partagés.

Auto-portée (tokens Otomata inline + Google Fonts), aucune dépendance front. Le markdown est
rendu avec `markdown-it-py` en **mode sûr** (`html=False` → le HTML brut du doc est échappé,
les liens `javascript:` neutralisés) : la page sort sur l'origine `dashboard.oto.ninja`, la
même que le localStorage Logto, donc un doc hostile ne doit jamais injecter de script."""
from __future__ import annotations

import html

from markdown_it import MarkdownIt

# Rendu CommonMark SÛR (html=False = HTML brut échappé, pas exécuté), réutilisé (stateless).
_MD = MarkdownIt("commonmark", {"html": False})


def _shell(*, title: str, inner: str) -> str:
    safe_title = html.escape(title or "Document")
    return f"""<!DOCTYPE html>
<html lang=fr><head>
<meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>{safe_title} · Oto</title>
<meta name=description content="Document partagé via Oto.">
<meta name=robots content="noindex">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=Hanken+Grotesk:wght@400..700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  :root{{--bg:#fefcf5;--surface:#fff;--paper2:#f4ecd2;--ink:#2c2112;--ink-soft:#4a3a23;
    --mute:#6c5e44;--hair:#dccfa8;--primary:#f0b41e;--accent:#2a87d8}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:'Hanken Grotesk',system-ui,sans-serif;
    line-height:1.6;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:760px;margin:0 auto;padding:48px 24px 40px}}
  .eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);font-weight:600}}
  h1{{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:34px;line-height:1.15;
    margin:8px 0 4px;letter-spacing:-.01em}}
  .meta{{color:var(--mute);font-size:13px;margin-bottom:8px}}
  .card{{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:32px 36px;margin:22px 0}}
  article{{color:var(--ink-soft);font-size:15.5px}}
  article h2{{font-family:'Bricolage Grotesque',sans-serif;font-size:22px;margin:1.4em 0 .4em;color:var(--ink)}}
  article h3{{font-family:'Bricolage Grotesque',sans-serif;font-size:18px;margin:1.2em 0 .3em;color:var(--ink)}}
  article p{{margin:.7em 0}}
  article a{{color:var(--accent)}}
  article ul,article ol{{margin:.6em 0;padding-left:22px}}
  article li{{margin:.25em 0}}
  article code{{font-family:'JetBrains Mono',monospace;font-size:.88em;background:var(--paper2);
    border-radius:5px;padding:1px 5px}}
  article pre{{background:var(--paper2);border:1px solid var(--hair);border-radius:10px;padding:14px 16px;
    overflow-x:auto}}
  article pre code{{background:none;padding:0}}
  article blockquote{{margin:.8em 0;padding:.2em 16px;border-left:3px solid var(--hair);color:var(--mute)}}
  article table{{border-collapse:collapse;margin:.8em 0;font-size:14px;display:block;overflow-x:auto}}
  article th,article td{{border:1px solid var(--hair);padding:6px 10px;text-align:left}}
  article img{{max-width:100%}}
  footer{{margin-top:28px;padding-top:16px;border-top:1px solid var(--hair);font-size:12.5px;color:var(--mute)}}
  footer a{{color:var(--accent);text-decoration:none}}
</style></head>
<body><div class=wrap>
{inner}
  <footer>Partagé via <a href="https://oto.ninja">Oto</a> — la boîte à outils d'automatisation.</footer>
</div></body></html>"""


def render(*, title: str, body_md: str, updated_at: object = None) -> str:
    body_html = _MD.render(body_md or "")
    meta = (f'<div class=meta>Mis à jour le {html.escape(str(updated_at)[:10])}</div>'
            if updated_at else "")
    inner = (f'  <div class=eyebrow>Document · Oto</div>\n'
             f'  <h1>{html.escape(title or "Document")}</h1>\n'
             f'  {meta}\n'
             f'  <div class=card><article>{body_html}</article></div>')
    return _shell(title=title, inner=inner)


def render_missing() -> str:
    inner = ('  <div class=eyebrow>Oto</div>\n'
             '  <h1>Document introuvable</h1>\n'
             '  <div class=card><article><p>Ce document n\'existe pas ou n\'est plus '
             'partagé.</p></article></div>')
    return _shell(title="Document introuvable", inner=inner)
