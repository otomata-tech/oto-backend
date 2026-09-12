"""Les liens qu'on rend à un utilisateur — et à quoi ils ressemblent CHEZ LUI.

Un client d'un partenaire recevait des liens vers notre tableau de bord : un produit
qu'il n'a pas. La première correction a fait suivre l'ADRESSE au tenant — insuffisant,
et le code du partenaire le prouve : **aucun de ses chemins ne ressemble aux nôtres**
(`/network/<org>/knowledge/<id>` là où nous servons `/docs/<id>`), et pour certaines
de nos vues il **n'a aucun équivalent** — ses tableaux n'existent pas. Coller nos
chemins sous son domaine aurait donc fabriqué des liens morts, ce qui est pire qu'un
lien à notre marque : un lien mort ne se diagnostique pas, il se subit.

D'où un patron par TYPE de lien, déclaré par le tenant, et une règle simple :

    pas de patron ⟹ pas de lien.

**Deux familles, et les confondre casse un parcours** :

- un lien **AFFICHÉ** (un tableau, une page partagée) peut ne pas exister : on n'écrit
  rien plutôt que d'envoyer quelque part. `link_for` rend alors `None`, et l'appelant
  omet le lien — il a toujours de quoi se rendre utile sans lui.
- une **REDIRECTION** (le retour d'une connexion OAuth) doit TOUJOURS aboutir : on ne
  peut pas « ne pas rediriger ». Sans patron, elle retombe sur la nôtre — l'utilisateur
  voit notre marque une fois, ce qui vaut mieux qu'une page blanche au milieu d'une
  connexion. C'est `redirect_for`, et c'est le seul chemin qui replie ainsi.

⚠️ **Mis à jour le 10/09/2026 (oto#63).** Le partenaire décrit plus haut a désormais
une page de tableau ; tant que sa ligne de tenant ne la déclare pas, ses comptes
reçoivent `null`. Un `null` NU disait « introuvable » à qui le lisait — d'où
`raison_sans_lien`, qui dit pourquoi l'adresse manque, et `patron_reclame`, qui ne fait
payer un paramètre coûteux (l'org de l'appelant) qu'au produit qui le réclame.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from . import config

logger = logging.getLogger(__name__)

# Nos propres chemins, par type. Source unique : ce que le tenant `oto` sert, et le
# défaut de tout tenant qui ne déclare rien. `{…}` = paramètres nommés du lien.
DEFAULT_PATHS: dict[str, str] = {
    "home": "",
    "table": "/data/{id}",
    "public_doc": "/p/d/{token}",
    # ⚠️ A dit `/docs/{id}` du 2026-08-13 (41e7928) au 2026-08-28 — un chemin que
    # NOTRE tableau de bord ne route pas : son routeur ne connaît que la section
    # `/documents` (sans id), et son attrape-tout renvoie tout chemin inconnu sur
    # `/overview`. Le lien n'aurait donc pas affiché d'erreur : il aurait ouvert la
    # page d'accueil en se faisant passer pour la page demandée. Il n'a jamais eu
    # d'appelant, ce qui l'a gardé invisible — et c'est très exactement le lien mort
    # que ce module existe pour interdire, posé chez nous. Le VRAI chemin d'une page,
    # celui que le front lui-même écrit (`searchNav.ts`, `InboxCard.vue`,
    # `ProjectDetailView`), l'ouvre DANS son projet : une page n'a pas d'écran à elle.
    # Conséquence : ce patron réclame `project_id`, et un appel qui ne le passe pas ne
    # rend aucun lien (garde de `_render`) — jamais une adresse à trous.
    "doc": "/projects/{project_id}?doc={id}",
    "project": "/projects/{id}",
    # L'espace facturation du tableau de bord (`/org/billing`, cf. le routeur
    # d'oto-dashboard) — c'est là que les factures se téléchargent. Un tenant qui
    # ne déclare pas ce patron n'a pas cette vue : l'e-mail part alors sans bouton,
    # plutôt qu'avec un lien mort.
    "billing": "/org/billing",
    "connectors": "/connectors",
    "connector_return": "/connectors?connector={connector}",
    "marketplace": "/connectors?tab=marketplace",
}


def _tenant_of(sub: Optional[str]):
    """L'entrée de registre du tenant de ce compte, ou None (tenant primaire inclus)."""
    if not sub:
        return None
    try:
        from . import tenancy
        registre = tenancy.current()
        # `entry_for_slug` rend None pour le tenant primaire comme pour un slug
        # inconnu — la distinction ne servait à personne ici, et la refaire à la
        # main était la deuxième copie d'une recherche qui n'a qu'un sens.
        return registre.entry_for_slug(registre.tenant_of(sub))
    except Exception:  # noqa: BLE001 — un lien ne casse jamais un appel
        logger.warning("résolution du tenant impossible pour un lien (fail-open)",
                       exc_info=True)
        return None


def _render(base: str, path: str, params: dict) -> Optional[str]:
    """Assemble base + patron. Un paramètre manquant ANNULE le lien plutôt que de
    produire une adresse à trous (`/network//knowledge/12`), qui mènerait à une page
    d'erreur en se faisant passer pour un lien valide."""
    try:
        rendu = path.format(**{k: v for k, v in params.items() if v is not None})
    except (KeyError, IndexError):
        logger.warning("lien non rendu : le patron %r attend un paramètre absent", path)
        return None
    if "{" in rendu or "//" in rendu.lstrip("https:").lstrip("/"):
        return None
    return f"{base}{rendu}" if rendu else base


def link_for(kind: str, *, sub: Optional[str] = None, **params: Any) -> Optional[str]:
    """Le lien de type `kind` à MONTRER à ce compte, ou **None** s'il n'existe pas
    chez lui.

    `None` n'est pas une erreur : c'est la réponse juste quand le produit de
    l'utilisateur n'a pas cette vue. L'appelant écrit alors sa réponse sans lien.
    """
    entry = _tenant_of(sub)
    if entry is None:
        chemin = DEFAULT_PATHS.get(kind)
        return None if chemin is None else _render(config.dashboard_url(), chemin, params)

    patrons = getattr(entry, "link_paths", None) or {}
    if kind not in patrons:
        return None                       # le tenant n'a pas cette vue : pas de lien
    base = (entry.dashboard_url or "").rstrip("/")
    if not base:
        return None                       # un patron sans adresse ne mène nulle part
    return _render(base, str(patrons[kind]), params)


def ou_poser_la_cle(sub: Optional[str], *, org: Any = None,
                    connecteur: Optional[str] = None) -> str:
    """Le complément « sur <page connecteurs> (connecteur X) » d'une phrase qui dit OÙ
    poser une clé — ou une chaîne VIDE quand le produit du compte ne déclare pas de page
    connecteurs. La phrase reste vraie sans lui : « pose ta propre clé » plutôt que
    « pose ta propre clé sur <une page qui n'existe pas> ».

    ⚠️ Vécu le 2026-09-11 (oto-backend#935) : les refus de credential
    (`access/resolve.py`) et la carte connecteur (`connectors/readiness.py`) collaient
    `/account` — NOTRE chemin — sous l'adresse du tenant du compte. Chez le seul tenant
    tiers déclaré, cette page répond 404 : c'est exactement le lien mort que ce module
    interdit. Le chemin vient désormais du patron `connectors` du tenant ; un tenant qui
    ne le déclare pas ne reçoit AUCUNE adresse, jamais la nôtre.

    `org` n'est lu que par un patron qui le réclame (`/org/{org}/connectors`) ; absent,
    le lien est annulé plutôt que rendu à trous (cf. `_render`)."""
    url = link_for("connectors", sub=sub, org=org)
    if not url:
        return ""
    return f" sur {url}" + (f" (connecteur {connecteur.capitalize()})" if connecteur else "")


# Le NOM de l'objet, pour dire à un humain ce qui n'a pas d'adresse.
_NOMS = {"table": "tableau", "doc": "page", "project": "projet",
         "public_doc": "page publique", "connectors": "connecteurs"}


def _chemin_pour(kind: str, sub: Optional[str]) -> Optional[str]:
    entry = _tenant_of(sub)
    if entry is None:
        return DEFAULT_PATHS.get(kind)
    return (getattr(entry, "link_paths", None) or {}).get(kind)


def patron_reclame(kind: str, param: str, *, sub: Optional[str] = None) -> bool:
    """Le patron de lien de ce compte pour `kind` porte-t-il `{param}` ?

    Sert à ne payer un paramètre coûteux QUE lorsque le produit qui recevra le lien le
    réclame : chez nous un tableau s'ouvre par son seul id, et résoudre l'org de
    l'appelant pour rien sur chaque ligne d'une liste serait une requête par ligne."""
    chemin = _chemin_pour(kind, sub)
    return bool(chemin) and "{" + param + "}" in str(chemin)


def raison_sans_lien(kind: str, *, sub: Optional[str] = None, **params: Any) -> Optional[str]:
    """POURQUOI `link_for` ne rend rien — `None` s'il rend bien un lien.

    `None` n'est pas une erreur (cf. la tête de ce module), mais un `null` NU était
    indiscernable d'un objet introuvable (oto#63) : l'appelant cherchait une panne qui
    n'existe pas, ou concluait que l'objet n'existe pas. Mêmes branches que
    `link_for`, dans le même ordre — une lecture du registre, deux formulations."""
    if link_for(kind, sub=sub, **params) is not None:
        return None
    nom = _NOMS.get(kind, kind)
    entry = _tenant_of(sub)
    if entry is None:
        chemin = DEFAULT_PATHS.get(kind)
        if chemin is None:
            return f"aucune page de {nom} n'existe dans ce produit"
    else:
        patrons = getattr(entry, "link_paths", None) or {}
        if kind not in patrons:
            return (f"le produit de ce compte ne déclare aucune page de {nom} : il n'y a "
                    f"pas d'adresse à donner. Le {nom} existe bel et bien — c'est "
                    "l'adresse qui manque, pas lui")
        if not (entry.dashboard_url or "").strip():
            return (f"le produit de ce compte déclare une page de {nom}, mais aucune "
                    "adresse de base : aucun lien ne peut être construit")
        chemin = str(patrons[kind])
    poses = {k for k, v in params.items() if v is not None}
    manquants = sorted(set(re.findall(r"\{(\w+)\}", str(chemin))) - poses)
    if manquants:
        return ("l'adresse de ce produit réclame "
                + ", ".join(f"`{m}`" for m in manquants) + ", que ce contexte ne porte pas"
                + (" — passe `_org=` pour la fixer" if "org" in manquants else ""))
    return "aucune adresse n'a pu être construite : le patron déclaré est illisible"


def redirect_for(kind: str, *, sub: Optional[str] = None, **params: Any) -> str:
    """Le lien de type `kind` vers lequel on REDIRIGE. Toujours une adresse.

    Utilisé au retour d'un consentement OAuth : à ce moment le navigateur DOIT
    atterrir quelque part. Sans patron chez le tenant, on sert le nôtre — voir notre
    marque une fois vaut mieux qu'une page blanche au milieu d'une connexion.
    """
    return (link_for(kind, sub=sub, **params)
            or _render(config.dashboard_url(), DEFAULT_PATHS.get(kind, ""), params)
            or config.dashboard_url())
