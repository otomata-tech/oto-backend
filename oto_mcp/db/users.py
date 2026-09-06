"""Utilisateurs : identité, migration tenant Logto, accès plateforme & quota, rôle, avatar, profil onboarding.

Extrait de l'ex-monolithe `db.py` (barreau final). Fonctions de domaine — la
plomberie est dans `_conn`. Ré-exporté par `db/__init__`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg

logger = logging.getLogger(__name__)

from ._conn import _connect


class OnboardingIncomplet(RuntimeError):
    """Une première inscription n'a pas produit ce qu'elle promet.

    Le compte existe (la ligne `users` est écrite et validée), mais l'un de ses deux
    effets de naissance a échoué : l'org maison, ou l'invitation d'org à honorer.
    Ces deux échecs étaient avalés — `except Exception: pass` — jusqu'au 2026-08-27
    (`docs/silences-2026-08-27.md`, sites B8 et B9). Un compte sans org maison ne
    plante pas là où il naît : il plante plus tard, ailleurs, et sans cause
    remontable (cf. `backfill_member_scope`, qui logue « pas d'org maison pour %s »
    sans jamais pouvoir dire pourquoi).

    ⚠️ **Ce que ce refus ne fait PAS** : annuler la ligne `users`. Elle est validée
    par sa propre transaction avant que les effets ne tournent, et le gate
    `inserted` ne se re-déclenche pas au login suivant — donc un échec DURABLE
    laisse un compte sans espace après ce seul cri. Le rattrapage reste
    `org_store.backfill_personal_orgs`, rejoué à chaque boot. Rendre la naissance
    ATOMIQUE est un lot à part, hors du périmètre de la correction des silences.
    """

    def __init__(self, sub: str, manques: list):
        self.sub, self.manques = sub, list(manques)
        super().__init__(
            f"inscription incomplète pour {sub} : " + ", ".join(self.manques) +
            " — le compte existe mais pas ce qui devait naître avec lui")


class CompteEnPause(RuntimeError):
    """Le geste refusé porte sur un compte MIS EN PAUSE (`users.suspended_at`).

    Levée par les deux écritures qui, sans elle, feraient revenir un compte
    neutralisé : `upsert_user` (qui RECRÉE une ligne absente) et `migrate_sub`
    (le seul `DELETE FROM users` du dépôt).

    Une exception plutôt qu'un retour falsy parce que les appelants d'`upsert_user`
    ignorent tous sa valeur de retour : un refus muet y serait indistinguable d'un
    succès — et un refus indistinguable d'un succès est exactement le mode d'échec
    que cette pause existe pour fermer.

    `code` est la chaîne servie aux DEUX faces, sans traduction : un signal remonté
    par un agent se retrouve tel quel dans le journal."""

    code = "account_suspended"

    def __init__(self, sub: str, motif: str, quoi: str):
        self.sub, self.motif = sub, motif
        super().__init__(f"{quoi} : le compte {sub} est en pause ({motif})")


def upsert_user(sub: str, email: Optional[str] = None, name: Optional[str] = None,
                locale: Optional[str] = None) -> None:
    """Create the user row if missing, refresh email/name if known.

    Le `(xmax = 0)` distingue insert/update sans SELECT préalable : 0 sur une ligne
    fraîchement insérée, ≠ 0 sur un UPDATE — ce qui permet de ne déclencher les
    effets de première inscription (réconciliation d'invitation, org maison) qu'au
    vrai INSERT.

    `locale` (oto-backend#701) : signal DÉDUIT (`Accept-Language`, chemin REST
    interactif — cf. `api/base._authenticate`), jamais un choix. `COALESCE(users.locale,
    EXCLUDED.locale)` — dans ce sens précis, pas celui d'email/name — pose la valeur
    déduite UNIQUEMENT si la ligne n'en porte aucune : un `PUT /api/me/locale`
    (`me.locale.set`) reste prioritaire à vie, y compris face aux logins suivants.
    Les 14 autres sites d'appel ne passent rien (`None`) → `COALESCE(x, NULL) = x`,
    sans effet, comportement inchangé.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO users (sub, email, name, locale)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(sub) DO UPDATE SET
                email = COALESCE(EXCLUDED.email, users.email),
                name  = COALESCE(EXCLUDED.name,  users.name),
                locale = COALESCE(users.locale, EXCLUDED.locale),
                updated_at = NOW()
            RETURNING (xmax = 0) AS inserted
            """,
            (sub, email, name, locale),
        ).fetchone()
        # ⚠️ Une ligne qui vient de NAÎTRE peut être la résurrection d'un compte
        # qu'on a délibérément neutralisé, et le scénario n'a rien de théorique : un
        # compte fusionné laisse son ancien identifiant dans `sub_aliases`, et ce
        # jeton-là reste signé et valide jusqu'à son expiration. S'il se présente
        # sans avoir été canonicalisé (drain désarmé, ou chaîne d'alias à plus d'un
        # maillon — `resolve_sub` ne fait QU'UN saut), on arrive ici avec un sub
        # inconnu : l'INSERT réussit, l'org maison naît, et la personne mise en pause
        # est de retour sous une identité neuve, vierge de toute marque. C'est mot
        # pour mot ce qui s'est produit avec la SUPPRESSION — 884 appels sous une
        # identité morte.
        #
        # ⚠️ **Sous le gate `inserted`, et il n'est pas décoratif.** Une première
        # version fusionnait ce contrôle dans l'INSERT lui-même : `sub_aliases` était
        # alors jointe à CHAQUE requête REST. C'est exactement la lecture que le
        # désarmement du rapprochement (2026-09-03) vient de retirer de ce chemin, et
        # pour la raison dite plus bas — c'est le chemin qui a déjà gelé la
        # production. Ici, le coût est d'une lecture UNE FOIS dans la vie d'un
        # compte, et le chemin chaud est identique à ce qu'il était.
        #
        # Posé AVANT les effets de naissance : rien n'a encore été créé quand il
        # refuse, et la levée annule la transaction — la ligne insérée repart avec.
        if row and row.get("inserted"):
            bloquant = conn.execute(
                """
                -- La CHAÎNE d'alias, pas le premier saut. `migrate_sub` écrit
                -- `(old → new)` sans aplatir les alias qui pointaient déjà vers
                -- `old` : deux fusions successives laissent A→B→C, et une chaîne à
                -- trois maillons a été mesurée en production. Une garde qui ne
                -- regarde qu'un saut trouve B — supprimé par la fusion, donc ni
                -- vivant ni en pause — et laisse passer, ce qui est très exactement
                -- la résurrection qu'elle existe pour interdire.
                --
                -- Clause `CYCLE` native : c'est la convention du dépôt pour toute
                -- récursion sur un graphe auto-référent, et un cliquet la vérifie
                -- (`tests/test_node_parent_cycle.py`). Rien n'interdit
                -- structurellement A→B→A ; sans elle la remontée tournerait jusqu'au
                -- timeout, sur le chemin de naissance d'un compte. La borne de
                -- profondeur qui l'accompagne est une seconde ceinture, contre une
                -- chaîne longue mais acyclique.
                WITH RECURSIVE chaine(sub, profondeur) AS (
                    SELECT a.new_sub, 1
                      FROM sub_aliases a WHERE a.old_sub = %s
                    UNION ALL
                    SELECT a.new_sub, c.profondeur + 1
                      FROM chaine c JOIN sub_aliases a ON a.old_sub = c.sub
                     WHERE c.profondeur < 16
                ) CYCLE sub SET boucle USING chemin
                SELECT u.sub, u.suspended_reason
                  FROM chaine c JOIN users u ON u.sub = c.sub
                 WHERE u.suspended_at IS NOT NULL AND NOT c.boucle
                 LIMIT 1
                """,
                (sub,)).fetchone()
            if bloquant and bloquant.get("sub"):
                logger.error(
                    "upsert_user: RÉSURRECTION refusée — l'identifiant %s redirige "
                    "vers %s, qui est en pause ; la ligne n'est pas créée",
                    sub, bloquant["sub"])
                raise CompteEnPause(bloquant["sub"],
                                    bloquant.get("suspended_reason") or "sans motif",
                                    f"l'identifiant {sub} y redirige")
    # Les DEUX effets de première inscription sont tentés, PUIS l'échec est rendu :
    # que l'un tombe ne dispense pas de l'autre, et l'erreur finale dit lesquels ont
    # manqué. Ils n'étaient ni journalisés ni remontés jusqu'au 2026-08-27 (sites B8
    # et B9 de `docs/silences-2026-08-27.md`) — un compte naissait alors à moitié, et
    # tout ce qui en dépendait échouait plus tard, ailleurs, sans cause remontable.
    manques: list = []
    if row and row.get("inserted") and email:
        # Réconciliation invitation↔signup : un invité d'org qui s'inscrit (par
        # n'importe quel chemin, pas seulement le lien /invite) voit son invitation
        # d'org en attente honorée par l'email vérifié → il rejoint directement
        # l'org au lieu de rester avec une invitation orpheline. Synchrone (une
        # fois, au 1er insert).
        try:
            from .. import org_store
            org_store.reconcile_signup_with_invitation(sub, email)
        except Exception:
            logger.error("upsert_user: invitation d'org NON honorée au signup "
                         "(sub=%s email=%s) — l'invité ne rejoint pas son org",
                         sub, email, exc_info=True)
            manques.append("reconcile_signup_with_invitation")
    if row and row.get("inserted"):
        # Suppression du perso (otomata-private) : tout user a TOUJOURS une org maison.
        # Si l'inscription ne l'a pas déjà rattaché à une org (invitation d'org
        # ci-dessus), on lui crée son espace. Idempotent, hors gate email.
        try:
            from .. import org_store
            org_store.ensure_personal_org(sub, email=email, name=name)
        except Exception:
            logger.error("upsert_user: org maison NON créée (sub=%s email=%s) — "
                         "le compte naîtrait sans espace", sub, email, exc_info=True)
            manques.append("ensure_personal_org")
    if manques:
        raise OnboardingIncomplet(sub, manques)
    # ⚠️ Le RAPPROCHEMENT d'identités a été retiré de ce chemin le 2026-09-03. Il
    # fusionnait à chaque login l'ancien compte de même email dans le nouveau
    # (`reconcile_tenant_migration`, toujours défini plus bas mais plus appelé par
    # aucun chemin servi). Trois raisons, dans cet ordre :
    #  - il ne servait plus : zéro rapprochement sur les 20 jours précédents ;
    #  - sa commande était réglée sur NOTRE émetteur, pas sur celui d'un tiers, donc
    #    il se déclenchait à CHAQUE connexion de CHACUN de nos comptes — et coûtait un
    #    `SELECT … WHERE lower(email)=…` sur `users` (aucun index ne le sert) en tête
    #    de chaque requête REST, sur le chemin qui a déjà gelé la production ;
    #  - il a ressuscité un compte supprimé, qui a ensuite servi 884 appels sous une
    #    identité morte.
    # Le DRAIN d'alias, lui, reste armé : c'est LUI qui empêche cette résurrection
    # (cf. `tenant_migration`). Les deux ne s'arrêtent pas ensemble — et surtout pas
    # dans cet ordre-là.
    # Le rapprochement reste possible en acte d'OPÉRATEUR : `migrate_sub(old, new,
    # operator_source=…)`, où « ces deux subs sont la même personne » est tranché hors
    # du code (ADR 0052 §6). Ce qui est retiré, c'est son déclenchement automatique.


def get_user(sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub = %s", (sub,)).fetchone()
        return dict(row) if row else None


_MAX_EMAILS_BY_SUBS = 200


def emails_by_subs(subs: list) -> dict:
    """`{sub: email}` pour un LOT de subs, en UNE requête.

    Sert les surfaces qui rendent « qui a fait ce geste » à partir d'un journal
    (`tool_calls.email` n'est pas peuplé à l'insert : le sink ne connaît que le
    `sub` du JWT). Résoudre à la LECTURE plutôt qu'à l'écriture vaut aussi pour
    les lignes déjà en base — aucun backfill — et garde le chemin chaud à zéro
    requête. Un sub inconnu (compte supprimé) est simplement absent. Lot borné."""
    wanted = [str(s) for s in dict.fromkeys(subs or []) if s][:_MAX_EMAILS_BY_SUBS]
    if not wanted:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, email FROM users WHERE sub = ANY(%s)", (wanted,)
        ).fetchall()
        return {r["sub"]: r["email"] for r in rows if r.get("email")}


# --- Bascule de tenant Logto (B1, otomata#35) -------------------------------
# Tables d'APPARTENANCE `(scope_id, sub)` portant un `is_active` unique par sub
# (index partiel `*_one_active`) : elles ne peuvent PAS passer par l'UPDATE nu de
# `_SUB_COLUMNS` — cf. l'étape 2 bis de `migrate_sub`. Toute nouvelle table de ce
# genre s'ajoute ICI (garde-fou : `tests/test_migrate_sub_inventory.py`).
_MEMBERSHIP_TABLES = (("org_members", "org_id"), ("org_group_members", "group_id"))

# Tables dont la **clé primaire CONTIENT** une colonne de sub : l'`UPDATE` nu de
# `_SUB_COLUMNS` y lève `UniqueViolation` dès que les deux comptes portent la même
# ligne (même canal opéré, même prêt) — et cette exception fait échouer TOUT le merge,
# pas seulement cette table. Même patron que les appartenances : on jette la ligne de
# l'ancien (le canonique est le nouveau), puis on repointe.
#
# ⚠️ Ces quatre colonnes portent des données que personne ne peut recréer de mémoire :
# le canal de messagerie opéré et les prêts de compte (ADR 0044 §H / #55). Elles
# étaient **hors inventaire** — donc emportées par le `DELETE FROM users` de l'étape 4,
# en silence. Trouvées le 13/08 en dérivant les FK `ON DELETE CASCADE` du DDL plutôt
# qu'en relisant la liste (garde-fou `tests/test_migrate_sub_cascade.py`).
# `(table, colonne de sub, reste de la PK)`.
_PK_SUB_TABLES = (
    ("unipile_operated_accounts", "sub", ("provider",)),
    ("connector_account_grants", "owner_sub", ("provider", "grantee_sub")),
    ("connector_account_grants", "grantee_sub", ("owner_sub", "provider")),
    # Le même prêt, cible GROUPE (oto#40) : `owner_sub` entre dans la PK
    # `(owner_sub, provider, grantee_group_id)` — l'UPDATE nu de `_SUB_COLUMNS` y
    # lèverait `UniqueViolation`, et donc ferait échouer TOUT le merge, dès que les
    # deux comptes de la personne ont prêté le même canal au même groupe (le cas
    # n'est pas tordu : c'est le même geste, refait après la bascule). Sans ce
    # pré-traitement, la ligne partait en CASCADE avec l'ancien compte à l'étape 4 :
    # le groupe perdait l'accès en silence, et le propriétaire fusionné n'avait plus
    # aucune trace de ce qu'il avait prêté — un partage d'équipe ne se re-consent pas
    # de mémoire.
    ("connector_account_group_grants", "owner_sub",
     ("provider", "grantee_group_id")),
    # Dossier du 23/08 (les colonnes à sub que le merge ABANDONNAIT — cf. le tripwire
    # `test_migrate_sub_sub_bearing_columns_are_triaged`) :
    # - l'acceptation CGU suit la personne : sans repointage, le compte fusionné se
    #   voyait redemander des CGU déjà acceptées. Les deux comptes ont accepté ⟹ en
    #   doublon on garde la ligne du canonique (l'acceptation la plus fraîche).
    #   ⚠️ Ceci est la PROJECTION (#487) : elle garde sa PK `(sub, doc_slug)`, donc
    #   son patron ne change pas. Le JOURNAL, lui, n'a aucune unicité et se repointe
    #   nu — cf. `legal_acceptance_events` dans `_SUB_COLUMNS`.
    ("legal_acceptances", "sub", ("doc_slug",)),
    # - la réservation de connecteur (gouvernance d'équipe/org) visait un identifiant
    #   mort : le membre re-fusionné perdait l'accès réservé. `principal_id` mélange
    #   group_id numérique et sub — un sub Logto n'est jamais un entier, l'UPDATE
    #   `col=old_sub` ne peut toucher que les lignes user (même argument que
    #   `resource_grants.principal_id`).
    ("connector_acl", "principal_id",
     ("scope_type", "scope_id", "connector", "principal_type")),
    # - une option offerte (comp) cessait de s'appliquer au compte fusionné — le
    #   symptôme nommé par la carte CLAUDE.md. `entity_id` mélange org_id numérique
    #   et sub : même argument de non-collision.
    ("option_comps", "entity_id", ("entity_type", "option")),
    # - le rôle d'admin de tenant (L-clés PR 2) : PK (slug, sub) ⟹ jeter la ligne de
    #   l'ancien si le canonique l'a déjà, repointer sinon. Une bascule reste bornée à
    #   UN MÊME tenant (R3), donc le rôle garde son slug.
    ("tenant_admins", "sub", ("slug",)),
    # Le refus de recevoir nos relances : PK = `sub` SEUL (reste de PK vide).
    # Un UPDATE nu lèverait `UniqueViolation` — et ferait échouer TOUT le merge —
    # dès que les deux comptes de la personne se sont désinscrits. Le refus suit
    # la personne : ne pas le repointer la ré-abonnerait en silence à la fusion,
    # ce qui est exactement ce qu'un opt-out interdit.
    ("outreach_optouts", "sub", ()),
)

# Colonnes de sub sous un INDEX UNIQUE qui n'est PAS la clé primaire — partiel ou
# non. Troisième famille, et elle existe parce que les deux premières répondent à des
# questions différentes : `_PK_SUB_TABLES` dédoublonne sur la PK (garde-fou
# `test_pk_sub_tables_reste_matches_the_real_primary_key`, qui refuse à juste titre
# une entrée dont la PK ne porte pas le sub), et `_SUB_COLUMNS` fait un UPDATE nu qui
# lèverait `UniqueViolation` ici. Y ranger une table par défaut d'un meilleur bac,
# c'était se tromper deux fois : le DELETE de l'étape 2 ter aurait supprimé TROP (il
# aurait pris `kind` pour une colonne de clé et jeté un essai en croyant dédupliquer
# un envoi).
#
# `(table, colonne de sub, AUTRES colonnes de l'index, prédicat partiel ou None)`.
# Le prédicat est un GABARIT : `{a}` reçoit l'alias de la ligne examinée, pour que la
# même chaîne serve les deux côtés du DELETE.
#
# ⚠️ Chaque entrée DOIT s'accompagner de son `(table, colonne)` dans `_SUB_COLUMNS` :
# cette liste ne fait que RETIRER la ligne en trop, c'est l'UPDATE nu qui repointe.
# Sans le doublon, on supprimerait sans jamais repointer — garde-fou
# `test_migrate_sub_unique_index.py`.
_UNIQUE_INDEX_SUB_TABLES = (
    # Les relances déjà reçues : `idx_outreach_once` = `(campaign, sub) WHERE
    # kind='send'`. Une trace d'envoi SUIT la personne — la FK est en CASCADE, donc
    # ne pas la repointer ne la « conserve » pas, elle la supprime à l'étape 4 : le
    # compte fusionné ressortirait « jamais relancé » et recevrait une seconde fois
    # le même mail, ce que la campagne promet d'éviter. Les lignes `kind='test'` ne
    # sont sous aucune unicité et suivent toutes, sans dédoublonnage.
    ("outreach_sends", "sub", ("campaign",), "{a}.kind = 'send'"),
)

# Inventaire des colonnes keyed-by-sub à repointer (issue oto-backend#56). Plain
# UPDATE : le nouveau sub est frais → aucun conflit de PK, SAUF user_account_profile
# (PK sub), les appartenances ci-dessus et connector_credentials (coffre user),
# traités à part.
_SUB_COLUMNS = [
    # ⚠️ Chaque entrée DOIT exister en DB : la boucle fait des UPDATE nus dans UNE
    # transaction — une table absente fait échouer TOUT le merge (vécu : `user_grants`,
    # droppée par 0044 §F mais restée listée → migrate_sub cassé jusqu'au nettoyage
    # Phase H B1 du 10/07, qui a aussi sorti les reliques datastore `user_datastores.sub`
    # et `datastore_shares` : colonnes mortes, plus rien ne les lit, DROP en B2).
    # données de l'user
    ("usage", "sub"), ("tool_calls", "sub"), ("usage_signals", "sub"),
    ("user_disabled_tools", "sub"), ("user_enabled_tools", "sub"),
    ("org_members", "sub"), ("org_group_members", "sub"),
    ("user_api_tokens", "sub"), ("unipile_accounts", "sub"), ("unipile_pending", "sub"),
    # Le PROPRIÉTAIRE d'un canal opéré : hors PK `(sub, provider)`, donc UPDATE nu
    # (le TITULAIRE, lui, est en PK → `_PK_SUB_TABLES`).
    ("unipile_operated_accounts", "owner_sub"),
    # ressources possédées + grants (ère ownership 0030/0042/0048 — ajoutées Phase H B1 :
    # l'inventaire n'avait jamais suivi, une bascule de tenant orphelinait les ressources
    # user-owned et les grants nominatifs). `owner_id`/`principal_id` mélangent sub et
    # ids numériques d'org/groupe : un sub Logto n'est jamais un entier → l'UPDATE nu
    # `col=old_sub` ne peut toucher que les lignes user.
    ("user_datastores", "owner_id"), ("projects", "owner_id"),
    ("resource_grants", "principal_id"), ("resource_grants", "granted_by"),
    # `guides` est gelée depuis le lot M1 (ses lignes vivent dans `nodes`) mais elle
    # existe encore et la prod y écrit pendant la fenêtre : les DEUX se repointent,
    # sinon une bascule de tenant orphelinerait ce que la conversion recopiera après.
    ("guides", "owner_id"), ("nodes", "owner_id"),
    # L'identité qu'un travail programmé PORTE — celle au nom de laquelle l'agent
    # agira (chantier agents autonomes, 02/09). Elle se repointe pour la même
    # raison que le reste : un travail `pending` doit s'exécuter au nom de la
    # PERSONNE, qui existe toujours sous son nouveau compte.
    # ⚠️ Et ne pas repointer ne conserverait RIEN : il n'y a pas de clé étrangère
    # entre un travail et son porteur, donc la ligne survivrait en désignant un
    # compte disparu. On ne préserverait pas une trace historique, on
    # fabriquerait un pointeur mort — et l'agent s'arrêterait en disant que
    # l'identité n'est plus valide, alors qu'elle l'est, sous un autre nom.
    # Hors PK (`runner_jobs` a `id` pour clé) : UPDATE nu, pas de `_PK_SUB_TABLES`.
    ("runner_jobs", "sub"),
    # l'HISTORIQUE de la personne (dossier du 23/08 — ces lignes survivaient au merge
    # rattachées à un identifiant mort, donc invisibles au compte fusionné : déroulés
    # et activité perdus de vue, déclencheurs orphelins) :
    ("runs", "sub"), ("project_activity", "sub"), ("runner_triggers", "sub"),
    # `runner_fleets.sub` = qui a DÉCLARÉ le passage (R4). Même raison que les
    # déclencheurs : une flotte rattachée à un identifiant mort devient invisible au
    # compte fusionné, et son auteur n'est plus lisible dans l'audit d'un passage.
    ("runner_fleets", "sub"),
    ("tool_calls", "effective_sub"),
    # Le JOURNAL des acceptations légales (#487) — la source de vérité du gate, donc
    # ce qui décide si le compte fusionné se voit redemander ses CGU. Aucune unicité :
    # un UPDATE nu est à la fois suffisant et le seul geste CORRECT ici — dédupliquer
    # SUPPRIMERAIT des preuves de consentement pour cause de doublon, alors que deux
    # acceptations du même document par deux comptes de la même personne sont deux
    # faits distincts, tous deux vrais.
    ("legal_acceptance_events", "sub"),
    # attribution (soft)
    ("projects", "created_by"),
    ("orgs", "created_by"),
    ("org_invitations", "invited_by"), ("org_invitations", "accepted_sub"),
    # Qui a REFUSÉ l'invitation (#654) : même nature qu'`accepted_sub`, donc même
    # repointage — la trace du refus pointerait sinon un compte mort, et l'idempotence
    # du refus (« déjà refusée par toi ») cesserait de reconnaître la personne.
    ("org_invitations", "declined_sub"),
    ("org_groups", "created_by"), ("org_instructions", "set_by"),
    # ⚠️ La bibliothèque est le SEUL endroit du code qui nomme encore sa table plutôt
    # que sa vue `guide_library` (#519 lot B4), et c'est délibéré : cet inventaire est
    # vérifié CONTRE LE DDL (`test_migrate_sub_inventory`), où une vue n'apparaît pas.
    # Y mettre la vue rendrait le garde-fou aveugle à une entrée réellement morte —
    # or c'est lui qui empêche `migrate_sub` d'échouer en entier sur une table
    # disparue. Cette ligne suit la table au lot D (#526).
    ("org_instruction_revisions", "set_by"), ("doctrine_library", "published_by"),
    # attribution (soft), dossier du 23/08 — qui a écrit/résolu/accordé quoi. Sans
    # repointage ces signatures pointaient un compte supprimé (affichage « inconnu »
    # au mieux, jointure vide au pire). `set_by` du coffre est HORS AAD (`_aad` =
    # entity/connector/account) : le repointer ne rend rien indéchiffrable.
    ("usage_signals", "resolved_by"), ("docs", "created_by"),
    ("project_files", "created_by"),
    ("doc_change_requests", "requested_by"), ("doc_change_requests", "resolved_by"),
    ("scheduled_emails", "created_by"), ("connector_credentials", "set_by"),
    ("connector_account_grants", "granted_by"), ("connector_acl", "granted_by"),
    # Le pendant GROUPE (oto#40). Colonne d'AUTEUR, hors PK et SANS FK : elle
    # survit donc au `DELETE FROM users` de l'étape 4 en désignant une ligne `users`
    # qui n'existe plus. Ne pas la repointer ne CONSERVE pas la trace — ça la rend
    # illisible (jointure vide, « inconnu » à l'affichage). Et le merge ne change
    # pas d'auteur : il donne un nouvel identifiant à la MÊME personne (borné à un
    # tenant, ADR 0052 §6). Sur ce chemin l'auteur EST le propriétaire
    # (`granted_by=ctx.sub`, le même sub qu'`owner_sub`) : repointer l'un sans
    # l'autre ferait dire à la ligne « accordé par un identifiant mort de celui qui
    # la possède ». L'identifiant d'origine reste retrouvable par `sub_aliases`.
    ("connector_account_group_grants", "granted_by"),
    ("option_comps", "granted_by"), ("grants", "created_by"),
    # Qui a posé une surcharge de propriété de connecteur (L6 pièce 2 c2). Colonne
    # d'AUTEUR, pas d'identité : un UPDATE nu suffit, comme pour les voisines.
    ("connector_settings", "set_by"),
    # Qui a déclaré un admin de tenant (L-clés PR 2) — colonne d'auteur.
    ("tenant_admins", "granted_by"),
    # Les relances reçues, et qui les a déclenchées. Le TITULAIRE (`sub`) est sous
    # index unique partiel : l'UPDATE nu ci-dessous ne suffit pas seul, il est
    # précédé du retrait de l'étape 2 quinquies (`_UNIQUE_INDEX_SUB_TABLES`).
    ("outreach_sends", "sub"), ("outreach_sends", "sent_by"),
    # Qui a mis un compte en pause — colonne d'AUTEUR sur `users` elle-même, hors PK
    # (la PK est `sub`). Sans FK : la ligne survivrait au `DELETE` de l'étape 4 en
    # désignant un identifiant disparu, donc la signature ne deviendrait pas
    # historique, elle deviendrait illisible. L'étape 3 tourne AVANT ce DELETE.
    ("users", "suspended_by"),
]


# Le DRAIN (la LECTURE de `sub_aliases`) vit dans son propre module depuis le
# 2026-09-03 : il ne faisait qu'UN saut alors que la table porte des CHAÎNES, et le
# corriger demande une récursion bornée et protégée du cycle — trop de matière pour
# rester une note en marge du merge. `migrate_sub` ci-dessous reste le seul ÉCRIVAIN
# de la table ; `sub_aliases.resolve_sub` en est le seul lecteur servi. Ré-exporté ici
# pour que la surface plate `db.resolve_sub` ne bouge pas.
from .sub_aliases import AliasNonResolvable, resolve_sub  # noqa: E402,F401


_ROLE_RANK = {"member": 0, "admin": 1, "super_admin": 2}


def _stronger_role(a: Optional[str], b: Optional[str]) -> str:
    """Le plus haut des deux rôles (une fusion n'enlève pas un privilège)."""
    ra, rb = _ROLE_RANK.get(a or "member", 0), _ROLE_RANK.get(b or "member", 0)
    return (a if ra >= rb else b) or "member"


def migrate_sub(old_sub: str, new_sub: str, *, operator_source: str = "") -> bool:
    """MERGE transactionnel ancien→nouveau compte (bascule de tenant, issue #56).
    Hérite les champs d'accès de l'ancien, repointe TOUTES les tables keyed-by-sub
    (les 3 FK `ON DELETE CASCADE` incluses, AVANT de supprimer l'ancien → pas de
    cascade destructrice) **et la marque d'espace personnel** (`orgs.personal_of`,
    hors inventaire car son index unique interdit l'UPDATE nu — étape 2 quater),
    supprime l'ancienne ligne users, pose l'alias. Idempotent
    (no-op si l'ancien sub n'existe plus). True si une migration a eu lieu.

    ⚠️ Le merge **par email** est borné à UN MÊME tenant (ADR 0052, R3 tranché le
    08/08). Entre deux émetteurs, il serait une fédération d'identités — ce que le §6
    interdit nommément : quiconque s'inscrit chez un tenant tiers sous l'adresse d'un
    autre absorberait son compte oto (rôle, orgs, coffre). Le garde-fou est ici plutôt
    qu'à l'appelant parce que c'est le SEUL endroit qui écrit `sub_aliases` : un alias
    cross-tenant ne peut donc pas naître d'un login, et `resolve_sub` ne peut pas en
    drainer un.

    `operator_source` est la SEULE porte cross-tenant, et elle n'est pas atteignable
    depuis un login : c'est un acte d'opérateur (déclarer un tenant qualifie ses subs
    ⟹ il faut repointer ce qui existait sous la forme nue). Elle ouvre le passage
    délibéré, jamais le merge automatique — l'appelant du chemin chaud
    (`reconcile_tenant_migration`) ne la renseigne pas, donc reste fermé. Ce qui la
    distingue d'un contournement : la décision « ces deux subs sont la même personne »
    est prise HORS du code, et la trace de qui l'a prise part au journal."""
    if not old_sub or not new_sub or old_sub == new_sub:
        return False
    from ..tenancy import current as _tenants
    if not _tenants().same_tenant(old_sub, new_sub) and not operator_source:
        logger.warning(
            "tenant migration REFUSÉE : %s et %s ne relèvent pas du même tenant "
            "(ADR 0052 §6 — pas de fédération d'identités entre tenants)",
            old_sub, new_sub)
        return False
    with _connect() as conn:
        old = conn.execute("SELECT * FROM users WHERE sub=%s", (old_sub,)).fetchone()
        if not old:
            return False  # déjà migré / inexistant
        # ⚠️ Un compte EN PAUSE ne se fusionne pas, dans aucun des deux sens. C'est
        # la seconde moitié de la garde d'`upsert_user`, et elle ferme le seul autre
        # chemin par lequel une pause peut disparaître :
        #  - source en pause ⟹ l'étape 4 (`DELETE FROM users`) emporterait la marque
        #    ET repointerait tout le patrimoine vers un compte vivant : le geste de
        #    neutralisation serait annulé par un geste qui ne le mentionne même pas ;
        #  - cible en pause ⟹ on verserait le patrimoine d'un compte vivant dans un
        #    compte neutralisé, donc on le rendrait inatteignable sans le vouloir.
        # Ce refus vaut aussi pour l'acte d'OPÉRATEUR (`operator_source`) : le
        # rapprochement automatique a été désarmé le 2026-09-03, mais la porte
        # manuelle reste ouverte, et c'est celle qui reste. Un opérateur qui veut
        # vraiment fusionner réveille d'abord — explicitement, avec sa trace.
        # Il LÈVE, il ne rend pas False : `False` veut déjà dire « déjà migré /
        # inexistant » sur ce chemin, et un refus indistinguable d'un no-op n'est
        # pas un refus.
        pause = old.get("suspended_at") and old_sub or None
        new_row = conn.execute("SELECT role, suspended_at, suspended_reason FROM users "
                               "WHERE sub=%s", (new_sub,)).fetchone() or {}
        if new_row.get("suspended_at"):
            pause = new_sub
        if pause:
            motif = (old if pause == old_sub else new_row).get("suspended_reason")
            logger.error("migrate_sub REFUSÉ : %s est en pause (%s → %s)",
                         pause, old_sub, new_sub)
            raise CompteEnPause(pause, motif or "sans motif",
                                f"fusion {old_sub} → {new_sub} refusée")
        # 1. fusionner le rôle SANS JAMAIS RÉTROGRADER : on prend le rôle le plus
        #    fort. ⚠️ Le naïf « hérite de l'ancien » downgrade le nouveau si l'ancien
        #    est un stub frais (member) re-fusionné par-dessus un compte établi
        #    (vécu 2026-06-23 : alexis super_admin repassé member).
        new = new_row  # déjà lu par la garde de pause ci-dessus
        conn.execute(
            """UPDATE users SET
                 role = %(role)s,
                 avatar_url = COALESCE(users.avatar_url, %(av)s), updated_at = NOW()
               WHERE sub = %(new)s""",
            {"role": _stronger_role(old["role"], new.get("role")),
             "av": old.get("avatar_url"), "new": new_sub},
        )
        # 2. user_account_profile (PK sub) : retirer le frais du new PUIS repointer
        #    l'ancien (garde l'historique). DELETE d'abord → pas de conflit PK.
        #    (La NOTE de l'user suit désormais par `("guides", "owner_id")` dans
        #    `_SUB_COLUMNS` — elle a quitté `user_agent_readme` avec l'ADR 0042.)
        conn.execute("DELETE FROM user_account_profile WHERE sub=%s", (new_sub,))
        conn.execute("UPDATE user_account_profile SET sub=%s WHERE sub=%s", (new_sub, old_sub))
        # 2 bis. APPARTENANCES (org_members / org_group_members) : elles ne se repointent
        #    pas en bloc, à cause de DEUX invariants que l'`UPDATE … SET sub=` de l'étape 3
        #    violerait. Vécu prod 2026-07-28 (un user à 2 comptes) : merge en échec
        #    à CHAQUE requête de l'user, donc jamais fusionné + un round-trip Logto et un
        #    traceback par appel.
        #    (a) PK (org_id, sub) : si les deux comptes sont dans la MÊME org, repointer
        #        crée un doublon → on garde la ligne du compte canonique (le new, dont le
        #        rôle vient d'être fusionné au plus fort) et on jette celle de l'ancien.
        #    (b) index partiel `*_one_active` (≤ 1 appartenance ACTIVE par sub) : l'ancien
        #        apporte SA ligne active → deux actives après repointage. Le contexte
        #        courant appartient au compte canonique : les appartenances reprises
        #        arrivent INACTIVES (elles restent accessibles via `oto_use_org`).
        #        ⚠️ Désactivation CONDITIONNELLE : si le new n'a AUCUNE active (stub frais),
        #        celle de l'ancien est la seule → la garder, sinon le compte fusionné se
        #        retrouverait sans org maison.
        for table, key in _MEMBERSHIP_TABLES:
            conn.execute(
                f"DELETE FROM {table} WHERE sub=%s AND {key} IN "
                f"(SELECT {key} FROM {table} WHERE sub=%s)", (old_sub, new_sub))
            conn.execute(
                f"UPDATE {table} SET is_active=FALSE WHERE sub=%s "
                f"AND EXISTS (SELECT 1 FROM {table} WHERE sub=%s AND is_active)",
                (old_sub, new_sub))
        # 2 ter. Colonnes de sub ENTRANT DANS UNE PK (canal opéré, prêts de compte) :
        #    même raison qu'en 2 bis — l'UPDATE nu violerait la PK quand les deux
        #    comptes portent la même ligne. On jette celle de l'ancien, puis on
        #    repointe. Sans ce pré-traitement, ces lignes partaient en CASCADE avec
        #    l'ancien compte à l'étape 4 : un canal de messagerie à reconnecter et
        #    des prêts à re-consentir, sans trace de ce qui a disparu.
        for table, col, reste in _PK_SUB_TABLES:
            # `reste` VIDE = la colonne de sub est à elle seule la clé (PK `sub`
            # nue) : « la même ligne » veut alors dire « une ligne, n'importe
            # laquelle ». Sans ce repli, le `AND` resterait suspendu et le SQL
            # serait invalide — un merge qui échoue en entier sur une syntaxe.
            meme_ligne = " AND ".join(f"a.{c} = b.{c}" for c in reste) or "TRUE"
            conn.execute(
                f"DELETE FROM {table} a WHERE a.{col}=%s AND EXISTS ("
                f"SELECT 1 FROM {table} b WHERE b.{col}=%s AND {meme_ligne})",
                (old_sub, new_sub))
            conn.execute(f"UPDATE {table} SET {col}=%s WHERE {col}=%s",
                         (new_sub, old_sub))
        # 2 quinquies. Colonnes de sub sous un INDEX UNIQUE qui n'est PAS la PK.
        #    Même geste qu'en 2 ter, mais la clé de « la même ligne » est celle de
        #    l'INDEX, pas celle de la table : la ranger en 2 ter aurait pris ses
        #    colonnes de prédicat (`kind`) pour des colonnes de clé et supprimé des
        #    lignes qu'aucune contrainte ne menaçait. Le prédicat partiel est
        #    reporté sur LES DEUX côtés — sans lui, on dédoublonnerait des lignes
        #    que l'index ne regarde même pas.
        #    Le repointage lui-même reste l'UPDATE nu de l'étape 3.
        for table, col, autres, predicat in _UNIQUE_INDEX_SUB_TABLES:
            meme_ligne = " AND ".join(f"a.{c} = b.{c}" for c in autres) or "TRUE"
            filtre_a = f" AND {predicat.format(a='a')}" if predicat else ""
            filtre_b = f" AND {predicat.format(a='b')}" if predicat else ""
            conn.execute(
                f"DELETE FROM {table} a WHERE a.{col}=%s{filtre_a} AND EXISTS ("
                f"SELECT 1 FROM {table} b WHERE b.{col}=%s{filtre_b} AND {meme_ligne})",
                (old_sub, new_sub))
        # 2 quater. La MARQUE d'espace personnel (`orgs.personal_of`) : hors de
        #    `_SUB_COLUMNS` parce qu'un UPDATE nu y violerait l'index unique
        #    `uq_orgs_personal_of` — et pas dans un cas tordu, dans le cas NOMINAL :
        #    le login crée le stub (donc son espace) AVANT que le merge ne le fusionne,
        #    si bien que les deux comptes en ont un.
        #    Sans ce traitement, la marque restait sur un identifiant qui n'existe plus.
        #    `get_personal_org` ne trouvait donc plus rien pour le compte survivant, et
        #    `ensure_personal_org` fabriquait un espace NEUF au boot suivant : deux
        #    organisations au même nom dans la liste de l'utilisateur, dont l'ancienne —
        #    celle qui porte son historique — n'est plus reconnue comme son espace.
        #    Constaté le 2026-08-14 sur 14 comptes, dont les 9 de la bascule de tenant
        #    du 13/08 (un espace en double par personne migrée).
        #    Règle : l'espace de l'ANCIEN compte porte l'historique ⟹ c'est lui qui
        #    reste l'espace personnel. Celui du nouveau est simplement DÉMARQUÉ — il
        #    redevient une organisation ordinaire, que son propriétaire peut supprimer.
        #    On ne l'archive pas ici : « cet espace n'a jamais servi » ne se décide pas
        #    au fond d'une transaction de merge, et un archivage automatique effacerait
        #    de la vue un espace qui, lui, aurait servi. L'avertissement ci-dessous le
        #    nomme pour que le ménage reste un acte explicite.
        perso_ancienne = conn.execute(
            "SELECT id FROM orgs WHERE personal_of=%s AND archived_at IS NULL",
            (old_sub,)).fetchone()
        if perso_ancienne:
            demarquees = conn.execute(
                "UPDATE orgs SET personal_of=NULL WHERE personal_of=%s "
                "AND archived_at IS NULL RETURNING id", (new_sub,)).fetchall()
            conn.execute("UPDATE orgs SET personal_of=%s WHERE id=%s",
                         (new_sub, perso_ancienne["id"]))
            if demarquees:
                logger.warning(
                    "tenant migration: espace personnel conservé = org #%s (celui de %s) ; "
                    "org(s) %s démarquée(s), à archiver si elles n'ont jamais servi",
                    perso_ancienne["id"], old_sub, [r["id"] for r in demarquees])
        # 3. repointer toutes les colonnes sub.
        for table, col in _SUB_COLUMNS:
            conn.execute(f"UPDATE {table} SET {col}=%s WHERE {col}=%s", (new_sub, old_sub))
        # 3 bis. Les ARÊTES du modèle d'accès (blueprint ADR 0053, L5) : `grantee_id`
        #    porte un sub quand `grantee_kind='user'` — sans repointage, un compte
        #    fusionné perdait ses grants de clé plateforme (la chaîne dit MUET, repli
        #    free-tier au mieux, rien au pire). Filtré par kind, pas dans
        #    `_SUB_COLUMNS` : `grantee_id` porte aussi des ids d'org. Pas de contrainte
        #    unique sur (resource, grantee) : si les DEUX comptes portaient une arête
        #    vivante vers la même instance, les deux survivent et « la plus favorable
        #    gagne » (sémantique 0053-D5, déjà celle des arêtes multiples). Les
        #    compteurs suivent l'arête par id — rien à toucher.
        conn.execute(
            "UPDATE grants SET grantee_id=%s WHERE grantee_kind='user' AND grantee_id=%s",
            (new_sub, old_sub))
        # coffre user : on repointe l'AUTEUR, jamais l'ENTITÉ.
        #
        # `_aad(entity_type, entity_id, connector, account)` — l'entité entre dans l'AAD,
        # pas l'auteur. Repointer `entity_id` sans rechiffrer donnait donc une ligne
        # que plus rien ne peut ouvrir : la fiche affiche « clé posée », chaque appel
        # échoue en `InvalidTag`, et le diagnostic accuse le connecteur. Une clé
        # ABSENTE se voit et se repose en dix secondes ; une clé présente-et-morte se
        # débogue une demi-journée (mode d'échec déjà vécu, cf. coffre / clé périmée).
        #
        # On abandonne donc la ligne user derrière : l'utilisateur repose sa clé et
        # l'interface dit la vérité. La ligne orpheline n'est pas supprimée — elle
        # reste rechiffrable à la main si on décide un jour de la récupérer.
        # ⚠️ Toute bascule de tenant doit donc s'accompagner de la LISTE des clés
        # personnelles à reposer, prévenue avant la fenêtre (ADR 0052 §Migrer).
        conn.execute("UPDATE connector_credentials SET set_by=%s WHERE set_by=%s", (new_sub, old_sub))
        # 4. supprimer l'ancienne ligne users (enfants FK déjà repointés).
        conn.execute("DELETE FROM users WHERE sub=%s", (old_sub,))
        # 5. alias (drain des vieux tokens → compte canonique).
        conn.execute(
            "INSERT INTO sub_aliases (old_sub, new_sub) VALUES (%s,%s) "
            "ON CONFLICT (old_sub) DO UPDATE SET new_sub=EXCLUDED.new_sub, migrated_at=NOW()",
            (old_sub, new_sub),
        )
    logger.info("tenant migration: merged %s → %s (%s)", old_sub, new_sub,
                operator_source or "par email")
    return True


def reconcile_tenant_migration(new_sub: str, email_hint: Optional[str] = None) -> bool:
    """Au login sur le nouveau tenant : récupère l'email AUTORITATIF du compte depuis
    Logto (Management API — le `primaryEmail` n'existe qu'après vérification, donc
    fiable même si le token ment) puis, si EXACTEMENT un autre compte partage cet email
    (l'ancien sub), le migre vers new_sub. No-op si email introuvable, 0 (rien à migrer)
    ou >1 (ambigu — on ne touche pas). Idempotent (l'ancien disparaît après migration).

    `email_hint` (claim email du token) n'est qu'un PRÉ-FILTRE pour éviter un appel
    Logto à chaque requête : si aucun autre compte ne porte cet email, rien à migrer →
    on ne sollicite pas Logto. Il n'entre JAMAIS dans la décision de merge (sécurité)."""
    if not new_sub:
        return False
    try:
        # Pré-filtre cheap sur le claim (non fiable) : court-circuite le cas courant
        # (déjà migré / rien à fusionner) sans round-trip Logto.
        if email_hint:
            with _connect() as conn:
                pre = conn.execute(
                    "SELECT 1 FROM users WHERE lower(email)=lower(%s) AND sub<>%s LIMIT 1",
                    (email_hint, new_sub),
                ).fetchone()
            if not pre:
                return False
        # Email AUTORITATIF (source de vérité) — la décision de merge se prend ici.
        from ..auth.facade import logto_user_primary_email
        from ..tenancy import ForeignTenantDirectory
        try:
            email = logto_user_primary_email(new_sub)
        except ForeignTenantDirectory as e:
            # Compte d'un tenant tiers : son email autoritatif vit dans SON annuaire,
            # que nous n'administrons pas. Rien à réconcilier ici — et on le dit en
            # une ligne (sans traceback) plutôt qu'à chaque requête de l'user, ce
            # chemin étant sur le trajet chaud d'`upsert_user`.
            logger.warning("reconcile_tenant_migration ignorée pour %s : %s", new_sub, e)
            return False
        if not email:
            return False
        with _connect() as conn:
            rows = conn.execute(
                "SELECT sub FROM users WHERE lower(email)=lower(%s) AND sub<>%s",
                (email, new_sub),
            ).fetchall()
        if len(rows) != 1:
            return False
        return migrate_sub(rows[0]["sub"], new_sub)
    except Exception:
        logger.warning("reconcile_tenant_migration échoué pour %s", new_sub, exc_info=True)
        return False


def get_user_by_email(email: str) -> Optional[dict]:
    """Le premier compte portant cette adresse — ⚠️ une adresse n'est PAS unique.

    Un même email peut porter plusieurs comptes : le nôtre et celui d'un tenant
    tiers (`tulina:…`), qualifiés par émetteur (ADR 0052). `fetchone()` en rend
    un, dans un ordre que rien ne fixe. Pour DÉCIDER (résoudre une cible,
    suspendre, changer un rôle), passer par `get_users_by_email` et refuser
    l'ambiguïté — cf. `capabilities/orgs/members._resolve_target`.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        return dict(row) if row else None


def get_users_by_email(email: str) -> list[dict]:
    """TOUS les comptes portant cette adresse, du plus ancien au plus récent.

    Existe parce qu'une adresse ne désigne pas un compte : mesuré le 05/09/2026,
    une adresse personnelle en porte deux — un sub nu chez nous, un sub qualifié
    `<tenant>:…` chez un tiers —, avec 91 et 98 appels sur trente jours. Un appelant qui
    filtrait par cette adresse en voyait 91 et ignorait les 98 autres — sans
    qu'aucun zéro ne l'alerte. Un chiffre plausible ne fait douter de rien.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE email = %s ORDER BY created_at, sub",
            (email,)).fetchall()
        return [dict(r) for r in rows]


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, email, name, role, created_at, updated_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def set_user_role(sub: str, role: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET role = %s, updated_at = NOW() WHERE sub = %s",
            (role, sub),
        )


def get_suspension(sub: str) -> Optional[dict]:
    """L'état de pause d'un compte : `None` s'il est vivant (le cas de tout le monde).

    Lecture d'un seul champ décisif (`suspended_at`) sur la clé primaire. C'est la
    SOURCE UNIQUE du prédicat — le seam `account_suspension` est le seul appelant
    prévu, et les deux faces passent par lui. Ne jamais dériver « en pause » d'autre
    chose (une absence de ligne, un rôle, une appartenance) : ce sont des faits
    différents, et le compte en pause a précisément une ligne et un rôle intacts."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub, suspended_at, suspended_by, suspended_reason FROM users "
            "WHERE sub = %s AND suspended_at IS NOT NULL", (sub,)).fetchone()
    return dict(row) if row else None


def suspend_account(sub: str, *, by: str, reason: str) -> Optional[dict]:
    """Met un compte en pause. Rend l'état posé, ou `None` si le compte n'existe pas.

    Idempotent au sens où re-poser une pause ne casse rien, mais **ne réécrit pas**
    une pause en cours : ni l'auteur, ni la date, ni le motif d'origine. Une pause
    est un fait daté ; l'écraser ferait perdre qui l'a décidée et quand, c'est-à-dire
    la seule chose que cette colonne existe pour retenir.

    ⚠️ Ce verbe n'écrit QUE ces trois colonnes. Rien n'est supprimé, rien n'est
    détaché : appartenances, projets, documents, lignes de tableau, credentials du
    coffre et journal restent en place et continuent de désigner ce compte."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE users SET suspended_at = COALESCE(suspended_at, NOW()), "
            "  suspended_by = COALESCE(suspended_by, %s), "
            "  suspended_reason = COALESCE(suspended_reason, %s), updated_at = NOW() "
            "WHERE sub = %s "
            "RETURNING sub, suspended_at, suspended_by, suspended_reason",
            (by, reason, sub)).fetchone()
    return dict(row) if row else None


def resume_account(sub: str) -> bool:
    """Réveille un compte en pause. True si une pause a bien été levée.

    Efface les trois colonnes : l'état courant est la seule chose qu'elles portent.
    L'historique — qui a mis en pause, quand, pourquoi, qui a réveillé — vit dans le
    journal des appels, où l'acte est enregistré avec son auteur et ses arguments.
    Rendre `False` sur un compte déjà vivant permet à l'appelant de distinguer
    « réveillé » de « il ne dormait pas », au lieu d'annoncer un geste qui n'a rien
    fait."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE users SET suspended_at = NULL, suspended_by = NULL, "
            "  suspended_reason = NULL, updated_at = NOW() "
            "WHERE sub = %s AND suspended_at IS NOT NULL RETURNING sub",
            (sub,)).fetchone()
    return bool(row)


def set_avatar_url(sub: str, url: Optional[str]) -> None:
    """Pose (ou efface si url=None) l'URL publique de l'avatar du user.

    URL publique servie depuis l'Object Storage — pas un secret, colonne en
    clair (hors coffre chiffré)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET avatar_url = %s, updated_at = NOW() WHERE sub = %s",
            (url, sub),
        )


def set_user_locale(sub: str, locale: str) -> None:
    """Pose la préférence de langue de l'UI dashboard ('en'|'fr').

    La validation de l'énum vit dans la capacité `me.locale.set` (Input pydantic) —
    ici on écrit la valeur telle quelle. Colonne en clair (préférence, pas un secret)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET locale = %s, updated_at = NOW() WHERE sub = %s",
            (locale, sub),
        )


def get_account_profile(sub: str) -> dict:
    """Fiche « situation avec oto » de l'user : {profile, updated_at}.

    Jamais None — un sub sans ligne renvoie l'état par défaut (profile vide).
    Lecture seule (ne crée pas la ligne)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT profile, updated_at FROM user_account_profile WHERE sub = %s",
            (sub,),
        ).fetchone()
    if not row:
        return {"profile": {}, "updated_at": None}
    profile = row["profile"]
    if isinstance(profile, str):  # selon le driver, JSONB peut revenir en texte
        try:
            profile = json.loads(profile)
        # noqa: SILENT — profil d'onboarding illisible ⇒ fiche sans profil, jamais d'échec d'auth
        except Exception:
            profile = {}
    return {"profile": profile or {}, "updated_at": row["updated_at"]}


def update_account_profile(sub: str, fields: Optional[dict] = None) -> dict:
    """Met à jour la fiche « situation avec oto » (upsert). `fields` est **shallow-mergé**
    dans le JSONB `profile` (clés existantes écrasées, les autres conservées). Renvoie
    l'état résultant (comme `get_account_profile`)."""
    upsert_user(sub)
    patch = json.dumps(fields or {})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_account_profile (sub, profile, updated_at)
            VALUES (%s, %s::jsonb, NOW())
            ON CONFLICT (sub) DO UPDATE SET
                profile = user_account_profile.profile || EXCLUDED.profile,
                updated_at = NOW()
            """,
            (sub, patch),
        )
    return get_account_profile(sub)
