"""Accès DB des credentials génériques (`connector_credentials`).

Coffre unique et canonique des secrets de connecteurs, per-entité (user OU org) :
clés API, sessions linkedin/crunchbase, OAuth Google multi-compte.

Chiffrement par enveloppe AES-256-GCM **obligatoire** : le secret vit dans
`secret_enc` (jamais de colonne plaintext) ; `set_credential` chiffre, le
déchiffrement JIT vit dans `get_credential` / `resolve_api_key`. Réutilise
`db._connect` (comme `org_store`) ; ne PAS importer depuis `db` les helpers
haut-niveau (cycle).

⚠️ **Le coffre NOMME ses lignes** (lot L6 pièce 2). `_upsert` et `_delete` sont
l'entonnoir unique d'écriture de `connector_credentials` — un seul `INSERT`, un seul
`DELETE` dans tout le dépôt — et c'est là, et nulle part ailleurs, que l'instance
(`connector_instances`) naît et s'archive, dans la MÊME transaction que le secret.
Accrocher la naissance aux surfaces (clé membre, org, groupe, plateforme, session
navigateur, OAuth) aurait demandé une dizaine de crochets pour le même effet, avec
une chance sur dix d'en oublier un. ⚠️ Écrire ici sans passer par ces deux
primitives laisse une ligne de coffre SANS instance — c'est ce que la requête
d'invariant de `tests/test_connector_instances_birth_live.py` détecte, et ce que le
garde-fou AST de `tests/test_connector_instances_l6.py` circonscrit à ce module.
Le lien reste le QUADRUPLET (`entity_type/entity_id/connector/account`) : aucune
colonne n'est ajoutée au coffre, l'AAD ne bouge pas.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Sequence

from . import providers, crypto
from .db import _connect, connector_instances

_WHITESPACE = re.compile(r"\s+")


def clean_field_value(field, raw):
    """Normalise une valeur de champ credential saisie (formulaire dashboard OU REST).
    Retire TOUS les whitespace (espaces, tabs, retours-ligne d'un copier-coller
    foireux) pour les champs où ils n'ont aucun sens — clés, tokens, ids :
    `field.whitespace_significant` False (défaut). Un champ où l'espace compte (mot de
    passe) n'est que strippé aux bords. Non-string renvoyé tel quel. SOURCE UNIQUE du
    nettoyage → les deux chemins de pose (user + org/groupe) en bénéficient."""
    if not isinstance(raw, str):
        return raw
    if getattr(field, "whitespace_significant", False):
        return raw.strip()
    return _WHITESPACE.sub("", raw)

logger = logging.getLogger(__name__)

USER = "user"
ORG = "org"
# Scope MEMBRE (ADR 0033) : le credential per-user est scopé (sub, org) — « ma clé
# dans CETTE org », plus de BYO org-agnostique. `entity_type='user'` ne survit que
# pour la famille oauth (google + mounts atlassian/folkmcp, flux dédiés) en
# attendant leurs barreaux (B3/B4).
MEMBER = "member"
# Scope PLATEFORME (ADR 0044 §F) : la clé plateforme partagée EST une instance du coffre
# (fin de la table legacy `platform_keys`). `entity_id` = le label de la clé (N par
# connecteur, cf. l'ex-`UNIQUE(provider,label)`). `meta` porte sa config non-secrète (dsn…).
PLATFORM = "platform"
# Scope TENANT (L-clés PR 1, blueprint ADR 0052) : la clé partagée d'un tenant TIERS —
# `entity_id` = le slug. Étage entre l'org et la plateforme dans la cascade ; façade
# `tenant_vault`. Le tenant primaire n'en porte jamais : ses clés partagées sont les
# instances PLATFORM (refus à la pose, barreau jamais sondé — les deux d'un même geste).
TENANT = "tenant"


def member_id(org_id: int, sub: str) -> str:
    """`entity_id` du scope membre : le couple (org, sub) encodé — l'AAD en dérive,
    donc un credential membre est cryptographiquement lié à son org."""
    return f"{org_id}:{sub}"


def get_instance_sharing(entity_type: str, entity_id: str, connector: str,
                         account: str = "") -> tuple[list, list]:
    """(share_down, share_side) d'une instance du coffre (ADR 0044). `share_down` =
    ALLOWLIST deny-by-default (vide = ouverte) ; `share_side` = EXTENSION (prêts
    nominatifs). Listes vides si la ligne n'existe pas (JSONB → list côté psycopg)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT share_down, share_side FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            (entity_type, entity_id, connector, account)).fetchone()
    if not row:
        return [], []
    return (row["share_down"] or []), (row["share_side"] or [])


def sharing_for_vault_rows(
        keys: "Sequence[tuple[str, str, str, str]]") -> dict[tuple, tuple[str, list, list]]:
    """`{quadruplet: (share_mode, share_down, share_side)}` — EN LOT.

    Sert la visibilité dérivée (R9), qui a besoin du partage de CHAQUE instance listée.
    **Une seule requête, jamais une par instance** : même raison que
    `db.instance_ids_for_vault_rows` — la projection tourne inline sur un serveur
    mono-loop, contre une base managée distante, et elle en rend des dizaines.

    ⚠️ Ces trois colonnes ne sont volontairement PAS ajoutées à `list_credentials` :
    sa forme part telle quelle vers le front (`capabilities/admin_console`), donc y
    ajouter des clés changerait une charge servie."""
    uniques = sorted({(k[0], str(k[1]), k[2], k[3] or "") for k in keys})
    if not uniques:
        return {}
    clause = ", ".join(["(%s, %s, %s, %s)"] * len(uniques))
    params: list = []
    for k in uniques:
        params += list(k)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_type, entity_id, connector, account, share_mode, "
            "share_down, share_side FROM connector_credentials "
            f"WHERE (entity_type, entity_id, connector, account) IN ({clause})",
            params).fetchall()
    return {(r["entity_type"], r["entity_id"], r["connector"], r["account"]):
            (r["share_mode"], r["share_down"] or [], r["share_side"] or [])
            for r in rows}


def list_shared_with(scopes: list) -> list[dict]:
    """Instances dont le `share_side` vise l'un des `scopes` (ex. `['user:sub',
    'group:2']`) — le « partagé avec moi » (ADR 0044). Métadonnées seulement (secret
    jamais lu). `[]` scopes ⟹ `[]`. `jsonb_exists_any` = indexé par le GIN share_side."""
    if not scopes:
        return []
    with _connect() as conn:
        return conn.execute(
            "SELECT entity_type, entity_id, connector, account, meta, secret_kind, "
            "set_by, set_at FROM connector_credentials "
            "WHERE jsonb_exists_any(share_side, %s)",
            (list(scopes),)).fetchall()


def set_instance_sharing(entity_type: str, entity_id: str, connector: str,
                         account: str = "", *, share_down=None, share_side=None) -> bool:
    """Met à jour `share_down`/`share_side` d'une instance EXISTANTE (ADR 0044) sans
    toucher au secret ; bump `version`. Ne met à jour que les champs fournis (non
    None). Renvoie False si aucune ligne (⟹ pas d'instance à partager — l'appelant
    lève actionnable). Le partage présuppose que la clé existe."""
    sets, params = ["version = version + 1"], []
    if share_down is not None:
        sets.append("share_down = %s::jsonb"); params.append(json.dumps(share_down))
    if share_side is not None:
        sets.append("share_side = %s::jsonb"); params.append(json.dumps(share_side))
    if len(sets) == 1:
        return True  # rien à changer
    params += [entity_type, entity_id, connector, account]
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE connector_credentials SET {', '.join(sets)} "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            tuple(params))
    return (cur.rowcount or 0) > 0


def list_platform_instances(provider: str) -> list[dict]:
    """ADR 0044 §F : les instances scope PLATEFORM d'un `provider` (label + partage +
    meta), SANS secret (le gagnant est déchiffré à part via `get_credential`, chemin chaud
    léger). Trié récent d'abord (miroir de l'ancien « la clé plateforme la plus récente »).
    C'est la source de la résolution du palier plateforme depuis le cutover R3."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_id AS label, share_mode, share_down, share_side, meta "
            "FROM connector_credentials WHERE entity_type=%s AND connector=%s "
            "ORDER BY set_at DESC", (PLATFORM, provider)).fetchall()
    return [{"label": r["label"], "share_mode": r["share_mode"],
             "share_down": r["share_down"] or [], "share_side": r["share_side"] or [],
             "meta": r["meta"] or {}} for r in rows]


def list_platform_credentials(provider: "str | None" = None) -> list[dict]:
    """ADR 0044 §F : clés plateforme (instances scope PLATFORM), SANS secret — pour la
    surface admin (remplace db.list_platform_keys_meta)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, entity_id AS label, set_at FROM connector_credentials "
            "WHERE entity_type=%s" + (" AND connector=%s" if provider else "") +
            " ORDER BY connector, set_at DESC",
            (PLATFORM, provider) if provider else (PLATFORM,)).fetchall()
    return [{"provider": r["connector"], "label": r["label"], "set_at": r["set_at"]}
            for r in rows]


def _latest_platform_label(conn, provider: str) -> "str | None":
    r = conn.execute(
        "SELECT entity_id FROM connector_credentials WHERE entity_type=%s AND connector=%s "
        "ORDER BY set_at DESC LIMIT 1", (PLATFORM, provider)).fetchone()
    return r["entity_id"] if r else None


def platform_grant(provider: str, scope: str, daily_quota: "int | None" = None,
                   label: "str | None" = None) -> None:
    """ADR 0044 §F R4 : accorde l'accès plateforme à `scope` (`user:<sub>` | `org:<id>`) sur
    l'instance du `provider` (label = clé la plus récente par défaut) — remplace
    grant_platform_key/grant_org_platform_key. Non free-tier ⟹ ajoute au `share_down`
    (mode 'closed'). Free-tier (`platform_key_open`) ⟹ accès déjà ouvert à tous : on ne pose
    QUE le quota (`meta.rate_limit_by[scope]`). Le quota s'applique dans les deux cas."""
    from . import grants_chain  # import tardif (grants_chain lit ce module)

    if grants_chain.is_chained(provider):
        # Blueprint ADR 0053 (lot L5) : accorder = poser une ARÊTE. On ne touche PLUS
        # la ligne du coffre — ni `share_mode`, ni `share_down`, ni `rate_limit_by`.
        # C'est exactement le geste qui a brûlé le 31/07 : ici il n'existe plus, donc
        # accorder à l'un ne peut plus retirer à l'autre. L'existant reste EN PLACE
        # (rien n'est effacé) — c'est ce qui rend le retour à l'ancien chemin possible.
        grants_chain.grant(provider, scope, daily_quota=daily_quota, label=label)
        return
    con = providers.REGISTRY.get(provider)
    free_tier = bool(con and getattr(con, "platform_key_open", False))
    with _connect() as conn:
        label = label or _latest_platform_label(conn, provider)
        if label is None:
            raise ValueError(f"aucune instance plateforme pour {provider!r}")
        row = conn.execute(
            "SELECT share_mode, share_down, meta FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
            (PLATFORM, label, provider)).fetchone()
        down, meta, mode = list(row["share_down"] or []), dict(row["meta"] or {}), row["share_mode"]
        if not free_tier:
            mode = "closed"
            if scope not in down:
                down.append(scope)
        rlb = dict(meta.get("rate_limit_by") or {})
        if daily_quota is not None:
            rlb[scope] = daily_quota
        if rlb:
            meta["rate_limit_by"] = rlb
        conn.execute(
            "UPDATE connector_credentials SET share_mode=%s, share_down=%s::jsonb, "
            "meta=%s::jsonb, version=version+1 "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
            (mode, json.dumps(down), json.dumps(meta), PLATFORM, label, provider))


def platform_revoke(provider: str, scope: str, label: "str | None" = None) -> None:
    """ADR 0044 §F R4 : retire l'accès de `scope` (retire du share_down + du rate_limit_by)."""
    from . import grants_chain  # import tardif (grants_chain lit ce module)

    if grants_chain.is_chained(provider):
        # L5 : l'arête s'ARCHIVE (0053-D7, jamais de suppression — sinon la
        # consommation passée disparaît). L'accès est coupé à la lecture suivante :
        # la chaîne, ayant un avis (« révoqué »), ne retombe pas sur l'ancien chemin.
        grants_chain.revoke(provider, scope, label=label)
        return
    with _connect() as conn:
        label = label or _latest_platform_label(conn, provider)
        if label is None:
            return
        row = conn.execute(
            "SELECT share_down, meta FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
            (PLATFORM, label, provider)).fetchone()
        if not row:
            return
        down = [s for s in (row["share_down"] or []) if s != scope]
        meta = dict(row["meta"] or {})
        rlb = dict(meta.get("rate_limit_by") or {})
        rlb.pop(scope, None)
        if rlb:
            meta["rate_limit_by"] = rlb
        else:
            meta.pop("rate_limit_by", None)
        conn.execute(
            "UPDATE connector_credentials SET share_down=%s::jsonb, meta=%s::jsonb, "
            "version=version+1 WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
            (json.dumps(down), json.dumps(meta), PLATFORM, label, provider))


# --- app OAuth de l'ÉDITEUR ---------------------------------------------------
#
# Une app OAuth n'est **pas** un credential d'accès : c'est l'identité de l'ÉDITEUR
# du logiciel (`client_id` + `client_secret`) qui DEMANDE l'accès. Le credential, lui,
# naît du consentement de l'utilisateur, ouvre SES données et lui appartient. Les
# confondre est la confusion de départ du mode « Self Client », où le même humain est
# éditeur et utilisateur — d'où une app à créer par personne, et des scopes cochés à la
# main (3 incidents : #190, #202, Desk articles-only).
#
# Les deux vivent dans le même coffre (même chiffrement, même rotation) mais à des
# adresses distinctes : l'app d'éditeur est rangée au scope PLATFORM sous un
# `entity_id` préfixé `editor:<data_center>` — une app OAuth est liée à SA région
# (`accounts.zoho.eu` rejette un client `.com`), donc la région fait partie de la clé.
#
# ⚠️ **L'invariant qui rend ce rangement sûr** : `walk_cascade` ne propose le palier
# plateforme que si le connecteur déclare `auth_modes ∋ 'platform'` (access/cascade.py). Les
# connecteurs à consentement ne le déclarent pas ⟹ une app d'éditeur n'est JAMAIS
# résolue comme credential d'appel. Conséquences voulues : elle n'ouvre aucune donnée
# par elle-même, et un membre qui n'a pas encore consenti ne peut pas hériter d'une app
# NUE (sans refresh_token) qui échouerait de façon opaque au premier appel. Figé par
# `tests/test_editor_app.py`.
#
# Un seul appelant la lit : le flux de consentement du module connecteur
# (`zoho_oauth.app_fields`). Jamais la résolution.

EDITOR_PREFIX = "editor:"


def editor_label(data_center: str) -> str:
    """`entity_id` de l'app d'éditeur pour une région (`editor:eu`)."""
    return f"{EDITOR_PREFIX}{(data_center or '').strip().lower()}"


def set_editor_app(connector: str, data_center: str, fields: dict,
                   set_by: Optional[str] = None) -> None:
    """Pose/rote l'app d'éditeur d'un connecteur pour une région.

    `_upsert` en direct : `set_credential` gate sur `require_credential`, qui refuse le
    scope plateforme à un connecteur sans `auth_modes ∋ 'platform'`. Ce refus est JUSTE
    pour une clé d'accès — et hors sujet pour une app d'éditeur, qui n'en est pas une
    (cf. l'invariant ci-dessus). Même échappatoire assumée que le chemin remote."""
    if not (fields.get("client_id") and fields.get("client_secret")):
        raise ValueError("client_id et client_secret requis")
    if not (data_center or "").strip():
        raise ValueError("data_center requis")
    with _connect() as conn:
        _upsert(conn, PLATFORM, editor_label(data_center), connector, "",
                pack_secret(connector, {"client_id": fields["client_id"],
                                        "client_secret": fields["client_secret"]}),
                set_by, None)


def get_editor_app(connector: str, data_center: str) -> Optional[dict]:
    """`{client_id, client_secret}` de l'app d'éditeur pour cette région, ou None.

    Ne rend QUE ces deux champs : ce qui est stocké là est l'identité de l'éditeur, pas
    un jeton d'accès — si un refresh_token s'y trouvait, il ne doit pas ressortir."""
    label = editor_label(data_center)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret_enc FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
            (PLATFORM, label, connector)).fetchone()
    if not row:
        return None
    fields = unpack_secret(connector, _reveal(row, PLATFORM, label, connector, "") or "")
    cid, sec = fields.get("client_id"), fields.get("client_secret")
    return {"client_id": cid, "client_secret": sec} if cid and sec else None


def list_editor_apps(connector: "str | None" = None) -> list[dict]:
    """Apps d'éditeur posées (connecteur + région + date), **sans secret** — surface
    admin. Le `client_id` lui-même n'est pas rendu : inutile pour décider, et il finit
    dans des captures d'écran."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, entity_id, set_at FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id LIKE %s"
            + (" AND connector=%s" if connector else "")
            + " ORDER BY connector, entity_id",
            (PLATFORM, EDITOR_PREFIX + "%", connector) if connector
            else (PLATFORM, EDITOR_PREFIX + "%")).fetchall()
    return [{"connector": r["connector"],
             "data_center": r["entity_id"][len(EDITOR_PREFIX):],
             "set_at": r["set_at"]} for r in rows]


def clear_editor_app(connector: str, data_center: str) -> bool:
    """Retire l'app d'éditeur d'une région. Les credentials DÉJÀ obtenus par
    consentement en gardent une copie (`persist` les range avec) : ils continuent de
    fonctionner, mais plus aucune nouvelle connexion ne pourra démarrer."""
    # Par `_delete` et pas par un DELETE brut (L6 pièce 2) : une app d'éditeur EST une
    # ligne de coffre au palier plateforme, donc elle a une instance — le filet de boot
    # lui en donnait déjà une. La retirer sans archiver l'instance ferait deux
    # populations différentes selon le chemin d'écriture.
    with _connect() as conn:
        return _delete(conn, PLATFORM, editor_label(data_center), connector, "")


# `meta` JSONB porte aussi des satellites SECRETS (audit 2026-06-13, otomata#29) :
# l'`access_token` bearer dérivé d'OAuth (google/atlassian) y vit en clair (le
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
    c = providers.REGISTRY.get(connector)
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
    c = providers.REGISTRY.get(connector)
    if c is not None and c.secret_kind == "basic_auth":
        import base64
        return base64.b64encode(
            f"{fields.get('email', '')}:{fields.get('password', '')}".encode()
        ).decode()
    schema = c.secret_fields if c is not None else ()
    if len(schema) <= 1:
        return next(iter(fields.values()), "") if fields else ""
    return json.dumps(fields)


class SecretUnpackError(RuntimeError):
    """Le secret stocké ne se relit pas selon le format déclaré par le connecteur.

    C'est une erreur **de coffre**, et elle doit se lire comme telle. Tant que
    `unpack_secret` rendait `{}` sur un secret illisible, le client était instancié
    **sans identifiants** et l'échec remontait comme une erreur d'authentification
    DU FOURNISSEUR : on accusait la clé du client là où la cause était chez nous
    (clé maître périmée ⇒ `InvalidTag`, cas vécu ; inventaire des silences du
    2026-08-27, site B6)."""


def unpack_secret(connector: str, secret: str) -> dict:
    """Inverse de `pack_secret` : reconstruit le dict des champs depuis la string
    stockée. Pour l'affichage (champs non-secrets) ET la résolution in-process
    (un client multi-secrets comme Silae s'instancie avec ces kwargs).

    **Lève `SecretUnpackError` si le secret ne se relit pas** — jamais `{}`, qui
    produirait un client sans identifiants (cf. la classe ci-dessus)."""
    c = providers.REGISTRY.get(connector)
    schema = c.secret_fields if c is not None else ()
    if c is not None and c.secret_kind == "basic_auth":
        import base64
        try:
            email, _, password = base64.b64decode(secret).decode().partition(":")
        except Exception as e:
            raise SecretUnpackError(
                f"credential `{connector}` illisible : le blob `basic_auth` stocké "
                f"n'est pas du base64 `email:password` ({type(e).__name__}). "
                f"Reposer le credential, ou vérifier la clé maître du coffre.")
        return {"email": email, "password": password}
    if len(schema) <= 1:
        return {(schema[0].name if schema else "key"): secret}
    try:
        loaded = json.loads(secret)
    except (ValueError, TypeError) as e:
        raise SecretUnpackError(
            f"credential `{connector}` illisible : le blob multi-champs stocké n'est "
            f"pas du JSON ({type(e).__name__}). Reposer le credential, ou vérifier "
            f"la clé maître du coffre.")
    if not isinstance(loaded, dict):
        raise SecretUnpackError(
            f"credential `{connector}` illisible : le blob multi-champs stocké est un "
            f"`{type(loaded).__name__}` JSON, pas un objet de champs.")
    return loaded


def split_secret_config(connector: str, fields: dict) -> tuple[dict, dict]:
    """Sépare les champs unpackés en `(secrets, config)` selon le flag `secret`
    du schéma déclaré (`Connector.secret_fields`). La config = champs non-secrets
    (endpoint/host : `base_url`, `data_center`, `org_id`…). Champ inconnu = traité
    comme secret (défaut prudent). Pur — pas d'accès coffre."""
    c = providers.REGISTRY.get(connector)
    is_secret = {f.name: f.secret for f in (c.secret_fields if c is not None else ())}
    secrets = {k: v for k, v in fields.items() if is_secret.get(k, True)}
    config = {k: v for k, v in fields.items() if not is_secret.get(k, True)}
    return secrets, config


class CredentialFieldsInvalid(ValueError):
    """Refus NOMMÉ d'une saisie de credential : le `code` pour la machine, la phrase
    pour l'humain — et pour l'agent qui la lira.

    Reste une `ValueError` dont le `str()` est le code : les call-sites qui font
    `AuthzDenied(400, str(e), …)` continuent de rendre le même code qu'avant."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def _guard_choices(c, provided: dict) -> None:
    """Refuse une valeur hors du jeu FERMÉ d'un champ (`CredentialField.choices`).

    Sans cette garde, `auth_mode='hedaer'` s'écrit `ok: true` et n'échoue qu'au
    premier appel réel — la famille accepté-puis-échoue (oto-backend#449)."""
    for f in (c.secret_fields if c is not None else ()):
        if not f.choices:
            continue
        raw = clean_field_value(f, provided.get(f.name))
        picked = str(raw or "").strip()
        if picked and picked.lower() not in f.choices:
            raise CredentialFieldsInvalid(
                "invalid_field_value",
                f"{f.label or f.name} : « {picked} » n'est pas une valeur attendue "
                f"({' | '.join(f.choices)}).")


def validate_fields(connector: str, provided: dict) -> dict:
    """Les champs RETENUS d'une saisie multi-champs, ou un refus nommé.

    SOURCE UNIQUE des trois set-paths (membre, équipe, org) : jeu fermé d'abord
    (`choices`), puis les seuls champs que le discriminant rend PERTINENTS
    (`Connector.fields_for`), et parmi eux les `required` doivent être non vides.

    ⚠️ **Les champs non pertinents sont ÉCARTÉS, pas stockés** : repasser un
    connecteur d'`oauth2` à `bearer` ne laisse pas traîner un `client_secret` mort
    dans le coffre. C'est voulu — un champ qu'aucun mode ne lit n'a pas à survivre
    à un changement de mode."""
    c = providers.REGISTRY.get(connector)
    _guard_choices(c, provided)
    relevant = c.fields_for(provided) if c is not None else ()
    kept: dict[str, str] = {}
    missing: list[str] = []
    for f in relevant:
        val = clean_field_value(f, provided.get(f.name))
        if not val:
            if f.required:
                missing.append(f.label or f.name)
            continue
        kept[f.name] = val
    # NOMMER le champ manquant : un « missing_credentials » sec oblige à deviner
    # lequel des douze champs bloque (vécu 28/07 sur un `data_center` vide).
    if missing:
        raise CredentialFieldsInvalid(
            "missing_credentials", "champ(s) requis vide(s) : " + ", ".join(missing))
    if not kept:
        raise CredentialFieldsInvalid("missing_credentials", "aucun champ renseigné.")
    return kept


def merge_with_existing(entity_type: str, entity_id: str, connector: str,
                        account: str, provided: dict) -> dict:
    """Complète une saisie PARTIELLE par ce qui est déjà au coffre (oto-backend#448).

    Règle unique : **seule une clé ABSENTE du corps est reprise de l'existant**. Une
    clé présente mais vide reste vide — c'est un effacement explicite. Le formulaire
    du dashboard, qui envoie tous ses champs, garde donc exactement son comportement
    d'avant ; ce qui devient possible est « je change l'URL, je ne touche pas à la clé ».

    ⚠️ **Le merge se fait ICI, côté serveur.** L'existant est relu et rechiffré dans
    la MÊME ligne (l'AAD dérive de `entity_type/entity_id/connector/account`, tous
    inchangés) : le secret ne repasse jamais par le client. C'est ce qui permet de
    repointer une `base_url` sans détenir le bearer — le piège à perte de données que
    formait « lecture impossible + écriture par remplacement total ».

    Un existant ILLISIBLE (ligne écrite sous une clé de chiffrement périmée) n'est pas
    une erreur ici : on rend la saisie telle quelle, et la validation nommera ce qui
    manque. Une repose complète doit rester possible quand le coffre ne se relit plus."""
    c = providers.REGISTRY.get(connector)
    declared = {f.name for f in (c.secret_fields if c is not None else ())}
    if len(declared) < 2 or declared <= set(provided):
        return dict(provided)          # mono-champ, ou saisie déjà complète
    try:
        existing = get_credential(entity_type, entity_id, connector, account)
    except Exception:
        # Une ligne illisible (écrite sous une clé de chiffrement périmée) ne doit pas
        # interdire de la RÉÉCRIRE en entier — mais elle se journalise : c'est un fait
        # d'exploitation, pas un détail.
        logger.warning("merge credential : coffre illisible pour %s/%s/%s (account=%r) "
                       "— saisie prise telle quelle",
                       entity_type, entity_id, connector, account, exc_info=True)
        return dict(provided)
    if not existing:
        return dict(provided)
    prior = unpack_secret(connector, existing)
    return {**{k: v for k, v in prior.items() if k in declared and k not in provided},
            **provided}


def secret_from_input(
    connector: str, api_key: Optional[str] = None, fields: Optional[dict] = None,
) -> str:
    """String secret à stocker pour un set-path PARTAGÉ (org/groupe), selon la forme
    du connecteur — SOURCE UNIQUE des capacités org.secret.set / group.secret.set
    (miroir du set-path user `api_routes_credentials.api_key_save`).

    - mono-champ (≤1 `secret_field`, api_key) → la valeur brute ;
    - multi-champs (≥2, ex. zoho/silae) → `validate_fields` (jeu fermé, pertinence
      par discriminant, `required` non vides), packés via `pack_secret`.

    Lève `CredentialFieldsInvalid` (une `ValueError`) actionnable : `empty_api_key`
    (mono vide), `missing_credentials` (champ requis absent/vide, ou aucun champ) ou
    `invalid_field_value` (valeur hors jeu fermé)."""
    c = providers.REGISTRY.get(connector)
    sfields = c.secret_fields if c is not None else ()
    if len(sfields) >= 2:
        return pack_secret(connector, validate_fields(connector, fields or {}))
    key = (api_key or "").strip()
    if not key:
        raise CredentialFieldsInvalid("empty_api_key", "clé d'API vide.")
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
    multi-compte ; '' = mono-compte), ou None. Déchiffrement JIT de `secret_enc`.
    Lève si le connecteur ne peut pas porter un credential à ce niveau d'entité
    (user→byo_user, org→org-partageable).

    ⚠️ Cette docstring a annoncé jusqu'au 2026-08-27 un « fallback plaintext (secret)
    pour les lignes non-migrées » : **la branche n'existe plus**. Le SELECT ne lit que
    `secret_enc` et `_reveal` LÈVE sur échec de déchiffrement — il n'y a aucun chemin
    plaintext. Carte périmée corrigée avec l'inventaire des silences.

    Primitive de déchiffrement : appelée par resolve_api_key (résolution, injecte
    au connecteur) ET api_key_get (lecture de SA clé par le propriétaire).
    status_for utilise `has_credential` (présence, sans déchiffrer)."""
    providers.require_credential(entity_type, connector)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret_enc FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    return _reveal(row, entity_type, entity_id, connector, account) if row else None


def get_credential_with_meta(entity_type: str, entity_id: str, connector: str,
                             account: str = "") -> Optional[dict]:
    """`{secret (déchiffré), meta, set_at, set_by}` ou None. Pour les connecteurs dont
    des satellites vivent dans `meta` : user_agent (linkedin/crunchbase),
    scopes/is_default (google). Même déchiffrement JIT que get_credential.

    `set_by` (2026-08-31, #671) : QUI a posé la ligne. Depuis que la valeur d'un champ
    secret ne se relit plus, « qui l'a posée et quand » est ce qui reste pour la
    reconnaître — servi par `me.credential.get`, dans le MÊME aller-retour SQL que le
    reste ; une seconde lecture pour deux colonnes serait un aller-retour de plus par
    ouverture d'écran.

    Un connecteur **remote** (ADR 0003/0011) est défini par la DONNÉE (`meta.base_url`,
    endpoint du bridge) → pas d'entrée registre attendue ; on lit donc la ligne
    d'abord et on n'applique la garde d'éligibilité registre que pour un connecteur
    NON-remote (et sur un miss)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret_enc, meta, set_at, set_by FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    meta = (row["meta"] if row else None) or {}
    if not meta.get("base_url"):
        providers.require_credential(entity_type, connector)
    if not row:
        return None
    return {"secret": _reveal(row, entity_type, entity_id, connector, account),
            "meta": meta, "set_at": row["set_at"], "set_by": row["set_by"]}


def instance_suspended(entity_type: str, entity_id: str, connector: str, account: str = "") -> bool:
    """`meta.suspended` d'une instance — lecture SANS déchiffrer (chemin cascade/status).
    Une instance suspendue est SAUTÉE par la résolution (la cascade tombe au barreau
    suivant) mais reste LISTÉE (KeyStack : « suspendue · Réactiver ») — ADR 0044, lot 2."""
    with _connect() as c:
        row = c.execute(
            "SELECT meta->>'suspended' AS s FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            (entity_type, entity_id, connector, account)).fetchone()
    return bool(row) and row["s"] == "true"


def credential_health(entity_type: str, entity_id: str, connector: str,
                      account: str = "") -> Optional[str]:
    """La raison du REJET enregistrée sur cette ligne de coffre, ou `None` si elle va
    bien (ou n'existe pas). Lecture SANS déchiffrer — même patron qu'`instance_suspended`,
    et pour la même raison : c'est un chemin de statut, il n'a rien à faire du secret.

    `meta.health_ko` / `meta.health_reason` sont écrits par l'aide partagée
    `connectors/health.py` (`record_health`/`mark_rejected`, oto#25 lot b2 — extraite
    de `capabilities/connectors/verify.py`, son seul écrivain jusque-là). Ils
    n'avaient jusqu'au 2026-09-03 qu'un seul lecteur, `access.status_for`, qui ne
    regarde QUE les clés de palier MEMBRE : le verdict porté par une clé d'ORG — le
    seul palier possible d'un connecteur `byo_org` only comme `linear` — n'était lu
    nulle part (#541). Rend la RAISON et pas un booléen : « rejetée » sans le motif du
    fournisseur envoie chercher à l'aveugle."""
    with _connect() as c:
        row = c.execute(
            "SELECT meta->>'health_ko' AS ko, meta->>'health_reason' AS why "
            "FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            (entity_type, entity_id, connector, account)).fetchone()
    if not row or row["ko"] != "true":
        return None
    # Un rejet sans motif reste un rejet : on le NOMME plutôt que de le taire, sinon
    # `health_ko` vrai + `health_reason` nul se lirait comme une clé saine.
    return row["why"] or "rejetée au dernier test (motif non conservé)"


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


class ConcurrencyConflict(Exception):
    """Écriture optimiste rejetée : l'instance a changé depuis la lecture (ADR 0044 B1)."""


def _upsert(conn, entity_type, entity_id, connector, account, secret, set_by, meta,
            expected_version=None) -> None:
    # Chiffrement obligatoire : secret_enc porte le ciphertext. crypto.encrypt lève
    # si OTO_MCP_MASTER_KEY absente (pas de stockage plaintext).
    enc = crypto.encrypt(secret, _aad(entity_type, entity_id, connector, account))
    # Verrou optimiste (ADR 0044 B1) : chaque écriture bump `version` ; sur une édition
    # VERSIONNÉE (expected_version fourni), une garde `WHERE version = attendu` sur la
    # branche DO UPDATE fait échouer silencieusement le write si un autre l'a devancé →
    # 0 ligne touchée ⟺ conflit d'insertion + garde en échec (jamais une création).
    where = ""
    params = [entity_type, entity_id, connector, account, enc, _secret_kind(connector),
              json.dumps(meta or {}), set_by]
    if expected_version is not None:
        where = "\n        WHERE connector_credentials.version = %s"
        params.append(expected_version)
    cur = conn.execute(
        f"""
        INSERT INTO connector_credentials
            (entity_type, entity_id, connector, account, secret_enc, secret_kind, meta, set_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (entity_type, entity_id, connector, account) DO UPDATE SET
            secret_enc = EXCLUDED.secret_enc,
            secret_kind = EXCLUDED.secret_kind,
            meta = EXCLUDED.meta,
            set_by = EXCLUDED.set_by,
            set_at = NOW(),
            version = connector_credentials.version + 1{where}
        """,
        tuple(params),
    )
    if expected_version is not None and (cur.rowcount or 0) == 0:
        raise ConcurrencyConflict(
            f"{connector} ({entity_type}:{entity_id}) modifié depuis la lecture "
            f"(version ≠ {expected_version}) — relis puis rejoue.")
    # L6 pièce 2 : la ligne existe, elle a droit à son nom. **Après** le verdict du
    # verrou optimiste (une écriture devancée ne doit rien laisser derrière elle) et
    # DANS la transaction de `conn` — une pose qui échoue plus loin n'emporte pas une
    # instance orpheline. Idempotent : une rotation de secret retombe sur la même
    # instance, elle ne renaît pas.
    connector_instances.name_vault_row(conn, entity_type, entity_id, connector, account)


def _delete(conn, entity_type, entity_id, connector, account) -> bool:
    cur = conn.execute(
        "DELETE FROM connector_credentials "
        "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
        (entity_type, entity_id, connector, account),
    )
    # L6 pièce 2 : l'instance s'ARCHIVE, elle ne se supprime pas (0053-D7 — un binding,
    # une arête ou une consommation qui la désignent doivent pouvoir la relire après
    # le retrait). Inconditionnel : appelé sans ligne à supprimer, il ne fait rien —
    # et s'il reste une instance vivante sans sa ligne, il RÉPARE l'écart.
    connector_instances.revoke_instances_for_vault_rows(
        conn, entity_type, entity_id, connector, account)
    return (cur.rowcount or 0) > 0


def clear_entity_credentials(entity_type: str, entity_id: str, conn=None) -> int:
    """Purge TOUS les credentials d'une entité — et archive leurs instances.

    Existe pour que la suppression d'un groupe cesse de faire un `DELETE` en masse
    depuis `group_store` : un retrait qui contourne l'entonnoir laisse les instances
    vivantes derrière lui, c'est-à-dire des objets qui désignent des clés disparues.
    Rend le nombre de lignes de coffre supprimées."""

    def _do(c) -> int:
        n = c.execute(
            "DELETE FROM connector_credentials WHERE entity_type = %s AND entity_id = %s",
            (entity_type, str(entity_id))).rowcount or 0
        connector_instances.revoke_instances_for_vault_rows(c, entity_type, entity_id)
        return n

    if conn is not None:
        return _do(conn)
    with _connect() as c:
        return _do(c)


def clear_connector_credentials(entity_type: str, entity_id: str, connector: str,
                                conn=None) -> int:
    """Purge TOUS les comptes d'UN connecteur pour une entité — et archive leurs
    instances. Même raison que `clear_entity_credentials` : la déconnexion de tous les
    comptes Google d'un membre passait par un `DELETE` en masse."""

    def _do(c) -> int:
        n = c.execute(
            "DELETE FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s",
            (entity_type, str(entity_id), connector)).rowcount or 0
        connector_instances.revoke_instances_for_vault_rows(
            c, entity_type, entity_id, connector)
        return n

    if conn is not None:
        return _do(conn)
    with _connect() as c:
        return _do(c)


def set_credential(
    entity_type: str,
    entity_id: str,
    connector: str,
    secret: str,
    set_by: Optional[str] = None,
    meta: Optional[dict] = None,
    conn=None,
    account: str = "",
    expected_version: Optional[int] = None,
) -> None:
    """Pose/rote le secret (UPSERT). secret_kind dérivé du registre. `account`
    discrimine le multi-compte ('' = mono-compte ; ex. email Google).

    `conn` : si fourni, participe à la transaction de l'appelant (dual-write
    ATOMIQUE — le write legacy et le write canonique commitent ou rollback
    ensemble). Sinon ouvre sa propre transaction.

    ⚠️ **`meta` omis n'est PAS « ne pas y toucher » — c'est ÉCRASER par `{}`.**
    L'upsert fait `meta = EXCLUDED.meta` avec `json.dumps(meta or {})` : tout appel
    sans `meta` efface les satellites de la ligne (`instance_url`, `identity_url`,
    `connected_at`, `health_ko`…). Un appelant qui ne veut réécrire QUE le secret doit
    relire le meta et le repasser (`get_credential_with_meta(...)["meta"]`), ou passer
    par `update_meta` pour un merge. Vécu 03/08 : un writer appelé à chaque appel
    d'outil vidait le meta d'un credential dès son premier usage — on ne savait plus
    sur quelle org Salesforce la clé pointait.

    Remote (ADR 0003/0011) défini par la donnée (`meta.base_url`) → pas d'entrée
    registre ; sinon, garde d'éligibilité registre.
    """
    if not (meta and meta.get("base_url")):
        providers.require_credential(entity_type, connector)
    if not secret:
        raise ValueError("secret requis")
    if conn is not None:
        _upsert(conn, entity_type, entity_id, connector, account, secret, set_by, meta,
                expected_version=expected_version)
    else:
        with _connect() as c:
            _upsert(c, entity_type, entity_id, connector, account, secret, set_by, meta,
                    expected_version=expected_version)


def clear_credential(entity_type: str, entity_id: str, connector: str, conn=None,
                     account: str = "") -> bool:
    """Supprime le credential (ce `account` ; '' = mono-compte). `conn` fourni →
    transaction de l'appelant."""
    if conn is not None:
        return _delete(conn, entity_type, entity_id, connector, account)
    with _connect() as c:
        return _delete(c, entity_type, entity_id, connector, account)


class NamedAccountRequired(ValueError):
    """Le connecteur porte déjà des comptes NOMMÉS à ce palier : un nouveau credential
    doit dire lequel il est (`account`), sinon la résolution verrait un '' impossible
    à désigner."""


class SingleAccountConnector(ValueError):
    """Ce connecteur ne résout PAS les comptes nommés : sa cascade lit la ligne mono
    (`account=''`). Accepter un compte nommé écrirait une ligne parfaitement valide
    que rien n'irait jamais lire (oto-backend#409)."""


def guard_account_write(entity_type: str, entity_id: str, connector: str,
                        account: str, org: Optional[int] = None) -> None:
    """Garde de pose d'un credential, commune aux TROIS surfaces déclaratives
    (membre `/api/settings/api-keys`, `org.secret.set`, `group.secret.set`).

    Le coffre stocke N lignes par (entité, connecteur, compte) pour TOUS les
    connecteurs, mais seule la résolution d'un connecteur multi-compte va les lire.
    D'où la règle : **effet ou refus nommé**, jamais « accepté puis ignoré ».
    - multi-compte → cohérence des noms (`ensure_named_coexistence`) ;
    - mono-compte (ou connecteur hors registre) + compte nommé → refus, en disant
      quel connecteur et quel compte, parce que c'est ce que l'appelant doit corriger.

    Tranche sur la CARDINALITÉ, avant toute lecture du coffre : un refus ne doit pas
    dépendre de l'état des comptes déjà posés.

    ⚠️ **Par `connectors.cardinality`, jamais par la propriété du registre** : une org
    peut l'avoir surchargée en base (L6 pièce 2 c2), et c'est la MÊME fonction que lit
    la résolution (`access.cascade._is_multi_account`). Une surcharge lue ici seulement
    accepterait un deuxième compte que personne n'irait lire — le défaut exact
    d'oto-backend#409, celui que cette garde existe pour fermer. `org` = l'org de
    contexte ; les surfaces déclaratives la connaissent et la passent."""
    from .connectors import cardinality
    if cardinality.is_multi_account(connector, org):
        ensure_named_coexistence(entity_type, entity_id, connector, account)
        return
    if account:
        raise SingleAccountConnector(
            f"Le connecteur `{connector}` ne gère qu'un seul compte par entité : "
            f"son credential se résout sans nom de compte, donc `{account}` ne "
            f"serait jamais lu. Repose-le sans `account`.")


def ensure_named_coexistence(entity_type: str, entity_id: str, connector: str,
                             account: str) -> None:
    """Cohérence des comptes d'un connecteur multi-compte à UN palier, avant une pose.
    '' (mono legacy) et comptes nommés ne coexistent pas : au premier compte NOMMÉ,
    la ligne '' migre vers un label (« principal », suffixé si pris) ; poser un ''
    là où des comptes nommés existent lève `NamedAccountRequired`. Même règle pour
    le membre (route `/api/settings/api-keys`) et les paliers partagés (org/groupe,
    capacités `org.secret.set` / `group.secret.set`)."""
    existing = [r["account"] for r in list_accounts(entity_type, entity_id, connector)]
    if account:
        if "" in existing:
            taken, target, i = set(existing) | {account}, "principal", 2
            while target in taken:
                target, i = f"principal-{i}", i + 1
            rename_account(entity_type, entity_id, connector, "", target)
    elif any(a for a in existing):
        raise NamedAccountRequired(
            "Ce connecteur a déjà des comptes nommés — précise `account`.")


@dataclass(frozen=True)
class RenameOutcome:
    """Ce qu'un renommage de compte a fait — au credential ET à son instance.

    Booléen par `__bool__` (`renamed`) pour rester lisible là où seul le succès
    compte. Les deux autres champs existent parce qu'un archivage ne doit jamais être
    SILENCIEUX : quand une instance vivante occupait déjà l'arrivée (un écart : une
    instance sans sa ligne de coffre), l'arrivée gagne et le départ s'archive — le
    geste le dit ici, en plus de le journaliser."""
    renamed: bool                       # la ligne de coffre a bien été déplacée
    moved: bool = False                 # l'instance a SUIVI (id conservé)
    archived_instance_id: "int | None" = None
    kept_instance_id: "int | None" = None

    def __bool__(self) -> bool:
        return self.renamed


def rename_account(entity_type: str, entity_id: str, connector: str,
                   old_account: str, new_account: str) -> RenameOutcome:
    """Renomme le segment `account` d'un credential. Re-chiffre (l'AAD lie le
    ciphertext à son compte → un simple UPDATE du champ casserait le déchiffrement).
    Sert au backfill de la ligne mono-compte '' vers un label nommé au passage au
    multi-compte (« principal »). Atomique (upsert new + delete old, même transaction).
    Faux si la ligne source est absente.

    ⚠️ **L'instance SUIT, elle ne renaît pas** (L6 pièce 2), et c'est le geste pour
    lequel l'identifiant stable existe : laissé aux seuls crochets de `_upsert` et
    `_delete`, un renommage tuerait l'instance et en ferait naître une autre — soit
    exactement ce qu'un ref composé fait déjà, et qu'on remplace. Le déplacement se
    fait donc EN PREMIER, dans la même transaction : après lui, le crochet de pose
    trouve l'instance déjà vivante à l'arrivée et le crochet de retrait n'en trouve
    plus au départ. Aucun des deux n'a de cas particulier à connaître."""
    row = get_credential_with_meta(entity_type, entity_id, connector, old_account)
    if row is None:
        return RenameOutcome(renamed=False)
    with _connect() as c:
        moved, archivee, conservee = connector_instances.move_instance_to_account(
            c, entity_type, entity_id, connector, old_account, new_account)
        _upsert(c, entity_type, entity_id, connector, new_account, row["secret"],
                None, row.get("meta"))
        _delete(c, entity_type, entity_id, connector, old_account)
    return RenameOutcome(renamed=True, moved=moved, archived_instance_id=archivee,
                         kept_instance_id=conservee)


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


def scan_vault_health() -> dict:
    """Scan de santé du coffre (#72) : parcourt TOUTES les lignes chiffrées de
    `connector_credentials` et teste leur déchiffrement — SANS jamais garder ni
    renvoyer le plaintext. Une ligne indéchiffrable = chiffrée avec une master key
    périmée (`InvalidTag`) ou enveloppe corrompue (cf. mémoire
    `vault_stale_key_corruption`) → cible de re-pose. Agrège par connecteur et par
    type d'entité, et liste les lignes KO par leur seule IDENTITÉ (entity_type/
    entity_id/connector/account — pas de secret).

    Prérequis : la master key doit être chargée. Sans elle, AUCUN déchiffrement
    n'est possible et 100 % des lignes apparaîtraient faussement périmées → on lève
    plutôt que rapporter un scan trompeur (le message « impossible » de
    `crypto.decrypt` ≠ le message « indéchiffrable » d'un vrai InvalidTag)."""
    if not crypto.encryption_enabled():
        raise RuntimeError(
            "OTO_MCP_MASTER_KEY absente — scan de santé impossible (aucun "
            "déchiffrement possible ; toutes les lignes apparaîtraient périmées)"
        )
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_type, entity_id, connector, account, secret_enc, set_at "
            "FROM connector_credentials WHERE secret_enc IS NOT NULL "
            "ORDER BY entity_type, connector, entity_id"
        ).fetchall()
    return classify_vault_rows(rows)


def classify_vault_rows(rows) -> dict:
    """Cœur PUR (hors DB) du scan de santé : classe des lignes de credentials en
    déchiffrant chacune — la valeur est JETÉE (test booléen, jamais de plaintext en
    sortie). Extrait de `scan_vault_health` (qui garde le SELECT) pour être testable
    sans PG. Suppose la master key présente : toute exception de `crypto.decrypt` =
    InvalidTag/corruption (indéchiffrable), pas une master key absente. Chaque `row`
    porte entity_type/entity_id/connector/account/secret_enc/set_at."""
    by_connector: dict[str, dict[str, int]] = {}
    by_entity_type: dict[str, dict[str, int]] = {}
    undecryptable: list[dict] = []
    for r in rows:
        et, eid = r["entity_type"], r["entity_id"]
        cn, acct = r["connector"], r["account"]
        bc = by_connector.setdefault(cn, {"total": 0, "undecryptable": 0})
        be = by_entity_type.setdefault(et, {"total": 0, "undecryptable": 0})
        bc["total"] += 1
        be["total"] += 1
        try:
            crypto.decrypt(r["secret_enc"], _aad(et, eid, cn, acct))  # jetée
        # noqa: SILENT — l'inventaire COMPTE les lignes indéchiffrables — c'est son objet
        except Exception:
            bc["undecryptable"] += 1
            be["undecryptable"] += 1
            undecryptable.append({
                "entity_type": et, "entity_id": eid, "connector": cn,
                "account": acct or None, "set_at": r["set_at"],
            })
    total = len(rows)
    return {
        "total": total,
        "ok": total - len(undecryptable),
        "undecryptable": len(undecryptable),
        "by_connector": by_connector,
        "by_entity_type": by_entity_type,
        "undecryptable_rows": undecryptable,
    }


# (list_remote_namespaces / org_remote_namespaces retirés — ADR 0034 B4 : le
# connecteur `bridge` universel remplace la découverte data-driven per-namespace.)


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


def list_member_orgs_for(sub: str, connector: str) -> list[int]:
    """Org ids où `sub` détient un credential MEMBRE pour `connector`, triés récent
    d'abord (`set_at` DESC). Base de l'instance PERSONNELLE cross-org (issue #172,
    piste A) : `member_id = f'{org}:{sub}'` → on extrait l'org du préfixe. Le `LIKE`
    ne PRÉSÉLECTIONNE que le suffixe `:sub` (index sur (entity_type, connector)) ;
    le filtre EXACT est refait en Python (un `sub` qui serait le suffixe d'un autre
    ne passe pas). Mono/multi-compte confondus (une org apparaît une fois)."""
    esc = sub.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT entity_id, MAX(set_at) AS set_at FROM connector_credentials "
            "WHERE entity_type=%s AND connector=%s AND entity_id LIKE %s ESCAPE '\\' "
            "GROUP BY entity_id ORDER BY set_at DESC",
            (MEMBER, connector, f"%:{esc}")).fetchall()
    out: list[int] = []
    for r in rows:
        org_s, _, rsub = str(r["entity_id"]).partition(":")
        if rsub == sub and org_s.isdigit():
            out.append(int(org_s))
    return out


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
        con = providers.REGISTRY.get(connector)
        # Mounts oauth (atlassian/folkmcp) : flux fédérés encore scope 'user'
        # (barreau ultérieur).
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

