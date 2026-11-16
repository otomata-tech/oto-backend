"""Mesure, en LECTURE SEULE, de ce que la bascule des comptes (oto-backend#1499, étape 3,
lot L3) couperait ou changerait — lot L2. Joué une fois, à la main, sur la base de prod.

**La bascule mesurée.** L3 résout un compte par les règles d'accès
(`connector_account_rules`, `docs/partage-de-compte.md` § Les règles de comptes), sans
repli : compte nommé → compte personnel de l'appelant → compte de l'équipe ACTIVE (écart
(e)) → compte de l'org. Deux candidats à la même étape → `ambiguous_account`, sauf si le
défaut persistant (`oto_identity op=set`) désigne un compte dont l'appelant est le
PROPRIÉTAIRE (écart (c)). Le compte d'autrui ne sert que nommé. Disparaissent : le pointeur
Unipile d'un compte prêté, le passage de la main d'une clé personnelle suspendue, la clé
personnelle d'une autre org (#172), les prêts entre orgs (écart (d)), l'équipe héritée
d'un projet partagé (#480).

**Avant L1 comme après.** Les règles « voulues » sont calculées en mémoire par le calcul
même de la reprise (`regles_de_comptes.calculer_les_regles`), sur les instances telles que
la reprise les laisserait (`sous_instances.INSTANCES_SIMULEES` : une sous-instance simulée
par compte Unipile qui n'en a pas). Si les tables de L1 existent, le script le dit, compare
les règles en base aux règles voulues, et juge l'accès avec les règles manuelles en plus.

**Ce que le journal permet.** `tool_calls` porte, par appel, le compte qui a servi
(`args.instance`, posé au résolveur) et le palier (`key_mode`) : les appels des
`--jours` derniers jours (30 par défaut) disent qui, aujourd'hui, se sert d'un compte
auquel il n'aurait plus accès, et à quel niveau (`tools/effets` : lire → read, écrire →
write, envoyer/détruire → sensitive).

**Ce qui dépend de l'appel** (équipe active du moment, projet de l'appel, politique « clé
perso interdite », éligibilité exacte à la clé plateforme) n'est pas rejoué : le script
donne une BORNE et la nomme (`non_mesurable`).

Sortie : des NOMBRES seulement, par catégorie et par connecteur — jamais un sub, une
adresse, un nom d'org ni un identifiant. JSON sur la sortie standard, puis un résumé en
français sur la sortie d'erreur.

    lanceur --script scripts/mesure_bascule_comptes_1499.py [--jours 30]

Sorties : 0 = mesure rendue ; 3 = lecture impossible.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from typing import Iterator, Optional

NIVEAUX = ("none", "read", "write", "sensitive", "manage")
_RANG = {n: i for i, n in enumerate(NIVEAUX)}
#: Le niveau qu'une classe d'effet exige (`tools/effets.CLASSES`).
NIVEAU_DE_L_EFFET = {"lire": "read", "ecrire": "write", "envoyer": "sensitive",
                     "detruire": "sensitive"}
#: Le défaut d'oto-core pour un membre d'équipe sur un compte de l'équipe.
DEFAUT_EQUIPE = "sensitive"
#: Le défaut d'oto-core pour un membre de l'org sur un compte de l'org (la reprise pose
#: une règle `sensitive` explicite, écart (a)).
DEFAUT_ORG = "read"


@contextmanager
def lecture_seule(url: Optional[str] = None) -> Iterator:
    """Une connexion dont la session ET la transaction refusent toute écriture."""
    import psycopg
    from psycopg.rows import dict_row
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL absent")
    with psycopg.connect(url, options="-c default_transaction_read_only=on",
                         row_factory=dict_row) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '300s'")
            yield conn


# ── Lecture ──────────────────────────────────────────────────────────────────────

_JOURNAL = """
SELECT sub, org_id, tool, key_mode,
       args->>'instance' AS ref, args->>'op' AS op, args->>'method' AS method,
       args->>'name' AS cible, args->'arguments'->>'op' AS cible_op,
       args->'arguments'->>'method' AS cible_method, count(*) AS n
  FROM tool_calls
 WHERE created_at >= NOW() - make_interval(days => %s) AND kind = 'mcp'
   AND sub IS NOT NULL AND (args ? 'instance' OR key_mode IS NOT NULL)
 GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
"""

_POINTEURS = """
SELECT o.sub, o.provider,
       (EXISTS (SELECT 1 FROM connector_account_grants g
                 WHERE g.owner_sub = o.owner_sub AND g.provider = o.provider
                   AND g.grantee_sub = o.sub)
        OR EXISTS (SELECT 1 FROM connector_account_group_grants gg
                     JOIN org_group_members m ON m.group_id = gg.grantee_group_id
                    WHERE gg.owner_sub = o.owner_sub AND gg.provider = o.provider
                      AND m.sub = o.sub)) AS pret_vivant,
       EXISTS (SELECT 1 FROM unipile_accounts ua
                 JOIN org_members m ON m.org_id = ua.org_id AND m.sub = o.sub
                WHERE ua.sub = o.owner_sub AND ua.provider = o.provider
                  AND ua.disconnected_at IS NULL) AS meme_org
  FROM unipile_operated_accounts o
 WHERE o.owner_sub <> o.sub
"""

_HERITAGES = """
SELECT p.owner_type, p.owner_id
  FROM grants g JOIN projects p ON g.resource_id = 'project:' || p.id::text
 WHERE g.resource_kind = 'project_credentials' AND g.revoked_at IS NULL
"""

_COFFRE = """
SELECT entity_type, entity_id, connector, account,
       COALESCE(meta->>'suspended', '') = 'true' AS suspendue,
       COALESCE(meta->>'is_default', '') NOT IN ('', 'false', '0', 'null') AS defaut
  FROM connector_credentials
 WHERE entity_type IN ('member', 'group', 'org', 'platform')
"""


def lire(conn, jours: int = 30) -> dict:
    """Tout ce que la mesure lit, dans la transaction en lecture seule de `conn`."""
    from oto_mcp.db import regles_de_comptes, sous_instances

    cur = conn.cursor()
    simulees = sous_instances.INSTANCES_SIMULEES
    d = {"jours": jours}
    d["partages"] = regles_de_comptes.lire_les_partages(cur, simulees)
    d["l1"] = cur.execute("SELECT to_regclass('connector_account_rules') IS NOT NULL "
                          "AS l1").fetchone()["l1"]
    d["regles_en_base"] = (cur.execute(
        "SELECT instance_id, subject_user, subject_group, subject_org, level, origine, "
        "org_id FROM connector_account_rules").fetchall() if d["l1"] else [])
    d["sous_instances_simulees"] = cur.execute(
        f"SELECT count(*) AS n FROM {simulees} s WHERE s.id < 0").fetchone()["n"]
    d["instances"] = cur.execute(
        f"SELECT id, connector, owner_type, owner_id, account FROM {simulees} s "
        "WHERE s.revoked_at IS NULL").fetchall()
    d["membres_org"] = {(r["org_id"], r["sub"]) for r in cur.execute(
        "SELECT org_id, sub FROM org_members")}
    d["membres_equipe"] = cur.execute(
        "SELECT group_id, sub, is_active FROM org_group_members").fetchall()
    d["coffre"] = cur.execute(_COFFRE).fetchall()
    d["pointeurs"] = cur.execute(_POINTEURS).fetchall()
    d["heritages"] = cur.execute(_HERITAGES).fetchall()
    d["journal"] = cur.execute(_JOURNAL, (jours,)).fetchall()
    return d


# ── Calcul (pur) ─────────────────────────────────────────────────────────────────

def _par(compteur: Counter) -> dict:
    return dict(sorted(compteur.items()))


def _regles_effectives(d: dict, voulues) -> dict:
    """`{instance_id: [(user, group, org_entiere, org_id, niveau)]}` — les règles voulues
    (reprise), plus les règles manuelles en base si L1 est là (elles gagnent sur la
    reprise du même sujet, comme dans `reprendre_les_prets`)."""
    from oto_mcp.db.regles_de_comptes import NIVEAU_REPRIS, ORIGINE_REPRISE
    regles = {cle: (v["org_id"], NIVEAU_REPRIS) for cle, v in voulues.regles.items()}
    for r in d["regles_en_base"]:
        if r["origine"] != ORIGINE_REPRISE:
            cle = (r["instance_id"], r["subject_user"], r["subject_group"], r["subject_org"])
            regles[cle] = (r["org_id"], r["level"])
    par_instance: dict = defaultdict(list)
    for (iid, user, groupe, org_entiere), (org_id, niveau) in regles.items():
        par_instance[iid].append((user, groupe, org_entiere, org_id, niveau))
    return par_instance


def _comparer_l1(d: dict, voulues) -> Optional[dict]:
    if not d["l1"]:
        return None
    from oto_mcp.db.regles_de_comptes import ORIGINE_REPRISE
    reprises = {(r["instance_id"], r["subject_user"], r["subject_group"], r["subject_org"])
                for r in d["regles_en_base"] if r["origine"] == ORIGINE_REPRISE}
    voulu = set(voulues.regles)
    return {"regles_en_base": len(d["regles_en_base"]),
            "manuelles": sum(1 for r in d["regles_en_base"]
                             if r["origine"] != ORIGINE_REPRISE),
            "reprises_manquantes": len(voulu - reprises),
            "reprises_perimees": len(reprises - voulu)}


class _Contexte:
    """Les index en mémoire : coffre, équipes, instances."""

    def __init__(self, d: dict):
        self.groupes = d["partages"]["groupes"]                 # group_id → org_id
        self.membres_org = d["membres_org"]
        self.equipes_de: dict = defaultdict(set)                # sub → {group_id}
        self.active: dict = {}                                  # sub → group_id
        self.membres_de: dict = defaultdict(set)                # group_id → {sub}
        for r in d["membres_equipe"]:
            self.equipes_de[r["sub"]].add(r["group_id"])
            self.membres_de[r["group_id"]].add(r["sub"])
            if r["is_active"]:
                self.active[r["sub"]] = r["group_id"]
        self.membre: dict = defaultdict(list)    # (org, sub, c) → [(account, susp, defaut)]
        self.equipe: dict = defaultdict(list)    # (group_id, c) → [(account, defaut)]
        self.org: dict = defaultdict(list)       # (org_id, c) → [(account, defaut)]
        self.plateforme: set = set()
        for r in d["coffre"]:
            et, eid, c = r["entity_type"], r["entity_id"], r["connector"]
            if et == "member":
                org, _, sub = eid.partition(":")
                if org.isdigit():
                    self.membre[(int(org), sub, c)].append(
                        (r["account"], r["suspendue"], r["defaut"]))
            elif et == "group" and eid.isdigit():
                self.equipe[(int(eid), c)].append((r["account"], r["defaut"]))
            elif et == "org" and eid.isdigit():
                self.org[(int(eid), c)].append((r["account"], r["defaut"]))
            elif et == "platform":
                self.plateforme.add(c)
        self.instances = {r["id"]: r for r in d["instances"]}
        self.instance_par_cle = {(r["owner_type"], r["owner_id"], r["connector"],
                                  r["account"]): r["id"] for r in d["instances"]}

    def equipe_active_dans(self, sub: str, org: int) -> Optional[int]:
        g = self.active.get(sub)
        return g if g is not None and self.groupes.get(g) == org else None

    def equipes_dans(self, sub: str, org: int) -> list:
        return sorted(g for g in self.equipes_de.get(sub, ()) if self.groupes.get(g) == org)

    def perso_actives(self, org: int, sub: str, c: str) -> list:
        return [a for a in self.membre.get((org, sub, c), ()) if not a[1]]

    def autres_orgs(self, sub: str, c: str, org: int) -> bool:
        return any(o != org and s == sub and cc == c and self.membre[(o, s, cc)]
                   for (o, s, cc) in self.membre)

    def replis(self, sub: str, org: int, c: str) -> list:
        """Ce qui sert aujourd'hui sous le palier personnel (borne : l'équipe ACTIVE
        de l'appel n'est pas connue, on prend l'équipe active posée)."""
        from oto_mcp import providers
        out = []
        if c in providers.ORG_SHAREABLE_PROVIDERS:
            g = self.equipe_active_dans(sub, org)
            if g is not None and self.equipe.get((g, c)):
                out.append("equipe")
            if self.org.get((org, c)):
                out.append("org")
        con = providers.connector_for_provider(c)
        if con is not None and "platform" in con.auth_modes and c in self.plateforme:
            out.append("plateforme")
        return out


def _niveau(ctx: _Contexte, regles: dict, sub: str, iid: Optional[int],
            owner_type: str, owner_id: str) -> tuple[str, str]:
    """`(niveau, voie)` de `sub` sur un compte après bascule — sans repli."""
    if owner_type == "member" and owner_id.partition(":")[2] == sub:
        return "manage", "proprietaire"
    niveau, voie = "none", "aucune_regle"
    if owner_type == "group" and owner_id.isdigit() and sub in ctx.membres_de.get(
            int(owner_id), ()):
        niveau, voie = DEFAUT_EQUIPE, "membre_de_l_equipe"
    if owner_type == "org" and owner_id.isdigit() and (int(owner_id), sub) in ctx.membres_org:
        niveau, voie = DEFAUT_ORG, "membre_de_l_org"
    for user, groupe, org_entiere, org_id, n in regles.get(iid, ()):
        vaut = ((user is not None and user == sub)
                or (groupe is not None and groupe in ctx.equipes_de.get(sub, ()))
                or (org_entiere and (org_id, sub) in ctx.membres_org))
        if vaut and _RANG[n] > _RANG[niveau]:
            niveau, voie = n, "regle"
    return niveau, voie


def _effet(ligne: dict) -> str:
    """Le niveau qu'un appel du journal a exercé. Un outil absent de la table des effets
    est un outil en lecture (`tools/lecture`) ; un juge qui lit la base (`api_act`
    `call`) n'est pas rejoué : le doute est le plus contraignant."""
    from oto_mcp.tools import effets
    args: dict = {}
    if ligne["op"] is not None:
        args["op"] = ligne["op"]
    if ligne["method"] is not None:
        args["method"] = ligne["method"]
    if ligne["cible"] is not None:
        args["name"] = ligne["cible"]
        args["arguments"] = {k: v for k, v in (("op", ligne["cible_op"]),
                                               ("method", ligne["cible_method"]))
                             if v is not None}
    try:
        if effets.lit_la_base(ligne["tool"], args):
            return "sensitive"
        classe, _ = effets.effet(ligne["tool"], args)
    except effets.EffetNonDeclare:
        return "read"
    return NIVEAU_DE_L_EFFET[classe]


def _instance_du_ref(ctx: _Contexte, ref: str):
    """`(instance_id|None, owner_type, owner_id, connecteur)` d'un ref du journal, ou
    None pour un ref plateforme/tenant ou illisible."""
    from oto_mcp import instance_refs
    try:
        r = instance_refs.parse_ref(ref)
    except ValueError:
        return None
    if r.level == "inst":
        i = ctx.instances.get(r.instance_id)
        return (r.instance_id, i["owner_type"], i["owner_id"], i["connector"]) if i else None
    if r.level == "member":
        cle = ("member", f"{r.org_id}:{r.sub}", r.connector, r.account)
    elif r.level == "group":
        cle = ("group", str(r.group_id), r.connector, r.account)
    elif r.level == "org":
        cle = ("org", str(r.org_id), r.connector, r.account)
    else:
        return None
    return ctx.instance_par_cle.get(cle), cle[0], cle[1], r.connector


def _prets_entre_orgs(voulues) -> dict:
    coffre, unipile, autres = Counter(), Counter(), Counter()
    for e in voulues.non_reprises:
        if e["motif"] == "hors_org" and e["source"] == "pret_du_coffre":
            coffre[e["connecteur"]] += 1
        elif e["motif"] == "hors_org":
            unipile[e["canal"]] += 1
        else:
            autres[f"{e['source']}/{e['motif']}"] += 1
    return {"prets_du_coffre_entre_orgs": {"total": sum(coffre.values()),
                                           "par_connecteur": _par(coffre)},
            "prets_unipile_entre_orgs": {"total": sum(unipile.values()),
                                         "par_canal": _par(unipile)},
            "prets_non_repris_autres_motifs": _par(autres)}


def _cles_autre_org(ctx: _Contexte) -> dict:
    """#172 : la clé personnelle d'une AUTRE org sert aujourd'hui quand la personne n'a
    pas de clé locale active, pour un connecteur `personal_cross_org` mono-compte."""
    from oto_mcp import providers
    from oto_mcp.connectors import cardinality
    triplets, sans_repli, personnes = Counter(), Counter(), defaultdict(set)
    connecteurs = {c for (_, _, c) in ctx.membre if providers.is_personal_cross_org(c)}
    orgs_de: dict = defaultdict(set)
    for (o, s, c), comptes in ctx.membre.items():
        if c in connecteurs and comptes:
            orgs_de[(s, c)].add(o)
    for (org, sub) in ctx.membres_org:
        for c in connecteurs:
            if cardinality.is_multi_account(c, org) or ctx.perso_actives(org, sub, c):
                continue
            if not (orgs_de.get((sub, c), set()) - {org}):
                continue
            triplets[c] += 1
            personnes[c].add(sub)
            if not ctx.replis(sub, org, c):
                sans_repli[c] += 1
    return {"personne_x_org_x_connecteur": sum(triplets.values()),
            "personne_x_connecteur": sum(len(v) for v in personnes.values()),
            "sans_autre_repli": sum(sans_repli.values()),
            "par_connecteur": _par(triplets)}


def _ambigus(ctx: _Contexte) -> dict:
    """Personnes × connecteur multi-compte qui tomberaient en `ambiguous_account` : à
    l'étape atteinte, plusieurs comptes et aucun défaut POSSÉDÉ. `nouveaux` : un défaut
    posé au palier équipe ou org les départage aujourd'hui, il ne le fera plus (c) ;
    `deja` : ambigus dès aujourd'hui."""
    from oto_mcp import providers
    from oto_mcp.connectors import cardinality
    nouveaux, deja, borne = Counter(), Counter(), Counter()
    connecteurs = ({c for (_, _, c) in ctx.membre} | {c for (_, c) in ctx.equipe}
                   | {c for (_, c) in ctx.org})
    for (org, sub) in ctx.membres_org:
        for c in connecteurs:
            if not cardinality.is_multi_account(c, org):
                continue
            perso = ctx.perso_actives(org, sub, c) if providers.is_byo_user(c) else []
            if perso:
                if len(perso) > 1 and sum(1 for a in perso if a[2]) != 1:
                    deja[("personnel", c)] += 1
                continue
            if c not in providers.ORG_SHAREABLE_PROVIDERS:
                continue
            for g in ctx.equipes_dans(sub, org):
                if len(ctx.equipe.get((g, c), ())) > 1:
                    borne[c] += 1
                    break
            g = ctx.equipe_active_dans(sub, org)
            palier, comptes = ("equipe", ctx.equipe.get((g, c), [])) if g is not None \
                else ("org", [])
            if not comptes:
                palier, comptes = "org", ctx.org.get((org, c), [])
            if len(comptes) < 2:
                continue
            if sum(1 for a in comptes if a[1]) == 1:
                nouveaux[(palier, c)] += 1
            else:
                deja[(palier, c)] += 1

    def _forme(cpt: Counter) -> dict:
        par_palier, par_connecteur = Counter(), Counter()
        for (palier, c), n in cpt.items():
            par_palier[palier] += n
            par_connecteur[c] += n
        return {"total": sum(cpt.values()), "par_palier": _par(par_palier),
                "par_connecteur": _par(par_connecteur)}
    return {"nouveaux": _forme(nouveaux), "deja_ambigus_aujourd_hui": _forme(deja),
            "borne_haute_toute_equipe_de_la_personne": {
                "total": sum(borne.values()), "par_connecteur": _par(borne)}}


def _suspendues(ctx: _Contexte) -> dict:
    """Clé personnelle suspendue : aujourd'hui la main passe au palier du dessous ;
    après bascule, plus de repli."""
    avec, sans, par_repli = Counter(), Counter(), Counter()
    for (org, sub, c), comptes in ctx.membre.items():
        for _compte, suspendue, _ in comptes:
            if not suspendue:
                continue
            if (org, sub) not in ctx.membres_org:
                continue
            replis = ctx.replis(sub, org, c)
            if any(not s for _a, s, _d in comptes):
                replis = ["autre_compte_personnel"] + replis
            if ctx.autres_orgs(sub, c, org):
                replis.append("autre_org")
            if replis:
                avec[c] += 1
                par_repli[replis[0]] += 1
            else:
                sans[c] += 1
    return {"avec_repli_aujourd_hui": sum(avec.values()), "par_connecteur": _par(avec),
            "premier_repli": _par(par_repli), "sans_repli": sum(sans.values())}


def _pointeurs(d: dict) -> dict:
    total, par_canal, etat = 0, Counter(), Counter()
    for r in d["pointeurs"]:
        total += 1
        par_canal[r["provider"]] += 1
        etat["pret_vivant_meme_org" if r["pret_vivant"] and r["meme_org"]
             else "pret_vivant_entre_orgs" if r["pret_vivant"] else "pret_disparu"] += 1
    return {"total": total, "par_canal": _par(par_canal), "par_etat": _par(etat)}


def _heritages(d: dict, ctx: _Contexte) -> dict:
    par_proprio, equipes = Counter(), set()
    for r in d["heritages"]:
        par_proprio[r["owner_type"]] += 1
        if r["owner_type"] == "group" and str(r["owner_id"]).isdigit():
            equipes.add(int(r["owner_id"]))
    connecteurs = Counter()
    for (g, c), comptes in ctx.equipe.items():
        if g in equipes:
            connecteurs[c] += len(comptes)
    return {"aretes_vivantes_par_proprietaire_du_projet": _par(par_proprio),
            "equipes_heritables": len(equipes),
            "equipes_heritables_avec_un_compte": len(
                {g for (g, _c), comptes in ctx.equipe.items() if g in equipes and comptes}),
            "comptes_d_equipe_heritables_par_connecteur": _par(connecteurs)}


def _journal(d: dict, ctx: _Contexte, regles: dict, pointeurs_subs: set) -> dict:
    """Les appels des `jours` derniers jours : paliers, et ce que la bascule couperait."""
    par_mode, plateforme_tenant = Counter(), Counter()
    pertes_appels, pertes_couples = Counter(), defaultdict(set)
    par_connecteur = defaultdict(Counter)
    insuffisant_couples, insuffisant_appels = set(), 0
    equipe_non_active_appels, unipile_pointeur_appels = 0, 0
    for r in d["journal"]:
        n = r["n"]
        par_mode[r["key_mode"] or "inconnu"] += n
        ref = r["ref"]
        if not ref:
            continue
        if ref.startswith(("platform:", "tenant:")):
            plateforme_tenant[ref.split(":", 1)[0]] += n
            continue
        cible = _instance_du_ref(ctx, ref)
        if cible is None:
            continue
        iid, ot, oid, connecteur = cible
        sub = r["sub"]
        if connecteur == "unipile" and sub in pointeurs_subs:
            unipile_pointeur_appels += n
        if ot == "member" and oid.partition(":")[2] == sub:
            org_cle = int(oid.partition(":")[0])
            if r["org_id"] is not None and org_cle != r["org_id"]:
                motif = "cle_perso_d_une_autre_org"
            else:
                continue
        else:
            niveau, voie = _niveau(ctx, regles, sub, iid, ot, oid)
            if ot == "group" and voie == "membre_de_l_equipe" \
                    and ctx.active.get(sub) != int(oid):
                equipe_non_active_appels += n
            if niveau == "none":
                motif = {"member": "pret_sans_regle", "group": "equipe_hors_appartenance",
                         "org": "org_hors_appartenance"}.get(ot, "autre")
            else:
                if _RANG[_effet(r)] > _RANG[niveau]:
                    insuffisant_couples.add((sub, ref))
                    insuffisant_appels += n
                continue
        pertes_appels[motif] += n
        pertes_couples[motif].add((sub, ref))
        par_connecteur[motif][connecteur] += n
    return {
        "appels_par_palier": _par(par_mode),
        "appels_plateforme_tenant_par_ref": _par(plateforme_tenant),
        "pertes_d_acces": {m: {"appels": pertes_appels[m],
                               "personne_x_compte": len(pertes_couples[m]),
                               "appels_par_connecteur": _par(par_connecteur[m])}
                           for m in sorted(pertes_appels)},
        "niveau_inferieur_a_l_exerce": {"personne_x_compte": len(insuffisant_couples),
                                        "appels": insuffisant_appels},
        "borne_equipe_non_active_au_moment_de_la_mesure": equipe_non_active_appels,
        "borne_unipile_appels_de_personnes_a_pointeur": unipile_pointeur_appels,
    }


NON_MESURABLE = [
    "équipe active de chaque appel : elle se choisit par appel (jeton, vue, accueil) ; "
    "le script prend l'équipe active posée aujourd'hui, et borne par toute équipe de la "
    "personne dans l'org (comptes_ambigus.borne_haute…, journal.borne_equipe_non_active…)",
    "projet de l'appel (#480) : l'héritage se décide au `_project=` ; borne = les arêtes "
    "vivantes et les équipes héritables, et, au journal, les appels d'un compte d'équipe "
    "ou d'org par quelqu'un qui n'en est pas membre",
    "politique « clé perso interdite » (#1239) et éligibilité exacte à la clé plateforme "
    "(share_down, quotas, option payante) : non rejouées, les replis comptés sont un "
    "maximum",
    "prêt Unipile opéré par pointeur : le journal note la clé Unipile qui a servi, pas le "
    "compte opéré ; borne = appels Unipile des personnes qui ont un pointeur vers autrui",
    "cardinalité multi-compte surchargée par org : lue comme au serveur (base), sinon "
    "défaut du registre",
]


def mesurer(d: dict) -> dict:
    """La mesure, PURE, sur ce que `lire` a lu. Des nombres seulement."""
    from oto_mcp.db.regles_de_comptes import calculer_les_regles
    voulues = calculer_les_regles(d["partages"])
    ctx = _Contexte(d)
    regles = _regles_effectives(d, voulues)
    sujets = Counter("personne" if u else "equipe" if g else "org"
                     for (_i, u, g, _o) in voulues.regles)
    out = {"jours": d["jours"],
           "tables_l1_en_base": bool(d["l1"]),
           "sous_instances_unipile_simulees": d["sous_instances_simulees"],
           "regles_voulues": {"total": len(voulues.regles), "par_sujet": _par(sujets),
                              "comparaison_avec_la_base": _comparer_l1(d, voulues)}}
    out.update(_prets_entre_orgs(voulues))
    out["cles_perso_d_une_autre_org"] = _cles_autre_org(ctx)
    out["comptes_ambigus"] = _ambigus(ctx)
    out["cles_perso_suspendues"] = _suspendues(ctx)
    out["pointeurs_unipile_vers_autrui"] = _pointeurs(d)
    out["equipes_heritees_projet_partage"] = _heritages(d, ctx)
    out["journal"] = _journal(d, ctx, regles, {r["sub"] for r in d["pointeurs"]})
    out["non_mesurable"] = NON_MESURABLE
    return out


def resume(m: dict) -> str:
    """Le résumé lisible, en français."""
    j = m["journal"]
    pertes = j["pertes_d_acces"]
    amb = m["comptes_ambigus"]
    lignes = [
        f"Mesure de la bascule des comptes (#1499, L2) — journal des {m['jours']} derniers jours.",
        "Tables des règles (L1) en base : " + ("oui" if m["tables_l1_en_base"] else "non")
        + f" ; règles voulues : {m['regles_voulues']['total']}"
        + f" ; sous-instances Unipile simulées : {m['sous_instances_unipile_simulees']}.",
        f"Prêts du coffre entre orgs (non repris) : {m['prets_du_coffre_entre_orgs']['total']}.",
        f"Prêts Unipile entre orgs (non repris) : {m['prets_unipile_entre_orgs']['total']}.",
        "Clé personnelle d'une autre org (#172) servie faute de clé locale : "
        f"{m['cles_perso_d_une_autre_org']['personne_x_connecteur']} personne×connecteur, "
        f"dont {m['cles_perso_d_une_autre_org']['sans_autre_repli']} sans autre repli.",
        f"Nouveaux cas d'ambiguïté (défaut d'équipe ou d'org non possédé) : "
        f"{amb['nouveaux']['total']} ; déjà ambigus aujourd'hui : "
        f"{amb['deja_ambigus_aujourd_hui']['total']}.",
        f"Clés personnelles suspendues avec un repli aujourd'hui : "
        f"{m['cles_perso_suspendues']['avec_repli_aujourd_hui']}.",
        f"Pointeurs Unipile vers le compte d'autrui : "
        f"{m['pointeurs_unipile_vers_autrui']['total']}.",
        f"Équipes héritables par un projet partagé : "
        f"{m['equipes_heritees_projet_partage']['equipes_heritables']}.",
        "Appels par palier : " + ", ".join(f"{k} {v}" for k, v in
                                           j["appels_par_palier"].items()) + ".",
        "Appels qui perdraient l'accès : " + (", ".join(
            f"{k} {v['appels']} ({v['personne_x_compte']} personne×compte)"
            for k, v in pertes.items()) or "aucun") + ".",
        f"Niveau calculé inférieur à l'exercé : "
        f"{j['niveau_inferieur_a_l_exerce']['personne_x_compte']} personne×compte.",
        "Non mesurable hors appel réel : " + "; ".join(m["non_mesurable"]) + ".",
    ]
    return "\n".join(lignes)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--jours", type=int, default=30,
                   help="profondeur du journal d'appels lu (défaut 30)")
    args = p.parse_args(argv)
    try:
        with lecture_seule() as conn:
            donnees = lire(conn, args.jours)
    except Exception as e:  # noqa: SILENT — la mesure n'a pas eu lieu : dit et code 3
        print(f"lecture impossible : {type(e).__name__}: {e}", file=sys.stderr)
        return 3
    m = mesurer(donnees)
    print(json.dumps(m, indent=2, ensure_ascii=False, default=str))
    print(resume(m), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
