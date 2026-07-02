"""Landing HTML publique d'un endpoint MCP anonyme (ADR 0032).

La MÊME URL `<slug>.mcp.oto.cx` sert deux publics : un **navigateur** (GET text/html)
reçoit cette page de présentation (nom, pitch, outils, « brancher dans Claude ») ;
**Claude/Mistral** (POST / Accept event-stream) reçoivent le serveur MCP. Zéro URL en
plus, découvrable + partageable. Auto-portée (tokens Otomata inline + Google Fonts),
aucune dépendance front. Contenu échappé (le brief est du contenu utilisateur)."""
from __future__ import annotations

import html


def render(*, name: str, brief_md: str, tools: list[str], connect_url: str) -> str:
    safe_name = html.escape(name or "Endpoint MCP")
    # Brief léger : paragraphes (double saut) → <p>, sauts simples → <br>. Échappé.
    paras = [p.strip() for p in (brief_md or "").split("\n\n") if p.strip()]
    brief_html = "".join(
        f"<p>{html.escape(p).replace(chr(10), '<br>')}</p>" for p in paras[:4]
    ) or '<p class="mute">Un outil Oto, utilisable sans compte.</p>'
    chips = "".join(f"<span class=chip>{html.escape(t)}</span>" for t in tools)
    url = html.escape(connect_url)
    return f"""<!DOCTYPE html>
<html lang=fr><head>
<meta charset=utf-8><meta name=viewport content="width=device-width, initial-scale=1">
<title>{safe_name} · Oto</title>
<meta name=description content="Outil Oto brachable dans Claude, sans compte.">
<link rel=preconnect href="https://fonts.googleapis.com">
<link rel=preconnect href="https://fonts.gstatic.com" crossorigin>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400..800&family=Hanken+Grotesk:wght@400..700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
  :root{{--bg:#fefcf5;--surface:#fff;--paper2:#f4ecd2;--ink:#2c2112;--ink-soft:#4a3a23;
    --mute:#6c5e44;--hair:#dccfa8;--primary:#f0b41e;--primary-soft:#fbe7a8;--primary-ink:#5a3b03;--accent:#2a87d8}}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--bg);color:var(--ink);font-family:'Hanken Grotesk',system-ui,sans-serif;
    line-height:1.55;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:640px;margin:0 auto;padding:56px 24px 40px}}
  .eyebrow{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;color:var(--mute);font-weight:600}}
  h1{{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:38px;line-height:1.1;
    margin:10px 0 6px;letter-spacing:-.01em}}
  .lede{{color:var(--ink-soft);font-size:16px}}
  .lede p{{margin:.5em 0}}
  .card{{background:var(--surface);border:1px solid var(--hair);border-radius:14px;padding:22px;margin:26px 0}}
  .card h2{{font-family:'Bricolage Grotesque',sans-serif;font-size:15px;margin:0 0 10px;font-weight:700}}
  .url{{display:flex;gap:8px;align-items:center;background:var(--paper2);border:1px solid var(--hair);
    border-radius:9px;padding:10px 12px;font-family:'JetBrains Mono',monospace;font-size:13px;word-break:break-all}}
  .url code{{flex:1;color:var(--ink)}}
  button{{font-family:inherit;font-size:12.5px;font-weight:600;cursor:pointer;border:1px solid var(--hair);
    background:var(--surface);color:var(--ink);border-radius:8px;padding:7px 12px}}
  button:hover{{background:var(--paper2)}}
  .cta{{background:var(--primary);border-color:var(--primary);color:var(--primary-ink)}}
  .cta:hover{{filter:brightness(.97)}}
  ol{{margin:14px 0 0;padding-left:20px;color:var(--ink-soft);font-size:14px}}
  ol li{{margin:5px 0}}
  .chips{{display:flex;flex-wrap:wrap;gap:6px}}
  .chip{{font-family:'JetBrains Mono',monospace;font-size:11.5px;background:var(--primary-soft);
    color:var(--primary-ink);border-radius:6px;padding:3px 8px}}
  .mute{{color:var(--mute)}}
  footer{{margin-top:34px;padding-top:16px;border-top:1px solid var(--hair);font-size:12.5px;color:var(--mute)}}
  footer a{{color:var(--accent);text-decoration:none}}
</style></head>
<body><div class=wrap>
  <div class=eyebrow>Outil Oto · sans compte</div>
  <h1>{safe_name}</h1>
  <div class=lede>{brief_html}</div>

  <div class=card>
    <h2>Brancher dans Claude ou Mistral</h2>
    <div class=url><code id=u>{url}</code><button class=cta onclick="navigator.clipboard.writeText(document.getElementById('u').textContent).then(()=>{{this.textContent='copié ✓'}})">copier l'URL</button></div>
    <ol>
      <li>Dans Claude : <strong>Paramètres → Connecteurs → Ajouter un connecteur personnalisé</strong>.</li>
      <li>Colle l'URL ci-dessus. <strong>Aucun compte à créer</strong> — la connexion est instantanée.</li>
      <li>Les outils ci-dessous deviennent disponibles dans tes conversations.</li>
    </ol>
  </div>

  {f'<div class=card><h2>Outils exposés</h2><div class=chips>{chips}</div></div>' if chips else ''}

  <footer>Propulsé par <a href="https://oto.ninja">Oto</a> — la boîte à outils d'automatisation.</footer>
</div></body></html>"""
