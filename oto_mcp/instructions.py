"""Instructions serveur MCP (champ FastMCP `instructions=`) — le **contexte oto**
injecté à Claude au handshake `initialize`. C'est LE canal fiable de bootstrap d'un
agent (model-agnostic), pas un appel d'outil volontaire.

Refonte #50 (amende ADR 0014/0017) — l'artefact injecté est **composé de 2 blocs** :

- **Bloc A — secret sauce plateforme** : posture + boucle d'usage + **catalogue de
  namespaces** (dérivé du registre). Prose stockée en DB
  (`platform_instructions['secret_sauce']`), éditable seulement par l'admin plateforme,
  **inviolable par l'org**, toujours injectée ; le catalogue est appendé à la composition.
  La constante `_SECRET_SAUCE` = le défaut seedé au boot + le fallback (aucun accès DB à
  l'import).
- **Bloc C — contexte dynamique** par-(sub, org) : section de contexte résolu (org /
  équipe / connecteurs actifs / N derniers projets / derniers déroulés / fiche
  « situation avec oto » de l'user) + la **doctrine de base de l'org** (`claude_md`) avec
  substitution des variables `{{org}}` / `{{user}}` / `{{équipe}}` / `{{connecteurs_actifs}}`.

L'onboarding n'est PAS un bloc : c'est un projet « Découverte » (ADR 0032 §7) semé à la
création de l'org perso, qui remonte via la ligne « Projets récents » du bloc C.

`render()` (STATIQUE, sans DB) = bloc A seed + catalogue → défaut de boot et fallback.
`compose_session(sub, org_id)` (RUNTIME) = l'artefact réel par session.
Tout est **fail-open** : toute erreur retombe sur la surface statique."""
from __future__ import annotations

import logging

from . import providers

logger = logging.getLogger(__name__)

# Clé du bloc plateforme en DB (table `platform_instructions`).
KEY_SECRET_SAUCE = "secret_sauce"

# --- Bloc A — secret sauce plateforme (défaut seedé + fallback) -------------
_SECRET_SAUCE = """\
Oto — TA boîte à outils d'automatisation (prospection B2B, données entreprise FR, CRM, email, messagerie, base de connaissance…). Ces outils `*_` AGISSENT sur les comptes et données de l'utilisateur : utilise-les DIRECTEMENT pour répondre. **Par défaut, pour toute tâche dans ce périmètre, sers-toi d'oto sans qu'on te dise « avec oto ».** Oto n'est PAS un sujet à rechercher sur le web — ne lance pas de recherche web pour « découvrir Oto » ; tout ce qu'il faut est ici et dans les outils.

**Encadre et remonte.** Quand tu exécutes une procédure — un workflow doctriné OU un déroulé one-shot qui mérite d'être tracé — ouvre-la par `run_start(label, doctrine?)` (passe `doctrine`=slug pour une doctrine nommée, omets-le pour un run ad-hoc) et ferme-la par `run_finish(run_id, outcome)` (done|abandoned|failed|blocked). **Remonte tout signal d'usage** via `feedback(signal, kind, target, text?)` : `signal='gap'` quand oto ne couvre PAS ce dont tu as besoin (outil, doctrine ou donnée manquants — `target`=ce que tu voulais faire) plutôt que d'abandonner en silence ; `signal='tool_feedback'` quand un outil se comporte mal ou excellemment (`target`=le nom de l'outil). **Déclenche-le DE TOI-MÊME, immédiatement, sans attendre que l'utilisateur te le demande** : dès qu'un outil échoue (erreur, timeout), renvoie un résultat trompeur/vide/incohérent, ou qu'une capacité te manque pour agir — appelle `feedback` sur le coup, puis poursuis. Un signal manqué = un bug que la plateforme ne verra jamais. C'est ainsi que la plateforme apprend.

**Travaille dans un projet.** Un projet est le foyer d'une tâche : son contexte (brief, tableaux, connecteurs préconfigurés, procédures). Quand tu agis POUR un projet, active-le par `oto_use_project(project_id)` (liste/charge via `oto_project` op=list/get) — alors tes connecteurs prennent l'identité préconfigurée du projet, tes runs lui sont rattachés, et tes tableaux de sortie doivent y être liés (`oto_project(op=link, target_type=tableau)`). Une procédure exécutée dans un projet partage SES ressources (tableaux, connecteurs) : ne crée pas de ressources propres à la procédure. Pour une tâche ad-hoc sans projet existant (extraction one-shot, prospection ponctuelle…), **crée un projet** pour héberger sa sortie et sa trace plutôt que de travailler hors-sol."""

# En-tête du catalogue de namespaces (dérivé du registre), appendé au bloc A.
_CATALOG_HEADER = (
    "Namespaces (capacités appelables ; certaines « à activer » selon la config de ton "
    "org — leurs outils apparaissent une fois activées) :"
)

_DOCTRINE_HEADER = "## Doctrine de ton organisation"
_CONTEXT_HEADER = "## Ton contexte oto"

# Tokens de variable substitués dans la doctrine d'org (bloc C). Auto-contexte v1.
_VAR_TOKENS = ("{{org}}", "{{user}}", "{{équipe}}", "{{equipe}}", "{{connecteurs_actifs}}")


# --- Lecture des blocs plateforme (DB override → seed) ----------------------

def _platform_block(key: str, seed: str) -> str:
    """Le bloc plateforme `key` : override DB s'il existe et non vide, sinon `seed`
    (constante). Fail-open au seed. Runtime uniquement (jamais à l'import)."""
    try:
        from . import db
        row = db.get_platform_instruction(key)
        body = ((row or {}).get("body_md") or "").strip()
        if body:
            return body
    except Exception:
        logger.warning("lecture bloc plateforme '%s' échouée (fallback seed)", key,
                       exc_info=True)
    return seed.strip()


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

    org = org_store.get_org(org_id) or {}
    org_name = org.get("name") or f"#{org_id}"

    user_name = ""
    if sub:
        try:
            u = db.get_user(sub) or {}
            user_name = (u.get("name") or u.get("email") or "").strip()
        except Exception:
            pass

    role = ""
    if sub:
        try:
            role = roles.effective_org_role(sub, org_id) or ""
        except Exception:
            pass

    group_name = ""
    try:
        from . import group_store
        gid = access.current_group(sub) if sub else None
        if gid is not None:
            group_name = ((group_store.get_group(gid) or {}).get("name") or "").strip()
    except Exception:
        pass

    connectors: list[str] = []
    if sub:
        try:
            providers_status = (access.status_for(sub).get("providers") or {})
            connectors = sorted(
                name for name, st in providers_status.items()
                if st.get("mode") in ("user", "group", "org", "platform")
            )
        except Exception:
            pass

    projects: list[str] = []
    try:
        rows = db.list_projects_for_owners([("org", str(org_id))])
        # + les projets LIVRÉS à cette org (partagés via resource_grants, #52) — c'est
        # l'exposition au handshake : le client ouvre le projet livré en un message.
        seen = {r.get("id") for r in rows}
        principals = [("org", str(org_id))] + ([("user", sub)] if sub else [])
        rows += [r for r in db.list_projects_granted_to(principals)
                 if r.get("id") not in seen]
        projects = [r.get("name") or f"#{r.get('id')}" for r in rows[:5]]
    except Exception:
        pass

    runs: list[dict] = []
    if sub:
        try:
            runs = db.recent_runs(sub, org_id, limit=5)
        except Exception:
            pass

    # Fiche « situation avec oto » (ce que l'agent sait de l'utilisateur, entretenu via
    # `oto_profile`) — réinjectée pour personnaliser l'aide. Best-effort.
    profile: dict = {}
    if sub:
        try:
            profile = db.get_account_profile(sub).get("profile") or {}
        except Exception:
            pass

    return {
        "org_name": org_name, "user_name": user_name, "role": role,
        "group_name": group_name, "connectors": connectors,
        "projects": projects, "runs": runs, "profile": profile,
    }


def _apply_vars(body: str, ctx: dict) -> str:
    """Substitue les variables d'auto-contexte dans la doctrine d'org. Les tokens
    inconnus sont laissés tels quels (intention de l'auteur)."""
    repl = {
        "{{org}}": ctx["org_name"],
        "{{user}}": ctx["user_name"] or "—",
        "{{équipe}}": ctx["group_name"] or "—",
        "{{equipe}}": ctx["group_name"] or "—",
        "{{connecteurs_actifs}}": ", ".join(ctx["connectors"]) or "—",
    }
    for token, value in repl.items():
        if token in body:
            body = body.replace(token, value)
    return body


def _format_context(ctx: dict) -> str:
    """La section « ## Ton contexte oto » — auto-contexte + anticipation (projets,
    déroulés). Lignes optionnelles : seules celles avec de la donnée sont rendues."""
    lines = [_CONTEXT_HEADER, ""]
    role = f" (ton rôle : {ctx['role']})" if ctx["role"] else ""
    lines.append(f"- Organisation : {ctx['org_name']}{role}")
    if ctx["group_name"]:
        lines.append(f"- Équipe active : {ctx['group_name']}")
    if ctx["connectors"]:
        lines.append(f"- Connecteurs actifs : {', '.join(ctx['connectors'])}")
    if ctx["projects"]:
        lines.append(f"- Projets récents : {' · '.join(ctx['projects'])}")
    if ctx["runs"]:
        bits = []
        for r in ctx["runs"]:
            label = r.get("label") or r.get("run_id") or "?"
            doc = f" [{r['doctrine']}]" if r.get("doctrine") else ""
            outcome = f" → {r['outcome']}" if r.get("outcome") else " (en cours)"
            bits.append(f"{label}{doc}{outcome}")
        lines.append(f"- Derniers déroulés : {' · '.join(bits)}")
    profile_md = _format_profile(ctx.get("profile") or {})
    if profile_md:
        lines += ["", profile_md]
    return "\n".join(lines)


# Libellés lisibles des champs connus de la fiche (cf. tools/profile.PROFILE_FIELDS) ;
# une clé libre inconnue est rendue telle quelle.
_PROFILE_LABELS = {
    "full_name": "Nom", "role": "Rôle", "company": "Entreprise / secteur",
    "goals": "Objectifs", "crm": "CRM", "connectors_wanted": "Connecteurs voulus",
    "tone": "Ton / préférences",
}


def _format_profile(profile: dict) -> str:
    """La fiche « situation avec oto » de l'user (champs remplis seulement). '' si vide.
    Entretenue par l'agent via `oto_profile` ; sert à personnaliser l'aide."""
    rows = []
    for key, value in profile.items():
        text = str(value).strip() if value is not None else ""
        if not text:
            continue
        rows.append(f"- {_PROFILE_LABELS.get(key, key)} : {text}")
    if not rows:
        return ""
    return "### Ce que tu sais de l'utilisateur\n" + "\n".join(rows)


def _block_c(sub: str | None, org_id: int | None) -> str:
    """Le bloc contexte dynamique : section de contexte résolu + doctrine de base de
    l'org (avec variables). '' si pas d'org. Fail-open : doctrine simple sans contexte
    si la résolution échoue."""
    if org_id is None:
        return ""
    try:
        ctx = _resolve_context(sub, org_id)
    except Exception:
        logger.warning("résolution du contexte org=%s échouée (fail-open doctrine)",
                       org_id, exc_info=True)
        return _doctrine_only(org_id)

    sections = [_format_context(ctx)]
    doctrine = _format_doctrine(org_id, ctx)
    if doctrine:
        sections.append(doctrine)
    return "\n\n".join(sections)


def _format_doctrine(org_id: int, ctx: dict) -> str:
    """La doctrine de base de l'org (`claude_md`), variables substituées, sous son
    en-tête. '' si absente/vide."""
    try:
        from . import org_store
        instr = org_store.get_instruction(org_id, org_store.BASE_SLUG)
        body = ((instr or {}).get("body_md") or "").strip()
    except Exception:
        logger.warning("lecture doctrine org=%s échouée (fail-open)", org_id, exc_info=True)
        return ""
    if not body:
        return ""
    return f"{_DOCTRINE_HEADER} ({ctx['org_name']})\n\n{_apply_vars(body, ctx)}"


def _doctrine_only(org_id: int) -> str:
    """Fallback : la doctrine seule (sans section de contexte, sans variables), si la
    résolution du contexte a échoué mais qu'on peut encore lire la doctrine."""
    try:
        from . import org_store
        instr = org_store.get_instruction(org_id, org_store.BASE_SLUG)
        body = ((instr or {}).get("body_md") or "").strip()
        if not body:
            return ""
        name = (org_store.get_org(org_id) or {}).get("name") or f"#{org_id}"
    except Exception:
        return ""
    return f"{_DOCTRINE_HEADER} ({name})\n\n{body}"


# --- Composition ------------------------------------------------------------

def render() -> str:
    """Surface STATIQUE (constantes seules, aucun accès DB) : bloc A (secret sauce +
    catalogue dérivé). Défaut de boot `FastMCP(instructions=…)` et fallback ultime."""
    return f"{_SECRET_SAUCE.strip()}\n\n{_catalog()}"


def compose_session(sub: str | None, org_id: int | None) -> str:
    """L'artefact injecté pour UNE session : bloc A (toujours) + bloc C (contexte +
    doctrine, si org). Runtime. Fail-open géré dans chaque bloc (un bloc qui échoue
    retombe sur son seed / est omis)."""
    parts = [_block_a()]
    block_c = _block_c(sub, org_id)
    if block_c:
        parts.append(block_c)
    return "\n\n".join(parts)


def default_block(key: str) -> str:
    """Le défaut (seed constant) du bloc plateforme — sert la surface admin à afficher
    le contenu effectif quand la DB n'a pas (encore) de ligne."""
    return {KEY_SECRET_SAUCE: _SECRET_SAUCE}.get(key, "").strip()


def seed_platform_blocks() -> None:
    """Pose au boot le défaut du bloc plateforme s'il n'existe pas encore (idempotent,
    best-effort). Le code reste le défaut ; la DB porte l'override admin."""
    try:
        from . import db
        db.seed_platform_instruction(KEY_SECRET_SAUCE, _SECRET_SAUCE.strip())
    except Exception:
        logger.warning("seed du bloc plateforme échoué (non bloquant)", exc_info=True)


def skills_index_md(org_id: int | None) -> str:
    """Index markdown des doctrines NOMMÉES (skills) d'une org — `slug — titre :
    description`, SANS les corps. Sert à enrichir DYNAMIQUEMENT la description de
    l'outil `oto_get_doctrine` au `tools/list` (les skills ne sont PAS des outils →
    absents de `tools/list`, donc invisibles sans ça). Fail-open : '' si pas d'org /
    aucune doctrine / erreur."""
    if org_id is None:
        return ""
    try:
        from . import org_store
        rows = org_store.list_instructions(org_id)   # exclut la base (claude_md)
    except Exception:
        logger.warning("skills_index_md: lecture org=%s échouée (fail-open)",
                       org_id, exc_info=True)
        return ""
    if not rows:
        return ""
    lines = ["Doctrines nommées de ton org (passe le `slug` pour charger le corps) :"]
    for r in rows:
        desc = (r.get("description") or "").strip()
        lines.append(f"- {r['slug']} — {r['title']}" + (f" : {desc}" if desc else ""))
    return "\n".join(lines)
