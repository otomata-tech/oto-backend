"""Descripteur d'auth unifié (ADR 0024, B1).

`Connector.auth` = {method, cardinality, fields} dérivé du registre, source
unique du widget credential de la `ConnectorCard`. Ces tests verrouillent la
dérivation (pas de drift entre `secret_kind`/`kind` et `auth`) sans toucher le
comportement runtime (B1 = additif, no-op).
"""
from oto_mcp.providers import _REGISTRY_LIST, public_catalog

_METHODS = {"secret", "oauth", "cookie", "remote", "hosted", "none"}


def test_method_in_closed_set():
    for c in _REGISTRY_LIST:
        assert c.auth_method in _METHODS, f"{c.name}: méthode hors enum"


def test_method_derivation_matches_kind_and_secret_kind():
    for c in _REGISTRY_LIST:
        m = c.auth_method
        if c.hosted_auth:
            # flux hébergé tiers (unipile) — prime sur le credential sous-jacent.
            assert m == "hosted", c.name
        elif c.kind == "remote" and not c.credential_fields:
            # bridge legacy (ADR 0003) : credential posé par grant, pas de formulaire
            assert m == "remote", c.name
        elif c.kind == "remote":
            # bridge nouveau modèle (ADR 0034) : credential_fields déclarés →
            # formulaire self-serve standard
            assert m == "secret", c.name
        elif c.secret_kind in ("oauth", "cookie", "none"):
            assert m == c.secret_kind, c.name
        else:
            # api_key / basic_auth / fields / refresh_token → saisie de secret(s)
            assert m == "secret", c.name


def test_fields_only_for_secret_or_hosted_method():
    # un schéma de saisie n'a de sens que pour method=secret (formulaire de
    # champs) OU hosted (le credential reste une clé résolue en cascade, même si
    # la connexion user passe par un flux hébergé) ; les flux oauth/cookie/remote/
    # none sont dédiés, sans formulaire.
    for c in _REGISTRY_LIST:
        if c.auth["fields"]:
            assert c.auth_method in ("secret", "hosted"), \
                f"{c.name}: fields hors method secret/hosted"


def test_hosted_is_unipile_only():
    hosted = {c.name for c in _REGISTRY_LIST if c.auth_method == "hosted"}
    assert hosted == {"unipile"}, hosted


def test_multi_account_is_google_only():
    multi = {c.name for c in _REGISTRY_LIST if c.auth_multi_account}
    assert multi == {"google"}, multi


def test_catalog_exposes_auth():
    cat = {c["name"]: c for c in public_catalog()}
    g = cat["google"]
    assert g["auth"]["method"] == "oauth"
    assert g["auth"]["cardinality"] == "multi_account"
    # secret_kind reste exposé le temps de la transition, dérivable de auth.
    assert g["secret_kind"] == "oauth"
    assert cat["serper"]["auth"] == {
        "method": "secret",
        "cardinality": "single",
        "fields": [{"name": "key", "label": "API key", "secret": True, "required": True, "help": ""}],
    }
