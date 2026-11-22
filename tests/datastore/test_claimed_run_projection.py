"""`_claimed_run` : la ligne servie dit POUR QUEL RUN elle est réservée.

Le bail servi disait à QUI (`_claimed_by`) et JUSQU'À QUAND (`_claimed_until`), jamais
POUR QUEL RUN — alors que `datastore_rows.claimed_run` le porte depuis le verrou natif.
Conséquence côté surveillance : on voyait qu'un agent tenait une ligne, jamais laquelle
tenait laquelle. Le serveur savait pourtant déjà répondre : la colonne est là. Elle
n'était simplement servie à personne — et ce que le run sait de lui-même ne dit rien à un
tiers qui regarde la file.

Ce que ces tests verrouillent :

1. **La couture servie**, sur TOUS les chemins qui rendent une ligne réservée
   (liste, fiche par id, curseur keyset, curseur trié, file de supervision, et le
   claim lui-même) : contre un vrai PostgreSQL, avec la réservation posée comme en
   production (middleware `_run_id=` + outil monté par `register_all`).
2. **Les trois états, pas deux** : `_claimed_run` = le run ; `null` = le bail a été
   pris SANS run (une personne sur la file du dashboard) ; ABSENT = pas de bail du
   tout. Un `null` fabriqué par un SELECT qui aurait oublié la colonne dirait « ce
   bail n'a pas de run » — un fait faux, pas un trou visible.
3. **La garde mécanique** qui empêche le point 2 de retomber : toute requête de
   `oto_mcp/db/` qui projette une ligne pour l'API et nomme `claimed_by` doit nommer
   `claimed_run`. `_row_to_dict` lit la colonne par CLÉ (pas `.get`) : un chemin qui
   l'oublie LÈVE au lieu de servir un faux « sans run ».

Depuis oto-backend#664, la libération ligne à ligne efface aussi `claimed_run`. La
projection, elle, ne parle du run que sous `claimed_by IS NOT NULL` : une colonne
restée garnie (par un chemin qui l'oublierait) ne sortirait pas, et le test
`test_une_ligne_liberee_ne_dit_plus_rien_du_run` le fige.
"""
from __future__ import annotations

import asyncio
import ast
import pathlib
import uuid

import pytest

SUB = "sub-claimed-run"
ORG = 664


# ── ① la garde mécanique : elle ne demande à personne de se souvenir ────────────

# La signature d'une requête qui construit une ligne pour `_row_to_dict` : ces quatre
# colonnes, dans cet ordre, sont son en-tête. On ne cherche pas « une requête qui parle
# de baux » (il y en a qui libèrent, qui comptent, qui sondent) mais « une requête qui
# RÉPOND une ligne ».
_ENTETE_LIGNE = "row_id, created_at, updated_at, data"


def _requetes_de_ligne() -> list[tuple[str, str]]:
    """(fichier, littéral SQL) des requêtes de `db/` qui projettent une ligne servie.

    Par l'AST, jamais par `grep` : un littéral SQL est écrit en plusieurs morceaux
    concaténés, et une recherche textuelle sur le fichier verrait des colonnes qui
    n'appartiennent pas à la même requête.
    """
    racine = pathlib.Path(__file__).resolve().parents[2] / "oto_mcp" / "db"
    trouvees = []
    for f in sorted(racine.glob("*.py")):
        arbre = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(arbre):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                sql = n.value
            elif isinstance(n, ast.BinOp) or isinstance(n, ast.JoinedStr):
                continue
            else:
                continue
            if _ENTETE_LIGNE in sql and "claimed_by" in sql:
                trouvees.append((f.name, sql))
    return trouvees


def test_toute_projection_de_ligne_porte_claimed_run():
    """Une requête qui rend une ligne AVEC son bail rend les trois champs du bail.

    Sans cette garde, ajouter un chemin de lecture de plus ferait servir
    `_claimed_run: null` sur des lignes qui ONT un run — un mensonge silencieux, la
    forme de défaut la plus coûteuse qu'on connaisse ici. Avec elle, l'oubli est
    rouge à l'écriture du SELECT.
    """
    requetes = _requetes_de_ligne()
    assert requetes, ("aucune requête de projection trouvée : la signature "
                      f"{_ENTETE_LIGNE!r} a changé, la garde ne garde plus rien")
    manquantes = [(f, sql) for f, sql in requetes if "claimed_run" not in sql]
    assert not manquantes, (
        "ces requêtes projettent une ligne avec `claimed_by` sans `claimed_run` — "
        "`_row_to_dict` lit la colonne par clé, elles lèveraient en production :\n"
        + "\n".join(f"  {f}: {sql[:120]}…" for f, sql in manquantes))


# Le PRÉDICAT de fraîcheur, mot pour mot. Il vit dans les requêtes et nulle part
# ailleurs : c'est PostgreSQL qui dit si un bail court encore, sur la même horloge que
# la garde `datastore_active_lease`. Refaire la comparaison en Python serait une
# SECONDE implémentation de la règle — et celle qu'on a retirée était fausse en germe :
# comparer les horodatages en TEXTE n'est juste que tant que le formateur de lignes émet
# un séparateur espace sans fuseau. Un chemin qui rendrait un `T` (0x54 > 0x20) aurait
# lu TOUT bail comme actif, en silence.
_FRAICHEUR = "(claimed_until IS NOT NULL AND claimed_until > NOW()) AS claim_active"


def _sans_espaces(sql: str) -> str:
    return " ".join(sql.split())


def test_toute_projection_de_ligne_dit_la_FRAICHEUR_du_bail():
    """Une requête qui rend une ligne AVEC son bail dit aussi s'il court ENCORE.

    Cinq copies du même prédicat valent une définition unique tant que ce cliquet les
    tient identiques : il les énumère déjà toutes (c'est le même relevé que pour
    `claimed_run`), donc l'exigence neuve ne coûte aucun dispositif de plus. Sans lui,
    un SELECT ajouté demain ferait lever `_row_to_dict` en production — ou pire, une
    copie du prédicat dériverait et deux lectures voisines se remettraient à répondre
    différemment sur la même ligne, ce qui est exactement le défaut fermé ici."""
    requetes = _requetes_de_ligne()
    assert requetes, "la signature a changé, la garde ne garde plus rien"
    manquantes = [(f, sql) for f, sql in requetes
                  if _FRAICHEUR not in _sans_espaces(sql)]
    assert not manquantes, (
        "ces requêtes projettent une ligne avec `claimed_by` sans dire si le bail "
        f"court encore ({_FRAICHEUR}) :\n"
        + "\n".join(f"  {f}: {sql[:120]}…" for f, sql in manquantes))


def test_aucune_comparaison_de_bail_ne_se_refait_en_Python():
    """La contrepartie, et c'est elle qui empêche le retour en arrière : le
    sérialiseur LIT un verdict, il n'en calcule pas un second."""
    import inspect

    from oto_mcp.datastore import core
    src = inspect.getsource(core.DatastorePg._row_to_dict)
    assert 'row["claim_active"]' in src, "le verdict doit venir de la requête"
    for interdit in ("_maintenant_iso", "datetime.now", "utcnow"):
        assert interdit not in src, (
            f"`{interdit}` de retour dans la projection : la fraîcheur d'un bail se "
            "tranche dans la requête, pas au fil d'une lecture d'horloge en Python")


def test_claimed_run_est_une_colonne_de_plateforme():
    """`_claimed_run` ne doit pas pouvoir être écrasé par une colonne utilisateur."""
    from oto_mcp.datastore.columns import _META_COLS
    assert "_claimed_run" in _META_COLS


@pytest.mark.parametrize("oubliee", ["claimed_run", "claim_active"])
def test_la_projection_leve_si_la_colonne_na_pas_ete_lue(oubliee):
    """Le refus est le comportement VOULU, il se prouve — et pour CHAQUE colonne du
    bail séparément.

    ⚠️ Écrit en un seul cas, ce test passait pour la mauvaise raison : il nommait
    `claimed_run` mais levait sur la première colonne manquante, quelle qu'elle soit.
    Une garde qui ne peut pas dire ce qu'elle a vraiment attrapé n'en est pas une.

    ⚠️ Et aucune date en dur ici : les horodatages ne sont plus comparés en Python,
    donc décaler l'horloge de la suite ne change pas cette couleur. Un test qui figeait
    un instant futur passait jusqu'à la veille du jour où il devenait faux."""
    from oto_mcp.datastore.core import DatastorePg
    complete = {"row_id": "r1", "created_at": "c", "updated_at": "u", "data": {},
                "claimed_by": "w", "claimed_until": "peu importe",
                "claimed_run": None, "claim_active": True}
    DatastorePg._row_to_dict(dict(complete))          # témoin : complète, ça passe
    amputee = {k: v for k, v in complete.items() if k != oubliee}
    with pytest.raises(KeyError, match=oubliee):
        DatastorePg._row_to_dict(amputee)


def test_le_champ_est_declare_au_contrat_servi():
    """Servi n'est pas déclaré : un client typé ne voit que ce que l'OpenAPI nomme."""
    from oto_mcp.capabilities.datastore.rows import Row
    champ = Row.model_fields["claimed_run"]
    assert champ.serialization_alias == "_claimed_run"
    assert champ.description and "null" in champ.description, \
        "le contrat doit dire ce que `null` veut dire, sinon il sera lu « aucun run »"


# ── ② la couture servie, contre un vrai PostgreSQL ─────────────────────────────


@pytest.fixture
def surface(live, monkeypatch):
    """Les outils `data_*` tels que le serveur les monte, l'acteur tenu."""
    from oto_mcp.datastore.core import make_store
    from oto_mcp.tools import datastore as T
    monkeypatch.setattr(T, "_acting_store", lambda: make_store(SUB))
    monkeypatch.setattr(T, "_ns", lambda ns: ns)
    monkeypatch.setattr(T, "_project_hint", lambda ns: None)


_OUTILS: dict = {}


def _outil(nom: str):
    """Ce que charge le BOOT (`register_all`), pas un module seul."""
    if nom not in _OUTILS:
        from fastmcp import FastMCP

        from oto_mcp.tools import register_all
        m = FastMCP("t-claimed-run")
        register_all(m)
        _OUTILS[nom] = asyncio.run(m.get_tool(nom))
    return _OUTILS[nom]


def _run() -> str:
    from oto_mcp import db
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=SUB, org_id=ORG, label="surveillance")
    return run_id


def _table(n: int = 1):
    from oto_mcp import db
    ns = "file-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", SUB, ns)
    ns = str(ns_id)
    for i in range(n):
        db.datastore_insert_row(ns_id, f"r{i}", {"siren": f"5511100{i}",
                                                 "statut": "a_enrichir"})
    return ns, ns_id


def _claim(ns: str, run: str | None) -> dict:
    """La réservation comme elle arrive en production : `_run_id=` lu des arguments
    BRUTS par le middleware, posé, retiré, puis l'outil dispatché."""
    from oto_mcp.middleware.call_context import CallContextMiddleware

    outil = _outil("data_claim_next")

    class _Msg:
        pass

    class _Ctx:                     # comme le vrai MiddlewareContext : PAS de get_state
        pass

    msg = _Msg()
    msg.name = "data_claim_next"
    msg.arguments = {"datastore": ns, "worker": "poste-1",
                     "filter": {"statut": "a_enrichir"}, "lease_s": 600}
    if run is not None:
        msg.arguments["_run_id"] = run
    ctx = _Ctx()
    ctx.message = msg

    async def _next(c):
        return await outil.run(c.message.arguments)

    async def _go():
        return await CallContextMiddleware().on_call_tool(ctx, _next)

    return asyncio.run(_go()).structured_content


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store(SUB)


def test_tous_les_chemins_servis_disent_le_run(surface):
    """Le claim, la liste, la fiche, les deux curseurs et la file de supervision : la
    même ligne, le même run. Un seul chemin qui l'oublierait suffirait à rendre la
    vue incohérente selon l'écran ouvert."""
    ns, ns_id = _table(1)
    run = _run()

    pris = _claim(ns, run)["row"]
    assert pris["_claimed_run"] == run, "le claim rend déjà l'adresse du travail"
    rid = pris["_id"]

    store = _store()
    lignes = {
        # `list_rows` → `datastore_list_rows` ; `cursor_rows` sans `order_by` →
        # `datastore_list_rows_after` (keyset) ; avec `order_by` → la voie triée.
        # Les trois SELECT sont distincts : les couvrir tous les trois est le sujet.
        "liste": next(r for r in store.list_rows(ns) if r["_id"] == rid),
        "par_id": store.get_row(ns, rid),
        "curseur": next(r for r in store.cursor_rows(ns, limit=10)["rows"]
                        if r["_id"] == rid),
        "curseur_trie": next(r for r in store.cursor_rows(
            ns, limit=10, order_by="_created_at")["rows"] if r["_id"] == rid),
        "file": next(r for r in store.queue(ns) if r["_id"] == rid),
    }
    for nom, ligne in lignes.items():
        assert ligne["_claimed_run"] == run, f"{nom} ne dit pas quel run tient la ligne"
        assert ligne["_claimed_by"] == "poste-1", f"{nom} : le bail est incomplet"


def test_un_bail_sans_run_vaut_null_et_pas_une_absence(surface):
    """Réserver sans jeton de run est un cas NORMAL (la file pilotée par une
    personne). La réponse doit le dire : la clé est là, sa valeur est `null` — « ce
    bail n'appartient à aucun run », pas « je ne sais pas ».

    Par le STORE, comme la face REST de la file : le tool d'agent, lui, refuse hors
    run (#727, `test_claim_sans_run_refuse_727.py`)."""
    ns, _ = _table(1)
    pris = _store().claim_next(ns, worker="poste-1", lease_s=600)
    assert pris["_claimed_by"] == "poste-1", "le bail est bien posé"
    assert "_claimed_run" in pris, "la clé manquante se lirait « pas de bail »"
    assert pris["_claimed_run"] is None


def test_une_ligne_libre_ne_porte_aucune_des_trois_cles(surface):
    """Absent, pas nul : sur les millions de lignes jamais réservées, trois `null`
    par ligne seraient du bruit dans toutes les lectures."""
    ns, _ = _table(1)
    ligne = _store().list_rows(ns)[0]
    for cle in ("_claimed_by", "_claimed_until", "_claimed_run"):
        assert cle not in ligne


def test_une_ligne_liberee_ne_dit_plus_rien_du_run(surface):
    """La projection se tait dès que le bail tombe : le run d'un bail rendu ne sort
    pas (la colonne elle-même est effacée depuis oto-backend#664)."""
    ns, ns_id = _table(1)
    run = _run()
    rid = _claim(ns, run)["row"]["_id"]

    from oto_mcp import db
    assert db.datastore_release_claim(ns_id, rid, "poste-1") is True
    ligne = _store().get_row(ns, rid)
    assert "_claimed_run" not in ligne, \
        "servir le run d'un bail mort ferait croire qu'un travail tient la ligne"


def test_rendre_les_lignes_du_run_efface_l_adresse(surface):
    """La contrepartie à écrire noir sur blanc : `_claimed_run` répond « sur quelle
    ligne ce run est-il MAINTENANT », jamais « quelle ligne ce run a-t-il
    travaillée ». Un run conclu n'a plus d'adresse."""
    ns, _ = _table(1)
    run = _run()
    rid = _claim(ns, run)["row"]["_id"]

    from oto_mcp import db
    assert db.datastore_release_by_run(run) == 1
    assert "_claimed_run" not in _store().get_row(ns, rid)
