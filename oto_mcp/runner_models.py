"""Le catalogue des modèles qu'un agent hébergé peut déclarer — et leur FAMILLE.

Un agent (déclencheur, flotte) nomme un modèle ; le travail qu'il produit emporte
ce modèle ET sa famille. La famille est ce qui ROUTE : un worker ne réserve que
les travaux d'une famille qu'il sait servir, c'est-à-dire du dépôt de clé qu'il
nomme au claim (`provider`, cf. `runner_jobs._cle_de_modele`). Les deux mots
coïncident à dessein — la famille EST le nom du dépôt.

⚠️ **Un travail SANS famille, un worker de plateforme l'arrête à la réservation**
(`runner_jobs._SANS_MODELE`), et un déclencheur sans modèle n'en enfile plus — sa
livraison est refusée sur `motif_sans_famille` (21/10/2026).

⚠️ **Le catalogue refuse à l'ÉCRITURE, jamais à la lecture.** Une valeur inconnue
déjà en base (une flotte déclarée avec un modèle libre avant ce lot) se lit
toujours ; elle ne voyage simplement pas avec le travail, puisque rien ne sait la
router.

Pur : aucune dépendance, pour que la base comme les capacités puissent le lire.
"""
from __future__ import annotations

from typing import NamedTuple, Optional


class Modele(NamedTuple):
    id: str
    label: str
    family: str
    #: L'effort de réflexion — propriété du MODÈLE catalogué, pas un réglage
    #: par campagne ni par déclencheur (14/09/2026).
    #: `None` = le fournisseur applique son défaut, comme avant ce champ.
    effort: Optional[str] = None
    #: Le plafond de COMPLÉTION d'un tour — propriété du modèle comme l'effort, parce que
    #: le raisonnement partage ce plafond avec la réponse (14/09/2026). Mesuré au
    #: banc : mistral-medium-2604 en `high` monte à 6 964 jetons de
    #: complétion par tour (p90 3 968). `None` = le worker garde le sien.
    #: ⚠️ Un modèle qui RAISONNE (un effort autre que `none`) en déclare un : le worker
    #: lève sans lui (oto-runner `agent_llm_openai.plafond_de_sortie`).
    max_output_tokens: Optional[int] = None
    #: Les hôtes OpenRouter AUTORISÉS pour ce modèle, dans l'ordre de préférence
    #: (`provider.order` + `provider.only`, slugs d'endpoint OpenRouter : `deepinfra/fp8`).
    #: Propriété du modèle comme l'effort : un même id est servi par des dizaines d'hôtes,
    #: à des prix qui vont du simple au décuple et en quantifications différentes (fp4).
    #: Les épingler rend le tour PRÉVISIBLE — même qualité, et un prix borné par le plus
    #: cher de la liste (le tour est journalisé à SON coût réel, `tours_au_cout`). Vide =
    #: le fournisseur route où il veut (toute autre famille).
    providers: tuple[str, ...] = ()


#: ⚠️ L'ORDRE est la préférence : le modèle proposé par défaut est le premier
#: modèle SERVI de cette liste (cf. `catalogue`). Aucun n'est marqué en dur.
MODELES: tuple[Modele, ...] = (
    Modele("claude-sonnet-5", "Claude Sonnet 5", "anthropic"),
    Modele("claude-opus-5", "Claude Opus 5", "anthropic"),
    # ⚠️ `none` : Haiku 4.5 refuse `output_config.effort` (400, mesuré le 14/09/2026), que
    # le worker Anthropic envoie sinon à chaque tour. Avec `none`, il n'en envoie aucun.
    Modele("claude-haiku-4-5", "Claude Haiku 4.5", "anthropic", effort="none"),
    # La voie Conversations des workers de production (cf. oto-runner).
    Modele("mistral-large-2512", "Mistral Large", "mistral", max_output_tokens=8192),
    Modele("mistral-medium-2604", "Mistral Medium", "mistral", effort="high",
           max_output_tokens=16000),
    # Demandé par l'opérateur des campagnes le 14/09/2026, pour la passe F après que Medium
    # a été écarté pour son coût : au banc, en effort haut, 37/42 sur la priorité contre
    # 32/42 pour Large, pour environ la moitié du coût de Large, et 3 769 jetons de sortie
    # par tour au plus. Même plafond que Medium : le raisonnement le partage avec la réponse.
    Modele("mistral-small-2603", "Mistral Small", "mistral", effort="high",
           max_output_tokens=16000),
    # ⚠️ Les modèles OUVERTS servis par OpenRouter (famille `openrouter`, dépôt de clé du
    # même nom) : la voie bon marché des agents hébergés, en Chat Completions sur
    # `https://openrouter.ai/api/v1`. Placés APRÈS les familles d'avant : le défaut se dérive
    # du premier modèle servi, et un worker OpenRouter qui démarre ne le déplace pas.
    # Hôtes épinglés en fp8, jamais fp4 ni alias `~`. Un tour sur la clé partagée est
    # journalisé au coût qu'OpenRouter a rendu (`tours_au_cout`), aucune ligne de barème
    # ne suit cette liste. Le premier hôte est le moins cher de ceux qu'on garde ; les
    # suivants sont des secours de capacité, que `order` n'atteint que si les précédents
    # refusent (429, panne) — un secours plus cher ne coûte donc qu'en cas de panne.
    # ⚠️ Aux États-Unis par défaut (13/11/2026) : Flash et MiniMax n'ont plus aucun hôte
    # en Chine, donc l'org qui n'a PAS permis la Chine (`orgs.allow_cn_models`, FALSE
    # par défaut) les a quand même. V4 Pro reste servi en Chine (StreamLake, Baidu : la
    # moitié du prix de GMI Cloud) et ne s'offre qu'à l'org qui l'a permis.
    # Le raisonnement partage le plafond de complétion avec la réponse : chacun déclare
    # le sien (`max_output_tokens`), comme les modèles Mistral qui raisonnent.
    #
    # DeepSeek V4.1 Flash (1017) : le modèle du banc « procédures réelles » — suit les
    # procédures, s'arrête sur une donnée manquante, ~×0,2 le coût de Claude. Contexte 1M.
    # DeepInfra (0,098 / 0,294 / 0,0029 $/M, 13/11/2026) est désormais moins cher que
    # StepFun en entrée et en sortie ; GMI Cloud (0,18 / 0,72) est le secours.
    Modele("deepseek/deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash", "openrouter",
           effort="medium", max_output_tokens=16000,
           providers=("deepinfra/fp8", "gmicloud/fp8")),
    Modele("deepseek/deepseek-v4-pro", "DeepSeek V4 Pro", "openrouter",
           effort="medium", max_output_tokens=16000,
           providers=("streamlake/fp8", "baidu/fp8", "gmicloud/fp8")),
    # GLM-5.3 (z-ai/glm-5.3) RETIRÉ le 12/11/2026 : sur un run long (60 tours), il conclut
    # par du texte après une écriture au lieu d'enchaîner (20 tours sur 60), ce qui
    # termine le travail dans la boucle. À ne remettre qu'après un run long qui enchaîne.
    # MiniMax M3 : GMI Cloud (0,24 / 0,96, contexte 1M, 13/11/2026) puis Parasail (0,30 /
    # 1,20, celui du run long de 60 tours du 12/11). StreamLake retiré : plus de Chine.
    Modele("minimax/minimax-m3", "MiniMax M3", "openrouter",
           effort="medium", max_output_tokens=16000,
           providers=("gmicloud/fp8", "parasail/fp8")),
    # ⚠️ L'ABONNEMENT de la personne (OTO-130), pas une clé : ces travaux tournent
    # dans SON sandbox, sur le programme Claude Code officiel où elle s'est
    # connectée elle-même. La famille est donc un dépôt qu'AUCUNE org ne dépose et
    # que la plateforme ne paie pas — `FAMILLES_PERSONNELLES` ci-dessous dit à la
    # garde d'argent de ne pas aller chercher une clé qui n'existe pas.
    #
    # ⚠️ Des ids PRÉFIXÉS, et non les ids nus du catalogue Anthropic : la famille se
    # DÉDUIT du modèle (`famille`), donc deux voies d'exécution ne peuvent pas
    # partager un id. `claude-sonnet-5` reste la voie « clé de l'org » ;
    # `sub:sonnet` est la voie « abonnement ». Le runner retire le préfixe et passe
    # l'alias au programme, qui sert ce que le forfait de la personne autorise.
    Modele("sub:sonnet", "Claude Sonnet (abonnement)", "claude_subscription"),
    Modele("sub:opus", "Claude Opus (abonnement)", "claude_subscription"),
    Modele("sub:haiku", "Claude Haiku (abonnement)", "claude_subscription"),
)

_PAR_ID = {m.id: m for m in MODELES}

#: Les familles connues — c'est-à-dire les seuls dépôts dont la présence se note.
FAMILLES = frozenset(m.family for m in MODELES)


#: Les familles servies par l'abonnement PERSONNEL de quelqu'un. La convention de
#: nom (`*_subscription`) est la seule source : la base (`db/runner_jobs`) et la garde
#: (`capabilities/_abonnement`) la relisent d'ici, jamais d'une liste recopiée.
FAMILLES_PERSONNELLES = frozenset(f for f in FAMILLES if f.endswith("_subscription"))
#: Les modèles qui tournent sur l'abonnement d'une personne — ce que l'écriture d'une
#: retouche confronte au modèle STOCKÉ, sans relire le déclencheur (`update_trigger`).
MODELES_PERSONNELS = tuple(m.id for m in MODELES if m.family in FAMILLES_PERSONNELLES)


#: OÙ TOURNE un modèle (12/11/2026) : ce que l'écran dit au client — le pays des
#: serveurs qui reçoivent chaque tour (la consigne, les lignes lues, les résultats
#: d'outils). Une RÉGION par hôte : `us`, `eu`, `cn`. Sources lues le 12/11/2026 :
#: `GET https://openrouter.ai/api/v1/providers` (`headquarters`, `datacenters`) pour
#: Anthropic (US), Mistral (FR), Baidu (CN), StreamLake (CN), DeepInfra (US),
#: Parasail (US) et GMICloud (US, centres US) ; StepFun n'y avait pas de siège le
#: 12/11 (ses conditions relèvent du droit de Hong Kong, donc `cn`) et y figure en CN
#: le 13/11. StepFun et StreamLake ne sont plus épinglés (13/11), gardés ici pour
#: qu'un retour en arrière dise encore où ils tournent. ⚠️ Un hôte épinglé (`Modele.providers`) absent d'ici fait échouer un
#: banc : un modèle ne se sert jamais sans dire où il tourne.
HOTES: dict[str, tuple[str, str]] = {
    "anthropic": ("Anthropic", "us"),
    "mistral": ("Mistral AI", "eu"),
    "stepfun": ("StepFun", "cn"),
    "streamlake": ("StreamLake", "cn"),
    "baidu": ("Baidu", "cn"),
    "deepinfra": ("DeepInfra", "us"),
    "parasail": ("Parasail", "us"),
    "gmicloud": ("GMI Cloud", "us"),
}
#: Qui a CONSTRUIT le modèle, et son pays (ISO) — distinct d'où il tourne : un modèle
#: chinois peut tourner aux États-Unis. Clé : la famille, ou le préfixe de l'id
#: OpenRouter (`deepseek/…`).
FABRICANTS: dict[str, tuple[str, str]] = {
    "anthropic": ("Anthropic", "US"),
    "claude_subscription": ("Anthropic", "US"),
    "mistral": ("Mistral AI", "FR"),
    "deepseek": ("DeepSeek", "CN"),
    "minimax": ("MiniMax", "CN"),
}
#: Le routeur entre l'agent et l'hôte, quand il y en a un : il voit aussi chaque tour.
#: OpenRouter, Inc. — New York (openrouter.ai/terms, lu le 12/11/2026).
ROUTEURS: dict[str, tuple[str, str]] = {"openrouter": ("OpenRouter", "us")}
#: L'hôte d'une famille servie par le seul fournisseur du modèle.
_HOTE_DE_LA_FAMILLE = {"anthropic": "anthropic", "claude_subscription": "anthropic",
                       "mistral": "mistral"}
#: Du plus au moins sensible : la région d'un modèle est la PIRE de ses hôtes.
_ORDRE_DES_REGIONS = ("cn", "us", "eu")


def hebergement(model: Optional[str]) -> Optional[dict]:
    """Où tourne `model` : `{region, hosts: [{name, region, role}], maker: {name,
    country}, router: {name, region} | None}`, None pour un modèle inconnu.

    `region` est la PIRE région de ses hôtes, routeur compris — `cn` dès qu'UN hôte de la
    liste est en Chine, même en secours (MiniMax M3 : premier hôte aux US). `role` :
    `only` (un seul hôte), sinon `first`, `backup`, et `last_backup` pour le dernier —
    l'ordre de `provider.order`. Lève `KeyError` sur un hôte que `HOTES` ne connaît pas."""
    m = _PAR_ID.get(model or "")
    if not m:
        return None
    slugs = ([p.split("/", 1)[0] for p in m.providers] if m.providers
             else [_HOTE_DE_LA_FAMILLE[m.family]])
    roles = (["only"] if len(slugs) == 1
             else ["first"] + ["backup"] * (len(slugs) - 2) + ["last_backup"])
    hosts = [{"name": HOTES[s][0], "region": HOTES[s][1], "role": r}
             for s, r in zip(slugs, roles)]
    routeur = ROUTEURS.get(m.family)
    regions = {h["region"] for h in hosts} | ({routeur[1]} if routeur else set())
    cle = m.family if m.family in FABRICANTS else m.id.split("/", 1)[0]
    fabricant = FABRICANTS[cle]
    return {"region": next(r for r in _ORDRE_DES_REGIONS if r in regions),
            "hosts": hosts, "maker": {"name": fabricant[0], "country": fabricant[1]},
            "router": ({"name": routeur[0], "region": routeur[1]} if routeur else None)}


def famille(model: Optional[str]) -> Optional[str]:
    """La famille d'un modèle du catalogue, ou None (absent, ou inconnu)."""
    m = _PAR_ID.get(model or "")
    return m.family if m else None


#: Le motif d'une livraison REFUSÉE parce que l'agent ne déclare aucun modèle
#: (21/10/2026) : un travail sans famille, un worker de plateforme l'arrête à la
#: réservation (`runner_jobs._SANS_MODELE`), donc le déclencheur ne l'enfile plus.
MOTIF_SANS_MODELE = "cet agent ne déclare aucun modèle : il ne sera pas exécuté"


def motif_sans_famille(model: Optional[str]) -> Optional[str]:
    """Pourquoi un travail de ce modèle ne partirait PAS, ou None s'il a une famille.

    Le tick et le webhook refusent la livraison sur ce motif au lieu d'enfiler un
    travail sans famille. Un modèle déclaré mais absent du catalogue (retiré depuis)
    est nommé tel quel : le dire « sans modèle » enverrait chercher le mauvais geste."""
    if famille(model):
        return None
    if not model:
        return MOTIF_SANS_MODELE
    return (f"cet agent déclare un modèle inconnu du catalogue (`{model}`) : "
            "il ne sera pas exécuté")


def charge(model: Optional[str]) -> dict:
    """Ce qu'un travail emporte de son modèle : `{model, model_family}`, plus
    `effort` et `max_output_tokens` (14/09/2026) SI le modèle catalogué les déclare —
    mistral-medium-2604 tourne en effort HAUT sous 16 000 jetons de complétion,
    claude-haiku-4-5 sans effort — et `providers`, les hôtes OpenRouter épinglés. Une clé
    que le modèle ne déclare pas ne part pas.

    ⚠️ Un modèle que le catalogue ne connaît pas ne part PAS : sans famille, il
    atteindrait un worker quelconque qui tenterait de l'appeler chez un
    fournisseur qui ne le sert peut-être pas. Ne rien envoyer rend le travail au
    comportement d'avant — le worker tourne sur son propre modèle."""
    m = _PAR_ID.get(model or "")
    if not m:
        return {}
    charge = {"model": model, "model_family": m.family}
    if m.effort:
        charge["effort"] = m.effort
    if m.max_output_tokens:
        charge["max_output_tokens"] = m.max_output_tokens
    if m.providers:
        charge["providers"] = list(m.providers)
    return charge


def repli_api(model: Optional[str]) -> Optional[str]:
    """Le modèle `anthropic` de même TIER qu'un modèle d'ABONNEMENT — l'autre
    façon de payer le même travail (OTO-130, repli plafond, 27/09/2026).

    Dérivé du même préfixe que la famille (`sub:sonnet` -> tier `sonnet` ->
    `claude-sonnet-5`) — jamais une table à part qui divergerait du catalogue le
    jour où un tier est ajouté d'un côté et pas de l'autre. `None` si `model`
    n'est pas un abonnement, ou si aucun modèle `anthropic` ne porte ce tier."""
    m = _PAR_ID.get(model or "")
    if not m or m.family not in FAMILLES_PERSONNELLES:
        return None
    tier = (model or "").split(":", 1)[-1]
    if not tier:
        return None
    return next((c.id for c in MODELES if c.family == "anthropic" and tier in c.id),
                None)


def familles_de_repli(vers_famille: Optional[str]) -> frozenset[str]:
    """Les familles d'ABONNEMENT dont au moins un modèle a un équivalent tier
    dans `vers_famille` (`repli_api`) — dérivée, jamais recopiée : un tier
    ajouté d'un côté sans l'autre sort de lui-même de cet ensemble."""
    if not vers_famille:
        return frozenset()
    return frozenset(
        m.family for m in MODELES
        if m.family in FAMILLES_PERSONNELLES
        and famille(repli_api(m.id)) == vers_famille)


def catalogue(familles_servies, familles_sans_cle=()) -> list[dict]:
    """Le catalogue tel qu'un écran le propose : chaque modèle, s'il est SERVI —
    une famille dont un worker a sondé la file dans la fenêtre de présence — et
    celui à proposer par DÉFAUT.

    ⚠️ **Le défaut se DÉRIVE de ce qui est servi, il ne se déclare pas** : c'est le
    premier modèle servi dans l'ordre de `MODELES`. Une marque posée en dur
    (`claude-sonnet-5` jusqu'au 12/09/2026) proposait un modèle que les workers de
    production, qui ne servent que `mistral`, ne servaient pas : un agent qui la
    suivait se faisait refuser `model_not_served`.

    ⚠️ **Aucun défaut quand aucune famille n'est servie** — jamais de repli sur le
    premier du catalogue, qui proposerait un modèle refusé. L'absence se lit avec
    `families: []`, et le geste est alors de ne nommer aucun modèle : le worker
    tourne sur le sien. Le défaut ne s'écrit nulle part : un agent posé sans modèle
    reste NULL, et un modèle choisi n'est jamais changé.

    ⚠️ **Le défaut écarte les familles que l'org ne peut pas payer** (14/09/2026) :
    `familles_sans_cle` = celles dont la clé est EXIGÉE et que l'org n'a pas déposée
    (`capabilities/_cle_exigee.manquantes`). Un modèle de ces familles reste `served`
    — un worker le sert, et l'org peut déposer sa clé puis le choisir — mais il n'est
    jamais PROPOSÉ. Le premier worker Anthropic « clés clients seules » avait fait de
    `claude-sonnet-5` le défaut de toutes les orgs, dont aucune n'avait de clé : un agent
    qui le suivait se faisait refuser `model_key_required`. Si toutes les familles
    servies sont écartées, aucun défaut — jamais un repli sur un modèle refusé."""
    servies = set(familles_servies or ())
    # ⚠️ Un ABONNEMENT n'est JAMAIS le défaut (OTO-130, revue du 21/09/2026). Il ne
    # sert que la personne qui s'y est connectée : proposé à toute une org, il
    # ferait refuser `subscription_not_connected` à quiconque suit la proposition —
    # la faute exacte du défaut en dur corrigée le 12/09, par une autre porte. Le
    # cas n'a rien de théorique : il suffit que le pool d'abonnement soit le SEUL
    # vivant (une preprod, un incident sur les autres pools). Il reste `served` :
    # qui est connecté le choisit, personne ne se le voit proposer.
    proposables = servies - set(familles_sans_cle or ()) - FAMILLES_PERSONNELLES
    defaut = next((m.id for m in MODELES if m.family in proposables), None)
    return [{"id": m.id, "label": m.label, "family": m.family,
             "default": m.id == defaut, "served": m.family in servies,
             "hosting": hebergement(m.id)}
            for m in MODELES]
