"""Le partage d'agents EN BASE — ce qu'une doublure ne peut pas prouver.

1. `partages_d_agents` lit en UNE requête le meilleur partage VIVANT parmi les
   principaux de l'appelant (`write` l'emporte, un partage échu ne compte pas).
2. Supprimer un agent retire ses partages.
3. Le forfait d'un propriétaire se prête à la PERSONNE qu'il a nommée éditrice (prêt
   nommé, inchangé) ; à une équipe ou à l'org entière partagées par lui, seulement par
   SON geste (`poser_pret_du_forfait`, sous le modèle courant) — un changement de
   modèle ou une reprise le rendent caduc. La garde d'écriture de `update_trigger` lit
   le même prêt.
4. Une retouche enregistre son auteur (`updated_by`).
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


def _org_reelle(dsn, *membres):
    """Une org en base, ses membres, et une équipe de l'org avec le premier membre : le
    prêt relit l'appartenance en base (équipe, org entière)."""
    from oto_mcp import group_store
    with psycopg.connect(dsn, autocommit=True) as c:
        org = c.execute("INSERT INTO orgs (name) VALUES ('Prêt') RETURNING id").fetchone()[0]
        for m in membres:
            c.execute("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (org, m))
    equipe = group_store.create_group(org, "Équipe du prêt")
    group_store.add_group_member(equipe, membres[0])
    return org, equipe


def test_le_pret_nomme_reste_celui_d_avant(live, pg_module_dsn):
    """Un partage `write` À UNE PERSONNE posé par le propriétaire prête, sans geste ni
    lien au modèle ; ni un partage d'admin, ni la lecture, ni l'org de la révision 0035."""
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
    db.grant_resource("runner_trigger", rid, "org", str(ORG), role="editor",
                      granted_by="migration:0035_agents_partages_a_l_org")
    assert db.forfaits_pretes([tid, autre["id"]], "nomme") == {tid}
    assert db.forfaits_pretes([tid], "nomme", nomme=True) == {tid}
    for sub in ("par-admin", "lecteur", "quelqu-un-de-l-org"):
        assert db.forfaits_pretes([tid], sub) == set(), sub
        assert db.update_trigger(tid, ORG, {"input": "x"},
                                 hors_abonnement_d_autrui=sub) is None, sub
    ecrit = db.update_trigger(tid, ORG, {"input": "par le nommé"},
                              hors_abonnement_d_autrui="nomme", par="nomme")
    assert (ecrit["input"], ecrit["updated_by"]) == ("par le nommé", "nomme")
    # Le modèle change : le prêt nommé ne dépend pas du modèle.
    db.update_trigger(tid, ORG, {"model": "sub:opus"}, par="nomme")
    assert db.forfaits_pretes([tid], "nomme") == {tid}
    # Échu, le partage ne prête plus rien.
    with psycopg.connect(pg_module_dsn, autocommit=True) as conn:
        conn.execute("UPDATE resource_grants SET expires_at = NOW() - interval '1 day' "
                     "WHERE resource_id = %s AND principal_id = 'nomme'", (rid,))
    assert db.forfaits_pretes([tid], "nomme") == set()
    assert db.update_trigger(tid, ORG, {"input": "y"},
                             hors_abonnement_d_autrui="nomme") is None


def test_l_equipe_et_l_org_n_empruntent_que_par_le_geste(live, pg_module_dsn):
    from oto_mcp import db
    org, equipe = _org_reelle(pg_module_dsn, "equipier", "membre", "nomme")
    t = db.create_trigger(org, "proprio", procedure="veille", tz="UTC", tools=["a"],
                          cron="0 8 * * *", model="sub:sonnet")
    rid, tid = str(t["id"]), t["id"]
    db.grant_resource("runner_trigger", rid, "group", str(equipe), role="editor",
                      granted_by="proprio")
    db.grant_resource("runner_trigger", rid, "org", str(org), role="editor",
                      granted_by="proprio")
    db.grant_resource("runner_trigger", rid, "user", "nomme", role="editor",
                      granted_by="proprio")
    # Partagés AVANT tout geste : fermés ; le nommé emprunte comme avant.
    assert t["subscription_lent"] is False
    for sub in ("equipier", "membre"):
        assert db.forfaits_pretes([tid], sub) == set(), sub
        assert db.update_trigger(tid, org, {"input": "x"},
                                 hors_abonnement_d_autrui=sub, par=sub) is None, sub
    assert db.forfaits_pretes([tid], "nomme") == {tid}

    # Seul le propriétaire courant, sous le modèle courant, pose le prêt.
    assert db.poser_pret_du_forfait(tid, org, "un-admin", "sub:sonnet") is None
    assert db.poser_pret_du_forfait(tid, org, "proprio", "sub:opus") is None
    assert db.poser_pret_du_forfait(tid, org, "proprio", "sub:sonnet")["subscription_lent"]
    assert db.forfaits_pretes([tid], "equipier") == {tid}, "l'équipe nommée par lui"
    assert db.forfaits_pretes([tid], "membre") == {tid}, "l'org, membre réel"
    assert db.forfaits_pretes([tid], "dehors") == set(), "pas un étranger à l'org"
    assert db.forfaits_pretes([tid], "equipier", nomme=True) == set()
    assert db.update_trigger(tid, org, {"input": "par l'équipe"},
                             hors_abonnement_d_autrui="equipier",
                             par="equipier")["updated_by"] == "equipier"

    # Le modèle change : caduc ; un nouveau geste le rouvre ; le retrait le referme.
    db.update_trigger(tid, org, {"model": "sub:opus"}, par="proprio")
    assert db.forfaits_pretes([tid], "membre") == set()
    assert db.poser_pret_du_forfait(tid, org, "proprio", "sub:opus")["subscription_lent"]
    assert db.forfaits_pretes([tid], "membre") == {tid}
    assert db.poser_pret_du_forfait(tid, org, "proprio", None)["subscription_lent"] is False
    assert db.forfaits_pretes([tid], "membre") == set()
    assert db.forfaits_pretes([tid], "nomme") == {tid}, "le nommé n'en dépend pas"


def test_a_la_reprise_le_pret_ne_suit_pas(live, pg_module_dsn):
    from oto_mcp import db
    org, _ = _org_reelle(pg_module_dsn, "membre", "nouveau")
    t = db.create_trigger(org, "proprio", procedure="veille", tz="UTC", tools=["a"],
                          cron="0 8 * * *", model="sub:sonnet")
    db.grant_resource("runner_trigger", str(t["id"]), "org", str(org), role="editor",
                      granted_by="proprio")
    db.poser_pret_du_forfait(t["id"], org, "proprio", "sub:sonnet")
    db.update_trigger(t["id"], org, {"input": "x"}, par="membre")
    repris, ancien, _ = db.reprendre_trigger(t["id"], org, "nouveau")
    assert (ancien, repris["sub"]) == ("proprio", "nouveau")
    assert repris["subscription_lent"] is False, "le prêt de l'ancien s'efface"
    assert repris["updated_by"] is None, "un transfert n'est pas une retouche"
    assert db.forfaits_pretes([t["id"]], "membre") == set()
    # Même si le repreneur repartage à l'org : sans SON geste, rien ne se prête.
    db.grant_resource("runner_trigger", str(t["id"]), "org", str(org), role="editor",
                      granted_by="nouveau")
    assert db.forfaits_pretes([t["id"]], "membre") == set()


def test_les_outils_plateforme_ne_valent_que_pour_l_agent_au_seul_proprietaire(live):
    """Ce qui permet à un travail d'exercer des `oto_admin_*` avec le rôle plateforme de
    son porteur (`verrou_org.outils_permis`) : la liste de son agent, SEULEMENT si cet agent
    n'est modifiable que par son porteur — abonnement personnel, aucun éditeur nommé par
    lui, aucun prêt collectif vivant. Un partage à l'org entière ou posé par un admin ne
    prête rien sans le geste. La liste ne suit pas l'agent à un transfert."""
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
    db.grant_resource("runner_trigger", str(seul["id"]), "org", str(ORG), role="editor",
                      granted_by="proprio")
    db.grant_resource("runner_trigger", str(seul["id"]), "user", "par-admin",
                      role="editor", granted_by="un-admin")
    assert db.outils_plateforme_du_travail(travail(seul)) == liste

    prete = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                              tools=["a", *liste], cron="0 8 * * *", model="sub:sonnet")
    arme(prete)
    db.grant_resource("runner_trigger", str(prete["id"]), "user", "nomme", role="editor",
                      granted_by="proprio")
    assert db.outils_plateforme_du_travail(travail(prete)) == [], "prêté à un éditeur"

    # Un éditeur nommé qui est lui-même opérateur plateforme n'emprunte rien : la liste
    # tient. Qu'il perde ce rôle, et elle se referme au travail suivant.
    entre_admins = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                                     tools=["a", *liste], cron="0 8 * * *", model="sub:sonnet")
    arme(entre_admins)
    db.upsert_user("autre-operateur", email="op@example.com")
    db.set_user_role("autre-operateur", "admin")
    db.grant_resource("runner_trigger", str(entre_admins["id"]), "user", "autre-operateur",
                      role="editor", granted_by="proprio")
    assert db.outils_plateforme_du_travail(travail(entre_admins)) == liste, "éditeur opérateur"
    db.set_user_role("autre-operateur", "member")
    assert db.outils_plateforme_du_travail(travail(entre_admins)) == [], "rôle perdu"
    # Un `admin` éditeur de l'agent d'un `super_admin` emprunterait le palier `super` :
    # la liste se ferme ; un `super_admin` éditeur n'emprunte rien.
    db.upsert_user("proprio", email="proprio@example.com")
    db.set_user_role("proprio", "super_admin")
    db.set_user_role("autre-operateur", "admin")
    assert db.outils_plateforme_du_travail(travail(entre_admins)) == [], "palier inférieur"
    db.set_user_role("autre-operateur", "super_admin")
    assert db.outils_plateforme_du_travail(travail(entre_admins)) == liste, "même palier"
    db.set_user_role("autre-operateur", "member")
    db.set_user_role("proprio", "member")

    collectif = db.create_trigger(ORG, "proprio", procedure="veille", tz="UTC",
                                  tools=["a", *liste], cron="0 8 * * *", model="sub:sonnet")
    arme(collectif)
    db.grant_resource("runner_trigger", str(collectif["id"]), "org", str(ORG),
                      role="editor", granted_by="proprio")
    assert db.outils_plateforme_du_travail(travail(collectif)) == liste, "sans le geste"
    db.poser_pret_du_forfait(collectif["id"], ORG, "proprio", "sub:sonnet")
    assert db.outils_plateforme_du_travail(travail(collectif)) == [], "prêt collectif"
    db.update_trigger(collectif["id"], ORG, {"model": "sub:opus"})
    assert db.outils_plateforme_du_travail(travail(collectif)) == liste, "prêt caduc"

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
