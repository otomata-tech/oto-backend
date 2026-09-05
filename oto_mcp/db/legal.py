"""Store des acceptations légales — ré-exporté par `db/__init__`.

**Deux tables, et une seule source de vérité** (#487) :

- `legal_acceptance_events` — le JOURNAL. Une ligne par acceptation (sub, org, doc,
  version, date, IP, user-agent, contexte), jamais écrasée. **C'est la seule chose
  qu'on LIT** : la question du gate (« a-t-il accepté la version courante ? ») se
  pose à la ligne la plus récente de chaque document ;
- `legal_acceptances` — la PROJECTION « dernière acceptation par (sub, doc) »,
  l'ancienne forme de la trace. **Plus personne ne la lit ici.** On continue de
  l'écrire, et c'est TRANSITOIRE : prod et preprod partagent la base, et le code
  servi en production avant ce lot fait son `ON CONFLICT (sub, doc_slug)` dessus.
  Cesser de l'écrire ferait régresser ce que CE code lit, tant qu'il tourne.

⚠️ **L'écriture double est datée, pas un fallback.** Elle disparaît avec la table
(issue #507), au tag SUIVANT celui qui embarque ce lot — c'est-à-dire dès que la
production sert le code qui lit le journal. Un fallback est un chemin de secours
permanent ; ceci est un pont, et il a une date de démolition.

Trace de consentement UNIQUEMENT ; les métadonnées des docs (version courante,
libellé, URL) vivent dans `legal_docs.py`.
"""
from __future__ import annotations

from ._conn import _connect


def get_legal_acceptances(sub: str) -> dict[str, dict]:
    """slug → {version, accepted_at} de la **dernière** acceptation de `sub`, par doc.

    Lit le JOURNAL, jamais la projection. `DISTINCT ON` prend la ligne la plus
    récente côté serveur — rapatrier tout l'historique pour n'en garder qu'une ligne
    par document ferait grossir la lecture la plus empruntée du gate à chaque
    ré-acceptation.

    Le départage se fait sur `id` et pas seulement sur la date : `accepted_at` vaut
    `NOW()`, qui est l'horloge de la TRANSACTION — les trois documents d'un `accept`
    d'achat portent le même horodatage, et deux acceptations d'un même document dans
    une même transaction seraient indépartageables sans lui."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (doc_slug) doc_slug, version, accepted_at "
            "FROM legal_acceptance_events WHERE sub = %s "
            "ORDER BY doc_slug, accepted_at DESC, id DESC",
            (sub,),
        ).fetchall()
        return {r["doc_slug"]: {"version": r["version"], "accepted_at": r["accepted_at"]}
                for r in rows}


def record_legal_acceptances(sub: str, items: list[tuple[str, str]], *,
                             context: str | None = None,
                             org_id: int | None = None,
                             ip: str | None = None,
                             user_agent: str | None = None) -> None:
    """AJOUTE une ligne de journal par (slug, version) accepté, et met à jour la
    projection. Jamais d'écrasement côté journal.

    La projection perdait la preuve : accepter les CGV 2.0 effaçait la trace de
    l'acceptation des CGV 1.0. Ce qu'on doit pouvoir opposer, c'est « à telle date,
    depuis telle adresse, il a accepté telle version » — le journal le porte.

    ⚠️ **L'upsert de la projection est TRANSITOIRE** (cf. l'en-tête du module) : il
    n'existe que pour que le code encore servi en PRODUCTION continue de voir les
    acceptations données depuis la preprod. Il part avec la table, issue #507. Il est
    dans la MÊME transaction que le journal : pendant la fenêtre, les deux ne peuvent
    pas diverger.

    Les quatre satellites SITUENT l'acte et sont tous facultatifs : ils viennent du
    transport (`client_trace`) ou de la session, et une trace absente reste `NULL`
    plutôt qu'une valeur inventée. `org_id` = l'org de session au moment de
    l'acceptation, c'est-à-dire le PAYEUR (ADR 0043) quand le contexte est `purchase`.
    La projection, elle, ne les porte pas : elle n'a jamais eu ces colonnes, et lui en
    ajouter serait la faire vivre au lieu de la démolir.

    Une seule transaction pour tout le lot : les trois documents d'un achat sont
    acceptés d'un seul geste, ils ne peuvent pas l'être à moitié."""
    if not items:
        return
    with _connect() as conn:
        for slug, version in items:
            conn.execute(
                "INSERT INTO legal_acceptance_events "
                "(sub, org_id, doc_slug, version, context, ip, user_agent, accepted_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
                (sub, org_id, slug, version, context, ip, user_agent),
            )
            # Projection legacy — voir l'avertissement ci-dessus. À retirer avec #507.
            conn.execute(
                "INSERT INTO legal_acceptances (sub, doc_slug, version, accepted_at) "
                "VALUES (%s, %s, %s, NOW()) "
                "ON CONFLICT (sub, doc_slug) DO UPDATE SET "
                "version = EXCLUDED.version, accepted_at = EXCLUDED.accepted_at",
                (sub, slug, version),
            )


def list_acceptance_events(sub: str, *, doc_slug: str | None = None,
                           limit: int = 200) -> tuple[list[dict], int]:
    """L'HISTORIQUE complet des acceptations de `sub` — la preuve, pas l'état.

    Rend `(lignes, total)`. `get_legal_acceptances` répond « a-t-il accepté la version
    courante ? » et n'expose donc qu'une ligne par document ; ici on rend **chaque**
    acceptation avec ce qui la situe — l'adresse, l'agent, le contexte, l'org payeuse.
    C'est ce qu'on oppose à une contestation, et c'était en base sans aucune surface
    pour le sortir (oto#42 lot 2).

    ⚠️ **Le total est celui du jeu ENTIER, pas de la page rendue** (oto#42 règle 2) :
    un historique coupé à `limit` sans dire combien il en reste ferait écrire « il a
    accepté deux fois » à qui en compte deux sur trente.

    ⚠️ **Les quatre satellites NULS ne veulent pas dire « recopié ».** Le DDL du
    journal pose cette équivalence ; elle ne tient que dans un sens. La recopie de la
    projection (`_init.py`) les laisse bien à NULL — mais `record_legal_acceptances`
    aussi, dès qu'un appel arrive sans trace de transport. On ne peut donc pas
    DÉDUIRE l'origine d'une ligne, et cette lecture ne s'y risque pas : elle rend
    `null` tel quel, et c'est à la personne qui lit la preuve de savoir que `null`
    signifie « aucune trace enregistrée », jamais « aucune trace n'existait ».

    Le tri descend sur `(accepted_at, id)` pour la même raison que `DISTINCT ON` : les
    trois documents d'un achat portent l'horodatage de la TRANSACTION, donc le même,
    et seul `id` les ordonne."""
    where, args = ["sub = %s"], [sub]
    if doc_slug:
        where.append("doc_slug = %s")
        args.append(doc_slug)
    clause = " WHERE " + " AND ".join(where)
    borne = max(1, min(int(limit), 1000))
    with _connect() as conn:
        total = int(conn.execute(
            f"SELECT count(*) AS n FROM legal_acceptance_events{clause}",
            tuple(args)).fetchone()["n"])
        rows = conn.execute(
            "SELECT id, doc_slug, version, accepted_at, context, ip, user_agent, org_id "
            f"FROM legal_acceptance_events{clause} "
            "ORDER BY accepted_at DESC, id DESC LIMIT %s", tuple(args) + (borne,),
        ).fetchall()
    return list(rows), total


def get_tenant_legal_docs(tenant_slug: str) -> dict[str, dict]:
    """slug → {version, label, url} déclarés par CE tenant. Vide = aucun override —
    `legal_docs.docs_for` retombe alors sur `CURRENT_DOCS` tel quel."""
    if not tenant_slug:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT doc_slug, version, label, url FROM tenant_legal_docs WHERE tenant_slug = %s",
            (tenant_slug,),
        ).fetchall()
        return {r["doc_slug"]: {"version": r["version"], "label": r["label"], "url": r["url"]}
                for r in rows}


def set_tenant_legal_doc(tenant_slug: str, doc_slug: str, version: str, label: str, url: str) -> None:
    """Upsert l'override d'un tenant pour un slug — prend effet à la lecture suivante."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO tenant_legal_docs (tenant_slug, doc_slug, version, label, url, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW()) "
            "ON CONFLICT (tenant_slug, doc_slug) DO UPDATE SET "
            "version = EXCLUDED.version, label = EXCLUDED.label, url = EXCLUDED.url, "
            "updated_at = EXCLUDED.updated_at",
            (tenant_slug, doc_slug, version, label, url),
        )


def delete_tenant_legal_doc(tenant_slug: str, doc_slug: str) -> bool:
    """Retire l'override — le tenant retombe sur le défaut plateforme pour ce slug."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM tenant_legal_docs WHERE tenant_slug = %s AND doc_slug = %s",
            (tenant_slug, doc_slug),
        )
        return (cur.rowcount or 0) > 0
