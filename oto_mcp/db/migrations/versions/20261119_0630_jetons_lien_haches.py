"""Les jetons de lien sont stockés hachés, comme les invitations.

Deux liens rangeaient leur jeton tel quel : le lien public d'une page
(`docs.public_token`) et le lien d'une procédure partagée (`process_shares.token`).
Le code de ce lot ne range plus que leur haché — `docs.public_token_hash`,
`process_shares.token_hash`, la fonction des invitations (`db.tokens._hash_token`,
SHA-256 en hexadécimal) — et résout un lien présenté par ce haché.

**Ce que fait la révision**, sur une base où le code de ce lot a démarré ou non :

1. pose les deux colonnes `*_hash` si elles manquent (le démarrage les pose aussi, avec
   leurs index uniques) ;
2. remplit le haché de chaque lien existant — `encode(sha256(convert_to(jeton,
   'UTF8')), 'hex')`, l'empreinte que `_hash_token` calcule en Python, sans extension :
   un lien déjà distribué continue de répondre ;
3. sur la fiche d'un compte né d'un partage, remplace le jeton noté sous
   `acquired_via` par l'identifiant du partage (`share_id`, ce que le code de ce lot
   écrit), et retire tout jeton qui y resterait ;
4. retire des nœuds la copie du jeton qu'y avait posée la recopie des pages
   (`nodes.props->'public_token'`, recopie arrêtée au démarrage depuis le 01/09/2026) ;
5. retire les colonnes en clair `docs.public_token` (et son index) et
   `process_shares.token` (et son unicité) — §5.3 : un retrait passe par une révision.

Idempotente : chaque étape constate le catalogue ou ne vise que les lignes à faire ;
rejouée, elle ne fait rien.

**Verrous.** `RowExclusiveLock` sur les lignes visées (une poignée), puis
`AccessExclusiveLock` bref sur `docs` et `process_shares` pour les `DROP COLUMN`
(catalogue seul, aucune réécriture). `lock_timeout` 5 s : un échec net, qu'on rejoue.

⚠️ **Ordre avec le code : APRÈS LE TAG** (`docs/migrations-versionnees.md` §5.1). Le
code d'avant lit `docs.public_token` à chaque lecture de page : jouée pendant qu'il sert
encore, la révision le casserait. Le code de ce lot ne lit plus les colonnes en clair, et
son démarrage lève la contrainte `NOT NULL` de `process_shares.token` pour pouvoir
publier avant la révision. Entre le tag et la révision, un lien émis AVANT ne répond
pas (son haché n'est pas encore rempli) ; la révision le rouvre.

Retour arrière : lève — les jetons en clair ne sont plus nulle part.

Révision : 20261119_0630_jetons_lien_haches
Précédente : 20261118_1500_unipile_slots
"""
from __future__ import annotations

from alembic import op

revision = "20261119_0630_jetons_lien_haches"
down_revision = "20261118_1500_unipile_slots"
branch_labels = None
depends_on = None

_ATTENTE_MAX = "SET LOCAL lock_timeout = '5s'"

#: L'empreinte de `db.tokens._hash_token`, en SQL (PostgreSQL ≥ 11, sans extension).
_HACHE = "encode(sha256(convert_to({}, 'UTF8')), 'hex')"


def _colonne(table: str, colonne: str) -> str:
    return (f"EXISTS (SELECT 1 FROM information_schema.columns "
            f"WHERE table_schema = current_schema() AND table_name = '{table}' "
            f"AND column_name = '{colonne}')")


UPGRADE = f"""
ALTER TABLE docs ADD COLUMN IF NOT EXISTS public_token_hash TEXT;
ALTER TABLE process_shares ADD COLUMN IF NOT EXISTS token_hash TEXT;
DO $$
BEGIN
  IF {_colonne('docs', 'public_token')} THEN
    UPDATE docs SET public_token_hash = {_HACHE.format('public_token')}
     WHERE public_token IS NOT NULL AND public_token_hash IS NULL;
    DROP INDEX IF EXISTS idx_docs_public_token;
    ALTER TABLE docs DROP COLUMN public_token;
  END IF;
  IF {_colonne('process_shares', 'token')} THEN
    UPDATE process_shares SET token_hash = {_HACHE.format('token')}
     WHERE token IS NOT NULL AND token_hash IS NULL;
    UPDATE user_account_profile u
       SET profile = jsonb_set(u.profile #- '{{acquired_via,token}}',
                               '{{acquired_via,share_id}}', to_jsonb(s.id))
      FROM process_shares s
     WHERE u.profile->'acquired_via'->>'kind' = 'process_share'
       AND u.profile->'acquired_via'->>'token' = s.token;
    ALTER TABLE process_shares DROP COLUMN token;
  END IF;
END $$;
UPDATE user_account_profile SET profile = profile #- '{{acquired_via,token}}'
 WHERE profile->'acquired_via'->>'kind' = 'process_share'
   AND profile->'acquired_via' ? 'token';
UPDATE nodes SET props = props - 'public_token' WHERE props ? 'public_token';
"""


def upgrade() -> None:
    op.execute(_ATTENTE_MAX)
    op.execute(UPGRADE)


def downgrade() -> None:
    raise RuntimeError(
        "20261119_0630_jetons_lien_haches est irréversible : les jetons en clair ne "
        "sont plus rangés nulle part, seul leur haché l'est.")
