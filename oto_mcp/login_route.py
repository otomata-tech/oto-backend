"""/login GET (form) + POST (submit) routes used by PasswordOAuthProvider."""
from __future__ import annotations

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from .oauth import PasswordOAuthProvider


LOGIN_HTML = """\
<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Connexion – oto MCP</title>
  <style>
    :root {{ color-scheme: light dark; }}
    html, body {{ margin: 0; height: 100%; }}
    body {{
      display: grid; place-items: center;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: Canvas; color: CanvasText;
    }}
    form {{
      display: flex; flex-direction: column; gap: 12px;
      padding: 28px 32px; border: 1px solid color-mix(in srgb, CanvasText 15%, transparent);
      border-radius: 10px; min-width: 300px;
    }}
    h1 {{ font-size: 18px; margin: 0 0 4px; font-weight: 600; }}
    p {{ margin: 0; font-size: 13px; opacity: .7; }}
    input[type=password] {{
      padding: 10px 12px; font-size: 15px;
      border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
      border-radius: 6px; background: Canvas; color: CanvasText;
    }}
    button {{
      padding: 10px 12px; font-size: 15px; font-weight: 500;
      border: 0; border-radius: 6px;
      background: color-mix(in srgb, CanvasText 85%, Canvas);
      color: color-mix(in srgb, Canvas 90%, CanvasText);
      cursor: pointer;
    }}
    button:hover {{ opacity: .9; }}
    .err {{ color: #c0392b; font-size: 13px; margin: 0; }}
  </style>
</head>
<body>
  <form method="post" action="/login">
    <h1>oto MCP</h1>
    <p>Mot de passe pour autoriser Claude.</p>
    {error}
    <input type="hidden" name="nonce" value="{nonce}">
    <input type="password" name="password" placeholder="Mot de passe" autofocus required>
    <button type="submit">Autoriser</button>
  </form>
</body>
</html>
"""


def render_login(nonce: str, error: str | None = None) -> HTMLResponse:
    err_html = f'<p class="err">{error}</p>' if error else ""
    return HTMLResponse(LOGIN_HTML.format(nonce=nonce, error=err_html))


async def login_get(request: Request) -> HTMLResponse:
    provider: PasswordOAuthProvider = request.app.state.oauth_provider
    nonce = request.query_params.get("nonce", "")
    if not nonce or not provider.get_pending(nonce):
        return HTMLResponse(
            "<p>Lien expiré ou invalide. Relancez la connexion depuis Claude.</p>",
            status_code=400,
        )
    return render_login(nonce)


async def login_post(request: Request):
    provider: PasswordOAuthProvider = request.app.state.oauth_provider
    form = await request.form()
    nonce = (form.get("nonce") or "").strip()
    password = form.get("password") or ""
    pending = provider.get_pending(nonce)
    if not pending:
        return HTMLResponse(
            "<p>Session expirée. Relancez la connexion depuis Claude.</p>",
            status_code=400,
        )
    if not provider.check_password(password):
        return render_login(nonce, error="Mot de passe incorrect.")
    redirect_url = await provider.complete_login(nonce)
    if not redirect_url:
        return HTMLResponse("<p>Erreur interne.</p>", status_code=500)
    return RedirectResponse(redirect_url, status_code=303)
