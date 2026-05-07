"""Page `/settings` : permet à l'utilisateur de coller son cookie LinkedIn.

Auth : `current_user_sub_from_request()` (sera câblée à Logto par un autre
Claude). Pour l'instant lit `X-Oto-User-Sub` ou env `OTO_MCP_DEV_SUB`.
"""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from . import db
from .auth_hooks import current_user_sub_from_request


_PAGE = """\
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>oto MCP — Réglages</title>
  <style>
    :root {{ color-scheme: light dark; }}
    html, body {{ margin: 0; min-height: 100%; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: Canvas; color: CanvasText;
      display: grid; place-items: start center; padding: 60px 16px;
    }}
    main {{ width: 100%; max-width: 640px; }}
    h1 {{ font-size: 22px; margin: 0 0 6px; font-weight: 600; }}
    p.lede {{ margin: 0 0 28px; font-size: 14px; opacity: .7; }}
    section {{
      padding: 22px 24px; margin-bottom: 24px;
      border: 1px solid color-mix(in srgb, CanvasText 14%, transparent);
      border-radius: 10px;
    }}
    section h2 {{ font-size: 15px; margin: 0 0 6px; font-weight: 600; }}
    section .meta {{ font-size: 12px; opacity: .65; margin-bottom: 14px; }}
    label {{ font-size: 13px; opacity: .8; display: block; margin-bottom: 6px; }}
    textarea {{
      width: 100%; min-height: 80px; padding: 10px 12px;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
      border: 1px solid color-mix(in srgb, CanvasText 22%, transparent);
      border-radius: 6px; background: Canvas; color: CanvasText;
      box-sizing: border-box; resize: vertical;
    }}
    .row {{ display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }}
    button {{
      padding: 8px 14px; font-size: 14px; font-weight: 500;
      border: 0; border-radius: 6px; cursor: pointer;
      background: color-mix(in srgb, CanvasText 88%, Canvas);
      color: color-mix(in srgb, Canvas 90%, CanvasText);
    }}
    button.secondary {{
      background: transparent; color: CanvasText;
      border: 1px solid color-mix(in srgb, CanvasText 22%, transparent);
    }}
    .pill {{
      display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px;
      background: color-mix(in srgb, CanvasText 10%, transparent);
    }}
    .pill.ok {{ background: #1a7f3733; color: #1a7f37; }}
    .pill.empty {{ background: #c0392b33; color: #c0392b; }}
    .flash {{
      padding: 10px 12px; border-radius: 6px; margin-bottom: 18px;
      background: #1a7f3722; color: #1a7f37; font-size: 13px;
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .hint {{ font-size: 12px; opacity: .65; margin-top: 8px; }}
    .hint a {{ color: inherit; }}
  </style>
</head>
<body>
  <main>
    <h1>oto MCP — Réglages</h1>
    <p class="lede">Connecté en tant que <code>{user}</code>.</p>
    {flash}

    <section>
      <h2>LinkedIn — cookie de session</h2>
      <div class="meta">
        Statut : <span class="pill {pill_cls}">{pill_text}</span>
        {set_at_html}
      </div>
      <form method="post" action="/settings/linkedin">
        <label for="cookie">Valeur du cookie <code>li_at</code></label>
        <textarea id="cookie" name="cookie" placeholder="AQED…"
          autocomplete="off" autocorrect="off" spellcheck="false"></textarea>
        <p class="hint">
          Sur linkedin.com, ouvre les DevTools → Application → Cookies → copie
          la valeur de <code>li_at</code>. C'est ce cookie qui authentifie tes
          appels MCP LinkedIn pour ce compte.
        </p>
        <div class="row">
          <button type="submit">Enregistrer</button>
          <button type="submit" name="action" value="clear" class="secondary"
            formaction="/settings/linkedin/clear">Effacer</button>
        </div>
      </form>
    </section>
  </main>
</body>
</html>
"""


def _render(user_sub: str, cookie_set_at: str | None, flash: str | None = None) -> HTMLResponse:
    if cookie_set_at:
        pill_cls, pill_text = "ok", "configuré"
        set_at_html = f' · mis à jour le {cookie_set_at} UTC'
    else:
        pill_cls, pill_text = "empty", "non configuré"
        set_at_html = ""
    flash_html = f'<div class="flash">{flash}</div>' if flash else ""
    return HTMLResponse(_PAGE.format(
        user=user_sub,
        pill_cls=pill_cls,
        pill_text=pill_text,
        set_at_html=set_at_html,
        flash=flash_html,
    ))


def _unauthenticated() -> HTMLResponse:
    return HTMLResponse(
        "<p style='font-family:sans-serif;padding:40px'>Non authentifié. "
        "Cette page sera bientôt protégée par Logto.</p>",
        status_code=401,
    )


async def settings_get(request: Request) -> HTMLResponse:
    sub = current_user_sub_from_request(request)
    if not sub:
        return _unauthenticated()
    user = db.get_user(sub) or {}
    return _render(sub, user.get("linkedin_cookie_set_at"))


async def settings_linkedin_post(request: Request):
    sub = current_user_sub_from_request(request)
    if not sub:
        return _unauthenticated()
    form = await request.form()
    cookie = (form.get("cookie") or "").strip()
    if not cookie:
        user = db.get_user(sub) or {}
        return _render(sub, user.get("linkedin_cookie_set_at"),
                       flash="Cookie vide — rien enregistré.")
    db.set_linkedin_cookie(sub, cookie)
    return RedirectResponse("/settings?saved=1", status_code=303)


async def settings_linkedin_clear_post(request: Request):
    sub = current_user_sub_from_request(request)
    if not sub:
        return _unauthenticated()
    db.clear_linkedin_cookie(sub)
    return RedirectResponse("/settings?cleared=1", status_code=303)
