# Logos d'éditeur des connecteurs

Déposer ici un fichier par connecteur, nommé d'après son `name` au registre
(`oto_mcp/providers.py`) : `<name>.png` (ou `.webp`/`.jpg`).

Exemples : `serper.png`, `pennylane.png`, `sirene.png`, `folk.png`.

`scripts/seed_connector_logos.py` les uploade sur Scaleway Object Storage
(bucket `oto-media`) sous la clé conventionnelle `connector-logos/<name>.png`,
servie publiquement. `media_store.connector_logo_url(name)` dérive l'URL ;
`Connector.logo_url_for()` la renvoie dans `/api/connectors`.

Un connecteur sans asset n'a simplement pas de logo (placeholder côté UI).
Préférer des PNG carrés ~128–256 px, fond transparent.
