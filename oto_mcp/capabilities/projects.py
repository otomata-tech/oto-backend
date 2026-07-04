"""Projet — couche d'organisation (modèle produit 2026-06-27 ; owned resource ADR 0030).

Un **Projet** = un conteneur de travail POSSÉDÉ (owner_type/owner_id) : un nom + un
**brief** (le doc d'entrée, inline pour l'instant). CRUD co-déclaré MCP+REST (ADR 0009).
L'accès dérive du seam `ownership` : `can_access` (contenu, owner ∪ grants) pour
lire/écrire, `can_govern` (owner ∪ escalade `roles.py`) pour archiver.

Hors périmètre de cet incrément (suivants) : le **partage / transfert** (capacité
générique `oto_resource`, resource_type='project' déjà enregistré dans `ownership`),
les **liens** vers tableaux/procédures/connecteurs/bases, et le **Doc arborescent**
(le brief devient alors le Doc racine).
"""
from __future__ import annotations

import re
import secrets
from typing import Literal, Optional

from pydantic import BaseModel

from .. import db, org_store, ownership, roles, session_org
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

RTYPE = "project"


_LINK_TYPES = ("tableau", "procedure", "connecteur", "doc")


class ProjectInput(BaseModel):
    op: Literal["create", "list", "list_templates", "get", "update", "archive",
                "copy", "handoff", "link", "unlink", "activity", "inventory",
                "publish_mcp", "unpublish_mcp"]
    project_id: Optional[int] = None
    name: Optional[str] = None
    brief_md: Optional[str] = None
    is_template: Optional[bool] = None   # update : publier/retirer le projet comme MODÈLE (ADR 0032 §7 B5a)
    # publish_mcp : publier le projet en endpoint MCP dédié `<mcp_slug>.mcp.oto.cx` (ADR 0032, amende #44).
    mcp_slug: Optional[str] = None       # label de sous-domaine (^[a-z0-9-]{3,}$) ; en `secret`, sert de préfixe optionnel (un suffixe aléatoire est ajouté serveur)
    mcp_access: Optional[Literal["anonymous", "secret", "org"]] = None  # anonymous = sans login + listé ; secret = sans login, non listé, slug non devinable ; org = JWT + org épinglée
    mcp_tools: Optional[list[str]] = None  # allowlist figée du preset (les seuls tools exposés sur le sous-domaine)
    mcp_expose_datastore: Optional[bool] = None  # opt-in `secret` uniquement : exposer les tools data_* (datastore de l'org propriétaire), servis sous l'autorité de l'org — défaut False (datastore privé)
    # create : owner du projet — 'user' (défaut, perso) ou 'org' (classeur d'équipe).
    owner_type: Literal["user", "org"] = "user"
    owner_id: Optional[str] = None   # org.id si owner_type='org' ; ignoré pour 'user'
    # link / unlink : un pointeur typé vers une entité regroupée par le projet.
    target_type: Optional[Literal["tableau", "procedure", "connecteur", "doc"]] = None
    target_ref: Optional[str] = None   # datastore.id | doctrine slug | connecteur name | doc.id (page Documents)
    label: Optional[str] = None        # nom d'affichage (link)
    role: Optional[str] = None         # pourquoi cette entité est ici / son rôle dans le projet (ADR 0032 §2)
    config: Optional[dict] = None      # surcharge contextuelle PRÉFAITE du lien (ADR 0032 §4) — connecteur : {identity_id?, instructions_md?} (legacy : identité dans config ; multi-binding : voir identity_ref) ; tableau : {provision?: "shared"|"empty"|"seeded"} = comment la COPIE de projet traite ce tableau (ADR 0032 §6)
    identity_ref: Optional[str] = None  # connecteur : identité (compte) du BINDING — clé de multiplicité (#57) ; N liens par connecteur, une identité par binding. link sans identity_ref = binding par défaut ; unlink sans identity_ref = TOUS les bindings du connecteur
    slot: Optional[str] = None         # ADR 0035 (B2) : nom de SLOT que ce lien binde — vocabulaire DU PROJET (unicité (projet, slot) → 409 slot_taken). Fait correspondre le lien aux slots déclarés par les procédures (<slot:name>)


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _handoff_md(row: dict) -> str:
    """Texte copier-coller « reprendre dans Claude » (ADR 0032 §7 B5b) : un blob
    universel (Claude/GPT/markdown) qui pré-écrit « charge ce projet ». Pur (entrée
    = dict projet, sortie = str), sans I/O — testable isolément.

    SÉCURITÉ — n'embarque PAS le `brief_md` : un projet partagé/modèle peut porter un
    brief à contenu hostile (injection de prompt) qui, collé dans Claude, s'exécuterait
    comme une consigne. Le blob ne porte que l'instruction de CHARGEMENT (id + nom) ;
    l'agent lit le brief via `oto_project(op=get)` — donnée d'outil, pas texte pré-collé."""
    pid, name = row["id"], row.get("name") or f"#{row['id']}"
    return (
        f"Charge le projet Oto #{pid} « {name} » : appelle `oto_use_project({pid})` "
        f"pour l'activer dans cette conversation, puis `oto_project(op=get, "
        f"project_id={pid})` pour son brief, ses pages et ses entités liées. "
        f"Travaille DANS ce projet (ses connecteurs préconfigurés, ses tableaux de sortie)."
    )


def _mcp_url(slug: object, access: str) -> object:
    """URL du connecteur MCP d'un projet publié : `secret` → `<slug>.share.oto.cx/mcp`
    (partage navigable), `anonymous`/`org` → `<slug>.mcp.oto.cx/mcp`. None si non publié."""
    if not slug or access == "off":
        return None
    dom = "share.oto.cx" if access == "secret" else "mcp.oto.cx"
    return f"https://{slug}.{dom}/mcp"


def _view(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "brief_md": row.get("brief_md", ""),
        "owner_type": row["owner_type"], "owner_id": row["owner_id"],
        "is_template": bool(row.get("is_template")),
        # Publication MCP (ADR 0032) : présence + URLs dérivées. `secret` = partage
        # navigable `<slug>.share.oto.cx` (UI + /mcp) ; `anonymous`/`org` = `<slug>.mcp.oto.cx`.
        "mcp_slug": row.get("mcp_slug"),
        "mcp_access": row.get("mcp_access") or "off",
        "mcp_tools": list(row.get("mcp_tools") or []),
        "mcp_expose_datastore": bool(row.get("mcp_expose_datastore")),
        "mcp_url": _mcp_url(row.get("mcp_slug"), row.get("mcp_access") or "off"),
        # Base de PARTAGE navigable (lecture seule, humain) — mode `secret` uniquement.
        "share_url": (f"https://{row['mcp_slug']}.share.oto.cx"
                      if row.get("mcp_slug") and (row.get("mcp_access") or "off") == "secret" else None),
        "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
        "archived_at": row.get("archived_at"),
    }


def _require_active_org_visible(ctx: ResolvedCtx, row: dict) -> None:
    """Gate de CONTEXTE (ADR 0023) des accès par-id. Sans lui, une URL directe
    `/projects/<id>` (ou `oto_use_project`) atteint un projet d'une AUTRE de mes orgs —
    fuite hors contexte. Délègue la visibilité à `ownership.visible_in_org` (primitive
    partagée) ; ajoute un message ACTIONNABLE (bascule d'org) si l'acteur y a accès par
    une autre org, 404 non-disclosant sinon (ne révèle pas l'existence)."""
    if ownership.visible_in_org(ctx.sub, ctx.org_id, RTYPE, str(row["id"])):
        return
    rid = str(row["id"])
    if ownership.can_access(ctx.sub, RTYPE, rid, "read"):
        owner = ownership.owner_of(RTYPE, rid)
        oname = (org_store.get_org(int(owner[1])) or {}).get("name") \
            if owner and owner[0] == "org" else None
        hint = (f" Il appartient à l'org « {oname} » — bascule dessus (`oto_use_org`) "
                "pour l'ouvrir." if oname else "")
        raise AuthzDenied(403, "wrong_org_context",
                          f"Projet #{rid} hors de l'org active.{hint}")
    raise AuthzDenied(404, "unknown_project", f"Projet #{rid} inconnu.")


def _procedure_ref_to_id(org_id: Optional[int], ref: str) -> str:
    """Réf de procédure (ADR 0032) → l'ID stable de la doctrine. Accepte déjà un id
    (chiffres) ou un slug (résolu dans l'org du projet) ; fallback = laisser tel quel
    (doctrine introuvable / hors org → pas de casse, résolu à la lecture côté front)."""
    if not ref or ref.isdigit() or org_id is None:
        return ref
    inst = org_store.get_instruction(int(org_id), ref)
    return str(inst["id"]) if inst and inst.get("id") is not None else ref


def _resolve_tableau_id(owner_type: object, owner_id: object, ref: str) -> Optional[str]:
    """Réf de tableau → l'ID NUMÉRIQUE stable du namespace (comme les procédures). Accepte
    un id (chiffres, renvoyé tel quel) OU un nom de namespace, résolu contre le datastore du
    PROPRIÉTAIRE du projet. Renvoie None si un nom ne résout pas (namespace inexistant) — le
    caller décide (erreur au link, ref brute conservée à l'unlink). Stocker l'id garde la
    résolution cohérente (audit, list_project_links, share_ui l'attendent numérique)."""
    ref = str(ref or "").strip()
    if not ref or ref.isdigit():
        return ref or None
    ns = db.get_datastore_namespace(str(owner_type or ""), str(owner_id or ""), ref)
    return str(ns["id"]) if ns and ns.get("id") is not None else None


def _mcp_unresolvable_tools(row: dict, tools: list[str],
                            expose_datastore: bool = False) -> list[str]:
    """Sonde de publication SANS LOGIN (anonymous/secret, ADR 0032) : un endpoint sans
    login n'a pas d'identité user → un tool n'est servi que s'il est résoluble SANS `sub`.
    Renvoie la liste des tools **non résolubles** pour l'org propriétaire : tool spine/méta
    (`oto_*`, `data_*`… — sans connecteur, exige une identité) ou credential absent
    (`access.connector_resolvable_for_org`). **Non bloquant** (choix produit) : on publie
    quand même, ces tools sont exposés mais **échouent proprement à l'appel** (McpError, pas
    de fallback) ; la liste remonte en warning pour que l'humain configure une clé d'org ou
    retire les outils.

    `expose_datastore` (opt-in `secret`) : les tools `data_*` agissent alors SOUS l'org
    propriétaire (pas de connecteur, pas de `sub` requis) → considérés résolubles."""
    from .. import access, providers
    from ..tool_visibility import namespace_of
    if row.get("owner_type") != "org":
        return list(tools)  # pas d'org propriétaire → rien ne résout
    org_id = int(row["owner_id"])
    bad = []
    for t in tools:
        if expose_datastore and namespace_of(t) == "data":
            continue  # datastore de l'org, servi sous son autorité (opt-in)
        con = providers.connector_for_namespace(namespace_of(t))
        if con is None or not access.connector_resolvable_for_org(con.name, org_id):
            bad.append(t)
    return sorted(bad)


def _gen_secret_slug(base: Optional[str]) -> str:
    """Slug NON DEVINABLE pour un endpoint `secret` (URL secrète). Un préfixe optionnel
    lisible (issu du slug saisi, réduit à `[a-z0-9-]`) aide à identifier l'endpoint ; le
    suffixe aléatoire garantit l'imprévisibilité. C'est le SEUL secret d'accès du mode
    `secret` (URL-as-capability, endpoint servant sous les credentials de l'org) → 128 bits
    d'entropie, dimensionné contre le bruteforce en ligne (le préfixe, dérivé du nom, est
    devinable : l'entropie doit tenir dans le suffixe seul). `token_hex` reste dans
    `[a-f0-9]` ⊂ charset `_MCP_SLUG_RE` (`token_urlsafe` introduirait `A-Z_-`, rejeté)."""
    prefix = re.sub(r"[^a-z0-9]+", "-", (base or "").strip().lower()).strip("-")[:24]
    token = secrets.token_hex(16)  # 32 chars hex = 128 bits
    return f"{prefix}-{token}" if prefix else f"mcp-{token}"


def _project(ctx: ResolvedCtx, inp: ProjectInput) -> dict:
    sub = ctx.sub

    if inp.op == "create":
        _require(inp.name and inp.name.strip(), "missing_name", "`name` requis.")
        if inp.owner_type == "org":
            _require(inp.owner_id, "missing_owner",
                     "`owner_id` (org) requis pour un projet d'org.")
            _require(roles.is_org_member(sub, int(inp.owner_id)), "forbidden",
                     "Tu n'es pas membre de cette org.", 403)
            owner_type, owner_id = "org", str(inp.owner_id)
        else:
            # Défaut = org ACTIVE de l'user (plus de perso ; ctx.org_id toujours posé).
            _require(ctx.org_id is not None, "no_active_org", "Aucune org active.", 400)
            owner_type, owner_id = "org", str(ctx.org_id)
        pid = db.create_project(owner_type, owner_id, inp.name.strip(),
                                inp.brief_md or "", created_by=sub)
        db.log_project_activity(pid, sub, "project.create", inp.name.strip())
        return _view(db.get_project_by_id(pid))

    if inp.op == "list":
        # Scopé à l'org active (seam `ownership.active_owner`) : charger une org ne
        # montre QUE ses projets (l'org est le contexte, ADR 0023). Un projet d'une
        # autre org ne fuite plus. S'y AJOUTENT les projets PARTAGÉS à cette org, à
        # mes équipes DANS cette org, ou à moi (grant `resource_grants`, livraison
        # #52 / partage d'équipe) — marqués `shared` (l'owner reste l'org émettrice ;
        # ce n'est pas une fuite, c'est un don d'accès). Les groupes sont ceux de
        # l'org active seulement : pas de fuite cross-org.
        owner = ownership.active_owner(ctx.org_id)
        _require(owner is not None, "no_active_org", "Aucune org active.", 400)
        own = [_view(r) for r in db.list_projects_for_owners([owner])]
        seen = {p["id"] for p in own}
        principals = ownership.active_org_principals(ctx.sub, ctx.org_id)
        shared = [{**_view(r), "shared": True, "permission": r.get("permission")}
                  for r in db.list_projects_granted_to(principals)
                  if r["id"] not in seen]
        return {"projects": own + shared}

    if inp.op == "list_templates":
        # Modèles (is_template) lisibles par l'acteur — la bibliothèque copiable (B5a).
        owners = ownership.accessor_scope(sub).owner_pairs()
        return {"projects": [_view(r) for r in
                             db.list_projects_for_owners(owners, templates_only=True)]}

    # ops ciblées : project_id requis + existence
    _require(inp.project_id is not None, "missing_project", "`project_id` requis.")
    rid = str(inp.project_id)
    row = db.get_project_by_id(int(inp.project_id))
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)

    # Gate de CONTEXTE d'org (ADR 0023) — UNE fois pour toutes les ops par-id : un projet
    # n'est atteignable (lecture comme mutation) que DANS l'org qui le possède, jamais
    # depuis une AUTRE de mes orgs. Le pendant par-id du scoping de `op=list`. SEUL `copy`
    # y échappe : copier un MODÈLE (ou un projet lisible) cross-org est une feature (B5a).
    if inp.op != "copy":
        _require_active_org_visible(ctx, row)

    if inp.op == "get":
        from .. import project_audit
        links = db.list_project_links(int(inp.project_id))
        return {**_view(row),
                "can_write": ownership.can_access(sub, RTYPE, rid, "write"),
                "links": links,
                # B5 : liens vérifiés comme des refs — le lien mort remonte à l'agent
                # qui LIT le projet (brief), pas seulement à op=inventory (curation).
                # `links` réutilisé : pas de double chargement.
                "audit": project_audit.audit_project(int(inp.project_id), links)}

    if inp.op == "activity":
        return {"id": inp.project_id, "activity": db.list_project_activity(int(inp.project_id))}

    if inp.op == "handoff":
        # « Reprendre dans Claude » (B5b) : blob copier-coller qui charge ce projet.
        return {"id": inp.project_id, "markdown": _handoff_md(row)}

    if inp.op == "inventory":
        # Inventaire DÉRIVÉ du projet (ADR 0035 B4) — jamais déclaré : surface d'outils
        # = refs <tool:> des procédures liées ∪ usage observé des runs (0017), plus les
        # connecteurs (liens ∪ slots connecteur des procédures). Sert le préremplissage
        # de publish_mcp (l'humain cure) + le manifeste dashboard.
        from .. import org_store, providers, tool_registry
        from ..tool_visibility import namespace_of
        links = db.list_project_links(int(inp.project_id))
        procedures, proc_tools, slot_connectors = [], [], set()
        for l in links:
            if l["target_type"] != "procedure":
                continue
            ref = str(l["target_ref"])
            instr = org_store.get_instruction_by_id(int(ref)) if ref.isdigit() else None
            if not instr:
                procedures.append({"ref": ref, "resolved": False})
                continue
            refs = tool_registry.ref_names(instr.get("body_md") or "")
            slots = instr.get("slots") or []
            procedures.append({"ref": ref, "slug": instr["slug"], "resolved": True,
                               "tools": refs, "slots": slots})
            proc_tools += refs
            slot_connectors |= {s.get("connector") or s["name"] for s in slots
                                if s.get("type") == "connecteur"}
        run_tools = db.project_run_tools(int(inp.project_id))
        # Union suggérée : refs des procédures d'abord (l'intention), puis l'usage ;
        # les tools spine/méta (sans connecteur au registre : oto_*, run_*, data_*…)
        # sont écartés de la suggestion (non publiables), les sources restent brutes.
        seen, tools, connectors = set(), [], set(slot_connectors)
        for t in proc_tools + run_tools:
            if t in seen:
                continue
            seen.add(t)
            con = providers.connector_for_namespace(namespace_of(t))
            if con is None:
                continue
            tools.append(t)
            connectors.add(con.name)
        connectors |= {l["target_ref"] for l in links if l["target_type"] == "connecteur"}
        from .. import project_audit
        return {"id": inp.project_id, "tools": tools, "connectors": sorted(connectors),
                "sources": {"procedures": procedures, "runs": run_tools,
                            "tableaux": [{"slot": l.get("slot"), "namespace": l.get("namespace"),
                                          "ref": l["target_ref"]}
                                         for l in links if l["target_type"] == "tableau"]},
                # B5 : liens vérifiés comme des refs — morts / slots non bindés / inertes.
                "audit": project_audit.audit_project(int(inp.project_id), links)}

    if inp.op == "update":
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        # Publier/retirer comme MODÈLE est un acte de gouvernance (visible aux autres
        # membres de l'org comme bibliothèque) → can_govern, pas un simple write.
        if inp.is_template is not None:
            _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                     "Publier un modèle est réservé au propriétaire / admin.", 403)
        db.update_project(int(inp.project_id),
                          name=(inp.name.strip() if inp.name else None),
                          brief_md=inp.brief_md, is_template=inp.is_template)
        db.log_project_activity(int(inp.project_id), sub, "project.update", inp.name or None)
        return _view(db.get_project_by_id(int(inp.project_id)))

    if inp.op == "copy":
        # Copier un projet qu'on peut LIRE (le sien ou un modèle) → nouveau projet
        # possédé par l'org active (ADR 0032 §7 B5a). L'original reste intact.
        _require(ownership.can_access(sub, RTYPE, rid, "read"), "forbidden", "Accès refusé.", 403)
        _require(inp.name and inp.name.strip(), "missing_name", "`name` (cible) requis.")
        _require(ctx.org_id is not None, "no_active_org", "Aucune org active.", 400)
        new_id, warnings = db.duplicate_project(int(inp.project_id), inp.name.strip(),
                                                "org", str(ctx.org_id), copied_by=sub)
        return {**_view(db.get_project_by_id(new_id)),
                "links": db.list_project_links(new_id), "copied_from": inp.project_id,
                "warnings": warnings}

    if inp.op in ("link", "unlink"):
        _require(ownership.can_access(sub, RTYPE, rid, "write"), "forbidden", "Écriture refusée.", 403)
        _require(inp.target_type and inp.target_ref, "missing_target",
                 "`target_type` et `target_ref` requis.")
        # ADR 0032 « stop using slug » : une procédure est référencée par l'ID STABLE de
        # la doctrine. On accepte un slug (naturel côté agent) OU un id et on stocke l'id
        # (idem à l'unlink pour matcher les lignes migrées).
        target_ref = inp.target_ref
        identity_ref = inp.identity_ref
        config = dict(inp.config) if inp.config else None
        if inp.target_type == "procedure":
            proj_org = int(row["owner_id"]) if row.get("owner_type") == "org" else ctx.org_id
            target_ref = _procedure_ref_to_id(proj_org, target_ref)
        elif inp.target_type == "tableau":
            # Normalise nom→id (le datastore du propriétaire du projet). Stocker l'id garde
            # la résolution cohérente ; un nom introuvable au LINK = erreur (pas de lien mort
            # silencieux), mais un unlink d'une réf legacy/supprimée passe avec la réf brute.
            resolved = _resolve_tableau_id(row.get("owner_type"), row.get("owner_id"), target_ref)
            if resolved is not None:
                target_ref = resolved
            elif inp.op == "link":
                _require(False, "unknown_tableau",
                         f"Aucun tableau nommé « {target_ref} » dans le datastore du projet.", 404)
        elif inp.target_type == "connecteur":
            # L'identité est la clé du BINDING (#57). Fin du doublon : on la sort de
            # config.identity_id vers `identity_ref`. `identity_ref` explicite (front B4 /
            # agent) = multi-binding ; sinon on prend l'identité du config (chemin legacy).
            legacy_id = config.pop("identity_id", None) if config else None
            if identity_ref is None:
                identity_ref = legacy_id or None
            # Édition legacy (front actuel, pas d'identity_ref explicite) : s'il existe UN
            # binding unique avec une AUTRE identité, on le DÉPLACE (delete+insert) au lieu
            # d'en créer un 2e — préserve la sémantique « éditer le connecteur du projet ».
            if inp.op == "link" and inp.identity_ref is None:
                existing = [l for l in db.list_project_links(int(inp.project_id))
                            if l["target_type"] == "connecteur" and l["target_ref"] == target_ref]
                if len(existing) == 1 and existing[0].get("identity_ref") != identity_ref:
                    db.remove_project_link(int(inp.project_id), "connecteur", target_ref,
                                           identity_ref=existing[0].get("identity_ref"))
        # ADR 0035 (B2) : nom de slot bindé par ce lien — validé (hygiène de clé) puis
        # unicité (projet, slot) imposée par la DB (ValueError slot_taken → 409).
        slot = None
        if inp.op == "link" and inp.slot is not None:
            from .. import slots as slots_mod
            try:
                slot = slots_mod.normalize_name(inp.slot)
            except ValueError as e:
                _require(False, "invalid_slot", str(e), 400)
        if inp.op == "link":
            try:
                db.add_project_link(int(inp.project_id), inp.target_type, target_ref,
                                    inp.label, role=inp.role, config=config,
                                    identity_ref=identity_ref, slot=slot)
            except ValueError as e:
                code = "slot_taken" if str(e).startswith("slot_taken") else "bad_link"
                _require(False, code, str(e), 409 if code == "slot_taken" else 400)
        else:
            db.remove_project_link(int(inp.project_id), inp.target_type, target_ref,
                                   identity_ref=identity_ref)
        db.log_project_activity(int(inp.project_id), sub, f"project.{inp.op}",
                                f"{inp.target_type}:{inp.label or target_ref}")
        out = {"ok": True, "id": inp.project_id,
               "links": db.list_project_links(int(inp.project_id))}
        # B5 — complétude au link : lier une procédure dont des slots ne sont pas
        # bindés ⇒ WARNING immédiat (non bloquant), le pendant des refs mortes 0014.
        if inp.op == "link" and inp.target_type == "procedure":
            try:
                from .. import org_store, project_audit
                instr = (org_store.get_instruction_by_id(int(target_ref))
                         if str(target_ref).isdigit() else None)
                missing = project_audit.unbound_slots_for(instr, out["links"]) if instr else []
                if missing:
                    out["unbound_slots"] = missing
                    out["warning"] = (
                        f"la procédure `{instr['slug']}` déclare des slots non bindés dans ce "
                        f"projet : {', '.join(missing)} — binde chacun "
                        f"(`oto_project op=link project_id={inp.project_id} target_type=… "
                        "target_ref=… slot='<name>'`) avant de l'exécuter ici.")
            except Exception:  # noqa: BLE001 — warning best-effort, le link a réussi
                pass
        return out

    if inp.op in ("publish_mcp", "unpublish_mcp"):
        # Publier un endpoint MCP = acte de gouvernance (URL publique au nom de l'org).
        _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
                 "Publier un endpoint MCP est réservé au propriétaire / admin.", 403)
        if inp.op == "unpublish_mcp":
            db.set_project_mcp_publication(int(inp.project_id), slug=None, access="off", tools=[])
            db.log_project_activity(int(inp.project_id), sub, "project.unpublish_mcp", None)
            return _view(db.get_project_by_id(int(inp.project_id)))
        access_mode = inp.mcp_access or "anonymous"
        tools = [t for t in (inp.mcp_tools or []) if t and t.strip()]
        _require(bool(tools), "missing_tools", "`mcp_tools` (liste non vide) requis.", 400)
        # Opt-in datastore : réservé à `secret` (un endpoint `anonymous` est PUBLIC ; un
        # endpoint `org` a déjà un membre authentifié → data_* résout nativement).
        expose_datastore = bool(inp.mcp_expose_datastore)
        _require(not (expose_datastore and access_mode != "secret"),
                 "datastore_secret_only",
                 "mcp_expose_datastore est réservé à l'accès `secret` (un endpoint "
                 "`anonymous` est public, un endpoint `org` résout déjà data_* via le "
                 "membre authentifié).", 400)
        # Slug effectif : `secret` → non devinable, généré serveur (préfixe optionnel issu
        # du slug saisi) ; on RÉUTILISE le slug existant si l'endpoint est déjà secret
        # (re-publier ne doit pas casser l'URL déjà distribuée). anonymous/org : slug saisi requis.
        if access_mode == "secret":
            slug = (row.get("mcp_slug") if row.get("mcp_access") == "secret" and row.get("mcp_slug")
                    else _gen_secret_slug(inp.mcp_slug))
        else:
            _require(bool(inp.mcp_slug), "missing_slug", "`mcp_slug` requis.", 400)
            slug = inp.mcp_slug
        # Sonde credential-less NON bloquante (anonymous/secret) : on publie, les tools non
        # résolubles sont exposés mais échouent proprement à l'appel — la liste remonte en warning.
        unresolvable = (_mcp_unresolvable_tools(row, tools, expose_datastore)
                        if access_mode in ("anonymous", "secret") else [])
        try:
            db.set_project_mcp_publication(int(inp.project_id), slug=slug,
                                           access=access_mode, tools=tools,
                                           expose_datastore=expose_datastore)
        except ValueError as e:
            code = "slug_taken" if str(e).startswith("slug_taken") else "bad_slug"
            _require(False, code, str(e), 409 if code == "slug_taken" else 400)
        # Endpoint AUTHED (#44) : enregistre l'API resource Logto (audience JWT) pour que
        # Logto émette un JWT signé pour ce sous-domaine (sinon token opaque → invalid_token).
        # Best-effort : un échec Management API n'empêche pas la publication (loggué).
        resource_registered = None
        if access_mode == "org":
            try:
                from .. import oauth_facade
                oauth_facade.ensure_api_resource(
                    f"https://{slug}.mcp.oto.cx/mcp",
                    name=f"oto MCP — {row.get('name') or slug}")
                resource_registered = True
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).exception(
                    "ensure_api_resource échoué pour %s", slug)
                resource_registered = False
        db.log_project_activity(int(inp.project_id), sub, "project.publish_mcp",
                                f"{access_mode}:{slug}")
        out = _view(db.get_project_by_id(int(inp.project_id)))
        if resource_registered is not None:
            out["logto_resource_registered"] = resource_registered
        if unresolvable:
            out["mcp_unresolvable_tools"] = unresolvable
        return out

    # archive
    _require(ownership.can_govern(sub, RTYPE, rid), "forbidden",
             "Archivage réservé au propriétaire / admin.", 403)
    db.archive_project(int(inp.project_id))
    db.log_project_activity(int(inp.project_id), sub, "project.archive", row.get("name"))
    return {"ok": True, "id": inp.project_id, "archived": True}


CAPABILITIES += [
    Capability(
        key="me.project", handler=_project, Input=ProjectInput, authz=SUB_ONLY,
        description=(
            "Projects (organization layer, ADR 0030 owned resource). op=create (name, "
            "optional brief_md; owner_type user|org + owner_id for a team project) / list "
            "(ORG-SCOPED: the ACTIVE org's projects + projects shared with it or with you — "
            "switch org with oto_use_org to see another org's; every response echoes the "
            "effective org in `_org`) / list_templates (published MODEL projects you can copy) / "
            "get (project + its links + an `audit` of those links: dead_links / unbound_slots / "
            "inert_procedures — a linked entity that no longer resolves surfaces HERE, act on it) / "
            "update (name, brief_md, is_template = publish/unpublish "
            "as a copyable model) / copy (deep-copy a project you can read — its own or a model "
            "— into a NEW project in your active org: brief + doc tree + links + raw files; "
            "a tableau link stays a POINTER to the same namespace by default (config.provision "
            "absent/`shared`), but with config.provision=`empty`|`seeded` it is PROVISIONED — a "
            "FRESH namespace (same schema, rows only if `seeded`) so each copy gets its own "
            "isolated table (e.g. a campaign template's lead pool). A `shared` tableau owned by "
            "ANOTHER org is re-provisioned EMPTY (never a pointer to the source's private data), "
            "and links whose namespace no longer resolves are skipped — both surfaced in the "
            "response `warnings`. Pass project_id = source + name = target) / handoff (a copy-paste « resume in Claude » blob "
            "that pre-writes oto_use_project for this project) / archive / link & unlink "
            "(attach an entity: "
            "target_type tableau|procedure|connecteur|doc + target_ref = its id/slug/name "
            "(for `doc`: a Documents page id — attach a knowledge page from the org KB or "
            "any readable project), optional label + optional "
            "role = why this entity belongs to the project + optional config = the entity's "
            "PRE-MADE per-project override; for a connecteur: {identity_id?, instructions_md?} "
            "= which account to act as + prose instructions to apply (e.g. 'only filter "
            "agreements by the mutuelle theme'); for a tableau: {provision?: shared|empty|seeded} "
            "= how a project copy treats it (empty/seeded = each copy gets its own fresh table). "
            "Optional `slot` = the SLOT NAME this link BINDS for the project (ADR 0035): "
            "procedures declare required entities as slots and reference them <slot:name> "
            "in their prose — the project maps each name to a concrete entity via its links. "
            "Slot names are a PROJECT-wide vocabulary (unique per project → 409 slot_taken; "
            "two linked procedures sharing `sortie` share the binding). "
            "Re-linking without role/config/slot preserves the "
            "existing ones. get/link return each link's role + slot + config + a derived "
            "`cross_project` flag (the same entity is linked by another project → avoid brutal "
            "edits / ask); a tableau link also returns its resolved `namespace` — address THIS "
            "project's table by that name with the data_* tools (never hardcode a namespace). "
            "Share & transfer go through oto_resource (resource_type='project'). "
            "inventory = the project's DERIVED surface (union of the linked procedures' "
            "<tool:> refs + tools actually used by the project's runs, plus connectors "
            "from links & declared slots) — never retype a tool list: derive, then curate. "
            "publish_mcp (mcp_slug + mcp_access anonymous|secret|org + mcp_tools = the fixed "
            "tool allowlist) publishes the project as a dedicated MCP endpoint "
            "`<mcp_slug>.mcp.oto.cx/mcp`, the toolset served under the OWNER ORG's credentials — "
            "`anonymous` = no login + LISTED in the public directory; `secret` = no login but "
            "UNLISTED, the slug is server-generated & unguessable (a secret URL; mcp_slug is an "
            "optional readable prefix); `org` = Logto JWT + pins the org. For anonymous/secret, "
            "tools that aren't credential-less or resolvable for the org are published anyway but "
            "FAIL cleanly at call time — they come back in `mcp_unresolvable_tools` (configure an "
            "org key or drop them). mcp_expose_datastore (SECRET only) opts the `data_*` tools "
            "in: they then act under the OWNER ORG's authority (read/write the org's namespaces) "
            "without a login — off by default (the datastore stays private); refused on "
            "anonymous/org. unpublish_mcp removes it. get returns "
            "mcp_slug/mcp_access/mcp_tools/mcp_expose_datastore/mcp_url."
        ),
        mcp="oto_project",
        rest=RestBinding("POST", "/api/me/projects"),
    ),
]


# ── Bracelet de session « projet actif » (ADR 0032 §4, B2.2) ─────────────────
# `oto_use_project` pose un projet ACTIF pour la conversation (override de session,
# éphémère, MCP-only — pas de « projet maison »). Tant qu'il est actif, la résolution
# d'identité d'un connecteur applique la surcharge PRÉFAITE du projet (le compte épinglé
# sur le lien). Le bracelet SÉLECTIONNE un projet préfait ; il ne déclare aucune config.
# Miroir de `oto_use_org` (ADR 0023), sans persistance.


class UseProjectInput(BaseModel):
    project_id: int   # id d'un projet auquel tu as accès (cf. oto_project op=list)


class NoInput(BaseModel):
    pass


def _use_project(ctx: ResolvedCtx, inp: UseProjectInput) -> dict:
    """Active un projet pour CETTE conversation (override de session, ADR 0032 §4)."""
    rid = str(inp.project_id)
    row = db.get_project_by_id(inp.project_id)
    _require(row is not None, "unknown_project", f"Projet #{inp.project_id} inconnu.", 404)
    _require_active_org_visible(ctx, row)
    sid = session_org.current_session_id()
    _require(sid is not None, "no_session",
             "oto_use_project ne s'utilise que dans une conversation MCP.", 400)
    session_org.set_project_override(sid, inp.project_id)
    # Surcharges connecteur préfaites portées par ce projet (informatif pour l'agent).
    overrides = [{"connector": l["target_ref"], "config": l.get("config") or {}}
                 for l in db.list_project_links(inp.project_id)
                 if l.get("target_type") == "connecteur" and (l.get("config") or {})]
    return {"active_project": inp.project_id, "name": row.get("name"),
            "connector_overrides": overrides}


def _clear_project(ctx: ResolvedCtx, inp: NoInput) -> dict:
    """Quitte le projet actif de la conversation (retour « hors projet »)."""
    sid = session_org.current_session_id()
    if sid is not None:
        session_org.clear_project_override(sid)
    return {"active_project": None}


CAPABILITIES += [
    Capability(
        key="me.use_project", handler=_use_project, Input=UseProjectInput, authz=SUB_ONLY,
        description=(
            "Set the ACTIVE PROJECT for this conversation (project_id from oto_project "
            "op=list). While a project is active, connectors resolve the project's PRE-MADE "
            "identity (which account to act as), set up ahead of time on the project — you "
            "don't declare it. Ephemeral: this conversation only; a new conversation starts "
            "with no active project. Returns the project's connector overrides. Leave with "
            "oto_clear_project."
        ),
        mcp="oto_use_project",
    ),
    Capability(
        key="me.clear_project", handler=_clear_project, Input=NoInput, authz=SUB_ONLY,
        description="Leave the active project of this conversation (back to no project).",
        mcp="oto_clear_project",
    ),
]
