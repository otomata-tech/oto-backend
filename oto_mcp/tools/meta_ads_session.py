"""Meta Ads — le jeton d'un appel, et la traduction des refus.

L'autre moitié du connecteur : `auth/meta_ads.py` porte l'ACQUISITION ; ici on
sert. Pas de renouvellement (jeton BISU sans échéance, cf. `auth/meta_ads.py`).

Quatre refus, quatre gestes — et Meta les rend tous en 400 :
pas de compte connecté (autoriser), autorisation morte (reconnecter — la ligne de
coffre est marquée pour que la fiche le dise), débit atteint (attendre), panne
(réessayer).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from mcp.types import ErrorData, INVALID_PARAMS

from ..auth import meta_ads as ads_auth
from ..auth.meta_ads import CONNECTOR, _coeur, _ctx_org, _row, _scope
from ..connectors import health as connector_health
from ..mcp_errors import McpError

if TYPE_CHECKING:  # l'annotation seulement — jamais évaluée à l'exécution
    from oto.tools.meta_ads import MetaAdsClient

logger = logging.getLogger("oto_mcp.tools.meta_ads")

_RECONNECTER = ("Reconnect it from your connectors page, connector « Meta Ads » — "
                "and check that the ad accounts are still granted.")


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def resolve_token(sub: str) -> str:
    """Le jeton du compte connecté, ou un refus qui dit le geste."""
    org_id = _ctx_org(sub)
    row = _row(org_id, sub)
    if not row or not row.get("secret"):
        from .. import config
        raise RuntimeError(
            "No Meta Ads account connected. Authorize oto from your connectors page "
            f"({config.dashboard_url_for(sub)}/, connector « Meta Ads ») and pick "
            "the ad accounts to share.")
    return row["secret"]


def _marquer_mort(message: str) -> None:
    """Inscrit le rejet sur la ligne de coffre de l'appelant : la fiche dira « à
    reconnecter » sans attendre le prochain appel. Ne lève jamais — le refus, lui,
    part quand même. Synchrone : joué au fil d'exécution (il lit le contexte et
    écrit en base)."""
    from .. import access

    try:
        sub = access.current_user_sub_or_raise()
        entity_type, entity_id = _scope(_ctx_org(sub), sub)
        connector_health.mark_rejected(entity_type, entity_id, CONNECTOR, "", message)
    except Exception:  # noqa: BLE001 — un marquage raté ne masque pas le refus
        logger.warning("meta_ads : marquage de santé en échec", exc_info=True)


def _jeton_de_l_appelant() -> str:
    """`resolve_token` pour l'appelant courant — tout hors boucle : la résolution
    du sub et de l'org touche la base (`to_thread` copie le contexte)."""
    from .. import access

    return resolve_token(access.current_user_sub_or_raise())


async def _client() -> MetaAdsClient:
    """Le client Meta Ads de CET appelant.

    Le nom et l'annotation NON quotée sont un contrat : la sonde de version-skew
    (`tests/test_tools_client_methods_exist.py`) reconnaît les fabriques `_client`
    pour vérifier, au tag oto-core épinglé, les méthodes appelées par les outils.
    """
    try:
        jeton = await asyncio.to_thread(_jeton_de_l_appelant)
    except RuntimeError as e:
        raise _bad(str(e)) from e
    return _coeur().MetaAdsClient(jeton)


async def appeler(geste: str, fn, *args, **kwargs):
    """Joue un appel du cœur hors de la boucle et traduit ses refus.

    ⚠️ Seul le texte des erreurs RÉDIGÉES par le cœur (`MetaAdsError`, sans URL ni
    corps brut) traverse ; pour le reste on rend le TYPE — une exception d'une
    couche intermédiaire peut porter une URL ou un en-tête."""
    coeur = _coeur()
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except coeur.MetaAdsAuthExpired as e:
        message = f"Meta no longer accepts this authorization. {_RECONNECTER}"
        await asyncio.to_thread(_marquer_mort, message)
        raise _bad(message) from e
    except coeur.MetaAdsThrottled as e:
        raise _bad(str(e)) from e
    except (coeur.MetaAdsError, ValueError) as e:
        raise _bad(f"Meta could not serve {geste}: {e}") from e
    except Exception as e:
        logger.warning("meta_ads : %s a échoué — %s", geste, type(e).__name__)
        raise _bad(
            f"Meta did not answer {geste} ({type(e).__name__}). This is not an "
            "authorization refusal — retry.") from e


def avertir_au_demarrage() -> None:
    """Ce que l'exploitant doit savoir AU BOOT, en une ligne. Ne lève jamais."""
    ads_auth.avertir_au_demarrage()
    try:
        _coeur()
    except RuntimeError as e:
        logger.warning(
            "meta_ads : connecteur monté mais le cœur n'est pas installé — les "
            "outils `meta_ads_*` refuseront en le disant. Détail : %s", e)
