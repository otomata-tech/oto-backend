"""Les champs du connecteur `api` sont déclarés PAR mode d'auth (oto-backend#449).

Deux défauts gravés ici :
- douze champs affichés quel que soit le mode, alors que `bearer` en sert trois ;
- un mode incohérent (`header` sans `header_name`) accepté `ok: true` à l'écriture,
  qui n'échoue qu'au premier appel réel.

Et le TRIPWIRE qui empêche le registre et oto-core de diverger : le registre déclare
ce qu'il faut saisir, `oto.tools.http.build_auth` décide ce qu'il faut à l'appel. Les
deux doivent dire la même chose, sinon le formulaire redevient un piège.
"""
import pytest
from oto.tools.http import AUTH_MODES as CORE_AUTH_MODES, build_auth

from oto_mcp import credentials_store
from oto_mcp.providers.api import AUTH_MODES, CONNECTOR

BASE = {"base_url": "https://api.acme.test"}

# Ce que la lib attend des modes `session` et `visitor` : une configuration lisible (le
# visiteur a son propre jeu de clés), l'URL de base et la garde de l'hôte. Les autres
# modes n'en ont pas l'usage.
VALEURS = {"session": {"session": '{"login": {"path": "/api/login"}}'},
           "visitor": {"session": '{"login": {"path": "/", "method": "GET"}}'}}
_SESSION = {"base_url": BASE["base_url"], "guard": lambda url: None}
CONTEXTE = {"session": _SESSION, "visitor": _SESSION}


def _build(mode: str, fields: dict):
    return build_auth(mode, fields, **CONTEXTE.get(mode, {}))


def _valeur(mode: str, champ: str) -> str:
    return VALEURS.get(mode, {}).get(champ, "x")


def _required_for(mode: str) -> list:
    return [f for f in CONNECTOR.fields_for({"auth_mode": mode})
            if f.required and f.name not in ("base_url", "auth_mode")]


# --- Le tripwire registre ↔ oto-core ---------------------------------------

def test_le_jeu_de_modes_recopie_celui_doto_core():
    assert AUTH_MODES == CORE_AUTH_MODES


@pytest.mark.parametrize("mode", AUTH_MODES)
def test_les_champs_declares_suffisent_a_construire_lauth(mode):
    """Ce que le registre demande de saisir CONSTRUIT l'auth : pas de sous-déclaration
    (un formulaire complet qui échoue quand même au premier appel)."""
    _build(mode, {f.name: _valeur(mode, f.name) for f in _required_for(mode)})


@pytest.mark.parametrize("mode", AUTH_MODES)
def test_aucun_champ_declare_nest_superflu(mode):
    """…et pas de sur-déclaration non plus : retirer n'importe lequel des champs que
    le registre dit requis DOIT casser `build_auth`. Sans ce sens-là, le registre
    pourrait exiger des champs que personne ne lit."""
    required = _required_for(mode)
    for dropped in required:
        partial = {f.name: _valeur(mode, f.name) for f in required
                   if f.name != dropped.name}
        with pytest.raises(ValueError):
            _build(mode, partial)


# --- Ce que le mode rend pertinent ------------------------------------------

def test_bearer_ne_montre_pas_les_champs_doauth2_et_de_basic():
    names = [f.name for f in CONNECTOR.fields_for({"auth_mode": "bearer"})]
    assert names == ["base_url", "auth_mode", "label", "doc_path", "token"]


def test_session_demande_identifiant_mot_de_passe_et_etapes():
    by_name = {f.name: f for f in CONNECTOR.fields_for({"auth_mode": "session"})}
    assert list(by_name) == ["base_url", "auth_mode", "label", "doc_path", "username",
                             "password", "session"]
    assert by_name["password"].secret is True
    assert by_name["session"].secret is False


def test_visitor_ne_demande_que_les_etapes():
    """Un site sans compte : ni identifiant ni mot de passe, seulement la page d'entrée."""
    by_name = {f.name: f for f in CONNECTOR.fields_for({"auth_mode": "visitor"})}
    assert list(by_name) == ["base_url", "auth_mode", "label", "doc_path", "session"]
    assert by_name["session"].required is True and by_name["session"].secret is False


def test_une_carte_visitor_coherente_passe_et_ne_garde_rien_d_autre():
    etapes = '{"login": {"path": "/entree", "method": "GET"}}'
    kept = credentials_store.validate_fields("api", {
        **BASE, "auth_mode": "visitor", "session": etapes,
        "username": "reste", "password": "ancien"})
    assert kept == {**BASE, "auth_mode": "visitor", "session": etapes}


def test_une_carte_visitor_sans_etapes_est_refusee_a_lecriture():
    with pytest.raises(credentials_store.CredentialFieldsInvalid) as e:
        credentials_store.validate_fields("api", {**BASE, "auth_mode": "visitor"})
    assert e.value.code == "missing_credentials"
    assert "Session steps" in e.value.message


def test_les_etapes_de_session_gardent_leurs_espaces():
    """Le JSON des étapes peut porter une constante avec un espace : il n'est pas
    nettoyé comme une clé."""
    etapes = '{"login": {"path": "/l", "constants": {"client": "Mon espace"}}}'
    kept = credentials_store.validate_fields("api", {
        **BASE, "auth_mode": "session", "username": "u", "password": "p",
        "session": etapes})
    assert kept["session"] == etapes


def test_none_ne_demande_aucun_secret():
    names = [f.name for f in CONNECTOR.fields_for({"auth_mode": "none"})]
    assert names == ["base_url", "auth_mode", "label", "doc_path"]


def test_doc_path_est_optionnel_et_pertinent_quel_que_soit_le_mode():
    """`doc_path` n'est pas un secret, n'est requis dans aucun mode, et vaut pour
    tous : le tool `api_doc` ne dépend pas de auth_mode."""
    for mode in AUTH_MODES:
        fields = CONNECTOR.fields_for({"auth_mode": mode})
        by_name = {f.name: f for f in fields}
        assert "doc_path" in by_name
        assert by_name["doc_path"].required is False
        assert by_name["doc_path"].secret is False


def test_mode_absent_tout_reste_pertinent():
    """La saisie n'a pas encore choisi : masquer serait deviner."""
    assert len(CONNECTOR.fields_for({})) == len(CONNECTOR.secret_fields)


def test_le_front_recoit_de_quoi_filtrer_sans_connaitre_le_connecteur():
    auth = CONNECTOR.auth
    assert auth["field_discriminator"] == "auth_mode"
    by_name = {f["name"]: f for f in auth["fields"]}
    assert by_name["header_name"]["when"] == ["header"]
    assert by_name["auth_mode"]["choices"] == list(AUTH_MODES)
    assert by_name["base_url"]["when"] == []


# --- La cohérence se joue à l'ÉCRITURE, plus au call-time -------------------

def test_header_sans_header_name_est_refuse_a_lecriture():
    """Le cas nommé de l'issue : `ok: true` puis échec au premier appel réel."""
    with pytest.raises(credentials_store.CredentialFieldsInvalid) as e:
        credentials_store.validate_fields(
            "api", {**BASE, "auth_mode": "header", "token": "T"})
    assert e.value.code == "missing_credentials"
    assert "Header name" in e.value.message


def test_un_mode_mal_orthographie_est_refuse_avec_le_jeu_attendu():
    with pytest.raises(credentials_store.CredentialFieldsInvalid) as e:
        credentials_store.validate_fields("api", {**BASE, "auth_mode": "hedaer"})
    assert e.value.code == "invalid_field_value"
    assert "bearer" in e.value.message and "hedaer" in e.value.message


def test_bearer_naccepte_pas_dechapper_au_token():
    with pytest.raises(credentials_store.CredentialFieldsInvalid) as e:
        credentials_store.validate_fields("api", {**BASE, "auth_mode": "bearer"})
    assert "Token" in e.value.message


def test_une_saisie_coherente_passe():
    kept = credentials_store.validate_fields(
        "api", {**BASE, "auth_mode": "bearer", "token": "T"})
    assert kept == {**BASE, "auth_mode": "bearer", "token": "T"}


def test_les_champs_hors_mode_ne_sont_pas_stockes():
    """Repasser d'oauth2 à bearer ne laisse pas un `client_secret` mort au coffre."""
    kept = credentials_store.validate_fields("api", {
        **BASE, "auth_mode": "bearer", "token": "T",
        "client_secret": "VIEUX", "token_url": "https://old.test/token"})
    assert "client_secret" not in kept and "token_url" not in kept


def test_les_connecteurs_sans_discriminant_ne_changent_pas():
    """Les ~90 autres connecteurs n'ont pas de discriminant : mêmes champs qu'avant."""
    from oto_mcp import providers
    for c in providers.REGISTRY.values():
        if not c.field_discriminator:
            assert c.fields_for({"auth_mode": "bearer"}) == c.secret_fields
