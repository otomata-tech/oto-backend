"""ADR 0042 barreau 1 : la prose INIT (platform + user) vit dans `guides`
(delivery='init'), écrite/lue par `guide_store.{set,get}_init_guide`, et n'apparaît
JAMAIS dans le catalogue on-demand (`list_guides_for`). Fake modélisant `delivery`."""
import pytest

from oto_mcp import guide_store as G


class _FakeGuides:
    """Modèle minimal de la table `guides` avec la colonne `delivery`."""
    def __init__(self):
        self.rows = {}   # (scope, owner, slug) -> dict

    # -- init (delivery='init') --
    def get_init_guide_db(self, scope, owner_id, slug):
        r = self.rows.get((scope, str(owner_id), slug))
        return r if (r and r["delivery"] == "init") else None

    def set_init_guide_db(self, scope, owner_id, slug, body_md):
        row = {"scope": scope, "owner_id": str(owner_id), "slug": slug,
               "body_md": body_md or "", "delivery": "init", "updated_at": "T"}
        self.rows[(scope, str(owner_id), slug)] = row
        return row

    def seed_init_guide_db(self, scope, owner_id, slug, body_md):
        self.rows.setdefault((scope, str(owner_id), slug),
                             {"scope": scope, "owner_id": str(owner_id), "slug": slug,
                              "body_md": body_md or "", "delivery": "init", "updated_at": "T"})

    # -- on-demand (delivery='on-demand') --
    def list_guides_db(self, scope, owner_id):
        return [v for (s, o, _), v in sorted(self.rows.items())
                if s == scope and o == str(owner_id) and v["delivery"] == "on-demand"]

    def set_guide_db(self, scope, owner_id, slug, body_md, title="", description=""):
        row = {"scope": scope, "owner_id": str(owner_id), "slug": slug, "title": title,
               "description": description, "body_md": body_md, "delivery": "on-demand"}
        self.rows[(scope, str(owner_id), slug)] = row
        return row


@pytest.fixture
def db(monkeypatch):
    fake = _FakeGuides()
    import oto_mcp.db as real
    for n in ("get_init_guide_db", "set_init_guide_db", "seed_init_guide_db",
              "list_guides_db", "set_guide_db"):
        monkeypatch.setattr(real, n, getattr(fake, n))
    return fake


def test_user_init_roundtrip(db):
    assert G.get_init_guide("user", "u1") == {"body_md": "", "updated_at": None}
    out = G.set_init_guide("user", "u1", "  hello  ")
    assert out["body_md"] == "  hello  " and out["updated_at"] == "T"
    assert G.get_init_guide("user", "u1")["body_md"] == "  hello  "
    assert G.init_guide_body("user", "u1") == "hello"                    # strippé


def test_platform_init_roundtrip(db):
    G.set_init_guide("platform", "secret_sauce", "POSTURE")
    assert db.rows[("platform", "platform", "secret_sauce")]["delivery"] == "init"
    assert G.init_guide_body("platform", "secret_sauce") == "POSTURE"


def test_seed_does_not_overwrite(db):
    G.set_init_guide("user", "u1", "edited")
    G.seed_init_guide("user", "u1", "default")
    assert G.init_guide_body("user", "u1") == "edited"                   # seed = DO NOTHING


def test_init_readme_absent_from_on_demand_catalog(db):
    G.set_init_guide("user", "u1", "mon readme init")                    # slug 'readme'
    db.set_guide_db("user", "u1", "howto", "corps how-to")              # on-demand
    slugs = {g["slug"] for g in G.list_guides_for(sub="u1", org_id=None)}
    assert "howto" in slugs and "readme" not in slugs                    # init exclu


def test_set_init_guide_rejects_unmigrated_scope(db):
    with pytest.raises(G.GuideError):
        G.set_init_guide("org", "42", "x")      # org = barreau 2
