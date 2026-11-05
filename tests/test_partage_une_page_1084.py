"""Partager UNE page sans son projet — signal #1084, 18/09/2026.

Le cas : faire lire une page récapitulative à deux personnes d'une AUTRE org. Le seul
geste disponible partageait le projet entier ; elles ont vu ses vingt pages, internes
comprises, et le partage a été retiré. `set_public` n'est pas la réponse : il ouvre la
page à quiconque a le lien.

Ce qui est prouvé ici, sur une VRAIE base et par le chemin servi (règle d'autz
`RESOURCE_GOVERN` + handler `oto_resource` ; dispatcher `oto_doc`) :

1. le destinataire lit CETTE page, et aucun chemin ne lui rend le reste du projet —
   ni `list`/`search` du projet, ni la page sœur, ni la sous-page (qui n'hérite de
   rien), ni l'historique (`revisions`, qui peut porter ce que l'auteur a retiré
   avant de partager), ni les `backlinks` (qui nomment d'autres pages), ni le
   tableau lié ;
2. `viewer` = lecture seule ;
3. seul celui qui GOUVERNE le projet partage une page — un lecteur de la page ou du
   projet ne re-partage pas ;
4. l'élargissement laisse sa trace ADR 0068 §4 et l'e-mail au destinataire ;
5. `unshare` ferme la lecture tout de suite ;
6. la page ne devient pas publique.

⚠️ **Valeurs sentinelles.** La sœur, la sous-page et la version antérieure portent une
chaîne unique dans leur titre ou leur corps ; le test concatène TOUT ce que le
destinataire a reçu, refus compris, et y cherche ces chaînes. Un refus qui citerait
un titre voisin serait une fuite au même titre qu'une lecture.
"""
from __future__ import annotations

import json
import uuid

import pytest

from oto_mcp import db, group_store, org_store, ownership
from oto_mcp.capabilities import resources as R
from oto_mcp.capabilities._authz import RESOURCE_GOVERN
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.docs import core as D

AUTEUR, GERANT, LECTEUR = "u-1084-auteur", "u-1084-gerant", "u-1084-lecteur"
DEST, COLLEGUE, EQUIPIER, TIERS = ("u-1084-dest", "u-1084-collegue", "u-1084-equipier",
                                   "u-1084-tiers")
_S = uuid.uuid4().hex[:8]
SOEUR_TITRE, SOEUR_CORPS = f"TITRE-SOEUR-{_S}", f"CORPS-SOEUR-{_S}"
ENFANT_TITRE, ENFANT_CORPS = f"TITRE-ENFANT-{_S}", f"CORPS-ENFANT-{_S}"
REVISION = f"VERSION-ANTERIEURE-{_S}"
SENTINELLES = (SOEUR_TITRE, SOEUR_CORPS, ENFANT_TITRE, ENFANT_CORPS, REVISION)
RECAP_TITRE = f"Récap {_S}"
RECAP_CORPS = "Le récapitulatif : ce que le destinataire doit lire, et rien d'autre."


@pytest.fixture(scope="module")
def monde(live):
    for sub in (AUTEUR, GERANT, LECTEUR, DEST, COLLEGUE, EQUIPIER, TIERS):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=sub)
    x = org_store.create_org("Org de l'auteur", created_by=AUTEUR)
    y = org_store.create_org("Org des destinataires", created_by=DEST)
    z = org_store.create_org("Org tierce", created_by=TIERS)
    org_store.add_org_member(x, AUTEUR, "org_admin")
    org_store.add_org_member(x, EQUIPIER)
    org_store.add_org_member(y, DEST)
    org_store.add_org_member(y, COLLEGUE)
    org_store.add_org_member(z, TIERS)
    equipe = group_store.create_group(x, "Équipe X", created_by=AUTEUR)
    group_store.add_group_member(equipe, EQUIPIER)
    # Un projet PERSONNEL de l'auteur, rangé dans son org : ni l'équipe ni l'org X ne le
    # lisent — c'est ce qui rend l'audience « équipe » significative ci-dessous.
    pid = int(db.create_project("user", AUTEUR, "Clients", created_by=AUTEUR,
                                context_org_id=x))
    ownership.grant("project", str(pid), "user", GERANT, role="manager", granted_by=AUTEUR)
    ownership.grant("project", str(pid), "user", LECTEUR, role="viewer", granted_by=AUTEUR)
    recap = db.create_doc(pid, RECAP_TITRE, body_md=REVISION, created_by=AUTEUR)
    # La version partagée ; la précédente, avec sa sentinelle, reste dans l'historique.
    db.update_doc(recap, body_md=RECAP_CORPS + f"\n\n```oto-data\nmontants-{_S}\n```\n",
                  edited_by=AUTEUR)
    # La sœur CITE la page partagée : ses backlinks nommeraient la sœur.
    soeur = db.create_doc(pid, SOEUR_TITRE, body_md=f"{SOEUR_CORPS} — cf. [[{RECAP_TITRE}]]",
                          created_by=AUTEUR)
    enfant = db.create_doc(pid, ENFANT_TITRE, parent_id=recap, body_md=ENFANT_CORPS,
                           created_by=AUTEUR)
    ns = db.create_datastore("user", AUTEUR, f"montants-{_S}")
    db.add_project_link(pid, "tableau", str(ns), label="Montants")
    return {"x": x, "y": y, "z": z, "equipe": equipe, "projet": pid, "recap": recap,
            "soeur": soeur, "enfant": enfant, "tableau": ns}


def _gouverne(sub: str, **args) -> dict:
    """`oto_resource` par le chemin servi : la règle d'autz PUIS le handler, sur la face
    MCP (un agent partage — le régime ② de l'ADR 0068)."""
    inp = R.ResourceInput(**args)
    ctx = RESOURCE_GOVERN()(RawCtx(sub=sub), inp)
    ctx.channel = "mcp"
    return R._resources(ctx, inp)


def _partage(m, sub=AUTEUR, **principal) -> dict:
    return _gouverne(sub, op="share", resource_type="doc", resource_id=str(m["recap"]),
                     **principal)


def _retire(m, **principal) -> dict:
    return _gouverne(AUTEUR, op="unshare", resource_type="doc",
                     resource_id=str(m["recap"]), **principal)


def _doc(sub, org, **args):
    """UN appel d'`oto_doc` — `("ok", sortie)` ou `("refus", status, code, message)`."""
    try:
        return ("ok", D._doc(ResolvedCtx(sub=sub, org_id=org), D.DocInput(**args)))
    except AuthzDenied as e:
        return ("refus", e.status, e.code, e.message)


def _perso(sub) -> int:
    """L'org PERSO de `sub` — où `scope=org` compte aussi la personne."""
    return org_store.ensure_home_org(sub)


def _tous_les_chemins(m) -> dict:
    """Tout ce qu'un détenteur de la seule page peut tenter par `oto_doc`, hors la
    lecture de la page elle-même."""
    r, s, e, p = m["recap"], m["soeur"], m["enfant"], m["projet"]
    return {
        "get soeur": dict(op="get", doc_id=s),
        "get sous-page": dict(op="get", doc_id=e),
        "list projet": dict(op="list", project_id=p),
        "list projet fields=*": dict(op="list", project_id=p, fields=["*"]),
        "search projet": dict(op="search", project_id=p, query=_S),
        "revisions page": dict(op="revisions", doc_id=r),
        "revisions soeur": dict(op="revisions", doc_id=s),
        "backlinks page": dict(op="backlinks", doc_id=r),
        "backlinks sous-page": dict(op="backlinks", doc_id=e),
        "revert page": dict(op="revert", doc_id=r, revision_id=1),
        "update page": dict(op="update", doc_id=r, body_md="réécrit"),
        "patch page": dict(op="patch", doc_id=r, region="preamble", body_md="x"),
        "delete page": dict(op="delete", doc_id=r),
        "move page": dict(op="move", doc_id=r, parent_id=None, position=0),
        "set_public page": dict(op="set_public", doc_id=r, public=True),
        "create dans le projet": dict(op="create", project_id=p, title="intrus"),
        "bulk_create": dict(op="bulk_create", project_id=p, pages=[{"title": "intrus"}]),
    }


def _sans_sentinelle(recu) -> None:
    texte = json.dumps(recu, ensure_ascii=False, default=str)
    fuites = [s for s in SENTINELLES if s in texte]
    assert not fuites, f"le destinataire a reçu {fuites}"


# ── 1 & 2. La page, et rien d'autre ─────────────────────────────────────────────

def test_le_destinataire_lit_la_page_et_RIEN_d_autre_du_projet(monde):
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        lu = _doc(DEST, monde["y"], op="get", doc_id=monde["recap"])
        assert lu[0] == "ok", lu
        page = lu[1]
        assert page["title"] == RECAP_TITRE and page["body_md"].startswith(RECAP_CORPS)
        # L'adresse servie ouvre la page DANS son projet : un lien mort n'est pas servi.
        assert page["url"] is None and page["public"] is False

        recu = {nom: _doc(DEST, monde["y"], **args)
                for nom, args in _tous_les_chemins(monde).items()}
        # F-016 : par `doc_id`, une page illisible rend 404 `unknown_doc` (comme une
        # page absente) ; par `project_id`, le refus reste 403.
        ouverts = {nom: r for nom, r in recu.items() if r[0] != "refus" or r[1] not in (403, 404)}
        assert not ouverts, f"chemins qui ne refusent pas : {ouverts}"
        _sans_sentinelle({"page": page, "refus": recu})
        # La page n'a pas bougé : lecture seule.
        assert db.get_doc_by_id(monde["recap"])["body_md"].startswith(RECAP_CORPS)
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


def test_le_projet_et_le_tableau_lie_restent_fermes(monde):
    """Le grant vit sur `(doc, id)` : il ne touche ni le projet, ni ses tableaux."""
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        assert not ownership.can_access(DEST, "project", str(monde["projet"]))
        assert db.get_resource_grant("project", str(monde["projet"]), "user", DEST) is None
        assert monde["projet"] not in ownership.accessible_project_ids(DEST, monde["y"])
        assert not ownership.can_access(DEST, "datastore_namespace", str(monde["tableau"]))
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


def test_la_sous_page_n_herite_de_rien(monde):
    """Partager une page ne partage pas ses enfants — même sémantique que `set_public`."""
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        assert not ownership.can_access(DEST, "doc", str(monde["enfant"]))
        assert _doc(DEST, monde["y"], op="get", doc_id=monde["enfant"])[:3] == \
            ("refus", 404, "unknown_doc")
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


def test_shared_with_me_rend_la_page_et_elle_seule(monde):
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        # 29/09/2026 : servie dans toute org, avec le même contenu (une org perso est une
        # org comme une autre ; un partage à une personne n'appartient à aucune org).
        assert _doc(DEST, monde["y"], op="shared_with_me") == \
            _doc(DEST, _perso(DEST), op="shared_with_me")
        ok, recus = _doc(DEST, _perso(DEST), op="shared_with_me")
        assert ok == "ok"
        assert [d["id"] for d in recus["docs"]] == [monde["recap"]]
        entree = recus["docs"][0]
        assert (entree["role"], entree["via"], entree["shared_by"]) == \
            ("viewer", "person", AUTEUR)
        assert entree["url"] is None and "body_md" not in entree
        _sans_sentinelle(recus)
        # Rien pour qui n'a rien reçu.
        assert _doc(TIERS, _perso(TIERS), op="shared_with_me")[1]["docs"] == []
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


def test_shared_with_me_par_portee_la_personne_ou_l_org_consultee(monde):
    """`scope` sépare ce que la PERSONNE reçoit (`me`, toutes orgs) de ce que reçoit
    l'org CONSULTÉE et les équipes de l'appelant en elle (`org`). Sans `scope`, l'union
    historique. Toutes sont servies dans toute org (29/09/2026 : une org perso est une
    org comme une autre) ; dans l'org perso, `org` compte aussi la personne."""
    _partage(monde, email=f"{DEST}@exemple.test")
    _partage(monde, org_id=monde["y"], audience="org")
    _partage(monde, group_id=monde["equipe"], audience="team")
    try:
        def ids(sub, org, **a):
            ok, out = _doc(sub, org, op="shared_with_me", **a)
            assert ok == "ok"
            return [(d["id"], d["via"]) for d in out["docs"]], out["scope"]

        page = monde["recap"]
        # La personne : son partage nominatif, le même dans toute org (il était refusé
        # hors de l'org perso avant le 29/09/2026).
        assert ids(DEST, _perso(DEST), scope="me") == ([(page, "person")], "me")
        for org in (monde["y"], monde["z"]):
            assert ids(DEST, org, scope="me") == ([(page, "person")], "me")
        # Dans son org perso, `scope=org` compte aussi la personne.
        assert ids(DEST, _perso(DEST), scope="org") == ([(page, "person")], "org")
        # L'org consultée : le partage à l'org, jamais celui fait à la personne.
        assert ids(COLLEGUE, monde["y"], scope="org") == ([(page, "org")], "org")
        assert ids(COLLEGUE, _perso(COLLEGUE), scope="me") == ([], "me")
        # L'équipe de l'appelant DANS l'org consultée compte pour l'org.
        assert ids(EQUIPIER, monde["x"], scope="org") == ([(page, "team")], "org")
        # Une autre org que celle qui a reçu : rien.
        assert ids(TIERS, monde["z"], scope="org") == ([], "org")
        # Défaut : l'union (toutes ses orgs), la même dans toute org.
        assert ids(COLLEGUE, _perso(COLLEGUE))[0] == [(page, "org")]
        assert ids(COLLEGUE, _perso(COLLEGUE))[1] is None
        assert ids(COLLEGUE, monde["y"]) == ids(COLLEGUE, _perso(COLLEGUE))
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")
        _retire(monde, org_id=monde["y"])
        _retire(monde, group_id=monde["equipe"])


def test_scope_ne_s_applique_qu_a_shared_with_me(monde):
    assert _doc(DEST, monde["y"], op="get", doc_id=monde["recap"], scope="me")[:3] == \
        ("refus", 400, "unsupported_scope_arg")


# ── Les trois audiences ─────────────────────────────────────────────────────────

def test_audience_org_une_org_cliente_lit_la_page(monde):
    """Le cas du signal : deux personnes d'une AUTRE org. Partager à leur org les
    couvre toutes les deux — et personne d'ailleurs."""
    out = _partage(monde, org_id=monde["y"], audience="org")
    assert (out["principal_type"], out["role"]) == ("org", "viewer")
    try:
        for sub in (DEST, COLLEGUE):
            assert _doc(sub, monde["y"], op="get", doc_id=monde["recap"])[0] == "ok"
            assert _doc(sub, monde["y"], op="get", doc_id=monde["soeur"])[:2] == ("refus", 404)
            assert _doc(sub, monde["y"], op="list", project_id=monde["projet"])[:2] == \
                ("refus", 403)
        assert _doc(TIERS, monde["z"], op="get", doc_id=monde["recap"])[:2] == ("refus", 404)
        assert _doc(COLLEGUE, monde["y"], op="shared_with_me",
                    scope="org")[1]["docs"][0]["via"] == "org"
    finally:
        _retire(monde, org_id=monde["y"])
    assert _doc(COLLEGUE, monde["y"], op="get", doc_id=monde["recap"])[:2] == ("refus", 404)


def test_audience_equipe(monde):
    """L'équipe ne lit pas le projet (il est personnel) ; le partage lui ouvre la page."""
    assert _doc(EQUIPIER, monde["x"], op="get", doc_id=monde["recap"])[:2] == ("refus", 404)
    _partage(monde, group_id=monde["equipe"], audience="team")
    try:
        assert _doc(EQUIPIER, monde["x"], op="get", doc_id=monde["recap"])[0] == "ok"
        assert _doc(EQUIPIER, monde["x"], op="get", doc_id=monde["enfant"])[:2] == \
            ("refus", 404)
    finally:
        _retire(monde, group_id=monde["equipe"])


# ── 3. Qui partage ──────────────────────────────────────────────────────────────

def test_seul_qui_gouverne_le_projet_partage_une_page(monde):
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        # Le destinataire de la page, et un LECTEUR du projet entier : ni l'un ni
        # l'autre ne re-partage, ne retire, ni ne lit la fiche de gouvernance.
        for sub in (DEST, LECTEUR):
            for op in ("share", "unshare", "get"):
                with pytest.raises(AuthzDenied) as e:
                    _gouverne(sub, op=op, resource_type="doc",
                              resource_id=str(monde["recap"]),
                              email=f"{TIERS}@exemple.test")
                assert (e.value.status, e.value.code) == (403, "forbidden"), (sub, op)
        # Le GÉRANT du projet (grant `manager`, ADR 0048) gouverne ses pages : même
        # règle que pour le projet, lue sur le projet (`governed_by`), pas recopiée.
        _partage(monde, sub=GERANT, email=f"{TIERS}@exemple.test")
        assert _doc(TIERS, monde["z"], op="get", doc_id=monde["recap"])[0] == "ok"
        _retire(monde, email=f"{TIERS}@exemple.test")
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


@pytest.mark.parametrize("args", [{"role": "editor"}, {"role": "manager"},
                                  {"permission": "write"}])
def test_une_page_ne_se_partage_qu_en_lecture(monde, args):
    with pytest.raises(AuthzDenied) as e:
        _partage(monde, email=f"{DEST}@exemple.test", **args)
    assert (e.value.status, e.value.code) == (400, "doc_viewer_only")
    assert ownership.list_grants("doc", str(monde["recap"])) == []


@pytest.mark.parametrize("audience", ["public", "secret"])
def test_une_page_ne_se_publie_pas_par_ce_chemin(monde, audience):
    with pytest.raises(AuthzDenied) as e:
        _partage(monde, audience=audience)
    assert (e.value.status, e.value.code) == (400, "publication_unsupported")


def test_une_page_ne_se_transfere_pas(monde):
    with pytest.raises(AuthzDenied) as e:
        _gouverne(AUTEUR, op="transfer", resource_type="doc",
                  resource_id=str(monde["recap"]),
                  new_owner_email=f"{DEST}@exemple.test", confirm_transfer=True)
    assert (e.value.status, e.value.code) == (409, "transfer_failed")
    assert ownership.owner_of("project", str(monde["projet"])) == ("user", AUTEUR)


# ── 4. La trace ─────────────────────────────────────────────────────────────────

def test_l_elargissement_laisse_sa_trace_et_previent_le_destinataire(monde, monkeypatch):
    envoyes = []
    monkeypatch.setattr(R.email, "send_resource_shared_email",
                        lambda to, **k: envoyes.append((to, k)) or True)
    out = _partage(monde, sub=GERANT, email=f"{DEST}@exemple.test")
    try:
        assert out["notified"] is True
        assert [(to, k["type_label"], k["name"]) for to, k in envoyes] == \
            [(f"{DEST}@exemple.test", "page", RECAP_TITRE)]
        from oto_mcp.db._conn import _connect
        with _connect() as conn:
            ligne = conn.execute(
                "SELECT * FROM portee_elargissements WHERE ressource_type = 'doc' "
                "AND ressource_id = %s ORDER BY id DESC LIMIT 1",
                (str(monde["recap"]),)).fetchone()
        assert ligne is not None
        # L'AUTEUR de la page (celui dont le contenu s'ouvre) ET le gérant qui partage.
        assert (ligne["acteur_sub"], ligne["proprietaire_sub"], ligne["vers"]) == \
            (GERANT, AUTEUR, "person")
        assert ligne["ressource_nom"] == RECAP_TITRE
        assert sorted(ligne["destinataires"]) == sorted([AUTEUR, GERANT])
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


# ── 5. Le retrait ───────────────────────────────────────────────────────────────

def test_unshare_ferme_la_lecture_tout_de_suite(monde):
    _partage(monde, email=f"{DEST}@exemple.test")
    assert _doc(DEST, monde["y"], op="get", doc_id=monde["recap"])[0] == "ok"
    assert _retire(monde, email=f"{DEST}@exemple.test")["removed"] is True
    assert _doc(DEST, monde["y"], op="get", doc_id=monde["recap"])[:2] == ("refus", 404)
    assert _doc(DEST, _perso(DEST), op="shared_with_me")[1]["docs"] == []


# ── 6. Pas public ───────────────────────────────────────────────────────────────

def test_partager_la_page_ne_la_rend_pas_publique(monde):
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        assert db.get_doc_by_id(monde["recap"])["public_token"] is None
        # Sans compte, rien : ni par `oto_doc`, ni par la porte de lecture.
        assert _doc(None, None, op="get", doc_id=monde["recap"])[:2] == ("refus", 404)
        from oto_mcp.capabilities.docs import common
        assert common.acces_a_la_page(None, db.get_doc_by_id(monde["recap"])) is None
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")


# ── Gouvernance : ce que voit celui qui a partagé ───────────────────────────────

def test_la_fiche_et_la_liste_de_gouvernance(monde):
    _partage(monde, email=f"{DEST}@exemple.test")
    try:
        fiche = _gouverne(AUTEUR, op="get", resource_type="doc",
                          resource_id=str(monde["recap"]))
        assert (fiche["title"], fiche["project_id"], fiche["owner_id"]) == \
            (RECAP_TITRE, monde["projet"], AUTEUR)
        assert [(g["principal_id"], g["role"]) for g in fiche["grants"]] == [(DEST, "viewer")]
        assert "body_md" not in fiche
        liste = _gouverne(AUTEUR, op="list", resource_type="doc")
        assert [r["resource_id"] for r in liste["resources"]] == [str(monde["recap"])]
    finally:
        _retire(monde, email=f"{DEST}@exemple.test")
    assert _gouverne(AUTEUR, op="list", resource_type="doc")["resources"] == []
