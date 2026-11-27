"""Les rôles d'une app (`capabilities/apps_roles.py`), sans base : leur forme jugée contre
le manifeste, et le manifeste qu'un rôle laisse — il ne fait que retirer."""
from __future__ import annotations

from oto_mcp.capabilities import apps_roles as R
from oto_mcp.capabilities.apps_manifeste import forme

BASE = {
    "tables": {
        "fiches": {"table": "12", "fields": ["name", "status", "notes", "partner"],
                   "set": {"status": ["open", "won", "lost"]}, "add": ["name", "notes"]},
        "stats": {"table": "13"},
    },
    "agents": {"enrich": {"id": 7, "run": True, "input": True}},
    "files": True,
}


def _m(roles, default="user", **extra):
    return {**BASE, "roles": roles, "default_role": default, **extra}


def test_sans_roles_rien_ne_change():
    clean, problems = forme(BASE)
    assert problems == []
    assert "roles" not in clean
    assert R.resserrer(clean, None) == clean


def test_un_role_ne_peut_rien_ouvrir_que_le_manifeste_n_ouvre():
    _, problems = forme({**_m({"user": {"files": True}}), "files": False})
    assert any("takes no files" in p for p in problems)
    _, problems = forme(_m({"user": {
        "tables": {"fiches": {"fields": ["name", "secret"], "set": {"status": ["archived"],
                                                                  "name": ["x"]},
                              "add": ["partner"]},
                   "ailleurs": True},
        "agents": {"autre": True},
    }}))
    texte = "\n".join(problems)
    for attendu in ("not declared by the app: secret", "values the app can't set: archived",
                    "set.name: the app can't set", "can't fill: partner",
                    "tables.ailleurs: the app declares no such table",
                    "agents.autre: the app declares no such agent"):
        assert attendu in texte, attendu


def test_default_role_est_obligatoire_et_doit_exister():
    _, problems = forme({**BASE, "roles": {"user": {}}})
    assert any("default_role" in p for p in problems)
    _, problems = forme(_m({"user": {}}, default="admin"))
    assert any("default_role" in p for p in problems)
    _, problems = forme({**BASE, "default_role": "user"})
    assert any("needs `roles`" in p for p in problems)


def test_le_manifeste_resserre():
    clean, problems = forme(_m({
        "admin": {"label": "Admin", "sections": ["pipeline", "settings"]},
        "user": {"label": "Utilisateur",
                 "tables": {"fiches": {"fields": ["name", "status"], "set": {"status": ["won"]},
                                       "add": []}},
                 "agents": {"enrich": {"run": False}}, "files": False,
                 "sections": ["pipeline"]},
    }))
    assert problems == []
    admin = R.resserrer(clean, "admin")
    assert admin["tables"] == clean["tables"] and admin["files"] is True
    assert "roles" not in admin and "default_role" not in admin
    user = R.resserrer(clean, "user")
    assert set(user["tables"]) == {"fiches"}           # `stats` n'est pas ouvert
    f = user["tables"]["fiches"]
    assert f["table"] == "12" and f["fields"] == ["name", "status"]
    assert f["set"] == {"status": ["won"]} and f["add"] == []
    assert user["agents"]["enrich"] == {"id": 7, "run": False, "input": False}
    assert user["files"] is False
    assert R.acces(clean, "user") == {"role": "user", "role_label": "Utilisateur",
                                      "sections": ["pipeline"]}
    assert R.acces(clean, None)["sections"] is None


def test_une_cle_absente_herite_du_manifeste():
    clean, _ = forme(_m({"user": {"tables": {"fiches": True}}}))
    user = R.resserrer(clean, "user")
    assert user["tables"]["fiches"] == clean["tables"]["fiches"]
    assert user["agents"] == clean["agents"] and user["files"] is True


def test_un_role_inconnu_ne_resserre_rien_mais_n_est_jamais_choisi():
    """`resserrer` d'un rôle absent rend le manifeste entier : c'est pourquoi
    `role_du_lecteur` ne rend QUE des rôles déclarés, et `default_role` sinon."""
    clean, _ = forme(_m({"user": {"tables": {}}}))
    assert R.resserrer(clean, "user")["tables"] == {}
