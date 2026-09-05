"""Un jeton réservé s'écrit là où il a un sens, et le refus dit LEQUEL (#517, 29/08).

Inventaire du 29/08 sur trois passages d'une campagne réelle. Sa conclusion a renversé
sa prémisse : on cherchait des jetons mal NOMMÉS, les cas coûteux sont les jetons mal
PLACÉS — et parmi eux, ceux que rien ne refuse.

- `@claimed` posé dans le contenu d'une ligne : accepté, écrit en clair dans un fichier
  client. Aucun refus, aucune trace, une donnée fausse livrée.
- `_run_id` posé comme colonne : même famille. Le jeton du run gravé dans la fiche.
- `slot:<nom>` sur une opération de LIGNE côté capacité : passé brut au stockage, qui
  répond « namespace inconnu » — alors que les opérations de SCHÉMA, à côté, le
  résolvent depuis toujours.

Et la garde qu'il ne faut pas construire : une chaîne qui commence par `slot:` DANS UNE
VALEUR est une donnée légitime. Le dernier test de ce fichier existe pour ça.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import jetons


# ── La reconnaissance est exacte, jamais indulgente ──────────────────────────

@pytest.mark.parametrize("valeur,attendu", [
    ("@claimed", "@claimed"),
    ("slot:vivier", "slot:"),
    ("*", "*"),
    ("@claim", None),
    ("@claimed-2", None),
    ("@ma_ligne", None),
    ("slots:vivier", None),
    ("copie-eval-palier100", None),
    (None, None),
    (42, None),
])
def test_la_reconnaissance_d_un_jeton_est_exacte(valeur, attendu):
    assert jetons.jeton_de(valeur) == attendu


# ── Trois issues, et jamais une quatrième ────────────────────────────────────

def test_accepte_le_jeton_que_le_champ_accepte():
    for champ, valeur in [("namespace", "@claimed"), ("id", "@claimed"),
                          ("namespace", "slot:vivier"), ("fields", "*")]:
        jetons.verifier_adresse(champ, valeur)  # ne lève pas


def test_refuse_un_jeton_RECONNU_mais_mal_place_en_NOMMANT_le_champ():
    with pytest.raises(jetons.JetonMalPlace) as e:
        jetons.verifier_adresse("id", "slot:vivier")
    msg = str(e.value)
    assert "`namespace`" in msg, "le refus doit nommer OÙ le jeton s'écrit"
    assert "slot:" in msg


def test_refuse_l_etoile_hors_de_fields_en_nommant_fields():
    with pytest.raises(jetons.JetonMalPlace) as e:
        jetons.verifier_adresse("namespace", "*")
    assert "`fields`" in str(e.value)


def test_un_jeton_INCONNU_passe_sans_rien_dire():
    """La troisième issue — celle qui empêche la couture de devenir une grammaire."""
    for valeur in ("copie-eval-palier100", "@claim", "slots:x", "", "01a04aef-26c0"):
        jetons.verifier_adresse("id", valeur)
        jetons.verifier_adresse("namespace", valeur)


# ── Le contenu : seulement ce qui n'a AUCUN sens comme donnée ────────────────

def test_l_alias_dans_une_valeur_de_ligne_est_refuse():
    with pytest.raises(jetons.JetonMalPlace) as e:
        jetons.verifier_contenu({"siren": "1", "statut": "@claimed"})
    assert "ADRESSE" in str(e.value)


def test_l_alias_est_trouve_meme_IMBRIQUE():
    with pytest.raises(jetons.JetonMalPlace):
        jetons.verifier_contenu({"contacts": [{"nom": {"valeur": "@claimed"}}]})


def test_un_parametre_d_appel_pose_en_COLONNE_est_refuse():
    """Le cas qui grave un contexte d'exécution dans une fiche cliente."""
    for cle in ("_run_id", "_org", "_project", "_group", "_instance"):
        with pytest.raises(jetons.JetonMalPlace) as e:
            jetons.verifier_contenu({"siren": "1", cle: "abc"})
        assert f"`{cle}`" in str(e.value)
        assert "PARAMÈTRE" in str(e.value)


def test_UNE_CHAINE_slot_DANS_UNE_VALEUR_reste_une_donnee_legitime():
    """⚠️ Le test qui délimite la couture, et sans lequel elle casserait des écritures
    justes : `slot:` et `*` sont des chaînes qu'une ligne peut porter. Les refuser dans
    le contenu, ce serait se protéger d'une faute qu'on ne sait pas distinguer d'un
    texte ordinaire."""
    jetons.verifier_contenu({"note": "slot: machine à café, 2e étage"})
    jetons.verifier_contenu({"requete": "*", "colonnes": ["*", "siren"]})
    jetons.verifier_contenu({"libelle": "slot:vivier"})


def test_une_ligne_ordinaire_passe():
    jetons.verifier_contenu({"siren": "383701067", "raison_sociale": "ACME",
                             "contacts": [{"nom": "Dupont"}], "effectif": None})


# ── Sur la surface MCP : le refus arrive AVANT le stockage ───────────────────

def _tool(name: str):
    import asyncio

    from fastmcp import FastMCP

    from oto_mcp.tools import datastore as T
    m = FastMCP("t")
    T.register(m)
    return asyncio.run(m.get_tool(name)), T


class _StoreMuet:
    """Un store qui note ce qu'on lui demande — il ne doit RIEN recevoir."""

    def __init__(self):
        self.vu = []

    def __getattr__(self, nom):
        def _note(*a, **kw):
            self.vu.append((nom, a, kw))
            return {}
        return _note


def _monte(monkeypatch, nom):
    outil, T = _tool(nom)
    st = _StoreMuet()
    monkeypatch.setattr(T, "_acting_store", lambda: st)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    return outil, st


def _refus(outil, **kw):
    import asyncio

    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as e:
        asyncio.run(outil.run(kw))
    return str(e.value)


def test_MCP_un_slot_pose_dans_id_est_refuse_en_nommant_namespace(monkeypatch):
    """Avant : `slot:vivier` partait comme identifiant de ligne et revenait
    « row introuvable » — un refus qui désigne une cause fausse."""
    outil, st = _monte(monkeypatch, "data_write")
    msg = _refus(outil, namespace="vivier", id="slot:vivier", row={"a": 1})
    assert "`namespace`" in msg and "slot:" in msg
    assert st.vu == [], "rien ne doit atteindre le stockage"


def test_MCP_un_parametre_d_appel_pose_en_colonne_est_refuse(monkeypatch):
    """Avant : `_run_id` s'écrivait comme une colonne, sans un mot."""
    outil, st = _monte(monkeypatch, "data_write")
    msg = _refus(outil, namespace="vivier", row={"siren": "1", "_run_id": "abc"})
    assert "`_run_id`" in msg and "PARAMÈTRE" in msg
    assert st.vu == []


def test_MCP_une_valeur_qui_RESSEMBLE_a_un_slot_passe(monkeypatch):
    """La couture ne doit pas casser une écriture juste pour se protéger d'un texte."""
    outil, st = _monte(monkeypatch, "data_write")
    import asyncio
    asyncio.run(outil.run({"namespace": "vivier",
                           "row": {"note": "slot: machine à café"}}))
    assert st.vu, "l'écriture doit atteindre le stockage"


# ── Sur la face REST : LA MÊME couture, pas une seconde idée ─────────────────

def _rest():
    """Importé tard : `_datastore_rest` monte l'adaptateur, et ce fichier tourne aussi
    sans lui pour les tests de la couture seule."""
    import _datastore_rest as H

    from oto_mcp.capabilities.datastore import rows as dsr
    return H, dsr


class _StoreREST:
    def __init__(self):
        self.vu = []

    def append_row(self, ns, data, *, trace=None, readonly_override=False, origine_override=False):
        self.vu.append(("append_row", ns, data))
        return {"_id": "r9", **data}

    def update_row(self, ns, row_id, patch, *, trace=None, readonly_override=False, origine_override=False):
        self.vu.append(("update_row", ns, row_id, patch))
        return {"_id": row_id, **patch}

    # #658 : la surface REST relit ce relevé pour sa ligne de journal.
    off_forced: list = []

    def off_schema_report(self):
        return {}


@pytest.fixture
def rest(monkeypatch):
    H, dsr = _rest()
    st = _StoreREST()
    H.stub_authz(monkeypatch)
    monkeypatch.setattr(dsr, "make_store", lambda sub: st)
    monkeypatch.setattr(dsr.datastore_journal, "record", lambda *a, **k: None)
    monkeypatch.setattr(dsr.access, "resolve_namespace_ref",
                        lambda ns: "vivier-2026" if ns.startswith("slot:") else ns)
    return H, st


def test_REST_un_slot_est_RÉSOLU_comme_sur_les_operations_de_schema(rest):
    """⚠️ La divergence silencieuse de l'inventaire : les opérations de SCHÉMA de cette
    même couche résolvaient `slot:` depuis toujours, celles de LIGNES le passaient brut
    au stockage — qui répondait « namespace inconnu » sur un jeton parfaitement valide."""
    H, st = rest
    H.call("me.datastore.append_row", path_params={"namespace": "slot:vivier"},
           body={"a": 1})
    assert st.vu and st.vu[0][1] == "vivier-2026"


def test_REST_l_alias_dans_le_CORPS_est_refuse_et_n_atteint_pas_le_stockage(rest):
    H, st = rest
    status, corps = H.call("me.datastore.update_row",
                           path_params={"namespace": "vivier", "row_id": "r1"},
                           body={"statut": "@claimed"})
    assert status == 400, corps
    assert corps["error"] == "jeton_mal_place"
    assert st.vu == []


def test_REST_un_parametre_d_appel_en_colonne_est_refuse(rest):
    H, st = rest
    status, corps = H.call("me.datastore.append_row",
                           path_params={"namespace": "vivier"},
                           body={"siren": "1", "_run_id": "abc"})
    assert (status, corps["error"]) == (400, "jeton_mal_place")
    assert st.vu == []


def test_REST_une_valeur_qui_ressemble_a_un_slot_passe(rest):
    H, st = rest
    H.call("me.datastore.append_row", path_params={"namespace": "vivier"},
           body={"note": "slot: machine à café"})
    assert st.vu, "une donnée légitime ne doit pas être refusée"
