"""Les RÔLES d'une app (`docs/apps.md` § Rôles d'une app) : qui, parmi ses lecteurs, voit
et fait quoi.

Le manifeste déclare ce que l'app PEUT toucher (tableaux, champs, valeurs posables,
colonnes ajoutables, agents, fichiers). Il peut en plus déclarer des **rôles** —
`roles: {admin: {…}, user: {…}}` et `default_role` — dont chacun RESSERRE ce
manifeste pour qui le tient, jamais plus :

- `tables` : absent = tous les tableaux du manifeste ; présent = ceux-là SEULS, chacun
  `true` (tel que déclaré) ou `{fields?, set?, add?}`, dont chaque clé ABSENTE hérite du
  manifeste et chaque clé PRÉSENTE en est un sous-ensemble (`set: {}` et `add: []` :
  aucune écriture) ;
- `agents` : absent = ceux du manifeste ; présent = une liste de noms, ou
  `{nom: true | {run?, input?}}` (`run: false` : lire ses runs sans le lancer) ;
- `files` : absent = comme le manifeste ; `false` = ni dépôt ni liste de fichiers ;
- `rows` : un PÉRIMÈTRE DE LIGNES, `{tableau: {champ: "$email" | "$share"}}` — ce
  lecteur ne voit, n'écrit et n'ajoute que les lignes dont le champ porte son adresse
  vérifiée, ou la valeur posée sur son partage (`resource_grants.app_scope`, qui borne
  aussi seul) ; non résolu = aucune ligne (`lignes_du_lecteur`, `docs/apps.md`
  § Périmètre de lignes) ;
- `sections` : des noms libres. Le HTML marque ses parties (`data-oto-section="nom"`) ;
  servi à qui tient le rôle (`op=get`), il perd au serveur tout élément marqué d'une
  section que le rôle n'ouvre pas (`apps_sections.py`), et l'app lit les siennes par
  `oto.access()`. ⚠️ Seul le balisage MARQUÉ est retiré : un script non marqué part
  entier. Les données des tableaux, elles, sont protégées par le manifeste resserré.

**Où le rôle s'applique** : une seule fois, à l'entrée de l'appel d'une app ouverte
(`apps_appel._app_pour`) — le manifeste servi à TOUTE action (lectures, lot, agrégats,
gestes, consentement, fichiers, runs) est déjà le manifeste resserré, rien d'autre
n'a à le savoir. `op=get` sert ce même manifeste resserré, sans la définition des
rôles.

**Qui tient quel rôle** : le rôle d'app d'un partage (`resource_grants.app_role`, posé
par qui gouverne l'app, `oto_apps op=set_role`) ; le partage nominatif d'abord, puis
d'équipe, puis d'org — à type égal, le premier rôle dans l'ordre du manifeste. Sans
rôle posé (ou un rôle que le manifeste ne déclare plus) : `default_role`, OBLIGATOIRE
dès qu'un rôle est déclaré — jamais un repli sur le manifeste entier. **Qui peut
modifier l'app** (propriétaire, Edit, Manage) n'est borné par aucun rôle : il réécrirait
le manifeste. Un éditeur VOIT l'app comme un rôle par `as_role` sur l'appel.

Le rôle ne remplace ni View/Use (cliquer reste Use), ni la règle auteur ∩ lecteur :
il ne fait que retirer.
"""
from __future__ import annotations

from typing import Any, Optional

MAX_ROLES = 20
MAX_SECTIONS = 50
MAX_LABEL = 80
# Ce qu'un rôle met dans `rows` : l'adresse VÉRIFIÉE du lecteur, ou la valeur posée sur
# son partage (`oto_apps op=set_scope`).
EMAIL, PARTAGE = "$email", "$share"
VALEURS_DE_LIGNES = frozenset({EMAIL, PARTAGE})
# Les types de colonne qu'un périmètre de lignes peut viser : une égalité de texte y
# veut dire ce qu'elle dit. Une date compare des périodes, un composite ne se compare
# pas, un JSON ou une formule n'ont pas de valeur à « tenir ». Sans type = texte.
TYPES_DE_PERIMETRE = frozenset({None, "text", "number", "url", "email", "phone", "ref",
                                "enum", "bool"})


def _noms(v: Any) -> Optional[list[str]]:
    if not isinstance(v, list) or any(not isinstance(x, str) or not x for x in v):
        return None
    return v


def _table_du_role(where: str, spec: Any, t: dict, problems: list[str]) -> Optional[dict]:
    """La spécification d'un tableau dans un rôle, normalisée (clés présentes seules)."""
    if spec is True:
        return {}
    if not isinstance(spec, dict):
        problems.append(f"{where} must be true or an object with fields / set / add.")
        return None
    out: dict = {}
    if "fields" in spec:
        f = _noms(spec["fields"])
        if f is None:
            problems.append(f"{where}.fields must be a list of names.")
        else:
            if t.get("fields") is not None:
                trop = sorted(set(f) - set(t["fields"]))
                if trop:
                    problems.append(f"{where}.fields: not declared by the app: {', '.join(trop)}.")
            out["fields"] = f
    if "set" in spec:
        if not isinstance(spec["set"], dict):
            problems.append(f"{where}.set must map a field to its allowed values.")
        else:
            sets: dict[str, list[str]] = {}
            for champ, vals in spec["set"].items():
                ok = _noms(vals)
                permis = (t.get("set") or {}).get(champ)
                if ok is None:
                    problems.append(f"{where}.set.{champ} must be a list of values.")
                elif permis is None:
                    problems.append(f"{where}.set.{champ}: the app can't set this field.")
                elif set(ok) - set(permis):
                    problems.append(f"{where}.set.{champ}: values the app can't set: "
                                    f"{', '.join(sorted(set(ok) - set(permis)))}.")
                else:
                    sets[champ] = ok
            out["set"] = sets
    if "add" in spec:
        a = _noms(spec["add"]) if spec["add"] else []
        if a is None:
            problems.append(f"{where}.add must be a list of names.")
        else:
            trop = sorted(set(a) - set(t.get("add") or []))
            if trop:
                problems.append(f"{where}.add: columns the app can't fill: {', '.join(trop)}.")
            out["add"] = a
    inconnues = sorted(set(spec) - {"fields", "set", "add"})
    if inconnues:
        problems.append(f"{where}: unknown keys: {', '.join(inconnues)}.")
    return out


def _lignes_du_role(where: str, raw: Any, tables_du_role: Any, clean: dict, role: dict,
                    problems: list[str]) -> Optional[dict]:
    """`roles.<r>.rows` normalisé : `{tableau: {champ: "$email" | "$share"}}` — le
    PÉRIMÈTRE DE LIGNES du rôle (`docs/apps.md` § Périmètre de lignes). Le tableau doit
    être ouvert par le rôle, le champ ne pas être posable par lui (sinon le lecteur
    déplacerait une ligne hors de son périmètre — vers celui du voisin). L'existence et
    le type du champ se jugent contre le schéma (`apps_manifeste.auteur`)."""
    if not isinstance(raw, dict):
        problems.append(f"{where}.rows must map a table name to {{field: \"$email\" | "
                        "\"$share\"}}.")
        return None
    out: dict[str, dict] = {}
    for alias, spec in raw.items():
        w = f"{where}.rows.{alias}"
        t = clean["tables"].get(alias)
        if t is None:
            problems.append(f"{w}: the app declares no such table.")
            continue
        if isinstance(tables_du_role, dict) and alias not in tables_du_role:
            problems.append(f"{w}: this role doesn't open this table.")
            continue
        if not isinstance(spec, dict) or not spec:
            problems.append(f"{w} must map a field to \"$email\" or \"$share\".")
            continue
        # Ce que le rôle peut POSER sur ce tableau : son `set`, sinon celui du manifeste.
        sets = ((role.get("tables") or {}).get(alias) or {}).get("set")
        sets = (t.get("set") or {}) if sets is None else sets
        champs: dict[str, str] = {}
        for champ, v in spec.items():
            if not isinstance(champ, str) or not champ or champ.startswith("_") or "." in champ:
                problems.append(f"{w}: `{champ}` isn't a column name.")
            elif v not in VALEURS_DE_LIGNES:
                problems.append(f"{w}.{champ} must be \"$email\" (the viewer's verified "
                                "email) or \"$share\" (the value set on their share).")
            elif champ in sets:
                problems.append(f"{w}.{champ}: this role may set this field, so a viewer "
                                "could move a row out of their scope.")
            else:
                champs[champ] = v
        out[alias] = champs
    return out


def forme(raw: dict, clean: dict, alias_ok) -> list[str]:
    """Normalise `raw.roles` / `raw.default_role` dans `clean` (déjà normalisé pour ses
    tableaux et agents) et rend ce qui ne va pas. Sans rôles : rien n'est posé."""
    problems: list[str] = []
    raw_roles = raw.get("roles")
    if raw_roles is None or raw_roles == {}:
        if raw.get("default_role") is not None:
            problems.append("`default_role` needs `roles`.")
        return problems
    if not isinstance(raw_roles, dict):
        return ["`roles` must map a role name to what it opens."]
    if len(raw_roles) > MAX_ROLES:
        problems.append(f"At most {MAX_ROLES} roles.")
    roles: dict[str, dict] = {}
    for key, r in raw_roles.items():
        where = f"roles.{key}"
        if not alias_ok(key):
            problems.append(f"`{key}` isn't a usable role name.")
            continue
        if not isinstance(r, dict):
            problems.append(f"{where} must be an object.")
            continue
        role: dict = {}
        label = r.get("label")
        role["label"] = (label.strip()[:MAX_LABEL] if isinstance(label, str) and label.strip()
                         else key)
        if "tables" in r:
            if not isinstance(r["tables"], dict):
                problems.append(f"{where}.tables must map a table name to true or an object.")
            else:
                tables: dict[str, dict] = {}
                for alias, spec in r["tables"].items():
                    t = clean["tables"].get(alias)
                    if t is None:
                        problems.append(f"{where}.tables.{alias}: the app declares no such table.")
                        continue
                    n = _table_du_role(f"{where}.tables.{alias}", spec, t, problems)
                    if n is not None:
                        tables[alias] = n
                role["tables"] = tables
        if "agents" in r:
            ra = r["agents"]
            if isinstance(ra, list):
                ra = {a: True for a in ra if isinstance(a, str)} if _noms(ra) is not None else None
            if not isinstance(ra, dict):
                problems.append(f"{where}.agents must be a list of agent names or an object.")
            else:
                agents: dict[str, dict] = {}
                for alias, spec in ra.items():
                    a = clean["agents"].get(alias)
                    if a is None:
                        problems.append(f"{where}.agents.{alias}: the app declares no such agent.")
                        continue
                    if spec is True:
                        agents[alias] = {}
                    elif isinstance(spec, dict):
                        n = {}
                        for k in ("run", "input"):
                            if k in spec:
                                if not isinstance(spec[k], bool):
                                    problems.append(f"{where}.agents.{alias}.{k} must be true or false.")
                                elif spec[k] and not a.get(k):
                                    problems.append(f"{where}.agents.{alias}.{k}: the app doesn't "
                                                    f"allow it for this agent.")
                                else:
                                    n[k] = spec[k]
                        agents[alias] = n
                    else:
                        problems.append(f"{where}.agents.{alias} must be true or an object.")
                role["agents"] = agents
        if "rows" in r:
            lignes = _lignes_du_role(where, r["rows"], r.get("tables"), clean, role, problems)
            if lignes is not None:
                role["rows"] = lignes
        if "files" in r:
            if not isinstance(r["files"], bool):
                problems.append(f"{where}.files must be true or false.")
            elif r["files"] and not clean.get("files"):
                problems.append(f"{where}.files: the app takes no files.")
            else:
                role["files"] = r["files"]
        if "sections" in r:
            s = _noms(r["sections"])
            if s is None or len(s) > MAX_SECTIONS or any(len(x) > 64 for x in s):
                problems.append(f"{where}.sections must be a list of at most {MAX_SECTIONS} "
                                "short names.")
            else:
                if "*" in s:
                    # `*` = le COMMUN (`apps_sections.COMMUN`), servi à tous : un rôle ne
                    # le déclare pas, il l'a toujours.
                    problems.append(f"{where}.sections: `*` is reserved for markup every "
                                    "role gets.")
                role["sections"] = s
        inconnues = sorted(set(r) - {"label", "tables", "agents", "files", "sections", "rows"})
        if inconnues:
            problems.append(f"{where}: unknown keys: {', '.join(inconnues)}.")
        roles[key] = role
    defaut = raw.get("default_role")
    if not isinstance(defaut, str) or defaut not in raw_roles:
        problems.append("`default_role` must name one of `roles`: it is what a person "
                        "without a role of their own gets.")
    clean["roles"] = roles
    # L'ordre des rôles tel qu'ÉCRIT : JSONB ne garde pas l'ordre des clés d'un objet,
    # et c'est lui qui départage deux partages de même type (`role_du_lecteur`).
    clean["role_order"] = list(roles)
    clean["default_role"] = defaut if isinstance(defaut, str) else None
    return problems


def ordre(manifest: dict) -> list[str]:
    """Les rôles déclarés, dans l'ordre où le manifeste les a écrits (`role_order`)."""
    roles = manifest.get("roles") or {}
    ecrits = [k for k in (manifest.get("role_order") or []) if k in roles]
    return ecrits + [k for k in roles if k not in ecrits]


def champs_nommes(clean: dict) -> dict[str, set[str]]:
    """Par tableau, les champs que les rôles nomment — à juger contre le schéma quand le
    manifeste n'a pas de liste de champs (`apps_manifeste.auteur`)."""
    out: dict[str, set[str]] = {}
    for r in (clean.get("roles") or {}).values():
        for alias, spec in (r.get("tables") or {}).items():
            out.setdefault(alias, set()).update(spec.get("fields") or [])
        for alias, champs in (r.get("rows") or {}).items():
            out.setdefault(alias, set()).update(champs)
    return out


def champs_de_perimetre(clean: dict) -> dict[str, set[str]]:
    """Par tableau, les champs qu'un périmètre de lignes de rôle vise — à juger contre le
    schéma (présents ET d'un type comparable, `TYPES_DE_PERIMETRE`), même sur un tableau
    dont le manifeste ne liste pas les champs (`apps_manifeste.auteur`)."""
    out: dict[str, set[str]] = {}
    for r in (clean.get("roles") or {}).values():
        for alias, champs in (r.get("rows") or {}).items():
            out.setdefault(alias, set()).update(champs)
    return out


def resserrer(manifest: dict, role: Optional[str]) -> dict:
    """Le manifeste vu par qui tient `role` (None = le manifeste entier), SANS la
    définition des rôles. Ne fait que retirer."""
    m = {k: v for k, v in manifest.items() if k not in ("roles", "role_order", "default_role")}
    r = (manifest.get("roles") or {}).get(role) if role else None
    if r is None:
        return m
    tables = manifest.get("tables") or {}
    if "tables" in r:
        tables = {a: {**tables[a], **spec} for a, spec in r["tables"].items() if a in tables}
    m["tables"] = tables
    agents = manifest.get("agents") or {}
    if "agents" in r:
        out = {}
        for a, spec in r["agents"].items():
            if a not in agents:
                continue
            run = bool(agents[a].get("run")) and spec.get("run", True)
            # Passer un texte n'existe qu'en lançant : sans `run`, pas d'`input`.
            out[a] = {**agents[a], "run": run,
                      "input": run and bool(agents[a].get("input")) and spec.get("input", True)}
        agents = out
    m["agents"] = agents
    m["files"] = bool(manifest.get("files")) and r.get("files", True)
    return m


def choix_du_lecteur(sub: str, app: dict) -> dict:
    """Ce qui borne `sub` dans `app` (manifeste servi) : `{editeur, role, portees}`.

    - `editeur` : `sub` peut modifier l'app — ni rôle ni périmètre ne le bornent ;
    - `role` : le rôle d'app de son partage — nominatif, puis d'équipe, puis d'org ; à
      type égal le premier dans l'ordre du manifeste —, sinon `default_role` ; None si
      l'app ne déclare aucun rôle ;
    - `portees` : le périmètre de lignes (`app_scope`) de chaque partage RETENU — ceux
      qui portent le rôle choisi au type où il a été trouvé ; au rôle par défaut (ou
      sans rôles), les partages du type le plus précis qu'il tient. Plusieurs partages
      retenus (deux équipes, même rôle) : leurs périmètres s'appliquent TOUS (ET) —
      jamais l'un à la place de l'autre (`docs/apps.md` § Périmètre de lignes)."""
    from .. import db, ownership
    from .apps import APP_RTYPE

    manifest = app.get("manifest") or {}
    roles = manifest.get("roles") or {}
    rid = str(app["id"])
    partages = db.apps.app_shares_of(rid, list(ownership.accessor_scope(sub).principal_pairs()))
    if not roles and not any(g.get("app_scope") for g in partages):
        # Ni rôle ni périmètre : rien à borner, et rien à demander de plus (la question
        # « peut-il la modifier ? » est déjà posée par l'appel, `peut_cliquer`).
        return {"editeur": False, "role": None, "portees": []}
    if ownership.can_access(sub, APP_RTYPE, rid, "write"):
        return {"editeur": True, "role": None, "portees": []}
    rang = ordre(manifest)
    types = ("user", "group", "org")
    if roles:
        for ptype in types:
            tenus = [g for g in partages if g["principal_type"] == ptype
                     and g["app_role"] in roles]
            if tenus:
                role = min((g["app_role"] for g in tenus), key=rang.index)
                return {"editeur": False, "role": role,
                        "portees": [g.get("app_scope") or {} for g in tenus
                                    if g["app_role"] == role]}
    role = manifest.get("default_role") if roles else None
    for ptype in types:
        du_type = [g for g in partages if g["principal_type"] == ptype]
        if du_type:
            return {"editeur": False, "role": role,
                    "portees": [g.get("app_scope") or {} for g in du_type]}
    return {"editeur": False, "role": role, "portees": []}


def role_du_lecteur(sub: str, app: dict) -> Optional[str]:
    """Le rôle qui borne `sub` dans `app` (manifeste servi), None = aucun : l'app ne
    déclare pas de rôle, ou `sub` peut la modifier."""
    if not ((app.get("manifest") or {}).get("roles") or {}):
        return None
    return choix_du_lecteur(sub, app)["role"]


#: Un tableau dont le périmètre ne se résout pas (`$share` sans valeur posée, `$email`
#: sans adresse vérifiée) : AUCUNE ligne, aucun geste.
RIEN = None


def lignes_du_lecteur(manifest: dict, role: Optional[str], portees: list[dict],
                      email) -> dict:
    """Le périmètre de LIGNES de chaque tableau du manifeste RESSERRÉ (`resserrer`) :
    `{numéro: {champ: {"in": [valeurs admises], "pose": valeur}} | RIEN}` ; un tableau
    absent n'a aucun périmètre. `manifest` est le manifeste ENTIER (ses rôles), `email`
    une fonction sans argument qui rend l'adresse vérifiée du lecteur, ou None — appelée
    seulement si un rôle la demande.

    Chaque contrainte RESTREINT (ET) : la valeur que le rôle demande (`$email`,
    `$share`) et chaque valeur posée sur un partage retenu. Deux valeurs différentes pour
    un même champ = aucune ligne, jamais l'une ou l'autre."""
    r = (manifest.get("roles") or {}).get(role) if role else None
    rows = (r or {}).get("rows") or {}
    declares = manifest.get("tables") or {}
    if r is not None and "tables" in r:
        declares = {a: declares[a] for a in r["tables"] if a in declares}
    out: dict = {}
    for alias, t in declares.items():
        if not isinstance(t, dict) or not str(t.get("table") or "").isdigit():
            continue
        ns = str(t["table"])
        champs: dict[str, dict] = {}
        rien = False

        def restreindre(champ: str, admises: list, pose) -> None:
            nonlocal rien
            if champ in champs:
                garde = [v for v in champs[champ]["in"] if v in admises]
                if not garde:
                    rien = True
                    return
                champs[champ]["in"] = garde
            else:
                champs[champ] = {"in": list(admises), "pose": pose}

        for champ, v in (rows.get(alias) or {}).items():
            if v == EMAIL:
                e = email()
                if not e:
                    rien = True
                    continue
                # L'égalité est exacte : l'adresse telle que l'annuaire la tient, et en
                # minuscules (la forme qu'un tableau écrit le plus souvent).
                restreindre(champ, list(dict.fromkeys([e, e.lower()])), e)
            else:  # PARTAGE
                vals = [p.get(ns, {}).get(champ) for p in portees]
                if not vals or any(not _valeur_de_perimetre(x) for x in vals):
                    rien = True
                    continue
                for x in vals:
                    restreindre(champ, [x], x)
        for p in portees:
            for champ, x in (p.get(ns) or {}).items():
                if not _valeur_de_perimetre(x):
                    rien = True
                    continue
                restreindre(champ, [x], x)
        if rien:
            out[ns] = RIEN
        elif champs:
            out[ns] = champs
    return out


def _valeur_de_perimetre(v: Any) -> bool:
    """Une valeur qu'un périmètre peut tenir : un scalaire non vide. Jamais `""` ni
    `null` : une égalité sur le vide verrait les lignes que personne ne tient."""
    if isinstance(v, bool):
        return True
    if isinstance(v, (int, float)):
        return True
    return isinstance(v, str) and bool(v.strip())


def clauses(perimetre: dict) -> list[dict]:
    """Le périmètre d'UN tableau en clauses du filtre riche du store (`filters`, ET avec
    le filtre de l'app et la recherche `q`). `RIEN` : une clause qu'aucune ligne ne
    remplit (`_id` n'est jamais vide)."""
    if perimetre is RIEN:
        return [{"field": "_id", "op": "eq", "value": ""}]
    return [{"field": c, "op": "in", "value": spec["in"]} for c, spec in perimetre.items()]


def acces(manifest: dict, role: Optional[str]) -> dict:
    """Ce que l'app LIT de son lecteur (`oto.access()`) : son rôle et ses sections.
    `role` None = tout (aucune section n'est retirée : `sections` None)."""
    r = (manifest.get("roles") or {}).get(role) if role else None
    if r is None:
        return {"role": None, "role_label": None, "sections": None}
    return {"role": role, "role_label": r.get("label") or role, "sections": r.get("sections")}
