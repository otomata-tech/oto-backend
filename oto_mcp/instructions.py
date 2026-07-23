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

from . import providers

logger = logging.getLogger(__name__)

# Clé du bloc plateforme en DB (table `platform_instructions`).
KEY_SECRET_SAUCE = "secret_sauce"

# --- Bloc A — secret sauce plateforme (défaut seedé + fallback) -------------
_SECRET_SAUCE = """\
Oto — TA boîte à outils d'automatisation (prospection B2B, données entreprise FR, CRM, email, messagerie, base de connaissance…). Ces outils `*_` AGISSENT sur les comptes et données de l'utilisateur : utilise-les DIRECTEMENT pour répondre. **Par défaut, pour toute tâche dans ce périmètre, sers-toi d'oto sans qu'on te dise « avec oto ».** Oto n'est PAS un sujet à rechercher sur le web — ne lance pas de recherche web pour « découvrir Oto » ; tout ce qu'il faut est ici et dans les outils.

**Encadre et remonte.** Quand tu exécutes une procédure — un workflow doctriné OU un déroulé one-shot qui mérite d'être tracé — ouvre-la par `run_start(label, doctrine?)` (passe `doctrine`=slug pour une doctrine nommée, omets-le pour un run ad-hoc) et ferme-la par `run_finish(run_id, outcome)` (done|abandoned|failed|blocked). **Remonte tout signal d'usage** via `feedback(signal, kind, target, text?)` : `signal='gap'` quand oto ne couvre PAS ce dont tu as besoin (outil, doctrine ou donnée manquants — `target`=ce que tu voulais faire) plutôt que d'abandonner en silence ; `signal='tool_feedback'` quand un outil se comporte mal ou excellemment (`target`=le nom de l'outil). **Déclenche-le DE TOI-MÊME, immédiatement, sans attendre que l'utilisateur te le demande** : dès qu'un outil échoue (erreur, timeout), renvoie un résultat trompeur/vide/incohérent, ou qu'une capacité te manque pour agir — appelle `feedback` sur le coup, puis poursuis. Un signal manqué = un bug que la plateforme ne verra jamais. C'est ainsi que la plateforme apprend.

**Travaille dans un projet.** Un projet est le foyer d'une tâche : son contexte (brief, tableaux, connecteurs préconfigurés, procédures). Quand tu agis POUR un projet, passe le jeton `project=<id>` sur CHAQUE appel de travail (liste/charge via `oto_project` op=list/get — aucun état de session, ADR 0038) : tes connecteurs prennent alors l'identité préconfigurée du projet, l'org du projet s'applique, tes runs lui sont rattachés, et tes tableaux de sortie doivent y être liés (`oto_project(op=link, target_type=tableau)`). Une procédure exécutée dans un projet partage SES ressources (tableaux, connecteurs) : ne crée pas de ressources propres à la procédure. Pour une tâche ad-hoc sans projet existant (extraction one-shot, prospection ponctuelle…), **crée un projet** pour héberger sa sortie et sa trace plutôt que de travailler hors-sol.

**Porte ton contexte DANS l'appel, jamais dans un état de session.** Il n'y a AUCUN état de session serveur (ADR 0038) : quand une action dépend d'un contexte précis, passe-le EN PARAMÈTRE de l'appel — `project=<id>` (le jeton PRIMAIRE : org du projet, slots `slot:<nom>`, identités connecteur préfaites), `org=<id>` / `group=<id>` (agir dans une org/équipe donnée), `account=<id>` (le compte/identité à OPÉRER quand plusieurs sont possibles : un credential parmi plusieurs « 2 Zoho », OU le compte LinkedIn/messagerie sous la clé partagée — **y compris un compte qu'un pair t'a accordé** ; `oto_identity(op='list')` les liste, pin ÉPHÉMÈRE cet appel), `instance=<ref>` (une instance de connecteur PRÉCISE, refs via `oto_instance(op='list')`), `run_id=<id>` (rattacher l'appel à un `run_start`). Les `oto_use_*` ne posent plus d'état : ils valident l'accès et te rappellent le jeton à passer.

**Un outil non listé ? Appelle-le quand même via `oto_call`.** `oto_call(name, arguments)` est le pont universel : il exécute par son nom N'IMPORTE quel outil du catalogue — un outil masqué, un outil de FOD, ou un connecteur que tu VIENS d'activer. ⚠️ Activer un connecteur en cours de conversation ne monte PAS ses outils dans la session (le registre est figé à l'ouverture, et claude.ai n'applique pas le rechargement à chaud) : n'en conclus JAMAIS « la capacité n'existe pas ». Appelle-le tout de suite via `oto_call(name="<connecteur>_…", arguments={…})`, ou invite l'utilisateur à ouvrir une NOUVELLE conversation pour les voir montés. (Un sous-agent que tu lances hérite du même registre figé → lui aussi passe par `oto_call`.)

**Le compte démarre nu : les connecteurs s'INSTALLENT.** Un nouvel espace n'a AUCUN connecteur pré-installé — c'est le régime normal, pas une panne. Si la toolbox ne montre (presque) que des outils `oto_*`/`data_*`, ton rôle est de GUIDER : comprends ce que l'utilisateur veut faire, repère les capacités correspondantes dans le catalogue de namespaces ci-dessous (ou `oto_connector(op='list')` pour l'état par connecteur), propose-en 2-3 pertinentes et installe-les (`oto_connector(op='select', name=…)`). N'attends pas le remontage : exécute tout de suite via `oto_call`. Les capacités open data (`fr_*`, `foncier_*`, `juris_*`…) et à free tier (serper, hunter…) marchent sans aucune configuration ; celles à clé ou à compte se connectent sur le dashboard — dis-le simplement, ne simule jamais un résultat.

**Slots : la procédure déclare, le projet binde.** Une procédure déclare ses entités requises en slots nommés et sa prose les référence `<slot:name>` — jamais un nom d'instance en dur. Le projet fait la correspondance nom→instance via ses liens (`oto_project(op=link, …, slot='name')`). Tu adresses le tableau d'un slot avec `namespace='slot:<name>'` sur les tools `data_*`. Si un slot ne résout pas (pas de projet actif, ou nom non bindé), l'appel est REFUSÉ avec la marche à suivre : **matérialise le contexte d'abord** — demande quel projet (ou crées-en un), et pour chaque slot binde une ressource existante ou crée-la ; ne choisis JAMAIS une table « probable » à la place d'un binding manquant."""

# En-tête du catalogue de namespaces (dérivé du registre), appendé au bloc A.
_CATALOG_HEADER = (
    "Namespaces — le catalogue COMPLET des capacités de la plateforme. Aucune n'est "
    "installée d'office : celles absentes de ta toolbox s'installent via "
    "`oto_connector(op='select', name=…)` (durable) ou s'appellent ponctuellement via "
    "`oto_call` :"
)

# En-têtes des agent README cumulés (bloc C), du général au spécifique.
_README_ORG_HEADER = "## README de ton organisation"
_README_GROUP_HEADER = "## README de ton équipe"
_README_USER_HEADER = "## README de ton utilisateur"
_CONTEXT_HEADER = "## Ton contexte oto"

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
    group_id: int | None = None
    try:
        from . import group_store
        gid = access.current_group(sub) if sub else None
        if gid is not None:
            group_id = gid
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

    # Retour au proposeur (Ship 3) : mes propositions RÉCEMMENT traitées (acceptées /
    # refusées) — sinon l'agent qui a proposé ne voit jamais la résolution côté MCP
    # (le suivi ne vit que dans l'inbox du dashboard). Fenêtre courte = anti-répétition.
    proposals: list[dict] = []
    if sub:
        try:
            proposals = db.list_change_requests_by_requester(sub, since_days=7)[:5]
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
        "group_name": group_name, "group_id": group_id, "connectors": connectors,
        "projects": projects, "runs": runs, "proposals": proposals, "profile": profile,
    }


def _apply_vars(body: str, ctx: dict) -> str:
    """Substitue les variables d'auto-contexte dans la doctrine d'org. Les tokens
    inconnus sont laissés tels quels (intention de l'auteur)."""
    from datetime import date
    projets = " · ".join(ctx.get("projects") or []) or "—"
    role = ctx.get("role") or "—"
    repl = {
        "{{org}}": ctx["org_name"],
        "{{user}}": ctx["user_name"] or "—",
        "{{équipe}}": ctx["group_name"] or "—",
        "{{equipe}}": ctx["group_name"] or "—",
        "{{connecteurs_actifs}}": ", ".join(ctx["connectors"]) or "—",
        "{{rôle}}": role, "{{role}}": role,          # rôle de l'user dans l'org (#6 C)
        "{{date}}": date.today().isoformat(),         # date du jour (session)
        "{{projets_récents}}": projets, "{{projets_recents}}": projets,
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
    if ctx.get("proposals"):
        bits = []
        for cr in ctx["proposals"]:
            title = cr.get("doc_title") or cr.get("proposed_title") or "?"
            verdict = "acceptée" if cr.get("status") == "accepted" else "refusée"
            bits.append(f"« {title} » {verdict}")
        lines.append(f"- Tes propositions traitées : {' · '.join(bits)}")
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
    # substituées. Ordre = cumul de doctrine ; un scope vide est omis.
    layers = [{"key": "context", "label": "ton contexte oto", "body": _format_context(ctx)}]
    profile_md = _format_profile(ctx.get("profile") or {})
    if profile_md:
        layers.append({"key": "profile", "label": "ta fiche", "body": profile_md})
    for key, label, part in (
        ("org", "readme de ton org",
         _render_init_readme("org", org_id, f"{_README_ORG_HEADER} ({ctx['org_name']})", ctx)),
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
    body = guide_store.init_guide_body(scope, owner_id)
    if not body:
        return ""
    return f"{header}\n\n{_apply_vars(body, ctx)}"


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
    except Exception:
        name = f"#{org_id}"
    return f"{_README_ORG_HEADER} ({name})\n\n{body}"


# --- Composition ------------------------------------------------------------

def render() -> str:
    """Surface STATIQUE (constantes seules, aucun accès DB) : bloc A (secret sauce +
    catalogue dérivé). Défaut de boot `FastMCP(instructions=…)` et fallback ultime."""
    return f"{_SECRET_SAUCE.strip()}\n\n{_catalog()}"


def session_layers(sub: str | None, org_id: int | None) -> list[dict]:
    """L'artefact injecté DÉCOMPOSÉ en couches ordonnées `[{key, label, body}]` :
    bloc A (socle plateforme + catalogue dérivé) puis couches du bloc C. Invariant :
    `"\\n\\n".join(bodies) == compose_session(sub, org_id)` — sert la vue de
    transparence (`/api/me/agent-context`) sans dupliquer la composition."""
    return [
        {"key": "platform", "label": "socle oto",
         "body": _platform_block(KEY_SECRET_SAUCE, _SECRET_SAUCE)},
        {"key": "catalog", "label": "catalogue des capacités", "body": _catalog()},
    ] + _c_layers(sub, org_id)


def compose_session(sub: str | None, org_id: int | None) -> str:
    """L'artefact injecté pour UNE session : bloc A (toujours) + bloc C (contexte +
    doctrine, si org). Runtime. Fail-open géré dans chaque bloc (un bloc qui échoue
    retombe sur son seed / est omis)."""
    return "\n\n".join(l["body"] for l in session_layers(sub, org_id) if l["body"])


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


def skills_index_md(org_id: int | None) -> str:
    """Index markdown des doctrines NOMMÉES (skills) d'une org — `slug — titre :
    description`, SANS les corps. Sert à enrichir DYNAMIQUEMENT la description de
    l'outil `oto_procedure` au `tools/list` (les skills ne sont PAS des outils →
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
