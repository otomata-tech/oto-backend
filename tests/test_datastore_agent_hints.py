"""Messages actionnables des tools data_* (feedbacks #161/#163/#171) : helpers purs
`_unknown_filter_keys` / `_row_not_found_hint` sur un store stub (sans DB)."""
from __future__ import annotations

from oto_mcp.tools.datastore import _row_not_found_hint, _unknown_filter_keys


class _Store:
    """Stub : 2 lignes, clé métier déclarée `id`."""
    def __init__(self, rows=None, key="id"):
        self._rows = rows if rows is not None else [
            {"_id": "u-1", "id": "a", "nom": "Alpha", "val": 9},
            {"_id": "u-2", "id": "b", "nom": "Beta", "val": 2},
        ]
        self._key = key

    def cursor_rows(self, namespace, filter=None, limit=100, cursor=None):
        rows = self._rows
        if filter:
            rows = [r for r in rows
                    if all(str(r.get(k)) == str(v) for k, v in filter.items())]
        return {"rows": rows[:limit], "next_cursor": None}

    def declared_key(self, namespace):
        return self._key


def test_unknown_filter_keys_flags_typo():
    assert _unknown_filter_keys(_Store(), "ns", {"colonne_inexistante": "x"}) == {
        "colonne_inexistante"}


def test_known_filter_key_not_flagged():
    # colonne existante, valeur sans match → PAS un warning (0 résultat légitime)
    assert _unknown_filter_keys(_Store(), "ns", {"nom": "Gamma"}) == set()


def test_empty_namespace_never_warns():
    assert _unknown_filter_keys(_Store(rows=[]), "ns", {"x": 1}) == set()


def test_row_not_found_hint_points_to_business_key():
    # feedback #161 : id=a est la CLÉ MÉTIER, pas le _id technique → guider vers filter
    msg = _row_not_found_hint(_Store(), "ns", "a")
    assert "_id" in msg and 'filter={"id": "a"}' in msg and "u-1" in msg


def test_row_not_found_hint_without_key_stays_simple():
    msg = _row_not_found_hint(_Store(key=None), "ns", "zzz")
    assert "introuvable" in msg and "_id" in msg
