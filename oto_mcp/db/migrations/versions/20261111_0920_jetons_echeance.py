"""Les jetons `oto_` sans échéance reçoivent la leur : 90 jours après cette migration.

Un jeton neuf vit au plus 90 jours (`db.tokens.TTL_MAX_JOURS`) ; ceux d'avant le
plafond pouvaient être NULL, c'est-à-dire éternels — un mot de passe changé ne les
coupait pas. Décision du 10/11/2026 : ils expirent 90 jours après la mise en prod.

**Ce qui change.** `expires_at = NOW() + 90 jours` (un seul instant, celui de la
transaction) sur chaque ligne de `user_api_tokens` NON révoquée dont `expires_at` est
NULL, toutes natures comprises : `user`, `org` (clés d'org) et `delegation` (qui en ont
toujours une — aucune n'est touchée en pratique, aucune raison de les exclure). Une
ligne révoquée garde sa trace telle quelle : elle ne s'authentifie déjà plus.

**Verrous.** `RowExclusiveLock` sur `user_api_tokens` et le verrou des seules lignes
visées ; `verify_api_token` met à jour `last_used_at` à chaque appel, d'où
`lock_timeout` 5 s : un échec net, qu'on rejoue, plutôt qu'une file d'authentifications
bloquées.

**Ordre avec le code : indifférent.** L'ancien code lit `expires_at` comme le nouveau ;
le plafond des jetons neufs vit dans le code, pas ici.

**Retour arrière : impossible, et il le dit.** Après la migration, rien ne distingue
une ligne qu'elle a bornée d'un jeton émis le même jour avec 90 jours d'échéance.
Remettre NULL sur une approximation rendrait éternels des jetons qui ne l'ont jamais
été ; le retour lève `RetourImpossible`.

Révision : 20261111_0920_jetons_echeance
Précédente : 20261109_2120_alertes_slack
"""
from __future__ import annotations

from alembic import op

revision = "20261111_0920_jetons_echeance"
down_revision = "20261109_2120_alertes_slack"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

#: La valeur de `db.tokens.TTL_MAX_JOURS` au jour de la révision — écrite en dur : une
#: révision rejoue ce qu'elle a fait, pas ce que le code dira demain.
_JOURS = 90

BORNER = (
    "UPDATE user_api_tokens "
    f"SET expires_at = NOW() + INTERVAL '{_JOURS} days' "
    "WHERE expires_at IS NULL AND revoked_at IS NULL")


class RetourImpossible(RuntimeError):
    """La révision ne se défait pas : les lignes qu'elle a bornées ne se reconnaissent
    plus."""


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(BORNER)


def downgrade() -> None:
    raise RetourImpossible(
        "20261111_0920_jetons_echeance est irréversible : rien ne distingue un jeton "
        "qu'elle a borné d'un jeton émis avec 90 jours d'échéance, et remettre NULL "
        "rendrait éternels des jetons qui ne l'ont jamais été. Pour descendre sous elle, "
        "`alembic stamp 20261109_2120_alertes_slack` (les échéances posées restent).")
