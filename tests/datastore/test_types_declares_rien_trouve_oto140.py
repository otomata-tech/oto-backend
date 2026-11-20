"""oto#140 — le refus de type ne dit plus « omets la colonne » pour « rien trouvé ».

Contrat d'écriture : « cherché, rien » s'écrit `@empty`, la raison dans `comment` ;
omettre la colonne veut dire « je n'y touche pas ». Les refus `email` et `phone`
disaient l'inverse (« Si aucune adresse / aucun numéro n'a été trouvé, OMETS la
colonne ») — et c'est
précisément le cas qu'il aiguille : une phrase d'absence écrite dans la case valeur.
Chaque type armé porte désormais les deux gestes, chacun à sa place."""
from __future__ import annotations

import pytest

from oto_mcp.datastore.types_declares import TYPES_ARMES, types_trahis

_FAUX = {"number": "non trouvé", "bool": "non trouvé", "date": "non trouvé",
         "datetime": "non trouvé", "email": "non trouvé", "phone": "non trouvé",
         "ref": "non trouvé"}


def _colonne(ftype: str, cible: int = 1) -> dict:
    """La colonne `c` typée ; un lien (`ref`) déclare sa cible, sans quoi il ne se pose pas."""
    return {"key": "c", "type": ftype, **({"target": cible} if ftype == "ref" else {})}


def test_chaque_type_arme_a_une_valeur_fautive():
    assert set(_FAUX) == set(TYPES_ARMES)


@pytest.mark.parametrize("ftype", TYPES_ARMES)
def test_le_refus_dit_empty_pour_rien_trouve_et_omettre_pour_ne_pas_toucher(ftype):
    schema = {"fields": [_colonne(ftype)]}
    [refus] = types_trahis(schema, {"c": _FAUX[ftype]})
    assert ("if nothing was found, write `@empty` with the reason in `c.comment`; "
            "to leave the cell untouched, omit the column") in refus, refus
    # L'ancienne consigne envoyait omettre la colonne pour dire « rien trouvé ».
    assert "OMIT" not in refus and "an absence is not written" not in refus, refus


@pytest.mark.parametrize("ftype", TYPES_ARMES)
def test_le_geste_conseille_passe_sur_chaque_type(live, ftype):
    """La consigne se suit : `@empty` et sa raison sont acceptés par la colonne typée
    qui venait de refuser la phrase d'absence — sinon le refus enverrait vers un
    second refus."""
    import uuid

    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    sub = "usr_rien_trouve_140"
    db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    nom = "t140-" + uuid.uuid4().hex[:6]
    ns = str(db.create_datastore("user", sub, nom))
    st = make_store(sub)
    # Un lien vise un tableau que l'auteur lit : la cible est née avec la colonne.
    cible = db.create_datastore("user", sub, nom + "-cible") if ftype == "ref" else 1
    st.set_schema(ns, {"fields": [_colonne(ftype, cible)]})
    rid = st.append_row(ns, {"c": {"valeur": "@empty", "comment": "annuaire : rien"}})["_id"]
    assert st.get_row(ns, rid, layers="nested")["c"]["comment"] == "annuaire : rien"
