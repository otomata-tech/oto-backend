"""Surface d'invitation d'org (émission unifiée + code court).

Niveau contrat (sans DB) : présence des capacités MCP/REST consommées par
oto-dashboard et la forme des inputs (toggle mail, email optionnel, accept
par token/code). Cf. capabilities/orgs_invites.py.
"""
from oto_mcp.capabilities import orgs_invites as oi
from oto_mcp.capabilities import registry


def test_invite_caps_present():
    mcp = {c.mcp for c in registry.caps_with_mcp()}
    # ADR 0047 B3 : invite/accept vivent dans la console oto_org (op=invite /
    # op=accept_invite) ; referral/alpha sont RETIRÉS (ADR 0013 supersédé).
    assert "oto_org" in mcp
    assert {"oto_referral_link", "oto_invite_to_alpha",
            "oto_accept_invite", "oto_invite_member"} & mcp == set()


def test_rest_routes_preserved():
    pairs = {(b.verb, b.path) for c in registry.caps_with_rest() for b in c.rest_bindings()}
    for vp in [
        ("POST", "/api/me/invitations/accept"),
        ("POST", "/api/orgs/{id}/invitations"),
        ("GET", "/api/orgs/{id}/invitations"),
    ]:
        assert vp in pairs, vp


def test_send_email_toggle_defaults_true_email_optional():
    f = oi.InviteCreateInput.model_fields
    assert f["send_email"].default is True
    # email optionnel (None autorisé) → émission « code à partager soi-même »
    assert f["email"].default is None


def test_accept_input_multiform():
    f = oi.InviteAcceptInput.model_fields
    assert {"token", "code"} <= set(f)
    assert all(f[k].default is None for k in ("token", "code"))


def test_org_invite_create_requires_org_id():
    assert oi.InviteCreateInput.model_fields["org_id"].is_required()
