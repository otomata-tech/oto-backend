"""La déclaration des effets d'outil (`oto_mcp/tools/effets.py`) couvre tout le catalogue
monté, op par op, et ne ment pas sur les cas de l'audit (décision du 03/11/2026, lot 3).

Une protection d'outil vit dans le chemin commun que traverse tout appel, MCP direct ou
recette : elle juge ce que FAIT l'appel — `lire`, `ecrire`, `envoyer`, `detruire` — sur
cette déclaration, jamais sur un verbe deviné. Cette garde la rend mécanique :

- **couverture** : tout outil de connecteur qui n'est pas déclaré `LECTURE` est déclaré,
  avec EXACTEMENT les ops qu'il sert ; rien n'est déclaré qui ne soit servi ;
- **lecture** : un outil `LECTURE` n'est pas dans la table (son annotation le déclare :
  toutes ses ops lisent) ;
- **lots** : toute op qui écrit ou envoie, sur un outil qui prend une liste, déclare son
  lot — ou l'outil déclare que cette liste n'est pas un lot ;
- **audit** : les cas que le verbe classe mal (un `add` qui lance une séquence, un
  `http_act` DELETE, un `refund`…) gardent leur classe.

Un connecteur neuf, ou une op ajoutée, rougit ici : `scripts/amorcer_effets.py` propose ses
lignes, à relire avant de les coller.
"""
from __future__ import annotations

import asyncio

import pytest

from _mcp_app import static_mcp
from oto_mcp.capabilities import registry
from oto_mcp.mcp_errors import McpError
from oto_mcp.recipes import ecritures
from oto_mcp.tools import effets, http_calls
from oto_mcp.tools.effets import (EFFETS, JUGES, LOTS, PAS_UN_LOT, RELAIS, EffetNonDeclare,
                                  Lot, effet)
from oto_mcp.tools.lecture import en_lecture


def _servis():
    """TOUT ce que le serveur monte : outils de connecteur ET de plateforme."""
    outils = asyncio.run(static_mcp().list_tools(run_middleware=False))
    return {t.name: t for t in outils}


def _capacites_non_montees(servis) -> set[str]:
    """Les capacités déclarées au registre mais pas montées (drapeau éteint) : elles se
    déclarent AVANT d'être servies."""
    return {c.mcp for c in registry.caps_with_mcp()} - set(servis)


@pytest.fixture(scope="module")
def servis():
    return _servis()


def _op_schema(tool) -> dict:
    return (tool.parameters.get("properties") or {}).get("op") or {}


def _ops(tool) -> list[str]:
    op = _op_schema(tool)
    if "enum" in op:
        return list(op["enum"])
    if "const" in op:
        return [op["const"]]
    return [v for b in op.get("anyOf", ()) for v in b.get("enum", ())]


def _listes(tool) -> set[str]:
    def est_liste(s: dict) -> bool:
        return s.get("type") == "array" or any(est_liste(b) for b in s.get("anyOf", ()))
    return {k for k, s in (tool.parameters.get("properties") or {}).items()
            if k != "op" and est_liste(s)}


def test_toute_op_d_un_outil_qui_agit_est_declaree(servis):
    manquants = []
    for nom, t in sorted(servis.items()):
        if en_lecture(t) or nom in RELAIS:
            continue
        if nom not in EFFETS:
            manquants.append(nom)
            continue
        ops = _ops(t)
        declare = EFFETS[nom]
        if ops:
            assert isinstance(declare, dict), f"`{nom}` sert des ops : déclare-les une à une"
            assert set(effets.classes_par_op(nom)) == set(ops), (
                nom, sorted(set(ops) ^ set(effets.classes_par_op(nom))))
        elif not _op_schema(t):
            assert isinstance(declare, str), f"`{nom}` n'a pas d'`op` : une classe entière"
    manquants += sorted(_capacites_non_montees(servis) - set(EFFETS))
    assert not manquants, (
        f"{len(manquants)} outil(s) sans effet déclaré (scripts/amorcer_effets.py) : "
        f"{manquants}")


def test_rien_de_declare_qui_ne_soit_servi(servis):
    connus = set(servis) | _capacites_non_montees(servis)
    for nom in EFFETS:
        assert nom in connus, f"`{nom}` n'est plus servi : retire-le d'EFFETS"
    for table in (JUGES, effets.JUGES_EN_BASE, PAS_UN_LOT):
        for nom in table:
            assert nom in EFFETS, nom
    # Un relais n'a pas de classe à lui : il prend celle de sa cible.
    for nom in RELAIS:
        assert nom in connus, f"`{nom}` n'est plus servi : retire-le de RELAIS"
        assert nom not in EFFETS, f"`{nom}` est un relais : pas de ligne dans EFFETS"
    for cle in LOTS:
        nom, _, op = cle.partition(":")
        assert nom in EFFETS and (op or None) in effets.classes_par_op(nom), cle


def test_un_outil_en_lecture_n_est_pas_dans_la_table(servis):
    """Son annotation le déclare en entier : aucune de ses ops ne peut être classée
    autrement que `lire`."""
    for nom, t in servis.items():
        if en_lecture(t):
            assert nom not in EFFETS, f"`{nom}` est LECTURE : retire-le d'EFFETS"


def test_les_classes_sont_les_quatre_et_une_op_n_en_a_qu_une():
    for nom, decl in EFFETS.items():
        if isinstance(decl, str):
            assert decl in effets.CLASSES, nom
            continue
        vues: list[str] = []
        for classe, ops in decl.items():
            assert classe in effets.CLASSES, (nom, classe)
            assert ops.split(), (nom, classe)
            vues += ops.split()
        assert len(vues) == len(set(vues)), (nom, sorted({o for o in vues
                                                          if vues.count(o) > 1}))


def test_toute_op_qui_ecrit_sur_une_liste_declare_son_lot(servis):
    sans_lot = []
    for nom, t in servis.items():
        if nom not in EFFETS:
            continue
        listes = _listes(t)
        pas_un_lot = set(PAS_UN_LOT.get(nom, "").split())
        assert pas_un_lot <= listes, (nom, sorted(pas_un_lot - listes))
        cles = {c for k, lot in LOTS.items() if k.partition(":")[0] == nom
                for c in lot.cles}
        assert cles <= listes, (nom, sorted(cles - listes))
        assert not cles & pas_un_lot, (nom, sorted(cles & pas_un_lot))
        ecrivent = [op for op, classe in effets.classes_par_op(nom).items()
                    if classe in ("ecrire", "envoyer")]
        if not ecrivent:
            continue
        assert listes <= cles | pas_un_lot, (
            f"`{nom}` : {sorted(listes - cles - pas_un_lot)} — lot d'une op, ou PAS_UN_LOT")
        if not listes - pas_un_lot:
            continue
        for op in ecrivent:
            if effets.lot(nom, op) is None:
                sans_lot.append(f"{nom}:{op}" if op else nom)
    assert not sans_lot, sans_lot


def test_un_lot_ne_porte_que_sur_une_op_qui_ecrit_ou_envoie():
    for cle, lot in LOTS.items():
        nom, _, op = cle.partition(":")
        assert effets.classes_par_op(nom)[op or None] in ("ecrire", "envoyer"), cle
        assert 1 <= lot.max <= effets.LOT_MAX, cle


def test_une_ecriture_forcee_des_recettes_n_est_pas_declaree_en_lecture():
    """`recipes/ecritures.ECRITURES` reste en place : ce qu'il classe écriture n'est pas une
    lecture ici."""
    for outil, e in ecritures.ECRITURES.items():
        if outil not in EFFETS:
            continue
        par_op = effets.classes_par_op(outil)
        for op in (e.ecrire or ()):
            assert par_op[op] != "lire", (outil, op)
        if e.envoie:
            assert set(par_op.values()) >= {"envoyer"}, outil


# --- les cas de l'audit --------------------------------------------------------------------

@pytest.mark.parametrize("nom, args, classe, cle", [
    # Ajouter des contacts à une séquence la leur ENVOIE.
    ("apollo_sequence_contacts", {"op": "add"}, "envoyer", "contact_ids"),
    ("apollo_sequence", {"op": "activate"}, "envoyer", None),
    ("amplemarket_act", {"op": "add_sequence_leads"}, "envoyer", "leads"),
    ("routine_fire", {}, "envoyer", None),
    # Un appel brut se juge sur sa méthode.
    ("http_act", {"op": "request", "method": "DELETE"}, "detruire", None),
    ("http_act", {"op": "request", "method": "delete"}, "detruire", None),
    ("http_act", {"op": "request", "method": "PUT"}, "detruire", None),
    ("http_act", {"op": "request", "method": "GET"}, "lire", None),
    # Sans méthode : le doute. Remplacer la liste des actions détruit.
    ("http_act", {"op": "request"}, "detruire", None),
    ("http_act", {"op": "set_definition"}, "detruire", None),
    ("http_act", {"op": "import"}, "lire", None),
    # Créer une liste chez le fournisseur écrit, même si son verbe est `list`.
    ("lemlist_contact", {"op": "list_create"}, "ecrire", "contact_ids"),
    # Décidé : lancer un run chez le fournisseur écrit, il n'envoie pas.
    ("apify_run", {}, "ecrire", None),
    ("apify_run_sync", {}, "ecrire", None),
    ("clay_run_routine", {}, "ecrire", "items"),
    ("monid_run", {}, "ecrire", None),
    # Décidé : écrire des cellules écrit, même par-dessus des valeurs ; vider détruit.
    ("sheets_spreadsheet", {"op": "write"}, "ecrire", None),
    ("sheets_spreadsheet", {"op": "clear"}, "detruire", None),
    ("sharepoint_excel_act", {"op": "update_range"}, "ecrire", None),
    # Décidé : enregistrer un webhook vers une URL envoie (les données partent).
    ("lemlist_webhook", {"op": "add"}, "envoyer", None),
    ("folk_webhook", {"op": "create"}, "envoyer", None),
    ("linear_webhook", {"op": "update"}, "envoyer", None),
    ("oto_trigger", {"op": "connect_source"}, "envoyer", None),
    # Outils de plateforme.
    ("email_send", {}, "envoyer", "cc"),
    ("data_write", {}, "ecrire", "rows"),
    ("data_delete_datastore", {}, "detruire", None),
    ("oto_org", {"op": "invite"}, "envoyer", None),
    ("oto_fleet", {"op": "launch"}, "envoyer", None),
    ("oto_admin_set_plan", {}, "detruire", None),
    # Décidé : désactiver un outil écrit (oto_enable_tool le rétablit) ; réécrire un
    # guide détruit (aucune version précédente n'est gardée).
    ("oto_disable_tool", {}, "ecrire", None),
    ("oto_guide", {"op": "write"}, "detruire", None),
    ("oto_guide_v2", {"op": "write"}, "detruire", None),
])
def test_les_cas_de_l_audit(nom, args, classe, cle):
    obtenu, lot = effet(nom, args)
    assert obtenu == classe, (nom, args, obtenu)
    assert (lot.cle if lot else None) == cle, (nom, args, lot)


#: Décidé : un interrupteur que la même personne rebascule par une simple écriture
#: (`oto_enable_tool`) écrit, il ne détruit pas.
_REVERSIBLES = {("oto_disable_tool", None)}


def test_rembourser_annuler_desactiver_detruisent():
    """Un remboursement, un avoir, une désactivation, une annulation, une révocation
    détruisent — y compris quand le verbe ne le dit pas (`credit_note`)."""
    verbes = {"refund", "void", "deactivate", "cancel", "revoke", "disable"}
    for nom in EFFETS:
        for op, classe in effets.classes_par_op(nom).items():
            if (nom, op) in _REVERSIBLES:
                continue
            mots = set((op or nom).split("_"))
            if mots & verbes:
                assert classe == "detruire", (nom, op)
    for nom, op in [("nextmotion_invoice", "credit_note"), ("apollo_sequence", "deactivate"),
                    ("n8n_deactivate_workflow", None), ("leexi_users", "deactivate"),
                    ("zohosign_act", "cancel"), ("jev_job", "cancel")]:
        assert effets.classes_par_op(nom)[op] == "detruire", (nom, op)


def test_aucune_op_bulk_ni_delete_n_echappe_a_detruire():
    for nom in EFFETS:
        for op, classe in effets.classes_par_op(nom).items():
            if op and (op.startswith("bulk_") or op.split("_")[0] in (
                    "delete", "remove", "merge", "purge", "archive", "revoke", "trash")):
                assert classe == "detruire", (nom, op)


# --- la fonction de lecture ------------------------------------------------------------------

def test_effet_refuse_ce_qui_n_est_pas_declare():
    with pytest.raises(EffetNonDeclare):
        effet("outil_inconnu", {})
    with pytest.raises(EffetNonDeclare):
        effet("apollo_sequence", {"op": "op_inconnue"})


def test_sans_op_un_outil_multiplexe_prend_sa_classe_la_plus_contraignante():
    classes = set(effets.classes_par_op("apollo_sequence").values())
    attendu = max(classes, key=effets.CLASSES.index)
    assert effet("apollo_sequence", {})[0] == attendu


def test_un_lot_n_est_rendu_qu_a_une_op_qui_ecrit_ou_envoie():
    assert effet("apollo_sequence_contacts", {"op": "add"})[1] == Lot("contact_ids")
    for nom in EFFETS:
        for op, classe in effets.classes_par_op(nom).items():
            if classe in ("lire", "detruire") and nom not in JUGES and nom not in RELAIS:
                assert effet(nom, {"op": op} if op else {})[1] is None, (nom, op)


def test_tout_webhook_cree_envoie():
    """Décidé : une op qui crée ou enregistre un webhook vers une URL fait partir des
    données — `envoyer` (ou `detruire` quand elle remplace)."""
    for nom in EFFETS:
        if "hook" not in nom:
            continue
        for op, classe in effets.classes_par_op(nom).items():
            if op in ("create", "add", "upsert"):
                assert classe in ("envoyer", "detruire"), (nom, op)


# --- un relais prend l'effet de sa cible ---------------------------------------------------

def test_oto_call_prend_l_effet_de_sa_cible():
    assert effet("oto_call", {"name": "apollo_sequence_contacts",
                              "arguments": {"op": "add"}}) == ("envoyer", Lot("contact_ids"))
    assert effet("oto_call", {"name": "http_act", "arguments": {
        "op": "request", "method": "GET"}}) == ("lire", None)
    assert effets.cible("oto_call", {"name": "apollo_sequence", "arguments": {
        "op": "activate"}}) == ("apollo_sequence", {"op": "activate"})
    with pytest.raises(EffetNonDeclare):
        effet("oto_call", {"name": "outil_inconnu", "arguments": {}})


@pytest.mark.parametrize("args", [{}, {"name": ""}, {"name": None},
                                  {"arguments": {"op": "add"}}])
def test_oto_call_sans_cible_leve(args):
    """Décidé : un appel qui ne nomme aucune cible n'atteint rien — il n'y a rien à
    juger, il lève (jamais une classe par défaut)."""
    with pytest.raises(EffetNonDeclare):
        effet("oto_call", args)
    with pytest.raises(EffetNonDeclare):
        effets.lit_la_base("oto_call", args)


# --- http_act call : la méthode de l'action enregistrée, lue en base -----------------------

def _action_enregistree(monkeypatch, operations):
    monkeypatch.setattr(http_calls, "_sub", lambda: "sub-1")
    monkeypatch.setattr(http_calls, "_account_for", lambda sub, operation: None)
    monkeypatch.setattr(http_calls, "_resolved",
                        lambda account=None: type("Rc", (), {"account": "acme"})())
    monkeypatch.setattr(http_calls, "_definition", lambda rc: {"operations": operations})


@pytest.mark.parametrize("methode, classe", [("GET", "lire"), ("POST", "envoyer"),
                                             ("PATCH", "envoyer"), ("PUT", "detruire"),
                                             ("DELETE", "detruire")])
def test_http_act_call_se_juge_sur_la_methode_enregistree(monkeypatch, methode, classe):
    _action_enregistree(monkeypatch, [{"name": "faire", "method": methode, "path": "/x"}])
    assert effet("http_act", {"op": "call", "operation": "faire"}) == (classe, None)
    assert effets.lit_la_base("http_act", {"op": "call", "operation": "faire"})
    assert effets.lit_la_base("oto_call", {"name": "http_act",
                                           "arguments": {"op": "call"}})
    assert not effets.lit_la_base("http_act", {"op": "request", "method": "GET"})


def test_http_act_call_sans_action_leve(monkeypatch):
    _action_enregistree(monkeypatch, [{"name": "autre", "method": "GET", "path": "/x"}])
    with pytest.raises(McpError):
        effet("http_act", {"op": "call", "operation": "faire"})
    with pytest.raises(McpError):
        effet("http_act", {"op": "call"})
