"""Signal #699 — `oto_project(op=unlink)` répondait `ok: true` sans rien retirer.

Deux défauts d'un même geste, tels que vécus sur le projet 59 :

1. le SILENCE — la suppression touchait zéro ligne, et la réponse ne portait aucune
   trace de ce non-événement (le lien visé figurait encore dans les `links` rendus par
   ce même appel) ;
2. la ref INDÉLOGEABLE — un lien `tableau` stocké sous le NOM de son datastore (ligne
   d'avant la normalisation nom→id de `op=link`) alors que l'unlink canonisait la ref
   demandée en id AVANT de supprimer : on effaçait « 108 », la ligne s'appelle
   « suivi-commercial-index ». Même classe côté `procedure` (slug stocké vs id demandé).

Le banc passe par le HANDLER SERVI — `P._project`, celui que monte l'outil `oto_project`
et que sert `POST /api/me/projects` — et n'assert que sur ce que la surface EXPOSE
(la réponse, ou le refus levé). Jamais `remove_project_link` seule : son `rowcount` était
justement le fait que personne ne lisait.

La doublure de base imite la clause SQL réelle (égalité EXACTE sur `target_ref`,
`identity_ref` NULL = tous les bindings) — sans quoi le banc serait vert par indulgence.
"""
from __future__ import annotations

import types

import pytest

from oto_mcp.capabilities import projects as P
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u1", org_id=2)
ROW = {"id": 59, "owner_type": "org", "owner_id": "2", "name": "Développement commercial",
       "brief_md": "", "created_by": "u1", "archived_at": None,
       "created_at": "2026-07-02", "updated_at": "2026-09-03"}

# Le datastore de l'org 2 : ces NOMS résolvent. C'est cette résolution — légitime au
# link — qui faisait rater la suppression quand la ligne, elle, porte le nom.
NAMESPACES = {"suivi-commercial-index": 108, "vivier-eti-13": 48, "linkedin-feed": 12}
GUIDES = {"prospection": 4, "relance": 94}


@pytest.fixture
def surface(monkeypatch):
    """Le projet 59 et ses liens, avec les DEUX écritures qui coexistent en base :
    l'id canonique (« 12 ») et le nom/slug d'avant la normalisation."""
    etat = {
        "links": [
            {"target_type": "tableau", "target_ref": "12", "label": "Feed"},
            {"target_type": "tableau", "target_ref": "suivi-commercial-index",
             "slot": "index"},
            {"target_type": "procedure", "target_ref": "prospection"},
            {"target_type": "connecteur", "target_ref": "folk", "identity_ref": None},
            {"target_type": "connecteur", "target_ref": "folk", "identity_ref": "acc-2"},
        ],
        "activite": [],
    }

    def _remove(pid, tt, tr, identity_ref=None):
        # Miroir de la clause SQL de `db.remove_project_link` : égalité EXACTE sur la
        # ref stockée ; `identity_ref=None` emporte TOUS les bindings de l'entité.
        garde = [l for l in etat["links"]
                 if not (l["target_type"] == tt and l["target_ref"] == tr
                         and (identity_ref is None
                              or l.get("identity_ref") == identity_ref))]
        n = len(etat["links"]) - len(garde)
        etat["links"] = garde
        return n

    monkeypatch.setattr(P.db, "get_project_by_id",
                        lambda pid: dict(ROW) if pid == 59 else None)
    monkeypatch.setattr(P.db, "list_project_links", lambda pid: [dict(l) for l in etat["links"]])
    monkeypatch.setattr(P.db, "remove_project_link", _remove)
    # oto#119 — la vraie fonction rend ce qu'elle a FAIT ; une doublure qui rendrait
    # None ferait passer un handler cassé pour vert.
    monkeypatch.setattr(P.db, "add_project_link",
                        lambda *a, **k: {"status": "created", "changed": []})
    monkeypatch.setattr(P.db, "log_project_activity",
                        lambda pid, sub, action, detail=None: etat["activite"].append(action))
    monkeypatch.setattr(P.db, "get_datastore",
                        lambda ot, oid, name: ({"id": NAMESPACES[name], "datastore": name}
                                               if (ot, oid) == ("org", "2")
                                               and name in NAMESPACES else None))
    monkeypatch.setattr(P.org_store, "get_instruction",
                        lambda ot, oid, slug: ({"id": GUIDES[slug], "slug": slug}
                                               if slug in GUIDES else None))
    monkeypatch.setattr(P.ownership, "can_access", lambda sub, t, rid, want="read": True)
    monkeypatch.setattr(P.ownership, "visible_in_org", lambda sub, org, t, rid: True)
    # Garde de portée d'`op=link` : tout identifiant passe (ce banc décrit autre chose).
    monkeypatch.setattr(P.db, "refs_dans_la_portee",
                        lambda pid, t, refs: {int(r) for r in refs if str(r).isdigit()})
    _owner_of = P.ownership.owner_of
    monkeypatch.setattr(P.ownership, "owner_of", lambda t, rid: None if t in (
        "datastore_namespace", "doctrine") else _owner_of(t, rid))
    monkeypatch.setattr(P.group_store, "get_group",
                        lambda gid: {"id": gid, "org_id": 2, "name": "pôle"})
    monkeypatch.setattr(P.ownership, "accessor_scope",
                        lambda sub: types.SimpleNamespace(owner_pairs=lambda: [("org", "2")]))
    return etat


def _unlink(**kw):
    return P._project(CTX, P.ProjectInput(op="unlink", project_id=59, **kw))


def _refs(out, target_type):
    return [l["target_ref"] for l in out["links"] if l["target_type"] == target_type]


# --- (b) la ref indélogeable ------------------------------------------------------

def test_unlink_par_le_nom_d_un_tableau_est_refuse(surface):
    """Depuis le 08/11/2026 un tableau se désigne par son numéro, à l'unlink aussi : le
    nom est refusé (`datastore_by_name`) et rien n'est retiré — la ligne stockée sous
    son nom se défait par son numéro (test suivant)."""
    with pytest.raises(AuthzDenied) as e:
        _unlink(target_type="tableau", target_ref="suivi-commercial-index")
    assert (e.value.status, e.value.code) == (400, "datastore_by_name")
    assert len(surface["links"]) == 5 and surface["activite"] == []


def test_unlink_par_id_atteint_la_ligne_stockee_sous_son_nom(surface):
    """L'autre sens — et la SEULE branche qui exerce vraiment la canonisation : la ref
    donnée est l'id, la ligne porte le nom. Sans elle, le test précédent passerait
    encore avec un canonisateur cassé (les deux chaînes sont égales)."""
    out = _unlink(target_type="tableau", target_ref="108")
    assert "suivi-commercial-index" not in _refs(out, "tableau")
    assert out["removed"] == 1


def test_unlink_dune_procedure_stockee_sous_son_slug(surface):
    """Même classe côté `procedure` : la ligne porte le slug, `link` canonise en id."""
    out = _unlink(target_type="procedure", target_ref="prospection")
    assert _refs(out, "procedure") == []
    assert out["removed"] == 1


def test_un_tableau_encore_lie_survit_a_lunlink_de_son_voisin(surface):
    """Le rattrapage ne déborde pas : délier l'un ne déliera pas l'autre."""
    out = _unlink(target_type="tableau", target_ref="108")
    assert _refs(out, "tableau") == ["12"]


# --- (a) le silence ---------------------------------------------------------------

def test_unlink_sans_cible_refuse_au_lieu_de_repondre_ok(surface):
    with pytest.raises(AuthzDenied) as e:
        _unlink(target_type="tableau", target_ref="999")
    assert e.value.code == "link_not_found" and e.value.status == 404
    # La face MCP ne rend que le MESSAGE : il doit donc porter le fait ET la suite.
    assert "RIEN n'a été retiré" in e.value.message
    assert "« 12 »" in e.value.message and "« suivi-commercial-index »" in e.value.message


def test_un_unlink_refuse_ne_touche_ni_les_liens_ni_le_journal(surface):
    with pytest.raises(AuthzDenied):
        _unlink(target_type="tableau", target_ref="999")
    assert len(surface["links"]) == 5
    assert surface["activite"] == []          # pas d'entrée « project.unlink » fantôme


def test_le_refus_dit_quil_ny_a_aucun_lien_de_ce_type(surface):
    surface["links"] = [l for l in surface["links"] if l["target_type"] != "connecteur"]
    with pytest.raises(AuthzDenied) as e:
        _unlink(target_type="connecteur", target_ref="folk")
    assert "aucun lien `connecteur`" in e.value.message


def test_unlink_reussi_compte_les_bindings_retires(surface):
    """`removed` compte des LIGNES, pas des appels : un connecteur lié deux fois (deux
    identités) part en une fois, et le dit."""
    out = _unlink(target_type="connecteur", target_ref="folk")
    assert _refs(out, "connecteur") == []
    assert out["removed"] == 2
    assert surface["activite"] == ["project.unlink"]


def test_unlink_dun_binding_precis_ne_prend_que_le_sien(surface):
    out = _unlink(target_type="connecteur", target_ref="folk", identity_ref="acc-2")
    assert out["removed"] == 1
    assert [l.get("identity_ref") for l in surface["links"]
            if l["target_type"] == "connecteur"] == [None]


# --- signal #883 : le link reconnaît, lui aussi, les deux écritures ---------------

def _link_capture(monkeypatch):
    vu: dict = {}

    def _add(*a, **k):
        vu.update(k)
        return {"status": "updated", "changed": ["target_ref"],
                "rewritten_from": k.get("rewrite_from")}
    monkeypatch.setattr(P.db, "add_project_link", _add)
    return vu


def test_link_par_id_reconnait_le_tableau_stocke_sous_son_nom(surface, monkeypatch):
    """Le cas vécu : la ligne porte « suivi-commercial-index », l'agent relie par l'id
    108 pour changer le rôle. Le link doit désigner la ligne STOCKÉE à réécrire — sans
    quoi l'upsert crée un second lien et l'unlink, qui reconnaît les deux, les retire."""
    vu = _link_capture(monkeypatch)
    out = P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="tableau",
                                         target_ref="108", role="index"))
    assert vu["rewrite_from"] == "suivi-commercial-index"
    assert out["rewritten_from"] == "suivi-commercial-index"
    assert "duplicate_refs" not in out


def test_link_dun_tableau_deja_stocke_par_id_ne_reecrit_rien(surface, monkeypatch):
    """Contrôle négatif : « 12 » est stocké sous son id, aucune autre écriture ne le
    désigne — rien à réécrire, rien à signaler."""
    vu = _link_capture(monkeypatch)
    out = P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="tableau",
                                         target_ref="12", role="flux"))
    assert vu["rewrite_from"] is None
    assert "duplicate_refs" not in out


def test_une_procedure_sans_proprietaire_se_refuse_en_404(surface, monkeypatch):
    """N4 : `ownership._guide_owner` lève sur une ligne sans propriétaire — la pose
    répond « inconnue » (404), pas une 500."""
    def _leve(sub, t, rid, want="read"):
        if t == "doctrine":
            raise ValueError("procédure #94 sans propriétaire")
        return True
    monkeypatch.setattr(P.ownership, "can_access", _leve)
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="procedure",
                                       target_ref="94"))
    assert (e.value.status, e.value.code) == (404, "unknown_procedure")


# --- #1341 : un slug d'équipe ou personnel ne naît plus en lien mort --------------

def _paliers(monkeypatch, par_palier: dict) -> list:
    """`get_instruction` par palier : chaque slug n'existe QUE là où on le pose — une
    doublure qui ignorerait le palier ferait passer la recherche org-seule pour bonne."""
    lus: list = []

    def _get(ot, oid, slug):
        lus.append((ot, str(oid), slug))
        return par_palier.get((ot, str(oid), slug))

    monkeypatch.setattr(P.org_store, "get_instruction", _get)
    return lus


def test_link_par_slug_dune_procedure_dequipe_stocke_son_id(surface, monkeypatch):
    """Projet d'ÉQUIPE : le slug vit au palier de l'équipe, pas de l'org."""
    row = dict(ROW, owner_type="group", owner_id="12")
    monkeypatch.setattr(P.db, "get_project_by_id", lambda pid: dict(row))
    _paliers(monkeypatch, {("group", "12", "relance-equipe"): {"id": 501}})
    vu: dict = {}
    monkeypatch.setattr(P.db, "add_project_link",
                        lambda pid, tt, tr, *a, **kw: vu.update(ref=tr) or
                        {"status": "created", "changed": []})
    P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="procedure",
                                   target_ref="relance-equipe"))
    assert vu["ref"] == "501"


def test_link_par_slug_dune_procedure_personnelle_stocke_son_id(surface, monkeypatch):
    _paliers(monkeypatch, {("user", "u1", "ma-relance"): {"id": 777}})
    vu: dict = {}
    monkeypatch.setattr(P.db, "add_project_link",
                        lambda pid, tt, tr, *a, **kw: vu.update(ref=tr) or
                        {"status": "created", "changed": []})
    P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="procedure",
                                   target_ref="ma-relance"))
    assert vu["ref"] == "777"


def test_le_palier_du_projet_prime_sur_la_copie_personnelle(surface, monkeypatch):
    """Même slug à l'org du projet et chez le lieur : le lien vise celle du projet,
    que tous ses membres lisent."""
    _paliers(monkeypatch, {("org", "2", "relance"): {"id": 94},
                           ("user", "u1", "relance"): {"id": 777}})
    vu: dict = {}
    monkeypatch.setattr(P.db, "add_project_link",
                        lambda pid, tt, tr, *a, **kw: vu.update(ref=tr) or
                        {"status": "created", "changed": []})
    P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="procedure",
                                   target_ref="relance"))
    assert vu["ref"] == "94"


def test_un_slug_introuvable_se_refuse_au_link(surface, monkeypatch):
    """Ne jamais stocker un lien qui naît mort : refus nommé, rien n'est posé."""
    _paliers(monkeypatch, {})
    poses: list = []
    monkeypatch.setattr(P.db, "add_project_link", lambda *a, **k: poses.append(a))
    with pytest.raises(AuthzDenied) as e:
        P._project(CTX, P.ProjectInput(op="link", project_id=59, target_type="procedure",
                                       target_ref="nexiste-nulle-part"))
    assert (e.value.status, e.value.code) == (404, "unknown_procedure")
    assert "guide_id" in e.value.message and poses == []
