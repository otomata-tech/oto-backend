#!/usr/bin/env python3
"""Range sous leur NUMÉRO les tableaux que la base désigne encore par leur NOM.

⚠️ **À BLANC PAR DÉFAUT.** `--appliquer` écrit ; sans lui, rien ne bouge et le script
rend ce qu'il ferait, chiffré. Même règle que `renommer_contenus_datastore.py` : un
remplacement de masse sur des données de clients ne se lance pas par accident.

## Pourquoi ce script existe

Un tableau s'adresse par son numéro (`ns_id`), et un nom est REFUSÉ depuis le
08/11/2026 (`datastore/errors.TableauParNom`). Quatre endroits de la base pouvaient
garder un NOM :

    flottes    `runner_fleets.namespace` — les automatisations déclarées avant #1067
    liens      `project_links.target_ref` d'un lien `tableau` posé par nom
    jetons     les clés de `user_api_tokens.scopes.namespaces` (oto#158)
    contenus   le motif `datastore="<nom>"` des procédures actives, des pages de
               projet (`docs`) et des blocs de nœud — ce qu'un agent RECOPIE

⚠️ **Il ne devine jamais.** Un nom se résout dans la portée de son PROPRIÉTAIRE (le
déclarant d'une flotte, le propriétaire du projet, le porteur du jeton, le
propriétaire du contenu), avec les résolveurs du produit — jamais par la simple
existence du nom ailleurs (#365). Un nom qui désigne plusieurs tableaux, ou aucun, est
LISTÉ et laissé tel quel : c'est à un humain de trancher. La prose libre n'est pas
réécrite, seulement le motif `datastore="…"`.

⚠️ **Après le tag qui sert la portée par numéro** (sinon un jeton migré n'ouvrirait
plus rien sur l'ancien code). L'ordre : pousser, taguer, puis `--appliquer`.

L'état d'AVANT est écrit dans `--sauvegarde` dans le MÊME geste, avant le premier
UPDATE : c'est lui qui permet de revenir en arrière, pas un remplacement inverse.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys
from collections import Counter

SECTIONS = ("flottes", "liens", "jetons", "contenus")

#: Le motif réécrit, et lui seul : l'argument `datastore=` d'un appel montré à l'agent.
MOTIF = re.compile(r"""(\bdatastore\s*=\s*)(["'])([^"'\n]+)\2""")


def _est_un_nom(adresse: str) -> bool:
    a = (adresse or "").strip()
    return bool(a) and not a.isdigit() and not a.startswith("slot:")


def _portee_du_proprietaire(cur, owner_type: str, owner_id: str,
                            context_org_id=None):
    """La portée où se résout un nom écrit par un PROPRIÉTAIRE — la règle des liens de
    projet (`db.projects._portee_du_projet`). `None` pour un propriétaire sans portée
    de tableaux (plateforme, tenant) : son contenu est listé, jamais réécrit."""
    from oto_mcp.db.projects import _portee_du_projet
    if owner_type not in ("user", "org", "group"):
        return None
    parente = None
    if owner_type == "group":
        cur.execute("SELECT org_id FROM org_groups WHERE id::text = %s", (str(owner_id),))
        r = cur.fetchone()
        parente = r["org_id"] if r else None
    return _portee_du_projet({"owner_type": owner_type, "owner_id": owner_id,
                              "context_org_id": context_org_id,
                              "group_org_id": parente})


def _resoudre(noms, portee) -> tuple[dict, set]:
    from oto_mcp import db
    if portee is None or not noms:
        return {}, set()
    return db.resolve_datastore_ids_by_name(sorted(set(noms)), **portee)


# ── flottes ─────────────────────────────────────────────────────────────────

def flottes(cur, rapport: dict, avant: list, appliquer: bool) -> None:
    from oto_mcp.capabilities import _lignes_reservables as lr
    cur.execute("SELECT * FROM runner_fleets WHERE namespace IS NOT NULL "
                "AND btrim(namespace) <> '' AND btrim(namespace) !~ '^[0-9]+$' "
                "ORDER BY id")
    c = rapport.setdefault("flottes", Counter())
    for f in (dict(r) for r in cur.fetchall()):
        etat = f.get("status") or "?"
        c["par nom"] += 1
        try:
            cle = lr._cle_heritee(f)
        except lr.TableauAmbigu:
            c["AMBIGUËS (listées)"] += 1
            rapport.setdefault("_liste", []).append(("flotte ambiguë", f["id"], etat))
            continue
        if cle is None:
            c["SANS TABLEAU (listées)"] += 1
            rapport.setdefault("_liste", []).append(("flotte sans tableau", f["id"], etat))
            continue
        c["migrables"] += 1
        if etat in ("running", "armed"):
            c["  dont actives"] += 1
        avant.append({"section": "flottes", "id": f["id"], "namespace": f["namespace"],
                      "input": f.get("input"), "cle": cle})
        if appliquer:
            lr.fixer_le_tableau(f)


# ── liens de projet ─────────────────────────────────────────────────────────

def liens(cur, rapport: dict, avant: list, appliquer: bool) -> None:
    from oto_mcp.db.projects import _portee_du_projet
    cur.execute(
        "SELECT pl.id, pl.project_id, pl.target_ref, p.archived_at, p.owner_type, "
        "       p.owner_id, p.context_org_id, g.org_id AS group_org_id "
        "FROM project_links pl JOIN projects p ON p.id = pl.project_id "
        "LEFT JOIN org_groups g ON p.owner_type = 'group' AND g.id::text = p.owner_id "
        "WHERE pl.target_type = 'tableau' AND btrim(pl.target_ref) !~ '^[0-9]+$' "
        "ORDER BY pl.id")
    c = rapport.setdefault("liens", Counter())
    for l in (dict(r) for r in cur.fetchall()):
        nom = l["target_ref"]
        c["par nom"] += 1
        resolus, ambigus = _resoudre([nom], _portee_du_projet(l))
        if nom in ambigus or nom not in resolus:
            cause = "ambigu" if nom in ambigus else "introuvable"
            c[f"{cause.upper()}S (listés)"] += 1
            rapport.setdefault("_liste", []).append(
                (f"lien {cause}", l["id"], f"projet {l['project_id']}"
                 + (" archivé" if l["archived_at"] else "")))
            continue
        cle = str(resolus[nom])
        # L'unicité (projet, type, réf) : un lien par numéro vers le même tableau existe
        # déjà ⇒ celui-ci est un DOUBLON. Listé, pas supprimé : c'est un lien de client.
        cur.execute("SELECT 1 FROM project_links WHERE project_id = %s "
                    "AND target_type = 'tableau' AND target_ref = %s",
                    (l["project_id"], cle))
        if cur.fetchone():
            c["DOUBLONS d'un lien par numéro (listés)"] += 1
            rapport.setdefault("_liste", []).append(
                ("lien doublon", l["id"], f"projet {l['project_id']} → {cle}"))
            continue
        c["migrables"] += 1
        avant.append({"section": "liens", "id": l["id"], "target_ref": nom, "cle": cle})
        if appliquer:
            cur.execute("UPDATE project_links SET target_ref = %s "
                        "WHERE id = %s AND target_ref = %s", (cle, l["id"], nom))


# ── portées de jetons ───────────────────────────────────────────────────────

def jetons(cur, rapport: dict, avant: list, appliquer: bool) -> None:
    from oto_mcp.datastore.core import make_store
    cur.execute("SELECT id, sub, scopes FROM user_api_tokens "
                "WHERE scopes ? 'namespaces' AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at > NOW()) ORDER BY id")
    c = rapport.setdefault("jetons", Counter())
    for t in (dict(r) for r in cur.fetchall()):
        ns = (t["scopes"] or {}).get("namespaces") or {}
        noms = [k for k in ns if _est_un_nom(k)]
        if not noms:
            continue
        c["portées par nom"] += 1
        c["  clés par nom"] += len(noms)
        # La portée de l'ÉMISSION : ce que le porteur voit (`api_tokens._par_identifiant`).
        par_nom: dict = {}
        for n in make_store(t["sub"]).list_datastores():
            par_nom.setdefault(n["datastore"], set()).add(str(n["id"]))
        echecs = [k for k in noms if len(par_nom.get(k, ())) != 1]
        if echecs:
            c["NON MIGRABLES (listés)"] += 1
            rapport.setdefault("_liste", []).append(
                ("jeton non migrable", t["id"], f"{len(echecs)} clé(s) sans tableau unique"))
            continue
        ranges = {(next(iter(par_nom[k])) if k in noms else k): droit
                  for k, droit in ns.items()}
        c["migrables"] += 1
        avant.append({"section": "jetons", "id": t["id"], "scopes": t["scopes"]})
        if appliquer:
            cur.execute("UPDATE user_api_tokens SET scopes = %s::jsonb WHERE id = %s",
                        (json.dumps({**t["scopes"], "namespaces": ranges}), t["id"]))


# ── contenus servis aux agents ──────────────────────────────────────────────

def _reecrire(texte: str, portee, c: Counter) -> str:
    """Le texte, chaque `datastore="<nom>"` résolu sans ambiguïté remplacé par
    `datastore=<numéro>`. Les autres occurrences restent, et sont comptées."""
    noms = [m.group(3) for m in MOTIF.finditer(texte) if _est_un_nom(m.group(3))]
    resolus, ambigus = _resoudre(noms, portee)

    def _un(m):
        nom = m.group(3)
        if not _est_un_nom(nom):
            return m.group(0)
        if nom in resolus and nom not in ambigus:
            c["occurrences réécrites"] += 1
            return f"{m.group(1)}{resolus[nom]}"
        c["occurrences AMBIGUËS (laissées)" if nom in ambigus
          else "occurrences INTROUVABLES (laissées)"] += 1
        return m.group(0)
    return MOTIF.sub(_un, texte)


def _reecrire_json(valeur, portee, c: Counter):
    """Même réécriture, dans chaque chaîne d'un JSON (les `props` d'un bloc) : sur le
    texte sérialisé, les guillemets sont échappés et le motif ne se verrait pas."""
    if isinstance(valeur, str):
        return _reecrire(valeur, portee, c)
    if isinstance(valeur, list):
        return [_reecrire_json(v, portee, c) for v in valeur]
    if isinstance(valeur, dict):
        return {k: _reecrire_json(v, portee, c) for k, v in valeur.items()}
    return valeur


def _cite_un_nom(valeur) -> bool:
    if isinstance(valeur, str):
        return any(_est_un_nom(m.group(3)) for m in MOTIF.finditer(valeur))
    if isinstance(valeur, list):
        return any(_cite_un_nom(v) for v in valeur)
    if isinstance(valeur, dict):
        return any(_cite_un_nom(v) for v in valeur.values())
    return False


def contenus(cur, rapport: dict, avant: list, appliquer: bool) -> None:
    c = rapport.setdefault("contenus", Counter())
    requetes = (
        ("org_instructions", "id", "body_md",
         "SELECT id, body_md AS txt, owner_type, owner_id, NULL AS ctx FROM org_instructions "
         "WHERE archived_at IS NULL AND body_md ~ 'datastore\\s*=\\s*[\"'']'"),
        ("docs", "id", "body_md",
         "SELECT d.id, d.body_md AS txt, p.owner_type, p.owner_id, p.context_org_id AS ctx "
         "FROM docs d JOIN projects p ON p.id = d.project_id "
         "WHERE d.body_md ~ 'datastore\\s*=\\s*[\"'']'"),
        ("blocks", "id", "props",
         "SELECT b.id, b.props AS txt, n.owner_type, n.owner_id, NULL AS ctx "
         "FROM blocks b JOIN nodes n ON n.id = b.node_id "
         "WHERE b.props::text ~ 'datastore\\s*=\\s*\\\\[\"'']'"),
    )
    for table, pk, col, sql in requetes:
        cur.execute(sql)
        for r in (dict(x) for x in cur.fetchall()):
            texte = r["txt"] if col == "props" else (r["txt"] or "")
            if not _cite_un_nom(texte):
                continue
            c[f"{table} : documents qui citent un nom"] += 1
            portee = _portee_du_proprietaire(cur, r["owner_type"], r["owner_id"], r["ctx"])
            if portee is None:
                c[f"{table} : HORS PORTÉE (propriétaire {r['owner_type']}, listés)"] += 1
                rapport.setdefault("_liste", []).append(
                    (f"{table} hors portée", r["id"], r["owner_type"]))
                continue
            neuf = (_reecrire_json(texte, portee, c) if col == "props"
                    else _reecrire(texte, portee, c))
            if neuf == texte:
                continue
            c[f"{table} : documents réécrits"] += 1
            avant.append({"section": "contenus", "table": table, "col": col, "pk": pk,
                          "id": r["id"], "texte": texte, "neuf": neuf})
            if appliquer:
                if col == "props":
                    cur.execute("UPDATE blocks SET props = %s::jsonb WHERE id = %s",
                                (json.dumps(neuf, ensure_ascii=False), r["id"]))
                else:
                    cur.execute(f"UPDATE {table} SET {col} = %s WHERE {pk} = %s "
                                f"AND {col} = %s", (neuf, r["id"], texte))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--appliquer", action="store_true",
                    help="ÉCRIT en base. Sans lui : à blanc, rien n'est modifié.")
    ap.add_argument("--seulement", choices=SECTIONS, action="append",
                    help="Une section (répétable). Défaut : les quatre.")
    ap.add_argument("--sauvegarde", default="/opt/oto-mcp/sauvegardes",
                    help="Où écrire l'état AVANT, dans le même geste que l'écriture.")
    ap.add_argument("--details", action="store_true",
                    help="Liste aussi ce qui est laissé tel quel (identifiants seuls).")
    args = ap.parse_args(argv[1:])
    sections = args.seulement or list(SECTIONS)

    from oto_mcp.db._conn import _connect

    rapport: dict = {}
    avant: list = []
    # Première passe : LIRE et calculer, rien n'est écrit.
    with _connect() as conn, conn.cursor() as cur:
        for s in sections:
            globals()[s](cur, rapport, avant, False)

    if args.appliquer and avant:
        rep = pathlib.Path(args.sauvegarde)
        rep.mkdir(parents=True, exist_ok=True)
        quand = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        f = rep / f"tableaux-par-numero-{quand}.json"
        f.write_text(json.dumps(avant, ensure_ascii=False, default=str))
        if not f.exists() or f.stat().st_size == 0:
            print("  ⛔ sauvegarde ILLISIBLE — rien n'est écrit en base.")
            return 2
        print(f"  ⤷ sauvegarde AVANT : {f} ({len(avant)} entrées)")
        # Seconde passe : la même, qui écrit. Chaque UPDATE est conditionnel sur la
        # valeur lue : ce qui a bougé entre les deux passes n'est pas écrasé.
        rapport = {}
        with _connect() as conn, conn.cursor() as cur:
            for s in sections:
                globals()[s](cur, rapport, [], True)

    mode = "APPLIQUÉ" if args.appliquer else "À BLANC — rien n'a été écrit"
    print(f"=== {mode} ===")
    for s in sections:
        print(f"\n[{s}]")
        for cle, n in sorted((rapport.get(s) or {}).items()):
            print(f"  {cle:<55} {n}")
    if args.details and rapport.get("_liste"):
        print("\n[laissés tels quels]")
        for quoi, ident, info in rapport["_liste"]:
            print(f"  {quoi:<22} {ident:<10} {info}")
    if not args.appliquer:
        print("\n  Pour écrire : --appliquer. ⚠️ Seulement APRÈS le tag qui sert la portée "
              "par numéro.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
