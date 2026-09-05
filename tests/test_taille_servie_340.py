"""La taille de ce qui est SERVI, mesurée de bout en bout (oto-backend#340).

Ce que le journal disait, et ce qu'il ne disait pas : `duration_ms` mesure ce qu'un
appel coûte au SERVEUR. Rien ne mesurait ce qu'il coûte à la **fenêtre de l'agent** —
donc rien ne permettait de classer les outils bavards, ni de voir une dérive. Et ce
qu'on ne mesure pas ne produit aucun signal : l'absence de plainte n'a jamais prouvé
l'absence de coût.

⚠️ **Le banc qui compte est celui du CHEMIN** — `test_un_appel_REEL_…` monte un vrai
serveur FastMCP et appelle l'outil par un client. Les bancs voisins du middleware
(`test_calllog_*.py`) stubbent `call_next` avec `return "ok"` : ils INVENTENT la forme
du résultat, donc ils ne prouvent rien sur celle que fastmcp fait réellement passer.
Une mesure branchée sur une forme supposée rend `None` partout, sans casser un seul
test — et le tableau de bord afficherait des colonnes vides qu'on lirait « ces outils
ne servent rien ».

Le chemin complet a quatre relais, et chacun peut avaler la mesure en silence : le
middleware la calcule, le sink l'écrit, la colonne l'accueille, l'agrégat la rend.
Les tests ci-dessous les couvrent dans cet ordre.
"""
from __future__ import annotations

import asyncio
import os
import types
import uuid

import pytest

from oto_mcp import calllog


async def _drain():
    while calllog._PENDING:
        await asyncio.gather(*list(calllog._PENDING), return_exceptions=True)


def _serveur(rows):
    from fastmcp import FastMCP

    mcp = FastMCP("t340")

    @mcp.tool
    def bavard(n: int) -> str:
        return "x" * n

    @mcp.tool
    def rend_dict() -> dict:
        return {"items": [{"k": i, "v": "y" * 20} for i in range(5)]}

    @mcp.tool
    def rend_vide() -> str:
        return ""

    @mcp.tool
    def casse() -> str:
        raise RuntimeError("boom")

    async def sink(row):
        rows.append(row)

    mcp.add_middleware(calllog.ToolCallLogger(sink, server="oto",
                                              identity=lambda: {"sub": "u1"}))
    return mcp


def _ligne(rows, outil):
    trouvees = [r for r in rows if r["tool"] == outil]
    assert len(trouvees) == 1, f"{outil} : {len(trouvees)} lignes"
    return trouvees[0]


# ── 1. le chemin : un appel réel, de bout en bout ────────────────────────────

@pytest.mark.asyncio
async def test_un_appel_REEL_ecrit_la_taille_du_texte_servi():
    """⚠️ Le seul test qui prouve que la mesure est branchée sur la forme que fastmcp
    fait passer, et pas sur celle qu'on a supposée en lisant le code."""
    from fastmcp import Client

    rows: list = []
    async with Client(_serveur(rows)) as c:
        await c.call_tool("bavard", {"n": 1234})
    await _drain()

    assert _ligne(rows, "bavard")["result_size"] == 1234


@pytest.mark.asyncio
async def test_la_mesure_egale_ce_que_le_CLIENT_a_recu():
    """La forme la plus courante chez oto — un outil qui rend un `dict`. On ne compare
    pas à un `json.dumps` de notre cru (il donne un autre nombre : fastmcp sérialise
    avec ses propres séparateurs), mais à ce que le client tient en main."""
    from fastmcp import Client

    rows: list = []
    async with Client(_serveur(rows)) as c:
        recu = await c.call_tool("rend_dict", {})
    await _drain()

    servi = sum(len(b.text) for b in recu.content if getattr(b, "text", None))
    assert servi > 0
    assert _ligne(rows, "rend_dict")["result_size"] == servi


@pytest.mark.asyncio
async def test_zero_et_None_ne_disent_PAS_la_meme_chose():
    """⚠️ La distinction qui fait toute la valeur de la colonne : `0` = une réponse
    réellement vide (un fait sur l'outil), `NULL` = pas de mesure (un fait sur le
    journal). Les confondre ferait passer un outil en échec pour un outil gratuit —
    l'inverse exact de ce qu'on cherche."""
    from fastmcp import Client

    rows: list = []
    async with Client(_serveur(rows)) as c:
        await c.call_tool("rend_vide", {})
        with pytest.raises(Exception):
            await c.call_tool("casse", {})
    await _drain()

    assert _ligne(rows, "rend_vide")["result_size"] == 0
    # ⚠️ `.get()`, pas `[...]` : sur un échec le middleware ne pose PAS la clé — et
    # `insert_tool_call` lit la ligne au `.get()`, donc clé absente et clé à None
    # écrivent la même chose, NULL. Ce que ce banc garde est la colonne finale, pas
    # la forme du dict intermédiaire (le test live ci-dessous relit la base).
    assert _ligne(rows, "casse").get("result_size") is None, (
        "un appel en échec n'a rien servi de mesurable : NULL, pas 0")


@pytest.mark.asyncio
async def test_le_handshake_reste_hors_mesure():
    """`initialize` (`kind='protocol'`) ne sert pas de résultat d'outil. Il ne doit pas
    entrer dans le compte, sinon la moyenne par outil serait diluée par le protocole."""
    from fastmcp import Client

    rows: list = []
    async with Client(_serveur(rows)) as c:
        await c.call_tool("bavard", {"n": 3})
    await _drain()

    assert _ligne(rows, "initialize").get("result_size") is None


# ── 2. la fonction : ce qu'elle refuse de deviner ────────────────────────────

@pytest.mark.parametrize("objet,attendu", [
    (types.SimpleNamespace(content=[types.SimpleNamespace(text="abc"),
                                    types.SimpleNamespace(text="de")]), 5),
    (types.SimpleNamespace(content=[]), 0),
    (types.SimpleNamespace(content=[types.SimpleNamespace(text=None)]), 0),
    (types.SimpleNamespace(), None),          # pas de `content` du tout
    ("une chaîne nue", None),                 # la forme des bancs stubbés
    (None, None),
])
def test_les_formes_que_la_mesure_sait_et_ne_sait_pas_lire(objet, attendu):
    """Un bloc sans texte (une image, par exemple) compte 0 caractère de TEXTE : c'est
    ce que la colonne prétend mesurer, et rien d'autre. Une forme sans `content` rend
    `None` — la mesure se tait plutôt que d'inventer un zéro."""
    assert calllog.taille_servie(objet) == attendu


def test_la_mesure_ne_casse_JAMAIS_l_appel_qu_elle_observe():
    """Elle vit sur le chemin de chaque appel : une mesure qui lève ferait tomber un
    outil qui a réussi. Un `content` qui explose à l'itération doit rendre `None`."""
    class Piege:
        @property
        def content(self):
            raise RuntimeError("forme hostile")

    assert calllog.taille_servie(Piege()) is None


# ── 3. la base : la colonne accueille, et l'agrégat rend ─────────────────────

@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_taille_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom

    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _pose(tool, size, *, ok=True, kind="mcp"):
    from oto_mcp import db
    db.insert_tool_call({"tool": tool, "sub": "u-340", "kind": kind, "ok": ok,
                         "duration_ms": 10, "result_size": size})


def test_la_mesure_traverse_le_sink_et_ressort_de_la_base(live):
    """Le relais qu'on oublie : le middleware peut calculer juste et le sink ne pas
    porter la valeur — la colonne resterait NULL sans qu'aucun test ne bouge."""
    from oto_mcp.db import _connect

    _pose("t_traverse", 4242)
    with _connect() as conn:
        vu = conn.execute("SELECT result_size FROM tool_calls WHERE tool = %s",
                          ("t_traverse",)).fetchone()
    assert vu["result_size"] == 4242


def test_l_agregat_CLASSE_les_outils_par_ce_qu_ils_coutent(live):
    """Le chiffre qui répond à la question de l'issue : un outil appelé souvent pour
    peu pèse plus qu'un outil appelé deux fois pour beaucoup. C'est `total_chars` qui
    le dit — pas la moyenne."""
    from oto_mcp import db

    for _ in range(50):
        _pose("t_frequent", 2_000)
    for _ in range(2):
        _pose("t_enorme", 20_000)

    par_outil = {r["tool_name"]: r for r in db.tool_call_stats(since_days=1)["by_tool"]}
    frequent, enorme = par_outil["t_frequent"], par_outil["t_enorme"]

    assert frequent["avg_chars"] < enorme["avg_chars"], "la moyenne dit l'inverse…"
    assert frequent["total_chars"] > enorme["total_chars"], "…et le total dit le vrai"
    assert frequent["total_chars"] == 100_000


def test_sized_dit_sur_combien_d_appels_la_moyenne_PORTE(live):
    """⚠️ L'étiquette sans laquelle la mesure se lit faux. Les lignes antérieures à la
    colonne (et les appels en échec) portent NULL : la moyenne les ignore, donc elle
    est exacte sur un échantillon partiel. Sans `sized`, rien ne le dit — et on
    conclurait sur une période qu'on n'a pas mesurée."""
    from oto_mcp import db

    for _ in range(3):
        _pose("t_partiel", 100)
    for _ in range(7):
        _pose("t_partiel", None)          # l'historique d'avant la colonne

    par_outil = {r["tool_name"]: r for r in db.tool_call_stats(since_days=1)["by_tool"]}
    ligne = par_outil["t_partiel"]
    assert ligne["calls"] == 10
    assert ligne["sized"] == 3, "seules les lignes mesurées entrent dans la moyenne"
    assert ligne["avg_chars"] == 100
    assert ligne["total_chars"] == 300, "un total qui sous-déclare, et qui le DIT"


def test_le_total_de_plateforme_porte_son_denominateur(live):
    """Un volume servi sans son dénominateur invite à le diviser par `total_calls`,
    qui compte aussi les appels non mesurés."""
    from oto_mcp import db

    _pose("t_denominateur", 500)
    _pose("t_denominateur", None)
    stats = db.tool_call_stats(since_days=1)
    assert stats["served_chars"] > 0
    assert 0 < stats["sized_calls"] < stats["total_calls"], (
        "le journal porte des lignes non mesurées : le dénominateur doit s'en "
        "distinguer, sinon il ne sert à rien")


def test_les_gestes_REST_n_entrent_pas_dans_le_compte(live):
    """La lentille d'outils filtre `kind='mcp'`. Le middleware REST ne pose pas la
    taille — l'exposer côté routes afficherait une colonne toujours vide, qu'on
    lirait « ces routes ne servent rien » plutôt que « on ne les mesure pas »."""
    from oto_mcp import db

    avant = db.tool_call_stats(since_days=1)["served_chars"]
    _pose("PATCH /api/x", 999_999, kind="rest")
    assert db.tool_call_stats(since_days=1)["served_chars"] == avant


def test_l_ALTER_rattrape_une_table_qui_existe_DEJA(live):
    """⚠️ La table `tool_calls` vit en production depuis longtemps : son
    `CREATE TABLE IF NOT EXISTS` ne s'exécute plus, donc une colonne ajoutée au DDL
    n'arriverait jamais en base. Seul l'`ALTER` la rattrape.

    Mesuré plutôt qu'affirmé : on retire la colonne, on rejoue `init_db()`, elle doit
    revenir. Sans l'`ALTER`, ce test tombe — et c'est exactement le scénario du
    déploiement."""
    from oto_mcp.db import _connect, init_db

    with _connect() as conn:
        conn.execute("ALTER TABLE tool_calls DROP COLUMN result_size")
    init_db()
    with _connect() as conn:
        colonnes = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'tool_calls'").fetchall()}
    assert "result_size" in colonnes

    # …et elle accueille, pas seulement elle existe : une colonne rattrapée avec un
    # type inattendu passerait la vérification ci-dessus et refuserait l'écriture.
    _pose("t_apres_alter", 77)
    with _connect() as conn:
        vu = conn.execute("SELECT result_size FROM tool_calls WHERE tool = %s",
                          ("t_apres_alter",)).fetchone()
    assert vu["result_size"] == 77
