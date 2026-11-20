"""Une destination de transition s'écrit en LISTE — à la pose, au patch, à la lecture,
et l'existant se convertit (oto#63).

`lifecycle.transitions: {"a": "b"}` passait la pose (la boucle de contrôle enrobait la
chaîne d'une liste pour la juger) et était stocké tel quel. Ensuite, chaque lecteur le
relisait à sa façon : le validateur d'écriture le parcourait lettre par lettre, le
dashboard appelait `.map` dessus et levait au rendu. Quatre moitiés :

1. la POSE et le PATCH le refusent, sur toute colonne qui porte un cycle de vie, avec
   la forme exacte attendue ;
2. un bloc STOCKÉ hors forme fait refuser le changement d'état, en nommant la forme et
   le geste qui répare — il n'est plus parcouru lettre par lettre ;
3. `scripts/transitions_en_liste.py` convertit l'existant, à blanc par défaut ;
4. le texte servi dit la forme.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys
import uuid

import pytest

from oto_mcp.datastore import schema as S
from oto_mcp.datastore.errors import RowValidationError

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from scripts import transitions_en_liste as M  # noqa: E402


def _schema(transitions, *, secondaire=None) -> dict:
    champs = [{"key": "ref", "type": "text"},
              {"key": "statut", "type": "text",
               "lifecycle": {"states": ["neuf", "fait", "perdu"],
                             "transitions": transitions}}]
    if secondaire is not None:
        champs.append({"key": "suivi", "type": "text",
                       "lifecycle": {"states": ["x", "y"], "transitions": secondaire}})
    return {"key": "ref", "fields": champs}


# ── 1. la pose ───────────────────────────────────────────────────────────────

def test_une_chaine_a_la_place_d_une_liste_est_REFUSEE_avec_la_forme_exacte():
    """⚠️ Avant : aucune erreur — `[tos]` enrobait la chaîne, `fait` était un état
    connu, le schéma passait et se stockait avec sa chaîne."""
    erreurs = S.validate_schema_def(_schema({"neuf": "fait"}))
    assert len(erreurs) == 1, erreurs
    assert "`statut`" in erreurs[0] and "lifecycle.transitions['neuf']" in erreurs[0]
    assert "LIST" in erreurs[0] and 'str "fait"' in erreurs[0]
    assert '{"neuf": ["fait"]}' in erreurs[0], "le refus donne la forme exacte"


@pytest.mark.parametrize("valeur", [3, None, {"fait": True}, True])
def test_toute_valeur_qui_n_est_pas_une_liste_est_refusee(valeur):
    erreurs = S.validate_schema_def(_schema({"neuf": valeur}))
    assert any("must be a LIST" in e for e in erreurs), erreurs


def test_la_colonne_secondaire_est_jugee_aussi():
    """Un bloc secondaire n'est pas validé dans ses états, mais il est SERVI, et
    l'écran qui le lit appelle `.map` : sa forme se juge comme celle de la file."""
    erreurs = S.validate_schema_def(_schema({"neuf": ["fait"]}, secondaire={"x": "y"}))
    assert len(erreurs) == 1 and erreurs[0].startswith("`suivi`"), erreurs
    assert '{"x": ["y"]}' in erreurs[0]


def test_la_forme_juste_passe_et_une_table_absente_aussi():
    assert S.validate_schema_def(_schema({"neuf": ["fait"], "fait": []})) == []
    assert S.validate_schema_def(_schema(None)) == []


def test_une_table_qui_n_est_pas_un_objet_garde_son_refus():
    erreurs = S.validate_schema_def(_schema("fait"))
    assert any("lifecycle.transitions must be an object" in e and "str" in e
               for e in erreurs), erreurs


def _table(schema=None):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", "sub-o63", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store("sub-o63")
    if schema is not None:
        st.set_schema(ns, schema)
    return st, ns, ns_id


def test_la_pose_et_le_patch_refusent_par_la_surface(live):
    st, ns, ns_id = _table()
    with pytest.raises(ValueError, match=r'\{"neuf": \["fait"\]\}'):
        st.set_schema(ns, _schema({"neuf": "fait"}))
    st.set_schema(ns, _schema({"neuf": ["fait"]}))
    with pytest.raises(ValueError, match=r'\{"fait": \["perdu"\]\}'):
        st.patch_schema(ns, fields=[{"key": "statut",
                                     "lifecycle": {"transitions": {"fait": "perdu"}}}])
    st.patch_schema(ns, fields=[{"key": "statut",
                                 "lifecycle": {"transitions": {"fait": ["perdu"]}}}])
    from oto_mcp import db
    tr = db.get_datastore_by_id(ns_id)["schema"]["fields"][1]["lifecycle"][
        "transitions"]
    assert tr == {"neuf": ["fait"], "fait": ["perdu"]}


# ── 2. la lecture d'un bloc stocké hors forme ────────────────────────────────

def test_un_bloc_stocke_en_chaine_fait_refuser_la_transition_en_le_nommant(live):
    """⚠️ Avant : `neuf → fait` était refusé « (autorisées: ['a', 'f', 'i', 't']) » —
    la chaîne parcourue lettre par lettre. Le refus nomme maintenant la forme fautive
    et le patch qui répare ; le patch réparé, la transition passe."""
    from oto_mcp import db
    st, ns, ns_id = _table(_schema({"neuf": ["fait"]}))
    st.write_rows(ns, [{"ref": "r1", "statut": "neuf"}])
    db.set_datastore_schema(ns_id, _schema({"neuf": "fait"}))   # l'existant d'avant
    with pytest.raises(RowValidationError) as e:
        st.write_rows(ns, [{"ref": "r1", "statut": "fait"}], key="ref")
    message = str(e.value)
    assert "invalid form" in message and '{"neuf": ["fait"]}' in message, message
    assert "data_patch_schema" in message
    assert "'f'" not in message, "plus jamais parcourue lettre par lettre"
    st.patch_schema(ns, fields=[{"key": "statut",
                                 "lifecycle": {"transitions": {"neuf": ["fait"]}}}])
    st.write_rows(ns, [{"ref": "r1", "statut": "fait"}], key="ref")
    assert st.list_rows(ns)[0]["statut"] == "fait"


def test_table_des_transitions_leve_et_rend_des_listes():
    lc = {"states": ["a", "b"], "transitions": {"a": "b"}}
    with pytest.raises(ValueError, match="invalid form"):
        S.table_des_transitions("statut", lc)
    assert S.table_des_transitions("statut", {"transitions": {"a": ["b"]}}) == {"a": ["b"]}
    assert S.table_des_transitions("statut", {"states": ["a"]}) is None


# ── 3. le script ─────────────────────────────────────────────────────────────

def test_la_conversion_pure_et_idempotente():
    avant = _schema({"neuf": "fait", "fait": ["perdu"]}, secondaire={"x": "y"})
    plan = M.convertir(copy.deepcopy(avant))
    assert sorted(plan.converties) == ["statut.neuf: 'fait'", "suivi.x: 'y'"]
    lcs = [f["lifecycle"]["transitions"] for f in plan.schema["fields"] if "lifecycle" in f]
    assert lcs == [{"neuf": ["fait"], "fait": ["perdu"]}, {"x": ["y"]}]
    assert S.validate_schema_def(plan.schema) == []
    assert M.convertir(plan.schema).vide
    assert M.convertir(_schema({"neuf": ["fait"]})).vide and M.convertir(None).vide


@pytest.mark.parametrize("transitions", [{"neuf": None}, {"neuf": {"a": 1}}, "fait"])
def test_une_valeur_sans_conversion_evidente_est_a_arbitrer(transitions):
    with pytest.raises(M.Inmigrable, match="statut"):
        M.convertir(_schema(transitions))


def _passage(appliquer, **filtres):
    lignes: list[str] = []
    bilan = M.executer(appliquer=appliquer, sortie=lignes.append, **filtres)
    return bilan, "\n".join(lignes)


def test_tableau_a_blanc_puis_ecrit_par_la_pose_journalise_et_idempotent(live):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    _, _, ns_id = _table()
    ancien = _schema({"neuf": "fait"})
    db.set_datastore_schema(ns_id, copy.deepcopy(ancien))
    bilan, texte = _passage(False, tableaux=[ns_id])
    assert db.get_datastore_by_id(ns_id)["schema"] == ancien, "à blanc = rien d'écrit"
    assert bilan["tableaux"]["a_convertir"] == 1, texte
    assert "statut.neuf: 'fait' → en liste" in texte
    bilan, texte = _passage(True, tableaux=[ns_id])
    assert bilan["tableaux"]["ecrits"] == 1, texte
    assert db.get_datastore_by_id(ns_id)["schema"]["fields"][1]["lifecycle"][
        "transitions"] == {"neuf": ["fait"]}
    with _connect() as conn:
        journal = conn.execute(
            "SELECT sub, args FROM tool_calls WHERE tool = 'data_set_schema' "
            "AND args->>'datastore' = %s", (str(ns_id),)).fetchall()
    assert len(journal) == 1 and journal[0]["sub"] is None
    assert journal[0]["args"]["migration_systeme"] == M.MIGRATION
    assert "statut.neuf: 'fait'" in str(journal[0]["args"]["converties"])
    assert _passage(False, tableaux=[ns_id])[0]["tableaux"]["a_convertir"] == 0


def test_un_tableau_qui_a_bouge_est_saute(live, monkeypatch):
    from oto_mcp import db
    _, _, ns_id = _table()
    ancien = _schema({"neuf": "fait"})
    db.set_datastore_schema(ns_id, ancien)
    vrai = M.inventaire

    def _perime(tableaux=None):
        lus = vrai(tableaux)
        db.set_datastore_schema(ns_id, {**ancien, "description": "bougé"})
        return lus
    monkeypatch.setattr(M, "inventaire", _perime)
    bilan, texte = _passage(True, tableaux=[ns_id])
    assert bilan["tableaux"]["bouges"] == [ns_id] and "SAUTÉ" in texte
    assert db.get_datastore_by_id(ns_id)["schema"]["description"] == "bougé"


PROPRIO = "u-o63-proc"
SLOTS = [{"name": "vivier", "type": "tableau", "schema": _schema({"neuf": "fait"})},
         {"name": "crm", "type": "connecteur", "connector": "attio"}]


def test_une_procedure_s_ecrit_en_nouvelle_version(live):
    """`set_instruction` ne valide pas (les slots le sont en amont) : c'est par là
    que l'existant d'avant se rejoue."""
    from oto_mcp import org_store
    slug = "o63-" + uuid.uuid4().hex[:4]
    org_store.set_instruction("user", PROPRIO, slug, "1. Qualifier.",
                              slots=copy.deepcopy(SLOTS), set_by="u-auteur")
    p = org_store.get_instruction("user", PROPRIO, slug)
    bilan, texte = _passage(True, procedures=[p["id"]])
    assert bilan["procedures"]["ecrits"] == 1, texte
    apres = org_store.get_instruction("user", PROPRIO, slug)
    assert apres["version"] == 2 and apres["set_by"] == M.AUTEUR
    vivier = next(s for s in apres["slots"] if s["name"] == "vivier")["schema"]
    assert vivier["fields"][1]["lifecycle"]["transitions"] == {"neuf": ["fait"]}
    assert org_store.get_instruction("user", PROPRIO, slug, 1)["slots"] == SLOTS
    assert _passage(False, procedures=[p["id"]])[0]["procedures"]["a_convertir"] == 0


def test_une_entree_de_bibliotheque_est_republiee(live):
    from oto_mcp import org_store
    from oto_mcp.db._conn import _connect
    slug = "o63-pub-" + uuid.uuid4().hex[:4]
    with _connect() as conn:
        conn.execute(
            "INSERT INTO guide_library (slug, title, description, body_md, slots, "
            "author_kind, author_display, category, tags, visibility, version, "
            "published_by) VALUES (%s, 'T', '', '1. Faire.', %s, 'otomata', 'Otomata', "
            "'', ARRAY['x'], 'unlisted', 1, 'u-publieur')",
            (slug, json.dumps(SLOTS)))
    e = org_store.get_library_entry(slug=slug, include_unlisted=True)
    bilan, texte = _passage(True, bibliotheque=[e["id"]])
    assert bilan["bibliotheque"]["ecrits"] == 1, texte
    apres = org_store.get_library_entry(entry_id=e["id"], include_unlisted=True)
    assert apres["version"] == e["version"] + 1
    assert apres["slots"][0]["schema"]["fields"][1]["lifecycle"]["transitions"] == {
        "neuf": ["fait"]}
    assert apres["slots"][1] == SLOTS[1]
    assert _passage(False, bibliotheque=[e["id"]])[0]["bibliotheque"]["a_convertir"] == 0


# ── 4. le texte servi ────────────────────────────────────────────────────────

def test_le_texte_servi_dit_la_forme():
    """Lu là où l'agent le lit : la fiche MCP de `data_set_schema`, l'OpenAPI des deux
    routes de schéma, le guide `datastore-semantics` tel que le semis le sert."""
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp import guide_store, openapi
    from oto_mcp.tools import datastore as D
    m = FastMCP("t")
    D.register(m)
    fiche = asyncio.run(m.get_tool("data_set_schema")).description or ""
    assert '`{"a": "b"}` is refused' in fiche
    routes = openapi.build()["paths"]["/api/datastores/{datastore}/schema"]
    for verbe in ("get", "patch"):
        assert "even for one" in routes[verbe]["description"], verbe
    guide = guide_store.file_guide("datastore-semantics")["body_md"]
    assert "Transitions are always written as a LIST" in guide


# ── 5. `terminal`, même défaut, même chemin ──────────────────────────────────

def _schema_terminal(terminal, *, secondaire=None) -> dict:
    sch = _schema({"neuf": ["fait", "perdu"]})
    sch["fields"][1]["lifecycle"]["terminal"] = terminal
    if secondaire is not None:
        sch["fields"].append({"key": "suivi", "type": "text",
                              "lifecycle": {"states": ["x", "y"], "terminal": secondaire}})
    return sch


def test_un_terminal_en_chaine_est_REFUSE_avec_la_forme_exacte():
    """⚠️ Avant : `"fait"` parcouru lettre par lettre — quatre « états inconnus »
    `'f'`, `'a'`, `'i'`, `'t'`, et aucune phrase sur la forme."""
    erreurs = S.validate_schema_def(_schema_terminal("fait"))
    assert len(erreurs) == 1, erreurs
    assert erreurs[0].startswith("`statut`") and "lifecycle.terminal" in erreurs[0]
    assert "LIST" in erreurs[0] and '"terminal": ["fait"]' in erreurs[0], erreurs[0]
    assert "'f'" not in erreurs[0]


def test_le_terminal_de_la_colonne_secondaire_est_juge_aussi():
    """⚠️ Avant : une colonne secondaire n'était pas regardée du tout — sa chaîne passait
    la pose, et l'écran qui l'appelle en `.map` levait."""
    erreurs = S.validate_schema_def(_schema_terminal(["fait"], secondaire="y"))
    assert len(erreurs) == 1 and erreurs[0].startswith("`suivi`"), erreurs
    assert '"terminal": ["y"]' in erreurs[0]


@pytest.mark.parametrize("valeur", [3, {"fait": True}, True, ""])
def test_tout_terminal_qui_n_est_pas_une_liste_est_refuse(valeur):
    erreurs = S.validate_schema_def(_schema_terminal(valeur))
    assert any("lifecycle.terminal must be a LIST" in e for e in erreurs), erreurs


def test_un_terminal_en_liste_passe_vide_compris():
    assert S.validate_schema_def(_schema_terminal(["fait", "perdu"])) == []
    assert S.validate_schema_def(_schema_terminal([])) == []


def test_un_terminal_stocke_en_chaine_leve_au_lieu_d_etre_derive():
    """⚠️ Avant : `terminal_states` ignorait la chaîne et DÉRIVAIT les terminaux — un
    autre ensemble que celui déclaré, sans un mot."""
    with pytest.raises(ValueError, match=r"invalid form.*data_patch_schema"):
        S.terminal_states(_schema_terminal("fait"))
    assert S.terminal_states(_schema_terminal(["fait"])) == {"fait"}


def test_la_conversion_met_le_terminal_en_liste():
    plan = M.convertir(_schema_terminal("fait", secondaire="y"))
    assert sorted(plan.converties) == ["statut.terminal: 'fait'", "suivi.terminal: 'y'"]
    assert [f["lifecycle"]["terminal"] for f in plan.schema["fields"]
            if "lifecycle" in f] == [["fait"], ["y"]]
    assert S.validate_schema_def(plan.schema) == []
    assert M.convertir(plan.schema).vide
    with pytest.raises(M.Inmigrable, match="terminal"):
        M.convertir(_schema_terminal({"a": 1}))


def test_le_guide_ne_dit_plus_que_deux_blocs_sont_refuses():
    """Le guide affirmait « deux colonnes qui porteraient un bloc sont refusées » : faux
    depuis le 08/09 — plusieurs cycles de vie sont permis, seules deux FILES le sont."""
    from oto_mcp import guide_store
    guide = guide_store.file_guide("datastore-semantics")["body_md"]
    assert "Deux colonnes qui porteraient un bloc" not in guide
    assert "Two columns that would carry a block" not in guide
    assert "is two QUEUES" in guide
    assert '`"terminal": ["gagne"]`' in guide
