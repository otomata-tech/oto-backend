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

# `anthropic_cle` : la vérification d'une clé `anthropic` à la pose (`preparer_pose`). Il
# importe ce module en retour, pour ses exceptions — import mutuel de MODULES, sans rien
# lire avant l'appel.
from . import anthropic_cle, providers, crypto
from .db import _connect, connector_instances
from .db.lecture_d_appel import lecture_d_appel

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
# pour la famille oauth (google, flux dédié ; atlassian/folkmcp jusqu'au 09/09/2026) en
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


def entity_for_scope(scope: str, org_id: int, sub: str,
                     group_id: Optional[int] = None) -> tuple[str, str]:
    """`(entity_type, entity_id)` de la ligne qu'un flux « Connecter » écrit au palier
    choisi (`member` | `org` | `group`) — la même table pour tous les flux."""
    if scope == ORG:
        return ORG, str(org_id)
    if scope == "group":
        if group_id is None:
            raise ValueError("palier équipe sans équipe")
        return "group", str(group_id)
    if scope == MEMBER:
        return MEMBER, member_id(org_id, sub)
    raise ValueError(f"palier inconnu : {scope!r}")


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


@lecture_d_appel
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


def services_pretes(entity_type: str, entity_id: str, connector: str,
                    account: str = "") -> dict[str, list]:
    """`{cible: [services]}` — les services qu'un prêt couvre, par cible du `share_side`
    (`access/prets.PORTEURS_A_SERVICES`). `{}` si la ligne n'existe pas ou ne prête
    aucun service : une cible absente n'a AUCUN service prêté, jamais « tous »."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT share_services FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            (entity_type, entity_id, connector, account)).fetchone()
    return dict(row["share_services"] or {}) if row else {}


def prets_de_l_org(org_id: int) -> list[dict]:
    """Les comptes que des MEMBRES de `org_id` prêtent : leurs lignes du coffre dont le
    `share_side` n'est pas vide (ADR 0044). Métadonnées seulement, le secret n'est
    jamais lu. Sert la vue d'admin des prêts de l'org (`org.loans.list`)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT entity_id, connector, account, share_side, share_services "
            "FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id LIKE %s AND share_side <> '[]'::jsonb "
            "ORDER BY entity_id, connector, account",
            (MEMBER, f"{int(org_id)}:%")).fetchall()


def set_instance_sharing(entity_type: str, entity_id: str, connector: str,
                         account: str = "", *, share_down=None, share_side=None,
                         share_services=None, journal: Sequence[dict] = ()) -> bool:
    """Met à jour `share_down`/`share_side`/`share_services` d'une instance EXISTANTE (ADR 0044) sans
    toucher au secret ; bump `version`. Ne met à jour que les champs fournis (non
    None). Renvoie False si aucune ligne (⟹ pas d'instance à partager — l'appelant
    lève actionnable). Le partage présuppose que la clé existe. `journal` : les gestes
    de prêt à noter (`db.loan_events.noter`), dans la même transaction."""
    sets, params = ["version = version + 1"], []
    if share_down is not None:
        sets.append("share_down = %s::jsonb"); params.append(json.dumps(share_down))
    if share_side is not None:
        sets.append("share_side = %s::jsonb"); params.append(json.dumps(share_side))
    if share_services is not None:
        sets.append("share_services = %s::jsonb"); params.append(json.dumps(share_services))
    if len(sets) == 1:
        return True  # rien à changer
    params += [entity_type, entity_id, connector, account]
    from .db import loan_events
    with _connect() as conn, conn.transaction():
        cur = conn.execute(
            f"UPDATE connector_credentials SET {', '.join(sets)} "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            tuple(params))
        if (cur.rowcount or 0) == 0:
            return False
        for geste in journal:
            loan_events.noter(conn, entity_type=entity_type, entity_id=entity_id,
                              connector=connector, account=account, **geste)
    return True


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


@lecture_d_appel
def list_all_platform_instances() -> dict[str, list[dict]]:
    """`{provider: [instances]}` pour TOUT le scope PLATEFORME, en UNE lecture —
    même forme que `list_platform_instances(provider)`, groupée. Destinée au
    chemin `/api/me` (`status_for`) : la marche d'un compte y visite ~30 connecteurs
    et lisait `list_platform_instances` autant de fois (une par provider atteignant
    le barreau plateforme, doublé côté chaîne de grants — `grants_chain.platform_rung`
    lit la même table). Ordre par connecteur puis récent d'abord, identique à
    `list_platform_instances` (pas de garantie inter-provider)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, entity_id AS label, share_mode, share_down, "
            "share_side, meta FROM connector_credentials WHERE entity_type=%s "
            "ORDER BY connector, set_at DESC", (PLATFORM,)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["connector"], []).append(
            {"label": r["label"], "share_mode": r["share_mode"],
             "share_down": r["share_down"] or [], "share_side": r["share_side"] or [],
             "meta": r["meta"] or {}})
    return out


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


#: La clé de `meta` qui porte les scopes RÉVOQUÉS d'une instance plateforme (F-046).
#: Miroir de `access.platform_grant.DENIED_META_KEY` (la couche `access` lit ce module,
#: pas l'inverse) ; `tests/test_cle_plateforme_equite_f046_live.py` tient l'égalité.
_REVOQUES = "share_denied"

#: Ce que `meta` porte des ACCORDS d'une clé plateforme — pas de la clé elle-même. Une
#: re-pose (rotation du secret) les garde : `set_platform_key`.
_META_DES_ACCORDS = ("rate_limit", "rate_limit_by", _REVOQUES)


def _lignes_plateforme_verrouillees(conn, provider: str) -> list[dict]:
    """Les lignes PLATEFORME de `provider`, récente d'abord, VERROUILLÉES jusqu'à la fin
    de la transaction. Accorder, révoquer et re-poser relisent `meta` puis la
    réécrivent : sans verrou, deux gestes concurrents se croisent et le dernier écrit
    efface l'autre — une révocation perdue sans un mot."""
    return conn.execute(
        "SELECT entity_id AS label, share_mode, share_down, meta FROM connector_credentials "
        "WHERE entity_type=%s AND connector=%s AND account='' ORDER BY set_at DESC "
        "FOR UPDATE", (PLATFORM, provider)).fetchall()


def _sans_refus(meta: dict, scope: str) -> dict:
    """`meta` dont le refus de `scope` est levé (`share_denied` retiré s'il se vide)."""
    meta = dict(meta)
    refus = [s for s in (meta.get(_REVOQUES) or []) if s != scope]
    if refus:
        meta[_REVOQUES] = refus
    else:
        meta.pop(_REVOQUES, None)
    return meta


def platform_grant(provider: str, scope: str, daily_quota: "int | None" = None,
                   label: "str | None" = None) -> None:
    """ADR 0044 §F R4 : accorde l'accès plateforme à `scope` (`user:<sub>` | `group:<gid>`
    (#1239) | `org:<id>`) sur
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
        lignes = _lignes_plateforme_verrouillees(conn, provider)
        label = label or (lignes[0]["label"] if lignes else None)
        if label is None:
            raise ValueError(f"aucune instance plateforme pour {provider!r}")
        # Le refus se lit sur toutes les clés du connecteur (`platform_revoke`) : lever le
        # blocage le lève sur chacune, sinon une autre clé refuserait encore.
        for autre in lignes:
            refus = (autre["meta"] or {}).get(_REVOQUES) or []
            if autre["label"] != label and scope in refus:
                conn.execute(
                    "UPDATE connector_credentials SET meta=%s::jsonb, version=version+1 "
                    "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
                    (json.dumps(_sans_refus(autre["meta"] or {}, scope)), PLATFORM,
                     autre["label"], provider))
        row = next((r for r in lignes if r["label"] == label), None)
        if row is None:
            raise ValueError(f"aucune instance plateforme {label!r} pour {provider!r}")
        down, meta, mode = list(row["share_down"] or []), dict(row["meta"] or {}), row["share_mode"]
        # F-046 : une clé payante OUVERTE À TOUS (`open`, `share_down` vide) le reste.
        # Accorder y ajoutait le scope au `share_down` en la FERMANT : lever le blocage
        # d'une org coupait la clé à toutes les autres — le geste du 31/07, par l'autre
        # bout. Sur une telle clé, accorder = lever la révocation + poser le quota.
        ouverte_a_tous = mode != "closed" and not down
        if not free_tier and not ouverte_a_tous:
            mode = "closed"
            if scope not in down:
                down.append(scope)
        # Accorder lève la révocation de CE scope (`access.platform_grant._platform_denied`).
        meta = _sans_refus(meta, scope)
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
    """ADR 0044 §F R4 : retire l'accès de `scope` (retire du share_down + du rate_limit_by).

    F-046 : retirer du `share_down` ne coupait RIEN sur une clé ouverte à tous (`open`,
    `share_down` vide) — un blocage de crédit sans effet. Le scope est désormais
    INSCRIT comme révoqué (`meta.share_denied`), refus qui prime sur tout le reste à la
    lecture (`access.platform_grant._platform_denied`) ; seul `platform_grant` l'efface.
    Et retirer le DERNIER bénéficiaire d'une clé `open` à liste ne la rend plus
    ouverte à tous : elle passe `closed` (liste vide = personne), ce qu'elle disait.

    Sans `label`, le refus s'inscrit sur TOUTES les clés plateforme du connecteur : en
    ne marquant que la plus récente, une autre clé ouverte servait l'appelant révoqué
    au tour suivant de la résolution. La lecture, elle, refuse dès qu'une clé du
    connecteur porte le refus — une clé posée après la révocation ne la lève pas."""
    from . import grants_chain  # import tardif (grants_chain lit ce module)

    if grants_chain.is_chained(provider):
        # L5 : l'arête s'ARCHIVE (0053-D7, jamais de suppression — sinon la
        # consommation passée disparaît). L'accès est coupé à la lecture suivante :
        # la chaîne, ayant un avis (« révoqué »), ne retombe pas sur l'ancien chemin.
        grants_chain.revoke(provider, scope, label=label)
        return
    with _connect() as conn:
        lignes = _lignes_plateforme_verrouillees(conn, provider)
        for row in lignes:
            if label is None or row["label"] == label:
                _revoquer_ligne(conn, provider, scope, row)


def _revoquer_ligne(conn, provider: str, scope: str, row: dict) -> None:
    """Retire `scope` d'UNE ligne plateforme (verrouillée par l'appelant) et l'inscrit
    révoqué (`meta.share_denied`)."""
    avant = list(row["share_down"] or [])
    down = [s for s in avant if s != scope]
    mode = row["share_mode"]
    if mode != "closed" and avant and not down:
        mode = "closed"
    meta = dict(row["meta"] or {})
    rlb = dict(meta.get("rate_limit_by") or {})
    rlb.pop(scope, None)
    if rlb:
        meta["rate_limit_by"] = rlb
    else:
        meta.pop("rate_limit_by", None)
    refus = list(meta.get(_REVOQUES) or [])
    if scope not in refus:
        refus.append(scope)
    meta[_REVOQUES] = refus
    conn.execute(
        "UPDATE connector_credentials SET share_mode=%s, share_down=%s::jsonb, "
        "meta=%s::jsonb, version=version+1 "
        "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
        (mode, json.dumps(down), json.dumps(meta), PLATFORM, row["label"], provider))


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
# Seuls les flux de consentement des modules connecteurs la lisent
# (`zoho_oauth.app_fields`, `google_oauth.app_for`). Jamais la résolution.
#
# ⚠️ **La forme du blob est FIXE, indépendante du schéma du connecteur.** Une app
# d'éditeur est TOUJOURS `{client_id, client_secret}` — c'est sa définition, pas un
# credential dont le connecteur déclarerait les champs. Passer par `pack_secret` la
# rendait dépendante de `vault_fields` : sur `google` (`secret_kind="oauth"`, schéma
# VIDE — le consentement a son propre flux, pas de formulaire), `pack_secret` ne garde
# que la première valeur, donc le `client_secret` était PERDU à la pose et
# `get_editor_app` rendait `None` sans un mot (mesuré le 23/09/2026, en amenant Google
# sur ce cran). Le JSON explicite est byte-à-byte ce que `pack_secret` produisait déjà
# pour zoho (≥ 2 champs) : les lignes existantes se relisent telles quelles.
#
# La CLÉ (`data_center`) est une RÉGION pour zoho — et, pour google, le tenant dont
# l'app est celle du PRODUIT qui la publie, pas d'une région. ⚠️ Les deux ne partagent
# PAS le même espace de noms : la clé d'un tenant est `tenant:<slug>`
# (`tenant_app_key`, donc `editor:tenant:<slug>`), jamais le slug nu. Un tenant nommé
# comme une région (`eu`, `com`…) aurait sinon posé, lu ou retiré l'app zoho de la
# plateforme pour cette région (revue de #1063). Même rangement, même accès, même
# invariant : rien n'est ajouté au coffre.

EDITOR_PREFIX = "editor:"
TENANT_APP_PREFIX = "tenant:"


def editor_label(data_center: str) -> str:
    """`entity_id` de l'app d'éditeur pour une région (`editor:eu`)."""
    return f"{EDITOR_PREFIX}{(data_center or '').strip().lower()}"


def tenant_app_key(slug: str) -> str:
    """Clé d'app d'éditeur d'un TENANT (`tenant:tulina`) — jamais confondable avec une
    région, aucune région ne portant ce préfixe."""
    return f"{TENANT_APP_PREFIX}{(slug or '').strip().lower()}"


def tenant_of_app_key(key: str) -> Optional[str]:
    """Le slug désigné par une clé d'app d'éditeur, ou `None` pour une clé de région."""
    k = (key or "").strip().lower()
    if not k.startswith(TENANT_APP_PREFIX):
        return None
    return k[len(TENANT_APP_PREFIX):] or None


def _pack_editor_app(fields: dict) -> str:
    """Le blob d'une app d'éditeur : JSON des deux champs, toujours."""
    return json.dumps({"client_id": fields["client_id"],
                       "client_secret": fields["client_secret"]})


def _unpack_editor_app(secret: str) -> dict:
    """Inverse exact — lève `SecretUnpackError` sur un blob illisible, comme
    `unpack_secret` : une app d'éditeur illisible est une erreur de coffre, pas une
    absence (le silence ferait retomber sur le Self Client sans rien dire)."""
    try:
        loaded = json.loads(secret)
    except (ValueError, TypeError) as e:
        raise SecretUnpackError(
            f"app d'éditeur illisible : le blob stocké n'est pas du JSON "
            f"({type(e).__name__}). Reposer l'app, ou vérifier la clé maître du coffre.")
    if not isinstance(loaded, dict):
        raise SecretUnpackError(
            f"app d'éditeur illisible : le blob stocké est un "
            f"`{type(loaded).__name__}` JSON, pas un objet de champs.")
    return loaded


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
                _pack_editor_app(fields), set_by, None)


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
    fields = _unpack_editor_app(_reveal(row, PLATFORM, label, connector, "") or "")
    cid, sec = fields.get("client_id"), fields.get("client_secret")
    return {"client_id": cid, "client_secret": sec} if cid and sec else None


@lecture_d_appel
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


# --- app OAuth DÉDIÉE d'une org ou d'une équipe ---------------------------------
#
# L'app d'éditeur, un cran plus bas : l'application OAuth (`client_id`/`client_secret`,
# et pour zoho sa RÉGION, qui fait partie de l'app) qu'une org ou une équipe pose POUR
# le consentement de ses membres — sans la poser comme credential d'appel. Posée en BYO
# au palier org, elle était résolue par la cascade comme un credential (une app NUE, sans
# `refresh_token`, servie à qui n'avait pas consenti) ; rangée ici, elle ne l'est jamais.
#
# Même rangement et même invariant que l'app d'éditeur : scope `PLATFORM`, `entity_id`
# `app:org:<id>` / `app:group:<id>` — préfixe distinct de `editor:`, donc invisible de
# `list_editor_apps` — et `walk_cascade` ne propose le palier plateforme qu'aux
# connecteurs qui déclarent `auth_modes ∋ 'platform'`, ce que les connecteurs à
# consentement ne font pas (figé par `tests/test_editor_app.py`). Seul le flux de
# consentement la lit (`zoho_oauth.app_fields`).
#
# ⚠️ Rangée au palier plateforme, elle ne part pas avec un export de périmètre de son
# org (`export_perimetre` ne prend jamais `platform`) : une org exportée la repose.

DEDICATED_APP_PREFIX = "app:"
DEDICATED_APP_OWNERS = ("org", "group")


def dedicated_app_label(owner_type: str, owner_id: int) -> str:
    """`entity_id` de l'app dédiée d'une org ou d'une équipe (`app:org:12`)."""
    if owner_type not in DEDICATED_APP_OWNERS:
        raise ValueError(f"propriétaire d'app dédiée {owner_type!r} : org ou group")
    return f"{DEDICATED_APP_PREFIX}{owner_type}:{int(owner_id)}"


def set_dedicated_app(owner_type: str, owner_id: int, connector: str, fields: dict,
                      set_by: Optional[str] = None) -> None:
    """Pose/rote l'app dédiée. `fields` = `client_id` + `client_secret` + la config non
    secrète que le flux du connecteur exige (zoho : `data_center`) — JSON explicite, comme
    l'app d'éditeur, indépendant du schéma du connecteur. `_upsert` en direct, pour la même
    raison que `set_editor_app`."""
    if not (fields.get("client_id") and fields.get("client_secret")):
        raise ValueError("client_id et client_secret requis")
    with _connect() as conn:
        _upsert(conn, PLATFORM, dedicated_app_label(owner_type, owner_id), connector, "",
                json.dumps(fields), set_by, None)


def get_dedicated_app(owner_type: str, owner_id: int, connector: str) -> Optional[dict]:
    """`{fields, set_at, set_by}` de l'app dédiée, ou None. Un blob illisible LÈVE
    (`SecretUnpackError`) : le taire ferait consentir avec une autre app sans le dire."""
    label = dedicated_app_label(owner_type, owner_id)
    with _connect() as conn:
        row = conn.execute(
            "SELECT secret_enc, set_at, set_by FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=''",
            (PLATFORM, label, connector)).fetchone()
    if not row:
        return None
    return {"fields": _unpack_editor_app(_reveal(row, PLATFORM, label, connector, "") or ""),
            "set_at": row["set_at"], "set_by": row["set_by"]}


@lecture_d_appel
def dedicated_apps(org_id: Optional[int], group_id: Optional[int]) -> dict[str, list[str]]:
    """`{connector: [owners]}` — the owners (`org`, `group`) that set a dedicated app,
    for EVERY connector, without decrypting: one read per call for the whole listing
    (the three Zoho connectors of `GET /api/me/connectors` share it), not one per
    connector."""
    # Le propriétaire se relit par le label qu'on a construit, jamais en redécoupant
    # `entity_id` (un découpage d'`entity_id` est réservé au scope membre,
    # `tests/test_tenant_l2_sub_opaque.py`).
    owners = {dedicated_app_label(t, i): t
              for t, i in (("org", org_id), ("group", group_id)) if i is not None}
    if not owners:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT connector, entity_id FROM connector_credentials WHERE entity_type=%s "
            "AND account='' AND entity_id = ANY(%s)",
            (PLATFORM, list(owners))).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["connector"], []).append(owners[r["entity_id"]])
    return out


def dedicated_app_owners(connector: str, org_id: Optional[int],
                         group_id: Optional[int]) -> list[str]:
    """The owners (`org`, `group`) that set a dedicated app for `connector` — the
    promise "an app is ready" of a connector listing."""
    return dedicated_apps(org_id, group_id).get(connector, [])


def clear_dedicated_app(owner_type: str, owner_id: int, connector: str) -> bool:
    """Retire l'app dédiée. Les credentials déjà obtenus en gardent une copie (`persist`) :
    ils marchent jusqu'au refresh qui échouera si l'app est aussi retirée chez le
    fournisseur ; les nouveaux consentements retombent sur la cascade."""
    with _connect() as conn:
        return _delete(conn, PLATFORM, dedicated_app_label(owner_type, owner_id),
                       connector, "")


# `meta` JSONB porte aussi des satellites SECRETS (audit 2026-06-13, otomata#29) :
# l'`access_token` bearer dérivé d'OAuth (google) y vit en clair (le
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
    - `basic_auth` → `base64("email:password")` (le format était aussi celui du fil
      vers un mount distant, qui le décodait ; plus aucun mount ne le consomme
      depuis que `planity` est natif, mais des credentials sont stockés ainsi —
      NE PAS changer sans réécrire ce qui est au coffre) ;
    - ≥2 champs (silae & co) → `json.dumps(fields)`.

    Inverse exact : `unpack_secret`."""
    c = providers.REGISTRY.get(connector)
    if c is not None and c.secret_kind == "basic_auth":
        import base64
        return base64.b64encode(
            f"{fields.get('email', '')}:{fields.get('password', '')}".encode()
        ).decode()
    schema = c.vault_fields if c is not None else ()
    # Les champs `in_meta` ne vont pas dans le chiffré (cf. `preparer_pose`) : jamais pris
    # pour la valeur brute — sinon un workspace serait stocké à la place de la clé.
    dans_le_chiffre = {k: v for k, v in fields.items() if k not in _noms_en_meta(c)}
    if len(schema) <= 1:
        return next(iter(dans_le_chiffre.values()), "") if dans_le_chiffre else ""
    return json.dumps(dans_le_chiffre)


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
    schema = c.vault_fields if c is not None else ()
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


def _noms_en_meta(c) -> frozenset:
    return frozenset(f.name for f in (c.secret_fields if c is not None else ()) if f.in_meta)


def meta_fields(connector: str, values: dict) -> dict:
    """Les champs DÉCLARÉS `in_meta` parmi `values` (une saisie validée, ou le `meta` d'une
    ligne) — et rien d'autre : `meta` porte aussi des satellites de service
    (`health_reason`, `verified_at`…), qui ne sont pas des champs du credential."""
    noms = _noms_en_meta(providers.REGISTRY.get(connector))
    return {k: v for k, v in (values or {}).items() if k in noms}


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


class VerificationImpossible(CredentialFieldsInvalid):
    """La saisie n'a pas pu être VÉRIFIÉE chez le fournisseur (réseau, délai, 429, 5xx) :
    la pose est refusée — une clé non vérifiée ne s'écrit pas — mais rien n'est jugé de
    la saisie. Ce n'est pas une erreur de saisie : les capacités de pose la rendent en
    503, les autres refus de ce module en 400 (`capabilities/_refus_de_pose.py`)."""


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


def _guard_prefixes(c, provided: dict) -> None:
    """Refuse une valeur qui ne commence par aucun préfixe ADMIS du champ
    (`CredentialField.prefixes`) — la forme d'une clé dit ce qu'elle est.

    Sans cette garde, un jeton de session posé comme clé Anthropic s'écrit `ok: true`
    et n'échoue qu'à la réservation, chez le worker. ⚠️ Le message nomme la forme
    attendue, JAMAIS la valeur : le champ est un secret."""
    for f in (c.secret_fields if c is not None else ()):
        if not f.prefixes:
            continue
        val = str(clean_field_value(f, provided.get(f.name)) or "")
        if val and not val.startswith(f.prefixes):
            raise CredentialFieldsInvalid(
                "invalid_field_value",
                f"{f.label or f.name} : forme inattendue — attendue : "
                f"{', '.join(p + '…' for p in f.prefixes)}.")


def validate_fields(connector: str, provided: dict) -> dict:
    """Les champs RETENUS d'une saisie multi-champs, ou un refus nommé.

    SOURCE UNIQUE des trois set-paths (membre, équipe, org) : jeu fermé d'abord
    (`choices`) et préfixes admis (`prefixes`), puis les seuls champs que le
    discriminant rend PERTINENTS (`Connector.fields_for`), et parmi eux les
    `required` doivent être non vides.

    ⚠️ **Les champs non pertinents sont ÉCARTÉS, pas stockés** : repasser un
    connecteur d'`oauth2` à `bearer` ne laisse pas traîner un `client_secret` mort
    dans le coffre. C'est voulu — un champ qu'aucun mode ne lit n'a pas à survivre
    à un changement de mode."""
    c = providers.REGISTRY.get(connector)
    _guard_choices(c, provided)
    _guard_prefixes(c, provided)
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

    Un existant ILLISIBLE (ligne écrite sous une clé de chiffrement périmée, ou blob
    qui ne se relit pas au format du connecteur) n'est pas
    une erreur ici : on rend la saisie telle quelle, et la validation nommera ce qui
    manque. Une repose complète doit rester possible quand le coffre ne se relit plus."""
    c = providers.REGISTRY.get(connector)
    declared = {f.name for f in (c.secret_fields if c is not None else ())}
    if len(declared) < 2 or declared <= set(provided):
        return dict(provided)          # mono-champ, ou saisie déjà complète
    try:
        ligne = get_credential_with_meta(entity_type, entity_id, connector, account)
        prior = ({**unpack_secret(connector, ligne["secret"]),
                  **meta_fields(connector, ligne.get("meta") or {})}
                 if ligne and ligne.get("secret") else None)
    except Exception:
        # Une ligne illisible (clé de chiffrement périmée, blob hors format) ne doit pas
        # interdire de la RÉÉCRIRE en entier — mais elle se journalise : c'est un fait
        # d'exploitation, pas un détail.
        logger.warning("merge credential : coffre illisible pour %s/%s/%s (account=%r) "
                       "— saisie prise telle quelle",
                       entity_type, entity_id, connector, account, exc_info=True)
        return dict(provided)
    if prior is None:
        return dict(provided)
    # Le chiffré ET les champs `in_meta` de la même ligne : un champ rangé dans `meta` se
    # conserve à la repose comme un champ du chiffré.
    return {**{k: v for k, v in prior.items() if k in declared and k not in provided},
            **provided}


def secret_from_input(
    connector: str, api_key: Optional[str] = None, fields: Optional[dict] = None,
) -> str:
    """String secret à stocker pour un set-path PARTAGÉ (org/groupe), selon la forme
    du connecteur — SOURCE UNIQUE des capacités org.secret.set / group.secret.set
    (miroir du set-path user `api_routes_credentials.api_key_save`).

    - mono-champ (≤1 `secret_field`) → la valeur brute, reçue par `api_key` OU sous le
      NOM de son champ déclaré dans `fields` (`{"key": …}`) — la forme que la route du
      palier membre lit déjà ;
    - multi-champs (≥2, ex. zoho/silae) → `validate_fields` (jeu fermé, pertinence
      par discriminant, `required` non vides), packés via `pack_secret`.

    Lève `CredentialFieldsInvalid` (une `ValueError`) actionnable : `empty_api_key`
    (mono vide), `conflicting_api_key` (deux valeurs différentes pour la même clé),
    `missing_credentials` (champ requis absent/vide, ou aucun champ) ou
    `invalid_field_value` (valeur hors jeu fermé, ou hors des préfixes admis)."""
    c = providers.REGISTRY.get(connector)
    sfields = c.secret_fields if c is not None else ()
    if len(sfields) >= 2:
        return pack_secret(connector, validate_fields(connector, fields or {}))
    # La clé UNIQUE se lit aussi sous le nom de son champ : les fenêtres de saisie
    # rendent les valeurs par nom de champ, et chaque écran devait sinon la recopier
    # dans `api_key`. Deux fois oublié (PayFit, puis l'API cabinet de Pennylane) : la clé
    # partait dans `fields`, ignorée, et la pose était refusée « clé d'API vide » alors
    # que l'utilisateur l'avait collée.
    key = (api_key or "").strip()
    du_champ = str((fields or {}).get(sfields[0].name) or "").strip() if sfields else ""
    if key and du_champ and key != du_champ:
        raise CredentialFieldsInvalid(
            "conflicting_api_key",
            f"deux clés différentes : `api_key` et le champ `{sfields[0].name}`.")
    key = key or du_champ
    if not key:
        raise CredentialFieldsInvalid("empty_api_key", "clé d'API vide.")
    return key


def preparer_pose(entity_type: str, entity_id: str, connector: str, account: str,
                  api_key: Optional[str] = None,
                  fields: Optional[dict] = None) -> tuple[str, dict]:
    """La pose d'un palier PARTAGÉ (org, équipe, tenant) : `(secret à chiffrer, patch de
    meta)` — SOURCE UNIQUE des trois capacités, qui fusionnent le patch dans le `meta`
    qu'elles écrivent (`_upsert` REMPLACE `meta`).

    Sans champ `in_meta`, rien ne change : écriture partielle (#448) puis
    `secret_from_input`, le patch est vide. Avec (14/09/2026) :
    - le chiffré ne reçoit que ses champs (`pack_secret`), le patch porte les autres ;
    - une clé posée SEULE par `api_key` — la forme d'avant l'ajout du champ, que des
      fronts envoient encore — se lit comme le champ unique du chiffré, et l'écriture
      partielle conserve le champ `in_meta` déjà posé au lieu de l'effacer ;
    - un champ `in_meta` envoyé VIDE s'efface, comme tout champ (#448).

    Une clé posée SEULE par `api_key` sur un connecteur dont le chiffré porte plusieurs
    champs (`forager` : clé + compte) se lit aussi comme son premier champ SECRET, et
    l'écriture partielle conserve les autres. C'est ici, à la pose, que la valeur brute
    prend la forme du coffre — jamais à la lecture."""
    c = providers.REGISTRY.get(connector)
    en_meta = _noms_en_meta(c)
    if (fields is None and (api_key or "").strip()
            and (en_meta or len(c.vault_fields) >= 2)):
        champ = next((f.name for f in c.vault_fields if f.secret), c.vault_fields[0].name)
        fields = {champ: api_key}
    if fields is not None:
        fields = merge_with_existing(entity_type, entity_id, connector, account, fields)
    if not en_meta:
        return secret_from_input(connector, api_key, fields), {}
    retenus = validate_fields(connector, fields or {})
    _verifier_chez_le_fournisseur(connector, retenus)
    return pack_secret(connector, retenus), meta_fields(connector, retenus)


def _verifier_chez_le_fournisseur(connector: str, retenus: dict) -> None:
    """La clé `anthropic` vérifiée CHEZ ANTHROPIC avant d'être écrite (`anthropic_cle`) :
    clé refusée, clé d'organisation sans son workspace, ou Anthropic injoignable — la
    pose est REFUSÉE (`CredentialFieldsInvalid`, ou `VerificationImpossible` quand
    Anthropic n'a pas répondu), jamais faite sur une clé non vérifiée.

    Ici, et pas dans une capacité : `preparer_pose` est le point de passage UNIQUE des
    poses partagées (org, équipe, tenant, plateforme). La vérification porte sur la
    saisie COMPLÉTÉE par le coffre (#448) : une repose du seul workspace se vérifie avec
    la clé déjà déposée. ⚠️ Appel réseau : les handlers qui posent sont synchrones, joués
    dans le threadpool — jamais dans la boucle."""
    if connector == "anthropic":
        anthropic_cle.verifier(retenus.get("key", ""), retenus.get("workspace_id"))


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
    get_credential_with_meta. `secret_enc` est NOT NULL (#521) : une ligne lue porte
    toujours son chiffré."""
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


#: Verdict de santé « la clé authentifie, le compte est à sec » (`meta.health_verdict`).
#: Même nom que le verdict de la sonde (`connectors.verify.NO_QUOTA`).
NO_QUOTA_VERDICT = "no_quota"
#: Tête de la raison rendue par `credential_health` quand le verdict est `no_quota` :
#: c'est ce qui fait dire à la carte connecteur « recharge » et non « repose la clé »
#: (`connectors/readiness`), sans changer la forme (une chaîne) que ses lecteurs lisent.
NO_QUOTA_REASON_PREFIX = "crédits épuisés"
#: Verdict « la clé n'authentifie pas » — même nom que celui de la sonde
#: (`connectors.verify.UNAUTHORIZED`).
UNAUTHORIZED_VERDICT = "unauthorized"
#: `meta.health_source` d'une marque posée À L'APPEL. Une marque de la sonde n'en porte
#: pas : seule une marque d'appel est levée par un appel réussi (`clear_call_health`).
CALL_SOURCE = "call"


def credential_health(entity_type: str, entity_id: str, connector: str,
                      account: str = "") -> Optional[str]:
    """La raison du REJET enregistrée sur cette ligne de coffre, ou `None` si elle va
    bien (ou n'existe pas). Lecture SANS déchiffrer — même patron qu'`instance_suspended`,
    et pour la même raison : c'est un chemin de statut, il n'a rien à faire du secret.

    `meta.health_ko` / `meta.health_reason` sont écrits par l'aide partagée
    `connectors/health.py` (`record_health`/`mark_rejected`, oto#25 lot b2 — extraite
    de `capabilities/connectors/verify.py`, son seul écrivain jusque-là). Ils
    n'avaient jusqu'au 2026-09-03 qu'un seul lecteur, `access.status_for`, qui ne
    regarde QUE les clés de palier MEMBRE : le verdict porté par une clé d'ORG (par
    exemple celle de `linear`, alors `byo_org` only) n'était lu nulle part (#541). Rend la RAISON et pas un booléen : « rejetée » sans le motif du
    fournisseur envoie chercher à l'aveugle."""
    with _connect() as c:
        row = c.execute(
            "SELECT meta->>'health_ko' AS ko, meta->>'health_reason' AS why, "
            "meta->>'health_verdict' AS verdict "
            "FROM connector_credentials "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s",
            (entity_type, entity_id, connector, account)).fetchone()
    if not row or row["ko"] != "true":
        return None
    # Un rejet sans motif reste un rejet : on le NOMME plutôt que de le taire, sinon
    # `health_ko` vrai + `health_reason` nul se lirait comme une clé saine.
    why = row["why"] or "rejetée au dernier test (motif non conservé)"
    if row["verdict"] == NO_QUOTA_VERDICT:
        return f"{NO_QUOTA_REASON_PREFIX} : {why}"
    return why


def clear_call_health(entity_type: str, entity_id: str, connector: str,
                      account: str) -> bool:
    """Lève une marque posée À L'APPEL, après un appel réussi sur cette clé : un compte
    à sec (`no_quota`), ou une clé refusée vue par un appel (`unauthorized` +
    `health_source = 'call'`). Un appel réussi prouve que la clé authentifie et paie.
    Une marque de la SONDE (`unauthorized` sans source d'appel, `invalid_grant`…) n'est
    levée que par la sonde ou en reposant la clé : la sonde teste ce que l'appel ne
    teste pas (un scope manquant, par exemple).

    UNE écriture conditionnelle, aucune si la ligne est saine ou porte une autre marque.
    True si une ligne a été démarquée. Même forme que le démarquage de la sonde."""
    with _connect() as c:
        cur = c.execute(
            "UPDATE connector_credentials SET meta = meta || %s::jsonb "
            "WHERE entity_type=%s AND entity_id=%s AND connector=%s AND account=%s "
            "AND meta->>'health_ko' = 'true' "
            "AND (meta->>'health_verdict' = %s "
            "     OR (meta->>'health_verdict' = %s AND meta->>'health_source' = %s))",
            (json.dumps({"health_ko": False, "health_reason": None,
                         "health_verdict": None, "health_source": None}),
             entity_type, entity_id, connector, account,
             NO_QUOTA_VERDICT, UNAUTHORIZED_VERDICT, CALL_SOURCE))
        return (cur.rowcount or 0) > 0


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


@lecture_d_appel
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
            "SELECT meta, set_at FROM connector_credentials "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s AND account = %s",
            (entity_type, entity_id, connector, account),
        ).fetchone()
    if not row:
        return None
    return {"set_at": row["set_at"], "meta": _public_meta(row["meta"])}


def statuts_mono_compte(entity_type: str, entity_id: str) -> dict[str, dict]:
    """`{connector: credential_status(entity_type, entity_id, connector)}` for every
    MONO-account row (`account=''`) of the entity — the same projection, from ONE read
    of its inventory (`list_credentials`, read once per call under
    `db.reuse_connection()`) instead of one query per connector (#1148)."""
    return {r["connector"]: {"set_at": r["set_at"], "meta": r["meta"]}
            for r in list_credentials(entity_type, entity_id) if r["account"] == ""}


def comptes_du_palier(entity_type: str, entity_id: str, connector: str) -> list[dict]:
    """`list_accounts` projected from the entity's inventory (`list_credentials`, read once
    per call): a shared tier's accounts cost no query of their own on a listing (#1148)."""
    return [{"account": r["account"], "meta": r["meta"], "set_at": r["set_at"]}
            for r in list_credentials(entity_type, entity_id) if r["connector"] == connector]


@lecture_d_appel
def has_credential(entity_type: str, entity_id: str, connector: str, account: Optional[str] = None) -> bool:
    """Présence d'un secret SANS déchiffrer (pour status_for / surface d'attaque
    réduite : /api/me n'a besoin que du booléen, jamais de la valeur).

    `account` None = n'importe quel compte (présence du connecteur, multi-compte
    inclus) ; '' = strictement le mono-compte ; une valeur = ce compte précis.

    « Détenir » = la ligne existe, la MÊME définition que `list_credentials` : la base
    interdit une ligne sans chiffré (`secret_enc NOT NULL`, #521). Jusqu'au 23/09/2026
    cette lecture filtrait `secret_enc IS NOT NULL` et `list_credentials` non, sur une
    colonne que le schéma laissait nullable — deux définitions qui pouvaient diverger."""
    sql = ("SELECT 1 FROM connector_credentials WHERE entity_type = %s AND entity_id = %s "
           "AND connector = %s")
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
    # Active = usable : ce qui vient de devenir utilisable s'active à la prochaine
    # session des membres qu'il atteint (`connectors/usable.py`), dans CETTE transaction.
    from .connectors import usable as _usable
    _usable.invalidate(conn, entity_type, entity_id)


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
    if cur.rowcount:
        # Active = usable : ce qui n'est plus utilisable se met en pause à la prochaine
        # session des membres qu'il atteignait (`connectors/usable.py`).
        from .connectors import usable as _usable
        _usable.invalidate(conn, entity_type, entity_id)
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


def set_platform_key(label: str, connector: str, secret: str,
                     set_by: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Pose une clé PLATEFORME (console admin). Un connecteur sans palier gratuit
    (`platform_key_open=False`) la reçoit FERMÉE : elle ne sert que les orgs accordées
    (`grant_platform`). Sans ça, la ligne naissait `share_mode='open'` (défaut de la
    colonne) avec un `share_down` vide, que `access.platform_grant` lit « ouvert à
    tous » : entre la pose et le premier accord, n'importe quel compte sans clé à lui
    consommait la clé payante d'oto, sans plafond (`default_quota=0`). Une rotation
    (re-pose sous le même libellé) garde l'état de la ligne tel quel — fermée reste
    fermée, ouverte reste ouverte, bénéficiaires, quotas et révocations compris
    (F-046) ; un connecteur à palier gratuit garde sa clé ouverte.

    `secret` est déjà à la forme du coffre : la pose passe par `preparer_pose`, comme
    les paliers org, équipe et tenant (`capabilities/api_tokens._keys_create`)."""
    c = providers.REGISTRY.get(connector)
    ouverte = bool(c and getattr(c, "platform_key_open", False))
    with _connect() as conn:
        # F-046 : une RE-POSE (même libellé) est une rotation du secret, pas une
        # nouvelle clé. Elle fermait toute clé payante (une clé ouverte à tous ne
        # servait plus personne, sans un mot) et l'upsert écrasait `meta` — quotas
        # par bénéficiaire et révocations compris. On relit la ligne : son
        # `share_mode` reste le sien (`share_down`/`share_side` ne sont pas dans le
        # SET de l'upsert) et les clés d'accord de `meta` sont reportées, sauf si la
        # pose les nomme elle-même.
        # Verrouillée : un accord ou une révocation entre cette lecture et l'upsert
        # serait écrasé par le `meta` relu ici.
        existante = conn.execute(
            "SELECT meta FROM connector_credentials WHERE entity_type = %s "
            "AND entity_id = %s AND connector = %s AND account = '' FOR UPDATE",
            (PLATFORM, label, connector)).fetchone()
        if existante is not None:
            reportee = dict(meta or {})
            ancienne = existante["meta"] or {}
            for k in _META_DES_ACCORDS:
                if k in ancienne and k not in reportee:
                    reportee[k] = ancienne[k]
            meta = reportee or None
        set_credential(PLATFORM, label, connector, secret, set_by=set_by, meta=meta,
                       conn=conn)
        if not ouverte and existante is None:
            conn.execute(
                "UPDATE connector_credentials SET share_mode = 'closed' "
                "WHERE entity_type = %s AND entity_id = %s AND connector = %s "
                "AND account = ''",
                (PLATFORM, label, connector))


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
        # Le refus remonte tel quel au dashboard : il doit dire QUOI faire, pas
        # seulement le nom d'un paramètre qui ne parle qu'à un agent.
        noms = ", ".join(f"« {a} »" for a in sorted(a for a in existing if a))
        raise NamedAccountRequired(
            f"Ce connecteur a déjà des comptes nommés ({noms}) : précise lequel "
            f"remplacer (`account`, ou « Remplacer » sur sa ligne dans le "
            f"dashboard), ou donne un nom pour en ajouter un.")


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


@lecture_d_appel
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
    # Un credential qui nomme un connecteur absent du catalogue servi ne se résout plus :
    # journalisé en erreur, jamais écarté en silence (#905). Un remote se définit par sa
    # donnée (`meta.base_url`, cf. `set_credential`), pas par le registre : hors du compte.
    from .connectors import orphelins
    orphelins.signaler("connector_credentials",
                       (r["connector"] for r in rows
                        if not (r["meta"] or {}).get("base_url")))
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
            "FROM connector_credentials "
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


@lecture_d_appel
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



def rekey_personal_credentials(conn, old_sub: str, new_sub: str) -> dict:
    """Les clés PERSONNELLES d'un compte fusionné suivent la personne (#439).

    Clé personnelle = entité `user` (`entity_id` = sub) ou `member` (`org:sub`). Ce
    sont les deux seules dont l'entité porte un sub, donc les deux seules qu'une
    fusion de comptes (`db.migrate_sub`) laissait derrière elle : l'AAD dérive de
    l'entité, et un `UPDATE entity_id` nu fabriquerait une ligne indéchiffrable. Elles
    restaient donc keyées sur l'ancien identifiant — **invisibles** pour leur
    propriétaire, puisque la résolution les cherche sous le sub canonique.

    Même geste que `backfill_member_scope` et `rename_account` : déchiffrer avec
    l'AAD de l'ancienne entité, rechiffrer avec celle de la nouvelle. Mais **en
    place** (un `UPDATE` de `entity_id` + `secret_enc`) plutôt que par pose + retrait :
    la ligne garde tout le reste — partages, version, date de pose, auteur — et son
    instance la suit avec son id (`move_instance_to_owner`).

    Dans la transaction de l'appelant (`conn`), lignes verrouillées. Deux cas laissent
    la ligne EN PLACE, journalisés et comptés, jamais avalés :
    - `collision` — le compte canonique porte déjà une clé sur ce même
      (connecteur, compte), ou une instance vivante l'occupe : la sienne gagne (c'est
      celle qu'il sert aujourd'hui) ; l'ancienne n'est ni écrasée ni supprimée ;
    - `illisible` — la ligne ne se déchiffre pas (clé maîtresse périmée) : la
      rechiffrer est impossible, la déplacer la rendrait pire.

    Idempotent : rejoué, il ne trouve plus rien sous l'ancien sub (hors cas laissés).
    Ne journalise jamais un secret — des connecteurs, des comptes et des entités."""
    from .db import loan_events
    bilan = {"rekeyed": 0, "collisions": 0, "illisibles": 0}
    if not old_sub or not new_sub or old_sub == new_sub:
        return bilan
    rows = conn.execute(
        "SELECT entity_type, entity_id, connector, account, secret_enc "
        "FROM connector_credentials "
        "WHERE (entity_type = %s AND entity_id = %s) "
        "   OR (entity_type = %s AND entity_id ~ '^[0-9]+:' "
        "       AND substr(entity_id, strpos(entity_id, ':') + 1) = %s) "
        "ORDER BY entity_type, entity_id, connector, account FOR UPDATE",
        (USER, old_sub, MEMBER, old_sub)).fetchall()
    for r in rows:
        etype, ancien = r["entity_type"], r["entity_id"]
        connector, account = r["connector"], r["account"]
        nouveau = (new_sub if etype == USER
                   else member_id(int(ancien.split(":", 1)[0]), new_sub))
        occupe = conn.execute(
            "SELECT 1 FROM connector_credentials WHERE entity_type = %s "
            "AND entity_id = %s AND connector = %s AND account = %s "
            "UNION ALL SELECT 1 FROM connector_instances WHERE owner_type = %s "
            "AND owner_id = %s AND connector = %s AND account = %s "
            "AND revoked_at IS NULL LIMIT 1",
            (etype, nouveau, connector, account,
             etype, nouveau, connector, account)).fetchone()
        if occupe:
            logger.warning(
                "rekey clés personnelles : %s %s/%s (compte %r) LAISSÉE sous %s — le "
                "compte canonique porte déjà cette clé (%s), elle est gardée",
                etype, connector, ancien, account, old_sub, nouveau)
            bilan["collisions"] += 1
            continue
        try:
            secret = crypto.decrypt(r["secret_enc"],
                                    _aad(etype, ancien, connector, account))
        except Exception:
            logger.warning(
                "rekey clés personnelles : %s %s/%s (compte %r) indéchiffrable — "
                "LAISSÉE sous %s, rien n'est rechiffré", etype, connector, ancien,
                account, old_sub, exc_info=True)
            bilan["illisibles"] += 1
            continue
        conn.execute(
            "UPDATE connector_credentials SET entity_id = %s, secret_enc = %s "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s "
            "AND account = %s",
            (nouveau, crypto.encrypt(secret, _aad(etype, nouveau, connector, account)),
             etype, ancien, connector, account))
        connector_instances.move_instance_to_owner(conn, etype, ancien, nouveau,
                                                   connector, account)
        loan_events.suivre_la_ligne(conn, etype, ancien, nouveau, connector, account)
        bilan["rekeyed"] += 1
    if rows:
        logger.info("rekey clés personnelles %s → %s : %s", old_sub, new_sub, bilan)
    return bilan


def suivre_les_prets_recus(conn, old_sub: str, new_sub: str) -> int:
    """Les prêts faits À une personne la suivent quand ses comptes fusionnent (#1251).

    Un prêt nominatif est une entrée `user:<sub>` du `share_side` de la ligne du
    PRÊTEUR, et, pour un compte Google ou Microsoft, la clé de la même cible dans
    `share_services`. Sans ce repointage, l'entrée visait un sub qui n'a plus de ligne
    `users` : le compte prêté cessait de servir l'emprunteur fusionné, sans refus qui
    l'explique. Chaque `user:<old_sub>` devient `user:<new_sub>`, à sa place dans la
    liste ; si le canonique y figure déjà, l'entrée de l'ancien disparaît (pas de
    doublon) et ses services s'ajoutent aux siens (c'est la même personne : l'union est
    ce que `access.prets.exiger_service_prete` lui servait déjà avant la fusion). Les
    entrées `group:` et `org:` ne portent pas de sub : intactes.

    `share_side` et `share_services` sont HORS de l'AAD (`_aad` : entité, connecteur,
    compte) : un UPDATE de ces colonnes laisse la ligne déchiffrable, sans rechiffrer.
    Les lignes se lisent par `share_side ? 'user:<old>'` (index GIN `share_side`) :
    `share_services` n'a de clé que pour une cible du `share_side` (écrits ensemble,
    `capabilities/connectors/sharing`).

    ⚠️ Le journal des prêts (`connector_loan_events.target`) n'est PAS réécrit : c'est
    un journal, il garde la cible telle qu'elle a été nommée au geste. Conséquence
    assumée : la date de dernière pose (`loan_events.dernieres_poses`) d'un prêt repointé
    n'est plus connue sous la nouvelle cible.

    Dans la transaction de l'appelant (`conn`), lignes verrouillées. Rend le nombre de
    lignes du coffre réécrites."""
    if not old_sub or not new_sub or old_sub == new_sub:
        return 0
    ancien, nouveau = f"user:{old_sub}", f"user:{new_sub}"
    rows = conn.execute(
        "SELECT entity_type, entity_id, connector, account, share_side, share_services "
        "FROM connector_credentials WHERE share_side ? %s "
        "ORDER BY entity_type, entity_id, connector, account FOR UPDATE",
        (ancien,)).fetchall()
    for r in rows:
        side = _repointer_la_cible(r["share_side"], ancien, nouveau)
        services = dict(r["share_services"] or {})
        if ancien in services:
            herites = services.pop(ancien) or []
            siens = list(services.get(nouveau) or [])
            services[nouveau] = siens + [s for s in herites if s not in siens]
        conn.execute(
            "UPDATE connector_credentials SET share_side = %s::jsonb, "
            "share_services = %s::jsonb, version = version + 1 "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s "
            "AND account = %s",
            (json.dumps(side), json.dumps(services), r["entity_type"], r["entity_id"],
             r["connector"], r["account"]))
    if rows:
        logger.info("prêts reçus %s → %s : %d ligne(s) du coffre repointée(s)",
                    old_sub, new_sub, len(rows))
    return len(rows)


def _repointer_la_cible(cibles, ancien: str, nouveau: str) -> list:
    """`cibles` (liste de principaux) où `ancien` devient `nouveau`, à sa place ; si
    `nouveau` y figure déjà, l'entrée en trop disparaît (pas de doublon). Les autres
    entrées (`group:`, `org:`, un tiers) ne bougent pas."""
    out: list = []
    for cible in cibles or []:
        cible = nouveau if cible == ancien else cible
        if cible == nouveau and nouveau in out:
            continue
        out.append(cible)
    return out


def _quota_le_plus_strict(a: "int | None", b: "int | None") -> "int | None":
    """Le plus restrictif de deux quotas journaliers ; `None` = sans plafond
    (`access.platform_grant._platform_quota` le sert tel quel)."""
    bornes = [q for q in (a, b) if q is not None]
    return min(bornes) if bornes else None


def suivre_les_accords_recus(conn, old_sub: str, new_sub: str) -> int:
    """Les ACCORDS et les REFUS nominatifs d'une clé plateforme suivent la personne quand
    ses comptes fusionnent (#1258). Trois endroits nomment `user:<sub>` sur une ligne du
    coffre, hors du `share_side` (`suivre_les_prets_recus`) :

    - `share_denied` dans `meta` (F-046) : la RÉVOCATION. C'est le point de sécurité :
      sans repointage, le refus visait un sub sans ligne `users`, et le compte fusionné
      était servi à nouveau (`access.platform_grant._platform_denied` lit `user:<sub>`).
      `user:<old>` devient `user:<new>`, sans doublon ;
    - `share_down` : les bénéficiaires d'une clé plateforme fermée, même règle ;
    - `rate_limit_by` dans `meta` (`{scope: quota journalier}`, écrit par
      `platform_grant`, lu par `_platform_quota`) : la clé `user:<old>` devient
      `user:<new>`. Si le canonique a déjà son quota, on garde le PLUS RESTRICTIF
      (`_quota_le_plus_strict`) : un quota plafonne la dépense d'une clé payante, posé
      par un admin pour UN bénéficiaire ; la somme ouvrirait à la personne fusionnée un
      plafond que personne n'a accordé, le minimum ne dépasse jamais ce qu'un admin a
      décidé pour l'une ou l'autre de ses identités.

    Conséquence assumée : un refus de l'ancien et un accord du canonique (ou l'inverse)
    se retrouvent sur la même personne, et le refus prime à la lecture — la règle de
    F-046, la plus prudente. Seul `platform_grant` le lève.

    `meta` et `share_down` sont HORS de l'AAD (`_aad` : entité, connecteur, compte) : un
    UPDATE laisse la ligne déchiffrable. Pas d'index sur ces deux colonnes : la lecture
    balaie `connector_credentials`, une fois par fusion. Dans la transaction de
    l'appelant (`conn`), lignes verrouillées. Rend le nombre de lignes réécrites."""
    if not old_sub or not new_sub or old_sub == new_sub:
        return 0
    ancien, nouveau = f"user:{old_sub}", f"user:{new_sub}"
    rows = conn.execute(
        "SELECT entity_type, entity_id, connector, account, share_down, meta "
        "FROM connector_credentials WHERE share_down ? %s "
        "OR meta -> 'share_denied' ? %s OR meta -> 'rate_limit_by' ? %s "
        "ORDER BY entity_type, entity_id, connector, account FOR UPDATE",
        (ancien, ancien, ancien)).fetchall()
    for r in rows:
        down = _repointer_la_cible(r["share_down"], ancien, nouveau)
        meta = dict(r["meta"] or {})
        if ancien in (meta.get(_REVOQUES) or []):
            meta[_REVOQUES] = _repointer_la_cible(meta[_REVOQUES], ancien, nouveau)
        rlb = dict(meta.get("rate_limit_by") or {})
        if ancien in rlb:
            herite = rlb.pop(ancien)
            rlb[nouveau] = (_quota_le_plus_strict(herite, rlb[nouveau])
                            if nouveau in rlb else herite)
            meta["rate_limit_by"] = rlb
        conn.execute(
            "UPDATE connector_credentials SET share_down = %s::jsonb, "
            "meta = %s::jsonb, version = version + 1 "
            "WHERE entity_type = %s AND entity_id = %s AND connector = %s "
            "AND account = %s",
            (json.dumps(down), json.dumps(meta), r["entity_type"], r["entity_id"],
             r["connector"], r["account"]))
    if rows:
        logger.info("accords reçus %s → %s : %d ligne(s) du coffre repointée(s)",
                    old_sub, new_sub, len(rows))
    return len(rows)


def copy_connector_rows(old: str, new: str) -> dict:
    """COPIES every vault row of connector `old` to connector `new` — when a card hands
    its credential over to a carrier (`sharepoint` → `microsoft`).

    A copy and not a move: the base is SHARED by prod and preprod, and the code still
    served on one of them reads `old` until its tag — moving would cut it off. Removing
    the `old` rows is a later gesture, once no served code reads them.

    The AAD binds the ciphertext to its connector: decrypt with `old`'s, re-encrypt with
    `new`'s (`_upsert`, the single INSERT funnel — the instance is born with the row).
    `meta`, `set_by` follow as they are.

    Idempotent BY A MARK, not by the target's presence: each source row handled gets
    `meta.copied_to = new` (also when `new` already has a row for that account). A
    predicate on the target alone would RESURRECT an account the person removed under
    `new` at the next boot. The code that still writes `old` passes its meta back
    (rotation, reconnection): the mark survives, so a reconnected account is not copied
    twice; an account added there after the copy has no mark, and is copied at the next
    boot. An undecryptable row stays unmarked, counted and logged — never copied.
    Never logs a secret."""
    bilan = {"copied": 0, "present": 0, "unreadable": 0}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT entity_type, entity_id, account, secret_enc, meta, set_by "
            "FROM connector_credentials WHERE connector = %s "
            "AND meta->>'copied_to' IS NULL "
            "ORDER BY entity_type, entity_id, account FOR UPDATE", (old,)).fetchall()
        for r in rows:
            etype, eid, account = r["entity_type"], r["entity_id"], r["account"]
            present = conn.execute(
                "SELECT 1 FROM connector_credentials WHERE entity_type = %s "
                "AND entity_id = %s AND connector = %s AND account = %s",
                (etype, eid, new, account)).fetchone()
            if present:
                update_meta(etype, eid, old, account, {"copied_to": new}, conn=conn)
                bilan["present"] += 1
                continue
            try:
                secret = crypto.decrypt(r["secret_enc"], _aad(etype, eid, old, account))
            except Exception:
                logger.warning("copy %s → %s: %s/%s (account %r) undecryptable — not copied",
                               old, new, etype, eid, account, exc_info=True)
                bilan["unreadable"] += 1
                continue
            meta = r["meta"] if isinstance(r["meta"], dict) else json.loads(r["meta"] or "{}")
            _upsert(conn, etype, eid, new, account, secret, r["set_by"], meta)
            update_meta(etype, eid, old, account, {"copied_to": new}, conn=conn)
            bilan["copied"] += 1
    return bilan


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
        # Ex-mounts oauth (atlassian/folkmcp, retirés le 09/09/2026) : scope 'user'
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

