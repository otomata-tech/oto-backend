"""Le jeton renouvelé par Salesforce doit être PERSISTÉ, là où l'ancien a été lu.

Salesforce impose la rotation (RTR) sur les External Client Apps : chaque
rafraîchissement invalide le jeton utilisé et en renvoie un neuf. oto-core le remonte
désormais via `on_refresh` ; c'est ici qu'on l'écrit.

Vécu le 31/07 : jeton posé à 12:07:31.570, sonde réussie à 12:07:32.089, jeton mort à
12:07:33 — notre propre vérification consommait le jeton juste après l'avoir écrit, et
personne ne récupérait le remplaçant.

⚠️ La réécriture est CONDITIONNELLE. Prod et preprod partagent la base ; deux appels
peuvent tourner en parallèle. Remettre en place un jeton déjà consommé est exactement
ce que Salesforce traite comme une compromission — il révoque alors le jeton courant ET
tous les access tokens associés.
"""
from __future__ import annotations

import pytest

from oto_mcp import credentials_store
from oto_mcp.tools.salesforce import _rotation_writer


class _RC:
    """Substitut de `ResolvedCredential` — seuls l'entité gagnante et le compte comptent."""

    def __init__(self, entity_type=None, entity_id=None, account=""):
        self.entity_type, self.entity_id, self.account = entity_type, entity_id, account


@pytest.fixture
def coffre(monkeypatch):
    """Coffre en mémoire : {(type, id, account): champs}."""
    store: dict[tuple, dict] = {}
    metas: dict[tuple, dict] = {}
    ecritures: list[tuple] = []

    def _get(entity_type, entity_id, connector, account=""):
        champs = store.get((entity_type, entity_id, account))
        return {"secret": champs, "meta": metas.get((entity_type, entity_id, account), {})} if champs else None

    def _unpack(_connector, secret):
        return dict(secret)

    def _pack(_connector, champs):
        return dict(champs)

    def _set(entity_type, entity_id, connector, secret, account="", **kw):
        store[(entity_type, entity_id, account)] = dict(secret)
        # Reproduit l'upsert réel : `meta = EXCLUDED.meta`, donc omettre l'argument
        # ÉCRASE par {} au lieu de préserver.
        metas[(entity_type, entity_id, account)] = dict(kw.get("meta") or {})
        ecritures.append((entity_type, entity_id, account))

    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _get)
    monkeypatch.setattr(credentials_store, "unpack_secret", _unpack)
    monkeypatch.setattr(credentials_store, "pack_secret", _pack)
    monkeypatch.setattr(credentials_store, "set_credential", _set)
    return store, ecritures, metas


def _pose(store, entity_type, entity_id, jeton, account=""):
    store[(entity_type, entity_id, account)] = {
        "client_id": "ci", "client_secret": "cs",
        "login_url": "https://x.my.salesforce.com", "refresh_token": jeton}


# --- l'écriture nominale -------------------------------------------------------

@pytest.mark.parametrize("entity_type,entity_id", [
    ("member", "2:sub-x"), ("group", "7"), ("org", "2")])
def test_le_jeton_est_reecrit_a_lentite_gagnante(coffre, entity_type, entity_id):
    """Membre, équipe ou org : on réécrit LÀ OÙ on a lu. Se tromper de niveau
    rangerait le jeton d'une org dans la clé perso, ou l'inverse."""
    store, _, metas = coffre
    _pose(store, entity_type, entity_id, "RT-1")
    _rotation_writer(_RC(entity_type, entity_id), "RT-1")({"refresh_token": "RT-2"})
    assert store[(entity_type, entity_id, "")]["refresh_token"] == "RT-2"


def test_les_autres_champs_survivent(coffre):
    """Read-merge-write : le secret est un blob unique, une écriture partielle
    effacerait client_id/client_secret/login_url."""
    store, _, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-1")
    _rotation_writer(_RC("member", "2:sub-x"), "RT-1")({"refresh_token": "RT-2"})
    champs = store[("member", "2:sub-x", "")]
    assert champs["client_id"] == "ci" and champs["client_secret"] == "cs"
    assert champs["login_url"] == "https://x.my.salesforce.com"


def test_le_compte_nomme_est_respecte(coffre):
    """Multi-compte : écrire sur le compte '' écraserait la mauvaise ligne."""
    store, _, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-1", account="prod")
    _rotation_writer(_RC("member", "2:sub-x", "prod"), "RT-1")({"refresh_token": "RT-2"})
    assert store[("member", "2:sub-x", "prod")]["refresh_token"] == "RT-2"


# --- démarquage (oto#25 lot b3) -------------------------------------------------
#
# `on_refresh` n'est invoqué qu'APRÈS un refresh d'access token RÉUSSI (jamais sur
# échec — cf. `oto.tools.salesforce.client`) : c'est le déclencheur "refresh réussi"
# du démarquage, à distinguer de la rotation du REFRESH token (une autre affaire,
# qui n'a pas lieu à chaque refresh).

def test_un_refresh_reussi_demarque_meme_sans_rotation(coffre, monkeypatch):
    """Beaucoup de fournisseurs ne tournent pas le refresh token à chaque usage —
    le démarquage, lui, ne doit PAS attendre une rotation qui n'arrivera peut-être
    jamais : la clé a bien réussi un refresh, c'est un fait, il se rapporte."""
    from oto_mcp.tools import salesforce as sf
    store, ecritures, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-1")
    vus = []
    monkeypatch.setattr(sf.connector_health, "record_health",
                        lambda *a: vus.append(a))
    _rotation_writer(_RC("member", "2:sub-x"), "RT-1")({"access_token": "AT"})
    assert vus == [("salesforce", ("member", "2:sub-x", ""), True, None)]
    assert not ecritures        # aucune rotation à persister, deux gestes distincts


def test_un_grant_plateforme_ne_demarque_rien(coffre, monkeypatch):
    """Pas de ligne de coffre pour une clé plateforme (`entity_type is None`) : rien
    à démarquer non plus — même garde que pour l'écriture de rotation."""
    from oto_mcp.tools import salesforce as sf
    vus = []
    monkeypatch.setattr(sf.connector_health, "record_health",
                        lambda *a: vus.append(a))
    _rotation_writer(_RC(None, None), "RT-1")({"refresh_token": "RT-2"})
    assert vus == []


# --- les cas où il ne faut RIEN écrire -----------------------------------------

def test_sans_rotation_aucune_ecriture(coffre):
    """Tous les fournisseurs ne tournent pas. Une écriture par appel d'outil serait
    du bruit pur — et de la contention sur une ligne chaude."""
    store, ecritures, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-1")
    _rotation_writer(_RC("member", "2:sub-x"), "RT-1")({"access_token": "AT"})
    assert not ecritures


def test_un_jeton_identique_nest_pas_reecrit(coffre):
    store, ecritures, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-1")
    _rotation_writer(_RC("member", "2:sub-x"), "RT-1")({"refresh_token": "RT-1"})
    assert not ecritures


def test_un_grant_plateforme_na_pas_de_ligne_a_reecrire(coffre):
    """`entity_type is None` = clé plateforme : sa config est l'environnement, pas une
    ligne du coffre. Écrire créerait une ligne fantôme."""
    _, ecritures, metas = coffre
    _rotation_writer(_RC(None, None), "RT-1")({"refresh_token": "RT-2"})
    assert not ecritures


def test_on_necrase_pas_un_jeton_plus_recent(coffre):
    """LE test qui compte. Un autre appel — ou l'autre environnement, la base étant
    partagée — a déjà tourné. Réécrire remettrait en place un jeton CONSOMMÉ, et sa
    réutilisation fait révoquer par Salesforce toute la connexion."""
    store, ecritures, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-3")            # déjà tourné par un autre
    _rotation_writer(_RC("member", "2:sub-x"), "RT-1")({"refresh_token": "RT-2"})
    assert store[("member", "2:sub-x", "")]["refresh_token"] == "RT-3"
    assert not ecritures


def test_une_ligne_disparue_ne_fait_pas_echouer_lappel(coffre):
    """Le credential a pu être supprimé pendant l'appel. L'utilisateur a un jeton
    d'accès valide en main : sa requête doit aboutir."""
    _, ecritures, metas = coffre
    _rotation_writer(_RC("member", "2:absent"), "RT-1")({"refresh_token": "RT-2"})
    assert not ecritures


# --- tripwire ------------------------------------------------------------------

def test_le_client_resout_lentite_et_branche_la_persistance():
    """`resolve_credential_fields` ne rend que les champs : revenir à lui ferait perdre
    l'entité gagnante, donc rendrait la réécriture impossible — en silence, la rotation
    reprenant son travail de sape."""
    import inspect
    from oto_mcp.tools import salesforce as sf
    src = inspect.getsource(sf.register)
    assert "resolve_credential(" in src, "l'entité gagnante n'est plus résolue"
    assert "on_refresh=" in src, "la persistance du jeton renouvelé n'est plus branchée"


# --- la sonde aussi consomme une rotation : elle doit persister ----------------

def test_la_sonde_branche_la_persistance():
    """LA régression du 31/07 (deuxième manche). La persistance avait été câblée sur
    le chemin des outils mais PAS sur la sonde — or c'est la sonde post-écriture qui
    tourne 500 ms après le consentement, et elle tuait le jeton qu'elle venait de
    valider. Une sonde qui consomme une rotation sans la persister détruit la
    connexion qu'elle prétend vérifier."""
    import inspect
    from oto_mcp.tools import salesforce as sf
    assert "on_refresh=" in inspect.getsource(sf._verify), (
        "la sonde consomme le jeton sans persister son remplaçant")


def test_la_sonde_ne_persiste_pas_des_champs_candidats(monkeypatch):
    """`api_key_save` sonde AVANT d'écrire : les champs testés ne correspondent à
    aucune ligne. Persister là écrirait dans le credential de quelqu'un d'autre — ou
    ressusciterait une ligne qu'on s'apprêtait à remplacer."""
    from oto_mcp import access
    from oto_mcp.tools import salesforce as sf

    class _RCautre:
        entity_type, entity_id, account = "member", "2:sub-x", ""
        fields = {"refresh_token": "RT-DEJA-EN-BASE"}

    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _RCautre())
    assert sf._rotation_writer_for("RT-CANDIDAT") is None


def test_la_sonde_hors_contexte_ne_casse_pas(monkeypatch):
    """CLI, test, pas d'org : la résolution échoue. On retombe sur l'ancien
    comportement plutôt que de faire échouer la sonde."""
    from oto_mcp import access
    from oto_mcp.tools import salesforce as sf

    def _boom(*a, **k):
        raise RuntimeError("pas de contexte de requête")

    monkeypatch.setattr(access, "resolve_credential", _boom)
    assert sf._rotation_writer_for("RT-1") is None


def test_la_rotation_ne_detruit_pas_le_meta(coffre):
    """RÉGRESSION du 03/08. L'upsert fait `meta = EXCLUDED.meta` : omettre l'argument
    n'est pas « ne pas y toucher », c'est écraser par {}. Comme la rotation réécrit à
    chaque appel d'outil, `instance_url` / `identity_url` / `connected_at` étaient
    effacés dès le premier usage — on ne savait plus sur quelle org Salesforce la clé
    pointait. Repéré parce qu'une clé ayant tourné depuis la veille avait un config
    vide, là où une clé fraîche avait le sien intact."""
    store, _, metas = coffre
    _pose(store, "member", "2:sub-x", "RT-1")
    metas[("member", "2:sub-x", "")] = {
        "instance_url": "https://x.my.salesforce.com",
        "identity_url": "https://login.salesforce.com/id/00D.../005...",
        "connected_at": "2026-08-02T14:47:00Z"}
    _rotation_writer(_RC("member", "2:sub-x"), "RT-1")({"refresh_token": "RT-2"})
    apres = metas[("member", "2:sub-x", "")]
    assert apres.get("instance_url") == "https://x.my.salesforce.com", (
        "la rotation a effacé instance_url : on ne sait plus quelle org est jointe")
    assert apres.get("identity_url") and apres.get("connected_at")
