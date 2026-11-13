"""Le cycle d'un effacement, de la demande au reçu (docs/effacement-d-org.md).

Sur une vraie base : la demande part chez le titulaire (le mail est capturé, le lien
en est tiré), seul le titulaire confirme, la confirmation suspend l'org, l'annulation
la rouvre ; puis le travail de fond, au terme du délai, purge, écrit le reçu et retire
de la demande ce qui était personnel. Les gestes HORS base (stockage, Unipile, Logto,
archives du journal) sont remplacés : chacun a son banc.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg.rows import dict_row  # noqa: E402

from oto_mcp import config, org_store  # noqa: E402
from oto_mcp.capabilities import org_erasure as cap  # noqa: E402
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx  # noqa: E402
from oto_mcp.effacement import demandes, externes, travail  # noqa: E402

TITULAIRE, MEMBRE, AUTRE = "cycle-titulaire", "cycle-membre", "cycle-autre"


@pytest.fixture(scope="module")
def base(live, pg_module_dsn):
    with psycopg.connect(pg_module_dsn, autocommit=True, row_factory=dict_row) as c:
        for s in (TITULAIRE, MEMBRE, AUTRE):
            c.execute("INSERT INTO users (sub, email) VALUES (%s, %s)", (s, f"{s}@exemple.test"))
        o = c.execute("INSERT INTO orgs (name, created_by) VALUES ('cycle', %s) RETURNING id",
                      (TITULAIRE,)).fetchone()["id"]
        autre = c.execute("INSERT INTO orgs (name) VALUES ('ailleurs') RETURNING id").fetchone()["id"]
        c.execute("INSERT INTO org_members (org_id, sub, org_role) VALUES (%s, %s, 'org_admin'),"
                  " (%s, %s, 'org_member'), (%s, %s, 'org_admin')",
                  (o, TITULAIRE, o, MEMBRE, autre, AUTRE))
        c.execute("INSERT INTO projects (owner_type, owner_id, name) VALUES ('org', %s, 'p')",
                  (str(o),))
    yield {"dsn": pg_module_dsn, "org": o}


# Partagé par les tests du module : ils se suivent comme les temps d'un même cycle.
_ENVOYES: list[dict] = []


@pytest.fixture
def mails(monkeypatch):
    # Une instance qui purge : sans l'interrupteur, demander et confirmer sont refusés.
    monkeypatch.setenv(travail.ENV_ACTIF, "1")
    envoyes = _ENVOYES
    monkeypatch.setattr(cap, "send_org_erasure_confirm_email",
                        lambda to, **kw: envoyes.append({"to": to, **kw}) or True)
    monkeypatch.setattr(travail, "_prevenir", lambda d, m: envoyes.append({"fin": d["id"]}))
    monkeypatch.setattr(config, "invite_base_url", lambda: "https://app.exemple.test")
    return envoyes


def _ctx(sub, org_id=None):
    return ResolvedCtx(sub=sub, org_id=org_id, role="member")


def _demander(org_id, sub=TITULAIRE, **kw):
    return cap._org_erasure(_ctx(sub, org_id), cap.OrgErasureInput(op="request", org_id=org_id, **kw))


def _jeton(mails) -> str:
    return mails[-1]["confirm_url"].rsplit("/", 1)[1]


def _confirmer(sub, jeton, op="confirm"):
    return cap._org_erasure_confirm(_ctx(sub), cap.ErasureConfirmInput(op=op, token=jeton))


def test_une_instance_qui_ne_purge_pas_refuse_la_demande(base, mails, monkeypatch):
    monkeypatch.delenv(travail.ENV_ACTIF)
    with pytest.raises(AuthzDenied) as e:
        _demander(base["org"])
    assert e.value.code == "erasure_disabled"


def test_le_lien_part_chez_le_titulaire_et_lui_seul_confirme(base, mails):
    out = _demander(base["org"])
    assert out["erasure"]["state"] == "requested" and out["emailed"] is True
    assert mails[-1]["to"] == f"{TITULAIRE}@exemple.test"
    assert out["erasure"]["confirm_to"] == "c***@exemple.test"
    jeton = _jeton(mails)
    assert _confirmer(AUTRE, jeton, "preview")["is_recipient"] is False
    with pytest.raises(AuthzDenied) as e:
        _confirmer(AUTRE, jeton)
    assert e.value.status == 403
    with pytest.raises(AuthzDenied) as e:
        _demander(base["org"])
    assert e.value.code == "erasure_pending"
    fait = _confirmer(TITULAIRE, jeton)
    assert fait["erasure"]["state"] == "scheduled"
    assert org_store.get_org_suspension(base["org"]) is not None
    # Usage unique : le même lien ne sert plus.
    with pytest.raises(AuthzDenied) as e:
        _confirmer(TITULAIRE, jeton)
    assert e.value.code == "invalid_link"


def test_annuler_rouvre_l_org(base, mails):
    out = cap._org_erasure(_ctx(TITULAIRE, base["org"]),
                           cap.OrgErasureInput(op="cancel", org_id=base["org"]))
    assert out["erasure"]["state"] == "cancelled"
    assert org_store.get_org_suspension(base["org"]) is None


def test_un_super_admin_non_membre_doit_donner_un_motif(base, mails):
    with pytest.raises(AuthzDenied) as e:
        _demander(base["org"], sub=AUTRE)
    assert e.value.code == "missing_reason"
    out = _demander(base["org"], sub=AUTRE, reason="demande RGPD reçue par mail")
    assert out["erasure"]["via"] == "operator"
    assert mails[-1]["par_operateur"] is True


def test_l_org_reste_suspendue_tant_que_l_effacement_est_confirme(base, mails):
    from oto_mcp.capabilities import org_suspension as susp
    _confirmer(TITULAIRE, _jeton(mails))
    with pytest.raises(AuthzDenied) as e:
        susp._org_suspension(_ctx("operateur"), susp.OrgSuspensionInput(op="resume",
                                                                        org_id=base["org"]))
    assert e.value.code == "erasure_scheduled"
    assert org_store.get_org_suspension(base["org"]) is not None
    # Le message servi aux membres dit « restaurer », pas « prendre un plan ».
    from oto_mcp import org_suspension as garde
    assert "restaurer" in garde.message(base["org"])


def test_sans_l_interrupteur_le_travail_n_ecrit_rien(base, mails, monkeypatch):
    monkeypatch.delenv(travail.ENV_ACTIF, raising=False)
    d = demandes.vivante(base["org"])
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("UPDATE org_erasures SET scheduled_for = NOW() - interval '1 second' "
                  "WHERE id = %s", (d["id"],))
    assert travail.passer() == {"actif": False, "a_faire": [d["id"]]}
    assert demandes.lire(d["id"])["state"] == "scheduled"
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("UPDATE org_erasures SET scheduled_for = NOW() + interval '7 days' "
                  "WHERE id = %s", (d["id"],))


def test_le_travail_purge_au_terme_du_delai_puis_retire_le_personnel(base, mails, monkeypatch):
    monkeypatch.setenv(travail.ENV_ACTIF, "1")
    d = demandes.vivante(base["org"])
    # Avant le terme : rien ne bouge.
    monkeypatch.setattr(externes, "supprimer_objets", lambda cles: len(cles))
    monkeypatch.setattr(externes, "liberer_sieges_unipile", lambda o: {"rendus": 0})
    monkeypatch.setattr(externes, "supprimer_organisation_logto", lambda i: False)
    supprimes = []
    monkeypatch.setattr(externes, "supprimer_comptes_de_connexion",
                        lambda subs: supprimes.extend(subs) or {"supprimes": len(subs)})
    monkeypatch.setattr(travail.archives, "reecrire", lambda *a, **kw: {"mois_lus": 0})
    from oto_mcp import media_store
    monkeypatch.setattr(media_store, "public_base", lambda: "https://stockage.exemple.test")
    monkeypatch.setattr(media_store, "client_et_seau", lambda: (None, "seau"))
    assert travail.passer() == {"expirees": 0, "terminees": 0, "en_attente": 0, "echecs": 0}
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("UPDATE org_erasures SET scheduled_for = NOW() - interval '1 second' "
                  "WHERE id = %s", (d["id"],))
    # Les comptes de connexion partent, puis la base ATTEND l'échéance des jetons émis.
    assert travail.passer()["en_attente"] == 1
    assert sorted(supprimes) == sorted([TITULAIRE, MEMBRE])
    from datetime import datetime, timedelta, timezone
    il_y_a_2h = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with psycopg.connect(base["dsn"], autocommit=True) as c:
        c.execute("UPDATE org_erasures SET receipt = jsonb_set(receipt, '{comptes,le}', "
                  "to_jsonb(%s::text)) WHERE id = %s", (il_y_a_2h, d["id"]))
    assert travail.passer()["terminees"] == 1
    fin = demandes.lire(d["id"])
    assert fin["state"] == "done"
    assert fin["org_name"] is None and fin["confirm_email"] is None and fin["snapshot"] is None
    assert fin["confirm_sub"] is None
    assert fin["receipt"]["repetition"]["lignes"] > 0
    assert fin["receipt"]["base"]["total"] > 0
    assert {"fin": d["id"]} in mails
    with psycopg.connect(base["dsn"], row_factory=dict_row) as c:
        assert c.execute("SELECT count(*) AS n FROM projects WHERE owner_type = 'org' "
                         "AND owner_id = %s", (str(base["org"]),)).fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM users WHERE sub = ANY(%s)",
                         ([TITULAIRE, MEMBRE],)).fetchone()["n"] == 0
        assert c.execute("SELECT count(*) AS n FROM users WHERE sub = %s",
                         (AUTRE,)).fetchone()["n"] == 1
    # Le commerce voit l'effacement fini, l'acquitte, et ne le revoit plus.
    assert [e["id"] for e in demandes.pour_le_commerce()] == [d["id"]]
    assert cap._acquitter(_ctx("commerce"), cap.CommerceAckInput(erasure_id=d["id"]))
    assert demandes.pour_le_commerce() == []
    with pytest.raises(AuthzDenied) as e:
        _demander(base["org"], sub=AUTRE, reason="encore")
    assert e.value.code == "already_erased"
