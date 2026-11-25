"""Le jugement du prêt (`apps_appel._preteur`), sans base : ce qu'un aperçu de
brouillon, une app sans personne nommée ou un manifeste réécrit prêtent — rien —, et la
console d'org qui le règle. Le chemin entier, contre un PostgreSQL réel :
`test_apps_pret_db.py`."""
import pytest

from oto_mcp import db
from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities import org_console as OC
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

T = {"table": "12", "fields": ["name"]}
MANIFEST = {"tables": {"leads": T}}


def _app(**kw):
    base = {"id": 5, "manifest": MANIFEST, "acts_as": "preteur", "acts_as_current": True}
    return {**base, **kw}


def test_un_apercu_de_brouillon_ne_prete_jamais():
    qui, raison = C._preteur(_app(_brouillon=True), "12", T, "read")
    assert qui is None and "draft" in raison


def test_sans_personne_nommee_rien_n_est_prete():
    qui, raison = C._preteur(_app(acts_as=None), "12", T, "read")
    assert qui is None and "nobody" in raison


def test_un_manifeste_qui_n_est_pas_celui_du_pret_ne_prete_pas():
    qui, raison = C._preteur(_app(acts_as_current=False), "12", T, "read")
    assert qui is None and "manifest changed" in raison


def test_la_console_regle_le_pret(monkeypatch):
    from oto_mcp.capabilities.orgs import apps_act_as as P
    pose = {}
    monkeypatch.setattr(P, "poser", lambda org, v: pose.setdefault("set", (org, v)) and {"ok": 1})
    monkeypatch.setattr(P, "lire", lambda org: {"org_id": org, "enabled": False})
    ctx = ResolvedCtx(sub="admin", org_id=7)
    OC._org_settings(ctx, OC.OrgSettingsInput(op="set", domain="apps_act_as", org_id=7,
                                              enabled=True))
    assert pose["set"] == (7, True)
    assert OC._org_settings(ctx, OC.OrgSettingsInput(op="get", domain="apps_act_as",
                                                     org_id=7))["enabled"] is False
    with pytest.raises(AuthzDenied) as e:
        OC._org_settings(ctx, OC.OrgSettingsInput(op="set", domain="apps_act_as", org_id=7))
    assert e.value.code == "missing_enabled"


def test_le_defaut_de_l_instance(monkeypatch):
    from oto_mcp.capabilities.orgs import apps_act_as as P
    monkeypatch.setattr(db.apps, "apps_act_as_de_l_org", lambda org: None)
    monkeypatch.delenv("OTO_APPS_ACT_AS_DEFAULT", raising=False)
    assert P.permis(7) is False
    monkeypatch.setenv("OTO_APPS_ACT_AS_DEFAULT", "true")
    assert P.permis(7) is True
    monkeypatch.setattr(db.apps, "apps_act_as_de_l_org", lambda org: False)
    assert P.permis(7) is False
    assert P.permis(None) is False
