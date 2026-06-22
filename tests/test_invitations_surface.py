"""Surface d'invitation refondue (émission unifiée + lien referral + code).

Niveau contrat (sans DB) : présence des capacités MCP/REST consommées par
oto-dashboard et la forme des inputs (toggle mail, email optionnel, accept
multi-forme). Cf. capabilities/orgs_invites.py.
"""
from oto_mcp.capabilities import orgs_invites as oi
from oto_mcp.capabilities import registry


def test_referral_and_accept_caps_present():
    mcp = {c.mcp for c in registry.caps_with_mcp()}
    assert {"oto_referral_link", "oto_invite_to_alpha", "oto_accept_invite",
            "oto_invite_member"} <= mcp


def test_rest_routes_preserved():
    pairs = {(b.verb, b.path) for c in registry.caps_with_rest() for b in c.rest_bindings()}
    for vp in [
        ("GET", "/api/me/referral-link"),
        ("POST", "/api/me/alpha-invites"),
        ("POST", "/api/me/invitations/accept"),
        ("POST", "/api/orgs/{id}/invitations"),
        ("POST", "/api/admin/alpha-invites"),
    ]:
        assert vp in pairs, vp


def test_send_email_toggle_defaults_true_email_optional():
    for Model in (oi.InviteCreateInput, oi.AlphaInviteInput, oi.AlphaInviteAdminInput):
        f = Model.model_fields
        assert f["send_email"].default is True, Model
        # email optionnel (None autorisé) → émission « code à partager soi-même »
        assert f["email"].default is None, Model


def test_accept_input_multiform():
    f = oi.InviteAcceptInput.model_fields
    assert {"token", "code", "carrier"} <= set(f)
    assert all(f[k].default is None for k in ("token", "code", "carrier"))


def test_org_invite_create_requires_org_id():
    assert oi.InviteCreateInput.model_fields["org_id"].is_required()
