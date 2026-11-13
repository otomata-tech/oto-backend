"""Banc de la recherche transverse (`oto_search`) à VOLUME RÉEL d'une grosse org.

Pourquoi un banc et pas un test : ce qui se mesure ici, c'est le PLAN que PostgreSQL
choisit sur des dizaines de milliers de contenus — une base de test de vingt lignes
ne le montre jamais (le planificateur y lit tout, et vite).

**Le jeu** (`preparer`), déterministe (graine fixe), dans une base DÉDIÉE :
- une org, un projet, un tableau ;
- 26 500 pages d'environ 2 100 caractères de français ;
- 27 000 lignes de tableau JSON (clés récurrentes, dont `ca`) d'environ 1 250 caractères ;
- 200 textes de fichier extraits d'environ 70 000 caractères.
Deux termes sont FRÉQUENTS (« ca » dans 60 % des pages et dans chaque ligne, par sa
clé ; « impact » dans 30 %), six sont rares. Aucun contenu ne porte les huit termes de
la requête longue ; quelques contenus PLANTÉS en portent six, d'autres cinq, d'autres
le seul « ca » avec un titre trompeur — la pertinence se lit sur eux.

**La mesure** (`mesurer`) : la capacité `me.search` de bout en bout (`_search`), avec
l'embedding de la requête SIMULÉ à 300 ms (aucun appel réseau), et la durée de chaque
source. Trois requêtes : huit mots dont le ET ne trouve rien, quatre mots dont le ET
trouve, un mot. Médiane de `--repetitions` passages après un passage de chauffe.

Pour comparer deux versions du code, la même commande se joue avec `PYTHONPATH` pointé
sur l'une puis l'autre (le chemin du paquet mesuré est imprimé) :

    # base jetable (aucune donnée réelle)
    docker run -d --rm --name banc-recherche -e POSTGRES_PASSWORD=test \\
        -p 127.0.0.1:55432:5432 pgvector/pgvector:pg17 -c shared_buffers=512MB
    DSN=postgresql://postgres:test@127.0.0.1:55432/postgres
    uv run python scripts/banc_recherche_volume.py preparer --dsn $DSN
    uv run python scripts/banc_recherche_volume.py mesurer --dsn $DSN
    PYTHONPATH=<export de la version de référence> \\
        uv run python scripts/banc_recherche_volume.py mesurer --dsn $DSN
    docker rm -f banc-recherche

`preparer` crée (ou recrée) la base `banc_recherche` sur le serveur du DSN. Elle pose un
`statement_timeout` de 60 s, pour qu'une requête dégénérée se lise « coupée » au lieu
de tenir le banc. Sortie : un tableau lisible sur la sortie standard, et le JSON complet
avec `--json <fichier>`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
from types import SimpleNamespace

BASE = "banc_recherche"
GRAINE = 20261112

N_PAGES = 26_500
N_LIGNES = 27_000
N_FICHIERS = 200
CAR_PAGE = 2_100
CAR_COMMENTAIRE = 1_100
CAR_FICHIER = 70_000

REQUETES = {
    "huit_mots": "remboursement forfait séances avoir impact CA board Monday",
    "quatre_mots": "remboursement forfait impact CA",
    "un_mot": "impact",
}
# Les termes de la requête longue, et leur fréquence par page (ou par ligne).
FREQUENTS = {"CA": 0.60, "impact": 0.30}
RARES = {"remboursement": 0.02, "forfait": 0.03, "séances": 0.02, "avoir": 0.04,
         "board": 0.01, "Monday": 0.005}
# Plantés : six termes sur huit (ni « board » ni « Monday »), cinq sur huit, et des
# leurres qui ne portent que le terme fréquent, répété, sous un titre qui en parle.
SIX = ["remboursement", "forfait", "séances", "avoir", "impact", "CA"]
CINQ = ["remboursement", "forfait", "séances", "impact", "CA"]
N_PLANTES = 4

MOTS = (
    "projet client équipe réunion budget planning livraison contrat fournisseur "
    "facture paiement trésorerie stratégie marché produit service qualité délai "
    "objectif résultat analyse rapport synthèse dossier document compte rendu "
    "décision validation proposition offre commande stock logistique transport "
    "entrepôt production atelier maintenance sécurité conformité audit contrôle "
    "procédure processus outil logiciel serveur données sauvegarde migration "
    "formation recrutement salarié collaborateur manager direction comité "
    "partenaire prestataire consultant mission étude diagnostic recommandation "
    "risque opportunité croissance marge coût prix tarif remise promotion vente "
    "achat négociation relance prospect devis signature juridique fiscal social "
    "comptable bilan exercice clôture prévision tableau indicateur suivi "
    "trimestre semestre annuel mensuel hebdomadaire calendrier échéance retard "
    "priorité urgence incident ticket support assistance demande réponse question "
    "rendez-vous accueil agenda secrétariat courrier archive classement tiers "
    "abonnement adhésion convention parcours inscription consultation suivi bilan "
    "région agence site boutique magasin réseau franchise enseigne ouverture "
    "travaux chantier devis métrage plan permis architecte fournisseurs matériaux"
).split()
LIENS = ("le la les un une des du de et en pour sur avec dans par au aux ce cette "
         "ces qui que est sont a ont été sera nous vous ils elle il pas plus très "
         "aussi mais donc ou ni car").split()


# ── génération ──────────────────────────────────────────────────────────────────

def _phrase(rng: random.Random) -> str:
    n = rng.randint(8, 16)
    mots = [rng.choice(MOTS) if rng.random() < 0.55 else rng.choice(LIENS)
            for _ in range(n)]
    return " ".join(mots).capitalize() + "."


def _texte(rng: random.Random, cible: int) -> str:
    morceaux, total = [], 0
    while total < cible:
        p = _phrase(rng)
        morceaux.append(p)
        total += len(p) + 1
    return " ".join(morceaux)


def _avec(rng: random.Random, texte: str, termes: list[str]) -> str:
    """Insère chaque terme à une place au hasard (une phrase de plus par terme)."""
    phrases = texte.split(". ")
    for t in termes:
        i = rng.randrange(len(phrases))
        phrases.insert(i, f"Point {t} {rng.choice(MOTS)} {rng.choice(MOTS)}")
    return ". ".join(phrases)


def _termes_tires(rng: random.Random, facteur: float = 1.0) -> list[str]:
    tires = [t for t, p in {**FREQUENTS, **RARES}.items() if rng.random() < p * facteur]
    if len(tires) == 8:                     # jamais les huit : le ET doit rester vide
        tires.remove("Monday")
    return tires


def _pages(rng: random.Random):
    for i in range(N_PAGES):
        titre = f"Note {i} {rng.choice(MOTS)} {rng.choice(MOTS)}"
        yield titre, _avec(rng, _texte(rng, CAR_PAGE), _termes_tires(rng))
    for k in range(N_PLANTES):
        yield f"PLANTE-6-{k} synthèse", _avec(rng, _texte(rng, CAR_PAGE), SIX)
        yield f"PLANTE-5-{k} synthèse", _avec(rng, _texte(rng, CAR_PAGE), CINQ)
        # Leurre : « CA » partout, rien d'autre de la requête.
        yield (f"LEURRE-{k} CA CA CA",
               _avec(rng, _texte(rng, CAR_PAGE), ["CA"] * 12))


def _lignes(rng: random.Random):
    for i in range(N_LIGNES):
        yield f"r{i}", {
            "ca": rng.randint(1_000, 900_000), "mois": f"2026-{rng.randint(1, 12):02d}",
            "client": f"{rng.choice(MOTS)} {rng.choice(MOTS)}",
            "statut": rng.choice(["ouvert", "clos", "en attente", "relancé"]),
            "commentaire": _avec(rng, _texte(rng, CAR_COMMENTAIRE),
                                 [t for t in _termes_tires(rng) if t != "CA"])}
    for k in range(N_PLANTES):
        yield f"plante-6-{k}", {"ca": 1, "mois": "2026-01", "client": "plante",
                                "statut": "ouvert",
                                "commentaire": _avec(rng, _texte(rng, CAR_COMMENTAIRE), SIX)}


def _fichiers(rng: random.Random):
    for i in range(N_FICHIERS):
        texte = _texte(rng, CAR_FICHIER)
        # Sur 70 000 caractères, la fréquence par page ferait porter les huit termes à
        # presque chaque fichier : tirage par FICHIER, à fréquence relevée.
        yield f"fichier-{i}.pdf", _avec(rng, texte, _termes_tires(rng, facteur=4.0))
    for k in range(N_PLANTES):
        yield f"plante-6-{k}.pdf", _avec(rng, _texte(rng, CAR_FICHIER), SIX)


def preparer(dsn_serveur: str) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    racine = psycopg.connect(dsn_serveur, autocommit=True)
    racine.execute(f'DROP DATABASE IF EXISTS "{BASE}" WITH (FORCE)')
    racine.execute(f'CREATE DATABASE "{BASE}"')
    racine.execute(f'ALTER DATABASE "{BASE}" SET statement_timeout = \'60s\'')
    racine.close()
    dsn = _dsn_base(dsn_serveur)
    os.environ["DATABASE_URL"] = dsn
    # L'identité qu'exige le démarrage d'une base neuve : celle d'une instance de banc.
    os.environ.setdefault("OTO_TENANT_PRIMAIRE_SLUG", "banc")
    os.environ.setdefault("OTO_BRAND_NAME", "banc")
    from oto_mcp.db import init_db
    from oto_mcp.db import search as dbs
    t0 = time.monotonic()
    init_db()
    print(f"schéma posé en {time.monotonic() - t0:.1f} s", file=sys.stderr)

    rng = random.Random(GRAINE)
    with psycopg.connect(dsn) as c:
        c.execute("SET statement_timeout = 0")      # le chargement, lui, prend son temps
        org = c.execute("INSERT INTO orgs (name) VALUES ('banc') RETURNING id").fetchone()[0]
        pid = c.execute(
            "INSERT INTO projects (owner_type, owner_id, name) VALUES ('org', %s, 'banc') "
            "RETURNING id", (str(org),)).fetchone()[0]
        ns = c.execute(
            "INSERT INTO user_datastores (owner_type, owner_id, namespace, schema) "
            "VALUES ('org', %s, 'suivi', %s) RETURNING id",
            (str(org), Jsonb({"fields": [{"key": "ca", "label": "CA"}]}))).fetchone()[0]
        t0 = time.monotonic()
        with c.cursor().copy("COPY docs (project_id, title, body_md) FROM STDIN") as cp:
            for titre, corps in _pages(rng):
                cp.write_row((pid, titre, corps))
        with c.cursor().copy("COPY datastore_rows (ns_id, row_id, data) FROM STDIN") as cp:
            for rid, data in _lignes(rng):
                cp.write_row((ns, rid, json.dumps(data, ensure_ascii=False)))
        for nom, texte in _fichiers(rng):
            fid = c.execute(
                "INSERT INTO project_files (project_id, s3_key, filename) "
                "VALUES (%s, %s, %s) RETURNING id", (pid, f"banc/{nom}", nom)).fetchone()[0]
            c.execute("INSERT INTO project_file_texts (file_id, status, extracted_text) "
                      "VALUES (%s, 'ok', %s)", (fid, texte))
        c.commit()
        print(f"contenus écrits en {time.monotonic() - t0:.1f} s", file=sys.stderr)
        # Le vecteur de classement rempli à 100 %, comme en production.
        t0 = time.monotonic()
        for table in dbs.RANKED_SOURCES:
            c.execute(dbs.rank_backfill_sql(table, 10_000_000))
        c.commit()
        print(f"vecteurs remplis en {time.monotonic() - t0:.1f} s", file=sys.stderr)
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET statement_timeout = 0")
        c.execute("VACUUM ANALYZE")
        tailles = c.execute(
            "SELECT (SELECT count(*) FROM docs) AS pages, "
            "(SELECT sum(length(body_md)) FROM docs) AS car_pages, "
            "(SELECT count(*) FROM datastore_rows) AS lignes, "
            "(SELECT sum(length(data::text)) FROM datastore_rows) AS car_lignes, "
            "(SELECT count(*) FROM project_file_texts) AS fichiers, "
            "(SELECT sum(length(extracted_text)) FROM project_file_texts) AS car_fichiers"
        ).fetchone()
    print("jeu :", dict(zip(("pages", "car_pages", "lignes", "car_lignes", "fichiers",
                             "car_fichiers"), tailles)), file=sys.stderr)


def _dsn_base(dsn_serveur: str) -> str:
    return dsn_serveur.rsplit("/", 1)[0] + "/" + BASE


# ── mesure ──────────────────────────────────────────────────────────────────────

# Les sources SQL de la recherche, telles que l'orchestrateur les appelle (`db.<nom>`).
SOURCES = ("search_docs_fts", "search_docs_semantic", "search_project_briefs",
           "search_briefs_semantic", "search_procedures_fts", "search_guides_fts",
           "search_guides_semantic", "search_datastore_rows_fts",
           "search_datastore_rows_semantic", "search_files_meta", "search_file_contents")
EMBEDDING_S = 0.3


def mesurer(dsn_serveur: str, repetitions: int) -> dict:
    import psycopg

    os.environ["DATABASE_URL"] = _dsn_base(dsn_serveur)
    import oto_mcp
    from oto_mcp import db, embeddings, ownership, search as search_mod
    from oto_mcp.capabilities import search as cap

    with psycopg.connect(os.environ["DATABASE_URL"]) as c:
        org, pid = c.execute("SELECT owner_id::bigint, id FROM projects LIMIT 1").fetchone()
        ns = c.execute("SELECT id, namespace AS datastore, owner_type, owner_id, schema "
                       "FROM user_datastores LIMIT 1").fetchone()
    namespace = {"id": ns[0], "datastore": ns[1], "owner_type": ns[2],
                 "owner_id": ns[3], "schema": ns[4]}

    # Le contexte d'accès est hors du sujet mesuré : il est résolu une fois, ici.
    ownership.accessible_project_ids = lambda *a, **k: [pid]
    ownership.perso_de_la_liste = lambda *a, **k: False
    search_mod._accessible_namespaces = lambda *a, **k: [namespace]
    cap._preparer = lambda ctx, inp: ([], [])

    durees: dict[str, float] = {}

    def _chrono(nom, f):
        def _f(*a, **k):
            t = time.perf_counter()
            try:
                return f(*a, **k)
            finally:
                durees[nom] = time.perf_counter() - t
        return _f

    for nom in SOURCES:
        setattr(db, nom, _chrono(nom, getattr(db, nom)))

    async def _vecteur(q, **_k):
        await asyncio.sleep(EMBEDDING_S)
        return [0.01] * embeddings.DIM
    embeddings.embed_query = _vecteur

    ctx = SimpleNamespace(sub="banc", org_id=int(org))

    def _une(q: str) -> tuple[float, dict, dict]:
        durees.clear()
        t = time.perf_counter()
        rep = asyncio.run(cap._search(ctx, cap.SearchInput(q=q, limit=20)))
        return time.perf_counter() - t, dict(durees), rep

    out = {"paquet": os.path.dirname(oto_mcp.__file__), "requetes": {}}
    for cle, q in REQUETES.items():
        _une(q)                                         # chauffe
        passages = [_une(q) for _ in range(repetitions)]
        totaux = [p[0] for p in passages]
        par_source = {s: statistics.median(p[1].get(s, 0.0) for p in passages)
                      for s in SOURCES if any(s in p[1] for p in passages)}
        hits = passages[-1][2]["hits"]
        out["requetes"][cle] = {
            "q": q, "total_s": statistics.median(totaux), "totaux_s": totaux,
            "par_source_s": par_source, "n_hits": len(hits),
            "tete": [(h["kind"], h["title"][:40]) for h in hits[:10]]}
    return out


def _afficher(res: dict) -> None:
    print(f"paquet mesuré : {res['paquet']}")
    for cle, r in res["requetes"].items():
        print(f"\n== {cle} — « {r['q']} » : total {r['total_s'] * 1000:.0f} ms "
              f"(passages : {', '.join(f'{t * 1000:.0f}' for t in r['totaux_s'])}), "
              f"{r['n_hits']} résultats")
        for s, d in sorted(r["par_source_s"].items(), key=lambda t: -t[1]):
            print(f"   {s:<32} {d * 1000:>9.1f} ms")
        print("   tête :", "; ".join(f"{k}:{t}" for k, t in r["tete"]))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("action", choices=("preparer", "mesurer"))
    p.add_argument("--dsn", required=True, help="DSN du SERVEUR jetable (base postgres)")
    p.add_argument("--repetitions", type=int, default=3)
    p.add_argument("--json", help="écrit le résultat complet dans ce fichier")
    a = p.parse_args()
    try:
        if a.action == "preparer":
            preparer(a.dsn)
            return 0
        res = mesurer(a.dsn, a.repetitions)
    finally:
        from oto_mcp.db import _conn
        if _conn._pool is not None:
            _conn._pool.close()
    _afficher(res)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
