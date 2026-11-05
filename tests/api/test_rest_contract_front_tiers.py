"""Ce qu'un front tiers a lu dans le contrat, REJOUÉ sur les routes servies (29/08/2026).

Un consommateur pur de l'API REST a dérivé son comportement du contrat — et le contrat
disait faux (`GroupUpdated` : « pas de 409 au renommage »), ne disait rien (la borne
du corps d'un guide), ou taisait un succès ambigu (inviter deux fois — devenu un refus
le 29/08/2026, #622). Les
déclarations ajoutées (`Capability.errors`, `NodeOut.doc_id/project_id`,
`ContentBlock.ordered`) ne valent que si le serveur rend ce qu'elles disent : ici
chaque cas part d'une requête HTTP sur la table de routes réelle (`make_routes`), avec
l'adaptateur de capacités et un vrai PostgreSQL — pas un handler appelé à la main sur
un store stubbé, ce que `test_group_refusals.py` et `test_leave_org.py` font déjà.

Le porteur est identifié par un vérifieur factice dont le bearer EST le sub : ce
qu'on teste est en aval de l'authentification, pas elle.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

# La recopie des tables historiques vers `nodes` est ARRÊTÉE depuis le 2026-09-01 :
# les deux univers vivent côte à côte, plus rien ne traduit l'un vers l'autre. Un
# test qui ouvre par la nouvelle surface un contenu créé dans l'ancienne ne décrit
# donc plus le système — il décrit le pont qu'on vient de retirer.
#
# **STRICT à dessein.** Le jour où l'un d'eux repasse au vert, c'est qu'une recopie
# est revenue : ce fichier doit le CRIER, pas l'absorber en silence.
#
# ⚠️ Marqué, pas supprimé, et la nuance compte : l'identifiant dérivé et les
# poignées `doc_id`/`project_id` sont SERVIS au front partenaire. Les retirer est un
# changement de contrat qui appartient à un arbitrage en cours, pas à un déblaiement
# de fin de chantier. Ces tests partent — ou sont réécrits sur du contenu natif —
# quand cet arbitrage est rendu.
_SANS_PROJECTION = pytest.mark.xfail(
    strict=True,
    reason="la recopie tables historiques → nodes est arrêtée (2026-09-01)",
)


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@front-tiers.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    """Une base JETABLE et le vrai `init_db()` (même recette que les lots de nœuds)."""
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_front_tiers_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    previous_url, previous_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield init_db
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = previous_pool
        if previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous_url
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(), mcp_instance=None)))


@pytest.fixture(scope="module")
def org(live):
    """Une org avec son admin (créateur), et un simple membre."""
    from oto_mcp import db, org_store
    admin, membre = "usr_ft_admin", "usr_ft_membre"
    for sub in (admin, membre):
        db.upsert_user(sub, email=f"{sub}@front-tiers.invalid", name=sub)
    oid = org_store.create_org("Org du front tiers", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    org_store.add_org_member(oid, membre, "org_member")
    org_store.set_active_org(admin, oid)
    return {"id": oid, "admin": admin, "membre": membre}


# ── PATCH /api/groups/{id} : 409 group_exists ─────────────────────────────────

def test_renommer_un_groupe_vers_un_nom_pris_rend_409_sur_la_route_servie(client, org):
    oid, admin = org["id"], org["admin"]
    r = client.post(f"/api/orgs/{oid}/groups", json={"name": "Finance"}, headers=_h(admin))
    assert r.status_code == 200, r.text
    r = client.post(f"/api/orgs/{oid}/groups", json={"name": "Sales"}, headers=_h(admin))
    assert r.status_code == 200, r.text
    gid = r.json()["group_id"]

    r = client.patch(f"/api/groups/{gid}", json={"name": "  finance "}, headers=_h(admin))
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "group_exists"
    assert r.json()["detail"]                      # actionnable, pas un code nu
    # Rien n'a été écrit : le groupe s'appelle toujours Sales.
    assert client.get(f"/api/groups/{gid}", headers=_h(admin)).json()["group"]["name"] == "Sales"
    # Et la création refuse pareil — même conflit, même réponse.
    r = client.post(f"/api/orgs/{oid}/groups", json={"name": "FINANCE"}, headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (409, "group_exists")


# ── POST /api/orgs/{id}/invitations : déjà membre / déjà invitée = 409 (#622) ──

def _file(client, oid, admin) -> list[dict]:
    return client.get(f"/api/orgs/{oid}/invitations", headers=_h(admin)).json()["invitations"]


def test_inviter_une_adresse_deja_invitee_rend_409_et_l_invitation_existante(client, org):
    """Décision du 29/08/2026 (#622) : une invitation encore valide pour la même adresse
    est un refus, pas un deuxième secret porteur. `details.invitation` porte de quoi la
    renvoyer (id, dates) — jamais son token. La comparaison est faite sur l'adresse
    normalisée (casse, espaces)."""
    oid, admin = org["id"], org["admin"]
    r1 = client.post(f"/api/orgs/{oid}/invitations",
                     json={"email": "Deux.Fois@front-tiers.invalid", "send_email": False},
                     headers=_h(admin))
    assert r1.status_code == 200, r1.text
    assert r1.json()["email"] == "deux.fois@front-tiers.invalid"      # normalisé
    existante = [i for i in _file(client, oid, admin)
                 if i["email"] == "deux.fois@front-tiers.invalid"]
    assert len(existante) == 1

    r2 = client.post(f"/api/orgs/{oid}/invitations",
                     json={"email": "  DEUX.fois@Front-Tiers.INVALID ", "send_email": False},
                     headers=_h(admin))
    assert (r2.status_code, r2.json()["error"]) == (409, "already_invited"), r2.text
    assert "deux.fois@front-tiers.invalid" in r2.json()["detail"]
    inv = r2.json()["details"]["invitation"]
    assert inv["id"] == existante[0]["id"]
    assert inv["created_at"] == existante[0]["created_at"]
    assert inv["expires_at"] == existante[0]["expires_at"]
    assert set(inv) == {"id", "created_at", "expires_at"}     # jamais le token
    token = r1.json()["invite_url"].rsplit("/", 1)[-1]
    assert token not in r2.text
    # Rien n'a été écrit : toujours UNE ligne pour cette adresse.
    assert [i["email"] for i in _file(client, oid, admin)].count(
        "deux.fois@front-tiers.invalid") == 1


def test_inviter_un_membre_actuel_rend_409(client, org):
    oid, admin, membre = org["id"], org["admin"], org["membre"]
    adresse = f"{membre}@front-tiers.invalid"
    r = client.post(f"/api/orgs/{oid}/invitations",
                    json={"email": adresse.upper(), "send_email": False}, headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (409, "already_member"), r.text
    assert adresse in r.json()["detail"] and "membre" in r.json()["detail"]
    assert "details" not in r.json()                      # rien à renvoyer : il est là
    assert adresse not in [i["email"] for i in _file(client, oid, admin)]


def test_la_meme_capacite_refuse_sur_la_face_mcp(org):
    """`oto_org op=invite` aboutit au même handler que la route (pas une copie) : le
    refus y est le même `AuthzDenied`, que l'adaptateur MCP rend en message."""
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    oid, admin, membre = org["id"], org["admin"], org["membre"]
    with pytest.raises(AuthzDenied) as e:
        oc._org(ResolvedCtx(sub=admin, org_id=oid),
                oc.OrgInput(op="invite", org_id=oid, email=f"{membre}@front-tiers.invalid",
                            send_email=False))
    assert (e.value.status, e.value.code) == (409, "already_member")
    with pytest.raises(AuthzDenied) as e:
        oc._org(ResolvedCtx(sub=admin, org_id=oid),
                oc.OrgInput(op="invite", org_id=oid, email="deux.fois@front-tiers.invalid",
                            send_email=False))
    assert (e.value.status, e.value.code) == (409, "already_invited")
    assert set(e.value.details["invitation"]) == {"id", "created_at", "expires_at"}


@pytest.mark.parametrize("sort", ["expiree", "consommee", "revoquee", "refusee"])
def test_une_invitation_qui_ne_vaut_plus_ne_bloque_pas(client, org, sort):
    """Expirée, consommée, révoquée ou REFUSÉE (#654) : la file ne la porte plus, une
    nouvelle invitation est un 200 normal. Le refus rejoint la liste pour une raison
    précise — c'est la seule reprise après un « non » : l'émetteur réinvite tout de
    suite, sans avoir à révoquer d'abord ce qui le bloquerait (#624)."""
    from oto_mcp import org_store
    oid, admin = org["id"], org["admin"]
    adresse = f"{sort}@front-tiers.invalid"
    corps = {"email": adresse, "send_email": False}
    r = client.post(f"/api/orgs/{oid}/invitations", json=corps, headers=_h(admin))
    assert r.status_code == 200, r.text
    inv_id = next(i["id"] for i in _file(client, oid, admin) if i["email"] == adresse)
    if sort == "expiree":
        with org_store._connect() as conn:
            conn.execute("UPDATE org_invitations SET expires_at = NOW() - interval '1 day' "
                         "WHERE id = %s", (inv_id,))
    elif sort == "consommee":
        org_store._mark_invitation_accepted(inv_id, "usr_ft_quelqu_un")
    elif sort == "refusee":
        assert org_store.mark_invitation_declined(inv_id, "usr_ft_quelqu_un") is True
    else:
        r = client.delete(f"/api/orgs/{oid}/invitations/{inv_id}", headers=_h(admin))
        assert r.status_code == 200, r.text
    # La même adresse est à nouveau invitable.
    r = client.post(f"/api/orgs/{oid}/invitations", json=corps, headers=_h(admin))
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


def test_inviter_sans_adresse_reste_un_lien_a_partager(client, org):
    """Le refus ne vaut que pour une adresse : sans email, aucun doublon possible."""
    oid, admin = org["id"], org["admin"]
    r1 = client.post(f"/api/orgs/{oid}/invitations", json={"send_email": False}, headers=_h(admin))
    r2 = client.post(f"/api/orgs/{oid}/invitations", json={"send_email": False}, headers=_h(admin))
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert r1.json()["email"] is None and r1.json()["invite_url"] != r2.json()["invite_url"]


# ── POST /api/me/invitations/reject : l'invité refuse (#654) ─────────────────
#
# Demande du même front tiers : « seul `/api/me/invitations/accept` existe ». Sans
# pendant négatif, une personne qui ne veut pas rejoindre gardait un badge qu'elle ne
# pouvait pas éteindre — seul l'ÉMETTEUR pouvait retirer l'invitation, et il
# n'apprenait jamais que c'était non.
#
# Le chemin est ADDITIF, et cette contrainte a décidé la forme : `accept` fait partie
# des opérations que le contrat du front épingle, donc lui ajouter un `decision:
# accept|reject` aurait déformé une entrée servie. Un verbe = un chemin.

def _invite(client, oid, admin, adresse) -> str:
    """Émet une invitation nominative et rend le TOKEN de son lien (extrait de
    `invite_url` — aucune surface ne le rend sous un autre nom)."""
    r = client.post(f"/api/orgs/{oid}/invitations",
                    json={"email": adresse, "send_email": False}, headers=_h(admin))
    assert r.status_code == 200, r.text
    return r.json()["invite_url"].rsplit("/", 1)[-1]


def _sub(nom: str) -> str:
    """Un compte dont l'adresse est celle que le vérifieur factice dérive du sub —
    donc un compte que ses invitations peuvent viser nominativement."""
    from oto_mcp import db
    db.upsert_user(nom, email=f"{nom}@front-tiers.invalid", name=nom)
    return nom


def test_l_invite_refuse_et_l_invitation_quitte_la_file_de_l_emetteur(client, org):
    """Le cas de l'issue, bout en bout : la file de l'émetteur se vide — sans que
    personne ne rejoigne quoi que ce soit."""
    from oto_mcp import org_store
    oid, admin = org["id"], org["admin"]
    invite = _sub("usr_ft_refuseur")
    token = _invite(client, oid, admin, "usr_ft_refuseur@front-tiers.invalid")

    assert "usr_ft_refuseur@front-tiers.invalid" in [
        i["email"] for i in _file(client, oid, admin)]

    r = client.post("/api/me/invitations/reject", json={"token": token}, headers=_h(invite))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "declined": True, "scope": "org", "org_id": oid,
                        "group_id": None, "name": "Org du front tiers"}

    assert "usr_ft_refuseur@front-tiers.invalid" not in [
        i["email"] for i in _file(client, oid, admin)]
    # Refuser n'est pas rejoindre à l'envers : aucune appartenance n'a été touchée.
    assert org_store.get_org_role(oid, invite) is None


def test_une_invitation_refusee_ne_peut_plus_etre_acceptee(client, org):
    oid, admin = org["id"], org["admin"]
    invite = _sub("usr_ft_regret")
    token = _invite(client, oid, admin, "usr_ft_regret@front-tiers.invalid")
    assert client.post("/api/me/invitations/reject", json={"token": token},
                       headers=_h(invite)).status_code == 200
    r = client.post("/api/me/invitations/accept", json={"token": token}, headers=_h(invite))
    assert (r.status_code, r.json()["error"]) == (410, "invalid_or_expired"), r.text


def test_refuser_deux_fois_rend_la_meme_reponse(client, org):
    """Idempotent comme l'acceptation : un double clic n'est pas une erreur."""
    oid, admin = org["id"], org["admin"]
    invite = _sub("usr_ft_double")
    token = _invite(client, oid, admin, "usr_ft_double@front-tiers.invalid")
    corps = {"token": token}
    r1 = client.post("/api/me/invitations/reject", json=corps, headers=_h(invite))
    r2 = client.post("/api/me/invitations/reject", json=corps, headers=_h(invite))
    assert (r1.status_code, r2.status_code) == (200, 200), (r1.text, r2.text)
    assert r1.json() == r2.json()


def test_detenir_le_token_ne_suffit_pas_a_refuser_l_invitation_d_un_autre(client, org):
    """L'ASYMÉTRIE assumée avec l'acceptation. Accepter avec un token qu'on détient est
    un geste sur soi ; refuser avec ce même token détruirait l'invitation d'un tiers —
    un token partagé par erreur deviendrait une porte pour annuler l'onboarding de
    quelqu'un d'autre, et sans appartenance créée, sans trace visible. Donc : seule
    l'adresse invitée refuse. L'invitation, elle, doit rester intacte."""
    oid, admin = org["id"], org["admin"]
    cible = _sub("usr_ft_cible")
    porteur = _sub("usr_ft_porteur")
    token = _invite(client, oid, admin, "usr_ft_cible@front-tiers.invalid")

    r = client.post("/api/me/invitations/reject", json={"token": token}, headers=_h(porteur))
    assert (r.status_code, r.json()["error"]) == (403, "not_the_invitee"), r.text
    assert r.json()["detail"]
    # Rien n'a été écrit : elle est toujours dans la file, et la CIBLE peut la refuser.
    assert "usr_ft_cible@front-tiers.invalid" in [i["email"] for i in _file(client, oid, admin)]
    assert client.post("/api/me/invitations/reject", json={"token": token},
                       headers=_h(cible)).status_code == 200


def test_une_invitation_anonyme_ne_se_refuse_pas(client, org):
    """Émise sans adresse (« lien à partager soi-même ») : elle n'est adressée à
    personne, elle n'allume aucun badge, et la retirer est le geste de son émetteur."""
    oid, admin = org["id"], org["admin"]
    r = client.post(f"/api/orgs/{oid}/invitations", json={"send_email": False},
                    headers=_h(admin))
    assert r.status_code == 200, r.text
    token = r.json()["invite_url"].rsplit("/", 1)[-1]
    r = client.post("/api/me/invitations/reject", json={"token": token},
                    headers=_h(_sub("usr_ft_curieux")))
    assert (r.status_code, r.json()["error"]) == (403, "not_the_invitee"), r.text


def test_refuser_sans_token_rend_400(client, org):
    r = client.post("/api/me/invitations/reject", json={}, headers=_h(org["admin"]))
    assert (r.status_code, r.json()["error"]) == (400, "missing_token"), r.text


def test_refuser_un_token_inconnu_rend_410(client, org):
    r = client.post("/api/me/invitations/reject", json={"token": "inv_inconnu"},
                    headers=_h(org["admin"]))
    assert (r.status_code, r.json()["error"]) == (410, "invalid_or_expired"), r.text


def test_le_refus_survit_a_l_inscription_par_la_meme_adresse(client, org):
    """Le piège le plus facile à manquer. Une invitation en attente pour une adresse
    est honorée AUTOMATIQUEMENT au premier signup avec cette adresse
    (`reconcile_signup_with_invitation`). Si le refus n'y était pas filtré, créer son
    compte après avoir dit non ferait rejoindre l'org quand même — le refus annulé
    par une mécanique que personne n'a déclenchée."""
    from oto_mcp import org_store
    oid, admin = org["id"], org["admin"]
    adresse = "usr_ft_signup@front-tiers.invalid"
    invite = _sub("usr_ft_signup")
    token = _invite(client, oid, admin, adresse)
    assert client.post("/api/me/invitations/reject", json={"token": token},
                       headers=_h(invite)).status_code == 200
    assert org_store.reconcile_signup_with_invitation("usr_ft_nouveau_compte", adresse) is None


def test_le_refus_par_token_mail_marche_aussi(client, org):
    """Le token n'est rendu par aucune surface autre que le lien mail (seul son hash
    est stocké) : on le prend à la source, comme le ferait qui clique le lien."""
    from oto_mcp import org_store
    oid, admin = org["id"], org["admin"]
    invite = _sub("usr_ft_token")
    _, token = org_store.create_invitation(
        oid, "usr_ft_token@front-tiers.invalid", "org_member", invited_by=admin)
    r = client.post("/api/me/invitations/reject", json={"token": token}, headers=_h(invite))
    assert r.status_code == 200, r.text
    assert r.json()["declined"] is True


def test_le_refus_d_une_invitation_d_equipe_ne_rejoint_ni_l_org_ni_l_equipe(client, org):
    """Un seul verbe pour les trois scopes de la cascade : l'invitation porte sa
    cible, le refus n'a pas à la redemander."""
    from oto_mcp import org_store
    oid, admin = org["id"], org["admin"]
    invite = _sub("usr_ft_equipe")
    r = client.post(f"/api/orgs/{oid}/groups", json={"name": "Refus"}, headers=_h(admin))
    assert r.status_code == 200, r.text
    gid = r.json()["group_id"]
    r = client.post(f"/api/groups/{gid}/invitations",
                    json={"email": "usr_ft_equipe@front-tiers.invalid", "send_email": False},
                    headers=_h(admin))
    assert r.status_code == 200, r.text
    token = r.json()["invite_url"].rsplit("/", 1)[-1]
    r = client.post("/api/me/invitations/reject", json={"token": token},
                    headers=_h(invite))
    assert r.status_code == 200, r.text
    assert (r.json()["scope"], r.json()["org_id"], r.json()["group_id"]) == ("team", oid, gid)
    assert org_store.get_org_role(oid, invite) is None


def test_la_meme_capacite_refuse_sur_la_face_mcp_654(client, org):
    """`oto_org op=reject_invite` aboutit au même handler que la route — les deux
    faces doivent refuser aux mêmes conditions, pas se ressembler."""
    from oto_mcp.capabilities import org_console as oc
    from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
    oid, admin = org["id"], org["admin"]
    invite = _sub("usr_ft_mcp")
    token = _invite(client, oid, admin, "usr_ft_mcp@front-tiers.invalid")
    with pytest.raises(AuthzDenied) as e:
        oc._org(ResolvedCtx(sub=admin, org_id=oid),
                oc.OrgInput(op="reject_invite", token=token))
    assert (e.value.status, e.value.code) == (403, "not_the_invitee")
    out = oc._org(ResolvedCtx(sub=invite), oc.OrgInput(op="reject_invite", token=token))
    assert out["ok"] is True and out["declined"] is True and out["org_id"] == oid


# ── PUT /api/me/guides/{scope}/{slug} : 400 body_too_large, borne en OCTETS ───

def test_un_corps_de_guide_au_dela_de_65536_octets_rend_400(client, org):
    admin = org["admin"]
    r = client.put("/api/me/guides/user/trop-gros", json={"body_md": "a" * 65537},
                   headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (400, "body_too_large"), r.text
    # Le refus doit porter la MESURE, pas seulement le verdict : la borne était publiée
    # depuis le 29/08, mais l'auteur d'un corps trop lourd ne savait pas de combien il
    # dépassait — et il ne pouvait pas le calculer, `maxLength` comptant des caractères
    # là où la garde compte des octets (oto#42, 4ᵉ règle ; leçon du datastore, #383).
    assert "65537" in r.json()["detail"] and "65536" in r.json()["detail"], r.text
    # La borne EXACTE passe.
    r = client.put("/api/me/guides/user/juste", json={"body_md": "a" * 65536},
                   headers=_h(admin))
    assert r.status_code == 200, r.text


def test_la_borne_est_en_octets_pas_en_caracteres(client, org):
    """40 000 caractères accentués = 80 000 octets : `maxLength: 65536` (des caractères)
    laisse passer, le serveur refuse — c'est écrit dans la description du champ."""
    r = client.put("/api/me/guides/user/accents", json={"body_md": "é" * 40_000},
                   headers=_h(org["admin"]))
    assert (r.status_code, r.json()["error"]) == (400, "body_too_large"), r.text
    # Et c'est ICI que la mesure compte le plus : 40 000 caractères pour 80 000 octets,
    # donc l'auteur qui compte ses caractères se croit à 61 % de la borne.
    assert "80000" in r.json()["detail"], r.text


def test_le_corps_dune_PROCEDURE_trop_lourde_rend_400_avec_la_mesure(client, org):
    """Jumeau du refus des guides, à un fichier de là (`orgs/instructions.py`) : même
    code, même borne, et il n'était gardé par AUCUN test. C'est le voisin immédiat que
    les correctifs de cette classe laissent intact d'habitude — cf. oto#42.

    ⚠️ La borne d'une PROCÉDURE a été relevée à 128 Ko le 03/09 (décision d'Alexis),
    celle des GUIDES reste à 64 : deux bornes, deux raisons — le corps d'un guide est
    injecté dans chaque session, celui d'une procédure est chargé à la demande. Ce
    banc a dû être ajusté pour cette raison, et non parce qu'il avait tort : 80 000
    octets, qui étaient refusés le matin même, passent désormais."""
    from oto_mcp.capabilities.orgs.instructions import _MAX_BODY_BYTES as BORNE
    # Juste AU-DESSUS de la borne, en accentué (2 octets par signe).
    trop = "é" * (BORNE // 2 + 1)
    r = client.put("/api/me/instructions/trop-lourde", json={"body_md": trop},
                   headers=_h(org["admin"]))
    assert (r.status_code, r.json()["error"]) == (400, "body_too_large"), r.text
    assert str(len(trop.encode())) in r.json()["detail"], r.text
    assert str(BORNE) in r.json()["detail"], r.text


def test_une_PROCEDURE_de_100_Ko_passe_depuis_le_relevement(client, org):
    """La borne neuve sert à quelque chose : un corps que l'ancienne refusait doit
    désormais s'écrire. Sans ce banc, on tiendrait le refus sans tenir l'ouverture —
    et un retour en arrière sur la constante passerait inaperçu."""
    from oto_mcp.capabilities.orgs.instructions import _MAX_BODY_BYTES as BORNE
    corps = "# Procédure\n\n" + ("é" * 50_000)      # 100 000 octets
    assert 64 * 1024 < len(corps.encode()) <= BORNE   # refusé hier, accepté aujourd'hui
    r = client.put("/api/me/instructions/cent-ko", json={"body_md": corps},
                   headers=_h(org["admin"]))
    assert r.status_code == 200, r.text


def test_la_borne_des_GUIDES_n_a_PAS_bouge(client, org):
    """Les deux constantes avaient la même valeur ; elles n'ont pas la même raison.
    Celle d'un guide protège un budget RÉEL — ce corps-là est injecté dans chaque
    session. Relever l'une ne doit jamais entraîner l'autre par symétrie."""
    from oto_mcp.capabilities.guides import _MAX_BODY_BYTES as GUIDE
    from oto_mcp.capabilities.orgs.instructions import _MAX_BODY_BYTES as PROC
    assert GUIDE == 64 * 1024 and PROC == 128 * 1024
    r = client.put("/api/me/guides/user/gros", json={"body_md": "é" * 40_000},
                   headers=_h(org["admin"]))
    assert (r.status_code, r.json()["error"]) == (400, "body_too_large"), r.text


# ── DELETE /api/me/orgs/{id}/membership : les quatre refus, dans l'ordre ──────

def test_quitter_une_org_dont_on_est_le_dernier_admin_rend_409(client, org):
    oid, admin = org["id"], org["admin"]
    r = client.delete(f"/api/me/orgs/{oid}/membership", headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (409, "last_org_admin"), r.text
    # Toujours membre, toujours admin : le refus n'a rien écrit.
    from oto_mcp import org_store
    assert org_store.get_org_role(oid, admin) == "org_admin"


def test_quitter_sa_derniere_org_rend_409(client, org):
    # Plus d'org perso (05/11/2026) : le refus porte sur la DERNIÈRE org du compte.
    from oto_mcp import db, org_store
    seul = f"seul-{org['id']}"
    db.upsert_user(seul, email=f"{seul}@front-tiers.invalid")
    unique = org_store.ensure_home_org(seul, email=f"{seul}@front-tiers.invalid")
    assert [int(o["org_id"]) for o in org_store.list_orgs_for_user(seul)] == [unique]
    r = client.delete(f"/api/me/orgs/{unique}/membership", headers=_h(seul))
    assert (r.status_code, r.json()["error"]) == (409, "last_org"), r.text


def test_quitter_une_org_dont_on_n_est_pas_membre_rend_404(client, org):
    from oto_mcp import db
    etranger = "usr_ft_etranger"
    db.upsert_user(etranger, email=f"{etranger}@front-tiers.invalid")
    r = client.delete(f"/api/me/orgs/{org['id']}/membership", headers=_h(etranger))
    assert (r.status_code, r.json()["error"]) == (404, "not_a_member"), r.text


def test_quitter_une_org_inconnue_rend_404(client, org):
    r = client.delete("/api/me/orgs/999999/membership", headers=_h(org["admin"]))
    assert (r.status_code, r.json()["error"]) == (404, "unknown_org"), r.text


def test_un_simple_membre_quitte_l_org(client, org):
    """Le chemin heureux, pour que les refus ne soient pas testés dans le vide."""
    oid, membre = org["id"], org["membre"]
    r = client.delete(f"/api/me/orgs/{oid}/membership", headers=_h(membre))
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "org_id": oid, "left": True}


# ── DELETE /api/orgs/{id} : 409 org_has_active_subscription (#400) ────────────

def test_archiver_une_org_qui_porte_un_abonnement_rend_409_puis_passe_une_fois_resilie(client):
    """Décision d'Alexis (#400, piste 1) : une org avec un abonnement qui prélève ne
    s'archive pas — elle sortirait des listings et plus personne ne pourrait le résilier.
    Rejoué sur la route SERVIE, avec le code que `Capability.errors` déclare."""
    from oto_mcp import db, org_store
    from oto_mcp.db import billing

    admin = "usr_ft_archive_admin"
    db.upsert_user(admin, email=f"{admin}@front-tiers.invalid", name=admin)
    oid = org_store.create_org("Org à abonnement", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    billing.upsert_org_subscription(oid, plan="standard", customer_id="cst_ft",
                                    mandate_id="mdt_ft", status="active")

    r = client.delete(f"/api/orgs/{oid}", headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (409, "org_has_active_subscription"), r.text
    assert "abonnement" in r.json()["detail"]
    assert org_store.get_org(oid) is not None          # toujours servie : rien n'a été écrit

    billing.mark_cancel_at_period_end(oid)              # « résilie d'abord »
    r = client.delete(f"/api/orgs/{oid}", headers=_h(admin))
    assert r.status_code == 200, r.text
    # `already_archived` (additif, e4b0e57d) : false ici, l'org vient d'être archivée.
    assert r.json() == {"ok": True, "org_id": oid, "archived": True,
                        "already_archived": False}


# ── GET /api/me/nodes/{id} : doc_id, project_id, ordered ──────────────────────

@pytest.fixture(scope="module")
def noeuds(live, org):
    """Un projet et une page CONVERTIS en nœuds (le vrai chemin : `init_db` projette),
    puis les blocs projetés par la passe de maintenance."""
    from oto_mcp.db import blocks, create_doc, create_project, shell as db_shell
    oid, admin = org["id"], org["admin"]
    pid = create_project("org", str(oid), "Refonte", brief_md="Le brief.", created_by=admin)
    did = create_doc(pid, "Plan", body_md="# Plan\n\n1. cadrer\n2. livrer\n\n- puce\n- autre\n",
                     created_by=admin)
    live()                                        # la projection projets/pages → nœuds
    blocks.backfill_node_blocks()
    return {"pid": pid, "did": did,
            "nod_prj": db_shell._public_id_derive("prj", str(pid)),
            "nod_doc": db_shell._public_id_derive("doc", str(did))}


@_SANS_PROJECTION
def test_une_page_ouverte_rend_ses_poignees_et_l_ordre_de_ses_listes(client, org, noeuds):
    r = client.get(f"/api/me/nodes/{noeuds['nod_doc']}", headers=_h(org["admin"]))
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["doc_id"], out["project_id"]) == (noeuds["did"], noeuds["pid"])
    listes = [b for b in out["body"] if b.get("role") == "list"]
    assert [b["ordered"] for b in listes] == [True, False]
    assert listes[0]["items"] == ["cadrer", "livrer"]
    assert all("ordered" not in b for b in out["body"] if b.get("role") != "list")


@_SANS_PROJECTION
def test_un_projet_ouvert_rend_son_project_id_sans_doc_id(client, org, noeuds):
    r = client.get(f"/api/me/nodes/{noeuds['nod_prj']}", headers=_h(org["admin"]))
    assert r.status_code == 200, r.text
    assert (r.json()["doc_id"], r.json()["project_id"]) == (None, noeuds["pid"])


def test_les_poignees_ouvrent_bien_les_autres_surfaces(client, org, noeuds):
    """La raison d'être des deux champs : ils sont acceptés tels quels par le partage
    (`/api/resources`) et par les pages (`/api/me/docs`). Sinon on aurait publié des
    entiers que personne ne prend."""
    admin = org["admin"]
    r = client.post("/api/resources", headers=_h(admin),
                    json={"op": "get", "resource_type": "project",
                          "resource_id": str(noeuds["pid"])})
    assert r.status_code == 200, r.text
    r = client.post("/api/me/docs", headers=_h(admin),
                    json={"op": "backlinks", "doc_id": noeuds["did"]})
    assert r.status_code == 200, r.text
    assert r.json()["doc_id"] == noeuds["did"]


# ── POST /api/me/instructions : créer sur un slug pris = 409 (#662) ────────────

_PROC = ("> **Self-improvement digest** — jamais déroulée.\n\n"
         "# Qualification\n\n```\n[Début] --> [Fin]\n```\n\nCorps.")


def test_creer_une_procedure_sur_un_slug_pris_rend_409_sur_la_route_servie(client, org):
    """Le signal du front tiers, rejoué au ras du fil.

    Il fabriquait son slug côté client et appelait `PUT …/instructions/{slug}` faute
    de verbe de création : un slug déjà pris répondait `200 ok` en ayant remplacé la
    procédure d'org. Le `POST` refuse — et la procédure en place se relit inchangée,
    ce qui est la moitié qui compte : un refus qui aurait déjà écrit ne vaudrait rien."""
    admin = org["admin"]
    r = client.post("/api/me/instructions", headers=_h(admin),
                    json={"slug": "qualification", "body_md": _PROC, "title": "Qualif"})
    assert r.status_code == 200, r.text
    assert (r.json()["slug"], r.json()["version"]) == ("qualification", 1)

    r = client.post("/api/me/instructions", headers=_h(admin),
                    json={"slug": "qualification", "body_md": "AUTRE CORPS"})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "slug_taken"
    assert r.json()["detail"]                       # actionnable, pas un code nu
    assert r.json()["details"]["version"] == 1

    relue = client.get("/api/me/instructions/qualification", headers=_h(admin)).json()
    assert (relue["body_md"], relue["version"]) == (_PROC, 1)

    # …et le PUT, lui, édite toujours : le refus n'a pas fermé le geste d'édition.
    r = client.put("/api/me/instructions/qualification", headers=_h(admin),
                   json={"body_md": _PROC + "\n\nÉdité."})
    assert (r.status_code, r.json()["version"]) == (200, 2)


def test_editer_avec_une_version_perimee_rend_409_version_conflict(client, org):
    """Le pendant sur le chemin d'ÉDITION : `expected_version` transforme
    l'écrasement d'un éditeur concurrent en refus, comme `expected_rev` sur les pages."""
    admin = org["admin"]
    r = client.post("/api/me/instructions", headers=_h(admin),
                    json={"slug": "relance-clients", "body_md": _PROC})
    assert r.status_code == 200, r.text

    r = client.put("/api/me/instructions/relance-clients", headers=_h(admin),
                   json={"body_md": _PROC + "\n\nv2 posée par un autre."})
    assert (r.status_code, r.json()["version"]) == (200, 2)

    r = client.put("/api/me/instructions/relance-clients", headers=_h(admin),
                   json={"body_md": "MA VERSION", "expected_version": 1})
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "version_conflict"
    assert r.json()["details"]["current_version"] == 2
    # Le travail de l'autre a survécu.
    relue = client.get("/api/me/instructions/relance-clients", headers=_h(admin)).json()
    assert relue["body_md"].endswith("v2 posée par un autre.")


# ── GET /api/orgs/{id}/audit-log/export : la pièce dit sa complétude (#770) ────

def test_l_export_d_audit_dit_son_total_sa_troncature_et_sa_borne(client, org):
    """La chaîne ENTIÈRE, sur la route servie : table de routes, adaptateur, autz,
    handler, store, enveloppe JSON. Un handler appelé à la main ne prouverait pas
    que ces champs traversent l'adaptateur et le modèle de sortie.

    Le journal d'audit est une pièce qu'un client produit pour se justifier. Ce qui
    se rejoue ici est donc ce qui la rend opposable : le total de la fenêtre, le fait
    que la réponse ne la porte pas toute, et la borne haute réellement appliquée.
    """
    from oto_mcp import db
    oid, admin = org["id"], org["admin"]
    for _ in range(5):
        db.insert_tool_call({"sub": admin, "kind": "mcp", "tool": "fr_get",
                             "ok": True, "org_id": oid, "duration_ms": 2})

    r = client.get(f"/api/orgs/{oid}/audit-log/export?limit=2", headers=_h(admin))
    assert r.status_code == 200, r.text
    p1 = r.json()
    assert p1["count"] == 2 and p1["total"] == 5
    assert p1["truncated"] is True and p1["next_cursor"]
    # `until` reste le réécho de ce qui a été reçu ; la borne appliquée est à côté.
    assert p1["until"] is None and p1["until_effectif"].endswith("Z")
    assert [c["namespace"] for c in p1["calls"]] == ["fr", "fr"]

    # Le curseur se renvoie TEL QUEL, et la concaténation vaut exactement le total.
    vus, cur, total = [c["id"] for c in p1["calls"]], p1["next_cursor"], p1["total"]
    while cur:
        page = client.get(f"/api/orgs/{oid}/audit-log/export?limit=2&cursor={cur}",
                          headers=_h(admin)).json()
        assert page["total"] == total, "la fenêtre gelée ne s'élargit pas en route"
        vus += [c["id"] for c in page["calls"]]
        cur = page["next_cursor"]
    assert len(vus) == len(set(vus)) == total

    # Une fenêtre qui tient dans une page ne se dit PAS tronquée.
    entier = client.get(f"/api/orgs/{oid}/audit-log/export", headers=_h(admin)).json()
    assert entier["count"] == entier["total"] == 5
    assert entier["truncated"] is False and entier["next_cursor"] is None


def test_les_deux_refus_declares_de_l_export_sont_rejoues(client, org):
    """`DeclaredError` DÉCRIT, ne fait rien : une déclaration sans rejeu promettrait
    un refus que le serveur ne rend pas."""
    from oto_mcp import db
    oid, admin = org["id"], org["admin"]
    # Ses propres lignes : un test qui hérite des écritures de son voisin passe ou
    # tombe selon l'ordre de collecte, pas selon ce qu'il prétend prouver.
    for _ in range(2):
        db.insert_tool_call({"sub": admin, "kind": "mcp", "tool": "oto_doc",
                             "ok": True, "org_id": oid, "duration_ms": 1})

    r = client.get(f"/api/orgs/{oid}/audit-log/export?cursor=pas-un-curseur",
                   headers=_h(admin))
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "invalid_cursor"
    assert r.json()["detail"]                       # actionnable, pas un code nu

    bon = client.get(f"/api/orgs/{oid}/audit-log/export?limit=1",
                     headers=_h(admin)).json()["next_cursor"]
    r = client.get(f"/api/orgs/{oid}/audit-log/export"
                   f"?cursor={bon}&since=2026-08-01", headers=_h(admin))
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "window_with_cursor"


def test_un_simple_membre_ne_lit_pas_le_journal_de_l_org(client, org):
    """Le gate n'a pas bougé : `ORG_ADMIN_OF`. On le rejoue parce que ce lot a
    touché l'entrée de la capacité."""
    r = client.get(f"/api/orgs/{org['id']}/audit-log/export", headers=_h(org["membre"]))
    assert r.status_code in (403, 404), r.text


# ── Les refus des capacités de connecteur, rejoués sur la route servie (#668) ──
#
# Quinze capacités déclarent désormais 37 refus nommés, là où le document n'en
# portait que trois génériques (401/403 et les 400 de l'adaptateur). Le garde-fou
# `test_capability_declared_errors.py` vérifie que chaque code est LEVÉ dans le
# module et PUBLIÉ ; il ne peut pas vérifier qu'une route y mène vraiment.
#
# ⚠️ Deux rejeux ici, pas trente-sept, et c'est un choix assumé : ils couvrent les
# deux FORMES de refus de ce périmètre — celui qui tombe avant toute lecture (une
# demande qu'on ne sert jamais) et celui qui tombe sur un nom qu'on ne reconnaît
# pas. Les autres suivent le même chemin de code ; les rejouer un par un
# coûterait quinze montages pour la même démonstration.

def test_demander_la_valeur_dune_cle_rend_403_secret_never_revealed(client, org):
    """Le refus le plus important du lot : il dit qu'une valeur ne se relit JAMAIS,
    au lieu de laisser croire à un droit manquant. Levé avant toute lecture du
    coffre — un 200 au corps amputé se lirait « aucune clé posée »."""
    oid, admin, _ = org
    r = client.get("/api/settings/api-keys/serper", params={"reveal": "true"},
                   headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (403, "secret_never_revealed"), r.text


def test_installer_un_connecteur_inconnu_rend_404_unknown_connector(client, org):
    """Trois situations tombent ici — nom inconnu, connecteur non exposé pour l'org,
    ou restreint par une règle. Le document les déclare sous ce seul code parce
    qu'elles sont indistinguables côté membre, et qu'elles se règlent toutes par la
    même demande à un admin."""
    oid, admin, _ = org
    r = client.post("/api/me/connectors/connecteur-qui-nexiste-pas/select",
                    headers=_h(admin))
    assert (r.status_code, r.json()["error"]) == (404, "unknown_connector"), r.text
