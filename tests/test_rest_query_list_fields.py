"""Un champ LISTE servi en `GET`/`DELETE` doit se dire dans une URL (#367).

Une adresse web ne sait pas transporter une liste : l'adaptateur REST verse une valeur
UNIQUE telle quelle (**une chaîne**), et pydantic coerce `str`→`int`/`bool` mais
**jamais** `str`→`list`. Un `Input` déclarant `list[...]` sur une
capacité liée en `GET` répond donc `400 invalid_input` — un refus qui ne nomme même pas
le champ.

**Ce n'est pas le défaut d'un endroit, c'est un défaut de FORME.** `?include=procedures`
sur `me.project_read` était déclaré, testé, documenté — et inatteignable pendant quinze
jours (livré le 13/08 par `c46d81e`, réparé le 28/08 par `22b7dc9`). Les tests d'alors
vérifiaient que le champ était DÉCLARÉ ; personne n'avait tapé l'URL. Corriger au cas par
cas laisse la forme revenir à la prochaine capacité — d'où une garde au SEAM, qui balaye
le registre entier et exerce le **vrai handler REST**, adaptateur compris.

**Une valeur d'exemple est EXIGÉE par champ** (`EXEMPLES` ci-dessous), et c'est le cœur
du dispositif. Un banc qui fabrique `a,b` tout seul se ferait refuser par la validation
métier (`kinds` n'accepte que des kinds connus) et rendrait un rouge qui n'a rien à voir
avec la forme : on ne saurait plus si l'URL est refusée parce qu'elle porte une chaîne ou
parce que la valeur est inconnue. Exiger l'exemple force aussi l'auteur d'un futur champ
liste à **taper l'URL une fois** — c'est exactement l'étape qui a manqué pendant quinze
jours. Un champ sans exemple fait rouge : le banc ne s'abstient jamais en silence.

Le patron de la maison quand la garde mord : déclarer `Optional[list[str] | str]` (la
forme RÉELLE de l'entrée, pas une facilité) et normaliser une fois, au bord — ou poser un
`field_validator(mode="before")` qui découpe la chaîne. Séparateur = **la virgule** pour
une valeur unique ; la forme répétée `?k=a&k=b` arrive en liste depuis #418 (29/08 —
avant, l'adaptateur ne gardait que la DERNIÈRE valeur ; garde :
`test_rest_query_repeated_param.py`). Ce banc-ci couvre la valeur UNIQUE.
"""
from __future__ import annotations

import json as _json
import typing

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from oto_mcp.capabilities import _authz, _rest_adapter
from oto_mcp.capabilities.registry import CAPABILITIES

# Une valeur RÉELLE par champ liste, telle qu'un intégrateur l'écrirait dans l'URL —
# validation métier comprise. `(clé de capacité, champ) -> query string du champ`.
EXEMPLES: dict[tuple[str, str], str] = {
    ("me.project_read", "include"): "spine,procedures",
    ("me.search", "kinds"): "page,procedure",
    ("me.node.rows", "filter"): "statut:actif",
    # oto#140 — les deux versions d'une case, demandées dans le MÊME appel : c'est
    # exactement l'URL que tape l'écran des écarts, et deux appels ne seraient pas
    # atomiques. Valeur métier, pas un `a,b` de façade.
    ("me.datastore.list_rows", "versions"): "current,origine",
    ("me.datastore.get_row", "versions"): "current,origine",
    # oto-backend#980 lot 2 — une colonne nue et une colonne suffixée d'une couche,
    # pour exercer la reconnaissance du suffixe (`champ.origine`) dans la projection
    # en amont de `flat_layers`, pas juste une liste de noms de colonnes plates.
    ("me.datastore.list_rows", "fields"): "email,email.origine",
    # Les crédits de deux runs d'un agent hébergé en une lecture — des ids tels que
    # `guide_run.new_run_id` les frappe (uuid4 hex).
    ("org.usage.calls", "run_id"): "3f2b9c0e8a7d4e6b9c1f2a3b4c5d6e7f,9a8b7c6d5e4f40312a1b0c9d8e7f6a5b",
    # Les orgs dont un tenant lit le premier appel (horloge d'essai) — des ids d'org.
    ("platform.usage.first_calls", "org_ids"): "12,34",
}

# Le reste d'un appel MINIMAL, quand aucun champ n'est requis SEUL mais qu'un parmi
# plusieurs l'est (validation `model_validator`) : le banc ne peut pas le deviner du
# type. `(clé de capacité) -> query string`. Sans cette ligne, la capacité ferait
# rouger le contrôle « appel minimal » pour une raison qui n'a rien à voir avec la
# forme des listes.
MINIMAUX: dict[str, dict[str, str]] = {
    # `tool` OU `run_id` : le relevé ne se lit jamais sur tout le journal.
    "org.usage.calls": {"tool": "linkedin_aiark_search"},
    # `org_ids` est requis ET liste : `_valeur_plausible` ne fabrique pas de liste.
    "platform.usage.first_calls": {"org_ids": "12"},
}


def _est_liste(annotation) -> bool:
    """`list[...]` quelque part dans l'annotation, `Optional`/`Union` traversés."""
    if annotation is None:
        return False
    if typing.get_origin(annotation) is list:
        return True
    return any(_est_liste(a) for a in typing.get_args(annotation))


def _champs_liste_en_url() -> list[tuple]:
    """(capacité, binding, champ) pour tout champ LISTE atteignable par une query
    string seule — soit les bindings `GET`/`DELETE` qui ne lisent pas de corps."""
    trouves = []
    for cap in CAPABILITIES:
        for b in cap.rest_bindings():
            if b.verb not in ("GET", "DELETE") or b.reads_body:
                continue
            for nom, f in cap.Input.model_fields.items():
                if _est_liste(f.annotation):
                    trouves.append((cap, b, nom))
    return trouves


def _valeur_plausible(annotation):
    """Une valeur de query string acceptable pour un champ REQUIS, dérivée de son type.

    Volontairement PAUVRE : un type qu'on ne sait pas fabriquer fait échouer le banc
    avec un message qui demande de l'étendre, plutôt que de rendre un vert obtenu en
    sautant la capacité. Un garde-fou qui s'abstient en silence ne garde rien."""
    origine = typing.get_origin(annotation)
    if origine is typing.Literal:
        return str(typing.get_args(annotation)[0])
    if origine is not None:                       # Optional/Union : le 1er type nu
        for a in typing.get_args(annotation):
            if a is not type(None):
                return _valeur_plausible(a)
    if annotation in (int, float):
        return "1"
    if annotation is str:
        return "x"
    if annotation is bool:
        return "true"
    return None


def _requete(cap, binding, extra: dict[str, str]):
    """Query string + params de chemin d'un appel MINIMAL à cette capacité."""
    query, path_params, chemin = {**MINIMAUX.get(cap.key, {}), **extra}, {}, binding.path
    for morceau in binding.path.split("/"):
        if not (morceau.startswith("{") and morceau.endswith("}")):
            continue
        ph = morceau[1:-1].split(":")[0]
        champ = (binding.path_map or {}).get(ph, ph)
        f = cap.Input.model_fields.get(champ)
        path_params[ph] = _valeur_plausible(f.annotation) if f else "1"
        chemin = chemin.replace(morceau, str(path_params[ph]))
    portes_par_le_chemin = {(binding.path_map or {}).get(p, p) for p in path_params}
    for nom, f in cap.Input.model_fields.items():
        if f.is_required() and nom not in query and nom not in portes_par_le_chemin:
            v = _valeur_plausible(f.annotation)
            assert v is not None, (
                f"le banc ne sait pas fabriquer une valeur pour `{cap.key}.{nom}` "
                f"({f.annotation}) — étends `_valeur_plausible`, ne saute pas la "
                "capacité : une capacité sautée est une garde absente.")
            query[nom] = v
    return chemin, query, path_params


async def _appeler(cap, binding, extra: dict[str, str]) -> tuple[int, dict]:
    """Exerce le VRAI handler REST — l'adaptateur est précisément ce qui est en cause.

    Rend `(0, {})` dès que le contrôle sort du parsing (autz ou métier qui lève) : ce
    qu'on mesure ici, c'est uniquement « l'entrée a-t-elle été acceptée »."""
    chemin, query, path_params = _requete(cap, binding, extra)
    qs = "&".join(f"{k}={v}" for k, v in query.items()).encode()

    def _json_error(_req, status, code, message=None):
        return JSONResponse({"error": code, "detail": message}, status_code=status)

    def _json_response(_req, payload, status=200):
        return JSONResponse({"ok": True}, status_code=status)

    async def _auth(_req, _verifier, **_kw):
        return "sub-banc", None

    handler = _rest_adapter._make_handler(cap, binding, None, _auth,
                                          _json_response, _json_error)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "method": binding.verb, "path": chemin,
                   "headers": [], "query_string": qs, "path_params": path_params},
                  _receive)
    try:
        rep = await handler(req)
    except Exception:            # noqa: BLE001 — au-delà du parsing : l'entrée a passé
        return 0, {}
    return rep.status_code, _json.loads(bytes(rep.body))


@pytest.fixture
def autz_sans_base(monkeypatch):
    """L'autz est résolue par l'adaptateur APRÈS le parsing mais AVANT le handler :
    sans ces deux seams, `SUB_ONLY` part chercher une base qui n'existe pas ici."""
    monkeypatch.setattr(_authz.access, "current_org", lambda sub: 1)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")


def test_il_y_a_bien_des_champs_liste_a_garder():
    """Une garde qui ne balaye rien serait verte pour la mauvaise raison."""
    assert _champs_liste_en_url(), (
        "aucun champ liste sur une capacité GET/DELETE — soit la forme a disparu, "
        "soit le balayage ne voit plus le registre.")


@pytest.mark.parametrize("cap,binding,champ", _champs_liste_en_url(),
                         ids=lambda x: getattr(x, "key", None) or str(x))
def test_chaque_champ_liste_declare_sa_valeur_dexemple(cap, binding, champ):
    """Sans exemple, l'épreuve d'en dessous ne prouve rien — donc pas d'exemple, rouge."""
    assert (cap.key, champ) in EXEMPLES, (
        f"`{cap.key}.{champ}` est un champ LISTE servi en `{binding.verb} "
        f"{binding.path}` : ajoute dans `EXEMPLES` la valeur qu'un intégrateur "
        "écrirait dans l'URL (valide métier, pas un `a,b` de façade). C'est cette "
        "ligne qui oblige à taper l'URL une fois — l'étape qui a manqué à #367.")


@pytest.mark.asyncio
@pytest.mark.parametrize("cap,binding,champ", _champs_liste_en_url(),
                         ids=lambda x: getattr(x, "key", None) or str(x))
async def test_un_champ_liste_se_dit_en_query_string(cap, binding, champ, autz_sans_base):
    """La requête littérale d'un intégrateur : une valeur à virgules dans l'URL.

    ⚠️ Contrôle d'abord SANS le champ : si l'appel minimal est déjà refusé, c'est le
    banc qui est faux, pas la capacité — et un rouge mal attribué coûte plus qu'un trou.
    """
    code, corps = await _appeler(cap, binding, {})
    assert not (code == 400 and corps.get("error") == "invalid_input"), (
        f"[banc] l'appel MINIMAL à {cap.key} est déjà refusé ({corps}) — les valeurs "
        f"fabriquées pour ses champs requis ne conviennent pas. Corrige "
        f"`_valeur_plausible` avant de conclure quoi que ce soit sur `{champ}`.")

    valeur = EXEMPLES[(cap.key, champ)]
    code, corps = await _appeler(cap, binding, {champ: valeur})
    assert not (code == 400 and corps.get("error") == "invalid_input"), (
        f"`{binding.verb} {binding.path}?{champ}={valeur}` est refusé : le champ "
        f"`{champ}` de `{cap.key}` déclare une LISTE, or une URL ne transporte que des "
        "chaînes. Déclare `Optional[list[str] | str]` (la forme réelle de l'entrée) ou "
        "pose un `field_validator(mode='before')` qui découpe sur la virgule — sinon le "
        "champ est inatteignable par la surface qui le porte, exactement comme "
        "`?include=procedures` pendant quinze jours (#367).")
