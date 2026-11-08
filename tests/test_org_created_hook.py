"""Le crochet de création d'org : un POST non bloquant, fermé sans config, et déclenché
par l'écrivain unique `org_store.create_org` APRÈS son commit."""
import logging
import threading

import pytest

from oto_mcp import org_created_hook as hook


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("OTO_ORG_CREATED_HOOK_URL", raising=False)
    monkeypatch.delenv("OTO_ORG_CREATED_HOOK_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _primary(monkeypatch):
    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", "main")


def _spy(monkeypatch):
    calls, done = [], threading.Event()

    class R:
        status_code = 200

    def post(url, headers=None, timeout=None):
        calls.append((url, headers, timeout))
        done.set()
        return R()

    monkeypatch.setattr(hook.requests, "post", post)
    return calls, done


def test_sans_config_rien_ne_part(monkeypatch):
    calls, done = _spy(monkeypatch)
    hook.notify(7, "main")
    assert not done.wait(0.2) and calls == []


def test_une_url_sans_secret_ne_part_pas(monkeypatch):
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/orgs/{org_id}/enrol")
    calls, done = _spy(monkeypatch)
    hook.notify(7, "main")
    assert not done.wait(0.2) and calls == []


def test_avec_config_un_post_au_chemin_de_l_org_avec_le_bearer(monkeypatch):
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/orgs/{org_id}/enrol")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_TOKEN", "s3cret")
    calls, done = _spy(monkeypatch)
    hook.notify(42, "main")
    assert done.wait(2)
    url, headers, timeout = calls[0]
    assert url == "https://svc.example/orgs/42/enrol"
    assert headers == {"Authorization": "Bearer s3cret"} and timeout == hook._TIMEOUT_S


def test_le_tenant_primaire_arrive_en_none_et_part(monkeypatch):
    """`config.tenant_slug_for` rend None pour le tenant primaire : c'est le cas de
    toute org d'un client, et elle doit être prévenue."""
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/orgs/{org_id}/enrol")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_TOKEN", "s3cret")
    calls, done = _spy(monkeypatch)
    hook.notify(525, None)
    assert done.wait(2)
    assert calls[0][0] == "https://svc.example/orgs/525/enrol"


def test_un_autre_tenant_ne_part_pas(monkeypatch):
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/orgs/{org_id}/enrol")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_TOKEN", "s3cret")
    calls, done = _spy(monkeypatch)
    hook.notify(9, "un-revendeur")
    assert not done.wait(0.2) and calls == []


def test_une_erreur_reseau_est_avalee(monkeypatch):
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/{org_id}")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_TOKEN", "s3cret")

    def boom(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(hook.requests, "post", boom)
    hook._post(1, "https://svc.example/{org_id}", "s3cret")  # ne lève pas


def test_create_org_previent_apres_l_insert(monkeypatch):
    """L'écrivain unique appelle le crochet avec l'id qu'il vient de créer."""
    from oto_mcp import org_store
    from oto_mcp.org_store import orgs

    seen = []
    monkeypatch.setattr(orgs.org_created_hook, "notify", lambda i, t: seen.append((i, t)))

    class Conn:
        def execute(self, *a, **k):
            return self

        def fetchone(self):
            return {"id": 99}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(org_store, "_connect", lambda: Conn())
    monkeypatch.setattr(orgs.config, "front_for", lambda x: (None, None))
    monkeypatch.setattr(orgs.config, "tenant_slug_for", lambda x: "primary")
    assert org_store.create_org("Acme", created_by="sub1") == 99
    assert seen == [(99, "primary")]


def test_une_org_d_un_autre_tenant_n_est_pas_notifiee(monkeypatch):
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/{org_id}")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_TOKEN", "s3cret")
    calls, done = _spy(monkeypatch)
    hook.notify(7, "partner")
    assert not done.wait(0.2) and calls == []


def test_tenant_primaire_non_declare_n_appelle_pas(monkeypatch):
    monkeypatch.delenv("OTO_TENANT_PRIMAIRE_SLUG")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_URL", "https://svc.example/{org_id}")
    monkeypatch.setenv("OTO_ORG_CREATED_HOOK_TOKEN", "s3cret")
    calls, done = _spy(monkeypatch)
    hook.notify(7, "main")
    assert not done.wait(0.2) and calls == []


def test_un_refus_du_service_se_journalise_en_error_sans_le_secret(monkeypatch, caplog):
    """Avalée, l'erreur reste visible : `error` part dans Sentry, et le jeton n'y est jamais."""
    class R:
        status_code = 503

    monkeypatch.setattr(hook.requests, "post", lambda *a, **k: R())
    with caplog.at_level(logging.WARNING, logger=hook.logger.name):
        hook._post(5, "https://svc.example/{org_id}", "s3cret")
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert "s3cret" not in caplog.text


def test_une_erreur_reseau_se_journalise_en_error_sans_le_secret(monkeypatch, caplog):
    def boom(*a, **k):
        raise ConnectionError("down Bearer s3cret")

    monkeypatch.setattr(hook.requests, "post", boom)
    with caplog.at_level(logging.WARNING, logger=hook.logger.name):
        hook._post(5, "https://svc.example/{org_id}", "s3cret")
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert "s3cret" not in caplog.text
