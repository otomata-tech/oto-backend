"""Smoke-test de la couche service prospection (ADR 0008, étape 2B).

Exerce `factgraph.prospection.*` org-scopé — la surface que REST (`api_routes_scout`)
et MCP (`tools/scout`) appellent. Hors suite pure (nécessite une DB).

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5467/poc \
          OTO_CONFIG_DISABLE_SOPS=1 .venv/bin/python -m scripts.smoke_scout_service
"""
from __future__ import annotations

from oto_mcp import db
from oto_mcp.factgraph import prospection as p


def main() -> None:
    db.init_db()
    with db._connect() as conn:
        conn.execute("TRUNCATE factgraph.workspace, factgraph.fact, factgraph.edge, "
                     "factgraph.prospect RESTART IDENTITY CASCADE")

    ORG = 999
    print("→ add_prospect x3 (org-scopé, workspace auto)")
    a = p.add_prospect(ORG, "552032534", "Théâtre du Rond-Point", 180, "1285")
    b = p.add_prospect(ORG, "900000001", "Festival d'Avignon", 250, "3090")
    c = p.add_prospect(ORG, "900000002", "Petite Cie", 40, "1285")
    p.add_contact(ORG, a, "Marie", tel="+33611", linkedin="marie")
    p.add_contact(ORG, a, "Jean", linkedin="jean")

    print("\n→ queue(org) :")
    for it in p.queue(ORG):
        print(f"    [{it['heat']:4}] fit={it['fit']:3}  {it['nom']}")

    print("\n→ claim_next(org, 'alice') :")
    picked = p.claim_next(ORG, "alice")
    print(f"    alice obtient : {picked['nom']} (fit {picked['fit']})")
    assert picked["fact_id"] == a, "devrait claim le top (TRP, fit 100)"
    assert all(it["nom"] != "Théâtre du Rond-Point" for it in p.queue(ORG)), "TRP devrait sortir de la file"

    print("\n→ record_action(Petite Cie, appel/rdv) → statut dérivé :")
    detail = p.record_action(ORG, c, "appel", "rdv", note="RDV jeudi")
    print(f"    statut={detail['statut']}, actions={len(detail['actions'])}")
    assert detail["statut"] == "rdv"
    assert all(it["nom"] != "Petite Cie" for it in p.queue(ORG)), "Petite Cie (rdv) devrait sortir"

    print("\n→ get_detail(ORG, TRP) :")
    d = p.get_detail(ORG, a)
    print(f"    {d['nom']} — contacts={[x['nom'] for x in d['contacts']]}, "
          f"statut={d['statut']}, claimed_by={d.get('claimed_by')}")
    assert len(d["contacts"]) == 2 and d["claimed_by"] == "alice"

    print("\n→ isolation org : org 1000 ne voit rien d'org 999")
    assert p.queue(1000) == []
    print("    ✓ workspace par org étanche")

    print("\n→ anti-IDOR : org 1000 ne peut PAS lire/écrire un prospect d'org 999")
    for label, fn in [
        ("get_detail", lambda: p.get_detail(1000, a)),
        ("record_action", lambda: p.record_action(1000, a, "appel", "rdv")),
        ("add_contact", lambda: p.add_contact(1000, a, "Pirate")),
    ]:
        try:
            fn()
            raise SystemExit(f"    ✗ FAILLE : {label} cross-org a réussi !")
        except KeyError:
            print(f"    ✓ {label} cross-org rejeté (KeyError)")
    # le prospect d'org 999 n'a pas été pollué par la tentative
    assert len(p.get_detail(ORG, a)["contacts"]) == 2, "un contact pirate a été écrit !"
    print("    ✓ aucune écriture pirate")

    print("\n✓ Service prospection validé (2B).")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()
