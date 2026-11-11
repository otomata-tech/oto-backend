"""Les listes d'outils enregistrées suivent DataForSEO, servi par sa description.

Le 11/11/2026, DataForSEO passe au lecteur générique de sa description
(`tools/decrits.py`) : ses huit outils écrits à la main (`dataforseo_keywords`,
`dataforseo_domain`, `dataforseo_backlinks`, `dataforseo_account`,
`dataforseo_ai_visibility`, `dataforseo_web_mentions`, `dataforseo_site_audit`,
`dataforseo_technologies`) deviennent UN outil, `dataforseo`, dont l'`op` est la fonction
décrite. Cette révision lit `outils_retires.RENOMMAGES_DECRITS`, sans la recopier.

**Le sens : les deux listes reçoivent `dataforseo`** (`outils_retires.remplacants_decrits`).
Une liste qui MASQUE un ancien outil masque `dataforseo` ; une liste qui AUTORISE un ancien
outil autorise `dataforseo`, c'est-à-dire tout DataForSEO — les mêmes lectures et le même
crawl de site qu'avant, quelques fonctions de plus (résultats Google, fiche et avis d'un
établissement) : il n'existe pas d'outil plus étroit à donner, le connecteur et sa clé
sont les mêmes.

La mécanique (tables, réécriture sur place sans doublon, travaux vivants du runner) est
celle de `20261021_1219_outils_act_msg`, chargée telle quelle. Le journal des appels et les
travaux TERMINÉS ne sont pas réécrits.

**Ordre avec le code : APRÈS le déploiement, ou juste avant** (cf. 0062). Prod et préprod
partagent la base : la jouer au tag de prod.

Retour arrière : aucun — les anciens noms ne sont plus servis.

Révision : 20261111_1930_dataforseo_decrit
Précédente : 20261111_1510_cell_sources
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic import context, op

revision = "20261111_1930_dataforseo_decrit"
down_revision = "20261111_1510_cell_sources"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"


def _revision_des_messageries():
    """La révision des messageries, pour sa mécanique (`reecrire_les_listes`) — sa table à
    elle, `RENOMMAGES_MESSAGERIES`, n'est pas lue."""
    chemin = Path(__file__).with_name("20261021_1219_outils_act_msg.py")
    spec = importlib.util.spec_from_file_location("revision_outils_act_msg", chemin)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def correspondances() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """`(autorise, masque)` de la bascule de DataForSEO : le même remplaçant des deux côtés."""
    from oto_mcp import outils_retires
    anciens = dict.fromkeys(r.ancien for r in outils_retires.RENOMMAGES_DECRITS
                            if r.depuis == outils_retires.LOT_DATAFORSEO)
    table = {a: outils_retires.remplacants_decrits(a) for a in anciens}
    return table, dict(table)


def upgrade() -> None:
    if context.is_offline_mode():
        # L'essai à blanc (`--sql`) n'a pas de base à lire (cf. 0062).
        return
    op.execute(_ATTENTE_MAX)
    _revision_des_messageries().reecrire_les_listes(op.get_bind(), *correspondances(),
                                                    revision)


def downgrade() -> None:
    raise NotImplementedError(
        f"{revision} n'a pas de retour arrière : les anciens noms ne sont plus servis "
        "(cf. docstring).")
