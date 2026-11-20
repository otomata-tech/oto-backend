"""Trois refus JUSTES qui ne sortaient pas comme des refus (05/09/2026).

Le fil commun, et il vaut mieux que chacun des trois : **un refus qui ne nomme pas sa
cause fait travailler l'appelant sur une hypothèse, et l'hypothèse coûte plus cher que
le refus.** Une session a cru à une perte de données sur le premier ; une autre a
travaillé une demi-heure sur une table sans schéma à cause du second.

1. **Le bail** (`RowLocked`) héritait d'`Exception`, donc aucune face REST ne
   l'attrapait : un refus juste sortait en **500 au corps vide**. La face MCP, elle,
   traduisait correctement depuis toujours.

   ⚠️ **Le code le SAVAIT.** La docstring de `BusinessKeyRequired`, écrite avant, nomme
   ce cas : « sans cet héritage, le refus ressortirait en Erreur interne du serveur —
   *le défaut déjà payé sur RowLocked* ». Un défaut connu, nommé à côté, laissé en
   place. Le savoir n'a jamais refusé personne.

2. **La forme de `lifecycle.transitions`** n'était pas jugée avant d'être parcourue :
   une chaîne, une liste ou un nombre produisait un `AttributeError`, pas une
   `ValueError` — donc pas un refus non plus, donc un 500 muet sur un schéma qu'on
   venait d'écrire. Même famille que le premier.

3. **La classe**, vérifiée plutôt que supposée : `RowLocked` était la SEULE exception
   du module qu'aucune face REST n'attrapait. Les six autres le sont toutes.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp.capabilities.datastore.rows import _write_refusal
from oto_mcp.datastore import errors as ds_errors


# ── 1. le bail sort comme un refus, avec son code propre ─────────────────────

def test_le_bail_derive_de_ValueError():
    """C'est l'héritage qui décide : les faces attrapent `ValueError` pour traduire
    un refus métier. Sans lui, aucune traduction — et un 500."""
    assert issubclass(ds_errors.RowLocked, ValueError)


def test_le_refus_de_bail_sort_en_409_et_dit_quoi_faire():
    """⚠️ 409 et non 400 : la requête est bien formée, c'est l'ÉTAT de la ligne qui
    s'y oppose. Un 400 enverrait corriger ce qui n'a rien à corriger."""
    refus = _write_refusal(ds_errors.RowLocked("r1", "worker-42", "2026-09-05T18:00Z"))
    assert refus.status == 409
    assert refus.code == "row_locked"
    # #515 : le refus mène par le `_run_id` manquant et ne nomme JAMAIS le titulaire
    # (le libellé d'un tiers) — l'échéance dit jusqu'à quand attendre.
    assert "_run_id" in refus.message, "la faute la plus fréquente, d'abord"
    assert "worker-42" not in refus.message, "on ne nomme pas le titulaire"
    assert "2026-09-05T18:00Z" in refus.message, "jusqu'à quand"
    assert "release it" in refus.message, "le geste de sortie"


def test_le_bail_ne_prend_pas_le_code_des_autres_refus():
    """Il précède `BusinessKeyRequired` et `RowValidationError` dans l'arbitrage :
    l'ordre des branches est le contrat, pas un détail de style."""
    autre = _write_refusal(ds_errors.RowValidationError(["x"]))
    assert autre.code == "row_invalid" and autre.status == 400


def test_dans_un_LOT_le_refus_de_bail_garde_sa_classe():
    """⚠️ La conséquence que mon propre changement a créée, et qu'un banc existant a
    attrapée : en devenant une `ValueError`, `RowLocked` tombait dans le `except
    ValueError` du batch, qui la ré-emballait en refus d'entrée invalide. Elle perdait
    son code 409 et le message du bail pour redevenir un « invalid_row_input » qui
    n'apprend rien.

    Le batch la ré-emballe désormais en gardant sa CLASSE, comme il le fait déjà pour
    la clé métier et la validation de schéma — seule la désignation de ligne change."""
    e = ds_errors.RowLocked("r0", "agent-1", "2026-09-05T18:00Z",
                            row="row 1/1 of the batch (siren=55111000)")
    assert isinstance(e, ValueError)
    assert "row 1/1 of the batch" in str(e), "la position dans le lot"
    assert "2026-09-05T18:00Z" in str(e), "…et l'échéance du bail, qui reste l'information utile"
    assert _write_refusal(e).status == 409


# ── 2. la classe : y en a-t-il d'autres ? ────────────────────────────────────

def test_aucune_exception_du_module_ne_sort_hors_des_types_traduits():
    """⚠️ Regarder la CLASSE plutôt que le cas. Chaque exception du datastore doit
    soit dériver de `ValueError` (traduite par `_write_refusal`), soit être attrapée
    NOMMÉMENT par les capacités — sinon elle remonte à Starlette et sort en 500.

    Ce banc tombe le jour où quelqu'un ajoute une exception sans lui donner l'une des
    deux sorties. C'est la seule façon de ne pas repayer ce défaut une troisième fois.
    """
    import inspect
    from pathlib import Path

    capacites = Path(inspect.getfile(_write_refusal)).parent
    attrapees = " ".join(p.read_text(encoding="utf-8")
                         for p in capacites.glob("*.py"))

    orphelines = []
    for nom, obj in vars(ds_errors).items():
        if not (inspect.isclass(obj) and issubclass(obj, Exception)):
            continue
        if obj.__module__ != ds_errors.__name__:
            continue
        if issubclass(obj, ValueError):
            continue                       # traduite par `_write_refusal`
        if f"except {nom}" in attrapees:
            continue                       # attrapée nommément
        # Attrapée par son PARENT, nommé : `DatastoreAmbigu` (#365) dérive de
        # `DatastoreNotFound` exprès, pour que chaque surface qui refuse
        # « introuvable » refuse aussi l'ambiguïté. `Exception` n'y compte pas.
        if any(f"except {b.__name__}" in attrapees for b in obj.__mro__[1:]
               if b.__module__ == ds_errors.__name__):
            continue
        orphelines.append(nom)

    assert not orphelines, (
        f"ces exceptions sortiraient en 500 muet : {orphelines}. Fais-les dériver de "
        "`ValueError` (traduction automatique) ou attrape-les nommément.")


# ── 3. une forme non prévue est un REFUS, pas une fuite technique ───────────


def _pose(st, ns, transitions):
    return st.set_schema(ns, {"fields": [
        {"key": "s", "role": "status",
         "lifecycle": {"states": ["a", "b"], "transitions": transitions}}]})


@pytest.mark.parametrize("forme", ["oui", ["a"], 3])
def test_une_forme_non_prevue_de_transitions_est_un_REFUS(live, forme):
    """⚠️ Avant : `AttributeError: 'str' object has no attribute 'items'` — pas une
    `ValueError`, donc pas un refus, donc un 500 au corps vide sur un schéma qu'on
    vient d'écrire. L'appelant pouvait croire la pose réussie et travailler sur une
    table sans schéma : c'est ce qui est arrivé, une demi-heure durant."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = str(db.create_datastore("user", "sub-refus", "t-" + uuid.uuid4().hex[:6]))
    st = make_store("sub-refus")

    with pytest.raises(ValueError) as e:
        _pose(st, ns, forme)
    message = str(e.value)
    assert "lifecycle.transitions" in message, "le refus doit NOMMER la clé fautive"
    assert type(forme).__name__ in message, "…et dire ce qu'il a reçu"
    assert "state" in message, "…et la forme attendue"


def test_la_forme_JUSTE_passe_toujours(live):
    """L'autre moitié : une garde qui refuserait le cas nominal serait pire que la
    fuite qu'elle remplace."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = str(db.create_datastore("user", "sub-refus", "t-" + uuid.uuid4().hex[:6]))
    st = make_store("sub-refus")
    _pose(st, ns, {"a": ["b"]})


def test_les_refus_DEJA_bons_du_lifecycle_ne_sont_pas_touches(live):
    """⚠️ Mesuré avant d'écrire : cinq refus du cycle de vie nommaient DÉJÀ la clé et
    la règle. Le rapport disait « le refus ne dit pas ce qui est invalide » ; c'était
    vrai d'un seul cas sur six. Ce banc garde les cinq autres — une correction qui
    dégraderait un voisin déjà bon est une perte qu'aucun test ne montre."""
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store

    ns = str(db.create_datastore("user", "sub-refus", "t-" + uuid.uuid4().hex[:6]))
    st = make_store("sub-refus")

    for schema, attendu in (
        ({"fields": [{"key": "s", "role": "status",
                      "lifecycle": {"states": ["a"], "transitions": {"a": ["ZZZ"]}}}]},
         "état cible inconnu"),
        ({"fields": [{"key": "s", "role": "status",
                      "lifecycle": {"states": ["a"], "max_claims": 3}}]},
         "abandon_state"),
        ({"fields": [{"key": "s", "role": "status",
                      "lifecycle": {"states": "a", "transitions": {}}}]},
         "liste non vide"),
        ({"fields": [{"key": "x", "type": "licorne"}]}, "type inconnu"),
    ):
        with pytest.raises(ValueError) as e:
            st.set_schema(ns, schema)
        assert attendu in str(e.value), f"{attendu} : {e.value}"
