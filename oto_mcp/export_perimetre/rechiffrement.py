"""Le rechiffrement des secrets d'un export : CHEZ NOUS, À L'EXPORT (décision du 28/09/2026).

Notre clé maîtresse ne sort jamais de notre infrastructure. Le processus d'export reçoit
la clé de l'instance CIBLE pour cette seule exécution, déchiffre chaque secret sous
notre clé, le rechiffre sous la sienne, et le fichier ne porte QUE des secrets chiffrés
pour la cible. L'import ne connaît que la clé de son instance ; le manifeste dit sous
quelle clé les secrets sont chiffrés par une EMPREINTE (`empreinte_cle`), jamais la clé.

`COLONNES_CHIFFREES` est la liste UNIQUE des valeurs chiffrées par `crypto` (la clé
de l'instance) : `tests/export_perimetre/test_colonnes_chiffrees.py` refuse une colonne
`*_enc` du schéma ou un appel à `crypto.encrypt` qu'elle ne couvre pas. Leur AAD lie le
chiffré à SA ligne, et c'est la fonction du code qui l'écrit qui la dit — jamais une
recopie ici :

- `connector_credentials.secret_enc` : `credentials_store._aad(entity_type, entity_id,
  connector, account)` — l'`entity_id` d'un compte ou d'un membre CONTIENT le sub, qui
  change à l'import : on déchiffre sous l'AAD de la ligne SOURCE, on rechiffre sous
  celle de la ligne CIBLE (`transformation`, la fonction même de l'import). Les clés
  d'instance (palier PLATFORM) y vivent aussi ;
- `runner_triggers.hook_signing_secret_enc` : `runner_hook._aad_du_secret(id)` ;
- `transcription_jobs.api_key_enc` : `transcription_worker._aad(audio_key)` ;
- `docs.public_token_enc` : `db.jetons_de_lien.aad_page(id)` — le lien public d'une page ;
- `process_shares.token_enc` : `db.jetons_de_lien.aad_partage(id)` — le lien d'une
  procédure partagée. Leur empreinte (`*_hash`) voyage telle quelle : un lien continue de
  répondre sur la cible ;
- les satellites scellés `connector_credentials.meta.<nom>_enc`
  (`credentials_store.aad_satellite`) : HORS de `AAD`, l'export ne les rechiffre pas.

Ces cinq colonnes (`AAD`, `classement.secrets`) sont celles que l'export rechiffre.

⚠️ Le clair ne vit que le temps d'un appel : ni fichier, ni journal, ni exception qui
le porterait (`crypto.decrypt` et `decrypt_with_key` lèvent sans le citer).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .. import credentials_store, runner_hook, transcription_worker, youtube_upload_worker
from ..db import jetons_de_lien
from .. import crypto
from ..crypto import decrypt_with_key, empreinte_cle, encrypt_with_key  # noqa: F401 — empreinte_cle : rendue aux lecteurs du module

#: table → (colonne chiffrée, AAD de la ligne).
AAD: dict[str, tuple[str, Callable[[dict], str]]] = {
    "connector_credentials": ("secret_enc", lambda l: credentials_store._aad(
        l["entity_type"], l["entity_id"], l["connector"], l["account"] or "")),
    "runner_triggers": ("hook_signing_secret_enc",
                        lambda l: runner_hook._aad_du_secret(l["id"])),
    "transcription_jobs": ("api_key_enc", lambda l: transcription_worker._aad(l["audio_key"])),
    "docs": ("public_token_enc", lambda l: jetons_de_lien.aad_page(l["id"])),
    "process_shares": ("token_enc", lambda l: jetons_de_lien.aad_partage(l["id"])),
}



@dataclass(frozen=True)
class Chiffree:
    """Une valeur chiffrée par `crypto` : la colonne `colonne` de `table`, ou, si
    `cle_meta` est donnée, la clé `cle_meta` de sa colonne JSONB `colonne`."""
    table: str
    colonne: str
    aad: Callable[[dict], str]
    cle_meta: Optional[str] = None

    @property
    def nom(self) -> str:
        return ".".join(filter(None, (self.table, self.colonne, self.cle_meta)))


def _satellite(nom: str) -> Chiffree:
    return Chiffree("connector_credentials", "meta", lambda l: credentials_store.aad_satellite(
        l["entity_type"], l["entity_id"], l["connector"], l["account"] or "", nom),
        cle_meta=f"{nom}_enc")


#: Toutes les valeurs chiffrées sous la clé de l'instance : la source unique.
COLONNES_CHIFFREES: tuple[Chiffree, ...] = (
    *(Chiffree(t, c, a) for t, (c, a) in AAD.items()),
    # Hors d'`AAD` : la table ne part pas à l'export (`classement`, exclue) — la session
    # d'un envoi en cours ne vaut que sur cette instance. Comptée et rechiffrée au
    # changement de clé comme les autres.
    Chiffree("youtube_upload_jobs", "session_url_enc",
             lambda l: youtube_upload_worker._aad(l["id"])),
    *(_satellite(nom) for nom in credentials_store.SEALED_SATELLITES),
)



def compter_key_refs(conn) -> dict[str, dict[str, int]]:
    """Par valeur chiffrée (`Chiffree.nom`), le nombre de lignes par `key_ref`
    (« 0x01 »…), lu dans la base : le premier octet des quatre premiers caractères base64
    de l'enveloppe, rien d'autre ne sort. Une enveloppe qui n'a pas la forme base64 compte
    sous « illisible ». Une seule requête ; la borne de temps est celle de l'appelant."""
    parties, params = [], []
    for c in COLONNES_CHIFFREES:
        valeur, p = (c.colonne, []) if c.cle_meta is None else (f"({c.colonne} ->> %s)",
                                                                 [c.cle_meta])
        parties.append(
            f"SELECT %s::text AS nom, CASE WHEN {valeur} ~ '^[A-Za-z0-9+/]{{4}}' "
            f"THEN get_byte(decode(left({valeur}, 4), 'base64'), 0) END AS key_ref, "
            f"count(*) AS n FROM {c.table} WHERE {valeur} IS NOT NULL GROUP BY 2")
        params += [c.nom, *p, *p, *p]   # dans l'ordre du texte : CASE, decode, WHERE
    compte: dict[str, dict[str, int]] = {c.nom: {} for c in COLONNES_CHIFFREES}
    for r in conn.execute(" UNION ALL ".join(parties), params).fetchall():
        k = "illisible" if r["key_ref"] is None else f"0x{r['key_ref']:02x}"
        compte[r["nom"]][k] = int(r["n"])
    return compte


class RechiffrementImpossible(RuntimeError):
    pass


def rechiffrer(table: str, source: dict, cible: dict, cle_cible: bytes) -> str | None:
    """L'enveloppe du secret de `table`, déchiffrée par le trousseau de cette instance
    sous l'AAD de la ligne `source`, rechiffrée sous `cle_cible` et l'AAD de la ligne
    `cible` (celle que l'import écrira)."""
    colonne, aad = AAD[table]
    enveloppe = source.get(colonne)
    if enveloppe is None:
        return None
    try:
        clair = crypto.decrypt(enveloppe, aad(source))
    except RuntimeError as e:
        raise RechiffrementImpossible(
            f"{table} : une valeur ne se déchiffre pas sous la clé de cette instance "
            "(enveloppe corrompue, ou ligne dont l'identité a bougé)") from e
    return encrypt_with_key(cle_cible, clair, aad(cible))


def lisible(table: str, ligne: dict, cle: bytes) -> bool:
    """Le secret de `ligne` se déchiffre-t-il sous `cle` et l'AAD de cette ligne ?"""
    colonne, aad = AAD[table]
    if ligne.get(colonne) is None:
        return True
    try:
        decrypt_with_key(cle, ligne[colonne], aad(ligne))
    except RuntimeError:
        return False
    return True
