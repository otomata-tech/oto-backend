"""Tokens API (auth CLI).

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect
from .users import upsert_user


_TOKEN_PREFIX = "oto_"

#: La durée de vie MAXIMALE d'un jeton `oto_` neuf, en jours — et celle que reçoit un
#: jeton demandé sans échéance. Un jeton sans échéance survivait à tout : un mot de
#: passe changé ne le coupait pas, et qui l'avait émis depuis une session volée gardait
#: l'accès indéfiniment. Les jetons émis AVANT ce plafond sans échéance ont reçu la
#: leur par la révision `20261111_0920_jetons_echeance` (90 jours après sa mise en prod).
TTL_MAX_JOURS = 90


class EcheanceTropLointaine(ValueError):
    """Une échéance demandée au-delà de `TTL_MAX_JOURS` : refusée, jamais rabotée — un
    jeton plus court que demandé cesserait de fonctionner sans que rien ne l'ait dit."""

    code = "token_ttl_too_long"


def echeance_bornee(ttl_days: Optional[int]) -> int:
    """L'échéance, en jours, d'un jeton neuf : celle demandée si elle tient sous le
    plafond, `TTL_MAX_JOURS` si aucune ne l'est (absente, nulle ou négative). Lève
    `EcheanceTropLointaine` au-delà. LA règle, partagée par le store et les capacités
    qui émettent : une seule lecture du plafond."""
    if ttl_days is not None and ttl_days > TTL_MAX_JOURS:
        raise EcheanceTropLointaine(
            f"ttl_days={ttl_days} : un jeton vit au plus {TTL_MAX_JOURS} jours")
    return ttl_days if ttl_days and ttl_days > 0 else TTL_MAX_JOURS


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def purger_delegations_expirees(sub: str) -> int:
    """Efface les jetons de DÉLÉGATION expirés de ce compte.

    ⚠️ Un jeton mort n'a aucune raison de rester : il est inutilisable, et
    l'accumulation est mécanique — un par travail exécuté. Appelée à l'émission
    plutôt que par une tâche de fond : le nettoyage est amorti sur l'usage, et
    il n'y a pas de mécanisme nouveau à faire vivre.

    ⚠️ Ne touche QUE `kind='delegation'` : un jeton d'utilisateur expiré reste
    visible, parce que son propriétaire doit pouvoir constater qu'il a expiré —
    c'est le sien, il l'a créé, sa disparition silencieuse serait une surprise.
    """
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_api_tokens WHERE sub = %s AND kind = 'delegation' "
            "AND expires_at IS NOT NULL AND expires_at < NOW()",
            (sub,),
        )
        return cur.rowcount or 0


def create_api_token(sub: str, label: str = "cli", ttl_days: Optional[int] = None,
                     scopes: Optional[dict] = None,
                     ttl_seconds: Optional[int] = None,
                     kind: str = "user", *, job_id: Optional[int] = None,
                     verrou_org: bool = False,
                     verrou_org_id: Optional[int] = None,
                     parent_id: Optional[int] = None,
                     created_by: Optional[str] = None) -> str:
    """Génère un token, persiste son hash, renvoie le plaintext une seule fois.

    `ttl_days` : le token expire après ce délai et est rejeté par
    `verify_api_token`. Au plus `TTL_MAX_JOURS` (au-delà : `EcheanceTropLointaine`) ;
    absent, il vaut `TTL_MAX_JOURS` — plus aucun jeton neuf n'est non-expirant
    (`echeance_bornee`). La révocation explicite est `revoke_api_token`.

    `job_id` / `verrou_org` / `verrou_org_id` : un jeton de DÉLÉGATION porte son
    travail et l'org de ce travail — toute résolution du porteur hors de cette org est
    refusée (`verrou_org.py`). `verrou_org_id=None` avec le verrou = aucune org.

    `scopes` (cf. `token_scopes.py`) : None = jeton NON PORTÉ, il est le sub.
    Non None = deny-by-default, seul ce que la portée nomme passe — la forme d'un
    jeton confié à une intégration tierce. Validé par `token_scopes.parse` AVANT
    d'arriver ici (le document est stocké tel quel).

    `parent_id` : le jeton ÉMETTEUR qui a émis celui-ci (`token_scopes.ISSUE`). Un
    enfant ne fonctionne que tant que son parent fonctionne (`verify_api_token`), et
    la révocation du parent le révoque (`revoke_api_token`).

    `created_by` : qui a émis le jeton quand ce n'est pas son porteur — l'admin d'une
    clé d'org (`kind='org'`, porteur = le compte de service de l'org).
    """
    # ⚠️ `ttl_seconds` gagne sur `ttl_days` : un jeton de délégation vit le temps
    # d'un bail (quelques minutes), pas d'une journée. Sans lui, le plus court
    # exprimable était 1 jour — soit ~90 fois la durée du besoin, pour un jeton
    # qui porte l'identité de quelqu'un d'autre. Le plafond vaut pour les deux, et
    # se juge AVANT toute écriture (`upsert_user` compris).
    if ttl_seconds and ttl_seconds > 0:
        if ttl_seconds > TTL_MAX_JOURS * 86_400:
            raise EcheanceTropLointaine(
                f"ttl_seconds={ttl_seconds} : un jeton vit au plus {TTL_MAX_JOURS} jours")
        expires = f"NOW() + INTERVAL '{int(ttl_seconds)} seconds'"
    else:
        expires = f"NOW() + INTERVAL '{echeance_bornee(ttl_days)} days'"
    # ⚠️ `upsert_user` CRÉE le compte s'il n'existe pas. Pour un jeton émis au nom
    # d'un tiers (délégation d'un travail programmé), l'existence du compte se
    # vérifie donc AVANT d'appeler ici — sinon on ressusciterait silencieusement
    # un compte supprimé, et on lui délivrerait un accès dans la foulée.
    upsert_user(sub)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO user_api_tokens (sub, label, token_hash, expires_at, "
            f"scopes, kind, job_id, verrou_org, verrou_org_id, parent_id, created_by) "
            f"VALUES (%s, %s, %s, {expires}, %s, %s, %s, %s, %s, %s, %s)",
            (sub, label, _hash_token(token),
             json.dumps(scopes) if scopes is not None else None, kind,
             job_id, True if verrou_org else None,
             verrou_org_id if verrou_org else None, parent_id, created_by),
        )
    return token


def verify_api_token(token: str) -> Optional[dict]:
    """Renvoie `{sub, scopes}` du token, et met à jour last_used_at — `scopes` None
    pour un jeton non porté. None (tout court) si inconnu, expiré ou révoqué (#523 :
    un jeton révoqué garde sa ligne, c'est ce filtre qui le coupe).

    UN SEUL statement (`UPDATE … RETURNING`), et pas un SELECT puis un UPDATE dans la
    même transaction : la forme en deux temps prenait AccessShareLock (SELECT) puis
    demandait RowExclusiveLock (UPDATE) sur `user_api_tokens`. Un `ALTER TABLE` de
    migration (init_db) qui se glissait ENTRE les deux se mettait en file d'attente
    derrière l'AccessShareLock, et l'UPDATE — bloqué derrière l'ALTER en attente
    (file FIFO des locks PG) — fermait le cycle : DeadlockDetected des deux côtés
    (Sentry, 5 événements côté boot + 2 côté requête, dernier 2026-07-30). Un seul
    statement prend son lock d'un coup : plus d'escalade intra-transaction, plus de
    cycle possible avec le DDL.

    Un jeton émis par un jeton ÉMETTEUR (`parent_id`) meurt avec lui : révoqué ou
    échu, le parent emporte ses enfants, sans écriture sur eux.
    """
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    h = _hash_token(token)
    with _connect() as conn:
        row = conn.execute(
            "UPDATE user_api_tokens t SET last_used_at = NOW() "
            "WHERE t.token_hash = %s AND t.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > NOW()) "
            "AND (t.parent_id IS NULL OR EXISTS ("
            "  SELECT 1 FROM user_api_tokens p WHERE p.id = t.parent_id "
            "  AND p.revoked_at IS NULL "
            "  AND (p.expires_at IS NULL OR p.expires_at > NOW()))) "
            "RETURNING t.id, t.sub, t.scopes, t.kind, t.job_id, t.verrou_org, "
            "t.verrou_org_id",
            (h,),
        ).fetchone()
        if not row:
            return None
        # `id` et `kind` servent le JOURNAL : nommer le jeton employé sans jamais
        # l'écrire. Deux appels du même compte par deux jetons différents étaient
        # jusqu'ici indistinguables — et un jeton de délégation ressemblait à une
        # session humaine.
        return {"sub": row["sub"], "scopes": _as_scopes(row.get("scopes")),
                "token_id": row["id"], "token_kind": row.get("kind"),
                # Le verrou d'org d'un jeton de délégation (`verrou_org.py`).
                "job_id": row.get("job_id"), "verrou_org": bool(row.get("verrou_org")),
                "verrou_org_id": row.get("verrou_org_id")}


def _as_scopes(raw: object) -> Optional[dict]:
    """`scopes` JSONB → dict. La row factory rend les JSONB en dict ; un pilote qui
    rendrait du texte ne doit pas faire d'un jeton PORTÉ un jeton libre — d'où le
    parse explicite, et le **fail-closed** (illisible ⇒ portée vide ⇒ rien ne passe)."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def list_api_tokens(sub: str, include_revoked: bool = False,
                    parent_id: Optional[int] = None,
                    kinds: tuple[str, ...] = ("user",)) -> list[dict]:
    """Les jetons de l'utilisateur. Les RÉVOQUÉS n'y sont que sur demande
    (`include_revoked`) : la liste sert d'abord à décider quoi couper, et un jeton
    déjà coupé n'y a rien à faire ; il reste lisible pour l'enquête (#523).
    `parent_id` borne la liste aux enfants de ce jeton émetteur. `kinds` : les
    natures listées — la liste d'une org (`("org", "user")`) montre ses clés ET ce
    que ses clés émettrices ont émis."""
    revoques = "" if include_revoked else "AND revoked_at IS NULL "
    enfants = "AND parent_id = %s " if parent_id is not None else ""
    params = ((sub, list(kinds), parent_id) if parent_id is not None
              else (sub, list(kinds)))
    with _connect() as conn:
        rows = conn.execute(
            # ⚠️ `kinds` vaut `('user',)` par défaut : cet écran annonce des jetons de CLI et
            # d'intégration continue. Les jetons de délégation — 12 minutes,
            # émis automatiquement, un par travail — n'y ont pas leur place :
            # ils feraient mentir l'écran, et son bouton « révoquer » porterait
            # sur un accès en cours d'usage.
            "SELECT id, label, created_at, last_used_at, expires_at, scopes, "
            "revoked_at, revoked_by, revoked_reason, parent_id, created_by "
            f"FROM user_api_tokens WHERE sub = %s AND kind = ANY(%s) {revoques}"
            f"{enfants}ORDER BY created_at DESC",
            params,
        ).fetchall()
        return [dict(r) for r in rows]


def revoke_api_token(sub: str, token_id: int, revoked_by: str,
                     reason: Optional[str], parent_id: Optional[int] = None) -> bool:
    """Révoque un jeton : il cesse de fonctionner, sa ligne RESTE (#523).

    ⚠️ Jusqu'au 23/09/2026, révoquer était un `DELETE` : après coup, on ne savait
    plus qui détenait le jeton, ni quand ni pourquoi il avait été coupé — précisément
    le cas où la trace compte (un jeton trop large stocké chez un tiers). Les partages
    et les instances de connecteur gardaient déjà leur date de révocation.

    False si le jeton n'existe pas pour ce sub OU s'il est déjà révoqué : une seconde
    révocation n'écrase pas la trace de la première.

    `parent_id` borne la révocation aux enfants de ce jeton émetteur. Révoquer un
    émetteur révoque ses enfants encore actifs, dans la même transaction : ils
    cessaient déjà de fonctionner (`verify_api_token`), la liste le DIT."""
    enfant = "AND parent_id = %s " if parent_id is not None else ""
    params = ((revoked_by, reason, sub, token_id, parent_id) if parent_id is not None
              else (revoked_by, reason, sub, token_id))
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE user_api_tokens SET revoked_at = NOW(), revoked_by = %s, "
            "revoked_reason = %s "
            f"WHERE sub = %s AND id = %s AND revoked_at IS NULL {enfant}",
            params,
        )
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE user_api_tokens SET revoked_at = NOW(), revoked_by = %s, "
            "revoked_reason = %s WHERE parent_id = %s AND revoked_at IS NULL",
            (revoked_by, f"émetteur {token_id} révoqué", token_id),
        )
        return True
