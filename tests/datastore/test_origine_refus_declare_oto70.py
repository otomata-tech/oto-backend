"""L'origine s'écrit DÉCLARÉE (oto#70 lot 2) — refusée en silence, permise déclarée.

Décision d'Alexis (05/09/2026, « c'est notre modèle d'agent experience ») : **écrire
l'origine reste possible pour tout le monde**, à condition que l'appelant remplisse un
paramètre par lequel il déclare comprendre ce qu'il fait (`origine_override`). Pas de
scope, pas de droit à accorder : ce qui est refusé, c'est le SILENCE. Le préavis daté
qui l'annonçait (01/10/2026) est retiré : le refus est la règle, sans date ni réglage.

Ce qui tient la règle :

- **le paramètre**, sur les deux faces, absent par défaut — et jamais ignoré : déclaré,
  l'écriture passe ;
- **le refus qui nomme les deux gestes** (écrire la valeur seule, ou déclarer) et la
  couche qui accueille la provenance (`comment`, oto#79).
"""
from __future__ import annotations

import re
import uuid

import pytest

from oto_mcp.datastore import schema as dsv2
# ⚠️ Le remplacement vise le module qui PORTE la valeur, pas la façade
# `schema` : depuis la coupe du 07/09/2026, celle-ci ré-exporte — remplacer
# sur elle ne change rien à ce que l'implémentation lit. Le déplacement reste
# pur pour les appelants ; seul le point de remplacement d'un banc change.
from oto_mcp.datastore import champs_reserves


# ── ce que l'appel POSE, indépendamment du format ─────────────────────────────

def test_poser_une_origine_est_releve():
    assert dsv2.origine_posee({"col": {"valeur": "x", "origine": "forgé"}}) == ["col"]


def test_reecrire_la_MEME_origine_ne_releve_rien():
    """⚠️ Relire puis repousser tel quel est un geste banal : le compter ferait refuser
    des appels qui ne changent rien."""
    avant = {"col": {"valeur": "B", "origine": "A"}}
    assert dsv2.origine_posee({"col": {"valeur": "x", "origine": "A"}}, avant) == []


def test_ecrire_la_valeur_seule_ne_releve_rien():
    """Le geste que le refus recommande ne doit surtout pas le déclencher."""
    assert dsv2.origine_posee({"col": "x"}, {"col": {"valeur": "B", "origine": "A"}}) == []


def test_effacer_une_origine_est_releve_aussi():
    """`{"origine": null}` retire une origine en place : c'est une modification de la
    couche, pas une abstention."""
    avant = {"col": {"valeur": "B", "origine": "A"}}
    assert dsv2.origine_posee({"col": {"origine": None}}, avant) == ["col"]


# ── le texte du refus ─────────────────────────────────────────────────────────

def test_le_refus_LIT_le_parametre_dans_la_constante(monkeypatch):
    """La substitution le prouve : si la phrase recopiait le nom en littéral, renommer
    la constante servirait un paramètre qui n'existe pas."""
    avant = dsv2.refus_origine(["c"])
    monkeypatch.setattr(champs_reserves, "PARAMETRE_ORIGINE", "zzz_sentinelle")
    apres = dsv2.refus_origine(["c"])
    assert apres != avant, "AUCUNE substitution"
    assert "zzz_sentinelle" in apres


def test_le_refus_nomme_les_DEUX_gestes_et_ne_renvoie_vers_PERSONNE():
    """Il n'y a rien à demander : pas de droit, donc pas de tiers. Un refus qui
    enverrait demander quelque chose ferait attendre une réponse qui ne viendra jamais
    — et, comme sur l'autre verrou de la plateforme (#668), enverrait chercher une
    manœuvre."""
    texte = dsv2.refus_origine(["prio"])
    assert "write the value alone" in texte.lower()
    assert "remains possible" in texte
    # Pour un import, le geste qui pose l'origine sans l'écrire soi-même est nommé.
    assert "`donnees_d_origine: true`" in texte
    # Plus de date : le refus est la règle, il ne raconte pas son histoire.
    assert "octobre" not in texte and "depuis le" not in texte
    # Plus de promesse d'un filet mort (rien ne pose l'origine d'office).
    assert "posée par la plateforme quand elle manque" not in texte
    assert f"`{dsv2.PARAMETRE_ORIGINE}: true`" in texte
    assert "nothing was written" in texte.lower()
    # ⚠️ Celui qui importe par URL signée ne PEUT pas suivre « ajoutez-le à cet appel » :
    # son PUT ne porte aucun paramètre. Le refus doit lui dire, là où il est, que sa
    # déclaration se fait au moment où l'URL est créée — sinon on l'envoie chercher une
    # manœuvre, ce que ce lot existe pour éviter.
    assert "oto_upload_url" in texte
    interdits = r"\b(demandez|permission|autorisation|habilitation|contactez)\b"
    assert not re.search(interdits, texte, re.I), texte
    assert not re.search(r"\b(ton|ta|tes|tu|écris)\b", texte, re.I), texte


def test_la_description_servie_NOMME_le_paramètre():
    """⚠️ Une capacité qu'aucun texte ne nomme n'existe pas pour un agent : il ne la
    découvrira pas, il retombera sur la manœuvre qu'on cherche à supprimer. C'est ce
    qui s'est passé sur l'autre verrou (#658/#668) — le refus était exact, la sortie
    n'était écrite nulle part, et deux agents ont réinventé « lever, écrire,
    remettre »."""
    for texte in (dsv2.description_parametre_origine(),
                  dsv2.description_parametre_origine(en=True)):
        assert dsv2.PARAMETRE_ORIGINE in texte
        assert "2026-10-01" not in texte and "octobre" not in texte
        assert not re.search(r"\b(demandez|permission|ask your|admin)\b", texte, re.I)


def test_les_deux_faces_disent_LE_MEME_paramètre():
    """La face REST sert la description française, la face MCP recopie l'anglaise dans
    sa docstring (`@mcp.tool()` lit la docstring littérale, et `description=`
    emporterait les descriptions d'arguments). La copie est donc SURVEILLÉE ici, faute
    de pouvoir être évitée : le jour où le nom bouge, ce banc tombe."""
    from oto_mcp.tools import datastore as tools_ds

    src = __import__("inspect").getsource(tools_ds)
    assert dsv2.PARAMETRE_ORIGINE in src
    assert "2026-10-01" not in src, "la docstring MCP annonce encore la date du préavis"


# ── l'upload signé : la porte de l'IMPORT ─────────────────────────────────────

def test_l_upload_signe_declare_au_MINT_et_le_PUT_ne_peut_pas_se_le_donner(monkeypatch):
    """⚠️ L'upload est LA porte de l'import — donc celle où poser l'origine est le plus
    légitime, et celle qui n'a AUCUN paramètre à passer : le PUT est une URL signée
    qu'un socle appelle sans rien décider.

    Sans ce chemin, le 1er octobre aurait refusé exactement le geste que la décision
    d'Alexis veut préserver, sans issue possible pour l'appelant.

    La déclaration est donc faite au mint, par celui qui prépare l'import, et SCELLÉE
    dans le jeton signé — à la réception, personne ne peut se l'accorder."""
    from oto_mcp import upload_tokens

    vus: dict = {}

    class _Store:
        def _write_rows_to_ns(self, ns_id, rows, *, key=None, readonly_override=False,
                              origine_override=False, donnees_d_origine=False, **_):
            vus["origine_override"] = origine_override
            return {"inserted": len(rows), "updated": 0, "count": len(rows)}

        def off_schema_report(self):
            return {}

        def _schema_of(self, ns_id):
            return None

    import oto_mcp.datastore.core as ds_core
    monkeypatch.setattr(ds_core, "make_store", lambda sub: _Store())

    corps = b'{"ref": "a", "prio": {"valeur": "B", "origine": "A"}}\n'
    for declare in (False, True):
        upload_tokens.materialize(
            "u1", {"kind": "datastore", "ns_id": 1, "datastore": "t",
                   "format": "ndjson", "key": None, "origine_override": declare},
            corps, "application/x-ndjson")
        assert vus["origine_override"] is declare


def test_un_jeton_d_AVANT_ce_lot_ne_declare_rien():
    """Les jetons déjà signés ne portent pas la clé : leur absence doit se lire « non
    déclaré », jamais « déclaré ». Le défaut penche du côté qui refuse."""
    from oto_mcp.capabilities.uploads import UploadUrlInput

    assert UploadUrlInput(target="datastore", datastore="t").origine_override is False


def test_un_jeton_emis_SANS_declaration_ne_se_rejoue_pas_AVEC(monkeypatch):
    """⚠️ Le corollaire du scellement : si la déclaration pouvait s'ajouter après coup,
    elle ne déclarerait plus rien — n'importe qui ayant l'URL se la donnerait.

    On l'éprouve en ATTAQUANT le jeton : on rouvre son payload, on y pose le drapeau,
    on recolle la signature d'origine. Le jeton doit être refusé — et le seul moyen de
    le faire accepter serait de connaître le secret de signature, c'est-à-dire d'être la
    plateforme."""
    import base64
    import json

    from oto_mcp import upload_tokens

    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "s3cr3t-de-banc")
    cible = {"kind": "datastore", "ns_id": 1, "datastore": "t",
             "format": "ndjson", "key": None, "origine_override": False}
    token, _exp = upload_tokens.sign("u1", None, cible)
    assert upload_tokens.verify(token)["target"]["origine_override"] is False

    p_b64, sig_b64 = token.split(".", 1)
    brut = base64.urlsafe_b64decode(p_b64 + "=" * (-len(p_b64) % 4))
    charge = json.loads(brut)
    charge["target"]["origine_override"] = True
    truque = base64.urlsafe_b64encode(
        json.dumps(charge, separators=(",", ":"), sort_keys=True).encode()
    ).rstrip(b"=").decode() + "." + sig_b64
    assert truque != token, "AUCUNE substitution : l'épreuve ne prouverait rien"
    assert upload_tokens.verify(truque) is None, "un jeton retouché a été accepté"


def test_la_reception_d_un_upload_ne_lit_AUCUN_parametre_de_requete():
    """L'autre moitié du scellement : le jeton ne peut pas être retouché (ci-dessus),
    encore faut-il que la réception ne prenne pas la déclaration AILLEURS — une query,
    un en-tête. Aujourd'hui `upload_receive` ne lit que la route (le jeton), le corps et
    le `content-type` ; ajouter une lecture de `query_params` rouvrirait la porte que le
    scellement ferme.

    ⚠️ Sonde de SOURCE, et elle le dit : elle n'exécute pas la requête, elle veille sur
    la forme du handler. Elle ne prouve pas qu'aucun chemin n'existe — elle arrête celui
    par lequel il reviendrait."""
    import inspect

    from oto_mcp.api import uploads as api_uploads

    src = inspect.getsource(api_uploads.upload_receive)
    assert "query_params" not in src, (
        "la réception lit un paramètre de requête : la déclaration scellée dans le "
        "jeton pourrait être contournée depuis l'URL")


# ── ce que ça fait vraiment, sur une base ─────────────────────────────────────


#: Sans format déclaré : le cas SUSPECT, celui où la plateforme ne pose jamais
#: d'origine — donc celle-ci ne peut venir que de l'écrivain (64 cellules mesurées).
SCHEMA = {"fields": [{"key": "ref", "type": "text"},
                     {"key": "prio", "type": "text"}]}


def _table(schema=SCHEMA):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns_id = db.create_datastore("user", "sub-origine", "t-" + uuid.uuid4().hex[:6])
    ns = str(ns_id)
    st = make_store("sub-origine")
    st.set_schema(ns, schema)
    return st, ns, ns_id


def test_l_ecriture_SILENCIEUSE_est_refusee(live):
    st, ns, ns_id = _table()
    with pytest.raises(ValueError) as e:
        st.append_row(ns, {"ref": "b", "prio": {"valeur": "B", "origine": "A"}})
    assert dsv2.PARAMETRE_ORIGINE in str(e.value)
    assert st.list_rows(ns) == [], "la ligne a été écrite malgré le refus"


def test_la_MEME_ecriture_DECLAREE_passe_sans_avertissement(live):
    """⚠️ Le fond de la décision : ce n'est pas l'écriture qu'on refuse. Un import qui
    doit vraiment poser l'origine le peut, sans rien demander à personne — et le
    paramètre n'est jamais ignoré : l'écriture déclarée passe, et aucun avertissement
    de préavis ne la suit."""
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "c", "prio": {"valeur": "B", "origine": "A"}},
                        origine_override=True, versions=("current", "origine"))
    assert (row["prio"], row["prio.origine"]) == ("B", "A")
    assert "origine_warning" not in st.off_schema_report()


def test_ecrire_la_valeur_seule_n_a_jamais_besoin_du_paramètre(live):
    """L'autre chemin que le refus nomme. Celui qui n'écrit pas d'origine ne doit RIEN
    changer."""
    st, ns, ns_id = _table()
    assert st.append_row(ns, {"ref": "d", "prio": "B"})["prio"] == "B"


def test_le_patch_par_id_est_gardé_COMME_LES_AUTRES(live):
    """⚠️ Le chemin que le barreau 1 avait oublié. `update_row` n'appelle pas
    `_merge_into_row` : c'est un cinquième chemin d'écriture, et c'est « le geste le
    plus courant d'un agent » — le fichier le dit lui-même, à l'endroit exact où la
    même omission avait déjà effacé l'origine une première fois."""
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "e", "prio": "B"})
    with pytest.raises(ValueError) as e:
        st.update_row(ns, row["_id"], {"prio": {"valeur": "C", "origine": "forgée"}})
    assert dsv2.PARAMETRE_ORIGINE in str(e.value)
    st.update_row(ns, row["_id"], {"prio": {"valeur": "C", "origine": "forgée"}},
                  origine_override=True)
    assert st.get_row(ns, row["_id"], versions=("current", "origine"))["prio.origine"] == "forgée"


# --- oto#79 : le texte nomme la couche qui accueille l'intention -----------------

def test_le_texte_nomme_la_couche_qui_accueille_l_intention_oto79():
    """Huit refus sur dix-huit rejouaient le geste refusé, la reprise la plus rapide à
    neuf secondes. L'agent ne cherchait pas à écrire une valeur : il cherchait à dire
    d'où elle venait, et le texte ne nommait jamais la couche qui accueille ça. Le refus
    voisin — celui d'une colonne servie en lecture — le fait depuis toujours.

    ⚠️ La forme d'écriture est exigée avec le nom : nommer `comment` sans montrer où il
    se met laisse l'agent deviner, et c'est ce qu'on cherche à supprimer."""
    for texte in (dsv2.refus_origine(["charge_affaires"]),):
        assert "`charge_affaires.comment`" in texte, texte
        assert '{"charge_affaires": {"comment": …}}' in texte, texte
        # Le registre ne change pas : ces textes VOUVOIENT (une personne décidera).
        assert not re.search(r"\b(ton|ta|tes|tu|écris)\b", texte, re.I), texte


def test_le_refus_qui_promettait_un_mecanisme_MORT_a_disparu():
    """La branche qui servait « l'origine est conservée, et posée si elle manque » était
    gardée par un cran SUPPRIMÉ le 08/09/2026 : elle ne pouvait plus jamais servir, et
    elle promettait un filet qui n'existe plus. Le banc garde les deux faits — le texte
    parti, et la raison pour laquelle il ne reviendra pas."""
    import inspect

    from oto_mcp.datastore import champs_reserves
    src = inspect.getsource(champs_reserves)
    assert "est posée par le système à partir de la valeur" not in src
    assert dsv2.system_origin_fields({"fields": [{"key": "x", "origine": "system"}]}) == set()

