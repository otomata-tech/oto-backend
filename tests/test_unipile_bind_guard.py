"""#559 → #581 — un `account_id` ne se lie pas sur parole, et il n'y a plus qu'UN chemin.

La clé Unipile de la plateforme est **partagée entre les organisations** : elle adresse
tout l'abonnement. Un `account_id` accepté sans contrôle sur ce socle-là, c'est une
frontière entre organisations qui tient à la bonne foi d'un corps de requête.

**#559 (PR #577)** : deux chemins écrivaient la MÊME liaison — le webhook de
notification et la réconciliation poll-and-bind — et un seul contrôlait. La garde a été
mise dans une seule fonction, `unipile_connect.bind_account`, que les deux appelaient.

**#581 (2026-08-29)** : le webhook était DORMANT — le fournisseur ne rappelle plus le
callback `notify_url` depuis sa v2 (le champ n'existe plus) ; zéro appel sur le mois de
journal retenu — et une route non authentifiée sans appelant légitime est une surface,
pas une fonctionnalité. Elle est retirée. Ce fichier tient donc deux choses :

1. l'ABSENCE : la route rend 404 sur la table servie, et le cliquet AST des écrivains de
   liaison — les écritures directes ET les appelants de la garde — a perdu l'écrivain
   webhook. Une route qui renaîtrait à ce chemin rougit ici et dans la table de routes
   figée ; un appelant nouveau de la garde se déclare ici, avec sa raison ;
2. la GARDE, conservée au point d'écriture pour le chemin vivant : `bind_account`
   refuse le compte d'un tiers (ligne vivante ou morte) sans rien écrire, lie ce qui est
   attendu (un identifiant libre, une ligne morte du réclamant), et la réconciliation
   passe par elle. Contre un VRAI PostgreSQL — la garde EST une requête, la stubber ne
   prouverait que le stub.
"""
from __future__ import annotations

import ast
import pathlib
import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

RACINE = pathlib.Path(__file__).resolve().parent.parent
VICTIME = "usr_victime_559"
PIRATE = "usr_pirate_559"
ACC_VICTIME = "acc_de_la_victime"
WEBHOOK = "/api/unipile/webhook"


# ─── 1. L'absence : la route ne répond plus ──────────────────────────────────

class _FakeVerifier:
    """`make_routes` ne fait que CAPTURER le verifier : un objet nu suffit."""


def test_la_route_du_webhook_ne_repond_plus():
    """La table SERVIE, pas un grep : c'est `api.routes.make_routes` qui décide de ce
    qui existe. Et 404, rien d'autre — pas un 200 d'ack, pas une redirection d'alias."""
    from oto_mcp.api import routes as api_routes

    table = list(api_routes.make_routes(_FakeVerifier(), mcp_instance=None))
    assert not [r for r in table if getattr(r, "path", None) == WEBHOOK], (
        "la route du webhook de liaison est de retour — elle a été retirée (#581) : "
        "dormante depuis la v2 du fournisseur, sans appelant légitime. Un webhook "
        "d'application v2 est une AUTRE route, vérifiée par signature.")
    r = TestClient(Starlette(routes=table)).post(
        WEBHOOK, content='{"status":"CREATION_SUCCESS","name":"x","account_id":"y"}',
        headers={"content-type": "application/json"})
    assert r.status_code == 404


# ─── 2. Le cliquet structurel : les listes FERMÉES des écrivains ─────────────

# Qui a le droit d'écrire une liaison en s'adressant DIRECTEMENT à la base — donc sans
# passer par la garde. La liste est fermée et chaque entrée porte sa raison. Un
# quatrième nom qui apparaît ici est exactement ce qui s'est produit avec #559 : une
# écriture parallèle née sans la garde de sa voisine.
_ECRIVAINS_DIRECTS = {
    # L'écrivain GARDÉ — celui par lequel tout identifiant venu d'un tiers doit passer.
    ("oto_mcp/unipile_binding.py", "bind_account"),
    # ADOPTION : l'identifiant sort d'une ligne que la base attribue déjà à ce `sub`
    # (`seat_binding_elsewhere` filtre dessus). Rien d'extérieur à confronter.
    ("oto_mcp/unipile_connect.py", "hosted_auth_url"),
    # BASCULE BYO : l'identifiant vient bien d'un appelant, mais il doit exister sur
    # SA PROPRE clé (`cli.list_accounts()`), et le sélecteur REFUSE la clé plateforme
    # (`_unipile_client` rend None hors BYO). Le socle partagé — d'où vient #559 —
    # n'est donc pas atteignable par là. La question « et deux membres d'une même org
    # sur une clé BYO commune ? » reste ouverte, et se traite ailleurs qu'ici : lui
    # appliquer cette garde refuserait un compte d'org délibérément partagé.
    ("oto_mcp/connectors/identities.py", "_unipile_select"),
}

# Qui a le droit d'appeler l'écrivain GARDÉ. Un seul : la réconciliation. L'écrivain
# webhook (`api/connectors.py::_bind_from_webhook`) a été retiré le 2026-08-29 (#581).
# Un nom qui apparaît ici est un nouveau chemin de liaison : il se déclare avec sa
# raison — et s'il est anonyme (un webhook d'application v2), il se vérifie par
# signature AVANT d'arriver ici.
_APPELANTS_DE_LA_GARDE = {
    ("oto_mcp/unipile_connect.py", "reconcile_pending"),
}


def _appels_du_corps(fonction):
    """Les appels du corps de `fonction`, SANS descendre dans ses fonctions internes :
    un appel se rapporte à la fonction la plus proche qui l'englobe, pas à toutes."""
    pile = list(ast.iter_child_nodes(fonction))
    while pile:
        n = pile.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(n, ast.Call):
            yield n
        pile.extend(ast.iter_child_nodes(n))


def _appelants(nom: str) -> set:
    """Relevé AST au grain de la FONCTION englobante — pas un grep, et pas un relevé
    par fichier : une allowlist par fichier blanchirait la prochaine écriture ajoutée
    dans le même module. Attrape `x.nom(...)` comme `nom(...)` nu."""
    trouves = set()
    for chemin in (RACINE / "oto_mcp").rglob("*.py"):
        arbre = ast.parse(chemin.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for appel in _appels_du_corps(noeud):
                f = appel.func
                appele = f.attr if isinstance(f, ast.Attribute) else (
                    f.id if isinstance(f, ast.Name) else None)
                if appele == nom:
                    trouves.add((str(chemin.relative_to(RACINE)), noeud.name))
    return trouves


def test_la_liste_des_ecrivains_de_liaison_est_fermee():
    trouves = _appelants("set_unipile_account")
    assert trouves == _ECRIVAINS_DIRECTS, (
        "une écriture de liaison a été ajoutée ou déplacée. Si elle prend son "
        "`account_id` d'un tiers (corps de requête, inventaire fournisseur), elle "
        "passe par `unipile_connect.bind_account` ; sinon, elle se justifie ici.\n"
        f"en trop : {sorted(trouves - _ECRIVAINS_DIRECTS)}\n"
        f"disparus : {sorted(_ECRIVAINS_DIRECTS - trouves)}")


def test_les_appelants_de_la_garde_sont_une_liste_fermee():
    """Le cliquet du retrait (#581) : l'écrivain webhook n'appelle plus la garde parce
    qu'il n'existe plus — et personne n'a pris sa place sans le dire ici."""
    trouves = _appelants("bind_account")
    assert trouves == _APPELANTS_DE_LA_GARDE, (
        "un chemin de liaison est apparu (ou a disparu). Un appelant nouveau de "
        "`bind_account` se déclare ici avec sa raison ; s'il est anonyme (webhook), il "
        "vérifie une signature AVANT d'arriver à la garde de propriété.\n"
        f"en trop : {sorted(trouves - _APPELANTS_DE_LA_GARDE)}\n"
        f"disparus : {sorted(_APPELANTS_DE_LA_GARDE - trouves)}")


# ─── 3. La garde, conservée pour le chemin vivant (base réelle) ──────────────

@pytest.fixture(scope="module")
def live(pg_dsn):
    """Base JETABLE + vrai `init_db()`, sur son propre pool.

    Base PROPRE et non le conteneur partagé : un `init_db()` dans la base de session
    y laisse ~67 tables et fait tomber des tests étrangers qui recréent la leur.
    """
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_559_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom

    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db

        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        conn.execute(sql, params)


def _rows(sql, params=()):
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


@pytest.fixture
def scene(live):
    """Deux orgs, deux personnes, et le compte de la victime DÉJÀ lié chez elle.

    La victime et le pirate sont dans des orgs différentes : c'est la frontière que
    le lot défend. Les deux orgs partagent la clé plateforme — donc `ACC_VICTIME`
    est techniquement joignable depuis l'org du pirate, et rien d'autre que cette
    garde ne l'en empêche.
    """
    _exec("DELETE FROM unipile_accounts")
    _exec("DELETE FROM unipile_pending")
    org_victime = _rows("INSERT INTO orgs (name) VALUES ('Org victime') RETURNING id")[0]["id"]
    org_pirate = _rows("INSERT INTO orgs (name) VALUES ('Org pirate') RETURNING id")[0]["id"]
    from oto_mcp import db

    db.set_unipile_account(VICTIME, ACC_VICTIME, account_name="La victime",
                           org_id=org_victime, provider="LINKEDIN", platform_seat=True)
    return {"org_victime": org_victime, "org_pirate": org_pirate}


def _pending(sub: str, org_id: int, provider: str = "LINKEDIN") -> str:
    from oto_mcp import db

    nonce = "nonce_" + uuid.uuid4().hex
    db.create_unipile_pending(nonce, sub, org_id, provider, platform_seat=True)
    return nonce


def _bind(sub: str, account_id: str, org_id: int):
    from oto_mcp import unipile_binding

    return unipile_binding.bind_account(sub, account_id, org_id=org_id,
                                        provider="LINKEDIN", platform_seat=True)


def _liaisons(sub: str) -> list[dict]:
    return _rows("SELECT account_id, org_id, disconnected_at FROM unipile_accounts "
                 "WHERE sub = %s ORDER BY org_id", (sub,))


def test_bind_account_refuse_le_compte_dun_tiers(scene):
    """Le cœur de #559, au point d'écriture : l'identifiant nommé est celui de
    quelqu'un d'autre — rien n'est écrit, ni pour le pirate ni contre la victime."""
    issue = _bind(PIRATE, ACC_VICTIME, scene["org_pirate"])
    assert (issue.bound, issue.reason) == (False, "account_not_claimable")
    assert _liaisons(PIRATE) == [], (
        "le compte d'un tiers a été lié au pirate — la clé plateforme étant partagée, "
        "il opère désormais sous le LinkedIn de la victime")
    assert [(l["account_id"], l["org_id"]) for l in _liaisons(VICTIME)] == [
        (ACC_VICTIME, scene["org_victime"])]


def test_bind_account_lie_un_identifiant_libre(scene):
    assert _bind(PIRATE, "acc_tout_neuf", scene["org_pirate"]).bound
    assert [(l["account_id"], l["org_id"]) for l in _liaisons(PIRATE)] == [
        ("acc_tout_neuf", scene["org_pirate"])]


def test_bind_account_relie_ma_propre_ligne_morte(scene):
    """Reconnexion : Unipile RÉUTILISE le compte existant (même `account_id`). Une
    ligne morte du réclamant est une preuve de propriété, pas un obstacle — sans quoi
    la garde casserait la reconnexion qu'elle prétend servir."""
    from oto_mcp import db

    db.clear_unipile_account(VICTIME, scene["org_victime"], "LINKEDIN")
    assert _bind(VICTIME, ACC_VICTIME, scene["org_victime"]).bound
    vivantes = [l for l in _liaisons(VICTIME) if l["disconnected_at"] is None]
    assert [l["account_id"] for l in vivantes] == [ACC_VICTIME]


def _client_qui_voit(comptes: list[dict]):
    class _Client:
        def list_accounts(self):
            return comptes

        def account_alive(self, _aid):
            return True

    return _Client()


def _sur_cle_plateforme(monkeypatch, comptes: list[dict]):
    from oto_mcp import access
    import oto.tools.unipile as core_unipile

    class _Cred:
        is_platform, key, config = True, "clef", {}

    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _Cred())
    monkeypatch.setattr(core_unipile, "make_unipile_client",
                        lambda **k: _client_qui_voit(comptes))


def test_la_reconciliation_refuse_le_compte_dun_tiers(scene, monkeypatch):
    """Le chemin VIVANT, de bout en bout : le compte de la victime est bien VISIBLE
    sur la clé partagée (c'est précisément ce qui rend la garde nécessaire), et la
    réconciliation ne le lie pas au pirate."""
    from oto_mcp import unipile_connect

    _sur_cle_plateforme(monkeypatch, [
        {"id": ACC_VICTIME, "provider": "linkedin",
         "created_at": "2099-01-01 00:00:00+00", "name": "La victime"}])
    _pending(PIRATE, scene["org_pirate"])
    out = unipile_connect.reconcile_pending(PIRATE)
    # ⚠️ Ce test comparait la réponse ENTIÈRE à `{bound: False, accounts: []}`. C'était
    # la FORME du refus, pas la garde : la garde, c'est qu'aucune liaison n'est écrite.
    # La forme a cassé dès que le refus a gagné sa raison (#689) — on tient donc ce qui
    # protège, et en prime que le refus se DIT.
    assert out["bound"] is False and out["accounts"] == []
    assert _liaisons(PIRATE) == []
    # Le pirate apprend qu'il n'y a pas de candidat — jamais que ce compte existe et
    # appartient à quelqu'un : le détail nomme les trois causes possibles sans
    # désigner la victime ni confirmer son existence.
    assert out["reason"] == "no_candidate"
    assert ACC_VICTIME not in str(out)


def test_la_reconciliation_passe_par_la_garde(scene, monkeypatch):
    """Structurel, et assumé comme tel : le chemin vivant consulte la garde
    partagée pour l'identifiant qu'il lie. Un chemin qui aurait la sienne — ou
    aucune — est la divergence de #559 qui revient."""
    from oto_mcp import unipile_connect

    vus = []
    monkeypatch.setattr(unipile_connect.unipile_binding, "account_claimable",
                        lambda sub, account_id, **k: vus.append(account_id) or True)
    _sur_cle_plateforme(monkeypatch, [
        {"id": "acc_tout_neuf", "provider": "linkedin",
         "created_at": "2099-01-01 00:00:00+00", "name": "Le pirate"}])
    _pending(PIRATE, scene["org_pirate"])
    assert unipile_connect.reconcile_pending(PIRATE)["bound"]
    assert "acc_tout_neuf" in vus, (
        "la réconciliation n'a pas consulté la garde partagée — elle en a une à "
        "elle, ou aucune")
    assert [l["account_id"] for l in _liaisons(PIRATE)] == ["acc_tout_neuf"]
