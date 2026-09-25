"""Sélection de connecteurs par un membre — modèle « marketplace » (ADR 0019).

**Trois faits distincts, ne pas confondre** (cf. ADR 0019) :
- *Exposition* = `connector_activation` (qui PEUT voir, gouvernance plateforme,
  deny-by-default, admin-only) — le **plafond**.
- *Proposition* = `orgs.default_connectors` (ce que l'org RECOMMANDE, consultatif).
- *Sélection* = **cette table** (ce que le MEMBRE installe dans son espace), per
  `(sub, org_id)`. C'est l'état neuf que le marketplace introduit.

Trois états membre, par connecteur :
- **non-sélectionné** : aucune ligne → reste dans la library/catalogue.
- **sélectionné-actif** (`state='active'`) : outils visibles (visibilité normale).
- **sélectionné-pause** (`state='paused'`) : installé mais outils masqués.

La table est la **source de vérité de la sélection** ; la *visibilité* reste
calculée (`tool_visibility.is_tool_visible` inchangé) — le middleware en dérive un
masquage supplémentaire (pause/non-sélection), jamais sur PROTECTED_TOOLS ni
grant-only. `org_id=0` = espace perso (sentinelle ADR 0015), comme `user_disabled_tools`.

NB barreau **B1** : table + helpers seuls, AUCUN appelant ne lit encore — canari de
déploiement (no-behavior-change). Le câblage (lecture `/api/me/connectors`, mutation,
masquage pause au middleware) suit en B3/B4/B5.

Convention : self-managing (ouvrent leur propre connexion, comme `connector_activation`).
Seul `init_schema` reçoit le `conn` de la transaction `db.init_db`. Aucun import
oto_mcp au niveau module (leaf) — `db` importé paresseusement.
"""
from __future__ import annotations

# Valeurs fermées de l'état de sélection.
ACTIVE = "active"
PAUSED = "paused"
STATES = (ACTIVE, PAUSED)

# PROVENANCE d'une installation (ADR 0050 §E7, oto#166) — qui a posé la ligne.
# Sans elle, une ligne semée, posée par le kit ou choisie par le membre étaient
# indiscernables, et aucune règle de retrait n'était tenable : retirer du kit ce
# que le kit a posé exige de savoir QUI l'a posé. `inconnue` = toute ligne écrite
# avant la trace, ou par un code qui ne la connaît pas (la production pendant la
# fenêtre préprod→tag : base partagée) — aucun geste d'org ne la retire jamais.
SOCLE = "socle"        # le socle plateforme `default_active`, au semis
KIT = "kit"            # le kit de l'org, au semis ou au geste de l'admin
ADMIN = "admin"        # poussée nominative d'un admin à UN membre
MEMBRE = "membre"      # le membre lui-même (installation, ou reprise après une pause)
INCONNUE = "inconnue"  # antérieure à la trace — jamais retirée par un geste d'org
ORIGINS = (SOCLE, KIT, ADMIN, MEMBRE, INCONNUE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_selected_connectors (
    sub         TEXT   NOT NULL,
    org_id      BIGINT NOT NULL DEFAULT 0,   -- 0 = espace perso (ADR 0015)
    connector   TEXT   NOT NULL,             -- nom de connecteur (registre providers/)
    state       TEXT   NOT NULL DEFAULT 'active',  -- 'active' | 'paused'
    selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Provenance (ADR 0050 §E7) : socle | kit | admin | membre | inconnue. Posée
    -- aussi par `ALTER … ADD COLUMN` dans `db/_init.py` pour la base PARTAGÉE
    -- prod/préprod, où ce `CREATE TABLE` est sauté — les deux définitions sont
    -- identiques (le cliquet `test_boot_order_replay` compare défaut et nullabilité).
    origin      TEXT   NOT NULL DEFAULT 'inconnue',
    PRIMARY KEY (sub, org_id, connector)
);
-- Le RETRAIT par le membre, retenu avec sa date (ADR 0050 §E6/§E7). Une table à part
-- et non un état de plus dans `user_selected_connectors` : le code servi AVANT ce lot
-- (base partagée) lit chaque ligne de cette table-là comme « installé ou en pause »,
-- et servirait un état qu'il ne connaît pas. Ici, il ne voit rien. Un retrait reste
-- un DELETE de la sélection, doublé de cette trace ; un `select`/`pause` du membre
-- l'efface (son dernier geste n'est plus un retrait).
CREATE TABLE IF NOT EXISTS connector_selection_removed (
    sub        TEXT   NOT NULL,
    org_id     BIGINT NOT NULL DEFAULT 0,
    connector  TEXT   NOT NULL,
    removed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id, connector)
);
-- Marque de transition (B6) : un (sub, org) « seedé » a reçu sa sélection initiale
-- (= l'exposé courant en active) au passage au régime strict « non-sélectionné =
-- masqué ». Évite de re-seeder un membre qui a légitimement tout désélectionné.
CREATE TABLE IF NOT EXISTS connector_selection_seeded (
    sub       TEXT   NOT NULL,
    org_id    BIGINT NOT NULL DEFAULT 0,
    seeded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id)
);
"""


# --- schéma (reçoit le conn de la transaction init_db) ----------------------

def init_schema(conn) -> None:
    """Crée la table. Idempotent. Appelé par `db.init_db` dans la même transaction."""
    conn.execute(_SCHEMA)


# --- lectures (self-managing) -----------------------------------------------

def list_selection(sub: str, org_id: int = 0) -> dict[str, str]:
    """Sélections du membre dans une org : `{connector: state}`. Les connecteurs
    absents de la map sont *non-sélectionnés*."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, state FROM user_selected_connectors WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    return {r["connector"]: r["state"] for r in rows}


def list_selection_detail(sub: str, org_id: int = 0) -> dict[str, dict]:
    """Même lecture que `list_selection`, avec la PROVENANCE de chaque ligne :
    `{connector: {"state": …, "origin": …}}` (ADR 0050 §E7)."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, state, origin FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    return {r["connector"]: {"state": r["state"], "origin": r["origin"]} for r in rows}


def list_removed(sub: str, org_id: int = 0) -> dict:
    """Retraits du membre dans une org : `{connector: removed_at}` (ADR 0050 §E6).
    Un connecteur de cette map n'est plus installé ET le membre l'a retiré lui-même :
    aucun geste d'org ne le lui remet."""
    from .. import db

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT connector, removed_at FROM connector_selection_removed "
            "WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchall()
    return {r["connector"]: r["removed_at"] for r in rows}


def state_of(sub: str, connector: str, org_id: int = 0) -> str | None:
    """État d'un connecteur pour le membre : 'active' | 'paused' | None (non-sélectionné)."""
    from .. import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT state FROM user_selected_connectors "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        ).fetchone()
    return row["state"] if row is not None else None


# --- écritures du MEMBRE (self-managing) --------------------------------------

def set_state(sub: str, connector: str, state: str, org_id: int = 0) -> None:
    """Geste du MEMBRE : installe (ou bascule actif↔pause) un connecteur. Upsert.

    Provenance (ADR 0050 §E7) : une installation par le membre, ou sa reprise après
    une pause, passe la ligne à `membre` — c'est ce qui la soustrait à un retrait du
    kit (décision Q1 : « un membre qui l'avait installé lui-même le garde »). Une
    PAUSE ne change pas la provenance d'une ligne existante : mettre en pause ce que
    le kit a posé ne l'approprie pas. Le geste efface un retrait antérieur du membre
    — son dernier geste n'est plus un retrait. ⚠️ Réservé aux gestes du membre : un
    geste d'org écrit par `install_for_member`, jamais par ici (la provenance mentirait)."""
    if state not in STATES:
        raise ValueError(f"état de sélection invalide: {state!r} (∈ {STATES})")
    from .. import db

    on_conflict = ("DO UPDATE SET state = EXCLUDED.state, selected_at = NOW(), "
                   "origin = EXCLUDED.origin" if state == ACTIVE
                   else "DO UPDATE SET state = EXCLUDED.state, selected_at = NOW()")
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state, origin) "
            "VALUES (%s, %s, %s, %s, %s) "
            f"ON CONFLICT (sub, org_id, connector) {on_conflict}",
            (sub, org_id, connector, state, MEMBRE),
        )
        conn.execute(
            "DELETE FROM connector_selection_removed "
            "WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        )


def unselect(sub: str, connector: str, org_id: int = 0) -> bool:
    """Geste du MEMBRE : retire un connecteur de sa sélection (→ retour library).
    Renvoie True si une ligne existait.

    Le retrait est RETENU avec sa date (ADR 0050 §E6) : sans lui, un ajout au kit
    réinstallait ce que le membre venait de retirer — la ligne effacée ne laissait
    aucune trace de son « non ». Un retrait qui ne trouve rien n'écrit rien : il n'y
    avait rien à retirer, et l'appelant le refuse."""
    from .. import db

    with db._connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_selected_connectors WHERE sub = %s AND org_id = %s AND connector = %s",
            (sub, org_id, connector),
        )
        if not (cur.rowcount or 0):
            return False
        conn.execute(
            "INSERT INTO connector_selection_removed (sub, org_id, connector) "
            "VALUES (%s, %s, %s) ON CONFLICT (sub, org_id, connector) "
            "DO UPDATE SET removed_at = NOW()",
            (sub, org_id, connector),
        )
        return True


# --- écriture d'un geste d'ORG (reçoit le conn de l'appelant) --------------------

def install_for_member(conn, sub: str, connector: str, org_id: int, origin: str) -> str:
    """Installe `connector` chez UN membre pour le compte d'un geste d'org (kit,
    poussée) — jamais par-dessus le membre (ADR 0050 §E4/§E6). Reçoit le `conn` de
    l'appelant : un geste d'org est UNE transaction. Rend ce qui s'est passé :

    - `installed`       — aucune ligne, aucun retrait : ligne posée, `active`, `origin` ;
    - `already_active`  — une ligne active existe (quelle qu'en soit la provenance) :
                          intacte ;
    - `paused`          — le membre l'a en pause : intacte (une pause tient) ;
    - `removed_by_member` — le membre l'a retiré lui-même : rien n'est posé.

    La lecture et l'écriture sont une seule instruction gardée par la PK et par le
    retrait, pas un `SELECT` puis un `INSERT` : un membre qui retire le connecteur
    pendant le geste ne le voit pas revenir."""
    if origin not in (SOCLE, KIT, ADMIN):
        raise ValueError(f"provenance d'un geste d'org invalide: {origin!r}")
    cur = conn.execute(
        "INSERT INTO user_selected_connectors (sub, org_id, connector, state, origin) "
        "SELECT %s, %s, %s, 'active', %s "
        " WHERE NOT EXISTS (SELECT 1 FROM connector_selection_removed "
        "                    WHERE sub = %s AND org_id = %s AND connector = %s) "
        "ON CONFLICT (sub, org_id, connector) DO NOTHING",
        (sub, org_id, connector, origin, sub, org_id, connector),
    )
    if cur.rowcount:
        return "installed"
    row = conn.execute(
        "SELECT state FROM user_selected_connectors "
        "WHERE sub = %s AND org_id = %s AND connector = %s",
        (sub, org_id, connector),
    ).fetchone()
    if row is None:
        return "removed_by_member"
    return "already_active" if row["state"] == ACTIVE else "paused"


# --- seed initial d'un (sub, org) — socle curé (ADR 0050) ---------------------

def is_seeded(sub: str, org_id: int = 0) -> bool:
    """True si ce (sub, org) a déjà reçu sa sélection initiale (cf. `seed_active`)."""
    from .. import db

    with db._connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = %s",
            (sub, org_id),
        ).fetchone()
    return row is not None


def seed_active(sub: str, origins: dict[str, str], org_id: int = 0) -> None:
    """Sélection initiale d'un (sub, org) (one-shot, marque `seeded`) : installe
    chaque connecteur de `origins` en `active`, sous sa provenance (`socle` ou
    `kit`, ADR 0050 §E7). L'appelant décide le contenu (`session_visibility`).
    Le reste de l'exposé démarre non-sélectionné (→ library). Idempotent, ne
    réécrit jamais une sélection existante — et ne remet jamais ce que le membre a
    retiré (un geste du kit a pu poser la ligne AVANT son premier passage, et il a
    pu la retirer depuis l'écran, qui ne sème pas)."""
    if not isinstance(origins, dict):
        raise TypeError("seed_active: `origins` = {connecteur: provenance}")
    from .. import db

    with db._connect() as conn:
        for name, origin in origins.items():
            install_for_member(conn, sub, name, org_id, origin)
        conn.execute(
            "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO NOTHING",
            (sub, org_id),
        )


# --- renommage d'un connecteur déposé (report des sélections) ------------------

def rename_selection(conn, old: str, new: str) -> int:
    """Reporte sur `new` les sélections restées sur un connecteur DÉPOSÉ `old`
    (reçoit le `conn` de la transaction `db.init_db`). Renvoie le nombre de lignes
    renommées (hors doublons résorbés). Idempotent : au rejeu, plus aucune ligne ne
    porte `old` — tout devient no-op.

    **Pourquoi une migration de boot et pas un one-shot manuel** : déposer un
    connecteur (#279 : `linkedin` → `aiark`) renomme le registre, mais la toolbox
    d'un membre est la liste de ses connecteurs INSTALLÉS (ADR 0019/0050) — un nom
    qui ne résout plus rien ne monte aucun outil, et sous le régime strict
    « non-sélectionné = masqué » le membre perd la surface entière, sans que rien ne
    le lui dise. Le geste était noté en prose (« reste à faire au tag prod ») : une
    note ne bloque rien et ne rappelle rien, sept tags sont passés au-dessus (#295).
    Une migration de données qui doit suivre un tag est une migration de BOOT.

    L'ORDRE des trois gestes est le correctif, pas un détail : la PK est
    `(sub, org_id, connector)`, donc un `UPDATE … SET connector = new` brut échoue
    sur toute paire qui portait DÉJÀ les deux (elles coexistaient — l'un en mode
    plateforme, l'autre en BYO). D'où : promouvoir, dédoublonner, renommer."""
    if old == new:
        raise ValueError("rename_selection: old et new identiques")
    # 1. Paires portant déjà `new` : le plus PERMISSIF gagne — si `old` était active,
    #    la ligne survivante doit l'être (le membre avait bien l'outil).
    conn.execute(
        "UPDATE user_selected_connectors a SET state = %s "
        " WHERE a.connector = %s AND a.state <> %s "
        "   AND EXISTS (SELECT 1 FROM user_selected_connectors b "
        "                WHERE b.sub = a.sub AND b.org_id = a.org_id "
        "                  AND b.connector = %s AND b.state = %s)",
        (ACTIVE, new, ACTIVE, old, ACTIVE),
    )
    # 2. … et l'ancienne ligne y est alors en trop (sinon le renommage viole la PK).
    conn.execute(
        "DELETE FROM user_selected_connectors a "
        " WHERE a.connector = %s "
        "   AND EXISTS (SELECT 1 FROM user_selected_connectors b "
        "                WHERE b.sub = a.sub AND b.org_id = a.org_id "
        "                  AND b.connector = %s)",
        (old, new),
    )
    # 3. Le reste se renomme sans conflit — et garde son `state` (une sélection en
    #    pause reste en pause : le renommage n'est pas une occasion d'installer).
    cur = conn.execute(
        "UPDATE user_selected_connectors SET connector = %s WHERE connector = %s",
        (new, old),
    )
    # 4. Les RETRAITS du membre suivent aussi (ADR 0050 §E6) : un retrait resté sur
    #    l'ancien nom ne protégerait plus rien, et le kit réinstallerait sous le
    #    nouveau ce que le membre avait retiré. Même ordre : un retrait déjà posé
    #    sous `new` gagne (c'est le plus récent des deux faits), l'ancien part.
    conn.execute(
        "DELETE FROM connector_selection_removed a WHERE a.connector = %s "
        "   AND EXISTS (SELECT 1 FROM connector_selection_removed b "
        "                WHERE b.sub = a.sub AND b.org_id = a.org_id AND b.connector = %s)",
        (old, new),
    )
    conn.execute(
        "UPDATE connector_selection_removed SET connector = %s WHERE connector = %s",
        (new, old),
    )
    return cur.rowcount or 0


# --- one-shot du SPLIT unipile (2026-08-28) ----------------------------------

# Sentinelle du fan-out de split, même ledger et même forme que `_BACKFILL_MARK`
# (jamais un sub réel — les subs Logto sont alphanumériques).
_SPLIT_MARK = "#unipile-split-fanout"
# Le split google (2026-09-26) : le compte + ses six services. Même ledger, sa propre
# sentinelle — deux déménagements, deux marqueurs.
GOOGLE_SPLIT_MARK = "#google-split-fanout"
GOOGLE_SERVICES = ("gmail", "drive", "sheets", "calendar", "tasks", "chat")


def split_fanout_pending(conn, targets: tuple[str, ...],
                         mark: str = _SPLIT_MARK) -> bool:
    """Le fan-out du split doit-il encore tourner sur CETTE base ? (one-shot)

    **Pourquoi ce garde-fou existe.** Le fan-out a été écrit « idempotent, donc
    rejouable à chaque boot » sur la foi d'un `ON CONFLICT DO NOTHING` — qui ne
    protège que les lignes PRÉSENTES. Or désélectionner un connecteur SUPPRIME sa
    ligne (`unselect`, un DELETE) : la protection ne couvrait donc pas le seul cas
    où elle comptait. Résultat vécu : qui retirait WhatsApp le retrouvait installé
    au redémarrage suivant, avec les cinq autres canaux — « ce n'est pas parce
    qu'un connecteur est actif que les autres doivent l'être ». Même mécanique sur
    les deux autres barreaux : une disponibilité plateforme éteinte à la main
    revenait allumée, une ACL d'org effacée revenait posée.

    Un fan-out de split n'est pas une convergence à maintenir — c'est un
    DÉMÉNAGEMENT, vrai une fois. Ce qui doit être rejouable, c'est le BOOT, pas
    l'écriture : d'où une sentinelle, exactement comme le backfill ADR 0050.

    **La base de prod l'a déjà reçu** (elle boote avec ce code depuis le
    2026-08-28), et poser la sentinelle sans plus rien regarder la ferait donc
    tourner une dernière fois — en réinstallant une dernière fois ce que les gens
    ont retiré. D'où la sonde : une base qui porte DÉJÀ une sélection sur l'un des
    canaux a reçu le déménagement, on marque sans réécrire. Une base neuve, ou
    restaurée d'avant le split, n'en porte aucune et le reçoit normalement."""
    # `mark` : UNE sentinelle par split (unipile 2026-08-28, google 2026-09-26) —
    # la seconde ne doit ni lire ni poser la première.
    done = conn.execute(
        "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = 0",
        (mark,),
    ).fetchone()
    if done:
        return False
    deja = conn.execute(
        "SELECT 1 FROM user_selected_connectors WHERE connector = ANY(%s) LIMIT 1",
        (list(targets),),
    ).fetchone()
    if deja:
        mark_split_fanout(conn, mark)
        return False
    return True


def mark_split_fanout(conn, mark: str = _SPLIT_MARK) -> None:
    """Pose la sentinelle du fan-out de split — à appeler APRÈS la passe."""
    conn.execute(
        "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, 0) "
        "ON CONFLICT DO NOTHING",
        (mark,),
    )


def fanout_selection(conn, source: str, targets: tuple[str, ...]) -> int:
    """Étend à `targets` la sélection de `source` — un connecteur qui se SCINDE.

    Pendant de `rename_selection` pour le cas 1→N. Le split unipile du 2026-08-28 en
    est le premier porteur : la carte « messagerie hébergée » est devenue sept cartes
    (le compte + ses six canaux), et un membre qui avait installé `unipile` doit
    retrouver ses outils WhatsApp et LinkedIn là où ils sont MAINTENANT. Sans ce
    geste, sous le régime strict « non-sélectionné = masqué » (ADR 0050), la surface
    de messagerie disparaît de la toolbox de tous ceux qui l'avaient — silencieusement,
    exactement le mode de panne de #295.

    `source` est CONSERVÉ : il ne disparaît pas du registre (il devient le compte
    fournisseur, qui porte la clé). C'est ce qui distingue un split d'un renommage.

    `ON CONFLICT DO NOTHING` sur la PK `(sub, org_id, connector)` : une paire qui
    porte déjà l'un des `targets` garde SON état — un membre qui avait déjà pausé un
    canal ne se le voit pas réinstaller par la migration. Idempotent, donc rejouable
    à chaque boot (base partagée preprod/prod, docs/live-migrations.md).

    Le `state` est HÉRITÉ de la source : une sélection en pause reste en pause. Un
    split n'est pas une occasion d'installer quelque chose."""
    n = 0
    for cible in targets:
        cur = conn.execute(
            "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
            "SELECT sub, org_id, %s, state FROM user_selected_connectors "
            " WHERE connector = %s "
            "ON CONFLICT DO NOTHING",
            (cible, source),
        )
        n += cur.rowcount or 0
    return n


# --- migration ADR 0050 : backfill one-shot des pairs pré-existants -----------

# Connecteurs `default_hidden` AU MOMENT du retrait du flag (ADR 0050 B3) — fait
# historique figé dans la migration : le backfill reconstitue ce que chaque membre
# VOYAIT (l'exposé de son org moins ces masqués), pas l'exposé brut.
_BACKFILL_HIDDEN = frozenset(
    {"attio", "brevoauto", "pennylaneged", "resend", "scaleway", "http", "bridge"})
# Sentinelle du one-shot (jamais un sub réel — les subs Logto sont alphanumériques).
# Posée dans `connector_selection_seeded` après la passe : le backfill ne rejoue
# JAMAIS, car un pair créé APRÈS lui doit recevoir le SOCLE au seed lazy, pas
# l'exposé historique.
_BACKFILL_MARK = "#adr0050-backfill"


def backfill_preexisting(conn) -> None:
    """One-shot ADR 0050 (reçoit le `conn` de la transaction `db.init_db`) : au
    passage au régime nominal « non-sélectionné = masqué », chaque (sub, org) DÉJÀ
    existant et jamais seedé reçoit en sélection `active` ce qu'il VOYAIT (exposé
    de l'org − ex-`default_hidden`) — zéro changement de toolbox pour l'existant.
    Les pairs déjà seedés (régime strict testé en canari) gardent leurs choix."""
    done = conn.execute(
        "SELECT 1 FROM connector_selection_seeded WHERE sub = %s AND org_id = 0",
        (_BACKFILL_MARK,),
    ).fetchone()
    if done:
        return
    from .activation import _resolve

    # Table UNIFIÉE `connector_availability` (chantier ACL, cadrage 10/07) — peuplée
    # AVANT ce backfill par `connector_activation.init_schema` (copie legacy) : cf.
    # l'ordre des appels dans db._init. Ne pas relire la table legacy (tombe en B2).
    rows = conn.execute(
        "SELECT scope_type, scope_id, connector, enabled FROM connector_availability "
        "WHERE scope_type IN ('platform', 'org')").fetchall()
    global_map: dict[str, bool] = {}
    overrides: dict[int, dict[str, bool]] = {}
    for r in rows:
        if r["scope_type"] == "platform":
            global_map[r["connector"]] = bool(r["enabled"])
        else:
            overrides.setdefault(int(r["scope_id"]), {})[r["connector"]] = bool(r["enabled"])
    # Tous les couples (sub, org) susceptibles d'un profil de visibilité : les
    # memberships + la sentinelle perso/globale org_id=0 (ADR 0015) — moins les
    # pairs déjà seedés.
    pairs = conn.execute(
        "SELECT sub, org_id FROM org_members "
        "UNION SELECT sub, 0 FROM users "
        "EXCEPT SELECT sub, org_id FROM connector_selection_seeded").fetchall()
    for p in pairs:
        exposed = _resolve(global_map, overrides.get(p["org_id"], {}))
        for name in sorted(exposed - _BACKFILL_HIDDEN):
            conn.execute(
                "INSERT INTO user_selected_connectors (sub, org_id, connector, state) "
                "VALUES (%s, %s, %s, 'active') "
                "ON CONFLICT (sub, org_id, connector) DO NOTHING",
                (p["sub"], p["org_id"], name),
            )
        conn.execute(
            "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, %s) "
            "ON CONFLICT (sub, org_id) DO NOTHING",
            (p["sub"], p["org_id"]),
        )
    conn.execute(
        "INSERT INTO connector_selection_seeded (sub, org_id) VALUES (%s, 0) "
        "ON CONFLICT (sub, org_id) DO NOTHING",
        (_BACKFILL_MARK,),
    )
