"""Smoke-test d'intégration du substrat factgraph (ADR 0008).

Hors suite pytest pure (nécessite une DB). Vérifie :
- le câblage `db.init_db()` (création idempotente du schéma factgraph) ;
- store : workspace, facts structurés (+ rejets), arêtes typées ;
- projection : scoring, file SQL, claim atomique, TTL, incrémental ;
- généricité : un workspace compta dans les mêmes tables.

Lancer :  DATABASE_URL=postgresql://poc:poc@localhost:5467/poc \
          .venv/bin/python -m scripts.smoke_factgraph
"""

from __future__ import annotations

from oto_mcp import db
from oto_mcp.factgraph import projection as proj
from oto_mcp.factgraph import schemas, store


def sec(t: str) -> None:
    print(f"\n{'─' * 68}\n{t}\n{'─' * 68}")


def main() -> None:
    sec("Câblage : db.init_db() (idempotent, crée le schéma factgraph)")
    db.init_db()
    db.init_db()  # 2e fois : doit être no-op sans erreur
    print("  ✓ init_db x2 OK")

    # repart propre sur le schéma factgraph
    with db._connect() as conn:
        conn.execute("TRUNCATE factgraph.workspace, factgraph.fact, factgraph.edge, "
                     "factgraph.prospect RESTART IDENTITY CASCADE")

    sec("Store : workspace prospection + facts structurés + arêtes")
    ws = store.get_or_create_workspace(org_id=999, kind="prospection", label="smoke")
    ent = store.add_fact(ws, "entreprise", {"siren": "552032534", "nom": "Théâtre du Rond-Point",
                                            "bp_an": 180, "idcc": "1285"})
    c1 = store.add_fact(ws, "contact", {"nom": "Marie", "tel": "+33611", "linkedin": "marie"})
    c2 = store.add_fact(ws, "contact", {"nom": "Jean", "linkedin": "jean"})
    store.link(c1, ent, "concerns")
    store.link(c2, ent, "concerns")
    print(f"  ✓ workspace #{ws}, entreprise #{ent}, contacts #{c1}/#{c2}")

    sec("Garde-fou structure (doit rejeter)")
    bads = [
        ("contact", {"tel": "0600"}, "nom requis manquant"),
        ("action", {"canal": "fax", "outcome": "x"}, "canal hors enum"),
        ("alien", {"x": 1}, "kind inconnu"),
    ]
    for kind, bad, why in bads:
        try:
            store.add_fact(ws, kind, bad)
            raise SystemExit(f"  ✗ {kind} {bad} aurait dû être rejeté")
        except schemas.SchemaError:
            print(f"  ✓ rejeté : {why}")
    try:
        store.link(ent, c1, "concerns")  # entreprise→contact interdit
        raise SystemExit("  ✗ arête entreprise→contact aurait dû être rejetée")
    except schemas.SchemaError:
        print("  ✓ rejeté : arête concerns entreprise→contact")

    sec("Projection : scoring + file SQL + claim atomique")
    # 3 prospects de plus pour la file
    for nom, bp, idcc, tel, li in [
        ("Festival d'Avignon", 250, "3090", None, "avignon"),
        ("Petite Cie", 40, "1285", "+33622", None),
        ("Mega Prod", 5000, "9999", "+33633", "mega"),
    ]:
        e = store.add_fact(ws, "entreprise", {"siren": str(900000000 + bp), "nom": nom,
                                              "bp_an": bp, "idcc": idcc})
        cd = {"nom": f"C {nom}"}
        if tel:
            cd["tel"] = tel
        if li:
            cd["linkedin"] = li
        store.link(store.add_fact(ws, "contact", cd), e, "concerns")

    n = proj.rebuild(ws)
    print(f"  rebuild: {n} prospects")
    for p in proj.queue(ws):
        print(f"    [{p['heat']:4}] fit={p['fit']:3}  {p['nom']}")

    a = proj.claim_next(ws, "alice")
    b = proj.claim_next(ws, "bob")
    assert a and b and a["siren"] != b["siren"], "collision de claim"
    print(f"  ✓ claim atomique : alice→{a['nom']} ({a['fit']}), bob→{b['nom']} ({b['fit']}), pas de collision")

    sec("TTL : claim zombie repris")
    with db._connect() as conn:
        conn.execute("UPDATE factgraph.prospect SET claimed_until = NOW() - interval '1 min' "
                     "WHERE claimed_by = 'alice'")
    again = proj.claim_next(ws, "carol")
    assert again and again["siren"] == a["siren"], "le zombie aurait dû être repris"
    print(f"  ✓ carol reprend le zombie d'alice ({again['nom']})")

    sec("Incrémental : action 'rdv' → reprojection → sort de la file")
    petite = [e for e in store.find(ws, "entreprise") if e["data"]["nom"] == "Petite Cie"][0]
    before = {p["nom"] for p in proj.queue(ws)}
    act = store.add_fact(ws, "action", {"canal": "appel", "outcome": "rdv"})
    store.link(act, petite["id"], "concerns")
    proj.project_entreprise(petite["id"])
    after = {p["nom"] for p in proj.queue(ws)}
    assert "Petite Cie" in (before - after), "Petite Cie aurait dû sortir de la file"
    print(f"  ✓ sortis de la file : {before - after}")

    sec("Généricité : workspace compta dans les mêmes tables (zéro DDL)")
    ws2 = store.get_or_create_workspace(org_id=999, kind="compta", label="smoke-compta")
    fac = store.add_fact(ws2, "facture", {"numero": "F-1", "montant_cents": 120000, "tiers": "ACME"})
    ecr = store.add_fact(ws2, "ecriture", {"libelle": "Vente", "montant_cents": 120000, "sens": "credit"})
    store.link(ecr, fac, "rapproche")
    print(f"  ✓ facture #{fac} ←rapproche— écriture #{ecr}, même schéma factgraph")

    print("\n✓ Intégration oto-backend validée.")


if __name__ == "__main__":
    try:
        main()
    finally:
        db._get_pool().close()  # évite les warnings « couldn't stop thread »
