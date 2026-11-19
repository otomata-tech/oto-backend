"""Lot M1 (blueprint ADR 0054/0063) — la table `nodes`, et les guides convertis dedans.

Ce que ces tests gardent n'est pas la mécanique SQL (elle a été exercée contre un
vrai PostgreSQL 17 : boot complet, conversion, rejeu, newer-wins), mais les
**décisions** que rien dans le code ne rappellerait à celui qui les défera par
mégarde :

1. la forme de la table est MESURÉE (banc M0, forme B) — l'élargir sans mesure est
   ce que 0063-D3 interdit nommément ;
2. **deux** index de requête, pas trois — et surtout aucun index de RECHERCHE :
   sur un vivier ils pèsent 99 % du temps d'écriture, leur sort se décide en M5 ;
3. la conversion suit TOUTE écriture de `guides` du même boot (l'ordre est le seul
   risque du lot, comme le seed-avant-colonne de L1) ;
4. elle est rejouable : gardée `to_regclass`, arbitrée `ON CONFLICT`, newer-wins ;
5. le concept de « guide » a disparu — une couche de contexte est une PAGE
   (0055-D4), et la livraison est une propriété ;
6. M1 livre la table et les guides, RIEN d'autre : personne d'autre que la façade
   ne touche encore `nodes`.
"""
from __future__ import annotations

import pathlib
import re

from oto_mcp.db import _schema

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DB = _ROOT / "oto_mcp" / "db"
# Le DDL n'est plus un fichier mais un ASSEMBLAGE (`db/schema/<domaine>.py`
# concaténés dans un ordre figé) : on lit la chaîne SERVIE, seule chose dont les
# ordres et les formes ci-dessous soient des propriétés.
_SCHEMA_SRC = _schema._SCHEMA
_INIT_SRC = (_DB / "_init.py").read_text(encoding="utf-8")
_GUIDES_SRC = (_DB / "guides.py").read_text(encoding="utf-8")


def _nodes_block() -> str:
    m = re.search(r"CREATE TABLE IF NOT EXISTS nodes \((.*?)\n\);", _SCHEMA_SRC, re.S)
    assert m, "le bloc `CREATE TABLE … nodes` a disparu de _schema.py"
    return m.group(1)


def test_nodes_carries_the_measured_shape():
    """La forme du banc, colonne pour colonne. Un ajout « au cas où » se paie cent
    mille fois sur un vivier (0063-D3 garde-fou 1) : il doit passer par une mesure,
    donc par un échec de ce test plutôt que par une revue distraite.

    **Élargie le 2026-09-01, et le garde-fou a fonctionné** : il a fait échouer
    l'ajout, qui est donc passé par un banc — 200 000 lignes-tableau de six champs
    métier, deux passes en ordre inversé. `data` sépare la donnée utilisateur des
    propriétés qu'oto interprète ; les trois colonnes de bail complètent les deux
    qui étaient là, pour que la file de travail reste UNE mécanique. Coût mesuré :
    **+4,7 % de volume** (46,5 Mo contre 44,4), et **14 % de moins en temps
    d'écriture** — séparer évite la concaténation jsonb qu'imposait le mélange.
    """
    body = _nodes_block()
    cols = {m.group(1) for m in re.finditer(r"^\s{4}([a-z_]+) ", body, re.M)}
    assert cols == {"id", "public_id", "parent_id", "position", "kind", "owner_type",
                    "owner_id", "props", "data", "claimed_by", "claimed_until",
                    "claimed_run", "claims", "abandon_reason",
                    "created_at", "updated_at"}, sorted(cols)


def test_owner_id_is_text_because_a_user_owner_is_a_sub():
    """Écart ASSUMÉ avec la forme mesurée (`owner_id BIGINT`) : un propriétaire de
    scope `user` est un sub Logto — `users.sub` EST la clé primaire, il n'existe
    aucun id numérique d'utilisateur — et `platform` n'a pas d'id du tout. Le
    remettre en BIGINT n'est pas un ajustement de type : c'est inventer un surrogate
    par utilisateur, donc une migration d'identité."""
    assert re.search(r"^\s{4}owner_id TEXT NOT NULL", _nodes_block(), re.M)


def test_exactly_two_query_indexes_in_the_schema():
    """L'arbre et le propriétaire (0063-D3 garde-fou 2) — **toujours deux, et c'est
    le nombre qui est gardé ici**, pas leur forme.

    ⚠️ L'ownership est PARTIEL depuis le lot M4 (#308) : `WHERE kind <> 'ligne'`. Ce
    n'est pas un assouplissement du garde-fou mais son application — 0054-D4 dit
    qu'une ligne n'a pas de propriétaire propre, donc « que possède cet acteur ? » ne
    se demande jamais d'une ligne, et l'index nu répondait 43 584 fois à une question
    posée quelques milliers de fois (mesuré : 16 kB contre 312).

    L'autre index de M-f, celui du **prédicat du bail**, n'est toujours pas là — et
    pas par oubli : `NOW()` n'étant pas IMMUTABLE, la forme partielle que prescrit le
    banc M0 est illégale, et les deux formes légales mesurées n'accélèrent rien
    (l'une est même un peu pire, l'autre inutilisable par la requête actuelle).

    Sa place est **la bascule de lecture, pas M4** : le chemin de claim vit encore
    sur `datastore_rows`, donc l'index et la réécriture qui le rendrait utile se
    tranchent ensemble, le jour où la file change de table. Ce qui reste à arbitrer
    d'ici là est un contrat de surface, pas un index — toute forme utile change
    l'ordre observable de `data_claim_next`, sauf à relâcher les baux expirés au fil
    du claim pour que `claimed_until IS NULL` (immuable, donc indexable en partiel)
    redevienne le prédicat complet.

    Les index de RECHERCHE, eux, ont quitté l'interdit en fermant #282 — mais ils ne
    sont pas ici : leur expression est construite par `db/search.py` (source unique
    index ↔ requête), cf. le test suivant."""
    idx = re.findall(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+) ON nodes\(([^)]*)\)",
                     _SCHEMA_SRC)
    assert idx == [("idx_nodes_parent", "parent_id"),
                   ("idx_nodes_owner_scoped", "owner_type, owner_id")], idx
    assert re.search(r"idx_nodes_owner_scoped[^;]*WHERE kind <> 'ligne'",
                     _SCHEMA_SRC, re.S), (
        "l'index d'ownership doit rester PARTIEL : sans son prédicat, il se remet à "
        "indexer chaque ligne de tableau — le coût que le lot M4 a précisément retiré.")
    assert not re.search(r"ON nodes\s+USING", _SCHEMA_SRC, re.I), (
        "un index GIN d'expression posé à la main dans _schema.py : son expression "
        "diverge alors de celle de la requête (elles ne peuvent plus venir du même "
        "helper), et le planner cesse de l'utiliser sans que rien ne le dise.")


def test_public_id_uniqueness_is_named():
    """Contrainte NOMMÉE (docs/live-migrations.md) : un `DROP CONSTRAINT IF EXISTS`
    futur ne doit pas pouvoir viser la contrainte toute neuve d'une install fraîche.
    Cette unicité porte AUSSI l'invariant que `guides` tenait en
    `UNIQUE (scope, owner_id, slug)` — l'identifiant public en est dérivé."""
    assert "CONSTRAINT nodes_public_id_key UNIQUE (public_id)" in _nodes_block()


# ⚠️ Deux tests ont disparu d'ici le 23/09/2026 avec ce qu'ils gardaient
# (otomata-tech/oto#239) : `test_conversion_follows_every_write_to_guides_of_the_same_boot`
# et `test_conversion_is_replayable`. La recopie `guides` → `nodes` ne se joue plus au
# démarrage, et la table est sortie du code — il n'y a plus ni ordre d'écriture à tenir
# ni rejeu à garantir. Les cliquets qui remplacent : `tests/test_search_reads_nodes.py` §4
# (plus rien ne touche la table, et rien ne la recrée).

def test_public_id_has_a_single_definition():
    """L'identifiant public est DÉRIVÉ de la clé naturelle. Deux dérivations qui
    divergeraient d'un caractère rempliraient la table de doublons, en silence.

    Il n'y en avait qu'UNE pour deux appelants (la façade et la conversion) ; depuis
    le retrait de la conversion (oto#239) il n'en reste qu'un, donc la dérivation est
    une CONSTANTE — la fonction paramétrable n'avait plus d'objet. Ce qui se garde
    reste le même : une seule écriture de la formule dans tout le module."""
    formules = re.findall(r"'nod_' \|\| substr\(md5\('ctx:'", _GUIDES_SRC)
    assert len(formules) == 1, formules
    assert "def _public_id_sql(" not in _GUIDES_SRC


def test_the_notion_of_guide_kind_does_not_exist():
    """0055-D4 : une couche de contexte EST une page. Le lot ne déménage pas une
    table, il dissout un concept — s'il restait un `kind='guide'`, la livraison
    resterait une NATURE et le modèle unique serait un modèle à exceptions."""
    from oto_mcp.db import guides as G
    assert G._KIND == "page"
    # Un seul genre nommé dans toute la façade, et il vient de la constante.
    assert set(re.findall(r"kind = '([^']+)'", _GUIDES_SRC)) == {"{_KIND}"}
    # La livraison, elle, est une clé de `props` — donc une propriété.
    assert "props->>'delivery'" in _GUIDES_SRC


def test_the_facade_no_longer_reads_the_legacy_table():
    """La bascule de lecture (0063-D4) : la surface `oto_guide` / `/api/me/guides/*`
    est inchangée, mais elle lit `nodes`. Aucun SQL de la façade ne doit plus
    toucher `guides` — sinon on retombe dans la double lecture, donc dans les deux
    vérités qui divergent. Le module ENTIER, depuis le retrait de la conversion."""
    assert not re.search(r"(FROM|INTO|UPDATE|TABLE) guides", _GUIDES_SRC), _GUIDES_SRC


def test_user_owned_nodes_follow_a_tenant_switch():
    """`migrate_sub` repointe les ressources d'un compte migré. Les couches de
    contexte d'un utilisateur ont changé de table : sans cette entrée, une bascule
    de tenant les orphelinerait — exactement le trou que l'inventaire avait déjà eu
    sur les ressources possédées (Phase H B1)."""
    from oto_mcp.db.users import _SUB_COLUMNS
    assert ("nodes", "owner_id") in _SUB_COLUMNS
    assert ("guides", "owner_id") not in _SUB_COLUMNS, (
        "La table `guides` est sortie du code (oto#239) : la repointer revient à "
        "écrire dans une table morte, et le merge échouerait après son DROP.")


def test_nothing_else_touches_nodes_yet():
    """M1 livre la table et les guides convertis, rien d'autre. Ce garde-fou
    n'interdit rien pour toujours — il force à ce que le premier autre lecteur de
    `nodes` (les pages en M2, les tableaux en M3) soit un acte délibéré, avec sa
    revue, plutôt qu'un effet de bord.

    Deux lecteurs s'y sont ajoutés délibérément en fermant #282 : la recherche et
    l'outbox d'embeddings, les deux seuls consommateurs de guides qui vivaient hors
    façade et étaient restés sur la table gelée.

    Un ÉCRIVAIN s'y ajoute au lot **M2** (#287) : `db/nodes.py`, la conversion des
    projets et des pages. C'est exactement l'acte délibéré que ce garde-fou
    réclamait — il n'écrit rien qui ne vienne de `projects`/`docs`, et **aucune
    surface ne lit encore ces nœuds-là** (la bascule de lecture est un autre lot).

    **Ce lot est arrivé le 17/08 : `db/shell.py`, la PREMIÈRE surface de lecture.**
    C'est `/shell` v0 — le rail du front, contracté avec lui (`shell-contract.md`) et
    accepté le 16/08. Le garde-fou a joué son rôle exactement comme annoncé : il a
    refusé le nouveau lecteur jusqu'à ce que son inscription soit écrite ici, avec son
    motif. Deux choses le bornent, et elles sont dans le module :
    lecture SEULE (les écritures restent sur les surfaces actuelles jusqu'à M-h), et
    `kind <> 'ligne'` dans chaque requête — sans quoi le rail avalerait les 43 584
    lignes du datastore ET perdrait l'index partiel d'ownership (`tests/test_shell_v0`
    le fige en lisant le SQL, pas un résultat).

    **Le second suit le 17/08 : `db/node_view.py`, l'ouverture d'UN nœud** (lot ④ de la
    même file). Mêmes bornes : lecture seule, prédicat de genre partout, et une de plus
    qui lui est propre — **ouvrir un tableau rend son SCHÉMA, jamais ses lignes**. La
    surface des lignes est un autre lot, paginé par curseur : un « ouvrir » qui ramène
    43 584 lignes n'est pas une fiche, c'est un export déguisé.

    **Le lot ⑧ ajoute une ÉCRITURE le 21/08** : `convert_guides` dans `db/nodes.py`
    (déjà inscrit) — les procédures deviennent des nœuds. Elle ferme un trou visible
    depuis la naissance du rail : un partage direct de procédure ne désignait aucun
    nœud, donc n'entrait pas dans la section « Partagé ». Rien de neuf n'est lu ici —
    c'est la même famille de conversion que les projets, les pages et les tableaux.

    **Le 2026-09-01, un écrivain de plus, et c'est le premier qui ne convertit rien** :
    `db/node_tables.py` — les tableaux et les lignes NATIFS. La recopie est arrêtée, la
    nouvelle surface part de vide : elle n'a plus qu'une façon d'être remplie, ses
    propres verbes. Ses bornes sont dans le module — jamais un nœud marqué par la
    recopie (chaque écriture porte `props->>'legacy' IS NULL`), et la donnée métier va
    dans la colonne `data`, jamais dans `props`."""
    allowed = {"oto_mcp/db/guides.py", "oto_mcp/db/_schema.py", "oto_mcp/db/_init.py",
               "oto_mcp/db/users.py", "oto_mcp/db/search.py", "oto_mcp/db/aux_embed.py",
               "oto_mcp/db/nodes.py", "oto_mcp/db/blocks.py", "oto_mcp/db/shell.py",
               "oto_mcp/db/node_view.py", "oto_mcp/db/node_tables.py",
               # Une révision, pas une surface : elle retire des `props` la copie du
               # jeton de lien public qu'y avait posée la recopie des pages.
               "oto_mcp/db/migrations/versions/20261119_0630_jetons_lien_haches.py"}
    offenders = []
    for path in (_ROOT / "oto_mcp").rglob("*.py"):
        rel = str(path.relative_to(_ROOT))
        if rel in allowed:
            continue
        # Les fragments de DDL (`db/schema/*`) sont l'ex-contenu de `_schema.py`,
        # déjà admis ci-dessus : déclarer une table n'est pas la lire.
        if rel.startswith("oto_mcp/db/schema/"):
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.search(r"\b(FROM|INTO|UPDATE|TABLE|JOIN) nodes\b", line):
                offenders.append(f"{rel}: {line.strip()}")
    assert not offenders, (
        "quelqu'un lit/écrit `nodes` hors du périmètre M1 : "
        f"{offenders}. Si c'est voulu, c'est un autre lot — retirer ce test dans le "
        "même commit pour que la bascule soit visible en revue.")
