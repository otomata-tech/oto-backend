"""Les agents posés sur un modèle Claude d'avant passent à la génération 5.5.

Le catalogue (`runner_models.MODELES`) place depuis le 11/11/2026 `claude-sonnet-5-5`,
`claude-opus-5-5` et `claude-haiku-5-5` DEVANT `claude-sonnet-5`, `claude-opus-5` et
`claude-haiku-4-5`, qui y restent, servis. Cette révision fait passer les agents déjà
posés sur un id d'avant à son successeur.

**Ce qui change.** Sur `runner_triggers.model`, `runner_triggers.forfait_prete_model` et
`runner_fleets.model`, chaque id d'avant devient son successeur (`_REMPLACES`, la table
de cette révision et d'elle seule). Les deux colonnes d'un déclencheur bougent dans la
MÊME écriture : un prêt de forfait n'est vivant que si `forfait_prete_model = model`
(`db.runner_triggers._COLS`) — réécrire l'une sans l'autre le rendrait caduc.
`updated_at`/`updated_by` ne bougent pas : personne n'a retouché l'agent.

**Ce qui ne change PAS : `runner_jobs`.** Un travail enfilé porte déjà sa charge
(`model`, `model_family`, `effort`) ; il se route par `model_family` (`anthropic`,
inchangée), et Anthropic sert toujours les ids d'avant — il tourne tel quel. Un `continue`
reprend À DESSEIN le modèle du départ de son run (`runner_jobs.modele_du_run`) : réécrire
l'historique ferait poursuivre un fil sur un autre modèle que celui où il a commencé.

**Verrous.** `RowExclusiveLock` et le verrou des seules lignes visées ; le tick lit et
écrit `runner_triggers` en continu, d'où `lock_timeout` 5 s : un échec net, qu'on rejoue.

⚠️ **Ordre avec le code : APRÈS LE TAG** (`docs/migrations-versionnees.md` §5.1 : une
révision se joue à la main sur notre box). Prod et préprod partagent la base, et le tick
des déclencheurs tourne dans les deux. Jouée avant que la prod serve ce catalogue, elle
lui ferait lire des ids 5.5 inconnus : chaque livraison de ces agents serait refusée
(`motif_sans_famille`) jusqu'au tag. D'où : fusion -> tag de prod portant ce catalogue ->
`oto-mcp migrer upgrade head` -> comptage (plus aucun agent sur un id d'avant). Avant le
geste, la base ne change pas et les deux processus connaissent les ids d'avant ; après,
les deux connaissent les ids 5.5. Une instance cible, qui migre à la montée, prend le code
et la révision ensemble : rien à attendre.

**Retour arrière.** La correspondance inverse, sur les mêmes colonnes. Un agent posé sur
un modèle 5.5 APRÈS la révision redescend lui aussi : un retour arrière se joue avant de
ramener un code qui ne connaîtrait pas les ids 5.5.

Révision : 20261111_1400_modeles_claude_55
Précédente : 20261111_0920_jetons_echeance
"""
from __future__ import annotations

from alembic import op

revision = "20261111_1400_modeles_claude_55"
down_revision = "20261111_0920_jetons_echeance"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

#: La correspondance au jour de la révision — écrite en dur ici, et nulle part ailleurs :
#: une révision rejoue ce qu'elle a fait, pas ce que le code dira demain.
_REMPLACES = (
    ("claude-sonnet-5", "claude-sonnet-5-5"),
    ("claude-opus-5", "claude-opus-5-5"),
    ("claude-haiku-4-5", "claude-haiku-5-5"),
)


def _cas(colonne: str, paires) -> str:
    branches = " ".join(f"WHEN '{de}' THEN '{vers}'" for de, vers in paires)
    return f"CASE {colonne} {branches} ELSE {colonne} END"


def _liste(ids) -> str:
    return ", ".join(f"'{i}'" for i in ids)


def ordres(paires) -> list[str]:
    """Les UPDATE qui portent `paires` (`(de, vers)`) sur les colonnes des agents."""
    de = _liste(d for d, _ in paires)
    return [
        "UPDATE runner_triggers "
        f"SET model = {_cas('model', paires)}, "
        f"forfait_prete_model = {_cas('forfait_prete_model', paires)} "
        f"WHERE model IN ({de}) OR forfait_prete_model IN ({de})",
        f"UPDATE runner_fleets SET model = {_cas('model', paires)} WHERE model IN ({de})",
    ]


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ordre in ordres(_REMPLACES):
        op.execute(ordre)


def downgrade() -> None:
    op.execute(_ATTENTE_MAX)
    for ordre in ordres(tuple((vers, de) for de, vers in _REMPLACES)):
        op.execute(ordre)
