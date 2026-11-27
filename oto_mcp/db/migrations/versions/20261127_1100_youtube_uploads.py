"""La file des mises en ligne YouTube d'une vidéo déposée sur un projet.

Une table NEUVE, née entière : `youtube_upload_jobs`
(`db/schema/youtube.py::YOUTUBE_UPLOAD_JOBS`) — posée aussi par le démarrage
(`CREATE TABLE IF NOT EXISTS` de l'assemblage), même DDL. FK vers `projects` et
`project_files` (ON DELETE CASCADE). Écrite par `youtube_act op=upload_video` quand la
vidéo est un gros fichier de projet, lue et avancée par `youtube_upload_worker`.

**Verrous.** Les FK neuves prennent un `SHARE ROW EXCLUSIVE` bref sur `projects` et
`project_files` (aucune ligne à valider : la table naît vide).

**Ordre avec le code : avant le code.** Le code de ce lot écrit et lit la table ;
l'ancien code l'ignore.

Retour arrière : retire la table. Les mises en ligne en cours s'arrêtent ; la vidéo
reste dans le projet, une nouvelle demande la renvoie.

Révision : 20261127_1100_youtube_uploads
Précédente : 20261127_1000_file_expiry
"""
from __future__ import annotations

from alembic import op

from oto_mcp.db.schema.youtube import YOUTUBE_UPLOAD_JOBS

revision = "20261127_1100_youtube_uploads"
down_revision = "20261127_1000_file_expiry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(YOUTUBE_UPLOAD_JOBS)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS youtube_upload_jobs")
