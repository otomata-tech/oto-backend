"""Le nom d'un outil, tel que le PRODUIT de l'utilisateur l'affiche (ADR 0052).

Un client d'un partenaire voit, dans sa conversation, la liste des outils qu'il vient
d'appeler. Elle disait `Oto doc`, `Oto project`, `Oto search` — sous le connecteur du
partenaire, dans son produit à lui. C'est la MÊME famille de défaut que le socle
d'instructions (« Sur <tenant> (Oto), tu es… », 13/08) et que les liens qui portaient
notre domaine : un texte au niveau plateforme alors qu'il décrit un produit. Sauf
qu'ici le texte n'est pas de la prose — c'est l'identifiant d'un outil, et il est
affiché à chaque appel.

Le préfixe `oto_` reste donc le nom CANONIQUE partout où un nom se stocke ou se
compare (registre, coffre de visibilité `user_disabled_tools`, journal `tool_calls`,
références `<tool:slug>` des procédures, gates par namespace). Ce module ne fait que
le TRADUIRE aux deux bords du protocole :

    tools/list   →  `oto_doc`     devient  `acme_doc`   (ce que l'utilisateur voit)
    tools/call   ←  `acme_doc`  redevient `oto_doc`     (ce que le serveur sait)

**Les deux noms sont acceptés à l'appel**, et ce n'est pas de la complaisance : la
prose déjà écrite (procédures d'org, guides, corps de guide, messages d'erreur)
cite les noms canoniques et personne ne peut la réécrire d'un coup. Un agent qui suit
une procédure de 2026-07 doit continuer à aboutir.

Trois choses qui coûteraient cher si on les oubliait :

- **Rien n'est renommé par défaut.** Le préfixe est DÉCLARÉ par le tenant
  (`tenants.tool_prefix`, NULL = inerte), jamais dérivé du slug. Renommer les outils
  d'un tenant est une rupture pour ses procédures et sa prose : ça se décide, ça ne
  s'attrape pas en existant. Même règle que `link_paths` — pas de patron, pas de lien.
- **Un préfixe ne peut pas être un namespace de connecteur** (`normalize_prefix`).
  Sinon `acme_search` désignerait à la fois l'alias d'`oto_search` et un vrai outil
  du connecteur `acme` : la traduction retour deviendrait ambiguë, et l'ambiguïté
  ici décide QUEL outil s'exécute. Le refus est loggé, jamais silencieux — un tenant
  dont le préfixe est refusé garde les noms canoniques, ce qui est visible.
- **La traduction est un PRÉFIXE, pas une table.** `oto_<reste>` ⇄ `<prefix>_<reste>`,
  total et réversible. Une table nom-à-nom aurait dérivé au premier outil ajouté.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Le namespace des outils de la plateforme — le seul renommé. `data_*` (substrat de
# stockage), `run_*`/`feedback` (boucle d'usage) et les connecteurs portent des noms
# de CAPACITÉ ou de FOURNISSEUR, pas notre marque : les renommer les rendrait
# méconnaissables sans rien corriger.
PRIMARY_PREFIX = "oto"

# Un préfixe entre dans un nom d'outil MCP : minuscules et chiffres, rien d'autre.
# Pas de `_` (il porte le découpage en namespace, cf. `tool_visibility.namespace_of`),
# pas de `-` (un slug de tenant en accepte un, un nom d'outil non).
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{1,23}$")

# Les namespaces SPINE de la plateforme : un préfixe qui vaudrait l'un d'eux ferait
# collisionner l'alias avec un outil réel (`data_write`, `run_start`, `feedback`).
_SPINE_PREFIXES = frozenset({PRIMARY_PREFIX, "data", "run", "feedback"})

# Un nom d'outil de la plateforme, tel qu'il apparaît DANS la prose injectée au
# handshake (`oto_doc`, `oto_connector`…). Le token `oto_<identifiant>` est réservé
# par construction — c'est un espace de noms que la plateforme s'est donné — donc le
# rencontrer dans l'artefact de session, c'est rencontrer un outil. Garde-fou :
# `tests/test_tool_alias_par_tenant.py` vérifie sur le MONTAGE RÉEL que chaque token
# de cette forme présent dans la prose servie EST un outil du registre.
_PROSE_RE = re.compile(r"\b" + PRIMARY_PREFIX + r"_([a-z][a-z0-9_]*)\b")


def normalize_prefix(value) -> str:
    """Le préfixe utilisable déclaré par un tenant, ou `""` (= aucun renommage).

    Refuse — en le LOGGANT — une forme qui ne peut pas être un nom d'outil, et un
    préfixe qui collisionnerait avec un namespace existant. Un refus rend `""`, donc
    les noms canoniques : la dégradation est le comportement d'avant ce lot.

    ⚠️ **Rien n'est réparé au passage** — ni espaces rognés, ni majuscules abaissées.
    Même règle que le slug d'un tenant (`tenancy.build`) : cette valeur entre dans un
    identifiant, donc elle vaut EXACTEMENT ce qui est déclaré. `Acme` corrigé en
    silence ferait dire `Acme` à l'écran de suivi et `acme_doc` aux outils, et
    personne n'irait chercher l'écart là.
    """
    prefix = str(value or "")
    if not prefix:
        return ""
    if not _PREFIX_RE.match(prefix):
        logger.warning(
            "préfixe d'outils %r refusé : un préfixe est [a-z][a-z0-9]{1,23} — ni `_` "
            "(il découpe le namespace), ni `-` (un slug en accepte, un nom d'outil "
            "non), ni une majuscule ou un espace, qu'on ne corrige pas en silence",
            prefix)
        return ""
    if prefix in _SPINE_PREFIXES or _is_connector_namespace(prefix):
        logger.warning(
            "préfixe d'outils %r refusé : ce namespace porte déjà des outils — "
            "`%s_…` désignerait à la fois un alias et un outil réel, et la traduction "
            "retour choisirait au hasard lequel exécuter", prefix, prefix)
        return ""
    return prefix


def _is_connector_namespace(prefix: str) -> bool:
    """Le préfixe est-il le namespace d'un connecteur déclaré ? Import LOCAL : ce
    module est importé par le middleware, `providers` tire tout le registre."""
    try:
        from . import providers
        return providers.connector_for_namespace(prefix) is not None
    except Exception:  # noqa: BLE001 — un registre illisible ne doit pas ouvrir la porte
        logger.warning("registre des connecteurs illisible : préfixe %r refusé par "
                       "précaution", prefix, exc_info=True)
        return True


def _tenant_entry(sub: Optional[str]):
    """L'entrée de registre du tenant TIERS de ce compte, ou None (compte de la
    plateforme, sub absent, registre illisible).

    **Aucun accès DB** — le registre d'émetteurs est en mémoire (bâti au boot). C'est
    ce qui autorise l'appel depuis un middleware, dans la boucle (serveur MONO-LOOP).
    """
    if not sub:
        return None
    try:
        from . import tenancy
        registre = tenancy.current()
        slug = registre.tenant_of(sub)
        if not slug or slug == tenancy.PRIMARY_SLUG:
            return None
        return next((e for e in registre.entries() if e.slug == slug), None)
    except Exception:  # noqa: BLE001 — un nom d'outil ne casse jamais un appel
        logger.warning("résolution du tenant impossible (fail-open)", exc_info=True)
        return None


def _sert_un_worker() -> bool:
    """La requête MCP en cours porte-t-elle un jeton de DÉLÉGATION — un travail du runner ?

    Le renommage est un AFFICHAGE : il sert le produit d'une personne, dans son client.
    Un travail du runner n'est personne. C'est notre worker, qui agit au nom du
    demandeur (`runner_jobs._delegue`) avec un jeton `kind='delegation'`, et tout ce
    qu'il confronte est écrit en canonique : l'allowlist du travail (`payload.tools`,
    EXACTE et fail-closed côté worker), l'outil de lecture de la procédure
    (`oto_procedure`), les `<tool:…>` dont l'allowlist d'un déclencheur se déduit. Lui
    servir `acme_doc` vidait donc son allowlist : `tools/list` ne portait plus aucun
    nom qu'elle cite, et l'agent tournait sans un seul outil — sans erreur, puisque le
    worker filtre en silence.

    Lu sur le jeton de la requête (claim `token_kind`, posé par
    `server._verify_api_token`) : aucun accès DB, appelable depuis la boucle. Hors
    requête MCP (REST, boot, tests) ⟹ False, le comportement d'avant.
    """
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
    # noqa: SILENT — hors contexte de requête MCP : pas de jeton, donc pas de worker
    except Exception:  # noqa: BLE001
        return False
    claims = getattr(token, "claims", None) or {}
    return claims.get("token_kind") == "delegation"


def declared_prefix_for(sub: Optional[str]) -> str:
    """Le préfixe DÉCLARÉ par le tenant de ce compte, ou `""` — quel que soit le client
    qui appelle. C'est celui des ÉCRITURES (`canonical_prose`, `canonical_names`) : un
    texte qui cite `acme_doc` revient au canonique, qui que soit l'auteur."""
    entry = _tenant_entry(sub)
    return normalize_prefix(getattr(entry, "tool_prefix", "")) if entry else ""


def prefix_for(sub: Optional[str]) -> str:
    """Le préfixe d'outils SERVI à ce compte, ou `""`.

    `""` aussi pour un travail du runner (`_sert_un_worker`), même sous un compte de
    tenant : ses noms sont les canoniques. Le jeton n'est lu que si un préfixe est
    déclaré — un compte de la plateforme ne paie rien de plus."""
    prefix = declared_prefix_for(sub)
    if prefix and _sert_un_worker():
        return ""
    return prefix


def server_identity_for(sub: Optional[str]) -> tuple[str, str]:
    """`(name, title)` que le handshake `initialize` doit annoncer — `("", "")` =
    inchangé (compte de la plateforme, tenant sans déclaration, erreur).

    Même défaut, dernier recoin : `serverInfo.name` valait `oto` dans le produit
    d'un partenaire, alors que les outils s'y appellent `<prefix>_…`. Même parti pris
    que le préfixe et les liens : **rien n'est renommé par défaut** — `name` suit le
    `tool_prefix` déclaré (l'identifiant, cohérent avec les noms d'outils), `title`
    suit le `name` du tenant (le libellé humain, celui du PRM). Un tenant qui n'a
    rien déclaré garde l'annonce d'avant, et ça se voit — pas de patron, pas de nom.
    """
    entry = _tenant_entry(sub)
    # Un travail du runner n'a pas de produit à afficher : il garde l'annonce d'avant,
    # comme ses noms d'outils (`prefix_for`).
    if entry is None or _sert_un_worker():
        return ("", "")
    return (normalize_prefix(getattr(entry, "tool_prefix", "")),
            str(getattr(entry, "name", "") or ""))


def public(name: str, prefix: str) -> str:
    """Le nom MONTRÉ : `oto_doc` → `acme_doc`. Tout autre nom passe inchangé."""
    if not prefix or not name or not name.startswith(PRIMARY_PREFIX + "_"):
        return name
    return prefix + name[len(PRIMARY_PREFIX):]


def canonical(name: str, prefix: str) -> str:
    """Le nom CONNU DU SERVEUR : `acme_doc` → `oto_doc`.

    Le nom canonique passe inchangé — les deux formes sont acceptées à l'appel (cf.
    l'en-tête du module : la prose déjà écrite cite les canoniques).
    """
    if not prefix or not name or not name.startswith(prefix + "_"):
        return name
    return PRIMARY_PREFIX + name[len(prefix):]


def canonical_names(names, sub: Optional[str]):
    """Une liste de NOMS d'outils déclarée par un appelant (`tools` d'une flotte, d'un
    déclencheur, d'un travail), ramenée au canonique AVANT d'être stockée.

    Un agent dans le client du produit ne voit que `acme_*` : c'est ce qu'il recopie
    dans `tools=[…]`. Stockée telle quelle, l'allowlist ne désignait plus rien pour le
    worker, servi en canonique (`prefix_for`) — l'agent déclaré ne recevait aucun
    outil. Le préfixe est celui du tenant DÉCLARANT ; tout autre nom passe inchangé (un
    connecteur ne peut pas porter ce namespace, cf. `normalize_prefix`). Ce qui n'est
    pas une liste passe aussi : la valider reste le travail de l'appelant.
    """
    prefix = declared_prefix_for(sub)
    if not prefix or not isinstance(names, list):
        return names
    return [canonical(n, prefix) if isinstance(n, str) else n for n in names]


def canonical_prose(text: str, sub: Optional[str]) -> str:
    """Les noms d'outils cités dans un texte ÉCRIT, ramenés au canonique — l'inverse de
    `rewrite_prose`, pour ce qu'on STOCKE (procédure, guide, consigne d'un agent).

    Ce qui se lit est traduit au nom du produit ; ce qui s'écrit doit donc revenir.
    Sans ça, un agent qui lit un guide servi en `acme_doc` et le réenregistre écrit
    `acme_doc` en base — et tout ce qui lit la base en canonique décroche : l'allowlist
    DÉDUITE d'une procédure (`<tool:acme_doc>` ne résout rien), le worker qui l'exécute,
    les puces d'outils du dashboard, et le retour arrière (préfixe retiré, le texte cite
    un outil qui n'existe pas).

    ⚠️ Plus étroit que l'aller, et c'est voulu : `oto_<x>` est un espace de noms RÉSERVÉ,
    `acme_<x>` non — un tableau `acme_leads`, un slug, un mot du client peuvent s'écrire
    ainsi. Seul un token dont le canonique EST un outil du registre boot, ou en préfixe un
    (`acme_use_*`, `acme_admin`), est ramené — exactement les tokens que l'aller traduit
    (`test_tout_token_oto_de_la_prose_servie_est_bien_un_outil`), donc relire puis
    réenregistrer rend le texte d'origine. Registre non réchauffé ⟹ rien n'est touché.

    Fail-open : une écriture ne devient jamais une erreur de traduction.
    """
    prefix = declared_prefix_for(sub)
    if not prefix or not text or (prefix + "_") not in text:
        return text
    try:
        from . import tool_registry
        noms = tool_registry.boot_tool_names()
        if not noms:
            logger.warning("registre d'outils non réchauffé : noms `%s_…` écrits tels "
                           "quels", prefix)
            return text
        connus = set(noms)

        def _canonique(m):
            nom = f"{PRIMARY_PREFIX}_{m.group(1)}"
            if nom in connus or any(n.startswith(nom) for n in noms):
                return nom
            return m.group(0)

        return re.sub(r"\b" + re.escape(prefix) + r"_([a-z][a-z0-9_]*)\b", _canonique, text)
    except Exception:  # noqa: BLE001 — une écriture ne casse pas sur un nom d'outil
        logger.warning("canonicalisation des noms d'outils échouée (fail-open)",
                       exc_info=True)
        return text


def public_namespace(namespace: str, prefix: str) -> str:
    """Le namespace MONTRÉ : `oto` → `acme`. Tout autre namespace passe inchangé.

    Un nom d'outil ne voyage jamais seul — `oto_tool_schema` en rend aussi le
    namespace, que l'agent relit pour se repérer. Le laisser en canonique ferait
    répondre « namespace: oto » à un compte dont tous les outils s'appellent
    `acme_…`, soit le nom interne réintroduit par la porte de derrière.
    """
    return prefix if (prefix and namespace == PRIMARY_PREFIX) else namespace


def rewrite_prose(text: str, prefix: str) -> str:
    """Les noms d'outils cités dans un TEXTE servi à l'agent, au nom du produit.

    Sans ça le renommage se retourne contre lui-même : l'artefact de session prescrit
    `oto_doc`, l'agent l'appelle (le serveur l'accepte), et le client réaffiche
    `Oto doc` — le nom qu'on voulait faire disparaître, à l'endroit exact où il se
    voit. Traduire la liste sans traduire la consigne ne corrige donc rien.
    """
    if not prefix or not text or (PRIMARY_PREFIX + "_") not in text:
        # Sortie sèche avant toute regex : appelée sur les ~480 descriptions du
        # `tools/list` (200 Ko), dont 27 seulement citent un outil. Le `in` coûte un
        # scan mémoire, la substitution un automate — et le serveur est MONO-LOOP.
        return text
    return _PROSE_RE.sub(lambda m: f"{prefix}_{m.group(1)}", text)
