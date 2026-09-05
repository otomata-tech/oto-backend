"""Inventaire MCP : aucune capacité du client lemlist ne reste hors des tools.

Côté oto-core, `test_lemlist_coverage.py` prouve que le CLIENT vise les 141
routes documentées. Ça ne dit rien de l'exposition : une méthode que personne
n'appelle est du code mort du point de vue d'un agent, et le trou ne se voit
nulle part — le connecteur a l'air complet, l'agent ne peut pas s'en servir.

Ce test ferme la seconde moitié de la chaîne : toute méthode du client qui
construit un chemin HTTP doit être appelée par au moins un tool. Les rares
exceptions sont NOMMÉES ci-dessous avec leur raison — jamais tolérées en
silence, ce qui est tout l'intérêt d'une liste écrite plutôt que d'un seuil.

Le second inventaire couvre l'étage en dessous, et il a été ajouté APRÈS coup :
la couverture au grain de la MÉTHODE est structurellement aveugle au trou le
plus courant — un tool qui appelle la bonne méthode en laissant tomber la moitié
de ses paramètres. Deux cas étaient déjà passés (un `settings` avalé à la
création d'une campagne, quatre filtres muets sur les signaux) : la docstring
promettait un filtre, la branche ne le transmettait pas, et l'agent recevait une
liste non filtrée qui avait l'air filtrée. `test_aucun_parametre_client_ne_reste_muet`
compare donc, pour chaque méthode appelée, sa signature à ce que les tools lui
passent réellement.
"""
from __future__ import annotations

import inspect
import re

import pytest

# Tout ici se mesure contre `LemlistClient` : NON CONCLUANT quand le venv
# n'exécute pas le tag épinglé. Marqué au MODULE délibérément — contre un client
# rabougri, les inventaires qui PASSENT sont les plus trompeurs des deux (moins de
# méthodes à couvrir ⟹ couverture triviale). Cf. `tests/_oto_core_pin.py`.
pytestmark = pytest.mark.exige_pin_oto_core


#: Méthodes qui atteignent l'API mais qu'aucun tool n'appelle, et pourquoi.
NON_EXPOSEES = {
    # Ancienne création de lead (email DANS le chemin). lemlist ne la documente
    # plus ; `create_lead` la remplace et c'est elle qu'expose le connecteur.
    "add_lead": "route héritée, remplacée par create_lead",
    # Une seule page de campagnes. Les tools passent par `list_all_campaigns`,
    # qui l'appelle en boucle et rend en plus le drapeau de troncature.
    "list_campaigns": "atteinte via list_all_campaigns",
    # Export CSV historique `/campaigns/{id}/export`, appelé SANS `state` : le
    # défaut de lemlist filtre alors tout et rend un CSV réduit à son en-tête.
    # Ni lui ni son enveloppe `get_all_leads` ne sont plus servis — c'est
    # `export_campaign_leads` (défaut `state="all"`) qui porte le geste depuis le
    # 05/09/2026, signal 719. Les exposer serait servir le piège lui-même.
    "export_leads": "route brute sans `state` — remplacée par export_campaign_leads",
    "get_all_leads": "enveloppe d'export_leads, hérite de son `state` absent",
    # Le geste doux de `delete_lead`, que `lemlist_lead(op="unsubscribe")`
    # obtient en passant `action=None` — un seul chemin d'appel, pas deux.
    "unsubscribe_lead": "alias de delete_lead sans action",
    # Compteurs dérivés d'une page d'activités, plafonnés à 1000 : remplacés par
    # `get_campaign_stats_v2` et gardés pour compatibilité.
    "get_campaign_stats": "déprécié au profit de get_campaign_stats_v2",
    # Composites locaux au-dessus de get_campaign/get_sequences : ils n'ajoutent
    # pas de route, et `lemlist_sequence(op="get")` rend déjà la matière.
    "get_campaign_tree": "composite local (get_campaign + get_sequences)",
    "sync_campaign": "composite local, écrit un fichier côté serveur",
    "save_campaign_tree": "écrit un fichier sur le serveur — jamais exposé",
    "get_sequence_steps": "composite local au-dessus de get_sequences",
}


def _client_source_methods() -> dict[str, str]:
    from oto.tools.lemlist import client as lm
    src = inspect.getsource(lm.LemlistClient)
    # Découpe la classe en méthodes : nom -> corps jusqu'à la prochaine def.
    parts = re.split(r"\n    (?:@\w+\n    )*def ", src)
    out = {}
    for part in parts[1:]:
        name = part.split("(", 1)[0].strip()
        out[name] = part
    return out


def _methods_that_reach_the_api() -> set[str]:
    """Méthodes PUBLIQUES qui construisent un appel HTTP, directement ou en
    déléguant à une voisine qui le fait."""
    methods = _client_source_methods()
    reaching = {n for n, body in methods.items()
                if "self._request(" in body or "requests.get(" in body}
    # Une délégation atteint l'API tout autant, et elle s'empile : `sync_campaign`
    # → `get_campaign_tree` → `get_campaign`. Point fixe, sinon la profondeur 2
    # passe au travers et l'inventaire ment dans le sens rassurant.
    grew = True
    while grew:
        grew = False
        for name, body in methods.items():
            if name in reaching:
                continue
            if any(f"self.{d}(" in body for d in reaching):
                reaching.add(name)
                grew = True
    return {n for n in reaching if not n.startswith("_")}


def _methods_called_by_tools() -> set[str]:
    from oto_mcp.tools import lemlist, lemlist_crm
    called: set[str] = set()
    for module in (lemlist, lemlist_crm):
        src = inspect.getsource(module)
        # Toute MENTION compte, pas seulement un appel direct : les tables de
        # dispatch (`{"add": client.add_unsubscribe, …}[op](email)`) et les
        # affectations (`mark = client.mark_lead_interested if …`) référencent la
        # méthode sans la parenthèse, et les compter autrement ferait crier le
        # garde-fou à tort — un garde-fou qui crie à tort finit ignoré.
        called |= set(re.findall(r"client\.(\w+)", src))
    return called


def test_toute_capacite_du_client_est_atteignable_par_un_tool():
    manquantes = sorted(
        _methods_that_reach_the_api() - _methods_called_by_tools()
        - set(NON_EXPOSEES))
    assert not manquantes, (
        f"{len(manquantes)} méthode(s) du client lemlist qu'aucun tool "
        f"n'appelle : {manquantes}. Expose-les, ou déclare-les dans "
        "NON_EXPOSEES avec la raison.")


def test_la_liste_dexceptions_ne_survit_pas_a_son_objet():
    """Une exception qui ne correspond plus à rien fait mentir la liste."""
    inconnues = sorted(set(NON_EXPOSEES) - _methods_that_reach_the_api())
    assert not inconnues, (
        f"exception(s) sur une méthode qui n'atteint plus l'API : {inconnues}")


def test_les_tools_nappellent_que_des_methodes_qui_existent():
    """Garde version-skew au grain de la méthode, sur les DEUX modules."""
    from oto.tools.lemlist import LemlistClient
    fantomes = sorted(
        m for m in _methods_called_by_tools()
        if not hasattr(LemlistClient, m))
    assert not fantomes, f"méthodes appelées mais absentes du client : {fantomes}"


@pytest.mark.parametrize("tool", [
    "lemlist_campaign_start", "lemlist_launch_lead", "lemlist_inbox_send",
    "lemlist_campaign_auto_review",
])
def test_tout_ce_qui_arme_ou_declenche_un_envoi_est_masque_par_defaut(tool):
    """Le contrat de sûreté du connecteur, énuméré plutôt que raconté."""
    from oto_mcp.tool_visibility import DEFAULT_HIDDEN_TOOLS
    assert tool in DEFAULT_HIDDEN_TOOLS


# --------------------------------------------------------------------------- #
# Étage du PARAMÈTRE : une méthode atteinte ne suffit pas
# --------------------------------------------------------------------------- #

#: Paramètres client qu'aucun tool ne transmet, DÉLIBÉRÉMENT.
PARAMETRES_NON_TRANSMIS = {
    ("create_campaign", "auto_review"): "armé par lemlist_campaign_auto_review",
    ("create_campaign", "auto_review_conditions"):
        "armé par lemlist_campaign_auto_review",
    # `version` bascule entre deux formes de charge de l'API. C'est un détail de
    # transport, pas une question d'agent : le client envoie déjà la bonne.
    ("get_lead", "version"): "commutateur de version d'API, choisi par le client",
    ("get_lead_by_email", "version"): "idem",
    ("get_team", "version"): "idem",
    # `GET /leads` accepte id OU email ; le tool sert l'email par la route dédiée
    # `GET /leads/{email}`, qui est celle que lemlist documente pour ce cas.
    ("get_lead", "email"): "servi par get_lead_by_email (route dédiée)",
}


def _client_calls() -> dict[str, list[tuple[int, set[str], bool]]]:
    """Par méthode client appelée : `(nb d'args positionnels, kwargs nommés,
    présence d'un `**dict`)` pour chaque site d'appel."""
    import ast

    from oto_mcp.tools import lemlist, lemlist_crm

    calls: dict[str, list] = {}
    for module in (lemlist, lemlist_crm):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            base = node.func.value
            if not (isinstance(base, ast.Name) and base.id == "client"):
                continue
            named = {kw.arg for kw in node.keywords if kw.arg}
            # `client.f(name, **kwargs)` transmet tout ce que le dict porte :
            # le compter comme un manque ferait crier le garde-fou à tort.
            splat = any(kw.arg is None for kw in node.keywords)
            calls.setdefault(node.func.attr, []).append(
                (len(node.args), named, splat))
    return calls


def test_aucun_parametre_client_ne_reste_muet():
    """Une docstring qui promet un filtre que la branche ne passe pas rend une
    liste non filtrée qui a l'air filtrée — le pire des deux mondes."""
    from oto.tools.lemlist import LemlistClient

    muets = []
    for name, sites in sorted(_client_calls().items()):
        method = getattr(LemlistClient, name, None)
        if method is None:
            continue
        try:
            params = [p for p in inspect.signature(method).parameters.values()
                      if p.name != "self"]
        except (TypeError, ValueError):
            continue
        if any(splat for _, _, splat in sites):
            continue
        couverts: set[str] = set()
        for npos, named, _ in sites:
            couverts |= {p.name for p in params[:npos]} | named
        for p in params:
            if p.kind is p.VAR_KEYWORD or p.name in couverts:
                continue
            if (name, p.name) in PARAMETRES_NON_TRANSMIS:
                continue
            muets.append(f"{name}.{p.name}")
    assert not muets, (
        f"{len(muets)} paramètre(s) qu'aucun tool ne transmet : {muets}. "
        "Transmets-les, ou déclare-les dans PARAMETRES_NON_TRANSMIS avec la "
        "raison — une charge silencieusement amputée est indétectable à l'usage.")


def test_les_exceptions_de_parametres_visent_des_parametres_reels():
    from oto.tools.lemlist import LemlistClient

    perimes = []
    for (meth, param) in PARAMETRES_NON_TRANSMIS:
        fn = getattr(LemlistClient, meth, None)
        if fn is None or param not in inspect.signature(fn).parameters:
            perimes.append(f"{meth}.{param}")
    assert not perimes, f"exception(s) sur un paramètre inexistant : {perimes}"


# --------------------------------------------------------------------------- #
# Étage du DÉFAUT : transmettre un paramètre ne suffit pas non plus
# --------------------------------------------------------------------------- #
#
# Troisième variante de la même famille, et la plus discrète : le paramètre est
# bien transmis — mais à `None`, ce qui ÉCRASE le défaut du client. Or plusieurs
# de ces défauts ne sont pas du confort : ils compensent des exigences de l'API
# relevées en live (`segmentType`/`signalProcessingType`/`activate` obligatoires
# sur une watch list, `state="all"` sans quoi un export rend une liste vide qui
# se lit « pas de leads »). Un `None` explicite rétablit donc exactement le bug
# que le défaut évite.
#
# L'inventaire de PARAMÈTRES ci-dessus est structurellement aveugle à ça : il
# prouve qu'un paramètre est transmis, pas que le transmettre à None est sûr.
# Convention qui en découle, et que ce test tient : **un tool construit ses
# kwargs et retire les None** plutôt que de passer None à un client qui a un
# vrai défaut.


def test_aucun_tool_n_ecrase_un_defaut_client_par_None():
    import ast

    from oto.tools.lemlist import LemlistClient
    from oto_mcp.tools import lemlist, lemlist_crm

    # Paramètres client dont le défaut porte une VALEUR (ni None ni sentinelle).
    porteurs: dict[str, set[str]] = {}
    for name in dir(LemlistClient):
        if name.startswith("_"):
            continue
        fn = getattr(LemlistClient, name)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        vals = {p.name for p in sig.parameters.values()
                if p.default is not p.empty and p.default is not None}
        if vals:
            porteurs[name] = vals

    fautes = []
    for module in (lemlist, lemlist_crm):
        tree = ast.parse(inspect.getsource(module))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            # Les arguments de CE tool qui valent None par défaut.
            nullables = set()
            args, defaults = fn.args.args, fn.args.defaults
            for arg, default in zip(args[len(args) - len(defaults):], defaults):
                if isinstance(default, ast.Constant) and default.value is None:
                    nullables.add(arg.arg)
            for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
                if isinstance(default, ast.Constant) and default.value is None:
                    nullables.add(arg.arg)
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)):
                    continue
                base = node.func.value
                if not (isinstance(base, ast.Name) and base.id == "client"):
                    continue
                for kw in node.keywords:
                    if (kw.arg in porteurs.get(node.func.attr, set())
                            and isinstance(kw.value, ast.Name)
                            and kw.value.id in nullables):
                        fautes.append(
                            f"{fn.name} → client.{node.func.attr}"
                            f"({kw.arg}={kw.value.id})")

    assert not fautes, (
        f"{len(fautes)} passage(s) d'un None explicite sur un paramètre à défaut "
        f"signifiant : {sorted(set(fautes))}. Construis les kwargs et retire les "
        "None — sinon le défaut du client ne sert à rien.")
