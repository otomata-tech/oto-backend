"""L'audience de l'email d'ACTIVATION d'un tenant : ses comptes qui ne se sont jamais
branchés.

Ce n'est PAS la relance de plateforme (`db/outreach.py`), et la différence est le
point de ce module. La relance est écrite par NOUS à NOS comptes, et elle écarte les
comptes de tout tenant tiers — leur écrire serait parler par-dessus un partenaire, dans
son produit. L'activation est l'inverse : c'est le TENANT qui écrit à SES comptes, dans
SA marque, depuis SON adresse, parce qu'il l'a déclaré (`OTO_ACTIVATION`, cf.
`activation.py`). On ne sélectionne donc QUE les comptes du tenant configuré, et
`_AUDIENCE_SQL` de la relance reste intact, garde-fou partenaire compris.

**« Jamais branché » = aucune ligne `mcp` NI `protocol`.** Une ligne `protocol` est un
`initialize` : la personne a ajouté le connecteur et s'est connectée, sans encore rien
demander. Lui écrire « ajoutez le connecteur à votre agent » serait faux — elle l'a fait. Elle
relève d'un autre message, pas de celui-ci.

**Une personne = une boîte mail**, comme la relance et pour la même raison (une même
adresse peut porter deux comptes du tenant) : le refus, le déjà-envoyé et l'activité se
lisent sur TOUS les comptes de la boîte (`p.subs`).

**Écartés dans la requête, pas par une consigne :** le compte suspendu ; l'opérateur de
plateforme (`users.role` admin|super_admin) ET ses adresses `+suffixe` — ce sont nos
comptes d'essai, mesuré le 03/10/2026 : cinq inscriptions de test le même jour ; les
domaines que le tenant déclare comme les siens (`exclude_domains`) ; le refus
(`outreach_optouts`) ; le déjà-envoyé sur cette campagne.

Le journal et le refus sont ceux de la relance (`outreach_sends`, `outreach_optouts`) :
aucune table neuve. ⚠️ Le refus est donc COMMUN aux deux canaux — se désinscrire de
l'activation désinscrit des relances, et réciproquement. Le digest de signaux, lui,
garde sa table (oto#150) ; si l'activation doit devenir un canal distinct, c'est une
table à ajouter, pas un `typ` à détourner.
"""
from __future__ import annotations

from ._conn import _connect
from .tenants import _SUB_TENANT_SQL, _TENANT_PREF_SQL
from .. import tenancy

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
     personnes AS (
         SELECT c.adresse,
                COUNT(*) AS comptes,
                ARRAY_AGG(c.sub) AS subs,
                (ARRAY_AGG(c.sub ORDER BY c.created_at DESC NULLS LAST, c.sub))[1] AS sub,
                (ARRAY_AGG(c.email ORDER BY c.created_at DESC NULLS LAST, c.sub))[1] AS email,
                (ARRAY_AGG(c.name ORDER BY (c.name IS NULL),
                    c.created_at DESC NULLS LAST, c.sub))[1] AS name,
                MIN(c.created_at) AS created_at,
                BOOL_OR(c.suspended_at IS NOT NULL) AS suspendu
           FROM comptes c
          GROUP BY c.adresse
     ),
     operateurs AS (
         SELECT lower(btrim(email)) AS adresse FROM users
          WHERE role IN ('admin', 'super_admin') AND email IS NOT NULL
     )
SELECT {{projection}}
  FROM personnes p
 WHERE p.created_at <= NOW() - make_interval(hours => %(delay_hours)s)
   AND p.created_at >= NOW() - make_interval(days => %(window_days)s)
   AND NOT p.suspendu
   AND split_part(p.adresse, '@', 2) <> ALL(%(exclude_domains)s)
   -- l'opérateur, et ses adresses `+suffixe` (nos comptes d'essai)
   AND regexp_replace(p.adresse, '\\+[^@]*@', '@')
       NOT IN (SELECT adresse FROM operateurs)
   AND NOT EXISTS (SELECT 1 FROM tool_calls tc
                    WHERE tc.sub = ANY(p.subs) AND tc.kind IN ('mcp', 'protocol'))
   AND NOT EXISTS (SELECT 1 FROM outreach_optouts o WHERE o.sub = ANY(p.subs))
   AND NOT EXISTS (SELECT 1 FROM outreach_sends s
                    WHERE s.sub = ANY(p.subs) AND s.campaign = %(campaign)s
                      AND s.kind = 'send')
"""

_COLONNES = "p.sub, p.email, p.name, p.created_at, p.comptes"


def _params(*, tenant: str, campaign: str, delay_hours: int, window_days: int,
            exclude_domains: list[str]) -> dict:
    return {"primary": tenancy.primary_slug(), "tenant": tenant, "campaign": campaign,
            "delay_hours": int(delay_hours), "window_days": int(window_days),
            "exclude_domains": [d.lower().strip() for d in exclude_domains if d.strip()]}


def audience(*, tenant: str, campaign: str, delay_hours: int, window_days: int,
             exclude_domains: list[str], cap: int) -> list[dict]:
    """Les personnes à qui l'email est dû, les plus anciennes d'abord, bornées à `cap`."""
    sql = (_AUDIENCE_SQL.format(projection=_COLONNES)
           + " ORDER BY p.created_at ASC LIMIT %(cap)s")
    params = _params(tenant=tenant, campaign=campaign, delay_hours=delay_hours,
                     window_days=window_days, exclude_domains=exclude_domains)
    params["cap"] = max(1, int(cap))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def taille(*, tenant: str, campaign: str, delay_hours: int, window_days: int,
           exclude_domains: list[str]) -> int:
    """L'audience ENTIÈRE, sans plafond — pour qu'un passage borné dise ce qu'il laisse."""
    sql = ("SELECT COUNT(*) AS n FROM ("
           + _AUDIENCE_SQL.format(projection="1") + ") x")
    params = _params(tenant=tenant, campaign=campaign, delay_hours=delay_hours,
                     window_days=window_days, exclude_domains=exclude_domains)
    with _connect() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])
