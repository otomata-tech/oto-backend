"""L'audience des emails d'ACTIVATION d'un tenant : à quelle ÉTAPE en est chaque
personne, et à qui un email est dû.

Ce n'est PAS la relance de plateforme (`db/outreach.py`), et la différence est le
point de ce module. La relance est écrite par NOUS à NOS comptes, et elle écarte les
comptes de tout tenant tiers — leur écrire serait parler par-dessus un partenaire, dans
son produit. L'activation est l'inverse : c'est le TENANT qui écrit à SES comptes, dans
SA marque, depuis SON adresse, parce qu'il l'a déclaré (`OTO_ACTIVATION`, cf.
`activation.py`). On ne sélectionne donc QUE les comptes du tenant configuré, et
`_AUDIENCE_SQL` de la relance reste intact, garde-fou partenaire compris.

## Les étapes, lues dans le journal

Une personne (une boîte mail : le refus, le déjà-envoyé et l'activité se lisent sur
TOUS ses comptes, `p.subs`) avance par trois jalons, chacun ouvrant l'email suivant :

- **`connect`** — jamais branchée : aucune ligne `mcp` NI `protocol`. Une ligne
  `protocol` est un `initialize` : la personne a ajouté le connecteur sans rien
  demander encore ; lui écrire « ajoutez le connecteur » serait faux. Une ligne `rest`
  (le tableau de bord) ne compte pas : ouvrir l'application n'est pas brancher son
  agent.
- **`first-process`** — branchée depuis 48 h, aucun déroulé de procédure terminé
  (`done`, même définition que la case « First process run » de l'écran d'accueil :
  un `run_start` portant une procédure, clos `done`), ET aucun appel d'agent depuis
  48 h. Ce dernier point compte : beaucoup travaillent sans jamais ouvrir de run, et
  un email « lancez votre premier processus » à quelqu'un qui travaille est du bruit.
- **`recurring`** — un déroulé terminé, mais rien de régulier : aucun agent
  programmé actif à son nom ET pas deux déroulés terminés des jours différents. Une
  tâche programmée côté Claude ne se voit pas d'ici ; deux jours distincts sont la
  trace qu'elle laisse.

## Ce qui vaut pour toutes les étapes

- **Une fois par étape et par personne** (campagne `activation-<étape>:<tenant>`).
- **48 h entre deux emails d'activation**, toutes étapes confondues — lu ICI, dans la
  requête, pas dans une consigne.
- **Fenêtres** : `connect` couvre `window_days` (le rattrapage des inscrits qui
  attendent déjà) ; les étapes suivantes ne valent que pendant `sequence_days` après
  l'inscription (la séquence des premiers jours).
- **Écartés** : suspendus ; opérateurs de plateforme (`users.role` admin|super_admin)
  ET leurs adresses `+suffixe` (nos comptes d'essai) ; domaines que le tenant déclare
  comme les siens (`exclude_domains`) ; désinscrits (`outreach_optouts`).

Le journal et le refus sont ceux de la relance (`outreach_sends`, `outreach_optouts`) :
aucune table neuve. ⚠️ Le refus est donc COMMUN aux deux canaux.
"""
from __future__ import annotations

from ._conn import _connect
from .tenants import _SUB_TENANT_SQL, _TENANT_PREF_SQL
from .usage import _runs_from_journal
from .. import tenancy

ETAPES = ("connect", "first-process", "recurring")

# Les prédicats d'une étape, sur la personne `p`. Littéraux de ce module, jamais une
# entrée d'appelant.
_CRITERE = {
    "connect": """p.branchee_le IS NULL
        AND p.created_at <= NOW() - make_interval(hours => %(delay_hours)s)
        AND p.created_at >= NOW() - make_interval(days => %(window_days)s)""",
    "first-process": """p.branchee_le <= NOW() - INTERVAL '48 hours'
        AND p.runs_termines = 0
        AND (p.dernier_appel_agent IS NULL
             OR p.dernier_appel_agent < NOW() - INTERVAL '48 hours')
        AND p.created_at >= NOW() - make_interval(days => %(sequence_days)s)""",
    "recurring": """p.runs_termines >= 1
        AND p.jours_de_runs < 2
        AND NOT p.agent_programme
        AND p.dernier_run_termine <= NOW() - INTERVAL '24 hours'
        AND p.created_at >= NOW() - make_interval(days => %(sequence_days)s)""",
}

_AUDIENCE_SQL = f"""
WITH pref AS ({_TENANT_PREF_SQL}),
     sub_tenant AS ({_SUB_TENANT_SQL}),
     comptes AS (
         SELECT lower(btrim(u.email)) AS adresse, u.sub, u.email, u.name,
                u.created_at, u.suspended_at
           FROM users u
           JOIN sub_tenant st ON st.sub = u.sub
           JOIN tenants t ON t.id = st.tenant_id
          WHERE t.slug = %(tenant)s
            AND u.email IS NOT NULL AND btrim(u.email) <> ''
     ),
     appels AS (
         SELECT tc.sub,
                MIN(tc.created_at) AS branche_le,
                MAX(tc.created_at) FILTER (WHERE tc.kind = 'mcp') AS dernier_appel
           FROM tool_calls tc
          WHERE tc.sub IN (SELECT sub FROM comptes) AND tc.kind IN ('mcp', 'protocol')
          GROUP BY tc.sub
     ),
     -- colonnes renommées : le journal sert le nom historique de la procédure
     journal (run_id, sub, org_id, label, procedure, version_procedure, started_at,
              finished_at, outcome, last_seen_at)
         AS ({_runs_from_journal(" AND s.sub IN (SELECT sub FROM comptes)")}),
     runs AS (
         SELECT sub, COUNT(*) AS n, COUNT(DISTINCT finished_at::date) AS jours,
                MAX(finished_at) AS dernier
           FROM journal
          WHERE procedure IS NOT NULL AND outcome = 'done'
          GROUP BY sub
     ),
     personnes AS (
         SELECT c.adresse,
                COUNT(*) AS comptes,
                ARRAY_AGG(c.sub) AS subs,
                (ARRAY_AGG(c.sub ORDER BY c.created_at DESC NULLS LAST, c.sub))[1] AS sub,
                (ARRAY_AGG(c.email ORDER BY c.created_at DESC NULLS LAST, c.sub))[1] AS email,
                (ARRAY_AGG(c.name ORDER BY (c.name IS NULL),
                    c.created_at DESC NULLS LAST, c.sub))[1] AS name,
                MIN(c.created_at) AS created_at,
                BOOL_OR(c.suspended_at IS NOT NULL) AS suspendu,
                MIN(a.branche_le) AS branchee_le,
                MAX(a.dernier_appel) AS dernier_appel_agent,
                COALESCE(SUM(r.n), 0) AS runs_termines,
                COALESCE(MAX(r.jours), 0) AS jours_de_runs,
                MAX(r.dernier) AS dernier_run_termine,
                BOOL_OR(EXISTS (SELECT 1 FROM runner_triggers rt
                                 WHERE rt.sub = c.sub AND rt.enabled
                                   AND rt.kind = 'schedule')) AS agent_programme
           FROM comptes c
           LEFT JOIN appels a ON a.sub = c.sub
           LEFT JOIN runs r ON r.sub = c.sub
          GROUP BY c.adresse
     ),
     operateurs AS (
         SELECT lower(btrim(email)) AS adresse FROM users
          WHERE role IN ('admin', 'super_admin') AND email IS NOT NULL
     )
SELECT {{projection}}
  FROM personnes p
 WHERE {{critere}}
   AND NOT p.suspendu
   AND split_part(p.adresse, '@', 2) <> ALL(%(exclude_domains)s)
   -- l'opérateur, et ses adresses `+suffixe` (nos comptes d'essai)
   AND regexp_replace(p.adresse, '\\+[^@]*@', '@')
       NOT IN (SELECT adresse FROM operateurs)
   AND NOT EXISTS (SELECT 1 FROM outreach_optouts o WHERE o.sub = ANY(p.subs))
   -- une fois par étape
   AND NOT EXISTS (SELECT 1 FROM outreach_sends s
                    WHERE s.sub = ANY(p.subs) AND s.campaign = %(campaign)s
                      AND s.kind = 'send')
   -- 48 h entre deux emails d'activation, toutes étapes confondues
   AND NOT EXISTS (SELECT 1 FROM outreach_sends s
                    WHERE s.sub = ANY(p.subs) AND s.kind = 'send'
                      AND s.campaign LIKE 'activation-%%'
                      AND s.sent_at > NOW() - INTERVAL '48 hours')
"""

_COLONNES = ("p.sub, p.email, p.name, p.created_at, p.comptes, p.branchee_le, "
             "p.runs_termines, p.agent_programme")


def campagne(etape: str, tenant: str) -> str:
    return f"activation-{etape}:{tenant}"


def _critere(etape: str) -> str:
    if etape not in _CRITERE:
        raise ValueError(f"étape inconnue : {etape!r} (attendu : {', '.join(ETAPES)})")
    return _CRITERE[etape]


def _params(*, etape: str, tenant: str, delay_hours: int, window_days: int,
            sequence_days: int, exclude_domains: list[str]) -> dict:
    return {"primary": tenancy.primary_slug(), "tenant": tenant,
            "campaign": campagne(etape, tenant),
            "delay_hours": int(delay_hours), "window_days": int(window_days),
            "sequence_days": int(sequence_days),
            "exclude_domains": [d.lower().strip() for d in exclude_domains if d.strip()]}


def audience(*, etape: str, tenant: str, delay_hours: int = 24, window_days: int = 30,
             sequence_days: int = 14, exclude_domains: list[str], cap: int) -> list[dict]:
    """Les personnes à qui l'email de cette étape est dû, les plus anciennes d'abord."""
    sql = (_AUDIENCE_SQL.format(projection=_COLONNES, critere=_critere(etape))
           + " ORDER BY p.created_at ASC LIMIT %(cap)s")
    params = _params(etape=etape, tenant=tenant, delay_hours=delay_hours,
                     window_days=window_days, sequence_days=sequence_days,
                     exclude_domains=exclude_domains)
    params["cap"] = max(1, int(cap))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def taille(*, etape: str, tenant: str, delay_hours: int = 24, window_days: int = 30,
           sequence_days: int = 14, exclude_domains: list[str]) -> int:
    """L'audience ENTIÈRE de l'étape, sans plafond."""
    sql = ("SELECT COUNT(*) AS n FROM ("
           + _AUDIENCE_SQL.format(projection="1", critere=_critere(etape)) + ") x")
    params = _params(etape=etape, tenant=tenant, delay_hours=delay_hours,
                     window_days=window_days, sequence_days=sequence_days,
                     exclude_domains=exclude_domains)
    with _connect() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])
