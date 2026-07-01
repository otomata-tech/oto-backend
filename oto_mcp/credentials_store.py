"""Accès DB des credentials génériques (`connector_credentials`).

Coffre unique et canonique des secrets de connecteurs, per-entité (user OU org) :
clés API, sessions linkedin/crunchbase, OAuth Google multi-compte.

Chiffrement par enveloppe AES-256-GCM **obligatoire** : le secret vit dans
`secret_enc` (jamais de colonne plaintext) ; `set_credential` chiffre, le
déchiffrement JIT vit dans `get_credential` / `resolve_api_key`. Réutilise
`db._connect` (comme `org_store`) ; ne PAS importer depuis `db` les helpers
haut-niveau (cycle).
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import connectors, crypto
from .db import _connect

logger = logging.getLogger(__name__)

USER = "user"
ORG = "org"
# Scope MEMBRE (ADR 0033) : le credential per-user est scopé (sub, org) — « ma clé
# dans CETTE org », plus de BYO org-agnostique. `entity_type='user'` ne survit que
# pour la famille oauth (google + mounts memento/atlassian/folkmcp, flux dédiés) en
# attendant leurs barreaux (B3/B4).
MEMBER = "member"


def member_id(org_id: int, sub: str) -> str:
    """`entity_id` du scope membre : le couple (org, sub) encodé — l'AAD en dérive,
    donc un credential membre est cryptographiquement lié à son org."""
    return f"{org_id}:{sub}"

# `meta` JSONB porte aussi des satellites SECRETS (audit 2026-06-13, otomata#29) :
# l'`access_token` bearer dérivé d'OAuth (google/memento) y vit en clair (le
# refresh_token, lui, est chiffré dans `secret_enc`). Les surfaces « statut /
# listing » (credential_status, list_accounts, list_credentials) sont consommées
# par /api/me, le listing d'org/groupe, etc. → elles ne doivent JAMAIS sérialiser
# un bearer vers le front. On le retire À LA SOURCE (defense-in-depth, un seul
# point) : les accesseurs qui ont VRAIMENT besoin du token (get_credential_with_meta,
# access_token_for) gardent `meta` entier ; les surfaces de statut servent le
# `meta` public.
_SECRET_META_KEYS = ("access_token", "refresh_token", "id_token")


def _public_meta(meta: Optional[dict]) -> dict:
    """`meta` débarrassé des satellites secrets (bearers OAuth) — pour les surfaces
    de statut/listing qui le sérialisent vers le front."""
    return {k: v for k, v in (meta or {}).items() if k not in _SECRET_META_KEYS}


def public_meta(meta: Optional[dict]) -> dict:
    """Wrapper public de `_public_meta` — `meta` sans les bearers secrets, pour la
    résolution de config non-secrète (endpoint/host : dsn, base_url…)."""
    return _public_meta(meta)


def _secret_kind(connector: str) -> str:
    c = connectors.REGISTRY.get(connector)
    return c.secret_kind if c else "api_key"


def pack_secret(connector: str, fields: dict) -> str:
    """Encode les champs d'un credential (modèle générique multi-champs, ADR 0011)
    en UNE string stockée (chiffrée *whole* dans `secret_enc`). Trois encodages
    selon la forme déclarée par le provider (`Connector.secret_fields`) :

    - 1 champ (api_key) → la valeur brute (back-compat des données existantes) ;
    - `basic_auth` → `base64("email:password")` (format de fil que le mount distant,
      ex. planity-mcp, décode — NE PAS changer sans casser le bridge) ;
    - ≥2 champs (silae & co) → `json.dumps(fields)`.

    Inverse exact : `unpack_secret`."""
    c = connectors.REGISTRY.get(connector)
    if c is not None and c.secret_kind == "basic_auth":
        import base64
        return base64.b64encode(
            f"{fields.get('email', '')}:{fields.get('password', '')}".encode()
        ).decode()
    schema = c.secret_fields if c is not None else ()
    if len(schema) <= 1:
        return next(iter(fields.values()), "") if fields else ""
    return json.dumps(fields)


def unpack_secret(connector: str, secret: str) -> dict:
    """Inverse de `pack_secret` : reconstruit le dict des champs depuis la string
    stockée. Pour l'affichage (champs non-secrets) ET la résolution in-process
    (un client multi-secrets comme Silae s'instancie avec ces kwargs)."""
    c = connectors.REGISTRY.get(connector)
    schema = c.secret_fields if c is not None else ()
    if c is not None and c.secret_kind == "basic_auth":
        import base64
        try:
            email, _, password = base64.b64decode(secret).decode().partition(":")
        except Exception:
            return {}
        return {"email": email, "password": password}
    if len(schema) <= 1:
        return {(schema[0].name if schema else "key"): secret}
    try:
        loaded = json.loads(secret)
        return loaded if isinstance(loaded, dict) else {}
    except (ValueError, TypeError):
        return {}


def split_secret_config(connector: str, fields: dict) -> tuple[dict, dict]:
    """Sépare les champs unpackés en `(secrets, config)` selon le flag `secret`
    du schéma déclaré (`Connector.secret_fields`). La config = champs non-secrets
    (endpoint/host : `base_url`, `data_center`, `org_id`…). Champ inconnu = traité
    comme secret (défaut prudent). Pur — pas d'accès coffre."""
    c = connectors.REGISTRY.get(connector)
    is_secret = {f.name: f.secret for f in (c.secret_fields if c is not None else ())}
    secrets = {k: v for k, v in fields.items() if is_secret.get(k, True)}
    config = {k: v for k, v in fields.items() if not is_secret.get(k, True)}
    return secrets, config


def secret_from_input(
    connector: str, api_key: Optional[str] = None, fields: Optional[dict] = None,
) -> str:
    """String secret à stocker pour un set-path PARTAGÉ (org/groupe), selon la forme
    du connecteur — SOURCE UNIQUE des capacités org.secret.set / group.secret.set
    (miroir du set-path user `api_routes.api_key_save`).

    - mono-champ (≤1 `secret_field`, api_key) → la valeur brute ;
    - multi-champs (≥2, ex. zoho/silae) → tous les champs déclarés requis non vides,
      packés via `pack_secret`.

    Lève `ValueError(code)` actionnable : `empty_api_key` (mono vide) ou
    `missing_credentials` (multi : champ déclaré absent/vide)."""
    c = connectors.REGISTRY.get(connector)
    sfields = c.secret_fields if c is not None else ()
    if len(sfields) >= 2:
        provided = fields or {}
        packed: dict[str, str] = {}
        for f in sfields:
            raw = provided.get(f.name)
            val = raw.strip() if isinstance(raw, str) else raw
            if not val:
                raise ValueError("missing_credentials")
            packed[f.name] = val
        return pack_secret(connector, packed)
    key = (api_key or "").strip()
    if not key:
        raise ValueError("empty_api_key")
    return key


def _aad(entity_type: str, entity_id: str, connector: str, account: str = "") -> str:
    """AAD liant le ciphertext à SA ligne (anti-transplant). Le segment `account`
    n'est ajouté que s'il est non vide → AAD INCHANGÉE pour le mono-compte
    (compat ascendante : un ciphertext mono-compte reste déchiffrable)."""
    base = f"connector_credentials:{entity_type}:{entity_id}:{connector}"
    return f"{base}:{account}" if account else base


def _reveal(row, entity_type: str, entity_id: str, connector: str, account: str) -> Optional[str]:
    """Secret en clair depuis une ligne : déchiffre `secret_enc`. Le chiffrement
    est obligatoire (pas de chemin plaintext) → un échec de déchiffrement LÈVE,
    jamais de fallback silencieux. Primitive partagée par get_credential /
    get_credential_with_meta."""
    if not row["secret_enc"]:
        return None
    return crypto.decrypt(row["secret_enc"], _aad(entity_type, entity_id, connector, account))


def get_credential(entity_type: str, entity_id: str, connector: str, account: str = "") -> Optional[str]:
    """Secret en CLAIR du connecteur pour cette entité (et ce `account` pour le
    multi-compte ; '' = mono-compte), ou None. Déchiffrement JIT si la ligne est
    chiffrée (secret_enc) ; fallback plaintext (secret) pour les lignes
    non-migrées / chiffrement désactivé. Lève si le connecteur ne peut pas porter
    un credential à ce niveau d'entité (user→byo_user, org→org-partageable).

    Primitive de déchiffrement : appelée par resolve_api_key (résolution, injecte
    au connecteur) ET api_key_get (lecture de SA clé par le propriétaire).
    status_for utilise `has_credential` (présence, sans déchiffrer)."""
    connectors.require_credential(entity_type, connector)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret_enc FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    return _reveal(row, entity_type, entity_id, connector, account) if row else None


def get_credential_with_meta(entity_type: str, entity_id: str, connector: str,
                             account: str = "") -> Optional[dict]:
    """`{secret (déchiffré), meta, set_at}` ou None. Pour les connecteurs dont des
    satellites vivent dans `meta` : user_agent (linkedin/crunchbase),
    scopes/is_default (google). Même déchiffrement JIT que get_credential.

    Un connecteur **remote** (ADR 0003/0011) est défini par la DONNÉE (`meta.base_url`,
    endpoint du bridge) → pas d'entrée registre attendue ; on lit donc la ligne
    d'abord et on n'applique la garde d'éligibilité registre que pour un connecteur
    NON-remote (et sur un miss)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret_enc, meta, set_at FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    meta = (row["meta"] if row else None) or {}
    if not meta.get("base_url"):
        connectors.require_credential(entity_type, connector)
    if not row:
        return None
    return {"secret": _reveal(row, entity_type, entity_id, connector, account),
            "meta": meta, "set_at": row["set_at"]}


def update_meta(entity_type: str, entity_id: str, connector: str, account: str,
                patch: dict, conn=None) -> bool:
    """Merge `patch` dans `meta` (JSONB ||) SANS toucher secret/secret_enc — pour
    les satellites mutables (access_token/expires_at Google sur refresh,
    is_default…), sans re-chiffrer le refresh_token. False si ligne absente."""
    def _do(c) -> bool:
        cur = c.execute(
            "UPDATE connector_credentials SET meta = meta || %s::jsonb "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            (json.dumps(patch), entity_type, entity_id, connector, account),
        )
        return (cur.rowcount or 0) > 0
    if conn is not None:
        return _do(conn)
    with _connect() as c:
        return _do(c)


def list_accounts(entity_type: str, entity_id: str, connector: str) -> list[dict]:
    """Lignes (account, meta, set_at) d'un connecteur multi-compte SANS secret
    (ni le secret chiffré, ni les bearers de `meta` — cf. `_public_meta`) — pour
    la sélection du défaut / le listing (google)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT account, meta, set_at FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s ORDER BY account",
            (entity_type, entity_id, connector),
        ).fetchall()
    return [{"account": r["account"], "meta": _public_meta(r["meta"]), "set_at": r["set_at"]} for r in rows]


def credential_status(entity_type: str, entity_id: str, connector: str,
                      account: str = "") -> Optional[dict]:
    """Présence + satellites NON-secrets (`meta` filtré par `_public_meta`, `set_at`)
    SANS déchiffrer — pour /api/me et autres surfaces de statut (mêmes garanties que
    has_credential : jamais le secret chiffré NI un bearer de `meta`). None si aucun
    credential."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT meta, set_at, (secret_enc IS NOT NULL) AS configured "
            "FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    if not row or not row["configured"]:
        return None
    return {"set_at": row["set_at"], "meta": _public_meta(row["meta"])}


def has_credential(entity_type: str, entity_id: str, connector: str, account: Optional[str] = None) -> bool:
    """Présence d'un secret SANS déchiffrer (pour status_for / surface d'attaque
    réduite : /api/me n'a besoin que du booléen, jamais de la valeur).

    `account` None = n'importe quel compte (présence du connecteur, multi-compte
    inclus) ; '' = strictement le mono-compte ; une valeur = ce compte précis."""
    sql = ("SELECT 1 FROM connector_credentials WHERE entity_type = %s AND entity_id = %s "
           "AND connector = %s AND secret_enc IS NOT NULL")
    params: tuple = (entity_type, entity_id, connector)
    if account is not None:
        sql += " AND account = %s"
        params += (account,)
    with _connect() as conn:
        return conn.execute(sql + " LIMIT 1", params).fetchone() is not None


def _upsert(conn, entity_type, entity_id, connector, account, secret, set_by, meta) -> None:
    # Chiffrement obligatoire : secret_enc porte le ciphertext. crypto.encrypt lève
    # si OTO_MCP_MASTER_KEY absente (pas de stockage plaintext).
    enc = crypto.encrypt(secret, _aad(entity_type, entity_id, connector, account))
    conn.execute(
        """
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, account, secret_enc, secret_kind, meta, set_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_type, entity_id, connector, account) DO UPDATE SET
            secret_enc = EXCLUDED.secret_enc,
            secret_kind = EXCLUDED.secret_kind,
            meta = EXCLUDED.meta,
            set_by = EXCLUDED.set_by,
            set_at = NOW()
        """,
        (entity_type, entity_id, connector, account, enc, _secret_kind(connector),
         json.dumps(meta or {}), set_by),
    )


def _delete(conn, entity_type, entity_id, connector, account) -> bool:
    cur = conn.execute(
        "DELETE FROM connector_credentials "
        "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
        (entity_type, entity_id, connector, account),
    )
    return (cur.rowcount or 0) > 0


def set_credential(
    entity_type: str,
    entity_id: str,
    connector: str,
    secret: str,
    set_by: Optional[str] = None,
    meta: Optional[dict] = None,
    conn=None,
    account: str = "",
) -> None:
    """Pose/rote le secret (UPSERT). secret_kind dérivé du registre. `account`
    discrimine le multi-compte ('' = mono-compte ; ex. email Google).

    `conn` : si fourni, participe à la transaction de l'appelant (dual-write
    ATOMIQUE — le write legacy et le write canonique commitent ou rollback
    ensemble). Sinon ouvre sa propre transaction.

    Remote (ADR 0003/0011) défini par la donnée (`meta.base_url`) → pas d'entrée
    registre ; sinon, garde d'éligibilité registre.
    """
    if not (meta and meta.get("base_url")):
        connectors.require_credential(entity_type, connector)
    if not secret:
        raise ValueError("secret requis")
    if conn is not None:
        _upsert(conn, entity_type, entity_id, connector, account, secret, set_by, meta)
    else:
        with _connect() as c:
            _upsert(c, entity_type, entity_id, connector, account, secret, set_by, meta)


def clear_credential(entity_type: str, entity_id: str, connector: str, conn=None,
                     account: str = "") -> bool:
    """Supprime le credential (ce `account` ; '' = mono-compte). `conn` fourni →
    transaction de l'appelant."""
    if conn is not None:
        return _delete(conn, entity_type, entity_id, connector, account)
    with _connect() as c:
        return _delete(c, entity_type, entity_id, connector, account)


def list_credentials(entity_type: str, entity_id: str) -> list[dict]:
    """Connecteurs configurés pour l'entité — SANS le secret (jamais exposé) ni les
    bearers de `meta` (filtré par `_public_meta`), mais AVEC les satellites non-secrets
    (base_url d'un bridge remote, scopes…). Une ligne par (connector, account) : le
    multi-compte apparaît en N lignes."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, account, secret_kind, set_by, set_at, meta FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s ORDER BY connector, account",
            (entity_type, entity_id),
        ).fetchall()
        return [{**dict(r), "meta": _public_meta(r["meta"])} for r in rows]


def list_remote_namespaces() -> set[str]:
    """Namespaces des connecteurs REMOTE (ADR 0003) — dérivés de la DONNÉE, pas
    d'un registre : tout credential d'org portant `meta.base_url` (= endpoint
    d'un bridge). Aucun nom client en dur ; un connecteur remote existe ssi une
    org a posé son credential. Consommé au boot par `tools/remote.py`."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, meta FROM connector_credentials WHERE entity_type = 'org'"
        ).fetchall()
    return {r["connector"] for r in rows if (r["meta"] or {}).get("base_url")}


def org_remote_namespaces(org_id) -> set[str]:
    """Namespaces remote possédés par cette org (ses credentials avec `base_url`).
    Le credential EST le grant : possession ⇒ visibilité (cf. la règle de masquage
    remote de `session_visibility`, ADR 0031)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, meta FROM connector_credentials WHERE entity_type = 'org' AND entity_id = %s",
            (str(org_id),),
        ).fetchall()
    return {r["connector"] for r in rows if (r["meta"] or {}).get("base_url")}


def first_entity_with(entity_type: str, connector: str,
                       prefer: Optional[str] = None) -> Optional[str]:
    """Premier `entity_id` ayant un credential pour ce connecteur, ou None.

    Sert au fetch de catalogue partagé d'un MCP fédéré (tools/mount) : le
    catalogue est identique pour tous, n'importe quel user connecté sert à le
    récupérer une fois au boot. `prefer` (compte désigné, ex. l'admin) est
    privilégié s'il a un credential — pour que le boot s'appuie sur un compte
    stable et déterministe plutôt que sur le premier user venu ; fallback sur
    l'ordre stable `set_at` sinon."""
    with _connect() as conn:
        if prefer:
            row = conn.execute(
                "SELECT entity_id FROM connector_credentials "
                "WHERE entity_type = %s AND connector = %s AND entity_id = %s LIMIT 1",
                (entity_type, connector, prefer),
            ).fetchone()
            if row:
                return row["entity_id"]
        row = conn.execute(
            "SELECT entity_id FROM connector_credentials "
            "WHERE entity_type = %s AND connector = %s ORDER BY set_at LIMIT 1",
            (entity_type, connector),
        ).fetchone()
        return row["entity_id"] if row else None


def backfill_member_scope() -> dict:
    """One-shot idempotent (boot, ADR 0033) : chaque credential per-user hors famille
    oauth passe du scope `('user', sub)` au scope `('member', '{home_org}:{sub}')`.

    L'AAD contient `entity_type:entity_id` → on ne peut PAS UPDATE la ligne : on
    déchiffre avec l'ancien AAD et on ré-écrit via `_upsert` (nouveau AAD), en
    préservant `meta`/`set_by` (le `set_at` est rafraîchi — acceptable, c'est la
    date de (re)pose). Ligne migrée = ligne supprimée. Une ligne indéchiffrable
    (InvalidTag pré-rotation, crypto désactivée) est LAISSÉE en place et loggée :
    plus rien ne lit le scope 'user' pour ces connecteurs → elle est inerte, pas
    dangereuse. No-op aux boots suivants (le WHERE se vide)."""
    from . import org_store  # lazy — org_store importe credentials_store (cycle)
    counts = {"migrated": 0, "skipped": 0}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_id, connector, account, secret_enc, meta, set_by "
            "FROM connector_credentials WHERE entity_type = %s", (USER,),
        ).fetchall()
    for r in rows:
        sub, connector, account = r["entity_id"], r["connector"], r["account"]
        con = connectors.REGISTRY.get(connector)
        # Mounts oauth (memento/atlassian/folkmcp) : flux fédérés encore scope 'user'
        # (barreau ultérieur — la fédération memento est systématique per-compte).
        # Google, lui, migre depuis B3 (db/google.py au scope membre). Connecteur
        # hors registre (legacy) : on ne migre pas ce qu'on ne connaît pas.
        if con is None or (con.secret_kind == "oauth" and connector != "google"):
            continue
        home = org_store.get_active_org(sub)
        if home is None:
            logger.warning("backfill_member_scope: pas d'org maison pour %s (%s) — skip",
                           sub, connector)
            counts["skipped"] += 1
            continue
        try:
            secret = crypto.decrypt(r["secret_enc"], _aad(USER, sub, connector, account))
        except Exception:
            logger.warning("backfill_member_scope: %s/%s (account=%r) indéchiffrable — "
                           "laissé en scope user (inerte)", sub, connector, account,
                           exc_info=True)
            counts["skipped"] += 1
            continue
        meta = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
        with _connect() as conn:
            _upsert(conn, MEMBER, member_id(home, sub), connector, account,
                    secret, r["set_by"] or sub, meta)
            _delete(conn, USER, sub, connector, account)
        counts["migrated"] += 1
    if counts["migrated"] or counts["skipped"]:
        logger.info("backfill_member_scope: %s", counts)
    return counts
