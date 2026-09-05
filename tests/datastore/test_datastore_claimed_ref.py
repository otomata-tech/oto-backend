"""Écrire « sur ma réservation » — l'alias `@claimed` (#517).

Ce que ces tests figent, et pourquoi c'est un défaut de plateforme et pas de
discipline : pour écrire sa fiche, un agent devait **repasser au serveur les
trente-deux caractères** que sa réservation venait de lui rendre. Mesuré sur trois
passages d'une campagne réelle, il en altère un, ou en fabrique un dans une
convention étrangère (`670d56b3-…` en uuid v4, `6723d393f9b0…` en 24 hexadécimaux).
Aucune consigne ne corrige ça : recopier une chaîne aléatoire n'est pas une question
de rigueur.

⚠️ **Et le refus qui s'ensuit ne coûte pas seulement l'écriture.** L'agent refusé
réessaie sans identifiant — c'est la conduite qu'on lui a écrite —, et une écriture
sans identifiant CRÉE au lieu de corriger. Le 29/08/2026, deux entreprises
inexistantes sont ainsi nées dans un tableau d'évaluation ; la veille, des fiches
d'essai étaient nées dans le fichier d'une cliente. **La fréquence de ce défaut est
faible ; sa conséquence est la seule qui fabrique de la donnée fausse.**

D'où l'alias : la réservation devient l'adresse. Elle porte déjà tout — la ligne ET
le tableau —, et le serveur la connaît. L'agent n'a plus rien à recopier ni à deviner.

**La réservation reste la preuve d'appartenance** (le jeton de run, ADR 0038) : cet
alias ne crée aucune propriété par identifiant. Sans run, il refuse — et le refus
nomme le paramètre manquant plutôt que de laisser chercher.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import core as D
from oto_mcp.datastore.errors import ClaimedRefUnresolved

CLAIMED = "@claimed"


def _store(monkeypatch, *, run="run-7", ns_id=7, noms=None):
    """Un store dont le run courant et la résolution de noms sont tenus.

    Le run y est OUVERT : depuis #645, le refus « ton travail ne tient rien » demande
    au journal si le travail est clos, pour dire un MOMENT plutôt qu'un état. Tenu ici
    plutôt qu'attrapé par le garde-fou du refus — un test qui passerait par le chemin
    de secours prouverait le secours, pas la règle. Le cas clos vit dans
    `test_claimed_run_clos_645.py`.
    """
    noms = noms or {7: "copie-eval-palier100", 9: "edition-vivier"}
    s = D.DatastorePg("u1")
    monkeypatch.setattr(D, "_current_run", lambda: run)
    monkeypatch.setattr(D.db, "run_closed_at", lambda _run: None, raising=False)
    monkeypatch.setattr(s, "_resolve", lambda ns, write=False:
                        next(i for i, n in noms.items() if n == ns))
    monkeypatch.setattr(s, "_ns_of", lambda i: {"namespace": noms[i]})
    return s


def _baux(monkeypatch, baux):
    """Les baux actifs que la couche db rendrait pour ce run/worker."""
    vus = {}

    def _leases(*, run_id=None, worker=None):
        vus.update(run_id=run_id, worker=worker)
        return baux

    monkeypatch.setattr(D.db, "datastore_active_leases_of", _leases, raising=False)
    return vus


# ── Le cas nominal : une réservation, une adresse ────────────────────────────

def test_claimed_resolves_to_the_row_held_in_this_namespace(monkeypatch):
    s = _store(monkeypatch)
    vus = _baux(monkeypatch, [{"ns_id": 7, "row_id": "01a04aef-26c0-7c16-9c58-42f8"}])
    assert s.resolve_claimed_ref("copie-eval-palier100") == "01a04aef-26c0-7c16-9c58-42f8"
    assert vus["run_id"] == "run-7", "l'appartenance se lit sur le RUN, pas ailleurs"


def test_the_worker_label_narrows_when_it_is_given(monkeypatch):
    """`data_release` porte déjà un `worker` : il sert de garde, comme au relâchement."""
    s = _store(monkeypatch)
    vus = _baux(monkeypatch, [{"ns_id": 7, "row_id": "r1"}])
    assert s.resolve_claimed_ref("copie-eval-palier100", worker="w-3") == "r1"
    assert vus["worker"] == "w-3"


# ── Le refus qui rattrape le MAUVAIS TABLEAU ─────────────────────────────────

def test_held_elsewhere_names_the_table_that_holds_it(monkeypatch):
    """LE cas qui a mis des fiches d'essai dans le fichier d'une cliente.

    L'agent vise le mauvais tableau ; sa réservation, elle, sait lequel est le bon.
    Le refus doit donc NOMMER ce tableau — sans quoi l'agent réessaie sans
    identifiant et crée une ligne dans le tableau où il n'aurait jamais dû écrire.
    """
    s = _store(monkeypatch)
    _baux(monkeypatch, [{"ns_id": 7, "row_id": "r1"}])
    with pytest.raises(ClaimedRefUnresolved) as e:
        s.resolve_claimed_ref("edition-vivier")
    msg = str(e.value)
    assert "copie-eval-palier100" in msg, "le tableau réellement réservé doit être nommé"
    assert "edition-vivier" in msg, "et celui qui a été visé, pour que l'écart se voie"


def test_nothing_held_at_all_says_so_and_points_at_the_claim(monkeypatch):
    s = _store(monkeypatch)
    _baux(monkeypatch, [])
    with pytest.raises(ClaimedRefUnresolved) as e:
        s.resolve_claimed_ref("copie-eval-palier100")
    assert "data_claim_next" in str(e.value)


def test_several_rows_held_here_refuses_and_names_them(monkeypatch):
    """Deviner laquelle serait pire que refuser : une fiche écrite sur la mauvaise
    ligne d'un fichier client ne se voit sur aucun écran."""
    s = _store(monkeypatch)
    _baux(monkeypatch, [{"ns_id": 7, "row_id": "r1"}, {"ns_id": 7, "row_id": "r2"}])
    with pytest.raises(ClaimedRefUnresolved) as e:
        s.resolve_claimed_ref("copie-eval-palier100")
    assert "r1" in str(e.value) and "r2" in str(e.value)


# ── Sans jeton de run, on refuse — et on dit lequel manque ───────────────────

def test_without_a_run_the_refusal_names_the_missing_token(monkeypatch):
    """La preuve d'appartenance reste le run (ADR 0038) : pas de propriété
    consolée par l'identifiant. Mais le refus enseigne le geste manquant — c'est
    exactement le paramètre que les agents omettent (#547)."""
    s = _store(monkeypatch, run=None)
    _baux(monkeypatch, [])
    with pytest.raises(ClaimedRefUnresolved) as e:
        s.resolve_claimed_ref("copie-eval-palier100")
    assert "_run_id" in str(e.value)


# ── Aucune magie sur les noms littéraux ──────────────────────────────────────

def test_a_literal_id_is_never_interpreted(monkeypatch):
    """`@claimed` est le SEUL alias. Un identifiant qui commence par « @ » sans
    être celui-là n'est pas deviné : il part tel quel et échoue comme avant."""
    assert D.est_ref_reservation(CLAIMED) is True
    for littéral in ("@claim", "@claimed-2", "claimed", "01a04aef-26c0-7c16", ""):
        assert D.est_ref_reservation(littéral) is False


# ── La surface : `@claimed` accepté par l'écriture et le relâchement ─────────

def _tool(name: str):
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as T
    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool(name)), T


class _StoreEspion:
    """Rend l'identifiant que la réservation désigne, et note ce qu'on lui passe."""

    def __init__(self, resolu="01a04aef-26c0-7c16-9c58-42f8", boum=None):
        self.resolu, self.boum, self.vu = resolu, boum, {}

    cible = ("copie-eval-palier100", "01a04aef-26c0-7c16-9c58-42f8")

    def resolve_claimed_target(self, *, worker=None):
        self.vu["target"] = worker
        if self.boum:
            raise self.boum
        return self.cible

    def resolve_claimed_ref(self, namespace, *, worker=None):
        self.vu["resolve"] = (namespace, worker)
        if self.boum:
            raise self.boum
        return self.resolu

    def update_row(self, namespace, row_id, row, *, readonly_override=False):
        self.vu["update"] = (namespace, row_id, row)
        return {"_id": row_id, **row}

    def release_claim(self, namespace, row_id, *, worker):
        self.vu["release"] = (namespace, row_id, worker)
        # Le contrat rend `{released, reason, lease}` depuis le 29/08 (#517) : le
        # booléen seul mêlait « rien à rendre » et « la ligne est à un autre ».
        return {"released": True, "reason": None, "lease": None}

    ligne = {"_id": "01a04aef-26c0-7c16-9c58-42f8", "siren": "1"}

    def get_row(self, namespace, row_id, *, layers="flat"):
        self.vu["get"] = (namespace, row_id)
        return self.ligne

    def cursor_rows(self, namespace, **kw):
        self.vu["cursor"] = (namespace, kw)
        return {"rows": [self.ligne], "next_cursor": None}

    def delete_row(self, namespace, row_id, **kw):
        self.vu["delete"] = (namespace, row_id)

    def declared_key(self, namespace):
        return None

    def get_schema(self, namespace):
        return {"columns": {"siren": {"type": "text"}}}

    def off_schema_report(self):
        return {}


def _monte(monkeypatch, nom, store):
    outil, T = _tool(nom)
    monkeypatch.setattr(T, "_acting_store", lambda: store)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    return outil


def _appel(outil, **kw):
    import asyncio
    return asyncio.run(outil.run(kw))


def _rendu(resultat) -> dict:
    """Le dict que l'appelant reçoit — la face MCP l'emballe dans un `ToolResult`."""
    return resultat.structured_content


def test_write_accepts_the_alias_and_writes_on_the_reserved_row(monkeypatch):
    st = _StoreEspion()
    outil = _monte(monkeypatch, "data_write", st)
    _appel(outil, namespace="copie-eval-palier100", id=CLAIMED, row={"statut": "enrichi"})
    assert st.vu["resolve"] == ("copie-eval-palier100", None)
    assert st.vu["update"] == ("copie-eval-palier100",
                              "01a04aef-26c0-7c16-9c58-42f8", {"statut": "enrichi"})


def test_release_accepts_the_alias_with_its_worker_guard(monkeypatch):
    st = _StoreEspion()
    outil = _monte(monkeypatch, "data_release", st)
    _appel(outil, namespace="copie-eval-palier100", id=CLAIMED, worker="w-3")
    assert st.vu["resolve"] == ("copie-eval-palier100", "w-3")
    assert st.vu["release"][1] == "01a04aef-26c0-7c16-9c58-42f8"


def test_an_unresolvable_alias_comes_back_ACTIONABLE_not_as_an_internal_error(monkeypatch):
    """Le refus est la moitié utile du mécanisme : il doit traverser la surface
    avec son texte. Un « erreur interne » à la place a déjà coûté une campagne."""
    from oto_mcp.mcp_errors import McpError
    st = _StoreEspion(boum=ClaimedRefUnresolved(
        "`@claimed` : ton travail ne tient rien dans `edition-vivier` — sa "
        "réservation porte sur `copie-eval-palier100`."))
    outil = _monte(monkeypatch, "data_write", st)
    with pytest.raises(McpError) as e:
        _appel(outil, namespace="edition-vivier", id=CLAIMED, row={"a": 1})
    assert "copie-eval-palier100" in str(e.value)


# ── Le refus « introuvable » enseigne la forme et rappelle la réservation ────

def test_row_not_found_names_the_expected_shape_and_what_the_run_holds(monkeypatch):
    """Quand l'identifiant est fabriqué, le refus arrive au SEUL moment où l'agent
    peut encore corriger. Lui dire « introuvable » et rien d'autre le laisse
    réessayer sans identifiant — et créer une ligne. On lui donne donc les deux
    choses qui manquent : à quoi ressemble un identifiant, et ce qu'il tient déjà."""
    from oto_mcp.mcp_errors import McpError
    class _StoreIntrouvable(_StoreEspion):
        def update_row(self, namespace, row_id, row, *, readonly_override=False):
            raise RowNotFound()

        def claimed_hint(self, namespace):
            return ("ton travail tient `01a04aef-26c0-7c16` dans "
                    "`copie-eval-palier100` — écris avec `id=\"@claimed\"`")

    from oto_mcp.datastore.core import RowNotFound
    st = _StoreIntrouvable()
    outil = _monte(monkeypatch, "data_write", st)
    with pytest.raises(McpError) as e:
        _appel(outil, namespace="copie-eval-palier100",
               id="6723d393f9b0481d9b83b2b2", row={"a": 1})
    msg = str(e.value)
    assert "6723d393f9b0481d9b83b2b2" in msg, "l'identifiant refusé se cite"
    assert "@claimed" in msg, "et la sortie sans recopie se nomme"
    assert "copie-eval-palier100" in msg, "et ce que le travail tient déjà"


def test_the_hint_never_masks_the_refusal_when_it_cannot_be_built(monkeypatch):
    """Une piste est un bonus. Si la calculer échoue, le refus doit sortir quand
    même — sinon on remplacerait « introuvable » par une erreur interne."""
    from oto_mcp.mcp_errors import McpError
    from oto_mcp.datastore.core import RowNotFound

    class _StoreCassé(_StoreEspion):
        def update_row(self, namespace, row_id, row, *, readonly_override=False):
            raise RowNotFound()

        def claimed_hint(self, namespace):
            raise RuntimeError("base indisponible")

    outil = _monte(monkeypatch, "data_write", _StoreCassé())
    with pytest.raises(McpError) as e:
        _appel(outil, namespace="ns", id="zzz", row={"a": 1})
    assert "zzz" in str(e.value) and "introuvable" in str(e.value)


# ── `@claimed` mis dans le champ VOISIN — vécu à la première rencontre ───────
#
# 29/08, cinquième passage du palier de la campagne, coupé à deux lignes : **les agents
# passent `@claimed` dans `namespace`, pas dans `id`.** Deux écritures refusées sur
# cinq, en « namespace `@claimed` inconnu ».
#
# > **On leur retire un champ à recopier ; ils y mettent l'alias qu'on venait de leur
# > apprendre.** L'alias a été enseigné comme « la réservation est l'adresse » — et
# > l'adresse, pour eux, c'est d'abord le tableau.
#
# La réservation porte les DEUX. Refuser sur le champ voisin, c'est refuser une
# demande qu'on sait satisfaire.

def test_claimed_en_NAMESPACE_resout_le_tableau_ET_la_ligne(monkeypatch):
    s = _store(monkeypatch)
    _baux(monkeypatch, [{"ns_id": 7, "row_id": "r1"}])
    assert s.resolve_claimed_target() == ("copie-eval-palier100", "r1")


def test_claimed_en_namespace_sans_reservation_refuse_en_le_nommant(monkeypatch):
    s = _store(monkeypatch)
    _baux(monkeypatch, [])
    with pytest.raises(ClaimedRefUnresolved) as e:
        s.resolve_claimed_target()
    assert "data_claim_next" in str(e.value)


def test_claimed_en_namespace_avec_plusieurs_lignes_les_nomme_AVEC_leur_tableau(monkeypatch):
    """Sans tableau donné, l'ambiguïté porte sur deux dimensions : nommer les lignes
    sans dire où elles sont laisserait l'agent aussi démuni."""
    s = _store(monkeypatch)
    _baux(monkeypatch, [{"ns_id": 7, "row_id": "r1"}, {"ns_id": 9, "row_id": "r2"}])
    with pytest.raises(ClaimedRefUnresolved) as e:
        s.resolve_claimed_target()
    msg = str(e.value)
    assert "r1" in msg and "r2" in msg
    assert "copie-eval-palier100" in msg and "edition-vivier" in msg


def test_la_surface_accepte_claimed_en_namespace(monkeypatch):
    st = _StoreEspion()
    st.cible = ("copie-eval-palier100", "01a04aef-26c0-7c16-9c58-42f8")
    outil = _monte(monkeypatch, "data_write", st)
    _appel(outil, namespace=CLAIMED, row={"statut": "enrichi"})
    assert st.vu["update"] == ("copie-eval-palier100",
                              "01a04aef-26c0-7c16-9c58-42f8", {"statut": "enrichi"})


def test_les_deux_a_claimed_designent_la_meme_ligne(monkeypatch):
    st = _StoreEspion()
    st.cible = ("copie-eval-palier100", "r1")
    outil = _monte(monkeypatch, "data_write", st)
    _appel(outil, namespace=CLAIMED, id=CLAIMED, row={"a": 1})
    assert st.vu["update"][:2] == ("copie-eval-palier100", "r1")


def test_claimed_glisse_dans_row_est_REFUSE_en_nommant_la_faute(monkeypatch):
    """⚠️ Jamais « inconnu » sur un jeton que l'outil reconnaît : c'est ce qui a coûté
    deux écritures. Un refus qui dit « inconnu » envoie chercher une faute de frappe
    là où il n'y en a pas."""
    from oto_mcp.mcp_errors import McpError
    outil = _monte(monkeypatch, "data_write", _StoreEspion())
    with pytest.raises(McpError) as e:
        _appel(outil, namespace="ns", row={"siren": CLAIMED})
    msg = str(e.value)
    assert "@claimed" in msg and "`id`" in msg
    assert "inconnu" not in msg.lower()


# ── L'alias vaut aussi pour LIRE — le trou que l'inventaire a rendu certain ──
#
# Un agent à qui on enseigne « la réservation est l'adresse » essaiera de **lire** sa
# ligne avec. `data_rows` et `data_delete_row` l'ignoraient : « namespace `@claimed`
# inconnu ». **Un jeton que la plateforme reconnaît sur un verbe et déclare inconnu
# sur le verbe voisin est une incohérence, pas une garde.**

def test_la_LECTURE_accepte_l_alias_en_tableau_et_en_ligne(monkeypatch):
    st = _StoreEspion()
    st.cible = ("copie-eval-palier100", "r1")
    outil = _monte(monkeypatch, "data_rows", st)
    out = _appel(outil, namespace=CLAIMED)
    assert st.vu["get"] == ("copie-eval-palier100", "r1"), \
        "namespace=@claimed doit résoudre le tableau ET la ligne"


def test_la_lecture_accepte_l_alias_dans_id_seul(monkeypatch):
    st = _StoreEspion()
    outil = _monte(monkeypatch, "data_rows", st)
    _appel(outil, namespace="copie-eval-palier100", id=CLAIMED)
    assert st.vu["get"] == ("copie-eval-palier100", "01a04aef-26c0-7c16-9c58-42f8")


def test_la_suppression_accepte_l_alias(monkeypatch):
    st = _StoreEspion()
    st.cible = ("copie-eval-palier100", "r9")
    outil = _monte(monkeypatch, "data_delete_row", st)
    _, T = _tool("data_delete_row")
    monkeypatch.setattr(T.access, "current_user_sub_or_raise", lambda: "u-1")
    monkeypatch.setattr(T, "_store_for", lambda sub: st)
    _appel(outil, namespace=CLAIMED, id=CLAIMED)
    assert st.vu["delete"] == ("copie-eval-palier100", "r9")


# ── `*` demande TOUTES les colonnes, comme sur les autres surfaces ───────────

def test_etoile_dans_fields_rend_la_ligne_ENTIERE(monkeypatch):
    """`fields=["*"]` est le chemin vers le brut sur `oto_doc` et sur le feed. Sur
    `data_rows` il tombait dans « colonne inconnue » : l'agent croyait demander tout
    et recevait une projection sur une colonne qui n'existe pas — donc `_id` seul."""
    st = _StoreEspion()
    st.ligne = {"_id": "r1", "siren": "1", "raison_sociale": "ACME"}
    outil = _monte(monkeypatch, "data_rows", st)
    out = _rendu(_appel(outil, namespace="ns", id="r1", fields=["*"]))
    assert out == st.ligne, "aucune projection : la ligne entière"


def test_etoile_ne_declenche_PAS_l_avertissement_de_colonne_inconnue(monkeypatch):
    st = _StoreEspion()
    outil = _monte(monkeypatch, "data_rows", st)
    out = _rendu(_appel(outil, namespace="ns", fields=["*"]))
    assert "inconnue" not in str(out.get("warning", "")).lower()
    assert out["rows"] == [st.ligne], "et la ligne rendue reste ENTIÈRE"
