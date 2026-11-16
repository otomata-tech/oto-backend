"""Jetons API et clés plateforme, en capacités : et la garde qui les y retenait.

Neuf routes ont quitté `api_routes_admin.py` (module supprimé) et
`api/datastore.py` pour `capabilities/api_tokens.py` (27/08).

**Le cœur de ce fichier est le premier test.** Les six routes de jetons portaient
`allow_api_token=False` — un jeton `oto_` ne peut ni lister, ni créer, ni révoquer de
jeton, sinon une fuite s'auto-entretient : révoquer le jeton fuité ne suffit plus,
l'attaquant s'en est fait un second, non expirant. `_rest_adapter` ne savait pas
exprimer ce cran, et c'est LA raison pour laquelle ces six routes étaient restées
écrites à la main. Le migrer sans le porter aurait été une régression de sécurité — on
vérifie donc que le drapeau PART vraiment vers `authenticate`, sur les six et sur elles
seules.

Le reste garde trois asymétries membre/admin, toutes servies telles quelles :
201 vs 200 à la création, `{ok}` vs `{ok, id}` à la suppression, et le contrôle de
visibilité des tableaux qui n'existe qu'au palier membre.
"""
from __future__ import annotations

import json

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from _datastore_rest import cap, call, stub_authz

from oto_mcp import credentials_store
from oto_mcp.datastore import core as datastore
from oto_mcp.capabilities import _rest_adapter, api_tokens as at

_JETONS = [{"id": 7, "label": "cli", "created_at": "2026-08-01", "last_used_at": None,
            "expires_at": None, "scopes": None, "revoked_at": None,
            "revoked_by": None, "revoked_reason": None, "parent_id": None}]
_PORTEE = {"namespaces": {"clients": "read"}}


# --- 1. LA garde : un jeton ne fabrique pas de jeton ------------------------

_SANS_JETON = ("me.token.list", "me.token.create", "me.token.delete",
               "platform.token.list", "platform.token.create", "platform.token.delete")
_AVEC_JETON = ("platform.key.list", "platform.key.create", "platform.key.delete")


@pytest.mark.parametrize("cle", _SANS_JETON)
def test_les_six_routes_de_jetons_refusent_un_porteur_de_jeton(cle):
    """On n'inspecte pas le descripteur : on JOUE la route et on lit ce qui arrive à
    `authenticate`. Un test qui relit `binding.allow_api_token` prouverait que le champ
    est posé, pas qu'il est APPLIQUÉ — or c'est l'application qui est la garde."""
    recu = _jouer_et_capturer_auth(cle)
    attendu = {"allow_api_token": False}
    if cle.startswith("me."):
        # Le jeton ÉMETTEUR, et lui seul, franchit les routes de SES jetons
        # (`test_jeton_emetteur.py`) — jamais celles du palier admin.
        attendu["allow_issuer_token"] = True
    assert recu == attendu, (
        f"{cle} n'interdit plus le porteur de jeton : reçu {recu}. Un jeton qui peut "
        "en créer d'autres rend sa fuite auto-entretenue.")


@pytest.mark.parametrize("cle", _AVEC_JETON)
def test_les_cles_plateforme_n_heritent_pas_de_la_garde(cle):
    """Le cran est posé route par route, pas en bloc : les clés plateforme n'ont aucune
    raison de refuser un jeton, et l'adaptateur ne doit donc RIEN passer pour elles."""
    assert _jouer_et_capturer_auth(cle) == {}


def _jouer_et_capturer_auth(cle: str) -> dict:
    """Joue la route de `cle` avec un `authenticate` qui enregistre ses kwargs."""
    vus: dict = {}

    async def _auth(_req, _verifier, **kw):
        vus.update(kw)
        return None, JSONResponse({"error": "stop"}, status_code=401)   # on s'arrête là

    c = cap(cle)
    binding = c.rest_bindings()[0]
    handler = _rest_adapter._make_handler(
        c, binding, None, _auth,
        lambda _r, p, status=200: JSONResponse(p, status_code=status),
        lambda _r, s, code, d=None: JSONResponse({"error": code}, status_code=s))

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "method": binding.verb, "path": binding.path,
                   "query_string": b"", "headers": [], "path_params": {}},
                  receive=_receive)
    import asyncio
    asyncio.run(handler(req))
    return vus


# --- Socle ------------------------------------------------------------------

@pytest.fixture()
def socle(monkeypatch):
    vus: list = []
    monkeypatch.setattr(at.db, "get_user", lambda sub: {"sub": sub})
    monkeypatch.setattr(at.db, "list_api_tokens",
                        lambda sub, include_revoked=False, parent_id=None:
                        vus.append(("list", sub, include_revoked)) or list(_JETONS))
    monkeypatch.setattr(at.db, "create_api_token",
                        lambda sub, label=None, ttl_days=None, scopes=None,
                        parent_id=None, verrou_org=False, verrou_org_id=None:
                        vus.append(("create", sub, label, ttl_days, scopes)) or "oto_SECRET")
    monkeypatch.setattr(at.db, "revoke_api_token",
                        lambda sub, tid, revoked_by, reason, parent_id=None:
                        vus.append(("revoke", sub, tid, revoked_by, reason)) or True)
    monkeypatch.setattr(at.db, "KEY_PROVIDERS", {"serper", "apollo"})
    monkeypatch.setattr(credentials_store, "list_platform_credentials",
                        lambda provider=None: [{"provider": "serper", "label": "prod",
                                                "set_at": "2026-08-01"}])
    monkeypatch.setattr(credentials_store, "set_credential",
                        lambda et, eid, conn, secret, set_by=None, **kw:
                        vus.append(("set", et, eid, conn, set_by)))
    monkeypatch.setattr(credentials_store, "clear_credential",
                        lambda et, eid, conn, **kw: vus.append(("clear", et, eid, conn)) or True)
    monkeypatch.setattr(credentials_store, "set_platform_key",
                        lambda label, conn, secret, set_by=None, meta=None:
                        vus.append(("set", credentials_store.PLATFORM, label, conn, set_by)))
    # Pas de base : l'écriture partielle (`merge_with_existing`) ne trouve rien à compléter.
    monkeypatch.setattr(credentials_store, "get_credential_with_meta",
                        lambda *a, **kw: None)

    class _Store:
        def list_datastores(self):
            return [{"id": 12, "datastore": "clients"},
                    {"id": 13, "datastore": "doublon"}, {"id": 14, "datastore": "doublon"}]

    monkeypatch.setattr(datastore, "make_store", lambda sub: _Store())
    return vus


@pytest.fixture()
def super_admin(monkeypatch):
    stub_authz(monkeypatch)
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "is_super_admin", lambda sub: True)


# --- 2. Les trois asymétries membre / admin ---------------------------------

def test_la_creation_membre_rend_201_et_l_admin_200(monkeypatch, socle, super_admin):
    """Forme historique de chaque palier. « Harmoniser » casserait un appelant."""
    assert call("me.token.create", body={})[0] == 201
    assert call("platform.token.create", path_params={"sub": "u-9"}, body={})[0] == 200


def test_la_suppression_membre_rend_ok_seul_l_admin_rend_l_id(monkeypatch, socle,
                                                              super_admin):
    assert call("me.token.delete", path_params={"token_id": "7"})[1] == {"ok": True}
    assert call("platform.token.delete",
                path_params={"sub": "u-9", "token_id": "7"})[1] == {"ok": True, "id": 7}


@pytest.mark.parametrize("cle,pp", [("me.token.create", {}),
                                    ("platform.token.create", {"sub": "u-9"})])
def test_un_tableau_que_le_PORTEUR_ne_voit_pas_est_refuse(monkeypatch, socle,
                                                          super_admin, cle, pp):
    """Un tableau que le porteur ne voit pas est refusé : sinon le jeton serait muet
    et on le croirait branché. Depuis oto#158 le palier admin aussi : la portée se
    RANGE par identifiant, dans le catalogue du porteur — pas de l'émetteur — et un
    nom qui n'y désigne rien n'a pas d'identifiant à ranger."""
    portee = {"namespaces": {"inconnu": "read"}}
    code, out = call(cle, path_params=pp, body={"scopes": portee})
    assert code == 400 and out["error"] == "unknown_namespace"
    assert "inconnu" in out["detail"]


def test_un_nom_est_RANGE_sous_son_identifiant_et_la_reponse_le_rend(monkeypatch, socle):
    """oto#158 : la portée nomme un tableau par son identifiant — un renommage ne
    déplace plus ce que le jeton atteint. L'émission accepte encore un nom le temps du
    préavis, et rend la portée RANGÉE : c'est ce que l'appelant doit envoyer ensuite."""
    stub_authz(monkeypatch)
    code, out = call("me.token.create",
                     body={"scopes": {"namespaces": {"clients": "read"}}})
    assert code == 201, out
    assert out["scopes"] == {"namespaces": {"12": "read"}}
    assert socle[-1][4] == out["scopes"], "la portée rangée est celle qui est stockée"
    code, out = call("me.token.create", body={"scopes": {"namespaces": {"12": "read"}}})
    assert code == 201 and out["scopes"] == {"namespaces": {"12": "read"}}


def test_un_nom_que_portent_DEUX_tableaux_est_refuse_jamais_tranche(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.token.create",
                     body={"scopes": {"namespaces": {"doublon": "read"}}})
    assert code == 400 and out["error"] == "ambiguous_namespace"
    assert socle == []


@pytest.mark.parametrize("portee", [{"projects": {"12": "read"}}, {"runner": True}])
def test_une_portee_SANS_tableau_s_emet_quand_meme(monkeypatch, socle, portee):
    """`parse` rend légitimement une portée qui ne nomme AUCUN tableau — `projects` ou
    `runner` seuls. Or le contrôle de visibilité ne concerne QUE les tableaux : indexer
    `datastores` sans garde faisait un 500 d'une émission parfaitement valide.

    Pourquoi ce test vit ici et pas dans le banc des portées : `test_portee_runner.py`
    n'exerce que `parse` et `authorize` — la DÉCISION, jamais l'ÉMISSION. C'est ce trou
    qui a laissé passer le défaut, donc on joue la ROUTE, et on vérifie que la portée
    arrive INTACTE au stockage, pas seulement que le code de retour est bon.
    """
    stub_authz(monkeypatch)
    code, out = call("me.token.create", body={"scopes": portee})
    assert code == 201, f"une portée sans tableau ne s'émet plus : {code} {out}"
    assert out["scopes"] == portee
    assert socle[-1][0] == "create" and socle[-1][4] == portee, (
        f"la portée n'est pas arrivée au stockage : {socle[-1]}")


# --- 3. Le secret, et ce qui n'en sort pas ----------------------------------

def test_le_secret_n_est_rendu_qu_a_la_creation(monkeypatch, socle):
    """La liste ne porte JAMAIS le secret : il n'est stocké que haché. Le déclarer dans
    `ApiToken` par erreur aurait fait générer chez un intégrateur un champ qu'il aurait
    attendu en vain."""
    stub_authz(monkeypatch)
    code, out = call("me.token.list")
    assert code == 200
    assert set(out["tokens"][0]) == set(at.ApiToken.model_fields)
    assert "token" not in out["tokens"][0]
    assert call("me.token.create", body={})[1]["token"] == "oto_SECRET"


def test_un_libelle_trop_long_est_refuse_en_DISANT_la_mesure(monkeypatch, socle):
    """oto#42, 4ᵉ règle : une coupe sur une ÉCRITURE se refuse. Le libellé partait en
    base en `strip()[:32]`, sans que rien ne le dise — or il n'existe que pour
    reconnaître le jeton le jour où l'on décide de le révoquer, et la coupe emporte
    précisément ce qui le distingue de ses voisins.

    Le refus doit porter les DEUX nombres : la mesure atteinte et la borne. Sans la
    première, l'émetteur raccourcit à l'aveugle (leçon du datastore, signal #383)."""
    stub_authz(monkeypatch)
    brut = "  un libellé vraiment très long au-delà de trente-deux  "
    code, out = call("me.token.create", body={"label": brut})
    assert (code, out["error"]) == (400, "label_too_long")
    assert str(len(brut.strip())) in out["detail"] and "32" in out["detail"]
    # Et RIEN n'est écrit : un refus qui laisserait passer le geste serait pire que la
    # coupe — il rendrait une erreur ET un jeton.
    assert socle == []


def test_la_reponse_rend_le_libelle_ECRIT_jamais_le_brut(monkeypatch, socle):
    """Le banc que ce test remplace figeait l'inverse : réponse = brut de l'appelant,
    base = `strip()[:32]`. Il défendait la STABILITÉ de la réponse (« éviter qu'un
    nettoyage la change »), pas la coupe — cette intention est tenue ici, en plus
    strict : la réponse est exactement ce que la couche d'écriture a reçu, donc elle ne
    peut plus décrire une valeur qui n'existe nulle part."""
    stub_authz(monkeypatch)
    _, out = call("me.token.create", body={"label": "  runner prod  "})
    assert out["label"] == "runner prod" == socle[0][2]


def test_a_la_borne_exacte_le_libelle_arrive_INTACT(monkeypatch, socle):
    """Versant positif : la garde ne doit pas mordre AVANT la borne, ni raboter à
    l'espace près. C'est l'argument reçu par `create_api_token` qui est inspecté, pas
    la sortie de la doublure."""
    stub_authz(monkeypatch)
    pile = "x" * at._LABEL_MAX
    _, out = call("me.token.create", body={"label": pile})
    assert socle[0][2] == pile and out["label"] == pile


def test_un_libelle_absent_ou_blanc_vaut_cli(monkeypatch, socle):
    """`or "cli"` deux fois dans `_libelle` : absent, et vide-après-strip. Le second
    cas produisait un libellé VIDE avant le 03/09 — un jeton sans nom dans la liste où
    l'on révoque."""
    stub_authz(monkeypatch)
    for corps in ({}, {"label": "   "}):
        socle.clear()
        _, out = call("me.token.create", body=corps)
        assert out["label"] == "cli" == socle[0][2]


def test_une_cle_plateforme_ne_rend_jamais_son_secret(monkeypatch, socle, super_admin):
    code, out = call("platform.key.list")
    assert code == 200
    assert set(out["platform_keys"][0]) == set(at.PlatformKey.model_fields)
    assert "api_key" not in out["platform_keys"][0]


# --- 4. Les refus, avec leur code servi -------------------------------------

@pytest.mark.parametrize("cle,pp", [("me.token.delete", {"token_id": "zz"}),
                                    ("platform.token.delete",
                                     {"sub": "u-9", "token_id": "zz"})])
def test_un_id_illisible_rend_invalid_id_pas_invalid_input(monkeypatch, socle,
                                                           super_admin, cle, pp):
    """Pydantic dirait `invalid_input` : `token_id` est donc déclaré en TEXTE et converti
    au handler, pour garder le code que la console lit."""
    code, out = call(cle, path_params=pp)
    assert code == 400 and out["error"] == "invalid_id"


def test_un_jeton_inconnu_est_un_404(monkeypatch, socle):
    stub_authz(monkeypatch)
    monkeypatch.setattr(at.db, "revoke_api_token", lambda sub, tid, **kw: False)
    code, out = call("me.token.delete", path_params={"token_id": "7"})
    assert code == 404 and out["error"] == "unknown_token"


def test_emettre_pour_un_compte_inexistant_est_refuse(monkeypatch, socle, super_admin):
    monkeypatch.setattr(at.db, "get_user", lambda sub: None)
    for cle, pp in (("platform.token.list", {"sub": "u-9"}),
                    ("platform.token.create", {"sub": "u-9"})):
        code, out = call(cle, path_params=pp, body={})
        assert (code, out["error"]) == (404, "unknown_user")


@pytest.mark.parametrize("corps,erreur", [
    ({"provider": "zzz", "label": "p", "api_key": "K"}, "invalid_provider"),
    ({"provider": "serper", "label": "p"}, "missing_fields"),
    ({"provider": "serper", "api_key": "K"}, "missing_fields"),
    ({}, "invalid_provider"),
])
def test_les_refus_de_pose_d_une_cle_plateforme(monkeypatch, socle, super_admin,
                                                corps, erreur):
    code, out = call("platform.key.create", body=corps)
    assert code == 400 and out["error"] == erreur
    assert socle == [], "rien ne doit être écrit quand la pose est refusée"


def test_un_coffre_qui_refuse_le_provider_est_nomme(monkeypatch, socle, super_admin):
    def _boum(*a, **kw):
        raise ValueError("serper n'accepte pas de clé plateforme")

    monkeypatch.setattr(credentials_store, "set_platform_key", _boum)
    code, out = call("platform.key.create",
                     body={"provider": "serper", "label": "p", "api_key": "K"})
    assert code == 400 and out["error"] == "invalid_platform_provider"
    assert out["detail"] == "serper n'accepte pas de clé plateforme"


def test_une_cle_inconnue_est_un_404(monkeypatch, socle, super_admin):
    monkeypatch.setattr(credentials_store, "clear_credential", lambda *a, **kw: False)
    code, out = call("platform.key.delete",
                     path_params={"provider": "serper", "label": "prod"})
    assert code == 404 and out["error"] == "unknown_key"


# --- 5. `ttl_days`, tel qu'il est interprété --------------------------------

@pytest.mark.parametrize("brut,attendu", [
    (30, 30), ("30", 30),
    (-1, 90),            # str(-1) n'est pas fait de chiffres → ignoré → plafond
    ("abc", 90),
    (None, 90),
])
def test_ttl_days_n_accepte_que_des_chiffres(monkeypatch, socle, super_admin,
                                             brut, attendu):
    """Un `-1` ou un texte donnent « pas d'échéance demandée » plutôt qu'une erreur :
    c'est le comportement servi — et depuis le plafond, cela vaut 90 jours, jamais un
    jeton éternel."""
    corps = {} if brut is None else {"ttl_days": brut}
    code, out = call("platform.token.create", path_params={"sub": "u-9"}, body=corps)
    assert code == 200 and out["ttl_days"] == attendu
    assert socle[0][3] == attendu


# --- 6. Ce qui CHANGE pour un appelant --------------------------------------

@pytest.mark.parametrize("cle,pp", [("me.token.create", {}),
                                    ("platform.token.create", {"sub": "u-9"})])
def test_une_PORTEE_illisible_ne_produit_JAMAIS_un_jeton_non_porte(monkeypatch, socle,
                                                                  super_admin, cle, pp):
    """⚠️ **Le silence le plus cher de l'inventaire du 2026-08-27 (site B2), et il ne
    peut plus se reproduire ici.**

    Le patron `try: body = await request.json() / except: body = {}` transformait un
    `{"scopes": …}` mal formé en `scopes=None`, c'est-à-dire en jeton **NON PORTÉ** —
    les droits pleins du sub — à la place du jeton borné demandé ; au palier super-admin,
    non porté ET sans expiration. L'appelant recevait 200 et un secret : rien ne disait
    qu'il venait d'obtenir l'inverse de ce qu'il avait écrit.

    Le seam `json_body.read_json_body` (posé sur `main` par #459) refuse désormais un
    corps illisible, et l'adaptateur de capacités passe par lui. On le VÉRIFIE ici, sur
    les deux paliers : ce n'est pas parce qu'un seam existe qu'il est branché."""
    code, out = call(cle, path_params=pp, body=b'{"scopes": {malforme}')
    assert code == 400 and out["error"] == "invalid_json"
    assert socle == [], "aucun jeton ne doit être émis sur un corps illisible"


@pytest.mark.parametrize("cle,pp", [("me.token.create", {}),
                                    ("platform.token.create", {"sub": "u-9"})])
def test_un_corps_ABSENT_reste_un_jeton_cli_non_porte(monkeypatch, socle, super_admin,
                                                      cle, pp):
    """Le pendant du test précédent, et la raison pour laquelle le seam distingue les
    deux : corps ABSENT ⇒ `{}` ⇒ jeton `cli` non porté. C'est le contrat, pas un
    silence — durcir ça casserait tous les appelants qui postent sans corps."""
    code, out = call(cle, path_params=pp, body=None)
    assert code in (200, 201)
    assert out["label"] == "cli" and out["scopes"] is None


def test_un_corps_illisible_est_refuse_sur_la_pose_de_cle(monkeypatch, socle,
                                                         super_admin):
    """Le seam rend le MÊME `400 invalid_json` que la route écrite à la main : c'est un
    écart que ce chantier avait dû signaler, et que #459 a supprimé des deux côtés."""
    code, out = call("platform.key.create", body=b"{pas du json")
    assert code == 400 and out["error"] == "invalid_json"
    assert socle == []


def test_un_corps_non_objet_est_refuse_sur_la_pose_de_cle(monkeypatch, socle,
                                                          super_admin):
    """`invalid_body` — une liste n'a pas de champs à fusionner. La route d'origine
    rendait déjà ce code exact."""
    code, out = call("platform.key.create", body=[1, 2])
    assert code == 400 and out["error"] == "invalid_body"
    assert socle == []


def test_un_champ_inconnu_est_desormais_refuse(monkeypatch, socle, super_admin):
    code, out = call("platform.key.create",
                     body={"provider": "serper", "label": "p", "api_key": "K",
                           "apiKey": "K"})
    assert code == 400
    assert out["error"] == "unknown_fields" and "apiKey" in out["detail"]


# --- 7. `ttl_days` au palier MEMBRE (#514 lot d) -----------------------------

def test_le_palier_MEMBRE_pose_une_echeance(monkeypatch, socle, super_admin):
    """#514 lot (d), décision d'Alexis du 04/09/2026 : « expiration pour tout le monde ».

    Le champ était accepté du seul palier admin et IGNORÉ du palier membre. Quelqu'un
    qui voulait borner son propre jeton devait donc passer par un opérateur : la
    précaution existait sans être à sa portée — le motif même de #514, où un jeton émis
    depuis le dashboard ouvrait 35 tableaux en écriture parce que la seule voie sûre
    était impraticable."""
    code, out = call("me.token.create", body={"ttl_days": 30})
    assert code == 201
    assert out["ttl_days"] == 30, "la réponse doit RENDRE l'échéance"
    # …et surtout : elle doit atteindre l'écriture, pas seulement l'écho.
    assert socle[0][3] == 30, (
        "l'échéance n'est pas arrivée au store : le champ serait accepté-inerte, "
        "c'est-à-dire pire qu'absent — on croirait le jeton borné")


def test_l_echeance_membre_se_LIT_a_la_creation_ou_jamais(monkeypatch, socle, super_admin):
    """Sans échéance demandée : le plafond (90 jours), RENDU et ÉCRIT — c'est le SEUL
    instant où l'information est lisible : la liste ne rend pas le secret, et le
    porteur ne saura jamais de lui-même quand son jeton cesse de fonctionner."""
    code, out = call("me.token.create", body={})
    assert code == 201 and out["ttl_days"] == 90
    assert socle[0][3] == 90, "le plafond doit atteindre l'écriture, pas seulement l'écho"


@pytest.mark.parametrize("brut,attendu", [(30, 30), ("30", 30), (-1, 90), ("abc", 90)])
def test_les_DEUX_paliers_lisent_l_echeance_pareil(monkeypatch, socle, super_admin,
                                                   brut, attendu):
    """⚠️ La même tolérance des deux côtés, et c'est le point du lot : la règle vivait
    en une ligne dans le seul handler admin. L'ouvrir au membre en la RECOPIANT aurait
    fait deux lectures d'un même champ — elles auraient divergé au premier durcissement,
    et le contrat servi aurait dépendu du palier appelé. Une seule fonction (`_jours`).

    On garde `-1` → « pas d'échéance demandée » (donc le plafond de 90 jours) : c'est
    la lecture déjà servie côté admin, et le lot ouvre un palier, il ne durcit pas
    l'autre."""
    membre = call("me.token.create", body={"ttl_days": brut})[1]
    admin = call("platform.token.create", path_params={"sub": "u-9"},
                 body={"ttl_days": brut})[1]
    assert membre["ttl_days"] == admin["ttl_days"] == attendu


def test_un_connecteur_non_keyed_en_mode_platform_accepte_une_cle_plateforme(
        monkeypatch, socle, super_admin):
    """`transcription` n'est pas `keyed` (multi-champs) mais déclare `platform` : la
    route de pose ne le refuse plus `invalid_provider` (ADR 0074)."""
    code, out = call("platform.key.create",
                     body={"provider": "transcription", "label": "oto", "api_key": "K"})
    assert code == 200 and out["provider"] == "transcription"
    assert [v[:4] for v in socle if v[0] == "set"] == [
        ("set", "platform", "oto", "transcription")]


def _pose_capturee(monkeypatch):
    poses = []
    monkeypatch.setattr(credentials_store, "set_platform_key",
                        lambda label, conn, secret, set_by=None, meta=None:
                        poses.append((conn, label, secret, meta)))
    return poses


def test_une_cle_seule_prend_la_forme_du_coffre_a_la_pose(monkeypatch, socle, super_admin):
    """`forager` porte deux champs au coffre (clé + compte). La console n'envoie que
    `api_key` : la pose la range sous le premier champ secret, au format que la
    lecture (`unpack_secret`) relit. Stockée brute, elle ne se relisait pas."""
    import json
    poses = _pose_capturee(monkeypatch)
    code, _ = call("platform.key.create",
                   body={"provider": "forager", "label": "oto", "api_key": "fg-K"})
    assert code == 200
    ((conn, label, secret, meta),) = poses
    assert (conn, label, meta) == ("forager", "oto", None)
    assert json.loads(secret) == {"api_key": "fg-K"}
    assert credentials_store.unpack_secret("forager", secret) == {"api_key": "fg-K"}


def test_les_champs_d_un_multi_champs_se_posent_par_fields(monkeypatch, socle, super_admin):
    import json
    poses = _pose_capturee(monkeypatch)
    code, _ = call("platform.key.create",
                   body={"provider": "forager", "label": "oto",
                         "fields": {"api_key": "fg-K", "account_id": "42"}})
    assert code == 200
    assert json.loads(poses[0][2]) == {"api_key": "fg-K", "account_id": "42"}


def test_un_champ_requis_manquant_est_nomme(monkeypatch, socle, super_admin):
    poses = _pose_capturee(monkeypatch)
    code, out = call("platform.key.create",
                     body={"provider": "forager", "label": "oto",
                           "fields": {"account_id": "42"}})
    assert code == 400 and out["error"] == "missing_credentials"
    assert poses == []


def test_un_mono_champ_reste_la_valeur_brute(monkeypatch, socle, super_admin):
    poses = _pose_capturee(monkeypatch)
    code, _ = call("platform.key.create",
                   body={"provider": "serper", "label": "oto", "api_key": "sk-1"})
    assert code == 200 and poses[0][2] == "sk-1"


# --- #523 : la révocation laisse une trace -----------------------------------

def test_revoquer_trace_QUI_et_POURQUOI(monkeypatch, socle, super_admin):
    """Révoquer était un `DELETE` sans trace. L'auteur est l'APPELANT (pas le
    titulaire du jeton), le motif est celui du corps du DELETE."""
    code, _ = call("me.token.delete", path_params={"token_id": "7"},
                   body={"reason": "  fuite  "}, sub="u-1")
    assert code == 200
    code, _ = call("platform.token.delete",
                   path_params={"sub": "u-cible", "token_id": "8"}, sub="u-admin")
    assert code == 200
    revocations = [v for v in socle if v[0] == "revoke"]
    assert revocations == [("revoke", "u-1", 7, "u-1", "fuite"),
                           ("revoke", "u-cible", 8, "u-admin", None)]


def test_un_motif_trop_long_est_refuse_jamais_rabote(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.token.delete", path_params={"token_id": "7"},
                     body={"reason": "x" * (at._REASON_MAX + 1)})
    assert (code, out.get("error")) == (400, "reason_too_long")
    assert not [v for v in socle if v[0] == "revoke"]


@pytest.mark.parametrize("cle,pp", [("me.token.list", {}),
                                    ("platform.token.list", {"sub": "u-cible"})])
def test_les_revoques_ne_sont_listes_que_sur_demande(monkeypatch, socle, super_admin,
                                                      cle, pp):
    call(cle, path_params=pp)
    call(cle, path_params=pp, query=b"include_revoked=true")
    assert [v[2] for v in socle if v[0] == "list"] == [False, True]


# --- 8. Le plafond de 90 jours ------------------------------------------------

@pytest.mark.parametrize("cle,pp", [("me.token.create", None),
                                    ("platform.token.create", {"sub": "u-9"})])
def test_une_echeance_au_dela_du_plafond_est_refusee_nommement(monkeypatch, socle,
                                                               super_admin, cle, pp):
    """91 jours : 400 `token_ttl_too_long`, rien n'est émis — jamais un jeton raboté,
    qui cesserait de fonctionner avant la date que son porteur croit."""
    code, out = call(cle, path_params=pp, body={"ttl_days": 91})
    assert code == 400 and out["error"] == "token_ttl_too_long", out
    assert not [e for e in socle if e[0] == "create"]


@pytest.mark.parametrize("cle,pp", [("me.token.create", None),
                                    ("platform.token.create", {"sub": "u-9"})])
def test_le_plafond_lui_meme_est_accepte(monkeypatch, socle, super_admin, cle, pp):
    code, out = call(cle, path_params=pp, body={"ttl_days": 90})
    assert code in (200, 201) and out["ttl_days"] == 90
    assert socle[-1][3] == 90
