"""Consultation d'org en LECTURE SEULE par un opérateur plateforme (ViewAsMiddleware).

Régression/feature : un admin plateforme doit pouvoir CONSULTER une org tierce (dont
il n'est pas membre) via `X-Oto-Org`, mais **en lecture seule** — miroir du view-as
user. Le garde vit dans `ViewAsMiddleware` : non-membre → autorisé SSI opérateur
plateforme ET méthode GET ; écriture → 403 `view_as_read_only` ; non-opérateur → 403.

On pilote le middleware ASGI directement (auth JWT monkeypatchée) et on observe si
l'app aval est appelée (autorisé) ou si un 403 est rendu (refusé) — le vrai chemin,
pas un stub du garde (cf. leçon fail-open + tests stubbés).
"""
import pytest

from oto_mcp import access, api_routes, roles, session_org


class _DownstreamApp:
    """App ASGI aval : note qu'elle a été atteinte + rend 200."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


def _drive(mw, method, org_header):
    """Pousse une requête HTTP dans le middleware, renvoie (downstream_called, status)."""
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/me",
        "query_string": b"",
        "headers": [(b"x-oto-org", org_header.encode())],
    }
    events = []

    async def send(msg):
        events.append(msg)

    import asyncio

    asyncio.run(mw(scope, _receive, send))
    status = next((e["status"] for e in events if e["type"] == "http.response.start"), None)
    return status, events


@pytest.fixture
def _auth_op(monkeypatch):
    """Auth qui renvoie toujours le sub 'op' (sub réel de l'opérateur)."""
    async def _fake_auth(request, verifier, apply_view_as=True):
        return "op", None

    monkeypatch.setattr(api_routes, "_authenticate", _fake_auth)


def _make_mw():
    down = _DownstreamApp()
    return api_routes.ViewAsMiddleware(down, verifier=None), down


def test_platform_operator_reads_foreign_org(_auth_op, monkeypatch):
    """Opérateur plateforme, GET, org non-membre → autorisé (lecture)."""
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    mw, down = _make_mw()
    status, _ = _drive(mw, "GET", "172")
    assert down.called is True
    assert status == 200
    # Le contexte de consultation (view-org) est bien retombé après la requête.
    assert session_org.current_view_org() is None


def test_platform_operator_write_foreign_org_forbidden(_auth_op, monkeypatch):
    """Opérateur plateforme, POST, org non-membre → 403 (lecture seule, pas d'écriture)."""
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: True)
    mw, down = _make_mw()
    status, _ = _drive(mw, "POST", "172")
    assert down.called is False
    assert status == 403


def test_non_operator_foreign_org_forbidden(_auth_op, monkeypatch):
    """Simple user, GET, org non-membre → 403 (pas d'exception de supervision)."""
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: False)
    monkeypatch.setattr(access, "is_platform_operator", lambda sub: False)
    mw, down = _make_mw()
    status, _ = _drive(mw, "GET", "172")
    assert down.called is False
    assert status == 403


def test_member_reads_own_org(_auth_op, monkeypatch):
    """Membre de l'org → autorisé sans passer par l'exception opérateur (write inclus,
    géré par l'autz aval — ici on vérifie juste que le middleware laisse passer)."""
    monkeypatch.setattr(roles, "is_org_member", lambda sub, org: True)
    # is_platform_operator ne doit même pas être consulté sur ce chemin.
    monkeypatch.setattr(access, "is_platform_operator",
                        lambda sub: pytest.fail("membre : ne doit pas tester l'opérateur"))
    mw, down = _make_mw()
    status, _ = _drive(mw, "POST", "172")
    assert down.called is True
    assert status == 200
