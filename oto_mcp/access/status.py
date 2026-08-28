"""Le SNAPSHOT par connecteur — ce que `/api/me` rend au dashboard.

`status_for` est une PROJECTION, pas une résolution : il marche le walker en
sonde de présence (rien n'est déchiffré) et rend, pour chaque connecteur, le
niveau gagnant, les niveaux configurés au-delà, le quota du jour, l'étape
manquante déclarée par le module du connecteur et son état de santé.

Il est structurellement le miroir de `resolve_api_key` — c'est le walker qui
l'en garantit. Ses deux préchargements (sonde de présence + carte des quotas)
sont bâtis sur le SUJET du snapshot, jamais sur le requérant : c'est ce qui
tient la fiche admin d'un tiers juste.

Sommet du package : dépend de `scope`, `cascade`, `quotas` et `rbac`, et rien ne
dépend de lui.
"""
from __future__ import annotations

import logging

from .. import providers, credentials_store, db, group_store, status_hints
from ..connectors import link as connector_link
from . import cascade, quotas, rbac, scope

logger = logging.getLogger(__name__)


def status_for(sub: str, *, org: "int | None | object" = scope._UNSET,
               group: "int | None | object" = scope._UNSET) -> dict:
    """Snapshot pour `/api/me` — rôle + statut par provider :

    - `mode` : `user` (clé perso) | `platform` (grant + quota OK)
              | `over_quota` (grant mais quota épuisé)
              | `forbidden` (ni user key ni grant)

    `org`/`group` explicites (≠ _UNSET) = snapshot d'un TIERS contre SON propre
    contexte (fiche admin), sans current_org/current_group du requérant (anti-fuite).
    """
    role = scope.get_user_role(sub)
    # Org effective résolue une fois (perf : sinon 1 lookup/provider). None pour
    # tout user sans org → la branche org_secret ci-dessous est inerte. Via le seam
    # `current_org` → reflète l'override de session (MCP) ou la consultation (REST
    # view-as) le cas échéant, sinon la maison (ADR 0023).
    active_org = scope.current_org(sub) if org is scope._UNSET else org
    active_group = scope.current_group(sub) if group is scope._UNSET else group
    out: dict = {"role": role, "active_org": active_org,
                 "active_group": active_group, "providers": {}}
    # Équipes du sub dans l'org (une requête, partagée par tous les hints
    # `team_key_group` ci-dessous). Best-effort.
    #
    # ⚠️ **Deux préchargements, mesurés avant d'être écrits (21/08, 67 connecteurs,
    # 1 707 ms à chaud).** Ce qui pesait n'était PAS ce que le lot précédent avait
    # corrigé sur `/shell` : `current_org` est déjà résolu une seule fois ici (9 ms), et
    # appliquer le même correctif n'aurait rien gagné. Ce qui pesait :
    #   • les sondes `member` (589 ms) et `org` (488 ms) — 64 %, une marche par
    #     connecteur ⟹ **sonde préchargée** (l'inventaire lu une fois, la cascade répond
    #     en mémoire ; le walker reste intouché, c'est une sonde de plus) ;
    #   • **le quota, 410 ms et 24 %** — une requête par connecteur sur une table dont
    #     une seule rend tout le jour d'une personne. Personne ne l'avait vu.
    #
    # ⚠️ Les deux sont construits sur `active_org` — donc sur le SUJET du snapshot,
    # jamais sur le requérant. C'est ce qui fait que la fiche admin d'un tiers reste
    # juste : `org`/`group` explicites (≠ `_UNSET`) court-circuitent `current_org`, et
    # les préchargements SUIVENT cette valeur. Un préchargement bâti sur le contexte de
    # l'appelant rouvrirait la fuite que le seam scopé sur l'acteur a fermée.
    try:
        member_groups = (group_store.list_groups_for_user(sub, active_org)
                         if active_org is not None else [])
    # noqa: SILENT — fail-open par palier : un hoquet d'équipe ne prive pas l'org de sa fiche
    except Exception:
        member_groups = []
    try:
        sonde = cascade.preloaded_presence_probe(sub, org=active_org, groups=member_groups)
    except Exception:      # une accélération, jamais un prérequis
        logger.warning("status_for: préchargement des credentials indisponible",
                       exc_info=True)
        sonde = cascade.PRESENCE_PROBE
    try:
        quotas_du_jour = db.usage_today_map(sub)
    except Exception:
        logger.warning("status_for: préchargement des quotas indisponible", exc_info=True)
        quotas_du_jour = None

    def _used(provider: str) -> int:
        """Le compteur du jour — depuis la map préchargée, ou la lecture unitaire.

        Le repli n'est pas décoratif : si le préchargement a échoué, rendre 0 partout
        ferait afficher « quota intact » à quelqu'un qui l'a épuisé. Mieux vaut payer
        les 48 requêtes que mentir sur un quota."""
        # Le compteur est celui de la CLÉ (délégation) : un canal unipile lit
        # celui de son compte porteur, sinon il afficherait « quota intact » à
        # côté du plafond d'une clé déjà épuisée.
        porteur = providers.credential_provider(provider)
        if quotas_du_jour is not None:
            return quotas_du_jour.get(porteur, 0)
        return db.get_usage_today(sub, porteur)

    # Connecteurs qui DÉLÈGUENT leur credential (`Connector.credential_of`, split
    # unipile) : leur marche donnerait, par construction, EXACTEMENT celle de leur
    # porteur — `walk_cascade` normalise avant le premier barreau. La faire six fois
    # de plus n'ajoute pas une information, ça ajoute six marches sur LE chemin chaud
    # (`/api/me`, à chaque chargement du dashboard) — celui-là même pour lequel ce
    # bloc précharge l'inventaire du coffre et la carte des quotas. On les met donc
    # de côté ici et on RECOPIE l'entrée du porteur après la boucle.
    delegants = {p: providers.credential_provider(p) for p in db.KEY_PROVIDERS
                 if providers.credential_provider(p) != p}
    for provider in db.KEY_PROVIDERS:
        if provider in delegants:
            continue
        # Marche COMPLÈTE du walker en sonde PRÉSENCE (pas de déchiffrement sur le
        # chemin /api/me) : le gagnant donne le mode, les barreaux suivants restent
        # affichables (flags par niveau). Miroir STRUCTUREL de resolve_api_key —
        # toute divergence ferait mentir /api/me sur le mode réel.
        hits = list(cascade.walk_cascade(sub, provider, org=active_org, group=active_group,
                                 probe=sonde, want="auto"))
        user_has = any(r.mode == "user" and r.via == "local" for r in hits)
        group_has = any(r.mode == "group" for r in hits)
        org_has = any(r.mode == "org" for r in hits)
        grant = next((r.payload for r in hits if r.mode == "platform"), None)
        used = _used(provider)
        limit = (grant.get("daily_quota") if grant else None) or quotas.quota_for(provider)

        winner = hits[0] if hits else None
        if winner is None:
            mode = "forbidden"
        elif winner.mode == "platform" and limit and used >= limit:
            mode = "over_quota"
        else:
            mode = winner.mode

        out["providers"][provider] = {
            "mode": mode,
            "user_key_configured": user_has,
            "group_secret_configured": group_has,
            "org_secret_configured": org_has,
            "platform_key_label": grant["label"] if grant else None,
            "quota_used_today": used,
            # limit 0 = illimité (convention default_quota) → None pour que l'UI
            # affiche « ∞ », pas « /0 » (qui se lit comme un quota épuisé).
            "quota_daily": (limit or None) if grant else None,
            # Clé d'équipe « à portée » (membre d'une équipe qui a le secret, sans
            # l'avoir active) : rien ne résout mais une clé existe → l'UI doit le
            # dire au lieu d'un « pas de clé » sec.
            "team_key_group": (rbac.reachable_team_key(sub, active_org, provider,
                                                  groups=member_groups)
                               if mode == "forbidden" else None),
        }

    # Recopie des délégants (cf. ci-dessus) : même clé ⟹ même verdict, mot pour mot.
    # ⚠️ Copie, pas partage de référence : deux cartes qui pointeraient le même dict
    # se répondraient l'une l'autre au premier `.update()` d'un appelant.
    # Porteur absent (non keyed, ou registre incohérent) ⟹ on n'invente rien : la
    # carte n'a pas d'entrée, comme n'importe quel connecteur sans credential.
    for delegant, porteur in delegants.items():
        entree = out["providers"].get(porteur)
        if entree is not None:
            out["providers"][delegant] = dict(entree)

    # Credentials byo_user à champs déclarés, hors KEY_PROVIDERS (modèle générique
    # multi-champs, ADR 0011) : mounts basic_auth (planity) ET clients in-process
    # multi-secrets (silae, zoho). Pas de quota ni de grant — le credential EST le
    # grant (cf. resolve_mount_token / resolve_credential_fields). Miroir de la
    # cascade byo user > groupe actif > org (un provider `fields` org-shareable
    # résout par le secret d'équipe/org — l'ex-check user-only affichait
    # `forbidden` avec une clé d'org qui résolvait, l'UI mentait ; corrigé
    # 2026-07-16). Permet au dashboard d'afficher « configuré / remove ».
    for c in providers.REGISTRY.values():
        if (c.name in out["providers"] or not c.secret_fields
                or "byo_user" not in c.auth_modes):
            continue
        # Même walker, `want='byo'` (le credential EST le grant — pas de palier
        # plateforme ni de quota, cf. resolve_credential_fields).
        hits = list(cascade.walk_cascade(sub, c.name, org=active_org, group=active_group,
                                 probe=sonde, want="byo"))
        mode = hits[0].mode if hits else "forbidden"
        out["providers"][c.name] = {
            "mode": mode,
            "user_key_configured": any(r.mode == "user" and r.via == "local"
                                       for r in hits),
            "group_secret_configured": any(r.mode == "group" for r in hits),
            "org_secret_configured": any(r.mode == "org" for r in hits),
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
            "team_key_group": (rbac.reachable_team_key(sub, active_org, c.name,
                                                  groups=member_groups)
                               if mode == "forbidden" else None),
        }

    # Connecteurs à SESSION navigateur (`personal_session`, secret_kind="cookie" :
    # brevo/crunchbase) : pas de champ à saisir → connexion par Live View Browserbase
    # (MCP `<ns>_connect_start`), le credential = le Context persisté au coffre. On
    # expose juste « configuré + depuis quand » pour que la carte rende son widget
    # session (ADR 0026 prévoyait `providers` sans jamais l'alimenter → /api/me ne
    # disait plus rien sur ces sessions ; corrigé 2026-06-30).
    for c in providers.REGISTRY.values():
        if c.name in out["providers"] or c.secret_kind != "cookie":
            continue
        shareable = c.name in cascade.ORG_SHAREABLE_PROVIDERS
        st = (credentials_store.credential_status(
                  credentials_store.MEMBER,
                  credentials_store.member_id(active_org, sub), c.name)
              if active_org is not None else None)
        # Sessions partagées (connecteur org-partageable) : équipe active puis org.
        # Miroir de la cascade de résolution (membre > groupe > org).
        grp_st = (credentials_store.credential_status("group", str(active_group), c.name)
                  if shareable and active_group is not None else None)
        org_st = (credentials_store.credential_status("org", str(active_org), c.name)
                  if shareable and active_org is not None else None)
        meta = (st or {}).get("meta") or {}
        # `mode` = niveau gagnant de la cascade (membre > groupe > org), pour que la
        # carte dise sous quelle session on résout — comme les connecteurs keyés.
        if st:
            mode = "user"
        elif grp_st:
            mode = "group"
        elif org_st:
            mode = "org"
        else:
            mode = "forbidden"
        out["providers"][c.name] = {
            "mode": mode,
            "user_key_configured": st is not None,
            "session_set_at": st["set_at"] if st else None,
            # Identité/cible par défaut du sélecteur ADR 0024 (pennylaneged : la
            # société cliente = SA GED) — satellites PUBLICS du meta, la carte les
            # affiche sans lister (le listing = une session Browserbase louée).
            "identity_id": meta.get("default_identity_id"),
            "identity_label": meta.get("default_identity_label"),
            # Sessions partagées (une par scope) : présence + horodatage, pour que la
            # carte affiche/déconnecte chaque niveau. `session_set_at` reste le membre.
            "group_secret_configured": grp_st is not None,
            "group_session_set_at": grp_st["set_at"] if grp_st else None,
            "org_secret_configured": org_st is not None,
            "org_session_set_at": org_st["set_at"] if org_st else None,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }

    # 4e boucle — connecteurs à credential OAuth FÉDÉRÉ (atlassian, folkmcp, google).
    # Ils ne sont dans AUCUNE des trois boucles ci-dessus : `keyed=False`,
    # `secret_fields=0`, `secret_kind='oauth'`. Ils n'avaient donc pas d'entrée du tout —
    # et sans entrée, la décoration `pending_action` juste en dessous ne peut pas les
    # atteindre, `health_ko` non plus, et le verdict de la fiche n'a rien à lire. C'est
    # ce trou qui obligeait le dashboard à interroger `/api/<nom>/oauth/status`, donc à
    # connaître les connecteurs par leur nom.
    #
    # La LECTURE est déclarée par chaque module (`connector_link`) : les trois ne rangent
    # pas leur credential au même endroit (scope legacy ("user", sub) pour atlassian et
    # folkmcp, une ligne PAR COMPTE pour google). La TRADUCTION vers `ProviderStatus` —
    # la forme que le dashboard lit — se fait ici, une fois.
    for c in providers.REGISTRY.values():
        if c.name in out["providers"] or c.secret_kind != "oauth":
            continue
        link = connector_link.state(c.name, sub) if sub else None
        if link is None:
            continue          # pas de lecture déclarée, ou lecture en échec : on se tait
        out["providers"][c.name] = {
            # `forbidden` = « aucune clé ne résout », l'état par défaut d'un BYO pas
            # encore connecté (ce n'est PAS un refus RBAC — cf. la carte connecteur).
            "mode": "user" if link.linked else "forbidden",
            "user_key_configured": link.linked,
            "session_set_at": link.set_at,
            "group_secret_configured": False,
            "org_secret_configured": False,
            "platform_key_label": None,
            "quota_used_today": 0,
            "quota_daily": None,
        }

    # Étape manquante par connecteur (seam générique `pending_action`, lot 2) :
    # « la clé résout mais il reste une étape » (unipile : lier un canal…). La
    # spécificité vit DANS le module connecteur (hook `status_hints.register`),
    # jamais ici. Seuls les connecteurs à hook paient le coût ; fail-open.
    for name, entry in out["providers"].items():
        if status_hints.has_hook(name):
            entry["pending_action"] = status_hints.pending_action(
                name, sub, active_org, active_group, entry)

    # Santé du connecteur (flag persistant `meta.health_ko`, posé par la sonde verify =
    # le « read facile » de chaque connecteur) : un « connecteur KO » (session expirée,
    # token révoqué…) reste signalé jusqu'à ce qu'un test/reconnexion le rétablisse.
    # Lu en UN batch sur les clés MEMBRE de l'acteur — générique (tout connecteur), fail-open.
    if sub and active_org is not None:
        try:
            health = {r["connector"]: (r.get("meta") or {})
                      for r in credentials_store.list_credentials(
                          credentials_store.MEMBER, credentials_store.member_id(active_org, sub))
                      if r.get("account") == ""}
            for name, entry in out["providers"].items():
                m = health.get(name) or {}
                if m.get("health_ko"):
                    entry["health_ko"] = True
                    entry["health_reason"] = m.get("health_reason")
        # noqa: SILENT — fail-open par palier sur la fiche de statut
        except Exception:  # noqa: BLE001 — la santé est un bonus, jamais bloquant
            pass

    # RBAC connecteur (ADR 0025 org + 0012 B2 équipe) : « aucune clé ne résout »
    # (`mode='forbidden'`) et « l'accès t'est refusé » sont DEUX choses, et le snapshot
    # ne disait que la première. L'écran en tirait un « Réservé à certaines équipes —
    # demande à un admin » sur le simple fait qu'aucune clé n'était posée : un mur
    # affiché à quelqu'un que rien ne bloque, jusqu'à un org_admin devant SON propre
    # connecteur. Faux diagnostic repéré le 2026-07-16, resté sans signal pour le
    # corriger jusqu'ici (oto-dashboard#126).
    #
    # Même seam que l'enforcement call-time (`require_connector_access`), donc mêmes
    # escalades : super_admin, org_admin, chef d'équipe ne sont jamais refusés.
    # Fail-open INDÉPENDANT par palier, et fail-open GLOBAL : un hoquet de DB ne doit
    # pas inventer une restriction — mieux vaut ne rien annoncer que refuser à tort.
    denied: set = set()
    try:
        denied |= rbac.rbac_denied_connectors(sub, active_org) if sub else set()
    except Exception:
        logger.warning("status_for: RBAC d'org indisponible pour %s — aucune "
                       "restriction annoncée", sub, exc_info=True)
    try:
        denied |= rbac.group_rbac_denied_connectors(sub, active_group) if sub else set()
    except Exception:
        logger.warning("status_for: RBAC d'équipe indisponible pour %s — aucune "
                       "restriction annoncée", sub, exc_info=True)
    for name, entry in out["providers"].items():
        entry["rbac_restricted"] = name in denied
    return out
