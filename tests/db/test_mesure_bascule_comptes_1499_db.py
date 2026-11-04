"""`scripts/mesure_bascule_comptes_1499.py` (#1499, étape 3, L2) sur une vraie base.

Un jeu minimal par catégorie : prêts entre orgs (coffre, Unipile), clé personnelle d'une
autre org (#172), ambiguïté nouvelle (défaut d'équipe non possédé), clé suspendue avec
repli, pointeur Unipile, équipe héritable d'un projet partagé, et le journal (palier,
perte d'accès, niveau inférieur à l'exercé). La sortie ne porte que des nombres ; la
connexion refuse toute écriture.
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout

import pytest

psycopg = pytest.importorskip("psycopg")

# La racine du dépôt n'est pas dans `sys.path` en CI (cf. test_audit_sieges_580).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts import mesure_bascule_comptes_1499 as mesure  # noqa: E402

_U = uuid.uuid4().hex[:8]
SUBS = {n: f"sub-{n}-{_U}" for n in ("proprio", "pair", "etranger", "nomade", "equipier",
                                    "heritier", "lecteur")}
NOMS_D_ORG = (f"Org Secrete A {_U}", f"Org Secrete B {_U}")


def _c():
    import os
    from psycopg.rows import dict_row
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True, row_factory=dict_row)


def _x(sql, params=()):
    with _c() as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else None


def _compte(owner_type, owner_id, connector, account="", share_side=(), meta=None):
    _x("INSERT INTO connector_credentials (entity_type, entity_id, connector, account, "
       "secret_enc, share_side, meta) VALUES (%s, %s, %s, %s, 'x', %s, %s)",
       (owner_type, owner_id, connector, account, json.dumps(list(share_side)),
        json.dumps(meta or {})))
    return _x("INSERT INTO connector_instances (connector, owner_type, owner_id, account) "
              "VALUES (%s, %s, %s, %s) RETURNING id",
              (connector, owner_type, owner_id, account))[0]["id"]


def _appel(sub, org, tool, ref, key_mode, args=None):
    _x("INSERT INTO tool_calls (sub, org_id, tool, args, key_mode) VALUES (%s, %s, %s, %s, %s)",
       (SUBS[sub], org, tool, json.dumps({**(args or {}), "instance": ref}), key_mode))


@pytest.fixture(scope="module")
def base(live):
    from oto_mcp import db, org_store
    from oto_mcp.instance_refs import make_group_ref, make_member_ref, make_platform_ref
    a, b = (org_store.create_org(n) for n in NOMS_D_ORG)
    for nom, sub in SUBS.items():
        db.upsert_user(sub, email=f"{nom}-{_U}@exemple.test", name=f"Nom {nom} {_U}")
    membres = {a: ("proprio", "pair", "nomade", "equipier", "heritier", "lecteur"),
               b: ("etranger", "nomade")}
    for org, noms in membres.items():
        for n in noms:
            _x("INSERT INTO org_members (org_id, sub) VALUES (%s, %s)", (org, SUBS[n]))
    equipe = _x("INSERT INTO org_groups (org_id, name) VALUES (%s, %s) RETURNING id",
                (a, f"Equipe {_U}"))[0]["id"]
    _x("INSERT INTO org_group_members (group_id, sub, is_active) VALUES (%s, %s, TRUE)",
       (equipe, SUBS["equipier"]))

    # Prêt du coffre : à un pair de l'org (repris) et à une personne d'une autre org.
    zoho = _compte("member", f"{a}:{SUBS['proprio']}", "zoho",
                   share_side=[f"user:{SUBS['pair']}", f"user:{SUBS['etranger']}"])
    _x("INSERT INTO connector_account_rules (org_id, instance_id, subject_user, level) "
       "VALUES (%s, %s, %s, 'read')", (a, zoho, SUBS["lecteur"]))
    # Prêt Unipile entre orgs, et un pointeur vers le compte d'autrui (prêt dans l'org).
    db.set_unipile_account(SUBS["proprio"], f"ACC{_U}", f"Compte {_U}", org_id=a,
                           provider="LINKEDIN")
    db.set_account_grant(SUBS["proprio"], "LINKEDIN", f"ACC{_U}", SUBS["etranger"],
                         granted_by=SUBS["proprio"])
    db.set_account_grant(SUBS["proprio"], "LINKEDIN", f"ACC{_U}", SUBS["pair"],
                         granted_by=SUBS["proprio"])
    _x("INSERT INTO unipile_operated_accounts (sub, provider, account_id, owner_sub) "
       "VALUES (%s, 'LINKEDIN', %s, %s)", (SUBS["pair"], f"ACC{_U}", SUBS["proprio"]))
    # Avant L1 : la sous-instance n'existe pas, la mesure la simule.
    _x("DELETE FROM connector_instances WHERE starts_with(account, 'canal:')")
    # #172 : la clé Unipile du nomade vit dans l'org B seulement.
    _compte("member", f"{b}:{SUBS['nomade']}", "unipile")
    # Deux comptes d'équipe, départagés aujourd'hui par un défaut d'équipe.
    _compte("group", str(equipe), "attio", "a1", meta={"is_default": True})
    _compte("group", str(equipe), "attio", "a2")
    # Clé personnelle suspendue, l'org a la sienne.
    _compte("member", f"{a}:{SUBS['pair']}", "pennylane", meta={"suspended": True})
    _compte("org", str(a), "pennylane")
    # Un projet de l'équipe partagé avec héritage des clés.
    projet = _x("INSERT INTO projects (owner_type, owner_id, name) VALUES ('group', %s, %s) "
                "RETURNING id", (str(equipe), f"Projet {_U}"))[0]["id"]
    _x("INSERT INTO grants (resource_kind, resource_id, grantor_kind, grantor_id, "
       "grantee_kind, grantee_id) VALUES ('project_credentials', %s, 'user', %s, 'user', %s)",
       (f"project:{projet}", SUBS["equipier"], SUBS["heritier"]))

    pret = make_member_ref(a, SUBS["proprio"], "zoho")
    _appel("heritier", a, "attio", make_group_ref(equipe, "attio", "a1"), "group",
           {"op": "list"})
    _appel("nomade", a, "linkedin_unipile", make_member_ref(b, SUBS["nomade"], "unipile"),
           "user")
    _appel("etranger", b, "gmail_act", pret, "user", {"op": "send"})
    _appel("pair", a, "gmail_act", pret, "user", {"op": "send"})
    _appel("lecteur", a, "gmail_act", pret, "user", {"op": "send"})
    _appel("pair", a, "serper_search", make_platform_ref("serper", "cle"), "platform")
    return {"a": a, "b": b, "equipe": equipe}


@pytest.fixture(scope="module")
def sortie(base):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert mesure.main([]) == 0
    return json.loads(out.getvalue()), err.getvalue()


def test_chaque_categorie_est_comptee(sortie):
    m, _ = sortie
    assert m["sous_instances_unipile_simulees"] == 1
    assert m["prets_du_coffre_entre_orgs"] == {"total": 1, "par_connecteur": {"zoho": 1}}
    assert m["prets_unipile_entre_orgs"] == {"total": 1, "par_canal": {"LINKEDIN": 1}}
    autre_org = m["cles_perso_d_une_autre_org"]
    # Le nomade est aussi membre de son org personnelle : un couple, plusieurs orgs.
    assert autre_org["personne_x_connecteur"] == 1
    assert autre_org["sans_autre_repli"] == autre_org["personne_x_org_x_connecteur"] >= 1
    assert m["comptes_ambigus"]["nouveaux"] == {
        "total": 1, "par_palier": {"equipe": 1}, "par_connecteur": {"attio": 1}}
    susp = m["cles_perso_suspendues"]
    assert (susp["avec_repli_aujourd_hui"], susp["premier_repli"]) == (1, {"org": 1})
    assert m["pointeurs_unipile_vers_autrui"]["par_etat"] == {"pret_vivant_meme_org": 1}
    her = m["equipes_heritees_projet_partage"]
    assert (her["equipes_heritables"], her["comptes_d_equipe_heritables_par_connecteur"]) \
        == (1, {"attio": 2})


def test_le_journal_dit_les_pertes_les_paliers_et_le_niveau(sortie):
    j = sortie[0]["journal"]
    assert {m: v["appels"] for m, v in j["pertes_d_acces"].items()} == {
        "cle_perso_d_une_autre_org": 1, "equipe_hors_appartenance": 1, "pret_sans_regle": 1}
    assert j["appels_plateforme_tenant_par_ref"] == {"platform": 1}
    assert j["appels_par_palier"]["platform"] == 1
    # Le lecteur n'a qu'une règle `read` et a envoyé ; le pair a la règle reprise.
    assert j["niveau_inferieur_a_l_exerce"] == {"personne_x_compte": 1, "appels": 1}


def test_la_sortie_ne_porte_aucun_sub_adresse_ni_nom(sortie):
    m, resume = sortie
    texte = json.dumps(m, ensure_ascii=False) + resume
    interdits = [*SUBS.values(), *NOMS_D_ORG, _U, "@exemple.test"]
    assert not [x for x in interdits if x in texte]
    assert "Prêts du coffre entre orgs" in resume


def test_sans_les_tables_de_l1_la_comparaison_se_tait(base):
    with mesure.lecture_seule() as conn:
        d = mesure.lire(conn)
    m = mesure.mesurer({**d, "l1": False, "regles_en_base": []})
    assert m["regles_voulues"]["comparaison_avec_la_base"] is None
    assert m["prets_du_coffre_entre_orgs"]["total"] == 1


def test_la_connexion_refuse_toute_ecriture(base):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with mesure.lecture_seule() as conn:
            assert conn.execute("SHOW default_transaction_read_only").fetchone() == {
                "default_transaction_read_only": "on"}
            conn.execute("INSERT INTO org_groups (org_id, name) VALUES (%s, 'x')",
                         (base["a"],))
    assert not _x("SELECT 1 FROM org_groups WHERE name = 'x'")
