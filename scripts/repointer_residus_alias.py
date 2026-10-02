"""Reprend les RÉSIDUS d'une fusion de comptes passée : ce qui porte encore un ancien
identifiant de `sub_aliases` alors que son compte a été fusionné — oto-backend#439.

Deux familles de résidus, et un seul geste pour les deux :

- **ce que la fusion abandonnait** : les clés PERSONNELLES (coffre, entité `user` ou
  `member` = `org:sub`). L'AAD dérive de l'entité, donc la fusion les laissait sous
  l'ancien identifiant — invisibles pour leur propriétaire, que la résolution cherche
  sous son sub canonique. Elle les rechiffre désormais (`rekey_personal_credentials`) ;
- **ce qui a été écrit APRÈS la fusion** sous l'ancien identifiant : le journal REST
  attribuait le sub REVENDIQUÉ par le jeton (non résolu) jusqu'à ce que
  l'authentification publie le porteur canonique, et quelques colonnes n'étaient pas
  dans l'inventaire de la fusion.

Le geste est `db.users.repointer_patrimoine(conn, ancien, canonique)` — LE MÊME que
celui de `migrate_sub`, pas une copie : chaque étape ne touche que les lignes qui
portent encore l'ancien identifiant, d'où l'idempotence.

**Population** : chaque `old_sub` de `sub_aliases` SANS ligne `users`, dont la chaîne
d'alias aboutit à un compte vivant (`db.resolve_sub`). Sont ÉCARTÉS, et nommés :
- l'alias dont l'ancien identifiant a une ligne `users` — un compte RECRÉÉ après la
  fusion (#439 point 3) : deux comptes vivants, une décision humaine ;
- la chaîne qui ne se résout pas (`AliasNonResolvable`) ;
- le compte canonique EN PAUSE (même garde que `migrate_sub`).

## Deux passes, parce qu'une colonne sans index ne se traite pas d'un bloc

La première version faisait tout dans UNE transaction, constat compris. Jouée à blanc
en production le 30/09, elle est morte sur `statement_timeout` (15 s) dès le constat :
`SELECT count(*) … WHERE col = ANY(…)` sur une colonne SANS index balaie la table
entière — `tool_calls` (le journal : `effective_sub` et `view_as_sub` ne sont
délibérément pas indexés, un index s'y paie à chaque appel journalisé), `nodes`
(`owner_id` n'est indexé que derrière `owner_type` et hors lignes de tableau),
`doc_revisions`, `project_activity`… Le délai ne se relève pas : on découpe.

1. **La transaction** (tout-ou-rien, comme avant) : constat puis `repointer_patrimoine`
   pour tout ce qui se lit par un INDEX, ou vit dans une table BORNÉE par nature
   (`BORNEES`, chacune avec sa raison) — le coffre, les appartenances, le profil, les
   tables de réglages. Une erreur l'annule en entier.
2. **Les lots** (`PAR_LOTS`) : chaque colonne sans index d'une table à clé `id` est
   repointée par plages de `id` de `--taille-lot` (défaut 20 000), CHAQUE plage dans sa
   propre transaction bornée à 15 s — un parcours d'index de clé primaire, jamais un
   balayage. En `--apply` chaque lot est validé à sa fin : **un lot appliqué ne se
   défait pas si un lot suivant expire**. La reprise est la relance : un lot ne touche
   que les lignes qui portent ENCORE un ancien identifiant, donc un lot rejoué est sans
   effet, et `--depuis <table>:<id>` saute ce qui a déjà été parcouru. À blanc, chaque
   lot est rejoué puis annulé, et son compte de lignes est le constat de la table.

La passe 2 ne commence qu'après la validation de la passe 1 : une interruption laisse
toujours un état cohérent ligne par ligne (une ligne est repointée ou ne l'est pas),
et la relance termine le travail.

    python -m scripts.repointer_residus_alias            # constat + passe à blanc
    python -m scripts.repointer_residus_alias --apply
    python -m scripts.repointer_residus_alias --apply --depuis tool_calls:4200000

Sorties : 0 = fait (ou passe à blanc) ; 2 = paramètres ; 3 = la transaction a échoué
(rien d'écrit) ; 4 = un lot a échoué (les lots précédents sont validés en `--apply`,
la ligne de reprise est imprimée).
"""
from __future__ import annotations

import argparse
import sys

from oto_mcp import db
from oto_mcp.db import _connect
from oto_mcp.db.sub_aliases import AliasNonResolvable
from oto_mcp.db.users import (_MEMBERSHIP_TABLES, _PK_SUB_TABLES, _SUB_COLUMNS,
                              repointer_patrimoine)

_TIMEOUTS = ("SET LOCAL statement_timeout = '15s'", "SET LOCAL lock_timeout = '5s'")
TAILLE_LOT = 20_000

# Colonnes de `_SUB_COLUMNS` SANS index utilisable, dans une table à clé `id` : elles
# se repointent par plages de clé primaire, jamais d'un bloc. Garde :
# `tests/test_repointer_residus_alias.py` dérive les index du DDL et refuse toute
# colonne traitée d'un bloc qui n'en a pas (ni raison d'être bornée).
PAR_LOTS: dict[str, tuple[str, ...]] = {
    "tool_calls": ("effective_sub", "view_as_sub"),
    "nodes": ("owner_id",),
    "doc_revisions": ("edited_by",),
    "docs": ("created_by", "updated_by"),
    "project_activity": ("sub",),
    "usage_signals": ("sub", "resolved_by"),
    "org_member_events": ("sub", "actor_sub"),
    "user_api_tokens": ("revoked_by",),
    "user_datastores": ("owner_id",),
    "projects": ("owner_id", "created_by"),
    "functions": ("owner_id", "created_by"),
    "function_versions": ("proposed_by", "decided_by"),
    "recipes": ("owner_id", "created_by"),
    "recipe_versions": ("proposed_by", "decided_by"),
    "runner_jobs": ("sub",),
    "transcription_jobs": ("sub",),
    "runner_triggers": ("sub",),
    "runner_fleets": ("sub",),
    "orgs": ("created_by",),
    "org_invitations": ("invited_by", "accepted_sub", "declined_sub"),
    "org_groups": ("created_by",),
    "doctrine_library": ("published_by",),
    "project_files": ("created_by",),
    "doc_change_requests": ("resolved_by",),
    "scheduled_emails": ("created_by",),
    "grants": ("created_by",),
    "credential_disparitions": ("acteur_sub",),
    "portee_elargissements": ("acteur_sub",),
}

# Colonnes sans index traitées D'UN BLOC dans la transaction, parce que leur table est
# bornée par NATURE (et non par l'état d'aujourd'hui) — sans clé `id` à découper, et de
# toute façon petite : la garde exige une raison pour chacune.
_REGLAGES = ("réglage par org, équipe, connecteur ou famille : borné par le nombre "
             "d'orgs et de connecteurs, pas par l'activité")
_UNE_PAR_COMPTE = "au plus quelques lignes par compte, jamais par appel"
BORNEES: dict[tuple[str, str], str] = {
    ("connector_settings", "set_by"): _REGLAGES,
    ("org_disabled_tools", "disabled_by"): _REGLAGES,
    ("group_disabled_tools", "disabled_by"): _REGLAGES,
    ("org_model_subscription_limits", "updated_by"): _REGLAGES,
    ("org_model_subscription_modes", "updated_by"): _REGLAGES,
    ("option_comps", "granted_by"): _REGLAGES,
    ("option_comps", "entity_id"): _REGLAGES,
    ("org_entitlements", "granted_by"): _REGLAGES,
    ("org_entitlements", "sub"): _REGLAGES,
    ("billing_contracts", "granted_by"): "un contrat par org au plus",
    ("tenant_admins", "granted_by"): "admins de tenant : une poignée par tenant",
    ("tenant_admins", "sub"): "admins de tenant : une poignée par tenant",
    ("platform_instructions", "updated_by"): "deux lignes (clé = bloc A / bloc B)",
    ("org_instructions", "set_by"): "une ligne par guide d'org ou d'équipe",
    ("org_instruction_revisions", "set_by"): "versions des guides d'org : bornées par "
                                             "les éditions humaines, pas par les appels",
    ("connector_credentials", "set_by"): "une ligne par clé posée",
    ("connector_account_grants", "granted_by"): "prêts de compte : " + _UNE_PAR_COMPTE,
    ("connector_account_group_grants", "granted_by"): "prêts de compte : " + _UNE_PAR_COMPTE,
    ("resource_grants", "principal_id"): "partages nominatifs : gestes humains",
    ("resource_grants", "granted_by"): "partages nominatifs : gestes humains",
    ("unipile_pending", "sub"): "connexions en attente, purgées à l'aboutissement",
    ("unipile_operated_accounts", "owner_sub"): _UNE_PAR_COMPTE,
    ("apollo_phone_reveals", "sub"): "commandes en attente, trente jours de vie",
    ("users", "suspended_by"): "une ligne par compte",
    # Pré-traitée DANS la transaction (dédoublonnage de l'étape 2 quinquies) : la
    # sortir en lots séparerait le dédoublonnage du repointage qu'il prépare.
    ("outreach_sends", "sub"): "une relance par compte et par campagne",
    ("outreach_sends", "sent_by"): "une relance par compte et par campagne",
}


def colonnes_par_lots() -> frozenset:
    return frozenset((t, c) for t, cols in PAR_LOTS.items() for c in cols)


def _colonnes() -> list[tuple[str, str]]:
    """Toutes les colonnes que `repointer_patrimoine` repointe, sans doublon."""
    vues: dict[tuple[str, str], None] = {}
    for t, c in (list(_SUB_COLUMNS) + [(t, c) for t, c, _ in _PK_SUB_TABLES]
                 + [(t, "sub") for t, _ in _MEMBERSHIP_TABLES]
                 + [("user_account_profile", "sub"), ("orgs", "personal_of")]):
        vues[(t, c)] = None
    return list(vues)


def colonnes_d_un_bloc() -> list[tuple[str, str]]:
    """Ce que la TRANSACTION compte et repointe d'un bloc — tout sauf `PAR_LOTS`."""
    lots = colonnes_par_lots()
    return [tc for tc in _colonnes() if tc not in lots]


def population(conn) -> tuple[list[tuple[str, str]], list[str]]:
    """`([(ancien, canonique)], [écartés, en clair])`."""
    anciens = [r["old_sub"] for r in conn.execute(
        "SELECT a.old_sub, EXISTS (SELECT 1 FROM users u WHERE u.sub = a.old_sub) "
        "AS vivant FROM sub_aliases a ORDER BY a.old_sub").fetchall()
        if not r["vivant"]]
    recrees = [r["old_sub"] for r in conn.execute(
        "SELECT a.old_sub FROM sub_aliases a JOIN users u ON u.sub = a.old_sub "
        "ORDER BY a.old_sub").fetchall()]
    ecartes = [f"{s} : ancien identifiant RECRÉÉ (ligne users vivante) — à trancher "
               "à la main" for s in recrees]
    paires = []
    for ancien in anciens:
        try:
            canonique = db.resolve_sub(ancien)
        except AliasNonResolvable as refus:
            ecartes.append(f"{ancien} : chaîne non résolvable ({refus.motif})")
            continue
        pause = conn.execute("SELECT suspended_at FROM users WHERE sub = %s",
                             (canonique,)).fetchone()
        if pause and pause.get("suspended_at"):
            ecartes.append(f"{ancien} → {canonique} : compte canonique en pause")
            continue
        paires.append((ancien, canonique))
    return paires, ecartes


def constat(conn, anciens: list[str]) -> list[tuple[str, int]]:
    """`[("table.colonne", n)]` des lignes qui portent encore un ancien identifiant,
    pour les seules colonnes traitées D'UN BLOC (indexées ou bornées) ; puis les clés
    personnelles. Les colonnes `PAR_LOTS` se comptent lot par lot (`lots`)."""
    out = []
    for t, c in colonnes_d_un_bloc():
        n = conn.execute(f"SELECT count(*) AS n FROM {t} WHERE {c} = ANY(%s)",
                         (anciens,)).fetchone()["n"]
        if n:
            out.append((f"{t}.{c}", n))
    n = conn.execute("SELECT count(*) AS n FROM connector_credentials "
                     "WHERE entity_type = 'user' AND entity_id = ANY(%s)",
                     (anciens,)).fetchone()["n"]
    if n:
        out.append(("coffre.user", n))
    n = conn.execute(
        "SELECT count(*) AS n FROM connector_credentials WHERE entity_type = 'member' "
        "AND entity_id ~ '^[0-9]+:' "
        "AND substr(entity_id, strpos(entity_id, ':') + 1) = ANY(%s)",
        (anciens,)).fetchone()["n"]
    if n:
        out.append(("coffre.member", n))
    return out


def sql_lot(table: str, cols: tuple[str, ...]) -> str:
    """Un lot : les lignes d'une PLAGE de clé primaire qui portent encore un ancien
    identifiant, chaque colonne remplacée par son canonique. La plage est ce qui en fait
    un parcours d'index (`id` est la clé), jamais un balayage."""
    maj = ", ".join(f"{c} = COALESCE((SELECT m.new FROM m WHERE m.old = t.{c}), t.{c})"
                    for c in cols)
    porte = " OR ".join(f"t.{c} = ANY(%(olds)s)" for c in cols)
    return (f"WITH m(old, new) AS (SELECT * FROM unnest(%(olds)s::text[], "
            f"%(news)s::text[])) UPDATE {table} t SET {maj} "
            f"WHERE t.id >= %(a)s AND t.id < %(b)s AND ({porte})")


class LotEnEchec(RuntimeError):
    def __init__(self, table: str, debut: int, cause: BaseException):
        self.table, self.debut = table, debut
        super().__init__(f"lot {table}:{debut} en échec ({type(cause).__name__}: {cause})")


def lots(paires: list[tuple[str, str]], *, apply: bool, taille: int = TAILLE_LOT,
         depuis: "tuple[str, int] | None" = None, connect=_connect,
         dire=print) -> dict[str, int]:
    """Repointe `PAR_LOTS` par plages de `id`, une transaction bornée PAR LOT.

    `apply` : chaque lot est validé à sa fin (un lot suivant qui expire ne le défait
    pas) ; sinon rejoué puis annulé. `depuis=(table, id)` reprend : les tables qui la
    précèdent dans `PAR_LOTS` et les plages sous `id` sont sautées. Rend
    `{table: lignes}` ; lève `LotEnEchec` (qui nomme la reprise) au premier échec."""
    olds = [a for a, _ in paires]
    news = [n for _, n in paires]
    tables = list(PAR_LOTS)
    if depuis is not None and depuis[0] not in PAR_LOTS:
        raise ValueError(f"--depuis : table {depuis[0]!r} hors de PAR_LOTS")
    bilan: dict[str, int] = {}
    for table in tables:
        if depuis is not None and tables.index(table) < tables.index(depuis[0]):
            continue
        cols = PAR_LOTS[table]
        with connect() as conn:
            bornes = conn.execute(f"SELECT min(id) AS lo, max(id) AS hi FROM {table}"
                                  ).fetchone()
        lo, hi = bornes["lo"], bornes["hi"]
        if lo is None:
            continue
        if depuis is not None and table == depuis[0]:
            lo = max(lo, depuis[1])
        sql = sql_lot(table, cols)
        total = 0
        for a in range(lo, hi + 1, taille):
            try:
                with connect() as conn:
                    for s in _TIMEOUTS:
                        conn.execute(s)
                    n = conn.execute(sql, {"olds": olds, "news": news, "a": a,
                                           "b": a + taille}).rowcount or 0
                    if apply:
                        conn.commit()
                    else:
                        conn.rollback()
            except Exception as exc:
                raise LotEnEchec(table, a, exc) from exc
            total += n
        bilan[table] = total
        dire(f"  {table:28s} {','.join(cols):40s} {total}")
    return bilan


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="scripts.repointer_residus_alias")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--taille-lot", type=int, default=TAILLE_LOT)
    p.add_argument("--depuis", help="<table>:<id> — reprendre la passe par lots ici "
                   "(la transaction se rejoue, sans effet si déjà faite)")
    args = p.parse_args(argv)
    depuis = None
    if args.depuis:
        table, _, debut = args.depuis.partition(":")
        if table not in PAR_LOTS or not debut.isdigit():
            print(f"--depuis attend <table de PAR_LOTS>:<id>, reçu {args.depuis!r}",
                  file=sys.stderr)
            return 2
        depuis = (table, int(debut))

    # ── passe 1 : la transaction ────────────────────────────────────────────
    with _connect() as conn:
        for s in _TIMEOUTS:
            conn.execute(s)
        paires, ecartes = population(conn)
        print(f"{len(paires)} ancien(s) identifiant(s) à reprendre, "
              f"{len(ecartes)} écarté(s)")
        for e in ecartes:
            print(f"  écarté — {e}")
        if not paires:
            conn.rollback()
            return 0
        anciens = [a for a, _ in paires]
        print("\nconstat, colonnes indexées ou bornées (lignes sous un ancien identifiant) :")
        for nom, n in constat(conn, anciens) or [("rien", 0)]:
            print(f"  {nom:48s} {n}")
        total = {"rekeyed": 0, "collisions": 0, "illisibles": 0}
        try:
            for ancien, canonique in paires:
                bilan = repointer_patrimoine(conn, ancien, canonique,
                                             sauf=colonnes_par_lots())
                for k in total:
                    total[k] += bilan[k]
                if any(bilan.values()):
                    print(f"  {ancien} → {canonique} : coffre {bilan}")
            apres = constat(conn, anciens)
        except Exception as exc:
            conn.rollback()
            print(f"\nÉCHEC de la transaction ({type(exc).__name__}: {exc}) — rien "
                  "n'est écrit, la passe par lots n'a pas commencé", file=sys.stderr)
            return 3
        print(f"\nclés personnelles : {total['rekeyed']} rechiffrée(s), "
              f"{total['collisions']} laissée(s) (le compte canonique a la sienne), "
              f"{total['illisibles']} illisible(s)")
        print("reste après repointage (attendu : les seules clés laissées) :")
        for nom, n in apres or [("rien", 0)]:
            print(f"  {nom:48s} {n}")
        if args.apply:
            conn.commit()
            print("transaction validée.")
        else:
            conn.rollback()
            print("transaction rejouée puis ANNULÉE.")

    # ── passe 2 : les lots ──────────────────────────────────────────────────
    print(f"\ncolonnes sans index, par lots de {args.taille_lot} id "
          f"({'validés un à un' if args.apply else 'rejoués puis annulés'}) :")
    try:
        lots(paires, apply=args.apply, taille=args.taille_lot, depuis=depuis)
    except LotEnEchec as echec:
        print(f"\nÉCHEC : {echec}", file=sys.stderr)
        if args.apply:
            print("les lots précédents sont VALIDÉS ; reprendre par :\n  python -m "
                  f"scripts.repointer_residus_alias --apply --depuis "
                  f"{echec.table}:{echec.debut}", file=sys.stderr)
        return 4
    if not args.apply:
        print("\npasse à blanc — rien n'est écrit (--apply pour valider)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
