"""Scoping de `DatastorePg.list_datastores` (ADR 0023/0030).

La liste est une LISTE DE CONTENU : elle scope sur l'org ACTIVE (owner
`ownership.active_owner`) et sur MES groupes DE CETTE ORG — jamais l'union
cross-org (`accessor_scope`), cf. la règle de `ownership.active_owner` et le
tripwire `test_owner_scope_tripwire.py`. Pendant datastore du test projets
`test_list_includes_projects_shared_to_my_team`.

On monkeypatche les seams (access/group_store/db/ownership), pas de DB.
"""
import pytest

from oto_mcp.datastore import core as D
from oto_mcp import access, group_store, roles


@pytest.fixture(autouse=True)
def _member_by_default(monkeypatch):
    # ADR 0049 : `_active_scope` escalade l'org_admin (tous les groupes de l'org) —
    # défaut des tests = simple membre ; les tests admin surchargent.
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: False)


OWNED = {"id": 1, "datastore": "leads", "owner_type": "org", "owner_id": "99",
         "created_at": "2026-07-02", "schema": None}
GRANTED = {"id": 2, "datastore": "accords", "owner_type": "org", "owner_id": "42",
           "created_at": "2026-07-02", "schema": None, "permission": "read"}


def _wire(monkeypatch, rec, *, org=99, groups=({"group_id": 5, "org_id": 99, "name": "sales"},),
          perso=99):
    monkeypatch.setattr(access, "current_org", lambda sub: org)
    # L'org PERSO de l'appelant (décision du 28/09/2026) : par défaut l'org active —
    # les bancs d'org de TRAVAIL passent une autre valeur.
    monkeypatch.setattr(D.ownership.org_store, "home_org", lambda sub: perso)
    monkeypatch.setattr(D.db, "list_datastores_shared_to_user",
                        lambda sub: rec.setdefault("to_me", []).append(sub) or [])

    def fake_groups(sub, org_id):  # positionnel strict : droppe l'arg org = TypeError
        rec["groups_for"] = (sub, org_id)
        return list(groups)

    monkeypatch.setattr(group_store, "list_groups_for_user", fake_groups)

    def fake_owned(owners):
        # Deux lectures depuis le 29/09/2026 (une org perso est une org comme une
        # autre) : le jeu POSSÉDÉ de la liste (org, équipes, et moi dans l'org perso),
        # puis mes tableaux perso (`ownership.mes_tableaux_ici`, `[("user", sub)]`),
        # filtrés sur l'org de création. `owners` = la PREMIÈRE, celle que ces bancs
        # figent ; `appels` les garde toutes.
        rec.setdefault("owners", owners)
        rec.setdefault("appels", []).append(owners)
        return rec.get("rows_for", {}).get(tuple(owners), [OWNED])

    def fake_granted(sub, org_ids, group_ids):
        rec["granted_to"] = (sub, org_ids, group_ids)
        return [GRANTED]

    monkeypatch.setattr(D.db, "list_datastores_for_owners", fake_owned)
    monkeypatch.setattr(D.db, "list_datastores_granted_to", fake_granted)
    monkeypatch.setattr(D.ownership, "can_govern", lambda sub, t, rid: False)
    # La liste juge sa gouvernance par lot (`can_govern_many`, même règle).
    monkeypatch.setattr(D.ownership, "can_govern_many",
                        lambda sub, t, owners: {rid: False for rid in owners})


def test_list_datastores_scopes_groups_on_active_org(monkeypatch):
    # Les grants interrogés = org active + TOUS mes groupes DE L'ORG ACTIVE
    # (pas le seul groupe actif, pas les groupes de mes autres orgs).
    rec = {}
    _wire(monkeypatch, rec)
    out = D.make_store("u1").list_datastores()

    assert rec["groups_for"] == ("u1", 99)          # le filtre org est bien passé
    # ADR 0049 (cadrage 10/07) : le contenu possédé = org active + MES équipes de cette
    # org (un tableau team-owned se liste sans grant, comme un projet de pôle).
    # Jamais MOI comme propriétaire du jeu collectif (07/10/2026) : mes tableaux perso
    # passent par `mes_tableaux_ici`, qui les range dans leur org de création — même
    # dans l'org perso (99 ici), où `("user", u1)` ne sert plus qu'aux partages reçus.
    assert rec["owners"] == [("org", "99"), ("group", "5")]
    assert rec["to_me"] == ["u1"]           # org perso : les partages faits à moi
    assert rec["granted_to"] == ("u1", [99], [5])   # grants org active + mes groupes de cette org

    by_id = {e["id"]: e for e in out}
    assert by_id[1]["shared"] is False and by_id[1]["can_write"] is True
    assert by_id[2]["shared"] is True and by_id[2]["permission"] == "read"
    assert by_id[2]["can_write"] is False


def test_list_datastores_org_admin_sees_all_team_tableaux(monkeypatch):
    # Gouvernance inaliénable (ADR 0049) : l'org_admin liste les tableaux de TOUS les
    # pôles de son org, même sans en être membre — même règle que `oto_project op=list`.
    rec = {}
    _wire(monkeypatch, rec, groups=())
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, oid: True)
    monkeypatch.setattr(group_store, "list_groups",
                        lambda org_id: [{"id": 5, "org_id": org_id}, {"id": 6, "org_id": org_id}])
    D.make_store("adm").list_datastores()
    # Un org_admin voit les tableaux de toutes les équipes ; les siens passent par
    # `mes_tableaux_ici` (07/10/2026), jamais ceux des autres personnes
    # (oto-backend#870) : aucun `("user", …)` dans le jeu collectif.
    assert rec["owners"] == [("org", "99"), ("group", "5"), ("group", "6")]


def test_org_store_lists_owned_without_sub(monkeypatch):
    # Store agissant-org (endpoint secret opt-in) : sub-less, contexte = org
    # propriétaire seule, aucun groupe, pas de gouvernance.
    rec = {}
    _wire(monkeypatch, rec, org=None)  # current_org ne doit PAS être consulté
    out = D.make_org_store(99).list_datastores()
    assert rec["owners"] == [("org", "99")]
    assert rec["granted_to"] == (None, [99], [])   # sub=None, org propriétaire, zéro groupe
    assert "groups_for" not in rec                 # pas de scope de groupe (sub-less)
    by_id = {e["id"]: e for e in out}
    assert by_id[1]["can_govern"] is False and by_id[1]["is_personal"] is False


def test_org_store_write_uses_org_principal(monkeypatch):
    # L'écriture d'un store agissant-org se décide sur `org_can_access(org, …)`,
    # jamais sur `can_access(sub, …)` (il n'y a pas de sub).
    seen = {}
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, sub, org_ids, group_ids: {"id": 1} if ns == "leads" else None)
    monkeypatch.setattr(D.ownership, "org_can_access",
                        lambda org_id, t, rid, want="read": seen.setdefault("org", (org_id, want)) or True)
    monkeypatch.setattr(D.ownership, "can_access",
                        lambda *a, **k: pytest.fail("can_access(sub) ne doit pas être appelé en mode org"))
    ns_id = D.make_org_store(99)._resolve("leads", write=True)
    assert ns_id == 1 and seen["org"] == (99, "write")


def test_list_datastores_dedups_owned_over_granted(monkeypatch):
    # Un datastore possédé ET accordé ne sort qu'une fois, en possédé.
    rec = {}
    _wire(monkeypatch, rec)
    monkeypatch.setattr(D.db, "list_datastores_granted_to",
                        lambda sub, org_ids, group_ids: [dict(OWNED, permission="read")])
    out = D.make_store("u1").list_datastores()
    assert [e["id"] for e in out] == [1]
    assert out[0]["shared"] is False


def test_list_datastores_no_active_org_is_empty(monkeypatch):
    # Filet : sans org active (ne devrait plus arriver post-abolition du perso),
    # la liste est vide — pas de retombée sur un scope plus large.
    rec = {}
    _wire(monkeypatch, rec, org=None)
    assert D.make_store("u1").list_datastores() == []
    assert "groups_for" not in rec and "granted_to" not in rec


def test_resolve_by_name_scopes_to_active_org(monkeypatch):
    # RÉGRESSION (fuite cross-org, symétrique au fix projets) : la résolution PAR NOM
    # scope sur l'org active — `resolve_datastore_ns` reçoit [org active] + mes groupes
    # DE CETTE ORG, jamais l'union de toutes mes orgs (`accessor_scope`). Un datastore
    # d'une AUTRE de mes orgs (introuvable dans ce scope) lève DatastoreNotFound.
    rec = {}
    monkeypatch.setattr(access, "current_org", lambda sub: 44)
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org_id: [{"group_id": 7, "org_id": org_id, "name": "x"}])

    def fake_resolve(datastore, *, sub, org_ids, group_ids):
        rec["args"] = (datastore, sub, org_ids, group_ids)
        return None    # possédé par une autre org → hors de [44] → introuvable

    monkeypatch.setattr(D.db, "resolve_datastore_ns", fake_resolve)
    with pytest.raises(D.DatastoreNotFound):
        D.make_store("u1").resolve_ns_id("leads")
    assert rec["args"] == ("leads", "u1", [44], [7])   # org active seule, pas l'union


def test_resolve_finds_active_org_datastore(monkeypatch):
    # Un datastore possédé par l'org active se résout bien (org_ids = [org active]).
    monkeypatch.setattr(access, "current_org", lambda sub: 99)
    monkeypatch.setattr(group_store, "list_groups_for_user", lambda sub, org_id: [])
    monkeypatch.setattr(
        D.db, "resolve_datastore_ns",
        lambda datastore, *, sub, org_ids, group_ids: {"id": 1} if org_ids == [99] else None)
    assert D.make_store("u1").resolve_ns_id("leads") == 1


# ── Endpoint partagé : scope aux tableaux liés au projet + read-only (#193) ──
def test_org_store_scoped_to_allowed_ns_ids(monkeypatch):
    # Le store agissant-org d'un endpoint partagé est SCOPÉ aux tableaux liés au projet
    # (allowed_ns_ids) : list_datastores ne renvoie QUE ces ids (anti-fuite — sans ça
    # l'endpoint exposerait tout le datastore de l'org).
    rec = {}
    _wire(monkeypatch, rec, org=None)          # OWNED id=1, GRANTED id=2
    out = D.make_org_store(99, allowed_ns_ids={1}).list_datastores()
    assert [e["id"] for e in out] == [1]        # id 2 (hors scope) filtré
    assert D.make_org_store(99, allowed_ns_ids=set()).list_datastores() == []  # scope vide = rien


def test_org_store_resolve_outside_scope_not_found(monkeypatch):
    # Résoudre un datastore HORS du scope projet lève DatastoreNotFound (on ne divulgue
    # pas l'existence d'un datastore hors périmètre).
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, *, sub, org_ids, group_ids: {"id": 2})   # existe côté org
    with pytest.raises(D.DatastoreNotFound):
        D.make_org_store(99, allowed_ns_ids={1})._resolve("accords")        # id 2 ∉ {1}
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, *, sub, org_ids, group_ids: {"id": 1})
    assert D.make_org_store(99, allowed_ns_ids={1})._resolve("leads") == 1   # dans le scope → OK


def test_anon_project_scope_resolves_name_and_id_links(monkeypatch):
    # Le scope d'un endpoint partagé couvre les liens tableau par ID **et** par NOM
    # (liens legacy d'avant la normalisation nom→id — vécu sur le projet Marché preprod
    # où data_list_datastores revenait vide car les refs étaient des noms). Les deux
    # arrivent RÉSOLUS par `db.list_project_links` (`datastore_id`) ; un nom qui n'y
    # aboutit pas à un tableau unique (hors portée, ambigu, #365) n'expose rien.
    from oto_mcp.tools import datastore as TD
    monkeypatch.setattr(TD.db, "list_project_links", lambda pid: [
        {"target_type": "tableau", "target_ref": "70", "datastore_id": 70},
        {"target_type": "tableau", "target_ref": "accords_worklist", "datastore_id": 67},
        {"target_type": "tableau", "target_ref": "vivier", "datastore_ambigu": True},
        {"target_type": "tableau", "target_ref": "ailleurs"},          # hors portée
        {"target_type": "procedure", "target_ref": "x"},              # ignoré (pas tableau)
    ])
    monkeypatch.setattr(TD.db, "get_datastore",
                        lambda *a: pytest.fail("résolution par nom hors de list_project_links"))
    assert TD._anon_project_tableau_ns_ids(7) == frozenset({70, 67})
    assert TD._anon_project_tableau_ns_ids(None) == frozenset()   # pas de projet → rien


def test_org_store_read_only_blocks_write(monkeypatch):
    # read_only=True : l'écriture lève DatastoreReadOnly AVANT le check ownership.
    monkeypatch.setattr(D.db, "resolve_datastore_ns",
                        lambda ns, *, sub, org_ids, group_ids: {"id": 1})
    monkeypatch.setattr(D.ownership, "org_can_access",
                        lambda *a, **k: pytest.fail("pas de check ownership en read_only"))
    store = D.make_org_store(99, allowed_ns_ids={1}, read_only=True)
    with pytest.raises(D.DatastoreReadOnly):
        store._resolve("leads", write=True)
    assert store._resolve("leads") == 1        # lecture OK en read_only


def test_un_tableau_qu_on_vient_de_CREER_se_liste_la_ou_il_est_cree(monkeypatch):
    """oto-backend#870 — le banc qui manquait, et son absence explique tout.

    Mesuré en PRODUCTION le 04/09 : `data_create_datastore` rend un id, la liste
    appelée aussitôt ne le montre pas, `data_delete_datastore` le supprime — donc il
    existait. Le créateur conclut que sa création a échoué. Classe `oto#42` : le code
    sait, la réponse ne le dit pas — et son couple, une écriture sans lecteur.

    ⚠️ Aucun banc ne faisait ce trajet-là. Les deux bancs de scope FIGEAIENT le jeu de
    propriétaires sans l'utilisateur, donc ils CERTIFIAIENT l'absence ; ceux de la
    capacité stubbent le store et injectent les verdicts. Chacun prouvait sa moitié,
    aucun ne joignait les deux — le défaut vivait exactement dans l'espace entre eux.
    Un test qui crée puis lit est le seul qui pouvait le voir.
    """
    # Décisions des 29/09 et 07/10/2026 : un tableau créé sans précision est À LA
    # PERSONNE ; il se liste, pour elle seule, dans l'org où elle l'a CRÉÉ
    # (`context_org_id`), et là seulement — l'org perso ne liste plus ceux créés
    # ailleurs. Elle garde ceux SANS org de création (legacy), qui sinon ne sortiraient
    # nulle part. 99 = org de création ; perso=5 en fait une org de travail.
    ici = {"id": 11, "datastore": "cree_ici", "owner_type": "user", "owner_id": "u1",
           "context_org_id": 99, "created_at": "2026-09-29", "schema": None}
    ailleurs = {**ici, "id": 12, "datastore": "cree_ailleurs", "context_org_id": 7}
    sans_org = {**ici, "id": 13, "datastore": "legacy", "context_org_id": None}
    for perso, attendus in ((99, {11, 13}), (5, {11})):
        rec = {"rows_for": {(("user", "u1"),): [ici, ailleurs, sans_org]}}
        _wire(monkeypatch, rec, perso=perso)
        store = D.make_store("u1")
        assert store._default_owner() == ("user", "u1")
        out = store.list_datastores()
        assert [("user", "u1")] in rec["appels"], "mes tableaux perso sont consultés"
        assert {e["id"] for e in out} & {11, 12, 13} == attendus, (
            "un tableau personnel se liste dans son org de création (pour son "
            "propriétaire), et l'org perso garde seulement ceux sans org de création")


def test_dans_une_org_de_TRAVAIL_la_liste_ne_rend_ni_personnel_ni_partage_a_moi(monkeypatch):
    """Décision du 28/09/2026 : hors de l'org perso, la liste ne demande ni mes
    tableaux perso ni ceux partagés à moi en personne — le jeu possédé est l'org et
    mes équipes, et les partages sont ceux faits à l'org ou à mes équipes."""
    rec = {}
    _wire(monkeypatch, rec, perso=5)
    D.make_store("u1").list_datastores()
    assert rec["owners"] == [("org", "99"), ("group", "5")]
    assert rec["granted_to"] == ("u1", [99], [5])
    assert "to_me" not in rec
    # Mes tableaux perso CRÉÉS dans cette org s'y listent pour moi (29/09/2026) : la
    # seconde lecture ne porte que sur moi, filtrée sur l'org de création.
    assert rec["appels"][1:] == [[("user", "u1")]]


def test_parite_recherche_liste(monkeypatch):
    """« Cherchable ⇔ lisible » — l'invariant que le CLAUDE.md pose en critère de
    merge, tenu par un banc plutôt que par une phrase.

    ⚠️ Il a été FAUX une journée (oto-backend#870) : `search._accessible_namespaces`
    interrogeait `active_org_principals` (org + moi + mes groupes) quand
    `list_datastores` n'interrogeait que l'org. Un tableau personnel était donc
    trouvable par la recherche et absent de la liste — et la docstring de la recherche
    AFFIRMAIT la parité, ce qui rendait l'écart invisible à qui lisait le code.

    Les deux appellent maintenant la même fonction. Ce banc compare les JEUX obtenus,
    pas les noms de fonctions : renommer l'une sans l'autre ne le tromperait pas."""
    import inspect
    from oto_mcp import ownership, search

    src_liste = inspect.getsource(D.DatastorePg.list_datastores)
    src_reche = inspect.getsource(search._accessible_namespaces)
    for nom, src in (("liste", src_liste), ("recherche", src_reche)):
        assert "proprietaires_collectifs_de_liste" in src, (
            f"la {nom} n'interroge plus le même jeu de propriétaires que l'autre : "
            "l'invariant « cherchable ⇔ lisible » se rompt en silence, et c'est le "
            "sens de l'écart qui décide s'il cache ou s'il fuit")
        # 28/09/2026 : et elles ajoutent de la même façon, dans l'org perso, ce qui
        # est partagé à moi en personne. Jeux comparés sur base réelle :
        # `tests/test_listes_seule_l_org.py::test_la_recherche_reste_en_parite…`.
        assert "list_datastores_shared_to_user" in src, (
            f"la {nom} ne rend plus les partages faits à moi dans l'org perso : "
            "l'autre face, si")
    # Et le jeu lui-même : trois paliers dans l'org perso, deux ailleurs.
    monkeypatch.setattr(ownership.group_store, "list_groups_for_user",
                        lambda sub, org: [{"group_id": 5}])
    monkeypatch.setattr(ownership.org_store, "home_org", lambda sub: 99)
    assert ownership.principaux_de_liste("u1", 99) == [
        ("org", "99"), ("user", "u1"), ("group", "5")]
    assert ownership.principaux_de_liste("u1", 42) == [("org", "42"), ("group", "5")]
    # Le jeu POSSÉDÉ, lui, n'est jamais personnel (07/10/2026), org perso comprise.
    assert ownership.proprietaires_collectifs_de_liste("u1", 99) == [
        ("org", "99"), ("group", "5")]
