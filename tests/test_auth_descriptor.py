"""Descripteur d'auth unifié (ADR 0024, B1).

`Connector.auth` = {method, cardinality, fields} dérivé du registre, source
unique du widget credential de la `ConnectorCard`. Ces tests verrouillent la
dérivation (pas de drift entre `secret_kind`/`kind` et `auth`) sans toucher le
comportement runtime (B1 = additif, no-op).
"""
from oto_mcp.providers import _REGISTRY_LIST, REGISTRY, public_catalog

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
    # la connexion user passe par un flux hébergé) ; les flux oauth/cookie/
    # remote/none sont dédiés, sans formulaire. Un credential qui se COMPLÈTE
    # hors formulaire (salesforce, zoho server-based) reste `secret` : l'étape
    # restante se dit par `status_hints`, pas par une méthode d'auth de plus.
    for c in _REGISTRY_LIST:
        if c.auth["fields"]:
            assert c.auth_method in ("secret", "hosted"), \
                f"{c.name}: fields hors method secret/hosted"


def test_hosted_ce_sont_les_canaux_unipile_pas_le_compte():
    """`hosted` = « on connecte un COMPTE TIERS par un flux hébergé ».

    Depuis le split du 2026-08-28, ce sont les six CANAUX unipile — un flux hébergé
    par canal — et surtout PAS `unipile` lui-même : sa carte pose une clé
    d'abonnement, donc `secret`. L'inversion est le piège : `unipile` reste le
    connecteur qui porte la clé, il serait naturel de le croire encore hébergé, et
    le front rendrait alors un widget de connexion sur une carte qui attend un
    champ."""
    hosted = {c.name for c in _REGISTRY_LIST if c.auth_method == "hosted"}
    assert hosted == {"linkedin", "whatsapp", "telegram", "instagram", "messenger", "twitter"}, hosted
    assert REGISTRY["unipile"].auth_method == "secret"


def test_multi_account_providers():
    # Curés : google (OAuth N comptes), zoho (self-clients « 2 Zoho »),
    # browser (N sites derrière login, oto-private#79), folk (historique).
    multi = {c.name for c in _REGISTRY_LIST if c.auth_multi_account}
    assert {"google", "zoho", "browser", "folk"} <= multi, multi
    # Par défaut : TOUT connecteur dont le credential se POSE — clé simple
    # (2026-08-25) comme multi-champs (#409, 2026-08-27). Le coffre est segmenté
    # par `account` sur chaque ligne, la résolution traite un compte unique comme
    # avant. Le NOMBRE DE CHAMPS ne dit rien de la cardinalité : Slack en a deux
    # et porte pourtant un workspace par installation.
    for c in _REGISTRY_LIST:
        posed = (c.auth_method == "secret"
                 and c.secret_kind in ("api_key", "basic_auth", "fields")
                 and not c.personal_cross_org and not c.single_account)
        if posed:
            assert c.auth_multi_account, c.name
        elif c.name not in {"google", "zoho", "browser", "folk"}:
            # OAuth/cookie/none, hosted/remote, cross-org, exclusion explicite :
            # mono-compte.
            assert not c.auth_multi_account, c.name


def test_account_noun_says_the_provider_word():
    """Le descripteur porte le MOT que l'utilisateur emploie chez ce fournisseur —
    un compte Slack du coffre est un workspace, un compte Zoho une organisation, un
    compte du navigateur connecté un site. C'est le registre qui connaît ce
    vocabulaire ; l'écran l'affiche tel quel (oto-dashboard#121)."""
    assert REGISTRY["slack"].auth["account_noun"] == "workspace"
    assert REGISTRY["zoho"].auth["account_noun"] == "organisation"
    assert REGISTRY["browser"].auth["account_noun"] == "site"
    # Défaut : « compte » — jamais vide, l'écran n'a pas de repli à écrire.
    assert REGISTRY["serper"].auth["account_noun"] == "compte"
    assert all(c.auth["account_noun"] for c in _REGISTRY_LIST)


def test_catalog_exposes_auth():
    cat = {c["name"]: c for c in public_catalog()}
    g = cat["google"]
    assert g["auth"]["method"] == "oauth"
    assert g["auth"]["cardinality"] == "multi_account"
    # secret_kind reste exposé le temps de la transition, dérivable de auth.
    assert g["secret_kind"] == "oauth"
    assert cat["serper"]["auth"] == {
        "method": "secret",
        "cardinality": "multi_account",
        "account_noun": "compte",
        # Pas de champ qui en sélectionne d'autres : le cas des ~90 connecteurs à
        # schéma plat. `when`/`choices` vides = « ce champ vaut toujours, en saisie
        # libre » — ils sont TOUJOURS publiés pour que le front n'ait pas de repli
        # à écrire (#449).
        "field_discriminator": "",
        "fields": [{"name": "key", "label": "API key", "secret": True,
                    "required": True, "help": "", "when": [], "choices": []}],
    }
    # Et le descripteur `credential_fields` du catalogue est LE MÊME objet que
    # `auth["fields"]` — les deux listes décrivaient la même chose à deux endroits.
    assert cat["serper"]["credential_fields"] == cat["serper"]["auth"]["fields"]


def test_le_catalogue_publie_de_quoi_filtrer_un_formulaire_a_modes():
    """Un connecteur dont un champ SÉLECTIONNE les autres (`http` : `auth_mode`)
    publie tout ce qu'il faut pour ne montrer que les champs qui servent — sans que
    le front connaisse le connecteur (#449)."""
    http = {c["name"]: c for c in public_catalog()}["http"]
    assert http["auth"]["field_discriminator"] == "auth_mode"
    by_name = {f["name"]: f for f in http["auth"]["fields"]}
    assert by_name["auth_mode"]["choices"][:2] == ["bearer", "header"]
    assert by_name["client_secret"]["when"] == ["oauth2"]
    assert by_name["token"]["when"] == ["bearer", "header", "query"]
