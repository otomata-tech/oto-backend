"""Sous un jeton de délégation, `oto_procedure` ne sert que la procédure du travail (#1499,
étape 2).

Un agent hébergé désigne sa procédure par son SLUG seul (`runner_triggers.procedure`), et
son instruction dit « lis la procédure `<slug>` ». Sans palier, la lecture suit la copie
personnelle du porteur puis celle de l'org : l'équipe n'y entre pas. Mais `op=list` sans
palier cumule l'équipe active du porteur, et `op=get scope='group'` la lit — or tout MEMBRE
de l'équipe écrit à ce palier (#681). Un slug qui n'existe qu'au palier équipe donnait donc
à un membre, sans aucun droit sur l'agent, la main sur les consignes qu'il exécute avec les
comptes de son propriétaire.

Sur une vraie base (`live`) : le travail est ENFILÉ, son jeton émis par le vrai chemin du
runner (`runner_jobs._delegue`), le verrou posé depuis la ligne que rend la vraie
vérification du jeton, et chaque geste passe par la capacité servie (autz déclarée puis
handler).
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp.capabilities._types import AuthzDenied, RawCtx

PORTEUR = "sub-porteur-1499"
MEMBRE = "sub-membre-1499"
SLUG_EQUIPE = "relance-equipe"
SLUG_ORG = "relance-org"
SLUG_AUTRE = "autre-org"
SLUG_PERSO = "relance-perso"
CODE = "procedure_outside_job"


@pytest.fixture(scope="module")
def monde(live):
    """Une org, une équipe où sont le porteur de l'agent et un membre qui n'a AUCUN droit
    sur l'agent, et une procédure à chaque palier."""
    from oto_mcp import db, group_store, org_store
    for sub in (PORTEUR, MEMBRE):
        db.upsert_user(sub, email=f"{sub}@acme.test", name=sub)
    org = org_store.create_org(f"Acme 1499 {uuid.uuid4().hex[:4]}", created_by=PORTEUR)
    org_store.add_org_member(org, PORTEUR, "org_admin")
    org_store.add_org_member(org, MEMBRE, "org_member")
    equipe = group_store.create_group(org, "Relances")
    group_store.add_group_member(equipe, PORTEUR, "group_member")
    group_store.add_group_member(equipe, MEMBRE, "group_member")
    for sub in (PORTEUR, MEMBRE):
        org_store.set_active_org(sub, org)
        group_store.set_active_group(sub, equipe)
    _appel(MEMBRE, op="set", scope="group", slug=SLUG_EQUIPE, body_md="Équipe, v1.")
    for slug in (SLUG_ORG, SLUG_AUTRE, SLUG_PERSO):
        _appel(PORTEUR, op="set", scope="org", slug=slug, body_md=f"Org : {slug}.")
    _appel(PORTEUR, op="set", scope="user", slug=SLUG_PERSO, body_md="Perso.")
    return {"org": org, "equipe": equipe}


def _appel(sub: str, **args):
    """UN appel d'`oto_procedure` par le chemin servi : autz DÉCLARÉE puis handler."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "org.procedure.console")
    inp = cap.Input(**args)
    out = cap.handler(cap.authz(RawCtx(sub=sub), inp), inp)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


@pytest.fixture
def travail(monde):
    """Pose, pour la durée du test, le verrou du jeton d'un travail du porteur enfilé avec
    `payload` — émis et vérifié par les vrais chemins."""
    from oto_mcp import db, verrou_org
    from oto_mcp.capabilities import runner_jobs
    poses = []

    def poser(payload: dict) -> int:
        job = db.enqueue_job(monde["org"], "start", payload=payload, sub=PORTEUR)
        jeton = runner_jobs._delegue({"id": job["id"], "sub": PORTEUR,
                                      "org_id": monde["org"]},
                                     bail_s=60, claimant="test")["delegated_token"]
        poses.append(verrou_org.poser(verrou_org.depuis_ligne(db.verify_api_token(jeton))))
        return job["id"]
    yield poser
    for t in reversed(poses):
        verrou_org.lever(t)


def _agent(procedure):
    return {"procedure": procedure, "tools": ["oto_procedure"], "input": "x"}


def _refus(sub: str, **args) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        _appel(sub, **args)
    return e.value


def _slugs(out: dict) -> list[tuple[str, str]]:
    return [(g["slug"], g["scope"]) for g in out["guides"]]


# ── (a) Le chemin : un slug qui n'existe qu'au palier ÉQUIPE ────────────────────

def test_a_un_membre_sans_droit_sur_l_agent_ecrit_la_copie_d_equipe(monde):
    """La moitié du chemin qui reste OUVERTE, et doit le rester : écrire au palier équipe
    demande d'en être membre (#681), rien de plus."""
    out = _appel(MEMBRE, op="set", scope="group", slug=SLUG_EQUIPE, body_md="Équipe, v2.")
    assert out["ok"] and out["scope"] == "group"


def test_a_la_liste_du_travail_ne_montre_pas_la_copie_d_equipe(travail):
    travail(_agent(SLUG_EQUIPE))
    assert (SLUG_EQUIPE, "group") not in _slugs(_appel(PORTEUR, op="list"))


def test_a_la_lecture_au_palier_equipe_est_refusee(travail):
    travail(_agent(SLUG_EQUIPE))
    e = _refus(PORTEUR, op="get", slug=SLUG_EQUIPE, scope="group")
    assert (e.status, e.code) == (403, CODE)


# ── 2a. Sous délégation, la procédure du travail et rien d'autre ────────────────

def test_la_liste_du_travail_ne_rend_que_sa_procedure(travail):
    travail(_agent(SLUG_ORG))
    out = _appel(PORTEUR, op="list")
    assert _slugs(out) == [(SLUG_ORG, "org")]


def test_l_ancien_nom_de_la_liste_est_borne_aussi(travail):
    from oto_mcp import deprecations
    ancien = next(a for a, n in deprecations.CLES.items() if n == "guides")
    travail(_agent(SLUG_ORG))
    out = _appel(PORTEUR, op="list")
    assert [g["slug"] for g in out[ancien]] == [SLUG_ORG]


def test_la_liste_d_une_procedure_introuvable_est_vide(travail):
    travail(_agent(SLUG_EQUIPE))
    assert _appel(PORTEUR, op="list")["guides"] == []


def test_la_procedure_du_travail_se_lit_au_palier_ou_elle_se_resout(travail):
    travail(_agent(SLUG_ORG))
    out = _appel(PORTEUR, op="get", slug=SLUG_ORG)
    assert (out["slug"], out["scope"]) == (SLUG_ORG, "org")
    assert _appel(PORTEUR, op="get", slug=SLUG_ORG, scope="org")["scope"] == "org"


def test_la_copie_personnelle_du_porteur_masque_celle_de_l_org(travail):
    travail(_agent(SLUG_PERSO))
    out = _appel(PORTEUR, op="get", slug=SLUG_PERSO)
    assert out["scope"] == "user" and out["body_md"].startswith("Perso")
    assert _slugs(_appel(PORTEUR, op="list")) == [(SLUG_PERSO, "user")]
    e = _refus(PORTEUR, op="get", slug=SLUG_PERSO, scope="org")
    assert (e.status, e.code) == (403, CODE)


def test_un_slug_du_palier_equipe_seul_reste_introuvable(travail):
    travail(_agent(SLUG_EQUIPE))
    e = _refus(PORTEUR, op="get", slug=SLUG_EQUIPE)
    assert e.status == 404


def test_une_autre_procedure_est_refusee(travail):
    travail(_agent(SLUG_ORG))
    e = _refus(PORTEUR, op="get", slug=SLUG_AUTRE)
    assert (e.status, e.code) == (403, CODE)


def test_la_lecture_par_id_est_refusee(monde, travail):
    """Même celle de SA procédure : l'id ne dit pas le palier, le slug si."""
    from oto_mcp import org_store
    gid = org_store.get_instruction("org", monde["org"], SLUG_ORG)["id"]
    travail(_agent(SLUG_ORG))
    e = _refus(PORTEUR, op="get", guide_id=int(gid))
    assert (e.status, e.code) == (403, CODE)


@pytest.mark.parametrize("args", [
    {"op": "set", "slug": SLUG_ORG, "scope": "org", "body_md": "Réécrite."},
    {"op": "set", "slug": SLUG_ORG, "body_md": "Réécrite chez moi."},
    {"op": "create", "slug": "neuve-1499", "body_md": "Neuve."},
    {"op": "describe", "slug": SLUG_ORG, "scope": "org", "title": "Autre"},
    {"op": "archive", "slug": SLUG_ORG, "scope": "org"},
    {"op": "rename", "slug": SLUG_ORG, "scope": "org", "new_slug": "renommee-1499"},
    {"op": "delete", "slug": SLUG_ORG, "scope": "org"},
    {"op": "library_list"},
    {"op": "fork", "slug": "une-entree-publique"},
], ids=lambda a: a["op"] + ("-" + a["scope"] if "scope" in a else ""))
def test_aucune_ecriture_sous_delegation(monde, travail, args):
    from oto_mcp import org_store
    travail(_agent(SLUG_ORG))
    e = _refus(PORTEUR, **args)
    assert (e.status, e.code) == (403, CODE)
    assert org_store.get_instruction("org", monde["org"], SLUG_ORG)["body_md"] \
        .startswith("Org :")


def test_un_travail_sans_procedure_n_en_lit_aucune(travail):
    travail({"tools": ["oto_procedure"], "input": "x"})
    for args in ({"op": "list"}, {"op": "get", "slug": SLUG_ORG}):
        e = _refus(PORTEUR, **args)
        assert (e.status, e.code) == (403, CODE)


def test_hors_delegation_rien_ne_change(monde):
    out = _appel(PORTEUR, op="list")
    assert {(SLUG_EQUIPE, "group"), (SLUG_ORG, "org"), (SLUG_AUTRE, "org"),
            (SLUG_PERSO, "user")} <= set(_slugs(out))
    assert _appel(PORTEUR, op="get", slug=SLUG_EQUIPE, scope="group")["scope"] == "group"


def test_l_outil_n_est_servi_qu_a_un_travail_qui_a_une_procedure():
    from oto_mcp import outils_du_travail as odt
    assert "oto_procedure" in odt.outils_servis({"tools": ["a"], "procedure": "p"})
    assert "oto_procedure" not in odt.outils_servis({"tools": ["a"]})


# ── 2b. Un agent ne se pose que sur une procédure que son propriétaire lit par nom ─

def _agent_pose(sub: str, monde, **kw):
    from oto_mcp.capabilities import runner_triggers as RT
    from oto_mcp.capabilities._types import ResolvedCtx
    if kw.get("op") == "create":
        kw.setdefault("model", "claude-sonnet-5")
    return asyncio.run(RT._triggers(ResolvedCtx(sub=sub, org_id=monde["org"]),
                                    RT.TriggerInput(**kw)))


def _pose(sub, monde, slug):
    return _agent_pose(sub, monde, op="create", procedure=slug, cron="5 6 * * *",
                       tools=["data_write"])


@pytest.mark.parametrize("slug", [SLUG_EQUIPE, "nulle-part-1499"])
def test_une_procedure_inconnue_de_l_org_et_du_proprietaire_est_refusee(monde, slug):
    from oto_mcp import db
    avant = len(db.list_triggers(monde["org"]) or [])
    with pytest.raises(AuthzDenied) as e:
        _pose(PORTEUR, monde, slug)
    assert (e.value.status, e.value.code) == (400, "unknown_procedure")
    assert len(db.list_triggers(monde["org"]) or []) == avant


@pytest.mark.parametrize("slug", [SLUG_ORG, SLUG_PERSO])
def test_une_procedure_de_l_org_ou_du_proprietaire_passe_la_garde(monde, slug):
    """La garde laisse passer : ce qui refuse ensuite (runner, modèle) n'est pas elle."""
    try:
        _pose(PORTEUR, monde, slug)
    except AuthzDenied as e:
        assert e.code != "unknown_procedure"


def test_la_copie_personnelle_d_un_autre_ne_compte_pas(monde):
    from oto_mcp.capabilities import _procedure_du_travail as PT
    with pytest.raises(AuthzDenied) as e:
        PT.exiger_connue(monde["org"], MEMBRE, "perso-seule-1499")
    assert e.value.code == "unknown_procedure"
    _appel(PORTEUR, op="set", scope="user", slug="perso-seule-1499", body_md="À moi.")
    PT.exiger_connue(monde["org"], PORTEUR, "perso-seule-1499")
    with pytest.raises(AuthzDenied):
        PT.exiger_connue(monde["org"], MEMBRE, "perso-seule-1499")


def test_repointer_un_agent_suit_la_meme_regle(monde):
    from oto_mcp import db
    tid = db.create_trigger(monde["org"], PORTEUR, procedure=SLUG_ORG, tz="Europe/Paris",
                            tools=["data_write"], cron="5 6 * * *")["id"]
    with pytest.raises(AuthzDenied) as e:
        _agent_pose(PORTEUR, monde, op="update", trigger_id=int(tid), procedure=SLUG_EQUIPE)
    assert (e.value.status, e.value.code) == (400, "unknown_procedure")
    assert db.get_trigger(int(tid), monde["org"])["procedure"] == SLUG_ORG
