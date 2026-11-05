"""Les clés du tenant devenu PRIMAIRE : des instances PLATEFORME ouvertes à tous.

Un tenant TIERS range ses clés partagées au scope `tenant` du coffre (`entity_id` = son
slug). Le tenant primaire n'en porte jamais : ses clés partagées sont les instances
PLATEFORME, et la cascade ne sonde pas son barreau tenant (`credentials_store.TENANT`,
`tenant_vault.rung_tenant`). L'import fait du tenant exporté le primaire de la cible
(`importation.controler_tenant`) : ses lignes `tenant` y arriveraient sans personne pour
les lire, et ses orgs perdraient en silence les clés que le tenant leur fournissait.

`convertir` les range à leur place, dans la transaction de l'import et après sa
relecture. Pour chaque ligne `('tenant', <slug>, <connecteur>, <compte>)` :

1. le secret, déchiffré sous l'AAD tenant, est reposé au scope plateforme par
   `credentials_store.set_credential` — rechiffré sous l'AAD plateforme, `meta` et
   `set_by` conservés, trace `_conversion` —, sous le label du compte s'il en porte un
   (« Main »), sinon sous le slug ;
2. la ligne plateforme est relue : ouverte (`share_mode='open'`, aucun partage),
   déchiffrable sous sa propre AAD avec la même empreinte, instance nommée ;
3. l'arête « tout le monde » (`grants_chain.EVERYONE`) est posée, le geste même de
   `seed_everyone_edges` ;
4. la ligne tenant est retirée par l'entonnoir du coffre (son instance s'archive) ;
5. les arêtes tenant→org SANS contrainte qui la désignaient s'archivent : l'instance
   plateforme ouverte sert déjà toutes les orgs.

Refus (`ConversionRefusee`), tous avant la première écriture, chacun nommé :
- le slug n'est pas le tenant primaire de la base ;
- un connecteur ne déclare pas le mode `platform` : sa clé n'aurait pas de place ici ;
- deux clés tenant pour un même connecteur (deux instances ouvertes : laquelle sert ?) ;
- un label qui contiendrait `:` ;
- une ligne tenant qui porte un partage ;
- une instance plateforme déjà là pour un connecteur converti ;
- une arête AVEC contrainte (un quota par org) ou un lien de projet qui désigne une
  ligne tenant : l'ouvrir à tous effacerait cette borne.

Idempotente : une base sans ligne tenant du primaire ne change pas.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from .. import credentials_store as cs
from .. import grants_chain, instance_refs, providers
from ..db import connector_instances
from ..db import grants as db_grants

TRACE = "import:perimetre"


class ConversionRefusee(RuntimeError):
    """Les clés du tenant primaire ne se convertissent pas : rien n'est écrit."""


def _empreinte(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _refs(conn, slug: str, connecteur: str, compte: str) -> list[str]:
    """Ce par quoi une arête ou un lien désigne la ligne tenant : son ref de barreau
    (avec et sans compte) et celui de son instance."""
    refs = {grants_chain.tenant_ref(slug, connecteur),
            instance_refs.make_tenant_ref(slug, connecteur, compte)}
    iid = connector_instances.instance_id_for_vault_row(cs.TENANT, slug, connecteur, compte,
                                                        conn=conn)
    if iid is not None:
        refs.add(instance_refs.make_instance_ref(iid))
    return sorted(refs)


def _refus(conn, lignes: list[dict], labels: dict[str, str],
           refs: dict[str, list[str]]) -> list[str]:
    refus = []
    sans_mode = sorted({r["connector"] for r in lignes
                        if "platform" not in getattr(providers.REGISTRY.get(r["connector"]),
                                                     "auth_modes", ())})
    if sans_mode:
        refus.append(f"{', '.join(sans_mode)} : pas de mode `platform`, leur clé tenant "
                     "n'aurait personne pour la lire — retirer la clé à la source, ou "
                     "donner au connecteur le mode `platform`")
    vus: dict[str, int] = {}
    for r in lignes:
        vus[r["connector"]] = vus.get(r["connector"], 0) + 1
    doubles = sorted(c for c, n in vus.items() if n > 1)
    if doubles:
        refus.append(f"{', '.join(doubles)} : plusieurs clés tenant pour un même connecteur")
    mauvais = sorted(l for l in labels.values() if ":" in l or not l.strip())
    if mauvais:
        refus.append(f"labels plateforme invalides : {mauvais}")
    for r in lignes:
        if r["share_down"] or r["share_mode"] != "open":
            refus.append(f"{r['connector']} : la ligne tenant porte un partage")
    for r in conn.execute("SELECT connector, entity_id FROM connector_credentials "
                          "WHERE entity_type = %s AND connector = ANY(%s)",
                          (cs.PLATFORM, sorted(labels))).fetchall():
        refus.append(f"{r['connector']} : instance plateforme déjà là (label "
                     f"{r['entity_id']!r})")
    tous = sorted({x for v in refs.values() for x in v})
    contraintes = conn.execute(
        "SELECT resource_id, grantee_kind, grantee_id FROM grants WHERE revoked_at IS NULL "
        "AND resource_id = ANY(%s) AND constraints <> '{}'::jsonb ORDER BY id",
        (tous,)).fetchall()
    if contraintes:
        refus.append("arêtes AVEC contrainte vers une ligne tenant : " + ", ".join(
            f"{e['resource_id']} → {e['grantee_kind']}:{e['grantee_id']}" for e in contraintes))
    liens = conn.execute(
        "SELECT count(*) AS n FROM project_links "
        "WHERE config->>'instance_ref' = ANY(%s) OR identity_ref = ANY(%s)",
        (tous, tous)).fetchone()["n"]
    if liens:
        refus.append(f"{liens} lien(s) de projet désignent une ligne tenant")
    return refus


def _relire(conn, connecteur: str, label: str, empreinte: str) -> None:
    ligne = conn.execute(
        "SELECT secret_enc, share_mode, share_down FROM connector_credentials "
        "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = ''",
        (cs.PLATFORM, label, connecteur)).fetchone()
    if ligne is None:
        raise ConversionRefusee(f"{connecteur} : ligne plateforme absente après la pose")
    if ligne["share_mode"] != "open" or ligne["share_down"]:
        raise ConversionRefusee(f"{connecteur} : l'instance plateforme n'est pas ouverte à tous")
    try:
        relu = _empreinte(cs._reveal(ligne, cs.PLATFORM, label, connecteur, ""))
    except RuntimeError:
        raise ConversionRefusee(f"{connecteur} : indéchiffrable sous l'AAD plateforme") from None
    if relu != empreinte:
        raise ConversionRefusee(f"{connecteur} : empreinte relue différente de l'original")
    if connector_instances.instance_id_for_vault_row(cs.PLATFORM, label, connecteur, "",
                                                     conn=conn) is None:
        raise ConversionRefusee(f"{connecteur} : instance plateforme non nommée")


def convertir(conn, slug: str) -> dict[str, str]:
    """Range les clés tenant de `slug`, tenant primaire de la base de `conn`, en instances
    plateforme ouvertes à tous, dans la transaction de `conn`. Rend `{connecteur: label}`
    des clés converties (`{}` s'il n'y en a pas). Lève `ConversionRefusee` sans rien
    écrire, ou en cours de relecture : la transaction de l'appelant annule alors tout."""
    primaire = conn.execute("SELECT slug FROM tenants WHERE id = 1").fetchone()
    if primaire is None or primaire["slug"] != slug:
        raise ConversionRefusee(f"{slug!r} n'est pas le tenant primaire de cette base "
                                f"({primaire and primaire['slug']!r})")
    lignes = conn.execute(
        "SELECT connector, account, secret_enc, meta, set_by, share_mode, share_down "
        "FROM connector_credentials WHERE entity_type = %s AND entity_id = %s "
        "ORDER BY connector, account FOR UPDATE", (cs.TENANT, slug)).fetchall()
    if not lignes:
        return {}
    labels = {r["connector"]: (r["account"] or slug) for r in lignes}
    refs = {r["connector"]: _refs(conn, slug, r["connector"], r["account"]) for r in lignes}
    refus = _refus(conn, lignes, labels, refs)
    if refus:
        raise ConversionRefusee("clés du tenant primaire : " + " ; ".join(refus))

    trace = {"from": f"tenant:{slug}", "at": datetime.now(timezone.utc).isoformat(),
             "by": TRACE}
    for r in lignes:
        c, compte, label = r["connector"], r["account"], labels[r["connector"]]
        secret = cs._reveal(r, cs.TENANT, slug, c, compte)
        empreinte = _empreinte(secret)
        meta = {**(r["meta"] or {}), "_conversion": {**trace, "to": f"platform:{label}"}}
        cs.set_credential(cs.PLATFORM, label, c, secret, set_by=r["set_by"], meta=meta,
                          conn=conn)
        del secret
        _relire(conn, c, label, empreinte)
        ref = grants_chain.instance_ref(label, c)
        if not db_grants.edge_exists(ref, *grants_chain.EVERYONE, conn=conn):
            db_grants.insert_grant(
                resource_id=ref, grantor_kind=grants_chain.PLATFORM_SCOPE[0],
                grantor_id=grants_chain.PLATFORM_SCOPE[1],
                grantee_kind=grants_chain.EVERYONE[0], grantee_id=grants_chain.EVERYONE[1],
                constraints={}, source="manual", created_by=TRACE, conn=conn)
        if not cs.clear_credential(cs.TENANT, slug, c, conn=conn, account=compte):
            raise ConversionRefusee(f"{c} : ligne tenant introuvable au retrait")
    db_grants.revoke_unconstrained_edges([x for v in refs.values() for x in v], conn)
    return labels
