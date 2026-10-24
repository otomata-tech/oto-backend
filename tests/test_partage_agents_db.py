"""Le partage d'agents EN BASE — ce qu'une doublure ne peut pas prouver.

1. `partages_d_agents` lit en UNE requête le meilleur partage VIVANT parmi les
   principaux de l'appelant (`write` l'emporte, un partage échu ne compte pas).
2. Supprimer un agent retire ses partages.
3. Le forfait d'un propriétaire se prête à qui tient l'agent en ÉCRITURE — personne,
   équipe ou org dont il est membre, quel que soit l'auteur du partage
   (`forfaits_pretes`), et la garde d'écriture de `update_trigger` le lit elle aussi.
"""
from __future__ import annotations

import psycopg

ORG = 8200


def _agent(db, sub="proprio", org=ORG):
    return db.create_trigger(org, sub, procedure="veille", tz="UTC", tools=["a"],
                             cron="0 8 * * *")


def test_le_meilleur_partage_vivant_parmi_les_principaux(live, pg_module_dsn):
    from oto_mcp import db
    a, b, c = _agent(db), _agent(db), _agent(db)
    db.grant_resource("runner_trigger", str(a["id"]), "user", "lecteur", role="viewer")
    db.grant_resource("runner_trigger", str(a["id"]), "org", str(ORG), role="editor")
    db.grant_resource("runner_trigger", str(b["id"]), "user", "lecteur", role="viewer")
    db.grant_resource("runner_trigger", str(c["id"]), "user", "lecteur", role="editor")
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_id = %s", (str(c["id"]),))
    principaux = [("user", "lecteur"), ("org", str(ORG))]
    assert db.partages_d_agents([a["id"], b["id"], c["id"]], principaux) == {
        a["id"]: "write", b["id"]: "read"}, (
        "l'org en écriture l'emporte sur la personne en lecture ; un partage échu ne "
        "donne rien")
    assert db.partages_d_agents([a["id"]], [("user", "autre")]) == {}
    assert db.partages_d_agents([], principaux) == {}


def test_supprimer_un_agent_retire_ses_partages(live):
    from oto_mcp import db
    t = _agent(db)
    db.grant_resource("runner_trigger", str(t["id"]), "user", "editeur", role="editor")
    assert db.delete_trigger(t["id"], ORG)
    assert db.list_resource_grants("runner_trigger", str(t["id"])) == []


def test_le_forfait_se_prete_a_qui_tient_l_agent_en_ecriture(live, pg_module_dsn):
    """Un partage `write` vivant — à la personne, à son équipe, ou à toute l'org dont
    elle est membre — prête le forfait du propriétaire, quel que soit son auteur.
    Lecture, partage échu ou partage à une org dont on n'est pas membre : rien."""
    from oto_mcp import db
    t = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC", tools=["a"],
                          cron="0 8 * * *", model="sub:sonnet")
    autre = _agent(db)   # un agent sur une clé d'org : rien à prêter
    rid, tid = str(t["id"]), t["id"]
    db.grant_resource("runner_trigger", rid, "user", "nomme", role="editor",
                      granted_by="proprio")
    db.grant_resource("runner_trigger", rid, "user", "par-admin", role="editor",
                      granted_by="un-admin")
    db.grant_resource("runner_trigger", rid, "user", "lecteur", role="viewer",
                      granted_by="proprio")
    assert db.forfaits_pretes([tid, autre["id"]], "nomme") == {tid}
    assert db.forfaits_pretes([tid], "par-admin") == {tid}
    for sub in ("lecteur", "membre"):
        assert db.forfaits_pretes([tid], sub) == set(), sub

    # Toute l'org en écriture : chaque MEMBRE emprunte le forfait, pas un inconnu.
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, 'o') ON CONFLICT DO NOTHING",
                     (ORG,))
        conn.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, 'membre') "
                     "ON CONFLICT DO NOTHING", (ORG,))
    db.grant_resource("runner_trigger", rid, "org", str(ORG), role="editor",
                      granted_by="proprio")
    assert db.forfaits_pretes([tid], "membre") == {tid}
    assert db.forfaits_pretes([tid], "dehors") == set()

    # La garde d'écriture lit le même prêt.
    assert db.update_trigger(tid, ORG, {"input": "x"},
                             hors_abonnement_d_autrui="dehors") is None
    assert db.update_trigger(tid, ORG, {"input": "par l'équipe"},
                             hors_abonnement_d_autrui="membre")["input"] == "par l'équipe"

    # Échu, le partage ne prête plus rien.
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_id = %s AND principal_id IN ('nomme', %s)",
                     (rid, str(ORG)))
    assert db.forfaits_pretes([tid], "nomme") == set()
    assert db.forfaits_pretes([tid], "membre") == set()


def test_les_outils_plateforme_ne_valent_que_pour_l_agent_au_seul_proprietaire(live):
    """Ce qui permet à un travail d'exercer des `oto_admin_*` avec le rôle plateforme de
    son porteur (`verrou_org.outils_permis`) : la liste de son agent, SEULEMENT si cet agent
    n'est modifiable que par son porteur — abonnement personnel, AUCUN partage en écriture
    (à une personne, une équipe ou l'org, posé par qui que ce soit). Un partage en lecture
    ne retire rien. La liste ne suit pas l'agent à un transfert."""
    from oto_mcp import db
    liste = ["oto_admin_org", "oto_admin_signal"]

    def travail(t, sub="proprio", org=ORG):
        # `start` : le seul coup d'envoi d'un agent programmé (`runner_jobs.kind`).
        return db.enqueue_job(org, "start", {"trigger_id": t["id"]}, sub=sub)["id"]

    def arme(t):
        return db.update_trigger(t["id"], ORG, {"outils_plateforme": liste})

    seul = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                             tools=["a", *liste], cron="0 8 * * *", model="sub:sonnet")
    assert seul["platform_tools"] == [], "aucun outil d'admin par défaut"
    assert db.outils_plateforme_du_travail(travail(seul)) == []
    assert arme(seul)["platform_tools"] == liste
    db.grant_resource("runner_trigger", str(seul["id"]), "org", str(ORG), role="viewer",
                      granted_by="proprio")
    assert db.outils_plateforme_du_travail(travail(seul)) == liste, "lecture : rien"

    a_l_org = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                                tools=["a", *liste], cron="0 8 * * *", model="sub:sonnet")
    arme(a_l_org)
    db.grant_resource("runner_trigger", str(a_l_org["id"]), "org", str(ORG),
                      role="editor", granted_by="proprio")
    assert db.outils_plateforme_du_travail(travail(a_l_org)) == [], "l'org l'écrit"

    prete = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                              tools=["a", *liste], cron="0 8 * * *", model="sub:sonnet")
    arme(prete)
    db.grant_resource("runner_trigger", str(prete["id"]), "user", "nomme", role="editor",
                      granted_by="proprio")
    assert db.outils_plateforme_du_travail(travail(prete)) == [], "prêté à un éditeur"

    cle_d_org = _agent(db)   # tout admin de l'org le retouche
    arme(cle_d_org)
    assert db.outils_plateforme_du_travail(travail(cle_d_org)) == [], "clé d'org"
    assert db.outils_plateforme_du_travail(travail(seul, sub="autre")) == [], "autre porteur"
    assert db.outils_plateforme_du_travail(
        db.enqueue_job(ORG, "start", {}, sub="proprio")["id"]) == [], "enfilé à la main"
    assert db.outils_plateforme_du_travail(-1) == [], "introuvable"

    repris = db.reprendre_trigger(seul["id"], ORG, "nouveau")
    assert repris[0]["platform_tools"] == [], "la liste ne suit pas l'agent"
    assert db.outils_plateforme_du_travail(travail(seul, sub="nouveau")) == []
