"""Le feed LinkedIn tient dans un résultat d'outil — signal d'usage #384.

`linkedin_post(op="feed", limit=40)` rendait **67 383 caractères**, au-delà du
plafond d'un résultat MCP : observé en conditions réelles sur la procédure
`veille-linkedin`, le harnais a déversé la sortie dans un fichier et l'agent a dû
repasser au `jq` pour la ramener à 42 Ko — deux tours et un détour par le shell avant
de commencer le vrai travail. Un client MCP nu (agent n8n, pas de shell) n'a lui aucun
recours : il cale sur l'appel.

Ce que ce fichier verrouille, c'est le **défaut** — pas la présence d'un paramètre
optionnel de plus (ADR 0047 §Amendement du 11/08 : *le chemin paresseux doit être le
chemin juste*). Le signal jumeau #281 avait ajouté `fields`/`text_max_chars` à
`op="posts"` sans toucher au défaut : le payload est resté lourd, et le même incident
s'est rejoué ici. D'où, en regard de chaque allègement, le test que **rien n'est caché**
(chemin brut intact, colonnes écartées nommées dans la réponse).

Les tailles ci-dessous sont calibrées sur 40 lignes RÉELLES du miroir (texte : médiane
730, moyenne 971, max 2 712 caractères ; ligne brute ~1 650 caractères).
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

# Longueurs de texte reproduisant la distribution mesurée sur 40 posts réels
# (somme ~38 800 caractères) : quelques posts fleuve, une majorité de moyens, du bruit.
_TEXT_LENGTHS = [
    2712, 2410, 2088, 1904, 1743, 1602, 1488, 1371, 1266, 1180,
    1104, 1032, 966, 918, 872, 830, 792, 760, 744, 736,
    729, 722, 700, 664, 620, 574, 520, 470, 418, 372,
    320, 274, 228, 186, 140, 96, 60, 32, 12, 1,
]


def _row(i: int) -> dict:
    """Une ligne du miroir `linkedin-feed`, colonnes et formes réelles."""
    urn = f"urn:li:activity:74929182333737{90000 + i}"
    return {
        "_id": urn,
        "_created_at": "2026-08-11 12:28:37",
        "_updated_at": "2026-08-11 12:28:39",
        "urn": urn,
        "text": "x" * _TEXT_LENGTHS[i],
        "post_url": f"https://www.linkedin.com/feed/update/{urn}",
        "is_repost": False,
        # décroissant avec `i` : le feed re-trie par date, l'ordre de la fixture est
        # donc celui du résultat (item[0] = le plus récent = le plus long ici).
        "posted_at": f"2026-08-11T12:{39 - i:02d}:58.525000+00:00",
        "author_name": f"Auteur Numéro {i}",
        "feed_reason": "Suggéré pour toi" if i % 3 else None,
        "surfaced_by": None,
        "comments_count": i,
        "author_headline": "Fondateur & CEO | On parle IA appliquée, agents et ops",
        "comment_authors": [],
        "posted_relative": "6m •   ",
        "reactions_count": i * 3,
        "original_author_name": None,
    }


ROWS = [_row(i) for i in range(40)]


def _tool():
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    m = FastMCP("t")
    U.register(m)
    return asyncio.run(m.get_tool("linkedin_post")).fn


@pytest.fixture
def feed(monkeypatch):
    """Le miroir, servi depuis le datastore — aucun appel LinkedIn (feed frais)."""
    from oto_mcp.tools import unipile as U

    store = MagicMock()
    store.list_rows.return_value = list(ROWS)
    monkeypatch.setattr("oto_mcp.datastore.core.make_store", lambda sub: store)
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: MagicMock())
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr(U, "_feed_is_stale", lambda sub, provider="LINKEDIN": False)
    return _tool()


def _chars(payload) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _raw_page_chars() -> int:
    return _chars({"items": ROWS, "total": len(ROWS), "page": 0,
                   "limit": 40, "synced": False})


# --- le défaut, c'est-à-dire le sujet du signal --------------------------------

def test_le_defaut_tient_dans_un_resultat_doutil(feed):
    """LE test du signal : `limit=40` SANS paramètre supplémentaire.

    Le budget est exprimé en part de la page brute, pas en valeur absolue : ce qui
    compte est le coût PAR POST (la page grandit linéairement avec `limit`). Un défaut
    qui rendrait le brut — même flanqué de `fields` et `text_max_chars` optionnels —
    échoue ici, et c'est voulu : c'est exactement ce qui a été livré pour #281.

    Le seuil discrimine aussi la demi-mesure : sur cette fixture, **tronquer le texte
    seul** rend 0,76 de la page brute (le texte ne pèse que 60 %, le reste est de la
    redondance d'identifiants et de la comptabilité de miroir) et échoue donc ici ;
    troncature + vue de tri rend 0,66. Sur les 40 lignes réelles : 65 899 → 40 765
    caractères, soit 1 647 → 1 019 par post.
    """
    raw = _raw_page_chars()
    assert raw > 55_000, (
        "fixture non représentative : la page brute doit être énorme (la vraie, "
        "mesurée sur 40 lignes du miroir d'un compte réel, pèse 65 899 caractères)")

    out = feed(op="feed", limit=40)

    assert _chars(out) < 0.70 * raw, (
        f"la page par défaut pèse {_chars(out)} caractères pour {raw} en brut — "
        "un défaut qui ne coupe pas laisse l'agent faire le tri au shell (#384)")
    assert len(out["items"]) == 40, "alléger la page ne doit pas rendre moins de posts"


def test_le_texte_est_un_extrait_et_la_coupe_est_marquee(feed):
    out = feed(op="feed", limit=40)
    long_post, short_post = out["items"][0], out["items"][-1]

    assert len(long_post["text"]) == 601 and long_post["text"].endswith("…")
    assert long_post["text_truncated"] is True, (
        "une coupe non marquée ferait croire à l'agent qu'il a lu le post entier")
    assert "text_truncated" not in short_post, "un texte court n'est pas marqué coupé"
    assert len(short_post["text"]) == 1, "un texte déjà court n'est pas touché"


def test_le_defaut_garde_de_quoi_trier_et_agir(feed):
    """La vue par défaut doit porter tout ce dont la doctrine `veille-linkedin` se
    sert : classer (auteur, headline, date, texte, traction) et restituer (`post_url`),
    dédupliquer et rouvrir (`urn`)."""
    it = feed(op="feed", limit=40)["items"][0]
    for col in ("urn", "post_url", "author_name", "author_headline", "posted_at",
                "text", "reactions_count", "comments_count"):
        assert col in it, f"`{col}` sert au tri du feed : il ne peut pas sauter"


def test_le_defaut_ecarte_la_comptabilite_du_miroir(feed):
    """Les dates du MIROIR et le temps relatif figé au sync ne décrivent pas le post."""
    it = feed(op="feed", limit=40)["items"][0]
    for col in ("_created_at", "_updated_at", "posted_relative"):
        assert col not in it


def test_ce_qui_est_ecarte_est_nomme_dans_la_reponse(feed):
    """Un défaut qui résume doit DIRE ce qu'il a rogné, sinon il cache."""
    out = feed(op="feed", limit=40)
    proj = out["projection"]
    assert set(proj["omitted_fields"]) == {
        "_id",  # même chaîne que `urn` par construction — cf. _FEED_ADDRESSING
        "_created_at", "_updated_at", "posted_relative", "surfaced_by",
        "comment_authors"}
    assert proj["text_max_chars"] == 600
    assert "fields=['*']" in proj["hint"] and "text_max_chars=None" in proj["hint"]


# --- le chemin vers le brut : on ne retire rien du catalogue -------------------

def test_le_brut_reste_atteignable_a_loctet_pres(feed):
    """`fields=["*"]` + `text_max_chars=None` = les lignes du miroir, INTACTES."""
    out = feed(op="feed", limit=40, fields=["*"], text_max_chars=None)
    assert out["items"] == ROWS
    assert "projection" not in out, (
        "rien n'a été rogné : pas d'avertissement à poser")


def test_toutes_les_colonnes_avec_le_texte_en_extrait(feed):
    out = feed(op="feed", limit=40, fields=["*"])
    assert set(out["items"][0]) >= set(ROWS[0]), "aucune colonne perdue"
    assert out["items"][0]["text_truncated"] is True


# --- `fields` : la sémantique de `data_rows`, apprise une seule fois -----------

def test_fields_projette_comme_data_rows(feed):
    out = feed(op="feed", limit=40, fields=["author_name"], text_max_chars=None)
    it = out["items"][0]
    assert set(it) == {"author_name", "urn"}, (
        "comme `data_rows`, la projection garde toujours de quoi ADRESSER la ligne — "
        "l'`urn` SEUL, puisqu'il EST l'id de la ligne (le sync écrit "
        "`upsert_row(_FEED_NS, urn, item)`) : rendre `_id` en plus serait la même "
        "chaîne deux fois")


def test_une_colonne_inconnue_est_signalee_sans_bloquer(feed):
    """Même piège silencieux que la projection de `data_rows` : une faute de frappe
    rend une colonne vide sans rien dire."""
    out = feed(op="feed", limit=40, fields=["auteur_name"])
    assert "auteur_name" in out["warning"]
    assert len(out["items"]) == 40, "on signale, on ne bloque pas"


def test_fields_vide_est_refuse(feed):
    """Demande ambiguë : la traiter comme « pas de projection » rendrait silencieusement
    PLUS que le défaut — l'inverse de ce que l'appelant demandait."""
    from mcp.shared.exceptions import McpError

    with pytest.raises(McpError, match="liste vide"):
        feed(op="feed", limit=40, fields=[])


def test_text_max_chars_zero_est_refuse(feed):
    """`0` est faux en Python donc « aucune limite » : exactement l'inverse de ce que
    demande qui l'écrit."""
    from mcp.shared.exceptions import McpError

    with pytest.raises(McpError, match="text_max_chars"):
        feed(op="feed", limit=40, text_max_chars=0)


def test_lenveloppe_de_pagination_survit(feed):
    out = feed(op="feed", limit=10, page=1)
    assert out["total"] == 40 and out["page"] == 1 and out["limit"] == 10
    assert len(out["items"]) == 10


# --- l'autre bout du même seam (`_slim`) --------------------------------------

def test_les_posts_dun_membre_ont_le_meme_defaut(monkeypatch):
    """#281 avait ajouté les paramètres à `op="posts"` sans corriger son défaut : le
    même incident s'est rejoué sur le feed. Un seul extrait par défaut pour toute la
    famille — l'agent l'apprend une fois."""
    from fastmcp import FastMCP
    from oto_mcp.tools import unipile as U

    client = MagicMock()
    client.list_member_posts.return_value = {
        "items": [{"id": "p1", "social_id": "urn:li:activity:1", "text": "x" * 5000}],
        "cursor": None,
    }
    monkeypatch.setattr(U, "unipile_client", lambda *a, **k: client)
    monkeypatch.setattr("oto_mcp.access.current_user_sub_or_raise", lambda: "sub-1")
    monkeypatch.setattr(U, "_rate_limit_guard", lambda sub: None)

    m = FastMCP("t")
    U.register(m)
    profile = asyncio.run(m.get_tool("linkedin_profile")).fn

    it = profile(op="posts", identifier="marie-dupont")["items"][0]
    assert len(it["text"]) == 601 and it["text_truncated"] is True

    whole = profile(op="posts", identifier="marie-dupont",
                    text_max_chars=None)["items"][0]
    assert len(whole["text"]) == 5000, "le texte entier reste à un paramètre"


# --- le texte de l'ORIGINAL d'un repost est borné lui aussi -------------------

def _shape(lignes, fields=None, text_max_chars=600):
    from oto_mcp.tools import unipile as U
    return U._shape_feed(
        {"items": [dict(r) for r in lignes], "total": len(lignes), "page": 0,
         "limit": len(lignes)}, fields, text_max_chars)


def test_le_texte_de_loriginal_est_borne_comme_le_texte():
    """Depuis oto-core v1.80.0 un repost porte `original_text` — le propos RÉEL, quand
    `text` ne contient que le mot du re-partageur (souvent « 👏 »). Ne borner que
    `text` laisserait celui-là passer entier, et annulerait le plafond sur précisément
    les posts où il y a le plus à lire."""
    ligne = dict(ROWS[0], urn="urn:li:activity:repost", is_repost=True,
                 text="👏", original_text="z" * 3000)
    it = _shape([ligne])["items"][0]
    assert len(it["original_text"]) == 601 and it["original_text"].endswith("…")
    assert it["original_text_truncated"] is True, "la coupe doit être marquée"


def test_un_original_court_nest_pas_marque_tronque():
    ligne = dict(ROWS[0], urn="urn:li:activity:repost2", is_repost=True,
                 text="👏", original_text="court")
    it = _shape([ligne])["items"][0]
    assert it["original_text"] == "court"
    assert "original_text_truncated" not in it


def test_le_defaut_dit_de_quoi_le_post_est_fait():
    """Sans `content_type`, un post dont tout le propos est dans l'image (texte
    « 🧐 », 2 775 réactions) est INCLASSABLE — c'est le manque qui a motivé
    oto-core v1.80.0."""
    ligne = dict(ROWS[0], urn="urn:li:activity:image", text="🧐",
                 content_type="image", content_title="Schéma d'architecture")
    it = _shape([ligne])["items"][0]
    assert it["content_type"] == "image"
    assert it["content_title"] == "Schéma d'architecture"
