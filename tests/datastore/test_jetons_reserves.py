"""Un jeton réservé s'écrit là où il a un sens, et le refus dit LEQUEL (#517, 29/08).

Inventaire du 29/08 sur trois passages d'une campagne réelle. Sa conclusion a renversé
sa prémisse : on cherchait des jetons mal NOMMÉS, les cas coûteux sont les jetons mal
PLACÉS — et parmi eux, ceux que rien ne refuse.

- `_run_id` posé comme colonne : accepté, le jeton du run gravé dans la fiche d'un
  client. Aucun refus, aucune trace, une donnée fausse livrée.
- `slot:<nom>` sur une opération de LIGNE côté capacité : passé brut au stockage, qui
  répond « datastore inconnu » — alors que les opérations de SCHÉMA, à côté, le
  résolvent depuis toujours.

Et la garde qu'il ne faut pas construire : une chaîne qui commence par `slot:` DANS UNE
VALEUR est une donnée légitime. Un test de ce fichier existe pour ça.

## Le pronom retiré (07/09/2026)

`@claimed` — « la ligne que je tiens » — a été le troisième jeton de cette couture, du
29/08 au 07/09/2026. Il a été RETIRÉ : il se résolvait par le run courant, et un agent
est sans état — pas de « moi » stable auquel accrocher un pronom, plusieurs runs
coexistent, donc une adresse ambiguë par construction (1 300 refus mesurés en 90 jours).

Ce que les tests de la fin de ce fichier figent n'est pas sa disparition — c'est ce que
reçoit **celui qui l'écrit encore**. Un jeton retiré ne disparaît pas du trafic le jour
où on le retire ; s'il repartait tel quel, le stockage répondrait « tableau inconnu » ou
« ligne introuvable » sur une chaîne parfaitement orthographiée, et l'agent irait
chercher une faute de frappe. *Un refus qui nomme le geste qui aboutit est recopié à la
lettre dans la minute ; un refus muet fait perdre la ligne deux fois sur trois.*
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import jetons


# ── La reconnaissance est exacte, jamais indulgente ──────────────────────────

@pytest.mark.parametrize("valeur,attendu", [
    ("slot:vivier", "slot:"),
    ("*", "*"),
    # Le pronom retiré n'est plus un jeton : il n'a plus de champ où s'écrire, et c'est
    # `JETONS_RETIRES` — pas `jeton_de` — qui le reconnaît pour le refuser.
    ("@claimed", None),
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
    for champ, valeur in [("datastore", "slot:vivier"), ("fields", "*")]:
        jetons.verifier_adresse(champ, valeur)  # ne lève pas


def test_refuse_un_jeton_RECONNU_mais_mal_place_en_NOMMANT_le_champ():
    with pytest.raises(jetons.JetonMalPlace) as e:
        jetons.verifier_adresse("id", "slot:vivier")
    msg = str(e.value)
    assert "`datastore`" in msg, "le refus doit nommer OÙ le jeton s'écrit"
    assert "slot:" in msg


def test_refuse_l_etoile_hors_de_fields_en_nommant_fields():
    with pytest.raises(jetons.JetonMalPlace) as e:
        jetons.verifier_adresse("datastore", "*")
    assert "`fields`" in str(e.value)


def test_un_jeton_INCONNU_passe_sans_rien_dire():
    """La troisième issue — celle qui empêche la couture de devenir une grammaire."""
    for valeur in ("copie-eval-palier100", "@claim", "slots:x", "", "01a04aef-26c0"):
        jetons.verifier_adresse("id", valeur)
        jetons.verifier_adresse("datastore", valeur)


# ── Le contenu : seulement ce qui n'a AUCUN sens comme donnée ────────────────

def test_un_parametre_d_appel_pose_en_COLONNE_est_refuse():
    """Le cas qui grave un contexte d'exécution dans une fiche cliente."""
    for cle in ("_run_id", "_org", "_project", "_group", "_instance"):
        with pytest.raises(jetons.JetonMalPlace) as e:
            jetons.verifier_contenu({"siren": "1", cle: "abc"})
        assert f"`{cle}`" in str(e.value)
        assert "PARAMETER" in str(e.value)


def test_UNE_CHAINE_slot_DANS_UNE_VALEUR_reste_une_donnee_legitime():
    """⚠️ Le test qui délimite la couture, et sans lequel elle casserait des écritures
    justes : `slot:` et `*` sont des chaînes qu'une ligne peut porter. Les refuser dans
    le contenu, ce serait se protéger d'une faute qu'on ne sait pas distinguer d'un
    texte ordinaire."""
    jetons.verifier_contenu({"note": "slot: machine à café, 2e étage"})
    jetons.verifier_contenu({"requete": "*", "colonnes": ["*", "siren"]})
    jetons.verifier_contenu({"libelle": "slot:vivier"})


def test_le_pronom_RETIRE_dans_une_valeur_de_ligne_n_est_plus_qu_une_chaine():
    """⚠️ Un choix daté, pas un oubli (07/09/2026).

    Tant que `@claimed` était une ADRESSE, l'écrire dans le contenu d'une ligne était
    refusé : il finissait en clair dans un fichier client. Retiré des adresses, il n'est
    plus qu'une chaîne — et la règle du contenu ne refuse QUE ce qui n'a aucun sens comme
    donnée (`PARAMETRES_D_APPEL`). Le garder ici rouvrirait la garde qu'on refuse de
    construire : une valeur textuelle jugée sur sa ressemblance avec une syntaxe morte."""
    jetons.verifier_contenu({"siren": "1", "statut": "@claimed"})
    jetons.verifier_contenu({"contacts": [{"nom": {"valeur": "@claimed"}}]})


def test_une_ligne_ordinaire_passe():
    jetons.verifier_contenu({"siren": "383701067", "raison_sociale": "ACME",
                             "contacts": [{"nom": "Dupont"}], "effectif": None})


# ── Le jeton RETIRÉ : ce que reçoit celui qui l'écrit encore ─────────────────

@pytest.mark.parametrize("champ", ["datastore", "id", "fields", "filter"])
def test_le_pronom_retire_est_refuse_dans_TOUS_les_champs_d_adresse(champ):
    with pytest.raises(jetons.JetonRetire):
        jetons.verifier_adresse(champ, "@claimed")


def test_le_refus_du_pronom_NOMME_le_geste_qui_aboutit():
    """⚠️ L'exigence qui prime sur la propreté du retrait.

    Le refus doit rendre la conduite : la ligne s'adresse par son `_id` ou par sa clé
    métier, tous deux présents dans ce que `data_claim_next` a rendu. Et il ne doit
    surtout PAS ressembler à « inconnu » / « introuvable » — le refus qui envoie chercher
    une faute de frappe dans une chaîne juste."""
    with pytest.raises(jetons.JetonRetire) as e:
        jetons.verifier_adresse("id", "@claimed")
    msg = str(e.value)
    assert "REMOVED" in msg, "le refus doit dire que le raccourci n'existe plus"
    assert "`_id`" in msg and "business key" in msg, \
        "il doit nommer les DEUX façons d'adresser la ligne"
    assert "data_claim_next" in msg, "et l'appel qui les lui a déjà rendues"
    assert "`id`" in msg, "et le champ où l'identifiant se pose"
    for muet in ("inconnu", "introuvable"):
        assert muet not in msg.lower(), \
            f"« {muet} » envoie chercher une faute de frappe là où il n'y en a pas"


def test_le_refus_nomme_le_champ_OU_le_pronom_a_ete_pose():
    """Les agents l'ont écrit dans les deux : le refus doit se reconnaître dans l'appel
    qu'ils viennent de faire, pas décrire un cas général."""
    with pytest.raises(jetons.JetonRetire) as e:
        jetons.verifier_adresse("datastore", "@claimed")
    assert "in `datastore`" in str(e.value)


def test_un_jeton_retire_est_une_famille_de_refus_deja_traduite_par_les_deux_faces():
    """`JetonRetire` hérite de `JetonMalPlace` par CONTRAT : les deux faces traduisent
    déjà cette famille (`INVALID_PARAMS` côté agent, `400 jeton_mal_place` côté REST).
    Casser l'héritage ferait ressortir le refus en erreur interne — au moment précis où
    il porte la seule chose utile."""
    assert issubclass(jetons.JetonRetire, jetons.JetonMalPlace)
    assert issubclass(jetons.JetonMalPlace, ValueError)


def test_la_resolution_ne_touche_plus_au_store_pour_un_identifiant():
    """`resoudre` ne lit plus le bail : elle vérifie l'adresse et résout le TABLEAU.

    C'est le cœur du retrait — un pronom se résolvait par le run, un identifiant ne se
    résout pas du tout."""
    assert jetons.resoudre("vivier", "r1", resoudre_slot=lambda ns: ns) == ("vivier", "r1")
    assert jetons.resoudre("slot:v", None, resoudre_slot=lambda ns: "vivier-2026") \
        == ("vivier-2026", None)


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
    # Relevé de résolution du store (`DatastorePg.dernier_tableau`) : les
    # remises y prennent l'IDENTITÉ du tableau — nom canonique + `ns_id`.
    dernier_tableau = {"ns_id": 174, "datastore": "vivier"}

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


def test_MCP_un_slot_pose_dans_id_est_refuse_en_nommant_datastore(monkeypatch):
    """Avant : `slot:vivier` partait comme identifiant de ligne et revenait
    « row introuvable » — un refus qui désigne une cause fausse."""
    outil, st = _monte(monkeypatch, "data_write")
    msg = _refus(outil, datastore="vivier", id="slot:vivier", row={"a": 1})
    assert "`datastore`" in msg and "slot:" in msg
    assert st.vu == [], "rien ne doit atteindre le stockage"


@pytest.mark.parametrize("adresse", [
    {"datastore": "@claimed", "id": "@claimed"},
    {"datastore": "@claimed"},
    {"datastore": "copie-eval-palier100", "id": "@claimed"},
])
def test_MCP_le_pronom_retire_est_refuse_en_NOMMANT_la_conduite(monkeypatch, adresse):
    """Les trois formes que les agents ont réellement écrites, sur le verbe qui les a
    portées. Aucune n'atteint le stockage, et aucune ne revient « inconnu »."""
    outil, st = _monte(monkeypatch, "data_write")
    msg = _refus(outil, row={"statut": "enrichi"}, **adresse)
    assert "REMOVED" in msg and "data_claim_next" in msg and "`_id`" in msg
    assert "inconnu" not in msg.lower() and "introuvable" not in msg.lower()
    assert st.vu == [], "rien ne doit atteindre le stockage"


def test_MCP_le_pronom_retire_est_refuse_a_la_LECTURE_aussi(monkeypatch):
    """L'agent qui l'a appris comme « la réservation est l'adresse » l'employait partout
    où il donnait une adresse, y compris pour lire. Le refus doit être le MÊME."""
    outil, st = _monte(monkeypatch, "data_rows")
    msg = _refus(outil, datastore="@claimed")
    assert "REMOVED" in msg and "data_claim_next" in msg
    assert st.vu == []


def test_MCP_un_parametre_d_appel_pose_en_colonne_est_refuse(monkeypatch):
    """Avant : `_run_id` s'écrivait comme une colonne, sans un mot."""
    outil, st = _monte(monkeypatch, "data_write")
    msg = _refus(outil, datastore="vivier", row={"siren": "1", "_run_id": "abc"})
    assert "`_run_id`" in msg and "PARAMETER" in msg
    assert st.vu == []


def test_MCP_une_valeur_qui_RESSEMBLE_a_un_slot_passe(monkeypatch):
    """La couture ne doit pas casser une écriture juste pour se protéger d'un texte."""
    outil, st = _monte(monkeypatch, "data_write")
    import asyncio
    asyncio.run(outil.run({"datastore": "vivier",
                           "row": {"note": "slot: machine à café"}}))
    assert st.vu, "l'écriture doit atteindre le stockage"


# ── `*` demande TOUTES les colonnes, comme sur les autres surfaces ───────────
#
# Rapatriés le 07/09/2026 du banc de `@claimed`, où ils vivaient parce que l'inventaire
# des jetons y avait commencé. Le jeton `*` n'a rien à voir avec le pronom : il reste.

class _StoreLigne:
    """Un store qui rend une ligne — de quoi éprouver une PROJECTION."""
    # Relevé de résolution du store (`DatastorePg.dernier_tableau`) : les
    # remises y prennent l'IDENTITÉ du tableau — nom canonique + `ns_id`.
    dernier_tableau = {"ns_id": 174, "datastore": "vivier"}

    ligne = {"_id": "r1", "siren": "1", "raison_sociale": "ACME"}

    def __init__(self):
        self.vu = {}

    def get_row(self, datastore, row_id, *, layers="flat", versions=None):
        self.vu["get"] = (datastore, row_id)
        return self.ligne

    def cursor_rows(self, datastore, **kw):
        self.vu["cursor"] = (datastore, kw)
        return {"rows": [self.ligne], "next_cursor": None}

    def declared_key(self, datastore):
        return None

    def get_schema(self, datastore):
        return {"columns": {"siren": {"type": "text"}}}

    def off_schema_report(self):
        return {}


@pytest.fixture
def rows_outil(monkeypatch):
    outil, T = _tool("data_rows")
    st = _StoreLigne()
    monkeypatch.setattr(T, "_acting_store", lambda: st)
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    return outil, st


def _rendu(resultat) -> dict:
    """Le dict que l'appelant reçoit — la face MCP l'emballe dans un `ToolResult`."""
    return resultat.structured_content


def _appel(outil, **kw):
    import asyncio
    return asyncio.run(outil.run(kw))


def test_etoile_dans_fields_rend_la_ligne_ENTIERE(rows_outil):
    """`fields=["*"]` est le chemin vers le brut sur `oto_doc` et sur le feed. Sur
    `data_rows` il tombait dans « colonne inconnue » : l'agent croyait demander tout
    et recevait une projection sur une colonne qui n'existe pas — donc `_id` seul."""
    outil, st = rows_outil
    out = _rendu(_appel(outil, datastore="ns", id="r1", fields=["*"]))
    assert out == st.ligne, "aucune projection : la ligne entière"


def test_etoile_ne_declenche_PAS_l_avertissement_de_colonne_inconnue(rows_outil):
    outil, st = rows_outil
    out = _rendu(_appel(outil, datastore="ns", fields=["*"]))
    assert "inconnue" not in str(out.get("warning", "")).lower()
    assert out["rows"] == [st.ligne], "et la ligne rendue reste WHOLE"


# ── Sur la face REST : LA MÊME couture, pas une seconde idée ─────────────────

def _rest():
    """Importé tard : `_datastore_rest` monte l'adaptateur, et ce fichier tourne aussi
    sans lui pour les tests de la couture seule."""
    import _datastore_rest as H

    from oto_mcp.capabilities.datastore import rows as dsr
    return H, dsr


class _StoreREST:
    # Relevé de résolution du store (`DatastorePg.dernier_tableau`) : les
    # remises y prennent l'IDENTITÉ du tableau — nom canonique + `ns_id`.
    dernier_tableau = {"ns_id": 174, "datastore": 'vivier'}

    def __init__(self):
        self.vu = []

    def append_row(self, ns, data, *, trace=None, readonly_override=False, origine_override=False,
                   donnees_d_origine=False, **_):
        self.vu.append(("append_row", ns, data))
        return {"_id": "r9", **data}

    def update_row(self, ns, row_id, patch, *, trace=None, readonly_override=False, origine_override=False,
                   donnees_d_origine=False, **_):
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
    monkeypatch.setattr(dsr.access, "resolve_datastore_ref",
                        lambda ns: "vivier-2026" if ns.startswith("slot:") else ns)
    return H, st


def test_REST_un_slot_est_RÉSOLU_comme_sur_les_operations_de_schema(rest):
    """⚠️ La divergence silencieuse de l'inventaire : les opérations de SCHÉMA de cette
    même couche résolvaient `slot:` depuis toujours, celles de LIGNES le passaient brut
    au stockage — qui répondait « datastore inconnu » sur un jeton parfaitement valide."""
    H, st = rest
    H.call("me.datastore.append_row", path_params={"datastore": "slot:vivier"},
           body={"a": 1})
    assert st.vu and st.vu[0][1] == "vivier-2026"


def test_REST_le_pronom_retire_rend_400_avec_la_MEME_conduite_que_la_face_agent(rest):
    """Une divergence entre les deux faces sur un jeton s'instruit pendant des jours :
    le retrait vaut des deux côtés, et le refus y porte le même geste."""
    H, st = rest
    status, corps = H.call("me.datastore.update_row",
                           path_params={"datastore": "@claimed", "row_id": "r1"},
                           body={"statut": "enrichi"})
    assert (status, corps["error"]) == (400, "jeton_mal_place")
    assert "REMOVED" in corps["detail"] and "data_claim_next" in corps["detail"]
    assert st.vu == [], "rien ne doit atteindre le stockage"


def test_REST_le_pronom_retire_dans_le_row_id_est_refuse_aussi(rest):
    H, st = rest
    status, corps = H.call("me.datastore.update_row",
                           path_params={"datastore": "7", "row_id": "@claimed"},
                           body={"statut": "enrichi"})
    assert (status, corps["error"]) == (400, "jeton_mal_place")
    assert "`_id`" in corps["detail"]
    assert st.vu == []


def test_REST_le_pronom_dans_le_CORPS_n_est_plus_qu_une_donnee(rest):
    """Le pendant REST du choix daté : retiré des adresses, il ne se refuse plus dans
    une valeur de ligne."""
    H, st = rest
    status, _ = H.call("me.datastore.update_row",
                       path_params={"datastore": "7", "row_id": "r1"},
                       body={"statut": "@claimed"})
    assert status == 200, "une valeur textuelle ne se juge pas sur une syntaxe morte"
    assert st.vu, "l'écriture doit atteindre le stockage"


def test_REST_un_parametre_d_appel_en_colonne_est_refuse(rest):
    H, st = rest
    status, corps = H.call("me.datastore.append_row",
                           path_params={"datastore": "7"},
                           body={"siren": "1", "_run_id": "abc"})
    assert (status, corps["error"]) == (400, "jeton_mal_place")
    assert st.vu == []


def test_REST_une_valeur_qui_ressemble_a_un_slot_passe(rest):
    H, st = rest
    H.call("me.datastore.append_row", path_params={"datastore": "7"},
           body={"note": "slot: machine à café"})
    assert st.vu, "une donnée légitime ne doit pas être refusée"
