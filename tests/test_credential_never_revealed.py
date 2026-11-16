"""Une clé posée ne se relit JAMAIS — quel que soit le connecteur, quel que soit le palier.

Décision du 2026-08-31 (oto-backend#671). Jusqu'ici, `GET /api/settings/api-keys/{provider}`
rendait la VALEUR EN CLAIR de tout champ déclaré `reveal=True` — et c'était le DÉFAUT :
55 connecteurs (49 par dérivation du `secret_kind="api_key"`, 6 déclarés à la main) sortaient
leur clé entière à chaque ouverture d'écran. Le geste servait à recopier sa propre clé ; son
coût était que le secret traversait le réseau, le navigateur et l'état applicatif d'un
intégrateur, sans que personne ne l'ait demandé.

Ce que ces tests gravent :

1. **la valeur d'un champ `secret=True` ne sort par aucun chemin** — balayage de TOUT le
   registre, pas trois connecteurs choisis ;
2. **la clé du champ est ABSENTE du corps, jamais `null` ni `""`** — un client qui lisait
   `body["key"]` doit casser BRUYAMMENT, pas exporter une chaîne vide (c'est le mode
   d'échec réel de `oto ninja secrets get`, qui fait `export FOO=$(…)`) ;
3. **ce qui remplace la valeur reconnaît la clé sans la lire** : présence, date de pose,
   auteur, et une empreinte courte NON INVERSIBLE — un HMAC lié à la ligne du coffre, jamais
   des caractères du secret ;
4. **demander la valeur reçoit un refus NOMMÉ**, pas un 200 amputé ;
5. **les champs NON secrets continuent de sortir** — c'est la modification partielle de
   #448, et elle n'a jamais eu besoin de la révélation.
"""
from __future__ import annotations

import re

import pytest

from _datastore_rest import call, cap, stub_authz

from oto_mcp import credentials_store, providers
from oto_mcp.capabilities import me_credentials as mc

ORG, GROUP = 35, 31
SUB_POSEUR = "u-poseuse"

_HEX4 = re.compile(r"^[0-9a-f]{4}$")


def _sentinelles(connector: str) -> dict:
    """Une valeur RECONNAISSABLE par champ déclaré — de quoi la retrouver n'importe où
    dans le corps rendu, y compris imbriquée ou concaténée.

    ⚠️ La sentinelle se TERMINE par un marqueur : sans lui, `api` déclare `token`
    (secret) et `token_url` (non secret), et la valeur du second CONTIENT celle du
    premier. Le test aurait crié au secret sorti en montrant une valeur publique."""
    c = providers.REGISTRY[connector]
    return {f.name: f"SENTINELLE~{connector}~{f.name}~FIN" for f in c.secret_fields}


def _pose(monkeypatch, connector: str, fields: dict, *, set_at="2026-08-30T09:15:00+00:00",
          set_by=SUB_POSEUR):
    """Le coffre, en mémoire : la ligne existe, elle est datée et signée."""
    secret = credentials_store.pack_secret(connector, fields)
    monkeypatch.setattr(
        mc.credentials_store, "get_credential_with_meta",
        lambda et, eid, con, account="": {
            "secret": secret, "meta": {}, "set_at": set_at, "set_by": set_by})


@pytest.fixture()
def paliers(monkeypatch):
    monkeypatch.setattr(mc.access, "current_org", lambda sub: ORG)
    monkeypatch.setattr(mc.access, "current_group", lambda sub: GROUP)
    monkeypatch.setattr(mc.roles, "can_admin_group", lambda sub, gid: True)
    monkeypatch.setattr(mc.roles, "is_org_admin", lambda sub, oid: True)


def _lire(provider, query=b""):
    return call("me.credential.get", path_params={"provider": provider}, query=query)


def _scope_lisible(connector: str) -> bytes:
    """Le palier auquel ce connecteur accepte une saisie (`api` est `byo_org` pur)."""
    return b"" if providers.is_byo_user(connector) else b"scope=org"


# --- 1. Aucun secret ne sort, sur AUCUN connecteur --------------------------

def _connecteurs_a_secret() -> list[str]:
    return sorted(n for n, c in providers.REGISTRY.items()
                  if any(f.secret for f in c.secret_fields)
                  and (providers.is_byo_user(n) or providers.is_org_shareable(n)))


@pytest.mark.parametrize("connector", _connecteurs_a_secret())
def test_aucune_valeur_de_champ_secret_ne_sort_du_serveur(monkeypatch, paliers, connector):
    """Le balayage qui manquait : TOUT le registre, pas un échantillon. Un connecteur
    ajouté demain entre dans ce test sans que personne n'y pense."""
    stub_authz(monkeypatch)
    champs = _sentinelles(connector)
    _pose(monkeypatch, connector, champs)
    code, out = _lire(connector, query=_scope_lisible(connector))
    assert code == 200, out
    corps = repr(out)
    secrets = [f.name for f in providers.REGISTRY[connector].secret_fields if f.secret]
    for nom in secrets:
        assert champs[nom] not in corps, (
            f"{connector}.{nom} : la valeur du champ SECRET est sortie du serveur")


@pytest.mark.parametrize("connector", _connecteurs_a_secret())
def test_la_cle_dun_champ_secret_est_absente_pas_vide(monkeypatch, paliers, connector):
    """`null` ou `""` serait pire que rien : un appelant qui lisait la valeur croirait
    « pas de clé posée » et continuerait — c'est ainsi qu'une variable d'environnement
    part vide sans que personne ne le voie."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, connector, _sentinelles(connector))
    _, out = _lire(connector, query=_scope_lisible(connector))
    for f in providers.REGISTRY[connector].secret_fields:
        if f.secret:
            assert f.name not in out, (
                f"{connector}.{f.name} : la clé est présente (valeur {out[f.name]!r}) — "
                "un champ secret doit être ABSENT, pas vidé")


# --- 2. Ce qui remplace la valeur -------------------------------------------

def test_lempreinte_nomme_le_champ_sans_rien_en_dire(monkeypatch, paliers):
    """Ce que le front demandait (`•••• 3f7a`), sans donner un morceau de secret :
    quatre caractères d'un HMAC, jamais quatre caractères de la clé."""
    stub_authz(monkeypatch)
    cle = "sk-live-0123456789abcdef3f7a"
    _pose(monkeypatch, "serper", {"key": cle})
    code, out = _lire("serper")
    assert code == 200, out
    empreintes = out["read_fingerprints"]
    assert set(empreintes) == {"key"}
    assert _HEX4.match(empreintes["key"]), empreintes
    assert empreintes["key"] not in cle, "l'empreinte est un morceau de la clé"


def test_lempreinte_est_stable_et_suit_la_valeur(monkeypatch, paliers):
    """Stable d'une lecture à l'autre (sinon elle ne reconnaît rien), différente dès que
    la clé change (sinon elle ne dit pas qu'on a roté)."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, "serper", {"key": "K-UN"})
    _, a = _lire("serper")
    _, b = _lire("serper")
    assert a["read_fingerprints"] == b["read_fingerprints"]
    _pose(monkeypatch, "serper", {"key": "K-DEUX"})
    _, c = _lire("serper")
    assert c["read_fingerprints"]["key"] != a["read_fingerprints"]["key"]


def test_la_meme_cle_a_deux_endroits_na_pas_la_meme_empreinte(monkeypatch, paliers):
    """L'empreinte est LIÉE à sa ligne de coffre. Sans ça, un admin qui lit l'empreinte
    d'un palier et pose un candidat ailleurs disposerait d'un oracle de confirmation à
    1/65536 — quatre caractères suffisent à valider une clé devinée par ailleurs."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, "serper", {"key": "MEME-CLE"})
    _, membre = _lire("serper")
    _, org = _lire("serper", query=b"scope=org")
    assert membre["read_fingerprints"]["key"] != org["read_fingerprints"]["key"]


def test_la_pose_est_datee_et_signee(monkeypatch, paliers):
    """« Reconnaître une clé sans la lire » : présence, quand, par qui."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, "serper", {"key": "K"})
    _, out = _lire("serper")
    assert out["configured"] is True
    assert out["read_set_at"] == "2026-08-30T09:15:00+00:00"
    assert out["read_set_by"] == SUB_POSEUR


def test_un_champ_secret_vide_na_pas_dempreinte(monkeypatch, paliers):
    """Une empreinte sur du vide dirait « il y a quelque chose » — et serait la MÊME
    pour tous les champs vides de la même ligne."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, "api", {"base_url": "https://api.test", "auth_mode": "bearer",
                                "token": ""})
    _, out = _lire("api", query=b"scope=org")
    assert "token" not in out["read_fingerprints"]


# --- 3. Ce qui ne change pas : les champs non secrets ------------------------

def test_les_champs_non_secrets_sortent_toujours(monkeypatch, paliers):
    """La modification partielle de #448 tenait aux champs NON secrets — jamais à la
    révélation d'un secret. Retirer l'une ne touche pas l'autre."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, "api", {"base_url": "http://172.16.16.3:8097",
                                "auth_mode": "bearer", "token": "TOK"})
    code, out = _lire("api", query=b"scope=org")
    assert code == 200, out
    assert out["base_url"] == "http://172.16.16.3:8097"
    assert out["auth_mode"] == "bearer"
    assert "token" not in out


# --- 4. Demander la valeur reçoit un refus NOMMÉ ----------------------------

def test_demander_la_revelation_est_refuse_par_son_nom(monkeypatch, paliers):
    """Un 200 au corps amputé laisserait l'appelant conclure « pas de clé ». Le refus
    dit ce qui s'est passé et où aller."""
    stub_authz(monkeypatch)
    _pose(monkeypatch, "serper", {"key": "K"})
    code, out = _lire("serper", query=b"reveal=true")
    assert code == 403, out
    assert out["error"] == "secret_never_revealed"
    assert "SENTINELLE" not in repr(out) and "K" == "K"


def test_ne_pas_demander_la_revelation_reste_le_chemin_normal(monkeypatch, paliers):
    stub_authz(monkeypatch)
    _pose(monkeypatch, "serper", {"key": "K"})
    code, _ = _lire("serper", query=b"reveal=false")
    assert code == 200


# --- 5. Le registre ne peut plus déclarer un champ révélable -----------------

def test_le_registre_na_plus_de_cran_revelable():
    """Le cran retiré, pas neutralisé : un `reveal=True` laissé dans une déclaration
    future doit CASSER à l'import, pas être ignoré en silence."""
    with pytest.raises(TypeError):
        providers.CredentialField("key", "API key", secret=True, reveal=True)
    assert not hasattr(providers.CredentialField("key", "API key"), "reveal")


def test_aucun_champ_du_registre_ne_porte_encore_le_cran():
    assert [f.name for c in providers.REGISTRY.values() for f in c.secret_fields
            if hasattr(f, "reveal")] == []


# --- 6. L'autre face : il n'y en a pas ---------------------------------------

def test_la_lecture_de_credential_na_aucune_face_mcp():
    """Un secret ne passe pas en argument d'outil — et l'empreinte non plus n'a pas à
    entrer dans le contexte d'un modèle. La règle se vérifie, elle ne se raconte pas."""
    for cle in ("me.credential.get", "me.credential.set", "me.credential.clear"):
        assert cap(cle).mcp is None, f"{cle} a acquis une face MCP"


def test_les_paliers_partages_nechottent_toujours_rien(monkeypatch):
    """`PUT /api/orgs/{id}/secrets/{provider}` n'a jamais rendu ce qu'il écrit : on le
    grave ici pour que le durcissement du GET ne soit pas contourné par l'écriture."""
    for cle in ("org.secret.set", "group.secret.set"):
        champs = set(cap(cle).Output.model_fields)
        assert not (champs & {"api_key", "fields", "key", "secret"}), cle
