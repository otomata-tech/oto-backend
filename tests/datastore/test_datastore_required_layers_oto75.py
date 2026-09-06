"""`required_layers` : exiger qu'une valeur arrive AVEC sa provenance — oto#75, barreau 1.

L'attribut vivait dans trois schémas de production **sans aucun lecteur** : posé, servi
dans le contrat, et sans le moindre effet. Son auteur croyait la provenance exigée ;
rien ne l'exigeait, et rien ne le disait. Ce banc fige le lecteur qui lui manquait.

⚠️ **Ce que la garde ne fait pas** : elle n'empêche pas un commentaire FAUX. Elle oblige
à NOMMER une source, ce qui rend le mensonge vérifiable ; la vérité reste à la relecture
sur pièces.

Deux choses se prouvent ici et nulle part ailleurs :

 * **le préalable mesuré** — la restriction par `written` ne contient que des clés de
   PREMIER NIVEAU. Une contrainte posée sur une clé POINTÉE (`colonne.comment`) ne
   refuse donc jamais rien sur un patch, pas même sur le patch qui écrit la colonne :
   c'est l'état actuel de `pattern`/`max_length` sur une couche. La garde de ce lot est
   posée sur la colonne de BASE, et le banc le prouve en exerçant les deux côte à côte ;
 * **les DEUX faces**, outil et REST. Un avertissement voisin n'existe que sur REST
   alors que la description de l'outil promet le contraire ; le seam d'écriture est
   unique (`_check_row`), mais « unique » est une lecture de code — que les deux portes
   y arrivent se PROUVE, par une épreuve par face.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from oto_mcp.datastore import schema as dsv2

SUB = "sub-rl75"

_COMMENT = {"key": "qualif", "type": "text", "required_layers": ["comment"]}
_SCHEMA = {"key": "ref", "fields": [
    {"key": "ref", "type": "text"},
    _COMMENT,
    {"key": "libre", "type": "text"},
]}


# ── le PRÉALABLE : `written` ne contient que le premier niveau ───────────────

_POINTEE = {"fields": [
    {"key": "q", "type": "text", "max_length": 40},
    {"key": "q.comment", "max_length": 12},
]}
_TROP_LONG = {"q": {"valeur": "ok", "comment": "un commentaire beaucoup trop long"}}


def test_une_contrainte_posee_sur_une_COUCHE_ne_mord_pas_sur_un_patch():
    """Le préalable, mesuré. `written` = les clés de PREMIER NIVEAU que l'appelant a
    nommées ; un nom pointé n'y entre jamais (les noms pointés ne s'écrivent pas). Une
    borne déclarée sur `q.comment` est donc muette sur TOUT patch — y compris celui qui
    écrit `q`. Elle ne mord qu'à l'insertion, où `written` vaut None.

    Ce n'est pas le défaut de ce lot ; c'est le piège dans lequel il ne devait pas
    tomber, et la raison pour laquelle la garde se restreint par la colonne de BASE."""
    assert dsv2.validate_row(_POINTEE, _TROP_LONG, written=None), \
        "à l'insertion, la borne mord"
    assert dsv2.validate_row(_POINTEE, _TROP_LONG, written={"q"}) == [], \
        "le geste ÉCRIT `q` et la borne posée sur `q.comment` ne dit rien"
    assert dsv2.validate_row(_POINTEE, _TROP_LONG, written={"autre"}) == []


def test_la_garde_de_ce_lot_mord_sur_le_patch_qui_ECRIT_la_colonne():
    """L'autre moitié du préalable : la même restriction, posée sur la colonne de BASE,
    refuse là où la précédente se tait. Sans ce test, on aurait livré une règle acceptée
    qui ne refuse jamais rien sur une écriture partielle — et le premier signal aurait
    été un taux de conformité inexplicablement bon."""
    assert dsv2.validate_row(_SCHEMA, {"qualif": "x"}, written={"qualif"})
    assert dsv2.validate_row(_SCHEMA, {"qualif": "x"}, written=None)


# ── la sémantique, cas par cas ──────────────────────────────────────────────

_LISTE = {"fields": [
    {"key": "contacts", "type": "list",
     "of": {"type": "object",
            "fields": [{"key": "email", "type": "text",
                        "required_layers": ["comment"]},
                       {"key": "nom", "type": "text"}]}},
    {"key": "libre", "type": "text"},
]}
_OBJET = {"fields": [
    {"key": "occupant", "type": "object",
     "fields": [{"key": "naf", "type": "text", "required_layers": ["comment"]}]},
    {"key": "libre", "type": "text"},
]}


@pytest.mark.parametrize("quoi,schema,row,written,refuse", [
    ("valeur nue, colonne écrite", _SCHEMA, {"qualif": "x"}, {"qualif"}, True),
    ("valeur + comment", _SCHEMA,
     {"qualif": {"valeur": "x", "comment": "registre"}}, {"qualif"}, False),
    ("valeur nulle", _SCHEMA, {"qualif": None}, {"qualif"}, False),
    ("valeur vide", _SCHEMA, {"qualif": ""}, {"qualif"}, False),
    ("couche seule, sans valeur", _SCHEMA,
     {"qualif": {"comment": "une remarque"}}, {"qualif"}, False),
    ("patch sur une AUTRE colonne", _SCHEMA,
     {"qualif": "x", "libre": "y"}, {"libre"}, False),
    ("insertion entière", _SCHEMA, {"qualif": "x"}, None, True),
    ("comment vide", _SCHEMA,
     {"qualif": {"valeur": "x", "comment": ""}}, {"qualif"}, True),
    ("liste, sous-champ nu", _LISTE,
     {"contacts": [{"nom": "a", "email": "a@b.c"}]}, {"contacts"}, True),
    ("liste, sous-champ en couches", _LISTE,
     {"contacts": [{"email": {"valeur": "a@b.c", "comment": "carte"}}]},
     {"contacts"}, False),
    ("liste NON écrite par le geste", _LISTE,
     {"contacts": [{"email": "a@b.c"}], "libre": "y"}, {"libre"}, False),
    ("sous-record d'objet nu", _OBJET, {"occupant": {"naf": "6201Z"}},
     {"occupant"}, True),
    ("objet NON écrit par le geste", _OBJET,
     {"occupant": {"naf": "6201Z"}, "libre": "y"}, {"libre"}, False),
])
def test_la_semantique_du_vide_et_de_la_portee(quoi, schema, row, written, refuse):
    """Le vide ne déclenche RIEN — c'est la forme légitime d'une remarque. La portée
    descend dans les composites, et s'arrête à ce que le geste ne touche pas."""
    errors = dsv2.validate_row(schema, row, written=written)
    assert bool(errors) is refuse, (quoi, errors)


def test_une_liste_fautive_ne_dit_pas_300_fois_la_meme_chose():
    """Même borne que le refus de sous-champ inconnu : un refus qu'on ne peut pas lire
    ne vaut pas mieux qu'un silence."""
    errors = dsv2.validate_row(
        _LISTE, {"contacts": [{"email": f"a{i}@b.c"} for i in range(300)]},
        written={"contacts"})
    assert len(errors) == 1 and errors[0].startswith("contacts[0].email:")


@pytest.mark.parametrize("cran", [
    {"readonly": True},
    {"system": "write.at"},
    {"role": "status"},
])
def test_les_colonnes_que_l_appelant_n_ECRIT_pas_sont_hors_de_portee(cran):
    """Exiger une provenance de qui n'écrit pas la valeur ferait refuser des écritures
    que personne ne peut corriger : la colonne du fichier source, l'estampille reposée
    par la plateforme, et celle qui porte le cycle de vie."""
    schema = {"fields": [{"key": "q", "type": "text",
                          "required_layers": ["comment"], **cran}]}
    assert dsv2.validate_row(schema, {"q": "x"}, written={"q"}) == []


def test_la_couche_posee_par_la_PLATEFORME_ne_se_reclame_pas():
    """Sur une colonne `origine: "system"`, la plateforme pose la couche et REFUSE que
    l'appelant la nomme. L'exiger serait un requis impossible à satisfaire."""
    schema = {"fields": [{"key": "q", "type": "text", "origine": "system",
                          "required_layers": ["origine", "comment"]}]}
    assert dsv2.required_layers_of(schema["fields"][0]) == ("comment",)
    assert dsv2.validate_row(schema, {"q": {"valeur": "x", "comment": "src"}},
                             written={"q"}) == []


# ── le refus DIT OÙ écrire ──────────────────────────────────────────────────

def test_le_refus_nomme_la_colonne_ET_sa_destination():
    """Un refus qui dit seulement ce qui est interdit fait REJOUER le même appel —
    mesuré sur la famille voisine, huit refus sur dix-huit revenaient à l'identique,
    certains en neuf secondes. Il doit porter la FORME à écrire."""
    (msg,) = dsv2.validate_row(_SCHEMA, {"qualif": "x"}, written={"qualif"})
    assert msg.startswith("qualif:")                      # la colonne
    assert '"qualif": {"valeur": <ta valeur>, "comment": "…"}' in msg   # la destination
    assert "MÊME appel" in msg                            # et le tour où l'écrire


# ── la déclaration : refusée à la POSE, muette à l'écriture ────────────────

@pytest.mark.parametrize("mauvaise", ["comment", [], ["commentaire"], {"comment": 1}, 3])
def test_une_declaration_illisible_est_refusee_DEVANT_CELUI_QUI_LA_POSE(mauvaise):
    """La règle de famille (#329/#331/#347) : une forme non interprétée se refuse à la
    pose. C'est l'exacte faute que cet attribut a commise pendant trois schémas — une
    couche que la plateforme ne connaît pas n'exigerait rien, et son auteur croirait la
    provenance exigée."""
    errors = dsv2.validate_schema_def(
        {"fields": [{"key": "q", "type": "text", "required_layers": mauvaise}]})
    assert any("required_layers" in e for e in errors), (mauvaise, errors)


def test_un_schema_DEJA_EN_BASE_ne_fait_pas_exploser_une_ecriture():
    """Même parti pris que `max_length_of`/`pattern_of` : le lecteur est muet sur une
    déclaration illisible. L'attribut dort dans des tableaux de production depuis avant
    ce lecteur ; les rendre inécrivables serait pire que le silence qu'on corrige."""
    vieux = {"fields": [{"key": "q", "type": "text",
                         "required_layers": ["commentaire"]}]}
    assert dsv2.validate_row(vieux, {"q": "x"}, written={"q"}) == []


def test_la_regle_ne_s_ARME_que_sur_sa_propre_declaration():
    """`validation_active` arme la validation ENTIÈRE (types, requis, composites
    fermés). L'élargir ferait basculer dans ce régime, du jour au lendemain, les
    tableaux qui portent DÉJÀ l'attribut — sur des règles qu'ils n'ont jamais
    demandées. La garde s'arme donc seule, comme le cycle de vie."""
    assert dsv2.validation_active(_SCHEMA) is False
    assert dsv2.validate_row(_SCHEMA, {"qualif": "x"}, written={"qualif"})
    # et elle ne réveille rien d'autre : le type n'est pas opposable ici
    assert dsv2.validate_row(_SCHEMA, {"ref": 12}, written={"ref"}) == []


def test_le_deploiement_ANNONCE_qu_il_l_execute():
    """La sonde d'`enforced` : la clé qui a vécu trois schémas sans lecteur est la
    première qu'un client doit pouvoir opposer au serveur qui lui répond."""
    dsv2.reset_enforced_keys()
    try:
        assert "required_layers" in dsv2.enforced_keys()
    finally:
        dsv2.reset_enforced_keys()


# ── LES DEUX FACES, sur du vrai SQL ────────────────────────────────────────

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


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def _table():
    from oto_mcp import db
    ns = "rl75-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore_namespace("user", SUB, ns)
    _store().set_schema(ns, _SCHEMA)
    return ns, ns_id


def _lignes(ns_id: int) -> list[dict]:
    """Ce que porte la BASE, jamais ce que la réponse a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return [dict(r["data"] or {}) for r in conn.execute(
            "SELECT data FROM datastore_rows WHERE ns_id=%s", (ns_id,)).fetchall()]


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if nom not in _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-rl75")
        register_all(m)
        _OUTILS[nom] = asyncio.run(m.get_tool(nom))
    return _OUTILS[nom]


def _appeler(outil, **kw):
    r = outil.fn(**kw)
    return asyncio.run(r) if asyncio.iscoroutine(r) else r


@pytest.fixture
def face_outil(monkeypatch):
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)
    from oto_mcp import call_axes
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    return _outil("data_write")


def test_face_OUTIL_le_refus_tombe_a_l_ecriture(live, face_outil):
    """La face agent. Le refus nomme la colonne, et RIEN n'est écrit."""
    from oto_mcp.mcp_errors import McpError

    ns, ns_id = _table()
    with pytest.raises(McpError) as capture:
        _appeler(face_outil, namespace=ns, rows=[{"ref": "r1", "qualif": "PME"}])
    assert "qualif" in capture.value.error.message
    assert '"comment": "…"' in capture.value.error.message
    assert _lignes(ns_id) == []


def test_face_OUTIL_la_valeur_avec_sa_provenance_passe(live, face_outil):
    ns, ns_id = _table()
    _appeler(face_outil, namespace=ns,
             rows=[{"ref": "r1", "qualif": {"valeur": "PME", "comment": "INSEE"}}])
    (ligne,) = _lignes(ns_id)
    assert ligne["qualif"] == {"valeur": "PME", "comment": "INSEE"}


def test_face_REST_le_refus_tombe_a_l_ecriture(live, monkeypatch):
    """La face REST, par sa VRAIE route. Même refus, même message : c'est ce qui se
    prouve ici — le seam d'écriture est unique, mais « unique » est une lecture de
    code, et un avertissement voisin de cette famille ne sort que d'un seul côté."""
    from _datastore_rest import call, stub_authz

    stub_authz(monkeypatch, org_id=None)
    ns, ns_id = _table()
    code, corps = call("me.datastore.append_row", path_params={"namespace": ns},
                       body={"ref": "r1", "qualif": "PME"}, sub=SUB)
    assert code == 400 and corps["error"] == "row_invalid"
    assert "qualif" in corps["detail"] and '"comment": "…"' in corps["detail"]
    assert _lignes(ns_id) == []


def test_face_REST_le_PATCH_partiel_reste_ecrivable(live, monkeypatch):
    """La condition qui protège du gel : un patch sur une AUTRE colonne d'une ligne
    non conforme passe. Sans elle, on refermerait le gel silencieux d'oto-backend#284
    sous un nom neuf."""
    from _datastore_rest import call, stub_authz

    stub_authz(monkeypatch, org_id=None)
    ns, ns_id = _table()
    # une ligne posée AVANT la règle : valeur nue, sans couche
    from oto_mcp import db
    db.datastore_insert_row(ns_id, "r-vieille", {"ref": "r1", "qualif": "PME"})

    code, corps = call("me.datastore.update_row",
                       path_params={"namespace": ns, "row_id": "r-vieille"},
                       body={"libre": "note"}, sub=SUB)
    assert code == 200, corps
    assert _lignes(ns_id)[0]["libre"] == "note"

    # …et la MÊME ligne refuse dès que le geste réécrit la colonne gardée
    code, corps = call("me.datastore.update_row",
                       path_params={"namespace": ns, "row_id": "r-vieille"},
                       body={"qualif": "ETI"}, sub=SUB)
    assert code == 400 and corps["error"] == "row_invalid"
    assert "qualif" in corps["detail"]
