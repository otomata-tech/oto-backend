"""Pose les arêtes qui manquent à la chaîne de grants pour dire ce que le coffre dit.

**Le trou.** [ADR 0053](blueprint) n'avait pas de bénéficiaire « tout le monde », et
une clé plateforme OUVERTE (`share_mode='open'`, `share_down` vide) accorde pourtant à
quiconque. La fenêtre de double lecture L7 le comptait sous la classe
`free_tier_hors_modele` : tant qu'elle n'est pas à zéro, retirer l'ancien chemin
couperait des gens. Arbitrage du 2026-08-29 : **une arête « tout le monde » explicite
d'abord, l'extinction du free-tier mesurée connecteur par connecteur ensuite.**

**Deux sortes d'arêtes manquent, et les deux se posent ici** — c'est le même acte :
rendre la chaîne capable de dire tout ce que la ligne du coffre dit.

1. **« tout le monde »** — une arête `platform:platform → platform:platform` par
   instance ouverte. Le mot n'est pas inventé : la visibilité dérivée (R9)
   appelait EVERYONE le scope `platform`, et le CHECK de `grants.grantee_kind`
   l'accepte — **aucune migration de schéma**.
2. **les nominatives restées derrière** — un scope de `share_down` ∪
   `meta.rate_limit_by` sans aucune arête. Le semis de L5 ne couvrait que les
   connecteurs basculés (`CHAIN_CONNECTORS`) ; les autres portent encore des accès et
   des quotas que la chaîne ne sait pas lire. Relevé sur la base servie le 2026-08-29 :
   **9 orgs** sur une clé fermée et **9 personnes** sur une clé à quotas nominatifs.

⚠️ **Hors du boot, délibérément** (ADR 0065, et la consigne du lot) : ce sont des
écritures de données sur une base PARTAGÉE avec la production. Commande explicite, **à
sec par défaut**, qu'on lance une fois et qu'on regarde.

⚠️ **Ne touche JAMAIS la ligne du coffre** — ni `share_mode`, ni `share_down`, ni
`meta`. C'est le geste qui a brûlé le 31/07/2026 (accorder à l'un fermait la clé pour
tous les autres) : ici il n'existe pas. L'existant reste en place, donc le retour à
l'ancien chemin reste possible tant que `OTO_L7_DECIDE` vaut `legacy`.

**Idempotence, et ce qu'elle protège.** Une arête n'est posée que si le couple
(instance, bénéficiaire) n'en porte **aucune, révoquée comprise**. Rejouer ne duplique
pas — et surtout ne **ressuscite** pas un accès retiré à la main : une arête « tout le
monde » révoquée est la façon dont on éteindra un free-tier, et un second passage de
cette commande ne doit pas la rallumer.

    # sur la box, après déploiement :
    ssh -i ~/.ssh/alexis root@<box> \
      "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.seed_everyone_edges"
    #   ^ dry-run par défaut : énumère et compte, n'écrit rien
    #     --apply                pour exécuter
    #     --connector <nom>      pour borner à un connecteur (bascule progressive)

Après `--apply`, la commande **re-énumère** et affiche le reste (attendu 0). C'est la
vérification, pas une politesse : elle prouve que le prédicat ne voit plus ce qu'il
vient de traiter.
"""
from __future__ import annotations

import sys

from oto_mcp import credentials_store, grants_chain
from oto_mcp.db import grants as db_grants

SOURCE = "migration:l7"


def _quota(valeur) -> dict:
    """La contrainte `quota` d'une arête. **Absent ⟹ contrainte VIDE**, jamais zéro :
    `resolve` retombe alors sur le défaut du registre (`quotas.quota_for`), c'est-à-dire
    exactement ce que l'ancien chemin faisait. Un `{'quota': 0}` dirait « illimité » par
    convention, mais figerait un choix que la ligne du coffre ne faisait pas."""
    try:
        n = int(valeur)
    except (TypeError, ValueError):
        return {}
    return {"quota": n} if n else {}


def _manquantes(connector: str | None = None) -> list[dict]:
    """Les arêtes à poser, dans un ordre déterministe (connecteur, label, scope).

    Deux passages sur la même instance, parce que les deux questions sont différentes :
    « cette clé est-elle ouverte à tous ? » (la ligne du coffre) et « ce scope
    a-t-il déjà une arête ? » (la table des grants)."""
    out: list[dict] = []
    cles = credentials_store.list_platform_credentials(connector)
    for provider in sorted({c["connector"] for c in cles}):
        for inst in credentials_store.list_platform_instances(provider):
            label, meta = inst["label"], (inst.get("meta") or {})
            ref = grants_chain.instance_ref(label, provider)
            rlb = meta.get("rate_limit_by") or {}
            ouverte = (inst.get("share_mode") != "closed"
                       and not (inst.get("share_down") or []))
            if ouverte and not db_grants.edge_exists(ref, *grants_chain.EVERYONE):
                out.append({"ref": ref, "connector": provider, "label": label,
                            "genre": "tout_le_monde",
                            "grantee": grants_chain.EVERYONE,
                            "constraints": _quota(meta.get("rate_limit"))})
            for scope in sorted(set(inst.get("share_down") or []) | set(rlb)):
                kind, _, ident = str(scope).partition(":")
                if kind not in ("user", "org") or not ident:
                    out.append({"ref": ref, "connector": provider, "label": label,
                                "genre": "hors_vocabulaire", "grantee": (kind, ident),
                                "constraints": {}})
                    continue
                if db_grants.edge_exists(ref, kind, ident):
                    continue
                out.append({"ref": ref, "connector": provider, "label": label,
                            "genre": "nominative", "grantee": (kind, ident),
                            "constraints": _quota(rlb.get(scope, meta.get("rate_limit")))})
    return out


def _afficher(manquantes: list[dict]) -> dict:
    par_genre: dict = {}
    for m in manquantes:
        par_genre.setdefault(m["genre"], []).append(m)
    for genre in ("tout_le_monde", "nominative", "hors_vocabulaire"):
        lot = par_genre.get(genre, [])
        if not lot:
            continue
        print(f"\n[{genre}] {len(lot)}")
        for m in lot:
            q = m["constraints"].get("quota", "défaut du registre")
            print(f"  {m['ref']:<38} → {m['grantee'][0]}:{m['grantee'][1]:<40} quota={q}")
    return par_genre


def main(apply: bool, connector: str | None) -> int:
    manquantes = _manquantes(connector)
    par_genre = _afficher(manquantes)
    a_poser = [m for m in manquantes if m["genre"] != "hors_vocabulaire"]
    hors = par_genre.get("hors_vocabulaire", [])
    if hors:
        # Nommé, pas avalé : un scope que la chaîne ne sait pas dire est une décision
        # à prendre, pas une ligne à ignorer. L'ancien chemin ne le résout pas non
        # plus (`_platform_grantee_scope` ne connaît que user et org), donc ne rien
        # poser est le comportement JUSTE — mais il faut le voir.
        print(f"\n⚠️  {len(hors)} scope(s) hors vocabulaire user:/org: — non posés "
              "(l'ancien chemin ne les résout pas non plus).")

    print(f"\n{len(a_poser)} arête(s) à poser.")
    if not a_poser:
        print("rien à faire — la chaîne dit déjà tout ce que le coffre dit.")
        return 0
    if not apply:
        print("dry-run — rien n'a été écrit (--apply pour exécuter)")
        return 0

    for m in a_poser:
        db_grants.insert_grant(
            resource_id=m["ref"],
            grantor_kind=grants_chain.PLATFORM_SCOPE[0],
            grantor_id=grants_chain.PLATFORM_SCOPE[1],
            grantee_kind=m["grantee"][0], grantee_id=m["grantee"][1],
            constraints=m["constraints"],
            # `manual` et pas une source à nous : le réconciliateur billing (L9) ne
            # touche QUE ses propres grants (0053-D6), et ceux-ci sont l'héritage
            # d'un humain. `created_by` porte la traçabilité du lot.
            source="manual", created_by=SOURCE)
    print(f"{len(a_poser)} arête(s) posée(s).")

    reste = [m for m in _manquantes(connector) if m["genre"] != "hors_vocabulaire"]
    print(f"vérification : {len(reste)} arête(s) manquante(s) "
          "(attendu 0 — le prédicat ne les voit plus).")
    return 0 if not reste else 1


def run(argv: list[str]) -> int:
    """Analyse la ligne de commande et exécute. **Séparée de `main` pour être
    testable** : une analyse d'arguments qu'on ne peut exercer que par le vrai point
    d'entrée finit testée en la réécrivant dans le test — et c'est alors le test qu'on
    vérifie, pas le programme."""
    cible = None
    if "--connector" in argv:
        i = argv.index("--connector")
        cible = argv[i + 1] if i + 1 < len(argv) else ""
        if not cible.strip() or cible.startswith("--"):
            # Un `--connector` sans valeur valait `None`, c'est-à-dire TOUS les
            # connecteurs : avec `--apply`, une vague devenait le semis complet.
            # Un drapeau qui borne ne doit jamais élargir quand il est mal tapé.
            print("--connector attend un nom de connecteur (ex. --connector serper).")
            return 2
    return main(apply="--apply" in argv, connector=cible)


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
