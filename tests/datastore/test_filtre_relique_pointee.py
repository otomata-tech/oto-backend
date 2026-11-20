"""Un filtre sur `champ.couche` doit voir les DEUX endroits où cette couche peut vivre.

Une couche vit normalement IMBRIQUÉE — `data->'contact1_nom'->>'link'`. Mais il existe
en base des clés LITTÉRALES pointées au premier niveau — `data->>'contact1_nom.link'` —
écrites avant que la garde du 31/08 n'empêche d'en créer. Ce qui a été écrit avant est
toujours là.

⚠️ **Le filtre ne lisait que la forme imbriquée, donc il rendait 0 sur une donnée
présente.** Mesuré le 08/09/2026 : **765 occurrences** dans le parc — 741 sur un
tableau de production, 24 sur un autre.

**Ce qui rend ce défaut grave dépasse le filtre** : c'est le geste dont on se sert pour
VÉRIFIER une destruction. Une campagne venait de purger trois colonnes de données de
personnes ; un contrôle par ce filtre lui aurait dit qu'il ne restait rien. (Vérifié :
ces trois colonnes-là ne portaient pas de clé pointée, le faux zéro n'a pas menti sur
cette purge — mais il aurait pu.)

Et la portée dépasse la destruction : tout décompte de complétude ou de provenance fait
avec `{"field": "x.comment"}` sur ces tableaux rendait un chiffre faux.

Le correctif suit un patron qui existait DÉJÀ dix lignes plus haut : la lecture d'un
nom nu regarde les deux endroits (`FIELD_VALUE_PARAM_SQL`) depuis toujours. On ne
l'avait simplement pas étendue à la couche.
"""
from __future__ import annotations

import json
import uuid

import pytest


def _store():
    from oto_mcp.datastore.core import make_store
    return make_store("sub-test")


def _table_avec_relique():
    """Un tableau portant les CINQ formes. La relique et les colonnes hors schéma sont
    posées en base directement — les gardes d'écriture les refusent aujourd'hui
    (oto#124 pour une colonne non déclarée), mais la production en porte."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect

    ns = str(db.create_datastore("user", "sub-test", "t-" + uuid.uuid4().hex[:6]))
    st = _store()
    st.set_schema(ns, {"key": "siren", "fields": [
        {"key": "siren", "type": "text"},
        {"key": "declaree", "type": "text"}]})
    st.append_row(ns, {
        "siren": "1",
        "declaree": {"valeur": "a", "comment": "c1", "link": "L1"},
    })
    rid = st.list_rows(ns)[0]["_id"]
    existant = {"hors_schema": {"valeur": "b", "comment": "c2", "link": "L2"},
                "plat_comment": "x", "relique_nom.link": "https://x"}
    with _connect() as c:
        c.execute("UPDATE datastore_rows SET data = data || %s::jsonb "
                  "WHERE row_id = %s", (json.dumps(existant), rid))
        c.commit()
    return st, ns


def _combien(st, ns, champ):
    return len(st.cursor_rows(ns, filters=[{"field": champ, "op": "not_empty"}])["rows"])


@pytest.mark.parametrize("champ", [
    "declaree.comment",     # couche imbriquée, colonne DÉCLARÉE
    "declaree.link",
    "hors_schema.comment",  # couche imbriquée, colonne hors schéma
    "hors_schema.link",
    "plat_comment",         # clé plate SANS point
    "hors_schema",          # nom nu
])
def test_les_formes_qui_mordaient_deja_mordent_toujours(live, champ):
    """⚠️ La moitié qu'on casse en corrigeant l'autre. Le `COALESCE` ajouté ne doit
    rien changer là où le filtre était juste — sinon on aurait échangé un faux zéro
    contre un faux positif, ce qui est pire : un zéro se remarque, un chiffre plausible
    et faux ne se remarque pas."""
    st, ns = _table_avec_relique()
    assert _combien(st, ns, champ) == 1


def test_la_RELIQUE_pointee_est_enfin_vue(live):
    """Le cas mesuré : une clé littérale `X.link` au premier niveau. Le filtre la lisait
    comme « la couche `link` de la colonne `X` » et cherchait au mauvais endroit."""
    st, ns = _table_avec_relique()
    assert _combien(st, ns, "relique_nom.link") == 1


def test_un_champ_qui_n_existe_NULLE_PART_rend_toujours_zero(live):
    """⚠️ Le zéro doit rester possible, sinon le filtre ne mesure plus rien. Élargir
    une lecture jusqu'à ce qu'elle trouve toujours quelque chose est le mode d'échec
    symétrique du faux zéro — et le plus difficile à repérer."""
    st, ns = _table_avec_relique()
    assert _combien(st, ns, "inexistante.link") == 0
    assert _combien(st, ns, "inexistante") == 0


def test_le_patron_est_celui_du_nom_NU(live):
    """La lecture d'un nom nu regardait les deux endroits DEPUIS TOUJOURS. Le défaut
    n'était pas une idée manquante, c'était une idée non étendue — et c'est ce qui le
    rendait invisible : le voisin immédiat faisait déjà la bonne chose."""
    from oto_mcp.db.paths import FIELD_VALUE_PARAM_SQL, LAYER_VALUE_PARAM_SQL, VALUE_LAYER

    # Le nom nu lit toujours ses deux endroits — l'enveloppe (`->>'valeur'`) et la
    # forme plate (`data->>%s`) — même depuis qu'il suit la règle d'`unwrap`
    # (oto#163) plutôt qu'un `COALESCE`. C'est l'axe que ce banc garde, pas le mot.
    assert f"->>'{VALUE_LAYER}'" in FIELD_VALUE_PARAM_SQL
    assert "ELSE data->>%s END" in FIELD_VALUE_PARAM_SQL
    assert LAYER_VALUE_PARAM_SQL.startswith("COALESCE(")
