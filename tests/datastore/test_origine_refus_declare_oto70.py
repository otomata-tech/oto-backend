"""Second temps du préavis : l'origine s'écrit DÉCLARÉE (oto#70 lot 2, barreau 2).

Le barreau 1 prévenait. Celui-ci refuse — mais pas ce qu'on croyait refuser au départ.
Décision d'Alexis (05/09/2026, « c'est notre modèle d'agent experience ») : **écrire
l'origine reste possible pour tout le monde**, à condition que l'appelant remplisse un
paramètre par lequel il déclare comprendre ce qu'il fait. Pas de scope, pas de droit à
accorder, pas de population à reconnaître : ce qui est refusé, c'est le SILENCE.

Trois choses tiennent ce barreau, et il en faut trois :

- **le paramètre**, sur les deux faces, absent par défaut ;
- **le refus qui dit ce que l'avertissement disait** — corps partagé, pas recopié :
  celui qui s'est préparé pendant le préavis ne doit pas découvrir au moment du refus
  qu'on lui demandait autre chose ;
- **la trace qui distingue les deux écritures** — sans elle, « s'est adapté » et « a
  disparu » se lisent pareil après la date : dans les deux cas les écritures non
  déclarées tombent à zéro.

⚠️ Ce fichier grave aussi une omission du barreau 1, trouvée en le posant : le relevé
avait été branché sur quatre chemins d'écriture, pas sur `update_row` — le patch par
`id`, « le geste le plus courant d'un agent », dit le commentaire du fichier six lignes
au-dessus, où la MÊME omission avait déjà coûté l'effacement de l'origine.
"""
from __future__ import annotations

import re
import uuid
from datetime import date

import pytest

from oto_mcp.datastore import schema as dsv2


# ── la date : le code la porte, le réglage la déplace ─────────────────────────

def test_la_date_vit_dans_le_CODE_pour_que_l_annonce_soit_vraie(monkeypatch):
    """⚠️ L'inverse de ce que le barreau 1 avait écrit, et c'est délibéré. Une date qui
    n'existerait que dans l'env d'une box se lit « prochainement » partout où personne
    ne l'a posée : le produit annoncerait une échéance floue et n'en tiendrait aucune.
    Ici, ce que le tronc ANNONCE est ce qu'il REFUSERA."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    assert dsv2.date_refus() == dsv2.ORIGINE_REFUS_LE
    assert dsv2.date_refus_fr() in dsv2.avertissement_origine(["c"])


def test_le_REGLAGE_deplace_la_date_sans_deployer(monkeypatch):
    """La vraie exigence du barreau 1 : la fenêtre bougera si un écrivain se manifeste,
    et la déplacer ne doit pas demander un déploiement."""
    monkeypatch.setenv(dsv2.ENV_ORIGINE_REFUS_LE, "2026-11-15")
    assert dsv2.date_refus() == date(2026, 11, 15)
    assert "15 novembre 2026" in dsv2.avertissement_origine(["c"])


def test_le_francais_est_DERIVE_de_la_date_qui_refuse(monkeypatch):
    """Deux réglages — « la date affichée » et « la date qui coupe » — divergeraient un
    jour, et c'est l'affichage qui aurait tort : on annoncerait un jour et on
    refuserait un autre."""
    monkeypatch.setenv(dsv2.ENV_ORIGINE_REFUS_LE, "2027-03-01")
    assert "1er mars 2027" in dsv2.refus_origine(["c"])
    assert not dsv2.refus_arme(date(2027, 2, 28))
    assert dsv2.refus_arme(date(2027, 3, 1))


def test_un_reglage_illisible_LEVE_il_ne_retombe_pas_sur_le_defaut(monkeypatch):
    """⚠️ Le repli serait le pire des deux : une faute de frappe ferait promettre une
    échéance que rien n'applique, et personne ne le verrait — le produit continuerait
    d'annoncer poliment une date morte."""
    monkeypatch.setenv(dsv2.ENV_ORIGINE_REFUS_LE, "1er octobre 2026")
    with pytest.raises(ValueError) as e:
        dsv2.date_refus()
    assert dsv2.ENV_ORIGINE_REFUS_LE in str(e.value)


def test_la_bascule_se_lit_en_UTC_pas_au_fuseau_de_la_box(monkeypatch):
    """Deux box dans deux fuseaux refuseraient à deux instants différents, et le
    fuseau d'une machine n'est pas un fait de produit."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    veille = dsv2.ORIGINE_REFUS_LE.toordinal() - 1
    assert not dsv2.refus_arme(date.fromordinal(veille))
    assert dsv2.refus_arme(dsv2.ORIGINE_REFUS_LE)


# ── le refus dit ce que l'avertissement disait ────────────────────────────────

def test_le_refus_et_l_avertissement_PARTAGENT_leur_corps(monkeypatch):
    """⚠️ Partagé, pas recopié. La substitution le prouve : renommer le paramètre doit
    changer les DEUX textes. S'il n'en changeait qu'un, celui qui s'est préparé pendant
    le préavis se verrait refuser au nom d'un autre geste que celui qu'on lui avait
    demandé d'apprendre."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    avant = (dsv2.avertissement_origine(["c"]), dsv2.refus_origine(["c"]))
    monkeypatch.setattr(dsv2, "PARAMETRE_ORIGINE", "zzz_sentinelle")
    apres = (dsv2.avertissement_origine(["c"]), dsv2.refus_origine(["c"]))
    assert apres[0] != avant[0] and apres[1] != avant[1], "AUCUNE substitution"
    assert all("zzz_sentinelle" in t for t in apres)


def test_le_refus_nomme_les_DEUX_gestes_et_ne_renvoie_vers_PERSONNE(monkeypatch):
    """Il n'y a rien à demander : pas de droit, donc pas de tiers. Un refus qui
    enverrait demander quelque chose ferait attendre une réponse qui ne viendra jamais
    — et, comme sur l'autre verrou de la plateforme (#668), enverrait chercher une
    manœuvre."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    texte = dsv2.refus_origine(["prio"])
    assert "écrivez la valeur seule" in texte.lower()
    assert f"`{dsv2.PARAMETRE_ORIGINE}: true`" in texte
    assert "rien n'a été écrit" in texte.lower()
    # ⚠️ Celui qui importe par URL signée ne PEUT pas suivre « ajoutez-le à cet appel » :
    # son PUT ne porte aucun paramètre. Le refus doit lui dire, là où il est, que sa
    # déclaration se fait au moment où l'URL est créée — sinon on l'envoie chercher une
    # manœuvre, ce que ce lot existe pour éviter.
    assert "oto_upload_url" in texte
    interdits = r"\b(demandez|permission|autorisation|habilitation|contactez)\b"
    assert not re.search(interdits, texte, re.I), texte
    assert not re.search(r"\b(ton|ta|tes|tu|écris)\b", texte, re.I), texte


def test_la_description_servie_NOMME_le_paramètre(monkeypatch):
    """⚠️ Une capacité qu'aucun texte ne nomme n'existe pas pour un agent : il ne la
    découvrira pas, il retombera sur la manœuvre qu'on cherche à supprimer. C'est ce
    qui s'est passé sur l'autre verrou (#658/#668) — le refus était exact, la sortie
    n'était écrite nulle part, et deux agents ont réinventé « lever, écrire,
    remettre »."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    for texte in (dsv2.description_parametre_origine(),
                  dsv2.description_parametre_origine(en=True)):
        assert dsv2.PARAMETRE_ORIGINE in texte
        assert not re.search(r"\b(demandez|permission|ask your|admin)\b", texte, re.I)


def test_les_deux_faces_disent_LE_MEME_paramètre():
    """La face REST sert la description française, la face MCP recopie l'anglaise dans
    sa docstring (`@mcp.tool()` lit la docstring littérale, et `description=`
    emporterait les descriptions d'arguments). La copie est donc SURVEILLÉE ici, faute
    de pouvoir être évitée : le jour où le nom ou la date bouge, ce banc tombe."""
    from oto_mcp.tools import datastore as tools_ds

    src = __import__("inspect").getsource(tools_ds)
    assert dsv2.PARAMETRE_ORIGINE in src
    assert str(dsv2.ORIGINE_REFUS_LE) in src, (
        "la docstring MCP annonce une autre date que celle qui refuse")


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
                              origine_override=False):
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
            "u1", {"kind": "datastore", "ns_id": 1, "namespace": "t",
                   "format": "ndjson", "key": None, "origine_override": declare},
            corps, "application/x-ndjson")
        assert vus["origine_override"] is declare


def test_un_jeton_d_AVANT_ce_lot_ne_declare_rien():
    """Les jetons déjà signés ne portent pas la clé : leur absence doit se lire « non
    déclaré », jamais « déclaré ». Le défaut penche du côté qui refuse."""
    from oto_mcp.capabilities.uploads import UploadUrlInput

    assert UploadUrlInput(target="datastore", namespace="t").origine_override is False


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
    cible = {"kind": "datastore", "ns_id": 1, "namespace": "t",
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

@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_org_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


#: Sans format déclaré : le cas SUSPECT, celui où la plateforme ne pose jamais
#: d'origine — donc celle-ci ne peut venir que de l'écrivain (64 cellules mesurées).
SCHEMA = {"fields": [{"key": "ref", "type": "text"},
                     {"key": "prio", "type": "text"}]}


def _table(schema=SCHEMA):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", "sub-origine", ns)
    st = make_store("sub-origine")
    st.set_schema(ns, schema)
    return st, ns, ns_id


def _arme(monkeypatch, arme: bool = True):
    """Le refus, armé ou non, par la DATE — jamais par un drapeau de test : c'est la
    date qui le déclenchera en production, et une épreuve qui court-circuiterait ce
    chemin-là ne prouverait rien de ce qui se passera le jour venu."""
    quand = date(2000, 1, 1) if arme else date(2099, 1, 1)
    monkeypatch.setenv(dsv2.ENV_ORIGINE_REFUS_LE, quand.isoformat())


def _trace(ns_id: int) -> list[dict]:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return list(conn.execute(
            "SELECT colonne, ecritures, ecritures_declarees, derniere_declaree_at "
            "FROM origine_ecritures WHERE ns_id=%s ORDER BY colonne", (ns_id,)).fetchall())


def test_avant_la_date_l_ecriture_passe_avec_son_avertissement(live, monkeypatch):
    """Le barreau 1 tient tel quel : rien n'est refusé tant que la date n'est pas là."""
    _arme(monkeypatch, arme=False)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "a", "prio": {"valeur": "B", "origine": "A"}})
    assert (row["prio"], row["prio.origine"]) == ("B", "A")
    assert dsv2.PARAMETRE_ORIGINE in st.off_schema_report()["origine_warning"]


def test_apres_la_date_l_ecriture_SILENCIEUSE_est_refusee(live, monkeypatch):
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    with pytest.raises(ValueError) as e:
        st.append_row(ns, {"ref": "b", "prio": {"valeur": "B", "origine": "A"}})
    assert dsv2.PARAMETRE_ORIGINE in str(e.value)
    assert st.list_rows(ns) == [], "la ligne a été écrite malgré le refus"


def test_apres_la_date_la_MEME_ecriture_DECLAREE_passe(live, monkeypatch):
    """⚠️ Le fond de la décision : ce n'est pas l'écriture qu'on refuse. Un import qui
    doit vraiment poser l'origine le peut, sans rien demander à personne."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "c", "prio": {"valeur": "B", "origine": "A"}},
                        origine_override=True)
    assert (row["prio"], row["prio.origine"]) == ("B", "A")


def test_ecrire_la_valeur_seule_n_a_jamais_besoin_du_paramètre(live, monkeypatch):
    """L'autre chemin que l'avertissement nomme. Celui qui n'écrit pas d'origine ne
    doit RIEN changer — un préavis qui ferait bouger ces appels-là coûterait à des gens
    qui n'y sont pour rien."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    assert st.append_row(ns, {"ref": "d", "prio": "B"})["prio"] == "B"
    assert _trace(ns_id) == []


def test_le_patch_par_id_est_gardé_COMME_LES_AUTRES(live, monkeypatch):
    """⚠️ Le chemin que le barreau 1 avait oublié. `update_row` n'appelle pas
    `_merge_into_row` : c'est un cinquième chemin d'écriture, et c'est « le geste le
    plus courant d'un agent » — le fichier le dit lui-même, à l'endroit exact où la
    même omission avait déjà effacé l'origine une première fois."""
    _arme(monkeypatch, arme=False)
    st, ns, ns_id = _table()
    row = st.append_row(ns, {"ref": "e", "prio": "B"})
    _arme(monkeypatch)
    with pytest.raises(ValueError) as e:
        st.update_row(ns, row["_id"], {"prio": {"valeur": "C", "origine": "forgée"}})
    assert dsv2.PARAMETRE_ORIGINE in str(e.value)
    st.update_row(ns, row["_id"], {"prio": {"valeur": "C", "origine": "forgée"}},
                  origine_override=True)
    assert [t["ecritures_declarees"] for t in _trace(ns_id)] == [1]


def test_la_trace_SEPARE_l_ecriture_declaree_de_l_autre(live, monkeypatch):
    """⚠️ C'est elle qui dira, après la date, si un écrivain s'est ADAPTÉ ou a DISPARU.
    Les écritures non déclarées tombent à zéro dans les deux cas : un compteur unique
    ne saurait pas les distinguer, et on lirait un silence comme une réussite."""
    _arme(monkeypatch, arme=False)
    st, ns, ns_id = _table()
    st.append_row(ns, {"ref": "f", "prio": {"valeur": "B", "origine": "A"}})
    (ligne,) = _trace(ns_id)
    assert (ligne["ecritures"], ligne["ecritures_declarees"]) == (1, 0)
    assert ligne["derniere_declaree_at"] is None

    st.append_row(ns, {"ref": "g", "prio": {"valeur": "B", "origine": "A"}},
                  origine_override=True)
    (ligne,) = _trace(ns_id)
    assert (ligne["ecritures"], ligne["ecritures_declarees"]) == (2, 1)
    assert ligne["derniere_declaree_at"] is not None

    # ⚠️ Une écriture non déclarée qui SUIT ne doit pas effacer la date de la déclarée,
    # sinon « s'est adapté puis a rechuté » se lirait « n'a jamais déclaré ».
    st.append_row(ns, {"ref": "h", "prio": {"valeur": "B", "origine": "A"}})
    (ligne,) = _trace(ns_id)
    assert (ligne["ecritures"], ligne["ecritures_declarees"]) == (3, 1)
    assert ligne["derniere_declaree_at"] is not None


def test_un_appel_REFUSÉ_ne_gonfle_pas_la_population(live, monkeypatch):
    """Un refus n'est pas une écriture. Le compter ferait grossir la population de gens
    qui, précisément, n'ont pas réussi à écrire — et c'est sur ce nombre-là qu'on
    décidera s'il faut prévenir quelqu'un."""
    _arme(monkeypatch)
    st, ns, ns_id = _table()
    with pytest.raises(ValueError):
        st.append_row(ns, {"ref": "i", "prio": {"valeur": "B", "origine": "A"}})
    assert _trace(ns_id) == []
