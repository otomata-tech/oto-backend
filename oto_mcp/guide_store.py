"""Guides d'usage d'oto — how-to chargés à la demande (oto-backend#111, ADR 0042).

**Source ÉDITABLE = la base, pour TOUS les scopes** — platform, org, user (bascule
2026-07-16 : le scope platform était fichiers-PR, il est désormais éditable en ligne
comme les autres, gaté platform_admin). Les fichiers `oto_mcp/guides/<slug>.md`
(front-matter `title`/`description` + corps) sont la source du **SEMIS**.

⚠️ Le semis n'a longtemps fait qu'INSÉRER : une mise à jour d'un guide dans le dépôt
n'atteignait donc jamais un environnement déjà démarré, et le guide servi en
production a dérivé de plusieurs livraisons (otomata-tech/oto#236). Depuis le
2026-09-23, le semis pose l'**empreinte** du fichier qu'il a écrit
(`props->>'seed_sha256'`) et s'en sert au démarrage suivant :

- fichier changé **et** base encore au dernier semis → le guide est **mis à jour** ;
- base éditée depuis le dernier semis → le guide est **conservé**, la divergence est
  **signalée** (santé d'instance + Sentry) ;
- même empreinte → aucune écriture.

Rien ne refuse le démarrage (DDL additif, échec ouvert, fenêtre de healthcheck finie) :
un semis en échec ou un guide divergent est un DÉFAUT DE SANTÉ d'instance, servi par
`oto_admin_guides_semis`. Une ligne posée avant l'empreinte n'est pas devinée au boot —
elle est alignée par un geste unique, `scripts/aligner_guides_plateforme.py`.

Droits : lecture = tout authentifié (platform) / org active / self ; écriture =
platform_admin / org_admin / self.

Distinct des **guides nommés** (procédures d'ORG, per-org DB, `oto_procedure`,
avec slots/versions/publish) et des readmes INIT (delivery='init', injectés au
handshake) : la notion d'« instructions server » a deux étages — toujours-injecté
(bloc A/C) vs chargé-à-la-demande (guides plateforme / procédures d'org), découvert
sans
coût de prompt.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_GUIDES_DIR = Path(__file__).resolve().parent / "guides"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_FRONT_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse(text: str) -> tuple[dict, str]:
    """(front-matter dict, corps). Front-matter absent → ({}, texte entier)."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text.strip()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, m.group(2).strip()


def file_guide(slug: str) -> Optional[dict]:
    """Le SEED fichier d'un guide plateforme, ou `None`. Le repli de lecture."""
    return next((g for g in list_file_guides() if g["slug"] == slug), None)


def list_file_guides() -> list[dict]:
    """SEEDS fichiers : `[{slug, title, description, body_md}]` trié par slug.

    Semés au boot, ET **servis en repli quand la DB n'a pas de ligne** (scope platform) —
    le même régime que le bloc A (`instructions._platform_block`), qui retombe sur sa
    constante. Sans ce repli, l'absence de ligne = guide DISPARU jusqu'au prochain
    redémarrage : supprimer un guide plateforme le retirait du catalogue au lieu de le
    rendre à sa version de référence, et un environnement neuf n'avait de guides
    qu'après un boot réussi."""
    if not _GUIDES_DIR.is_dir():
        return []
    from . import db
    out = []
    for p in sorted(_GUIDES_DIR.glob("*.md")):
        try:
            meta, body = _parse(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            logger.warning("guide illisible: %s", p.name, exc_info=True)
            continue
        titre = meta.get("title") or p.stem
        description = meta.get("description") or ""
        out.append({"slug": p.stem,
                    "title": titre,
                    "description": description,
                    "body_md": body,
                    # L'empreinte de ce que le semis POSERAIT — la même fonction des
                    # deux côtés, sinon un fichier inchangé paraîtrait avoir bougé.
                    "seed_sha256": db.empreinte_de_couche(titre, description, body)})
    return out


# Où ranger chaque verdict rendu par `db.seed_guide_db`. Le dict est la table de
# correspondance UNIQUE : un verdict neuf côté base sans case ici lève un KeyError
# au boot du banc, il ne se range pas en silence dans « inchangé ».
_CASE_DU_VERDICT = {"seme": "semes", "mis_a_jour": "mis_a_jour",
                    "inchange": "inchanges", "diverge": "divergents",
                    "sans_empreinte": "sans_empreinte"}

#: Le rapport du DERNIER semis de ce process, lu par la santé d'instance
#: (`capabilities/guides_semis.py`). Un semis par démarrage, un démarrage par
#: process : la mémoire suffit, et une table de plus ne dirait rien de plus qu'un
#: redémarrage n'efface déjà.
_DERNIER_SEMIS: dict = {"fait": False}


def _alerte(message: str, *args) -> None:
    """Un défaut du semis : au journal **et** au suivi d'erreurs (Sentry).

    Le journal seul n'a pas de destinataire — c'est précisément comme ça que le guide
    `datastore-semantics` est resté périmé en production pendant plusieurs livraisons
    sans que personne ne l'apprenne. Même canal et même forme que `tenancy._refus`.
    Fail-open : un démarrage ne casse pas parce que le suivi d'erreurs est indisponible.
    """
    logger.warning(message, *args)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("oto.semis_guides", "defaut")
            sentry_sdk.capture_message(message % args if args else message,
                                       level="error")
    # noqa: SILENT — le suivi d'erreurs n'empêche jamais un démarrage
    except Exception:
        pass


def seed_platform_guides() -> dict:
    """Sème les guides plateforme depuis les fichiers `guides/*.md` (au démarrage).

    Rend le rapport du semis — c'est lui que sert la santé d'instance. Ne LÈVE pas
    sur un guide : un slug en échec est rangé dans le rapport et signalé, les autres
    sont semés. Refuser le démarrage n'est pas une option (fenêtre de healthcheck
    finie, ADR : DDL additif et échec ouvert étape par étape) ; le témoin est le
    défaut de santé, pas le refus.
    """
    from . import db
    rapport: dict = {"semes": [], "mis_a_jour": [], "inchanges": [], "divergents": [],
                     "sans_empreinte": [], "echecs": {}}
    for g in list_file_guides():
        try:
            verdict = db.seed_guide_db("platform", PLATFORM_OWNER, g["slug"],
                                       g["body_md"], g["title"], g["description"],
                                       seed_sha256=g["seed_sha256"])
        except Exception as e:  # noqa: BLE001 — un guide en échec n'emporte pas les autres
            logger.warning("semis du guide plateforme `%s` en échec : %s",
                           g["slug"], e, exc_info=True)
            rapport["echecs"][g["slug"]] = f"{type(e).__name__}: {e}"
            continue
        rapport[_CASE_DU_VERDICT[verdict]].append(g["slug"])

    _DERNIER_SEMIS.clear()
    _DERNIER_SEMIS.update({"fait": True,
                           "at": datetime.now(timezone.utc).isoformat(), **rapport})

    # Groupé : une base injoignable fait échouer les vingt guides, et vingt alertes
    # identiques ne disent rien de plus qu'une seule qui les nomme.
    if rapport["echecs"]:
        _alerte("guides non semés : %s",
                ", ".join(f"{slug} ({err})" for slug, err in sorted(rapport["echecs"].items())))
    # Un par guide, en revanche : chaque divergence est un texte servi à réconcilier,
    # et les agréger en ferait une seule issue qu'on ferme en en traitant une seule.
    for slug in rapport["divergents"]:
        _alerte("guide divergent : `%s` a été édité en base depuis son dernier semis — "
                "le fichier du dépôt n'est PAS servi (ADR 0042 : la base fait foi ; "
                "réconcilier ou aligner avec scripts/aligner_guides_plateforme.py)", slug)
    # Sans empreinte : état connu et daté (toute la population d'avant #236). Au
    # journal, pas au suivi d'erreurs — le geste de maintenance est le traitement,
    # et une alerte par guide à chaque démarrage jusqu'à ce qu'il tourne serait du bruit.
    if rapport["sans_empreinte"]:
        logger.warning("guides sans empreinte de semis (%d) : %s — geste de "
                       "maintenance `scripts/aligner_guides_plateforme.py` pas encore joué",
                       len(rapport["sans_empreinte"]), ", ".join(rapport["sans_empreinte"]))
    return rapport


def sante_du_semis() -> dict:
    """L'état du semis de CE process, en défauts nommés — la santé d'instance des
    guides plateforme. `fait: false` = le démarrage n'a pas (encore) semé, ce qui est
    en soi un défaut : personne ne peut affirmer que les fichiers sont servis."""
    etat = dict(_DERNIER_SEMIS)
    defauts = []
    if not etat.get("fait"):
        defauts.append({"code": "semis_absent", "slugs": [],
                        "detail": ("Aucun semis de guides plateforme dans ce process — "
                                   "le démarrage ne l'a pas joué, ou pas encore.")})
    for slug, err in sorted((etat.get("echecs") or {}).items()):
        defauts.append({"code": "guides_non_semes", "slugs": [slug], "detail": err})
    for slug in etat.get("divergents") or []:
        defauts.append({"code": "guide_divergent", "slugs": [slug],
                        "detail": ("Édité en base depuis le dernier semis : le fichier "
                                   "du dépôt n'est pas servi.")})
    if etat.get("sans_empreinte"):
        defauts.append({"code": "guides_sans_empreinte",
                        "slugs": list(etat["sans_empreinte"]),
                        "detail": ("Posés avant que le semis n'empreinte : le démarrage "
                                   "ne devine pas lequel du fichier ou de la base fait "
                                   "foi. Geste unique : "
                                   "`python -m scripts.aligner_guides_plateforme`.")})
    return {"fait": bool(etat.get("fait")), "at": etat.get("at"),
            "semes": etat.get("semes") or [], "mis_a_jour": etat.get("mis_a_jour") or [],
            "inchanges": etat.get("inchanges") or [],
            "divergents": etat.get("divergents") or [],
            "sans_empreinte": etat.get("sans_empreinte") or [],
            "echecs": etat.get("echecs") or {}, "defauts": defauts}


# Prose INIT dans `guides` (delivery='init'). Slugs canoniques par scope :
# platform = la clé passée (secret_sauce) ; org/group/user = 'readme'.
PLATFORM_OWNER = "platform"
PLATFORM_SLUG = "secret_sauce"
INIT_SLUG = "readme"
# Scopes dont la prose INIT vit dans `guides` (ADR 0042). TOUS depuis le barreau 2 :
# le readme d'org/group est sorti de `*_instructions[claude_md]` (split readme↔procédure,
# les procédures gardent leur table + versioning). Owner = org.id/group.id::text.
# `tenant` : le socle servi aux comptes d'un tenant tiers, à la place du socle
# plateforme. Owner = le SLUG du tenant (pas un id numérique : le slug est déjà la
# clé stable du registre, et il est lisible dans la table). Un tenant sans ligne
# retombe sur le socle plateforme — donc ajouter ce scope ne change rien tant que
# personne n'écrit.
_INIT_IN_GUIDES = ("platform", "tenant", "user", "org", "group")


def _init_ref(scope: str, ident: Optional[str]) -> tuple[str, str]:
    """(owner_id de colonne, slug) d'un readme init dans `guides`. Pour platform,
    `ident` EST le slug (la clé, ex. secret_sauce), l'owner est constant."""
    if scope == "platform":
        return PLATFORM_OWNER, (ident or PLATFORM_SLUG)
    return str(ident), INIT_SLUG


def init_key(scope: str, ident: Optional[str]) -> tuple[str, str]:
    """La clé STOCKÉE `(owner_id, slug)` d'un readme init — celle sous laquelle ses
    versions sont gardées (`db/guide_revisions.py`). Refuse un scope sans readme."""
    if scope not in _INIT_IN_GUIDES:
        raise GuideError(f"init_key: scope `{scope}` sans readme.")
    return _init_ref(scope, ident)


def init_guide_body(scope: str, owner_id: Optional[str] = None) -> Optional[str]:
    """Corps BRUT (stripped) de la prose « init » d'un scope. None si absent/vide/erreur
    (**fail-open** ; le rendu — header, variables, ordre, seed plateforme — reste chez
    l'appelant `instructions.py`).

    Tous les scopes (platform/user/org/group) = `guides` delivery='init' (ADR 0042)."""
    try:
        if scope in _INIT_IN_GUIDES:
            from . import db
            owner, slug = _init_ref(scope, owner_id)
            row = db.get_init_guide_db(scope, owner, slug)
        else:
            return None
    except Exception:  # noqa: BLE001
        logger.warning("init_guide_body(%s, %s) échec (fail-open)", scope, owner_id,
                       exc_info=True)
        return None
    body = ((row or {}).get("body_md") or "").strip()
    return body or None


def get_init_guide(scope: str, owner_id: Optional[str] = None) -> dict:
    """État d'un readme init (scopes dans `guides`) : `{body_md, updated_at}`. Jamais
    None — un owner sans ligne renvoie l'état vide. Sert les vues d'édition."""
    if scope not in _INIT_IN_GUIDES:
        raise GuideError(f"get_init_guide: scope `{scope}` pas encore dans guides.")
    from . import db
    owner, slug = _init_ref(scope, owner_id)
    row = db.get_init_guide_db(scope, owner, slug)
    # `rev` (signal #1203) : ce qu'une écriture repasse en `expected_rev` pour refuser
    # d'écraser un readme posé entre-temps — `REV_ABSENT` quand il n'y a rien.
    return {"body_md": (row or {}).get("body_md") or "",
            "updated_at": (row or {}).get("updated_at"), "rev": db.rev_de_couche(row)}


def set_init_guide(scope: str, owner_id: Optional[str], body_md: str, *,
                   edited_by: Optional[str], reason: str = "init",
                   expected_rev: Optional[str] = None) -> dict:
    """Écrit un readme init (upsert). Renvoie `{body_md, updated_at}`. Scopes dans
    `guides` seulement (platform/user au barreau 1).

    `edited_by` (OBLIGATOIRE, `None` explicite pour un geste sans compte) : qui écrit —
    le readme remplacé est gardé comme version à son nom (`db/guide_revisions.py`).
    `reason` : `init`, `delete` (le vider), `restore`."""
    if scope not in _INIT_IN_GUIDES:
        raise GuideError(f"set_init_guide: scope `{scope}` pas encore dans guides.")
    from . import db
    owner, slug = _init_ref(scope, owner_id)
    row = db.set_init_guide_db(scope, owner, slug, body_md, expected_rev=expected_rev,
                               edited_by=edited_by, reason=reason)
    if row is None:
        raise GuideDeliveryConflict(
            f"`{slug}` (scope {scope}) porte déjà un guide À CHARGER (`delivery="
            f"'on-demand'`), pas le readme injecté : écrire ici en remplacerait le "
            f"corps — refusé, rien n'a été écrit. Lis-le avec "
            f"`oto_guide(op='read', scope='{scope}', slug='{slug}')` ; s'il n'a plus "
            f"lieu d'être, retire-le (`op='delete'`) avant d'écrire le readme.")
    return {"body_md": row.get("body_md") or "", "updated_at": row.get("updated_at"),
            "rev": db.rev_de_couche(row), "previous_rev": row.get("previous_rev")}


def seed_init_guide(scope: str, ident: Optional[str], body_md: str) -> None:
    """Pose le défaut d'un readme init s'il n'existe pas (boot, idempotent)."""
    from . import db
    owner, slug = _init_ref(scope, ident)
    db.seed_init_guide_db(scope, owner, slug, body_md)


# --- Guides ON-DEMAND scopés (ADR 0042 B5, tout-DB 2026-07-16) ----------------------

class GuideError(ValueError):
    """Écriture de guide invalide (slug mal formé, scope non éditable…)."""


class GuideDeliveryConflict(GuideError):
    """La clé `(scope, owner, slug)` porte déjà une couche de l'AUTRE livraison.

    Sous-classe de `GuideError` pour que les appelants qui rattrapaient déjà la
    famille continuent de le faire ; les surfaces qui veulent lever le refus à part
    (409 plutôt que 400) l'attrapent AVANT."""


def _slug_ok(slug: str) -> bool:
    return bool(_SLUG_RE.match(slug or ""))


def _tenant_de(sub: Optional[str]) -> Optional[str]:
    """Le slug du tenant de ce compte, ou None s'il relève de la plateforme.

    **Pas de filet ici, et c'est délibéré.** Le socle d'accueil, lui, avale tout
    (`instructions._socle_for`, fail-open à trois détentes) parce qu'il s'exécute à
    CHAQUE ouverture de session : une exception y coûterait la connexion entière. Lire
    un guide est un appel d'outil ordinaire — un registre illisible doit s'y dire, pas
    se traduire en « tiens, la notice de quelqu'un d'autre ». Servir notre prose à un
    partenaire parce qu'une lecture a échoué est précisément le silence qu'on ferme
    partout ailleurs.
    """
    if not sub:
        return None
    from . import tenancy
    slug = tenancy.current().tenant_of(sub)
    return None if not slug or slug == tenancy.primary_slug() else slug


def list_guides_for(sub: Optional[str] = None, org_id: Optional[int] = None) -> list[dict]:
    """Guides on-demand VISIBLES par le caller : plateforme ∪ org active ∪ user —
    tout en DB. Chaque entrée porte son `scope`. Sans les corps."""
    from . import db
    # Catalogue plateforme = lignes DB ∪ seeds fichiers, la DB primant sur son slug.
    # L'union, sinon le catalogue mentirait au repli de lecture : un guide servable
    # (fichier sans ligne) resterait invisible, donc introuvable.
    en_db = {g["slug"]: g for g in db.list_guides_db("platform", PLATFORM_OWNER)}
    fichiers = {g["slug"]: g for g in list_file_guides()}
    par_slug = {slug: {"slug": slug, "scope": "platform",
                       "title": (en_db.get(slug) or fichiers[slug])["title"],
                       "description": (en_db.get(slug) or fichiers[slug])["description"]}
                for slug in sorted(set(en_db) | set(fichiers))}
    # Le tenant REMPLACE notre entrée de même slug, il ne s'y ajoute pas : deux lignes
    # pour un slug feraient choisir l'agent entre deux guides dont un seul lui sera
    # rendu — le catalogue mentirait sur la lecture, exactement ce que l'union
    # ci-dessus évite pour le repli fichier.
    if (slug_tenant := _tenant_de(sub)):
        for g in db.list_guides_db("tenant", slug_tenant):
            par_slug[g["slug"]] = {"slug": g["slug"], "scope": "tenant",
                                   "title": g["title"], "description": g["description"]}
    out = [par_slug[slug] for slug in sorted(par_slug)]
    if org_id is not None:
        out += [{"slug": g["slug"], "scope": "org", "title": g["title"],
                 "description": g["description"]} for g in db.list_guides_db("org", str(org_id))]
    if sub:
        out += [{"slug": g["slug"], "scope": "user", "title": g["title"],
                 "description": g["description"]} for g in db.list_guides_db("user", sub)]
    return out


def read_guide_scoped(slug: str, *, scope: Optional[str] = None,
                      org_id: Optional[int] = None, sub: Optional[str] = None) -> Optional[dict]:
    """Lit un guide on-demand. `scope` explicite, sinon cherche tenant → plateforme →
    org → user (1er match). Renvoie `{slug, scope, title, description, body_md}` ou
    None."""
    from . import db
    # `tenant` EN TÊTE : un partenaire qui a rédigé son guide doit servir le sien, pas
    # le nôtre. C'est le même cran que le socle d'accueil (`instructions._socle_for`,
    # 13/08) — sans lui, le texte le plus lu après le socle reste au niveau plateforme
    # alors qu'il décrit un produit.
    for sc in ([scope] if scope else ["tenant", "platform", "org", "user"]):
        if sc == "tenant":
            slug_tenant = _tenant_de(sub)
            g = db.get_guide_db("tenant", slug_tenant, slug) if slug_tenant else None
            if g:
                return {"slug": slug, "scope": "tenant", "title": g["title"],
                        "description": g["description"], "body_md": g["body_md"]}
        elif sc == "platform":
            g = db.get_guide_db("platform", PLATFORM_OWNER, slug)
            if not g:
                # Repli sur le SEED fichier — comme le bloc A retombe sur sa constante.
                # Une ligne absente rend le guide à sa version de référence ; elle ne le
                # fait pas disparaître.
                g = file_guide(slug)
            if g:
                return {"slug": slug, "scope": "platform", "title": g["title"],
                        "description": g["description"], "body_md": g["body_md"]}
        elif sc == "org" and org_id is not None:
            g = db.get_guide_db("org", str(org_id), slug)
            if g:
                return {"slug": slug, "scope": "org", "title": g["title"],
                        "description": g["description"], "body_md": g["body_md"]}
        elif sc == "user" and sub:
            g = db.get_guide_db("user", sub, slug)
            if g:
                return {"slug": slug, "scope": "user", "title": g["title"],
                        "description": g["description"], "body_md": g["body_md"]}
    return None


# Scopes où un guide ON-DEMAND s'écrit. `group` n'y est pas : l'étage équipe n'existe
# qu'en prose init.
_ONDEMAND_EDITABLE = ("platform", "tenant", "org", "user")
_ONDEMAND_EDITABLE_MSG = "scope éditable = platform | tenant | org | user."


def set_guide(scope: str, owner_id: str, slug: str, body_md: str,
              title: str = "", description: str = "", *,
              edited_by: Optional[str], reason: str = "write",
              expected_rev: Optional[str] = None) -> dict:
    """Crée/met à jour un guide on-demand (scope `platform`|`tenant`|`org`|`user` —
    l'AUTZ par scope est du ressort de l'appelant : platform_admin (platform, tenant) /
    org_admin / self). Slug strict. Renvoie `{slug, scope, title, description}`.

    `tenant` : la lecture servait déjà le guide d'un tenant avant le nôtre (même slug =
    le sien) ; il manquait l'écriture, faute de quoi un besoin propre à un partenaire
    n'avait d'autre place que le cœur. Owner = le slug du tenant, validé par l'appelant.

    `edited_by` (OBLIGATOIRE) : qui écrit — ce que l'écriture remplace est gardé comme
    version à son nom. `reason` : `write`, ou `restore` pour une restauration."""
    if scope not in _ONDEMAND_EDITABLE:
        raise GuideError(_ONDEMAND_EDITABLE_MSG)
    if not _slug_ok(slug):
        raise GuideError("slug invalide (min. `^[a-z0-9][a-z0-9-]*$`).")
    if not (body_md or "").strip():
        raise GuideError("body_md requis.")
    from . import db
    row = db.set_guide_db(scope, str(owner_id), slug, body_md.strip(),
                          (title or "").strip(), (description or "").strip(),
                          expected_rev=expected_rev, edited_by=edited_by, reason=reason)
    if row is None:
        raise GuideDeliveryConflict(
            f"`{slug}` (scope {scope}) n'est pas un guide à charger : c'est le readme "
            f"INJECTÉ de ce périmètre, concaténé au début de chaque session. Écrire "
            f"ici en REMPLACERAIT le corps pour tout le monde, et le guide resterait "
            f"introuvable à la lecture — refusé, rien n'a été écrit. Pour éditer ce "
            f"readme : `oto_guide(op='write', scope='{scope}', delivery='init')`. "
            f"Pour un guide à charger : choisis un autre slug que `{slug}`.")
    return {"slug": slug, "scope": scope, "title": row["title"],
            "description": row["description"], "rev": db.rev_de_couche(row),
            "previous_rev": row.get("previous_rev")}


def patch_guide(scope: str, owner_id: str, slug: str, appliquer, *,
                edited_by: Optional[str],
                expected_rev: Optional[str] = None) -> Optional[dict]:
    """Modifie le CORPS d'un guide on-demand EXISTANT : `appliquer(corps) -> corps`,
    joué par la base sous le verrou de la ligne (`db.patch_guide_db`) — l'édition
    partielle ne réémet pas le corps et ne perd pas une écriture concurrente.

    Mêmes gardes de scope et de slug que `set_guide`, même AUTZ du ressort de
    l'appelant. Renvoie l'ACCUSÉ `{slug, scope, title, description, updated_at, rev,
    body_md_length}` — jamais le corps —, ou `None` si aucun guide ne porte ce slug
    (un patch ne crée rien). Lève `db.GuideConflict` si `expected_rev` n'est plus la
    version en base, `GuideDeliveryConflict` si la clé porte le readme injecté."""
    if scope not in _ONDEMAND_EDITABLE:
        raise GuideError(_ONDEMAND_EDITABLE_MSG)
    if not _slug_ok(slug):
        raise GuideError("slug invalide (min. `^[a-z0-9][a-z0-9-]*$`).")
    from . import db
    row = db.patch_guide_db(scope, str(owner_id), slug, appliquer,
                            expected_rev=expected_rev, edited_by=edited_by)
    if row is None:
        if db.get_init_guide_db(scope, str(owner_id), slug) is not None:
            raise GuideDeliveryConflict(
                f"`{slug}` (scope {scope}) is not an on-demand guide: it is the "
                f"readme INJECTED into every session of that scope. Nothing was "
                f"written. A readme is not patched, it is rewritten whole: "
                f"`oto_guide(op='write', scope='{scope}', delivery='init')`.")
        return None
    return {"slug": slug, "scope": scope, "title": row["title"],
            "description": row["description"], "updated_at": row.get("updated_at"),
            "rev": db.guide_rev(row["title"], row["description"], row["body_md"]),
            "body_md_length": len(row["body_md"] or "")}


def delete_guide(scope: str, owner_id: str, slug: str, *,
                 edited_by: Optional[str],
                 expected_rev: Optional[str] = None) -> bool:
    """Supprime un guide on-demand ; il reste relisible et restaurable comme version
    (`db/guide_revisions.py`), gardée au nom de `edited_by` (OBLIGATOIRE)."""
    if scope not in _ONDEMAND_EDITABLE:
        raise GuideError(_ONDEMAND_EDITABLE_MSG)
    from . import db
    return db.delete_guide_db(scope, str(owner_id), slug, expected_rev=expected_rev,
                              edited_by=edited_by)


def guides_index_md(sub: Optional[str] = None, org_id: Optional[int] = None) -> str:
    """Index markdown des guides VISIBLES par le caller (plateforme ∪ org active ∪ user)
    — enrichit la description de `oto_guide` au `tools/list`, per-(sub, org), même pattern
    que `skills_index_md` pour les guides. Sans sub/org = plateforme seule (stdio/boot).
    '' si aucun guide."""
    guides = list_guides_for(sub, org_id)
    if not guides:
        return ""
    _tag = {"platform": "", "org": " [org]", "user": " [perso]"}
    lines = ["Guides disponibles (charge le corps avec `oto_guide(op=read, slug=…)`) :"]
    for g in guides:
        lines.append(f"- {g['slug']} — {g['title']}"
                     + (f" : {g['description']}" if g.get("description") else "")
                     + _tag.get(g.get("scope", "platform"), ""))
    return "\n".join(lines)
