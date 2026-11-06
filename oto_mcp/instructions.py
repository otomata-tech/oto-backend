"""Instructions serveur MCP (champ FastMCP `instructions=`) — le **contexte oto**
injecté à Claude au handshake `initialize`. ⚠️ Canal à livraison NON GARANTIE
(mesuré 2026-08, #478 : Claude Code tronque à 2 048 caractères, claude.ai n'injecte
rien) : le bloc A est depuis le 2026-09-01 un SOCLE-RÉSUMÉ ≤ 2 000 caractères,
autoportant, qui pointe la version intégrale — le guide plateforme `notice`
(`guides/notice.md`) — et `oto_context` (le bloc C à la demande). Budget gardé par
`tests/test_instructions_budget.py`.

Refonte #50 (amende ADR 0014/0017) — l'artefact injecté est **composé de 2 blocs** :

- **Bloc A — secret sauce plateforme** : posture + résumé des règles d'usage +
  **catalogue de namespaces** (dérivé du registre) ; le détail des règles vit dans
  le guide `notice` (`guides/notice.md`). Prose stockée en DB
  (`platform_instructions['secret_sauce']`), éditable seulement par l'admin plateforme,
  **inviolable par l'org**, toujours injectée ; le catalogue est appendé à la composition.
  La constante `_SECRET_SAUCE` = le défaut seedé au boot + le fallback (aucun accès DB à
  l'import).
- **Bloc C — contexte dynamique** par-(sub, org) : section de contexte résolu (org /
  équipe / connecteurs actifs / N derniers projets / derniers déroulés / fiche
  « situation avec oto » de l'user) + les **agent README cumulés** du général au
  spécifique — org (`org_instructions` slug `claude_md`) → équipe active
  (`org_group_instructions` slug `claude_md`) → user (`user_agent_readme`) — chacun
  avec substitution des variables `{{org}}` / `{{user}}` / `{{équipe}}` /
  `{{connecteurs_actifs}}`. (Le niveau plateforme du concept = le bloc A.)

L'onboarding n'est PAS un bloc : c'est un projet « Découverte » (ADR 0032 §7) semé à la
création de l'org perso, qui remonte via la ligne « Projets récents » du bloc C.

`render()` (STATIQUE, sans DB) = bloc A seed + catalogue → défaut de boot et fallback.
`compose_session(sub, org_id)` (RUNTIME) = l'artefact réel par session.
Tout est **fail-open** : toute erreur retombe sur la surface statique."""
from __future__ import annotations

import logging

from . import outils_retires, ownership, providers, run_status, tool_alias

logger = logging.getLogger(__name__)

# Clé du bloc plateforme en DB (table `platform_instructions`).
KEY_SECRET_SAUCE = "secret_sauce"

# --- Bloc A — secret sauce plateforme (défaut seedé + fallback) -------------
_SECRET_SAUCE = """\
Oto — TA boîte à outils d'automatisation (prospection B2B, données entreprise FR, CRM, email, messagerie, documents…). Ces outils `*_` AGISSENT sur les comptes et données de l'utilisateur : utilise-les DIRECTEMENT, sans qu'on te dise « avec oto » ni recherche web sur oto.

⚠️ Ce texte est souvent TRONQUÉ ou non livré par le client. Mode d'emploi complet = guide `notice` (`oto_guide op=read slug=notice`) — lis-le d'abord, puis `oto_context` (contexte de ton org). L'essentiel :

- **Procédure d'abord** : `oto_procedure op=list` — une procédure existante fait autorité.
- **Encadre et remonte** : `run_start(label, guide?)` → `_run_id=` sur CHAQUE appel → `run_finish(run_id, done|partial|failed|blocked)`. Échec d'outil ou capacité manquante → `feedback(signal='gap'|'tool_feedback')`, immédiatement, de toi-même.
- **Travaille dans un projet** : agir POUR un projet = `_project=<id>` sur chaque appel ; tâche ad-hoc = crée un projet, jamais hors-sol.
- **Aucun état de session** : tout contexte se passe DANS l'appel : `_project`/`_org`/`_group`/`_account`/`_instance` (optionnels — omis = défaut) et `_run_id`, OBLIGATOIRE dès qu'un run est ouvert ou une ligne réservée (sinon écriture REFUSÉE).
- **Ce qui dure** : règles de l'org → son guide (`oto_guide`, rendu par `oto_context`) ; la personne → sa fiche (`oto_profile`) et sa note (`oto_guide scope=user`) ; faits d'un travail → un doc de son projet (`oto_doc`). Cherche là avant le web.
- **Registre FIGÉ** : un outil non listé s'appelle via `oto_call(name, arguments)` — n'en déduis jamais qu'une capacité n'existe pas.
- **Toolbox = l'installé** (par toi, le kit de ton org, ou ce que tes clés et l'open data servent) ; le reste s'INSTALLE (`oto_connector op=list/select`, catalogue) ; open data/free tier = zéro config ; ne simule jamais.
- **Slots** : la procédure référence `<slot:nom>`, le projet binde ; non résolu = matérialise le contexte, jamais une table « probable ».
"""

# En-tête du catalogue de namespaces (dérivé du registre), appendé au bloc A.
_CATALOG_HEADER = (
    "Namespaces — le catalogue COMPLET des capacités de la plateforme. Ta toolbox n'en "
    "porte que l'installé (par toi, le kit de ton org, ou ce que tes clés et l'open data "
    "servent) : les autres s'installent via "
    "`oto_connector(op='select', name=…)` (durable) ou s'appellent ponctuellement via "
    "`oto_call` :"
)

# En-têtes des agent README cumulés (bloc C), du général au spécifique.
_README_ORG_HEADER = "## Your organization's README"
_README_GROUP_HEADER = "## Your team's README"
_README_USER_HEADER = "## Your user's README"
_CONTEXT_HEADER = "## Your oto context"

# Tokens de variable substitués dans les agent README (bloc C). Auto-contexte v1.
_VAR_TOKENS = ("{{org}}", "{{user}}", "{{équipe}}", "{{equipe}}", "{{connecteurs_actifs}}",
               "{{rôle}}", "{{role}}", "{{date}}", "{{projets_récents}}", "{{projets_recents}}")


# --- Lecture des blocs plateforme (DB override → seed) ----------------------

def _platform_block(key: str, seed: str) -> str:
    """Le bloc plateforme `key` : override DB s'il existe et non vide, sinon `seed`
    (constante). Fail-open au seed. La lecture DB est centralisée dans `guide_store`
    (ADR 0042 : source unique de la prose init ; le seed reste ici, son domicile)."""
    from . import guide_store
    return guide_store.init_guide_body("platform", key) or seed.strip()


def _catalog() -> str:
    """L'en-tête + le catalogue de namespaces dérivé du registre (toujours injecté)."""
    return f"{_CATALOG_HEADER}\n{providers.render_namespace_catalog()}"


def _block_a() -> str:
    return f"{_platform_block(KEY_SECRET_SAUCE, _SECRET_SAUCE)}\n\n{_catalog()}"


# --- Bloc C — contexte dynamique par-(sub, org) -----------------------------

def _resolve_context(sub: str | None, org_id: int) -> dict:
    """Résout l'auto-contexte d'un (sub, org) — réutilisé par la section de contexte
    ET la substitution de variables. Chaque champ best-effort (jamais bloquant)."""
    from . import access, db, org_store, roles

    org_name = _org_label(org_store.get_org(org_id) or {}, org_id)

    user_name = ""
    if sub:
        try:
            u = db.get_user(sub) or {}
            user_name = (u.get("name") or u.get("email") or "").strip()
        # noqa: SILENT — dette déclarée : bloc de contexte de handshake amputé en silence (#424, verdict C)
        except Exception:
            pass

    role = ""
    if sub:
        try:
            role = roles.effective_org_role(sub, org_id) or ""
        # noqa: SILENT — dette déclarée : bloc de contexte de handshake amputé en silence (#424, verdict C)
        except Exception:
            pass

    group_name = ""
    group_id: int | None = None
    try:
        from . import group_store
        gid = access.current_group(sub) if sub else None
        if gid is not None:
            group_id = gid
            group_name = ((group_store.get_group(gid) or {}).get("name") or "").strip()
    # noqa: SILENT — dette déclarée : bloc de contexte de handshake amputé en silence (#424, verdict C)
    except Exception:
        pass

    connectors: list[str] = []
    if sub:
        try:
            providers_status = (access.status_for(sub).get("providers") or {})
            connectors = sorted(
                name for name, st in providers_status.items()
                if st.get("mode") in ("user", "group", "org", "tenant", "platform")
            )
        # noqa: SILENT — dette déclarée : l'agent croit n'avoir aucune clé (#424, verdict C — le pire)
        except Exception:
            pass

    from .connectors import http_operations     # les connecteurs PERSONNALISÉS de l'org
    custom = http_operations.for_context(org_id, connectors)

    projects: list[str] = []
    try:
        # L'org et mes projets PERSONNELS créés dans cette org (décision du 29/09/2026,
        # `ownership.mes_objets_ici`) ; ce qui m'est partagé en personne, dans l'org où
        # c'est rangé si j'en suis membre, sinon dans ma maison (06/11/2026).
        rows = db.list_projects_for_owners([("org", str(org_id))],
                                           createur=ownership.mes_objets_ici(sub, org_id))
        # + les projets LIVRÉS à cette org (partagés via resource_grants, #52) — c'est
        # l'exposition au handshake : le client ouvre le projet livré en un message.
        seen = {r.get("id") for r in rows}
        # Partagés à l'org, et à moi en personne rangés ici (06/11/2026).
        recus = db.list_projects_granted_to([("org", str(org_id))]) + (
            ownership.projets_partages_a_moi_ici(sub, org_id) if sub else [])
        # Vue bornée (oto#270) : un partage personnel reçu n'est pas de l'org consultée.
        rows += [r for r in ownership.borner_a_la_vue(
                     sub, "project", recus,
                     rid=lambda r: r["id"])
                 if r.get("id") not in seen]
        projects = [r.get("name") or f"#{r.get('id')}" for r in rows[:5]]
    # noqa: SILENT — dette déclarée : bloc de contexte de handshake amputé en silence (#424, verdict C)
    except Exception:
        pass

    runs: list[dict] = []
    if sub:
        try:
            runs = db.recent_runs(sub, org_id, limit=5)
        # noqa: SILENT — dette déclarée : bloc de contexte de handshake amputé en silence (#424, verdict C)
        except Exception:
            pass

    # Fiche « situation avec oto » (ce que l'agent sait de l'utilisateur, entretenu via
    # `oto_profile`) — réinjectée pour personnaliser l'aide. Best-effort.
    profile: dict = {}
    if sub:
        try:
            profile = db.get_account_profile(sub).get("profile") or {}
        # noqa: SILENT — dette déclarée : bloc de contexte de handshake amputé en silence (#424, verdict C)
        except Exception:
            pass

    return {
        "org_name": org_name, "user_name": user_name, "role": role,
        "group_name": group_name, "group_id": group_id, "connectors": connectors,
        "custom_connectors": custom, "projects": projects, "runs": runs, "profile": profile,
    }


def _org_label(org: dict, org_id: int) -> str:
    """Le nom d'org tel que le contexte le rend (`{{org}}`, en-tête du readme d'org)."""
    return org.get("name") or f"#{org_id}"


def _apply_vars(body: str, ctx: dict, *, personnel: bool = True) -> str:
    """Substitue les variables d'auto-contexte dans le guide d'org. Les tokens
    inconnus sont laissés tels quels (intention de l'auteur).

    `personnel=False` (aperçu d'un MEMBRE TYPE, #1194) : seules les variables communes
    à tous les membres sont résolues (`{{org}}`, `{{équipe}}`, `{{date}}`) ; celles
    d'une personne (`{{user}}`, `{{rôle}}`, `{{connecteurs_actifs}}` — ses clés —,
    `{{projets_récents}}` — ses projets perso) restent telles quelles : chaque membre
    reçoit les siennes, et aucune valeur ne vaudrait pour tous."""
    from datetime import date
    repl = {
        "{{org}}": ctx["org_name"],
        "{{équipe}}": ctx["group_name"] or "—",
        "{{equipe}}": ctx["group_name"] or "—",
        "{{date}}": date.today().isoformat(),         # date du jour (session)
    }
    if personnel:
        projets = " · ".join(ctx.get("projects") or []) or "—"
        role = ctx.get("role") or "—"
        repl.update({
            "{{user}}": ctx["user_name"] or "—",
            "{{connecteurs_actifs}}": ", ".join(ctx["connectors"]) or "—",
            "{{rôle}}": role, "{{role}}": role,      # rôle de l'user dans l'org (#6 C)
            "{{projets_récents}}": projets, "{{projets_recents}}": projets,
        })
    for token, value in repl.items():
        if token in body:
            body = body.replace(token, value)
    return body


def _format_context(ctx: dict) -> str:
    """La section « ## Your oto context » — auto-contexte + anticipation (projets,
    déroulés). Lignes optionnelles : seules celles avec de la donnée sont rendues."""
    lines = [_CONTEXT_HEADER, ""]
    role = f" (your role: {ctx['role']})" if ctx["role"] else ""
    lines.append(f"- Organization: {ctx['org_name']}{role}")
    if ctx["group_name"]:
        lines.append(f"- Active team: {ctx['group_name']}")
    if ctx["connectors"]:
        lines.append(f"- Active connectors: {', '.join(ctx['connectors'])}")
    from .connectors.http_operations import context_line
    lines += [context_line(c) for c in ctx.get("custom_connectors") or []]
    if ctx["projects"]:
        lines.append(f"- Recent projects: {' · '.join(ctx['projects'])}")
    if ctx["runs"]:
        bits = []
        for r in ctx["runs"]:
            label = r.get("label") or r.get("run_id") or "?"
            # `guide` : `recent_runs` renomme la colonne d'hier dans sa lecture (#519,
            # la colonne elle-même ne se renomme qu'au lot D, #526).
            doc = f" [{r['guide']}]" if r.get("guide") else ""
            # Un run muet ne s'annonce plus « en cours » : l'agent lisait ça au
            # handshake et croyait reprendre un travail que personne ne menait plus.
            bits.append(f"{label}{doc} {run_status.describe(r)}")
        lines.append(f"- Recent runs: {' · '.join(bits)}")
    annonce = outils_retires.annonce_act_du_jour()
    if annonce:
        lines.append(f"- {annonce}")
    return "\n".join(lines)


# Libellés lisibles des champs connus de la fiche (cf. capabilities/profile.PROFILE_FIELDS) ;
# une clé libre inconnue est rendue telle quelle.
_PROFILE_LABELS = {
    "full_name": "Name", "role": "Role", "company": "Company / industry",
    "goals": "Goals", "crm": "CRM", "connectors_wanted": "Wanted connectors",
    "tone": "Tone / preferences",
}


def _format_profile(profile: dict) -> str:
    """La fiche « situation avec oto » de l'user (champs remplis seulement). '' si vide.
    Entretenue par l'agent via `oto_profile` ; sert à personnaliser l'aide."""
    rows = []
    for key, value in profile.items():
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        rows.append(f"- {_PROFILE_LABELS.get(key, key)}: {text}")
    if not rows:
        return ""
    return "### What you know about the user\n" + "\n".join(rows)


def _c_layers(sub: str | None, org_id: int | None) -> list[dict]:
    """Les couches du bloc C, ORDONNÉES — `[{key, label, body}]`, couches vides omises.
    `[]` si pas d'org. Fail-open : README d'org seul sans contexte si la résolution
    échoue. Source unique : `_block_c` (artefact injecté) et la vue de transparence
    `/api/me/agent-context` (pile de couches) en dérivent — derive don't duplicate."""
    if org_id is None:
        return []
    try:
        ctx = _resolve_context(sub, org_id)
    except Exception:
        logger.warning("résolution du contexte org=%s échouée (fail-open readme)",
                       org_id, exc_info=True)
        body = _org_readme_only(org_id)
        return [{"key": "org", "label": "readme de ton org", "body": body}] if body else []

    # Readmes « init » cumulés du général au spécifique (org → équipe → user) : le
    # MÊME primitif de guide, rendu uniformément par scope (ADR 0042). Chaque scope =
    # (owner, en-tête) ; corps lu via `guide_store.init_guide_body`, variables
    # substituées. Ordre = cumul de guide ; un scope vide est omis.
    layers = [{"key": "context", "label": "ton contexte oto", "body": _format_context(ctx)}]
    profile_md = _format_profile(ctx.get("profile") or {})
    if profile_md:
        layers.append({"key": "profile", "label": "ta fiche", "body": profile_md})
    for key, label, part in (
        ("org", "readme de ton org",
         _render_init_readme("org", org_id, _org_readme_header(ctx), ctx)),
        ("group", "readme de ton équipe",
         _render_init_readme("group", ctx.get("group_id"), _group_readme_header(ctx), ctx)),
        ("user", "ta note",
         _render_init_readme("user", sub, _README_USER_HEADER, ctx)),
    ):
        if part:
            layers.append({"key": key, "label": label, "body": part})
    return layers


def _block_c(sub: str | None, org_id: int | None) -> str:
    """Le bloc contexte dynamique : section de contexte résolu + fiche profil + agent
    README cumulés (org → équipe → user, variables substituées). '' si pas d'org."""
    return "\n\n".join(l["body"] for l in _c_layers(sub, org_id))


def _org_readme_header(ctx: dict) -> str:
    return f"{_README_ORG_HEADER} ({ctx['org_name']})"


def _group_readme_header(ctx: dict) -> str:
    """En-tête du readme d'équipe — suffixé du nom d'équipe s'il est connu."""
    name = f" ({ctx['group_name']})" if ctx.get("group_name") else ""
    return f"{_README_GROUP_HEADER}{name}"


def _render_init_readme(scope: str, owner_id, header: str, ctx: dict) -> str:
    """Un readme « init » d'un scope (org/group/user) : corps `guide_store.init_guide_body`
    variables substituées, sous `header`. '' si pas d'owner (owner_id falsy) ou corps
    vide — un scope absent est simplement omis du cumul. Source unique des ex-
    `_format_{org,group,user}_readme` (ADR 0042 : guide = primitif uniforme par scope)."""
    if not owner_id:
        return ""
    from . import guide_store
    return _render_readme(guide_store.init_guide_body(scope, owner_id), header, ctx)


def _render_readme(body: str | None, header: str, ctx: dict, *, personnel: bool = True) -> str:
    """Le rendu d'un readme « init » LU : `header` + corps, variables substituées ; ''
    si corps vide. Partagé par la session (`_render_init_readme`, lecture fail-open)
    et l'aperçu d'un membre type (`member_preview_layers`, lecture stricte)."""
    if not body:
        return ""
    return f"{header}\n\n{_apply_vars(body, ctx, personnel=personnel)}"


def _org_readme_only(org_id: int) -> str:
    """Fallback : le README d'org seul (sans section de contexte, sans variables), si
    la résolution du contexte a échoué mais qu'on peut encore le lire."""
    from . import guide_store
    body = guide_store.init_guide_body("org", org_id)
    if not body:
        return ""
    try:
        from . import org_store
        name = (org_store.get_org(org_id) or {}).get("name") or f"#{org_id}"
    # noqa: SILENT — nom d'org non lisible ⇒ « #id », le README reste servi
    except Exception:
        name = f"#{org_id}"
    return f"{_README_ORG_HEADER} ({name})\n\n{body}"


# --- Composition ------------------------------------------------------------

def render() -> str:
    """Surface STATIQUE (constantes seules, aucun accès DB) : bloc A (secret sauce +
    catalogue dérivé). Défaut de boot `FastMCP(instructions=…)` et fallback ultime."""
    return f"{_SECRET_SAUCE.strip()}\n\n{_catalog()}"


def _socle_for(sub: str | None) -> tuple[str, str]:
    """Le socle injecté à ce compte, et son étiquette — `(corps, label)`.

    Un compte d'un tenant tiers reçoit LE SOCLE DE SON TENANT s'il en existe un. Sans
    ce cran, l'assistant d'un partenaire se présente sous notre marque et renvoie vers
    nos adresses : constaté chez un client le 13/08 (« Sur <tenant> (Oto), tu es… » +
    un lien vers notre tableau de bord). Ce n'est pas un défaut de formulation — le
    texte est au niveau plateforme alors qu'il décrit un produit.

    Fail-open à trois détentes, parce que ce chemin est celui du handshake : pas de
    tenant, pas de ligne, ou lecture en erreur ⟹ le socle plateforme, à l'octet près.
    """
    if sub:
        try:
            from . import tenancy
            guide, label = _choisir_socle(tenancy.current().tenant_of(sub),
                                          _lire_init_fail_open)
            return guide["body_md"], label
        except Exception:  # noqa: BLE001 — le handshake ne casse jamais là-dessus
            logger.warning("socle de tenant illisible pour %s (fail-open)", sub,
                           exc_info=True)
    guide, label = _choisir_socle(None, _lire_init_fail_open)
    return guide["body_md"], label


def _choisir_socle(slug: str | None, lire) -> tuple[dict, str]:
    """LE choix du socle servi aux comptes du tenant `slug` — `(guide, label)`, guide =
    `{body_md, updated_at}` : celui du tenant TIERS s'il en a écrit un, sinon celui de
    la plateforme (base, sinon le seed `_SECRET_SAUCE`, `updated_at` None).

    `lire(scope, owner) -> {body_md, updated_at}` porte la politique de lecture : le
    handshake lit fail-open (`_lire_init_fail_open`), l'aperçu d'un membre type
    (`member_preview_layers`) lit strict et date chaque couche."""
    from . import tenancy
    if slug and slug != tenancy.primary_slug():
        guide = lire("tenant", slug)
        if guide["body_md"]:
            nom = next((e.name for e in tenancy.current().entries()
                        if e.slug == slug and e.name), slug)
            return guide, f"socle {nom}"
    guide = lire("platform", KEY_SECRET_SAUCE)
    if not guide["body_md"]:
        guide = {"body_md": _SECRET_SAUCE.strip(), "updated_at": None}
    return guide, "socle oto"


def _lire_init_fail_open(scope: str, owner) -> dict:
    """Lecture du handshake : `guide_store.init_guide_body` (fail-open), sans date."""
    from . import guide_store
    return {"body_md": guide_store.init_guide_body(scope, owner) or "", "updated_at": None}


def _lire_init_strict(scope: str, owner) -> dict:
    """Lecture de l'aperçu : `guide_store.get_init_guide` — une base illisible LÈVE."""
    from . import guide_store
    guide = guide_store.get_init_guide(scope, owner)
    return {"body_md": (guide["body_md"] or "").strip(), "updated_at": guide["updated_at"]}


def member_preview_layers(org: dict, group: dict | None) -> list[dict]:
    """Les couches d'instructions qu'un MEMBRE TYPE de l'org `org` (dans l'équipe
    `group`, si donnée) reçoit, dans l'ORDRE d'injection — `[{scope, body_md,
    updated_at}]`, `scope` ∈ platform | org | group, couches vides omises (#1194).

    PAS une seconde composition : mêmes primitives que `session_layers` — choix du
    socle (`_choisir_socle`, le tenant étant celui qui héberge l'org), en-têtes,
    variables (`_render_readme`), noms d'outils du produit (`tool_alias`). Le sub en
    moins, ce qui ne vaut que pour UNE personne est exclu : la section « Ton contexte
    oto » (rôle, clés, projets, déroulés), la fiche, la note perso, et les variables
    personnelles, laissées telles quelles (`_apply_vars(personnel=False)`). Le
    catalogue des namespaces (dérivé du registre, identique pour tous) n'est pas une
    couche d'instructions : il n'est pas rendu ici.

    Lecture STRICTE : une base illisible lève — un aperçu muet passerait pour une
    org sans instructions."""
    from .connectors import activation as connector_activation
    org_id = int(org["id"])
    ctx = {"org_name": _org_label(org, org_id),
           "group_name": ((group or {}).get("name") or "").strip()}
    slug = connector_activation.tenant_of_org(org_id)
    socle, _label = _choisir_socle(slug, _lire_init_strict)
    couches = [{"scope": "platform", **socle}]
    readmes = [("org", org_id, _org_readme_header(ctx))]
    if group is not None:
        readmes.append(("group", int(group["id"]), _group_readme_header(ctx)))
    for scope, owner, header in readmes:
        guide = _lire_init_strict(scope, owner)
        body = _render_readme(guide["body_md"], header, ctx, personnel=False)
        if body:
            couches.append({"scope": scope, "body_md": body,
                            "updated_at": guide["updated_at"]})
    prefix = tool_alias.declared_prefix_of_tenant(slug)
    if not prefix:
        return couches
    return [{**c, "body_md": tool_alias.rewrite_prose(c["body_md"], prefix)} for c in couches]


def session_layers(sub: str | None, org_id: int | None) -> list[dict]:
    """L'artefact injecté DÉCOMPOSÉ en couches ordonnées `[{key, label, body}]` :
    bloc A (socle plateforme + catalogue dérivé) puis couches du bloc C. Invariant :
    `"\\n\\n".join(bodies) == compose_session(sub, org_id)` — sert la vue de
    transparence (`/api/me/agent-context`) sans dupliquer la composition.

    Les noms d'outils cités y sont écrits au nom du PRODUIT du compte (`tool_alias`).
    Sinon le renommage se retourne contre lui-même : la consigne la plus lue de la
    plateforme prescrirait `oto_doc`, l'agent l'appellerait (le serveur l'accepte), et
    le client réafficherait `Oto doc` — le nom qu'on voulait faire disparaître, à
    l'endroit exact où il se voit. Appliqué couche par couche, donc l'invariant de
    composition tient sans le redire.
    """
    socle, label = _socle_for(sub)
    couches = [
        {"key": "platform", "label": label, "body": socle},
        {"key": "catalog", "label": "catalogue des capacités", "body": _catalog()},
    ] + _c_layers(sub, org_id)
    prefix = tool_alias.prefix_for(sub)
    if not prefix:
        return couches
    return [{**c, "body": tool_alias.rewrite_prose(c["body"], prefix)} for c in couches]


def compose_session(sub: str | None, org_id: int | None) -> str:
    """L'artefact injecté pour UNE session : bloc A (toujours) + bloc C (contexte +
    guide, si org). Runtime. Fail-open géré dans chaque bloc (un bloc qui échoue
    retombe sur son seed / est omis)."""
    return "\n\n".join(l["body"] for l in session_layers(sub, org_id) if l["body"])


def compose_published_project(project_id: int) -> str | None:
    """L'artefact servi au DESTINATAIRE d'un endpoint de projet publié (ADR 0032).

    Ce n'est PAS `compose_session` : le destinataire est un tiers sans compte. Lui
    injecter le socle plateforme lui sert un mode d'emploi d'outils qu'il n'a pas (et
    nous expose ~12 Ko de vocabulaire interne, feedback #309), pendant qu'il n'a AUCUN
    chemin vers ce que le projet contient — ni poussé, ni tiré (`oto_doc`/`oto_search`
    ne résolvent pas sans `sub`). On sert donc la prose publiée du projet, et rien
    d'autre. `brief_md` n'est jamais servi : il est interne par construction.

    None = pas de prose publiée → l'appelant laisse la surface statique en place.
    """
    try:
        from . import db
        row = db.get_project_by_id(int(project_id)) or {}
    # noqa: SILENT — projet publié non composable ⇒ pas d'instruction, jamais une fausse
    except Exception:  # noqa: BLE001 — fail-open, surface statique
        return None
    if not row:
        return None
    name = (row.get("name") or "").strip()
    header = f"# {name}\n\n" if name else ""
    body = (row.get("mcp_instructions_md") or "").strip()
    if body:
        return header + body
    # Rien de publié : on sert le strict minimum plutôt que le socle plateforme —
    # 12 Ko de vocabulaire interne chez un tiers, pour des outils qu'il n'a pas.
    return (header + "Cet espace a été partagé avec toi. Les outils listés ci-dessous "
            "sont les seuls disponibles : appelle-les pour découvrir ce qu'il contient. "
            "Son propriétaire n'a pas publié de mode d'emploi.")


def default_block(key: str) -> str:
    """Le défaut (seed constant) du bloc plateforme — sert la surface admin à afficher
    le contenu effectif quand la DB n'a pas (encore) de ligne."""
    return {KEY_SECRET_SAUCE: _SECRET_SAUCE}.get(key, "").strip()


def seed_platform_blocks() -> None:
    """No-op (ADR 0042) : la prose init plateforme vit dans `guides` (delivery='init').
    Aucun seed DB — la constante `_SECRET_SAUCE` reste le défaut/fallback (`_platform_block`
    y retombe quand `guides` n'a pas de ligne), et le backfill au boot copie l'override
    admin existant (ex-`platform_instructions`) dans `guides`. Conservée (appelée au boot
    par `server._build_mcp`) pour ne pas toucher le chemin de démarrage."""


# Marque de palier dans l'index — le tag DIT ce que la portée implique. Sans lui,
# une procédure perso et une procédure d'org se ressemblent, et l'agent croit avoir
# publié pour son org ce qu'il a écrit pour lui seul.
_TAG_PALIER = {"user": " [personal]", "org": "", "group": " [team]"}


def skills_index_md(sub: str | None = None, org_id: int | None = None) -> str:
    """Index markdown des procédures VISIBLES par le caller — les siennes (`user`),
    celles de son org, celles de son équipe active — `slug — titre : description`,
    SANS les corps. Sert à enrichir DYNAMIQUEMENT la description de l'outil
    `oto_procedure` au `tools/list` (les procédures ne sont PAS des outils → absentes
    de `tools/list`, donc invisibles sans ça). Fail-open : '' si rien / erreur.

    ⚠️ **Les trois paliers, pas seulement l'org (09/09/2026).** L'ADR 0068 a fait
    qu'une écriture sans `scope` va au palier PERSONNEL depuis le 04/09 ; `op=list`
    cumule bien les trois paliers depuis le même jour, mais cet index-ci — le seul
    texte poussé à CHAQUE agent à CHAQUE connexion — ne lisait que l'org. Une
    procédure écrite à soi n'apparaissait donc dans rien de ce que l'agent reçoit :
    elle existait, et personne ne pouvait la trouver sans la nommer.

    Même cumul, même ORDRE et mêmes seams que `capabilities.orgs.instructions._list_guides`
    (la sienne d'abord — c'est le premier endroit où l'on cherche ce qu'on a écrit).
    Les deux doivent bouger ensemble : un palier ajouté ici sans y être, ou l'inverse,
    et l'agent lit un index qui ne dit pas ce que `op=list` lui rendra."""
    entrees: list[tuple[str, dict]] = []
    try:
        from . import access, org_store
        if sub:
            entrees += [("user", r) for r in org_store.list_instructions("user", sub)]
        if org_id is not None:
            # exclut la base (claude_md)
            entrees += [("org", r) for r in org_store.list_instructions("org", org_id)]
        group_id = access.current_group(sub) if sub else None
        if group_id is not None:
            entrees += [("group", r) for r in org_store.list_instructions("group", group_id)]
    except Exception:
        logger.warning("skills_index_md: lecture sub=%s org=%s échouée (fail-open)",
                       sub, org_id, exc_info=True)
        return ""
    if not entrees:
        return ""
    lines = ["Procedures you can load (pass the `slug` to load the body) — "
             "`[personal]` = yours alone, `[team]` = your team's, no tag = your org's:"]
    for palier, r in entrees:
        desc = (r.get("description") or "").strip()
        lines.append(f"- {r['slug']} — {r['title']}"
                     + (f" : {desc}" if desc else "") + _TAG_PALIER.get(palier, ""))
    return "\n".join(lines)
