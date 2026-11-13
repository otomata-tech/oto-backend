"""L'annuaire Logto administré par la plateforme : jeton de management par annuaire, et
les gestes qui s'en servent (email primaire, double authentification, lien magique,
ressource d'API d'un sous-domaine publié).

L'inscription des clients MCP passe par Logto lui-même, par CIMD (Client ID Metadata
Document) — le `client_id` d'un client est l'URL de son document de métadonnées
(`docs/auth-logto.md`). Ce module ne touche à aucune application cliente ; celles des
clients sans CIMD sont créées par `auth/inscription_dynamique.py`, sous ce même jeton.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

_log = logging.getLogger("oto_mcp.annuaire")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"  # vs WAF Cloudflare (1010)
_MGMT_RESOURCE = "https://default.logto.app/api"
# Le credential de management de NOTRE annuaire, sous la même convention que celle
# qu'un tenant déclare : `<credential>_ID` / `<credential>_SECRET`. Le primaire n'est
# donc pas un cas particulier — c'est le premier annuaire, pas le seul.
_PRIMARY_CREDENTIAL = "OTO_MCP_LOGTO_M2M"


@dataclass(frozen=True)
class Directory:
    """Un annuaire Logto **administrable** : où prendre le jeton, où poser les appels
    `/api`, et sous quel credential.

    ⚠️ Les deux endpoints sont un COUPLE, pas une base unique. Chez Logto le jeton de
    management s'obtient sur l'endpoint d'ADMINISTRATION et les appels `/api` vont sur
    l'endpoint PRINCIPAL ; l'inverse rend `401 aud check_failed`. Sur notre annuaire
    les deux coïncident, ce qui est précisément ce qui a permis de vivre longtemps
    avec une seule variable — et ce qui l'aurait rendue fausse en silence au premier
    annuaire où ils diffèrent (celui du partenaire : un domaine public à sa marque, un
    hôte d'administration chez nous).

    ⚠️ `credential` est le NOM d'un couple de variables d'environnement, jamais une
    valeur : rien de secret n'entre dans cet objet, donc rien de secret ne peut sortir
    par un log, une trace d'exception ou une fiche de suivi.
    """
    label: str            # le tenant, pour les messages — jamais pour la sélection
    token_endpoint: str
    api_endpoint: str
    credential: str

    @property
    def key(self) -> str:
        """L'identité de ce qui PRODUIT le jeton — la clé de son cache.

        Le `label` en est exclu à dessein : deux tenants sur le même annuaire, sous le
        même credential, partagent légitimement un jeton. Tout le reste y entre, y
        compris l'endpoint d'API qui ne change pourtant pas le jeton : une différence
        quelque part doit donner un slot différent, jamais un jeton réutilisé au
        hasard d'une égalité partielle.
        """
        return f"{self.token_endpoint}|{self.api_endpoint}|{self.credential}"


# ⚠️ Un dict CLEFÉ par annuaire, jamais un singleton. Le cache était
# `{"value", "exp"}` au niveau du module, partagé par tous les appelants : brancher un
# second annuaire dessus ferait servir à l'un le jeton de l'autre — au mieux des refus
# intermittents, au pire une écriture dirigée vers le mauvais annuaire (oto#909).
_mgmt_toks: dict = {}


def _logto_base() -> str:
    return os.environ["LOGTO_ENDPOINT"].rstrip("/")


def _primary_directory() -> Directory:
    """NOTRE annuaire — le défaut de tout appel de management non qualifié."""
    from ..tenancy import primary_slug
    base = _logto_base()
    return Directory(primary_slug(), base, base, _PRIMARY_CREDENTIAL)


def directory_for_tenant(entry) -> "Directory | None":
    """L'annuaire administrable d'un tenant, ou **None** s'il n'en déclare pas.

    `None` est le défaut et le reste : authentifier les comptes d'un tenant ne donne
    aucun droit d'écrire dans son annuaire. N'en rend un que pour un tenant qui a
    DÉCLARÉ où frapper et sous quel nom — c'est-à-dire, en pratique, un tenant dont
    nous hébergeons l'annuaire.
    """
    mgmt = getattr(entry, "logto_mgmt", None)
    if not mgmt:
        return None
    return Directory(getattr(entry, "slug", "") or "?", mgmt["token_endpoint"],
                     mgmt["api_endpoint"], mgmt["credential"])


def _credential_present(d: Directory) -> bool:
    """Le process détient-il la clé de cet annuaire ? Déclaré en base ≠ injecté ici :
    l'écart est exactement ce qui doit se voir, pas se rattraper."""
    return bool(os.environ.get(f"{d.credential}_ID")
                and os.environ.get(f"{d.credential}_SECRET"))


def _mgmt_token(directory: "Directory | None" = None) -> str:
    import requests
    d = directory or _primary_directory()
    cid = os.environ.get(f"{d.credential}_ID")
    csec = os.environ.get(f"{d.credential}_SECRET")
    if not cid or not csec:
        raise RuntimeError(
            f"annuaire {d.label} : credential de management absent de "
            f"l'environnement ({d.credential}_ID / {d.credential}_SECRET)")
    now = time.time()
    slot = _mgmt_toks.get(d.key)
    if slot and slot["value"] and slot["exp"] > now + 30:
        return slot["value"]
    r = requests.post(
        f"{d.token_endpoint}/oidc/token",
        data={"grant_type": "client_credentials", "resource": _MGMT_RESOURCE, "scope": "all"},
        auth=(cid, csec), headers={"User-Agent": _UA}, timeout=15,
    )
    r.raise_for_status()
    j = r.json()
    _mgmt_toks[d.key] = {"value": j["access_token"],
                         "exp": now + int(j.get("expires_in", 3600))}
    return _mgmt_toks[d.key]["value"]


def logto_user_primary_email(sub: str) -> str | None:
    """Email primaire AUTORITATIF d'un user Logto (Management API). Dans Logto, le
    `primaryEmail` n'est posé qu'après vérification de l'adresse → sa présence vaut
    « email vérifié », et c'est la SOURCE DE VÉRITÉ (un claim de token, lui, peut
    mentir). Utilisé par la bascule de tenant pour décider d'un merge de comptes sans
    faire confiance au token. Renvoie None si user inconnu / Logto indispo (l'appelant
    ne migre alors PAS — fail-safe).

    ⚠️ **Lève `ForeignTenantDirectory` sur un sub qualifié** (`slug:sub`, tenant tiers)
    plutôt que d'interroger notre Logto avec un identifiant qui n'y existe pas : le
    None qui en sortirait est indistinguable d'un « email non vérifié », et l'échec se
    lirait à l'autre bout de la chaîne. Router la lecture vers l'émetteur du tenant
    demanderait ses credentials de management, qu'on n'a pas (oto-backend#274)."""
    import requests

    from ..tenancy import require_primary_tenant
    # AVANT le try : le message doit remonter à l'appelant, pas être absorbé par le
    # fail-safe ci-dessous (qui, lui, couvre les pannes réseau/Logto).
    require_primary_tenant(sub, "lecture de l'email primaire Logto")
    try:
        base, tok = _logto_base(), _mgmt_token()
        r = requests.get(
            f"{base}/api/users/{sub}",
            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA},
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("primaryEmail") or None
    except Exception as e:
        _log.warning("lookup primaryEmail Logto échoué pour %s : %s", sub, e)
        return None


def reset_user_mfa(sub: str) -> list[str]:
    """Efface tous les facteurs de double authentification d'un user Logto (Management
    API) — geste de récupération de compte : perte de l'appli ET des codes de secours,
    aucun autre moyen de rentrer. Renvoie les types retirés (pour l'audit du geste
    admin, jamais les valeurs). Lève si Logto est injoignable (l'appelant décide)."""
    import requests
    base, tok = _logto_base(), _mgmt_token()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA}
    r = requests.get(f"{base}/api/users/{sub}/mfa-verifications", headers=h, timeout=15)
    r.raise_for_status()
    removed = []
    for v in r.json():
        d = requests.delete(f"{base}/api/users/{sub}/mfa-verifications/{v['id']}", headers=h, timeout=15)
        d.raise_for_status()
        removed.append(v["type"])
    return removed


# ── Magic link : one-time-token Logto (onboarding sans saisie de code) ────────
# Le backend mint un OTT pour l'email de l'invité (Management API) ; le lien le
# porte → la custom UI Logto le consomme (signIn extraParams) → auth silencieuse,
# compte créé/loggé avec l'email EXACT (pas de mismatch). Best-effort : si le mint
# échoue (M2M absent, Logto down), l'invitation reste valide via le flow code email.
def mint_one_time_token(email: str, *, expires_in: int = 7 * 24 * 3600) -> str | None:
    """Mint un one-time-token Logto (magic link) pour `email`. Renvoie le token,
    ou None si le mint échoue (l'appelant dégrade vers le lien sans magic-link)."""
    import requests
    try:
        base, tok = _logto_base(), _mgmt_token()
        r = requests.post(
            f"{base}/api/one-time-tokens",
            json={"email": email, "expiresIn": int(expires_in)},
            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA,
                     "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["token"]
    except Exception as e:  # M2M non configuré, Logto indispo, expiry rejeté…
        _log.warning("OTT mint échoué pour %s : %s", email, e)
        return None


def magic_url(base_url: str, email: str, *, expires_in: int = 7 * 24 * 3600) -> str:
    """Augmente `base_url` d'un magic-link Logto pour `email` (params `otl` +
    `login_hint`). Le front fait `signIn({extraParams:{one_time_token: otl},
    loginHint})`. Si le mint échoue, renvoie `base_url` inchangé (dégradation
    gracieuse vers le flow code email)."""
    from urllib.parse import quote
    ott = mint_one_time_token(email, expires_in=expires_in)
    if not ott:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}otl={quote(ott, safe='')}&login_hint={quote(email, safe='')}"


def ensure_api_resource(indicator: str, *, name: str | None = None) -> None:
    """Enregistre (idempotent) une API resource Logto pour `indicator` (le resource
    indicator = l'audience JWT, ex. `https://<slug>.mcp.oto.cx/mcp`). Sans ça, Logto
    émet un token OPAQUE pour ce sous-domaine (≠ resource enregistrée) → `invalid_token`
    (blocage historique #44). Best-effort côté appelant : lève si la Management API
    échoue (l'appelant loggue et n'empêche pas la publication). Réutilise le M2M partagé."""
    import requests
    base, tok = _logto_base(), _mgmt_token()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA, "Content-Type": "application/json"}
    existing = requests.get(f"{base}/api/resources", headers=h, timeout=15)
    existing.raise_for_status()
    if any((r.get("indicator") == indicator) for r in existing.json()):
        return
    r = requests.post(
        f"{base}/api/resources",
        json={"name": name or indicator, "indicator": indicator},
        headers=h, timeout=15,
    )
    r.raise_for_status()
    _log.info("Logto API resource créée : %s", indicator)


# ── Fermer tout ce qu'un compte a d'ouvert chez Logto ─────────────────────────
# Un mot de passe changé ne ferme RIEN chez Logto : ni les sessions de son annuaire, ni
# les autorisations (grants) qui portent les jetons de rafraîchissement des clients
# OAuth — un client MCP autorisé depuis une session volée se rafraîchit donc
# indéfiniment. Appelé par la réception du webhook Logto (`auth/revocation_mot_de_passe`).

#: Ce qu'une requête de management attend au plus, et ce que la fermeture entière
#: attend au plus : une borne posée à SON niveau, pas seulement au plus profond — une
#: vingtaine de sessions à 15 s chacune ferait sinon attendre l'envoyeur plusieurs
#: minutes.
_DELAI_REQUETE_S = 10
_DELAI_FERMETURE_S = 30


class FermetureInachevee(RuntimeError):
    """Logto n'a pas tout fermé : une session ou une autorisation peut rester ouverte.
    Levée telle quelle, jamais avalée — c'est à l'appelant de dire « à refaire »."""


def fermer_sessions(sub: str) -> dict:
    """Ferme TOUTES les sessions Logto de `sub` (annuaire primaire), autorisations
    comprises, puis révoque les autorisations qui survivent à leur session. Rend
    `{"sessions": n, "grants": m}` (ce qui a été fermé). Lève `FermetureInachevee` si
    Logto refuse ou ne répond pas dans `_DELAI_FERMETURE_S`.

    Deux temps, parce qu'aucun ne suffit seul (Management API Logto 1.44) :
    1. `DELETE /api/users/{id}/sessions/{uid}?revokeGrantsTarget=all` ferme la session
       ET les autorisations qu'elle porte, premières et tierces parties comprises ;
    2. `GET /api/users/{id}/grants` puis `DELETE …/grants/{grantId}` : une autorisation
       hors session (`offline_access`, dont la session a expiré) garde son jeton de
       rafraîchissement — c'est elle que le premier temps ne voit pas.

    Un 404 sur un `DELETE` dit que la cible a disparu entre la liste et la suppression
    (session échue, autorisation déjà révoquée) : l'état voulu est atteint, il n'est
    pas compté. Tout autre refus lève."""
    import requests
    from urllib.parse import quote

    from ..tenancy import require_primary_tenant
    require_primary_tenant(sub, "fermeture des sessions Logto")
    fin = time.monotonic() + _DELAI_FERMETURE_S
    try:
        tok = _mgmt_token()
    except (RuntimeError, requests.RequestException) as e:
        # Credential de management absent, ou refusé : rien n'a été fermé.
        raise FermetureInachevee(
            f"fermeture des sessions de {sub} : pas de jeton de management ({e})") from e
    base = _logto_base()
    h = {"Authorization": f"Bearer {tok}", "User-Agent": _UA}
    compte = f"{base}/api/users/{quote(sub, safe='')}"

    def _appel(methode: str, url: str, **kw):
        reste = fin - time.monotonic()
        if reste <= 0:
            raise FermetureInachevee(
                f"fermeture des sessions de {sub} : délai de {_DELAI_FERMETURE_S} s "
                "dépassé, la suite n'est pas fermée")
        try:
            r = requests.request(methode, url, headers=h,
                                 timeout=min(_DELAI_REQUETE_S, reste), **kw)
        except requests.RequestException as e:
            raise FermetureInachevee(
                f"fermeture des sessions de {sub} : Logto injoignable ({e})") from e
        if methode == "DELETE" and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise FermetureInachevee(
                f"fermeture des sessions de {sub} : {methode} {url.removeprefix(base)} "
                f"→ {r.status_code}")
        return r

    sessions = [s["payload"]["uid"]
                for s in _appel("GET", f"{compte}/sessions").json().get("sessions", [])]
    fermees = sum(1 for uid in sessions
                  if _appel("DELETE", f"{compte}/sessions/{quote(uid, safe='')}",
                            params={"revokeGrantsTarget": "all"}) is not None)
    grants = [g["id"] for g in _appel("GET", f"{compte}/grants").json().get("grants", [])]
    revoques = sum(1 for gid in grants
                   if _appel("DELETE", f"{compte}/grants/{quote(gid, safe='')}")
                   is not None)
    return {"sessions": fermees, "grants": revoques}


class SuppressionInachevee(RuntimeError):
    """Logto n'a pas supprimé le compte. Levée telle quelle, jamais avalée : l'effacement
    d'org qui l'appelle reste à refaire (`docs/effacement-d-org.md`)."""


def supprimer_compte(sub: str) -> bool:
    """Supprime le compte `sub` de NOTRE annuaire (Management API `DELETE /api/users/{id}`).
    Rend True s'il a été supprimé, False s'il n'existait déjà plus (404 : l'état voulu
    est atteint). Lève `SuppressionInachevee` sur tout autre refus.

    Le seul appelant est l'effacement d'une org, pour les comptes dont c'était la SEULE
    org (décision du 12/11/2026) : un geste irréversible, posé après la confirmation du
    titulaire et le délai de grâce — jamais un raccourci de « mise en pause »
    (`docs/comptes-en-pause.md`)."""
    import requests
    from urllib.parse import quote

    from ..tenancy import require_primary_tenant
    require_primary_tenant(sub, "suppression d'un compte Logto")
    try:
        tok = _mgmt_token()
        r = requests.delete(f"{_logto_base()}/api/users/{quote(sub, safe='')}",
                            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA},
                            timeout=_DELAI_REQUETE_S)
    except (RuntimeError, requests.RequestException) as e:
        raise SuppressionInachevee(f"suppression du compte {sub} : {e}") from e
    if r.status_code == 404:
        return False
    if r.status_code >= 400:
        raise SuppressionInachevee(f"suppression du compte {sub} : DELETE → {r.status_code}")
    return True


def supprimer_organisation(logto_org_id: str) -> bool:
    """Supprime l'organisation Logto miroir d'une org (`mfa_mirror`). False = déjà
    partie (404). Lève `SuppressionInachevee` sur tout autre refus."""
    import requests
    from urllib.parse import quote
    try:
        tok = _mgmt_token()
        r = requests.delete(f"{_logto_base()}/api/organizations/{quote(logto_org_id, safe='')}",
                            headers={"Authorization": f"Bearer {tok}", "User-Agent": _UA},
                            timeout=_DELAI_REQUETE_S)
    except (RuntimeError, requests.RequestException) as e:
        raise SuppressionInachevee(f"suppression de l'organisation {logto_org_id} : {e}") from e
    if r.status_code == 404:
        return False
    if r.status_code >= 400:
        raise SuppressionInachevee(
            f"suppression de l'organisation {logto_org_id} : DELETE → {r.status_code}")
    return True
