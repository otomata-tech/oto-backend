"""`oto_search` dit l'ORG où vit chaque résultat, pas le chemin qui y mène (ADR 0071).

Le besoin : un agent qui audite l'org A restitue ce qu'il trouve. S'il présente un
contenu d'une autre org comme appartenant à A, il ment au client. La première réponse
(PR #1025, v1) déduisait l'org du CHEMIN d'accès — « possédé par le contexte » contre
« reçu par un partage ». C'est faux dans les deux sens, et chaque sens a son cas ici :

- le tableau PERSONNEL du visiteur, né dans l'org B, arrive par le chemin « à moi » :
  il passait pour « d'ici ». Son org n'est pas enregistrée (ADR 0071 §3) — la réponse
  juste est « inconnue », jamais un booléen deviné ;
- le projet d'un COLLÈGUE de la même org, partagé nommément, arrive par un partage :
  il passait pour « d'ailleurs ». Il vit dans A ;
- le projet d'un pôle de A arrive par un partage pour un membre, par la propriété pour
  un admin d'org : la réponse changeait avec le rôle. Elle ne doit pas ;
- `scope=project` sur un projet reçu d'une autre org rendait toujours « d'ici ».

Base réelle, chemin servi (`me.search`, la capacité derrière `oto_search`).

Depuis la décision du 28/09/2026 (ADR 0030 §9), ce qui est à MOI ne se cherche que depuis
ma maison (`perso`). Depuis le 06/11/2026, un PROJET partagé à MOI en personne se cherche
depuis l'org où il est rangé quand j'en suis membre (`ownership.projets_partages_a_moi_ici`) ;
l'org d'origine reste celle de l'objet.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp import db, group_store, org_store, ownership
from oto_mcp.capabilities import search as CAP
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.capabilities.docs import common as _docs_kind  # noqa: F401 — enregistre le kind `doc`

MOI, ADMIN, COLLEGUE, TIERS = ("u-origine-moi", "u-origine-admin", "u-origine-collegue",
                               "u-origine-tiers")
_S = uuid.uuid4().hex[:8]
MOT = f"cle{_S}"


@pytest.fixture(scope="module")
def monde(live):
    for sub in (MOI, ADMIN, COLLEGUE, TIERS):
        db.upsert_user(sub, email=f"{sub}@exemple.test", name=sub)
    # L'org d'inscription de MOI, avant qu'il rejoigne A et B : sa maison, rendue
    # active plus bas (plus d'org perso depuis le 05/11/2026, la maison = l'active).
    perso = org_store.ensure_home_org(MOI)
    a = org_store.create_org("Org auditée", created_by=ADMIN)
    b = org_store.create_org("Autre org", created_by=TIERS)
    org_store.add_org_member(a, ADMIN, "org_admin")
    org_store.add_org_member(a, COLLEGUE)
    org_store.add_org_member(b, TIERS)
    org_store.add_org_member(b, MOI)
    org_store.add_org_member(a, MOI)          # MOI : simple membre de A, et membre de B
    pole = group_store.create_group(a, "Pôle sans moi", created_by=ADMIN)

    def projet(owner_type, owner_id, nom, ctx=None, partage_a=None):
        pid = int(db.create_project(owner_type, str(owner_id), f"{nom} {MOT}",
                                    created_by=ADMIN, context_org_id=ctx))
        db.create_doc(pid, f"page {nom} {MOT}", body_md=f"corps {MOT}", created_by=ADMIN)
        if partage_a:
            ownership.grant("project", str(pid), *partage_a, role="viewer", granted_by=ADMIN)
        return pid

    p = {
        # Projet PERSONNEL d'un tiers, rangé dans B (`context_org_id`), partagé à MOI.
        "perso_b": projet("user", TIERS, "perso-b", ctx=b, partage_a=("user", MOI)),
        # Projet personnel d'un COLLÈGUE de A, partagé à MOI : il vit dans A.
        "collegue_a": projet("user", COLLEGUE, "collegue-a", ctx=a, partage_a=("user", MOI)),
        # Projet d'un pôle de A dont MOI n'est pas membre, partagé à MOI (l'admin le voit
        # par la propriété, MOI par le partage).
        "pole_a": projet("group", pole, "pole-a", partage_a=("user", MOI)),
        # Projet de l'org A.
        "org_a": projet("org", a, "org-a"),
        # Projet de l'org B dont UNE page seulement est partagée à MOI (#1084).
        "org_b": projet("org", b, "org-b"),
    }
    page_seule = db.create_doc(p["org_b"], f"page-seule {MOT}", body_md=MOT, created_by=TIERS)
    ownership.grant("doc", str(page_seule), "user", MOI, role="viewer", granted_by=TIERS)

    t = {
        # Tableau PERSONNEL de MOI, créé en travaillant pour B : rien ne le dit en base.
        "perso": db.create_datastore("user", MOI, f"tab-perso-{MOT}"),
        "org_a": db.create_datastore("org", str(a), f"tab-org-a-{MOT}"),
        "org_b": db.create_datastore("org", str(b), f"tab-org-b-{MOT}"),
    }
    ownership.grant(ownership.TYPE_RESSOURCE_DATASTORE, str(t["org_b"]), "org", str(a),
                    "read", granted_by=TIERS)
    db.datastore_insert_row(t["perso"], "r1", {"nom": f"ligne {MOT}"})
    org_store.set_active_org(MOI, perso)
    return {"a": a, "b": b, "p": p, "t": t, "page_seule": page_seule, "perso": perso}


def _cherche(sub, org, **k) -> dict:
    out = asyncio.run(CAP._search(ResolvedCtx(sub=sub, org_id=org),
                                  CAP.SearchInput(q=MOT, limit=50, **k)))
    return out


def _origines(out, kind) -> dict:
    """{ref ou project_id → (origin_org_id, other_org)} pour un grain."""
    cle = (lambda h: h["project_id"]) if kind in ("page", "brief") else (
        lambda h: h["ref"]["ns_id"] if kind == "ligne" else h["ref"])
    return {cle(h): (h["origin_org_id"], h["other_org"])
            for h in out["hits"] if h["kind"] == kind}


def test_tableau_personnel_ne_d_ici_mais_inconnu(monde):
    """Mon tableau personnel sort dans mon org perso (il est à moi, 28/09/2026) ; son
    org de naissance n'est pas enregistrée : `other_org` est `None`, pas `False`. Dans
    l'org A, il ne sort pas — seuls les tableaux de A et ceux partagés à A."""
    t = monde["t"]
    dans_a = _cherche(MOI, monde["a"], kinds=["tableau", "ligne"])
    assert _origines(dans_a, "tableau") == {
        t["org_a"]: (monde["a"], False),
        t["org_b"]: (monde["b"], True),
    }
    assert _origines(dans_a, "ligne") == {}
    dans_perso = _cherche(MOI, monde["perso"], kinds=["tableau", "ligne"])
    assert _origines(dans_perso, "tableau") == {t["perso"]: (None, None)}
    assert _origines(dans_perso, "ligne") == {t["perso"]: (None, None)}


def test_projet_personnel_d_une_autre_org_par_son_context_org(monde):
    """06/11/2026 : un projet partagé à MOI en personne se cherche depuis l'org où il est
    rangé (B, son `context_org_id`) quand j'en suis membre — plus depuis ma maison, qui
    est une org ordinaire depuis la fin de l'org perso (`projets_partages_a_moi_ici`)."""
    out = _cherche(MOI, monde["b"], kinds=["page", "brief"])
    assert _origines(out, "page")[monde["p"]["perso_b"]] == (monde["b"], False)
    assert _origines(out, "brief")[monde["p"]["perso_b"]] == (monde["b"], False)
    for org in (monde["a"], monde["perso"]):
        ailleurs = _cherche(MOI, org, kinds=["page", "brief"])
        assert monde["p"]["perso_b"] not in _origines(ailleurs, "page"), org


def test_projet_d_un_collegue_de_la_meme_org_partage_reste_d_ici(monde):
    """Partagé à MOI en personne, le projet d'un collègue de A se cherche depuis A, où
    il vit (06/11/2026), et y dit qu'il est d'ici — plus depuis ma maison."""
    pages = _origines(_cherche(MOI, monde["a"], kinds=["page"]), "page")
    assert pages[monde["p"]["org_a"]] == (monde["a"], False)
    assert pages[monde["p"]["collegue_a"]] == (monde["a"], False)
    pages = _origines(_cherche(MOI, monde["perso"], kinds=["page"]), "page")
    assert monde["p"]["collegue_a"] not in pages


def test_admin_et_membre_lisent_la_meme_org(monde):
    """Le projet du pôle : partage pour MOI, propriété pour l'admin — trouvé depuis A
    par les deux (06/11/2026), avec la même org d'origine."""
    pid = monde["p"]["pole_a"]
    moi = _origines(_cherche(MOI, monde["a"], kinds=["page"]), "page")
    admin = _origines(_cherche(ADMIN, monde["a"], kinds=["page"]), "page")
    assert moi[pid] == admin[pid] == (monde["a"], False)


def test_scope_projet_sur_un_projet_recu_d_ailleurs(monde):
    out = _cherche(MOI, monde["a"], scope="project", project=monde["p"]["perso_b"],
                   kinds=["page", "brief"])
    assert out["hits"], "le projet partagé doit rendre ses pages"
    assert {(h["origin_org_id"], h["other_org"]) for h in out["hits"]} == {(monde["b"], True)}


def test_page_partagee_seule_ne_sort_pas_et_son_projet_dit_son_org(monde):
    """#1084 : la page partagée seule reste gardée par son projet — la recherche ne la
    rend pas. Si elle la rendait, son org serait celle de son projet (B) ; le projet,
    lui, ne sort pas davantage."""
    out = _cherche(MOI, monde["a"])
    assert all(not (h["kind"] == "page" and h["ref"] == monde["page_seule"])
               for h in out["hits"])
    assert monde["p"]["org_b"] not in {h.get("project_id") for h in out["hits"]}


def test_le_champ_n_est_jamais_omis(monde):
    out = _cherche(MOI, monde["a"])
    assert out["hits"]
    for h in out["hits"]:
        assert "origin_org_id" in h and "other_org" in h, h
