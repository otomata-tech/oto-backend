"""Déplace des clés de connecteur du niveau ORG d'une org vers un niveau plus étroit :
les clés personnelles d'UN membre (`--vers membre`), ou une ÉQUIPE dédiée créée pour
l'occasion (`--vers equipe`).

Pourquoi un script : **restreindre un connecteur, c'est placer la clé au bon niveau**
(ADR 0053 D1). La réservation d'un connecteur à une partie des membres d'une org a
disparu le 24/09/2026 ; une org qui s'en servait pour réserver ses clés les voit
descendre au bon niveau par ce script.

Ce qu'il fait, pour chaque ligne de coffre `(org, <org>, <connecteur>, <compte>)` :

1. la DÉCHIFFRE avec l'AAD du niveau org (`credentials_store._aad`) ;
2. la RÉÉCRIT au niveau cible par l'entonnoir unique du coffre
   (`credentials_store._upsert`), donc re-chiffrée avec l'AAD de ce niveau — membre
   (`member`, `member_id(org, sub)`) ou équipe (`group`, `<id de l'équipe>`) —,
   `set_by` inchangé, `meta` inchangé plus une trace `_deplacement` ;
3. SUPPRIME la ligne d'org par `credentials_store._delete` (son instance s'archive,
   la nouvelle ligne reçoit la sienne — L6 pièce 2) ;
4. RELIT ce qu'il vient d'écrire avant de valider : déchiffrement au niveau cible,
   empreinte SHA-256 égale à celle du secret d'origine (on compare des empreintes,
   jamais des valeurs), ligne d'org absente. Un écart → ROLLBACK, sortie non nulle.

`--vers equipe` crée d'abord, DANS LA MÊME TRANSACTION, l'équipe `--equipe-nom` : le
membre `--sub` en est le chef (`group_admin`, et `created_by` — comme à la création
d'une équipe dans le produit, où le créateur devient chef) ; y entrent aussi, en
`group_member`, les membres de la liste EXPLICITE `--membres a,b,c`, et eux seuls.
⚠️ `--membres` est exigé : la table qui disait qui était autorisé (`connector_acl`) est
retirée (oto-backend#1169, son contenu est dans l'export attesté
`archives/connector_acl/…`), et le script ne devine pas — sans la liste, il ouvrirait
l'équipe à tous les membres en silence. Le chef seul : `--membres <sub du chef>`.
Chaque sub de la liste doit être membre de l'org.

⚠️ **Une clé d'équipe n'est lue que dans l'équipe ACTIVE** (`scope.current_group` :
jeton d'appel `_group=`, consultation, sinon l'équipe « maison » — UNE par sub, toutes
orgs confondues), ou par un pin explicite de l'instance (`_instance=`, lien de
projet). Une équipe neuve n'est l'équipe active de personne : ses membres ne résolvent
ses clés qu'après l'avoir rendue active. `--rendre-active` le fait, mais SEULEMENT pour
les membres dont l'org active est déjà cette org et qui n'ont aucune équipe active —
basculer une autre équipe active retirerait ses clés, et changer l'org active change
la boîte à outils MCP du compte (#1058) : ceux-là sont comptés, à régler à la main.

Tout dans **UNE transaction** : les lignes d'org visées sont verrouillées
(`FOR UPDATE`), `statement_timeout` et `lock_timeout` bornés. Pas de DDL.

**Passe à blanc par défaut** : elle REJOUE toutes les écritures (création de l'équipe
comprise) et la relecture dans la transaction, puis l'ANNULE — une contrainte qui
casserait en vrai casse déjà ici. `--apply` valide.

Refus (sortie non nulle, rien d'écrit en `--apply`) :
- le sub n'est pas membre de l'org ;
- `--vers equipe` sans `--membres`, ou un sub de `--membres` hors de l'org ;
- un connecteur demandé n'a pas de clé d'org, ou ne se lit pas au niveau cible
  (membre : `providers.require_credential("member", …)` ; équipe : le walker ne
  traverse le palier équipe que pour un connecteur org-partageable) ;
- `--vers membre` : le membre a déjà une clé du même connecteur et le déplacement ne
  peut pas coexister avec elle (mono-compte, ou ligne sans nom d'un multi-compte) — le
  script n'écrase ni ne renomme JAMAIS la clé du membre ; un compte nommé déjà pris
  est RENOMMÉ côté clé déplacée (`<compte>-org-<org>`, `principal-org-<org>` pour la
  ligne sans nom), et la sortie le dit ;
- `--vers equipe` : une équipe de ce nom existe déjà dans l'org (casse ignorée) ;
- un objet désigne encore une instance d'org déplacée (lien de projet
  `instance_ref`/`identity_ref`, arête `grants`, déclencheur ou flotte qui la
  nomme) : il casserait après le déplacement. `--force-orphan-bindings` passe outre,
  en le disant.

Ne sont PAS repris : les partages de l'instance d'org. `share_down` ne vit plus que
sur les clés plateforme (le cran BYO a été retiré, cf. `access/rbac.py`) ; la sortie
compte les lignes qui en portaient un. Les règles du compte (#1499) suivent son
instance. `share_mode` seul n'est pas un partage : `'open'` est sa
valeur par défaut.

**Aucun secret n'est affiché ni journalisé**, y compris en erreur : la sortie ne
contient que des comptes, des noms de connecteur, de compte et d'équipe, et (avec
`--show-members`) des subs.

    python -m scripts.deplacer_cles_org --org <id> --sub <sub> --connectors a,b \\
        --vers membre                                   # passe à blanc
    python -m scripts.deplacer_cles_org --org <id> --sub <sub> --connectors a,b \\
        --vers equipe --equipe-nom "<nom>" --membres a,b,c [--rendre-active] --apply

Sorties : 0 = fait (ou passe à blanc sans refus) ; 2 = paramètres, sub ou membre
hors de l'org ; 3 = refus (connecteur, collision, équipe existante, références) ; 4 =
relecture en échec (ROLLBACK).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from oto_mcp import credentials_store as cs, crypto, instance_refs, providers
from oto_mcp.db import _connect, connector_instances

STATEMENT_TIMEOUT = "15s"
LOCK_TIMEOUT = "5s"

OK, ERR_PARAMS, ERR_REFUS, ERR_RELECTURE = 0, 2, 3, 4

MEMBRE, EQUIPE = "membre", "equipe"
# Le coffre n'a pas de constante pour le palier équipe : `group_store` écrit
# `"group"` en dur (`get_group_secret`, `set_group_secret`). Même valeur ici.
GROUP = "group"


class RelectureEchouee(RuntimeError):
    """La relecture avant validation n'a pas retrouvé ce qui vient d'être écrit."""


@dataclass
class Cible:
    vers: str
    entity_type: str
    entity_id: str
    libelle: str


@dataclass
class Deplacement:
    connector: str
    source: str               # compte côté org ('' = mono-compte)
    cible: str                # compte côté cible
    renomme: bool = False


@dataclass
class Plan:
    deplacements: list = field(default_factory=list)
    refus: list = field(default_factory=list)        # (connecteur, motif)
    partages_non_repris: int = 0


def _empreinte(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _meta(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    return json.loads(raw or "{}")


def _table_existe(conn, nom: str) -> bool:
    row = conn.execute("SELECT to_regclass(%s) AS t", (nom,)).fetchone()
    return row["t"] is not None


def _compte_libre(base: str, pris: set, org: int) -> str:
    cible, i = f"{base}-org-{org}", 2
    while cible in pris:
        cible, i = f"{base}-org-{org}-{i}", i + 1
    return cible


def _lisible_au_niveau(vers: str, connector: str) -> Optional[str]:
    """Motif de refus si une clé de ce connecteur ne serait pas LUE au niveau cible."""
    if vers == MEMBRE:
        try:
            providers.require_credential(cs.MEMBER, connector)
        except ValueError:
            return "n'accepte pas de clé personnelle"
        return None
    try:
        providers.require_credential(GROUP, connector)
    except ValueError:
        return "n'accepte pas de clé d'équipe"
    # Le coffre accepte aussi un connecteur byo_user au palier équipe, mais le walker
    # ne le lit qu'org-partageable (`providers.is_org_shareable`) : une clé posée là
    # existerait sans jamais servir.
    if not providers.is_org_shareable(connector):
        return "une clé d'équipe ne serait jamais lue (connecteur non org-partageable)"
    return None


def _partage_reel(r) -> bool:
    return bool(r["share_down"] or r["share_mode"] == "closed")


def _planifier(conn, org: int, cible: Cible, connectors: list) -> Plan:
    from oto_mcp.connectors import cardinality
    plan = Plan()
    for connector in connectors:
        motif = _lisible_au_niveau(cible.vers, connector)
        if motif:
            plan.refus.append((connector, motif))
            continue
        lignes = conn.execute(
            "SELECT account, share_mode, share_down FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s "
            "ORDER BY account FOR UPDATE",
            (cs.ORG, str(org), connector)).fetchall()
        if not lignes:
            plan.refus.append((connector, "aucune clé au niveau de l'org"))
            continue
        existants = {r["account"] for r in conn.execute(
            "SELECT account FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s FOR UPDATE",
            (cible.entity_type, cible.entity_id, connector)).fetchall()}
        multi = cardinality.is_multi_account(connector, org)
        if existants and (not multi or "" in existants):
            plan.refus.append((connector, f"{cible.libelle} a déjà une clé qui ne peut pas "
                               "coexister avec la clé déplacée (mono-compte ou ligne "
                               "sans nom) — à régler à la main"))
            continue
        pris = set(existants)
        for r in lignes:
            source = r["account"]
            compte, renomme = source, False
            if existants:  # multi-compte, uniquement des comptes nommés côté cible
                base = source or "principal"
                compte, renomme = base, bool(source == "")
                if compte in pris:
                    compte, renomme = _compte_libre(base, pris, org), True
            pris.add(compte)
            plan.deplacements.append(Deplacement(connector, source, compte, renomme))
            if _partage_reel(r):
                plan.partages_non_repris += 1
    return plan


def _autres_membres(conn, org: int, sub: str) -> list:
    """Les autres membres de l'org : tous résolvent aujourd'hui une clé d'org (plus
    aucune réservation n'est lue depuis le 24/09/2026)."""
    return [r["sub"] for r in conn.execute(
        "SELECT sub FROM org_members WHERE org_id = %s AND sub <> %s ORDER BY sub",
        (org, sub)).fetchall()]


def _references(conn, org: int, deplacements: list) -> dict:
    """Objets qui désignent encore une instance d'org déplacée : {type: nombre}."""
    refs = set()
    for d in deplacements:
        refs.add(instance_refs.make_org_ref(org, d.connector, d.source))
        iid = connector_instances.instance_id_for_vault_row(
            cs.ORG, str(org), d.connector, d.source, conn=conn)
        if iid is not None:
            refs.add(instance_refs.make_instance_ref(iid))
    refs = sorted(refs)
    compte = {}
    if not refs:
        return compte
    if _table_existe(conn, "project_links"):
        compte["liens de projet"] = conn.execute(
            "SELECT count(*) AS n FROM project_links "
            "WHERE config->>'instance_ref' = ANY(%s) OR identity_ref = ANY(%s)",
            (refs, refs)).fetchone()["n"]
    if _table_existe(conn, "grants"):
        compte["arêtes grants"] = conn.execute(
            "SELECT count(*) AS n FROM grants WHERE resource_kind = 'connector_instance' "
            "AND revoked_at IS NULL AND resource_id = ANY(%s)", (refs,)).fetchone()["n"]
    for table, libelle in (("runner_triggers", "déclencheurs"), ("runner_fleets", "flottes")):
        if _table_existe(conn, table):
            compte[libelle] = conn.execute(
                f"SELECT count(*) AS n FROM {table} t WHERE EXISTS ("
                "SELECT 1 FROM unnest(%s::text[]) AS r(ref) "
                "WHERE strpos(row_to_json(t)::text, r.ref) > 0)", (refs,)).fetchone()["n"]
    return {k: v for k, v in compte.items() if v}


def _deplacer(conn, org: int, cible: Cible, deplacements: list) -> None:
    """Écrit puis relit. Lève `RelectureEchouee` sur tout écart (l'appelant annule)."""
    trace = {"from": f"org:{org}", "to": cible.vers,
             "at": datetime.now(timezone.utc).isoformat(),
             "by": "script:deplacer_cles_org"}
    empreintes = {}
    for d in deplacements:
        row = conn.execute(
            "SELECT secret_enc, meta, set_by FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (cs.ORG, str(org), d.connector, d.source)).fetchone()
        if conn.execute(
                "SELECT 1 FROM connector_credentials WHERE entity_type = %s "
                "AND entity_id = %s AND connector = %s AND account = %s",
                (cible.entity_type, cible.entity_id, d.connector, d.cible)).fetchone():
            raise RelectureEchouee(f"{d.connector} : le compte cible est apparu pendant le déplacement")
        secret = crypto.decrypt(row["secret_enc"], cs._aad(cs.ORG, str(org), d.connector, d.source))
        empreintes[(d.connector, d.cible)] = _empreinte(secret)
        meta = _meta(row["meta"])
        meta["_deplacement"] = dict(trace)
        cs._upsert(conn, cible.entity_type, cible.entity_id, d.connector, d.cible, secret,
                   row["set_by"], meta)
        cs._delete(conn, cs.ORG, str(org), d.connector, d.source)
        del secret
    for d in deplacements:
        row = conn.execute(
            "SELECT secret_enc FROM connector_credentials WHERE entity_type = %s "
            "AND entity_id = %s AND connector = %s AND account = %s",
            (cible.entity_type, cible.entity_id, d.connector, d.cible)).fetchone()
        if row is None:
            raise RelectureEchouee(f"{d.connector} : ligne cible absente après écriture")
        try:
            relu = crypto.decrypt(row["secret_enc"],
                                  cs._aad(cible.entity_type, cible.entity_id, d.connector, d.cible))
        except Exception:
            raise RelectureEchouee(f"{d.connector} : indéchiffrable au niveau cible") from None
        if _empreinte(relu) != empreintes[(d.connector, d.cible)]:
            raise RelectureEchouee(f"{d.connector} : empreinte différente de l'original")
        del relu
        if conn.execute(
                "SELECT 1 FROM connector_credentials WHERE entity_type = %s "
                "AND entity_id = %s AND connector = %s AND account = %s",
                (cs.ORG, str(org), d.connector, d.source)).fetchone():
            raise RelectureEchouee(f"{d.connector} : la ligne d'org est toujours là")


def _equipe_existe(conn, org: int, nom: str) -> bool:
    # Même garde que `groups._create_group` : collision insensible à la casse.
    return conn.execute(
        "SELECT 1 FROM org_groups WHERE org_id = %s AND lower(name) = lower(%s)",
        (org, nom)).fetchone() is not None


def _creer_equipe(conn, org: int, nom: str, chef: str, membres: list) -> int:
    """Miroir de `group_store.create_group` + `add_group_member` (le créateur est
    chef), écrit dans la transaction du script — ces fonctions ouvrent leur propre
    connexion et ne s'y prêteraient pas."""
    gid = int(conn.execute(
        "INSERT INTO org_groups (org_id, name, description, created_by) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (org, nom, "", chef)).fetchone()["id"])
    conn.execute("INSERT INTO org_group_members (group_id, sub, group_role) "
                 "VALUES (%s, %s, 'group_admin')", (gid, chef))
    for s in membres:
        conn.execute("INSERT INTO org_group_members (group_id, sub, group_role) "
                     "VALUES (%s, %s, 'group_member')", (gid, s))
    return gid


def _etat_actif(conn, org: int, subs: list) -> dict:
    """{sub: 'libre' | 'autre_equipe' | 'autre_org'} — ce qui décide si l'équipe neuve
    peut devenir active sans rien retirer au membre."""
    etat = {}
    for s in subs:
        org_active = conn.execute(
            "SELECT org_id FROM org_members WHERE sub = %s AND is_active", (s,)).fetchone()
        equipe_active = conn.execute(
            "SELECT group_id FROM org_group_members WHERE sub = %s AND is_active", (s,)).fetchone()
        if org_active is None or int(org_active["org_id"]) != org:
            etat[s] = "autre_org"
        elif equipe_active is not None:
            etat[s] = "autre_equipe"
        else:
            etat[s] = "libre"
    return etat


def _rendre_active(conn, org: int, gid: int, subs: list) -> None:
    # Miroir de `group_store.set_active_group` (index partiel « une seule active »),
    # restreint à des membres dont l'org active EST déjà `org` et sans équipe active :
    # l'org active ne bouge donc pas.
    for s in subs:
        conn.execute("UPDATE org_group_members SET is_active = (group_id = %s) WHERE sub = %s",
                     (gid, s))


def deplacer(org: int, sub: str, connectors: list, *, vers: str = MEMBRE,
             equipe_nom: Optional[str] = None, membres: Optional[list] = None,
             rendre_active: bool = False,
             apply: bool = False, show_members: bool = False,
             force_orphan_bindings: bool = False,
             out: Callable[[str], None] = print) -> int:
    connectors = sorted({c.strip() for c in connectors if c and c.strip()})
    if not connectors:
        out("aucun connecteur demandé (--connectors a,b,c)")
        return ERR_PARAMS
    if vers not in (MEMBRE, EQUIPE):
        out(f"--vers {MEMBRE}|{EQUIPE}")
        return ERR_PARAMS
    if vers == EQUIPE and not (equipe_nom or "").strip():
        out("--vers equipe demande --equipe-nom")
        return ERR_PARAMS
    if rendre_active and vers != EQUIPE:
        out("--rendre-active ne vaut que pour --vers equipe")
        return ERR_PARAMS
    liste = sorted({s.strip() for s in (membres or []) if s and s.strip()})
    if vers == EQUIPE and not liste:
        out("refus : --vers equipe exige --membres a,b,c, la liste explicite de qui entre "
            "dans l'équipe (le chef seul : --membres <sub du chef>). `connector_acl`, qui "
            "disait qui était autorisé, est retirée (oto-backend#1169) : son contenu est "
            "dans l'export attesté archives/connector_acl/…")
        return ERR_PARAMS
    if vers != EQUIPE and liste:
        out("--membres ne vaut que pour --vers equipe")
        return ERR_PARAMS
    mode = "APPLY" if apply else "DRY-RUN"
    with _connect() as conn:
        conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        membre = conn.execute(
            "SELECT org_role FROM org_members WHERE org_id = %s AND sub = %s",
            (org, sub)).fetchone()
        if membre is None:
            conn.rollback()
            out(f"[{mode}] refus : ce sub n'est pas membre de l'org {org}")
            return ERR_PARAMS

        autres = _autres_membres(conn, org, sub)
        hors_org = [s for s in liste if s != sub and s not in autres]
        if hors_org:
            conn.rollback()
            out(f"[{mode}] refus : {len(hors_org)} sub(s) de --membres ne sont pas membres "
                f"de l'org {org} : {', '.join(hors_org)}")
            return ERR_PARAMS
        equipe_refus = None
        gid = None
        membres_equipe: list = []
        if vers == MEMBRE:
            cible = Cible(MEMBRE, cs.MEMBER, cs.member_id(org, sub), "le membre")
        else:
            nom = equipe_nom.strip()
            membres_equipe = [s for s in liste if s != sub]
            if _equipe_existe(conn, org, nom):
                equipe_refus = f"une équipe « {nom} » existe déjà dans l'org {org}"
                cible = Cible(EQUIPE, GROUP, "", "l'équipe")
            else:
                gid = _creer_equipe(conn, org, nom, sub, membres_equipe)
                cible = Cible(EQUIPE, GROUP, str(gid), "l'équipe")

        plan = _planifier(conn, org, cible, connectors) if not equipe_refus else Plan()
        refs = _references(conn, org, plan.deplacements)

        if vers == MEMBRE:
            out(f"[{mode}] org {org} → clés personnelles d'un membre ({membre['org_role']}), "
                f"{len(connectors)} connecteur(s) demandé(s)")
        else:
            out(f"[{mode}] org {org} → équipe « {equipe_nom.strip()} » "
                f"({'à créer' if gid is not None else 'non créée'}), "
                f"{len(connectors)} connecteur(s) demandé(s)")
        if equipe_refus:
            out(f"  REFUS équipe : {equipe_refus}")
        for d in plan.deplacements:
            renom = " (renommé : compte déjà pris côté cible)" if d.renomme else ""
            out(f"  {d.connector} : compte {d.source or '(sans nom)'!s} → "
                f"{d.cible or '(sans nom)'}{renom}")
        for connector, motif in plan.refus:
            out(f"  REFUS {connector} : {motif}")
        if plan.partages_non_repris:
            out(f"  partage d'instance non repris sur {plan.partages_non_repris} ligne(s) "
                "(share_down ne vit plus que sur les clés plateforme)")
        out(f"  autres membres de l'org : {len(autres)}")

        actifs = []
        if vers == MEMBRE:
            if autres:
                for connector in connectors:
                    out(f"  {connector} : {len(autres)} membre(s) résolvent la clé d'org "
                        "aujourd'hui et la perdront")
            if show_members:
                for s in autres:
                    out(f"    membre : {s}")
        elif gid is not None:
            tous = [sub] + membres_equipe
            etat = _etat_actif(conn, org, tous)
            libres = [s for s in tous if etat[s] == "libre"]
            out(f"  membres de l'équipe : {len(tous)} (1 chef, {len(membres_equipe)} "
                "membre(s) désigné(s) par --membres) ; autres membres de l'org hors de "
                f"l'équipe : {len(autres) - len(membres_equipe)} (perdront ces clés)")
            out(f"  équipe active aujourd'hui : aucune {len(libres)}, "
                f"autre équipe {sum(etat[s] == 'autre_equipe' for s in tous)}, "
                f"org active différente {sum(etat[s] == 'autre_org' for s in tous)}")
            if rendre_active:
                actifs = libres
                _rendre_active(conn, org, gid, actifs)
                out(f"  --rendre-active : équipe rendue active pour {len(actifs)} membre(s) ; "
                    f"{len(tous) - len(actifs)} à régler à la main (autre équipe active, ou "
                    "org active différente)")
            out(f"  résoudront ces clés dès la validation : {len(actifs)} ; les autres "
                "seulement avec l'équipe active, `_group=`, ou un pin de l'instance")
            if show_members:
                for s in tous:
                    out(f"    membre : {s} ({etat[s]}{', rendue active' if s in actifs else ''})")

        if refs:
            out("  objets qui désignent une instance d'org déplacée : "
                + ", ".join(f"{k} {v}" for k, v in sorted(refs.items())))

        refuse = (bool(plan.refus) or bool(equipe_refus)
                  or (bool(refs) and not force_orphan_bindings))
        if refs and force_orphan_bindings:
            out("  --force-orphan-bindings : ces objets désigneront une instance archivée")
        if apply and refuse:
            conn.rollback()
            out(f"[{mode}] refusé, rien n'est écrit"
                + (" (références : --force-orphan-bindings pour passer outre)" if refs else ""))
            return ERR_REFUS

        try:
            _deplacer(conn, org, cible, plan.deplacements)
        except RelectureEchouee as e:
            conn.rollback()
            out(f"[{mode}] relecture en échec, ROLLBACK : {e}")
            return ERR_RELECTURE

        if apply:
            conn.commit()
            quoi = "" if gid is None else f" vers l'équipe #{gid}"
            out(f"[{mode}] {len(plan.deplacements)} clé(s) déplacée(s){quoi}, relues, commitées")
            return OK
        conn.rollback()
        out(f"[{mode}] {len(plan.deplacements)} déplacement(s) rejoué(s) et relu(s) puis "
            "ANNULÉ(S) — --apply pour committer")
        return ERR_REFUS if refuse else OK


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--org", type=int, required=True)
    p.add_argument("--sub", required=True, help="membre visé (vers membre) ou chef d'équipe")
    p.add_argument("--connectors", required=True, help="liste séparée par des virgules")
    p.add_argument("--vers", choices=(MEMBRE, EQUIPE), required=True)
    p.add_argument("--equipe-nom", help="nom de l'équipe à créer (--vers equipe)")
    p.add_argument("--membres", help="vers equipe : subs qui entrent dans l'équipe, séparés "
                   "par des virgules (exigé ; le chef seul : son propre sub)")
    p.add_argument("--rendre-active", action="store_true",
                   help="rend l'équipe active pour ses membres sans équipe active dans cette org")
    p.add_argument("--apply", action="store_true", help="valide (défaut : passe à blanc)")
    p.add_argument("--show-members", action="store_true")
    p.add_argument("--force-orphan-bindings", action="store_true")
    a = p.parse_args(argv)
    return deplacer(a.org, a.sub, a.connectors.split(","), vers=a.vers,
                    equipe_nom=a.equipe_nom,
                    membres=a.membres.split(",") if a.membres else None,
                    rendre_active=a.rendre_active,
                    apply=a.apply, show_members=a.show_members,
                    force_orphan_bindings=a.force_orphan_bindings)


if __name__ == "__main__":
    sys.exit(main())
