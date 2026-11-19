#!/usr/bin/env python3
"""Fait tourner les jetons LONGS qui ont pu s'écrire en clair au journal d'accès.

⚠️ **À BLANC PAR DÉFAUT.** `--appliquer` écrit ; sans lui, rien ne bouge et le script
rend ce qu'il ferait, en COMPTES seulement — jamais un jeton, jamais une adresse.

## Pourquoi ce script existe

Jusqu'à la v1.391.0 (29/09/2026), le journal d'accès d'uvicorn écrivait le chemin des
requêtes tel quel : tout jeton porté DANS le chemin partait en clair dans journald
(`journal_secrets.MasqueCheminAcces` le masque depuis). journald est purgé des lignes
d'avant la bascule ; ce script ferme l'autre moitié — un jeton lu pendant la fenêtre
ne doit plus rien ouvrir.

Toutes les routes à jeton dans le chemin ne sont pas concernées :

    upload      `/api/upload/{token}` — 15 min, usage unique (`upload_tokens._TTL`) :
                expiré de lui-même, rien à faire.
    désinscription `/o/u/{token}`, `/o/d/{token}` — HMAC sans échéance, mais n'ouvre
                QUE la désinscription d'un compte ; le révoquer demanderait de faire
                tourner `OTO_MCP_OAUTH_STATE_SECRET`, qui signe aussi les uploads, le
                state OAuth et les masques du journal. Hors de ce script.
    apollo      `/api/receivers/apollo/phones/{token}` — servie masquée dès sa mise
                en prod (même version) : jamais écrite en clair.

Restent les deux jetons LONGS, les SECTIONS de ce script :

    docs         le lien public d'une page (`/p/d/…`, `/api/public/docs/…`), SANS
                 échéance. Aucune date d'émission : tout lien public vivant est tenu
                 pour exposé. Le geste est celui du produit (`db.set_doc_public`) : le
                 lien est retiré, l'ancien répond 404. Aucune réémission : le lien est
                 rangé haché (`docs.public_token_hash`) et ne se montre qu'à qui
                 l'émet — c'est au propriétaire de rendre la page publique à nouveau.
    invitations  `org_invitations` en attente, émises AVANT la bascule — 7 jours de
                 vie (`OTO_MCP_INVITE_TTL_DAYS`), seul son hash est rangé. Le geste est
                 celui du produit (`org_store.revoke_invitation` et ses deux voisins).
                 Aucune réémission, aucun mail : l'invité ne détient que le lien du
                 mail, c'est à un admin de réinviter.

L'état d'AVANT est écrit dans `--sauvegarde` dans le MÊME geste, avant la première
écriture — sans aucun jeton : restaurer un lien exposé serait défaire la rotation.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys
from collections import Counter

SECTIONS = ("docs", "invitations")

#: La bascule : v1.391.0 en prod, et la borne de la purge de journald.
BASCULE = datetime.datetime(2026, 9, 29, 9, 4, tzinfo=datetime.timezone.utc)


# ── docs ────────────────────────────────────────────────────────────────────

def docs(cur, rapport: dict, avant: list, appliquer: bool) -> None:
    from oto_mcp import db
    cur.execute("SELECT d.id, d.project_id, d.public_token_hash, p.owner_type, p.owner_id "
                "FROM docs d LEFT JOIN projects p ON p.id = d.project_id "
                "WHERE d.public_token_hash IS NOT NULL ORDER BY d.id")
    lignes = [dict(r) for r in cur.fetchall()]
    c = rapport.setdefault("docs", Counter())
    orgs, proprietaires = set(), set()
    for d in lignes:
        c["liens publics vivants (tous tenus pour exposés)"] += 1
        proprietaires.add((d["owner_type"], d["owner_id"]))
        if d["owner_type"] == "org":
            orgs.add(d["owner_id"])
        avant.append({"section": "docs", "id": d["id"], "project_id": d["project_id"],
                      "etait_public": True})
        rapport.setdefault("_liste", []).append(("doc public", d["id"], d["owner_type"]))
        if not appliquer:
            continue
        # Conditionnel sur le lien lu : un lien retiré ou refait entre-temps par son
        # propriétaire n'est pas touché.
        cur.execute("SELECT public_token_hash FROM docs WHERE id = %s", (d["id"],))
        r = cur.fetchone()
        if not r or r["public_token_hash"] != d["public_token_hash"]:
            c["  a bougé entre les deux passes (laissé)"] += 1
            continue
        db.set_doc_public(d["id"], False)
        c["  rendus privés"] += 1
        if d["project_id"] is not None:
            db.log_project_activity(
                d["project_id"], None, "doc.rotate_public_token",
                "partage public retiré (rotation de sécurité du 29/09/2026)")
    c["propriétaires distincts"] = len(proprietaires)
    c["orgs touchées"] = len(orgs)


# ── invitations ─────────────────────────────────────────────────────────────

def _jour(horodatage) -> str:
    """AAAA-MM-JJ, que la connexion rende un datetime ou déjà une chaîne ISO."""
    return str(horodatage)[:10]


def _revoquer(inv: dict) -> bool:
    """Le geste du produit pour CETTE forme d'invitation ; False si aucune ne va."""
    from oto_mcp import org_store
    if inv["group_id"] is not None:
        return org_store.revoke_group_invitation(inv["group_id"], inv["id"])
    if inv["org_id"] is not None:
        return org_store.revoke_invitation(inv["org_id"], inv["id"])
    if inv["source"] == "platform_admin":
        return org_store.revoke_platform_invitation(inv["id"])
    return False


def invitations(cur, rapport: dict, avant: list, appliquer: bool) -> None:
    cur.execute("SELECT id, org_id, group_id, email, org_role, group_role, invited_by, "
                "source, created_at, expires_at FROM org_invitations "
                "WHERE accepted_at IS NULL AND declined_at IS NULL "
                "AND expires_at > NOW() AND created_at < %s ORDER BY id", (BASCULE,))
    lignes = [dict(r) for r in cur.fetchall()]
    c = rapport.setdefault("invitations", Counter())
    orgs = set()
    for inv in lignes:
        c["en attente, émises avant la bascule"] += 1
        if inv["org_id"] is not None:
            orgs.add(inv["org_id"])
        avant.append({"section": "invitations", **inv})
        rapport.setdefault("_liste", []).append(
            ("invitation", inv["id"], f"expire le {_jour(inv['expires_at'])}"))
        if appliquer:
            if _revoquer(inv):
                c["  révoquées"] += 1
            else:
                c["  SANS GESTE (laissées)"] += 1
    c["orgs touchées"] = len(orgs)
    if lignes:
        c["dernière échéance naturelle"] = max(_jour(i["expires_at"]) for i in lignes)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--appliquer", action="store_true",
                    help="ÉCRIT en base. Sans lui : à blanc, rien n'est modifié.")
    ap.add_argument("--seulement", choices=SECTIONS, action="append",
                    help="Une section (répétable). Défaut : les deux.")
    ap.add_argument("--sauvegarde", default="/opt/oto-mcp/sauvegardes",
                    help="Où écrire l'état AVANT, dans le même geste que l'écriture.")
    ap.add_argument("--details", action="store_true",
                    help="Liste aussi les lignes visées (identifiants seuls).")
    args = ap.parse_args(argv[1:])
    sections = args.seulement or list(SECTIONS)

    from oto_mcp.db._conn import _connect

    rapport: dict = {}
    avant: list = []
    # Première passe : LIRE et compter, rien n'est écrit.
    with _connect() as conn, conn.cursor() as cur:
        for s in sections:
            globals()[s](cur, rapport, avant, False)

    if args.appliquer and avant:
        rep = pathlib.Path(args.sauvegarde)
        rep.mkdir(parents=True, exist_ok=True)
        quand = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        f = rep / f"rotation-jetons-journal-{quand}.json"
        f.write_text(json.dumps(avant, ensure_ascii=False, default=str))
        if not f.exists() or f.stat().st_size == 0:
            print("  ⛔ sauvegarde ILLISIBLE — rien n'est écrit en base.")
            return 2
        print(f"  ⤷ sauvegarde AVANT : {f} ({len(avant)} entrées)")
        rapport = {}
        with _connect() as conn, conn.cursor() as cur:
            for s in sections:
                globals()[s](cur, rapport, [], True)

    mode = "APPLIQUÉ" if args.appliquer else "À BLANC — rien n'a été écrit"
    print(f"=== {mode} === (bascule : {BASCULE:%d/%m/%Y %H:%M} UTC)")
    for s in sections:
        print(f"\n[{s}]")
        for cle, n in sorted((rapport.get(s) or {}).items()):
            print(f"  {cle:<55} {n}")
    if args.details and rapport.get("_liste"):
        print("\n[lignes visées]")
        for quoi, ident, info in rapport["_liste"]:
            print(f"  {quoi:<14} {ident:<10} {info}")
    if not args.appliquer:
        print("\n  Pour écrire : --appliquer.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
