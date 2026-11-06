"""`users.locale` câblée dans les 6 gabarits transactionnels (oto-backend#700).

Avant ce lot, `email.py` servait tout en français, sans jamais consulter la
préférence de langue posée via `me.locale.set` — un compte qui avait mis son
dashboard en anglais recevait quand même ses invitations et notifications en
français. Chaque gabarit prend maintenant `locale: str | None = None` ; ce
banc vérifie que `'en'` sert la version anglaise et que toute autre valeur
(dont `None`, `users.locale IS NULL`) sert le FR **à l'octet près** — le
comportement d'avant, pour tout compte sans préférence.

Les appelants (invites/resources/docs/usage) sont couverts séparément, au
plus près de leur lookup `users.locale` (`test_invitations_surface.py`,
`test_project_delivery.py`, `test_docs.py`, `test_signal_reporter_notice.py`).
"""
from oto_mcp import email


def _captured(monkeypatch):
    sent = {}
    monkeypatch.setattr(email, "_send", lambda to, subject, html, **k:
                        sent.update(to=to, subject=subject, html=html) or True)
    return sent


# ── send_invite_email ────────────────────────────────────────────────────────

def test_invite_email_default_locale_is_french(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/CODE",
                            "Alice", brand="oto")
    assert sent["subject"] == "invitation à rejoindre acme sur oto"
    assert "invites you" not in sent["html"]


def test_invite_email_en(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/CODE",
                            "Alice", brand="oto", locale="en")
    assert sent["subject"] == "invitation to join acme on oto"
    assert "Alice invites you to join" in sent["html"]
    assert ">join<" in sent["html"]
    assert "rejoindre" not in sent["html"]


# ── send_resource_shared_email ───────────────────────────────────────────────

def test_resource_shared_email_en(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_resource_shared_email(
        "a@b.c", type_label="project", name="Campagne", permission="read",
        app_url="https://app", sharer="Bob", brand="oto", locale="en")
    assert sent["subject"] == "Campagne — project shared with you on oto"
    assert "Bob shared" in sent["html"] and "with read access" not in sent["html"]
    assert "(read access) on oto" in sent["html"]
    assert "partagé" not in sent["html"]


def test_resource_shared_email_default_is_french(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_resource_shared_email(
        "a@b.c", type_label="projet", name="Campagne", permission="write",
        app_url="https://app", sharer="Bob", brand="oto")
    assert "partagé" in sent["html"] and "shared" not in sent["html"]


# ── send_resource_transferred_email ──────────────────────────────────────────

def test_resource_transferred_email_en(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_resource_transferred_email(
        "a@b.c", type_label="project", name="Campagne", app_url="https://app",
        sharer="Bob", brand="oto", locale="en")
    assert sent["subject"] == "Campagne — project transferred to you on oto"
    assert "you are now the owner" in sent["html"]
    assert "propriétaire" not in sent["html"]


# ── send_signal_digest_email ─────────────────────────────────────────────────

def test_signal_digest_email_en(monkeypatch):
    sent = _captured(monkeypatch)
    items = [{"status": "resolved", "target": "x_tool", "created_at": "2026-08-17",
             "body": "it breaks", "resolution": "fixed"}]
    email.send_signal_digest_email("a@b.c", items=items, brand="oto", locale="en")
    assert sent["subject"] == "1 update from your agents on oto: what happened"
    assert "your agents flagged 1 item on oto" in sent["html"]
    assert "done" in sent["html"]
    assert "vos agents" not in sent["html"]


def test_signal_digest_email_en_plural_and_grouping(monkeypatch):
    sent = _captured(monkeypatch)
    items = [{"status": "declined", "target": "y_tool", "created_at": d,
             "body": "same issue", "resolution": "won't fix"}
             for d in ("2026-08-01", "2026-08-05")]
    email.send_signal_digest_email("a@b.c", items=items, brand="oto", locale="en")
    assert sent["subject"].startswith("2 updates from your agents")
    assert "2 reports, 2026-08-01 to 2026-08-05" in sent["html"]
    assert "not pursued" in sent["html"]


def test_signal_digest_email_default_is_french_unchanged(monkeypatch):
    """Comportement inchangé pour `locale=None` — c'est la seule ligne de défense
    contre une régression silencieuse sur les comptes sans préférence."""
    sent = _captured(monkeypatch)
    items = [{"status": "resolved", "target": "x_tool", "created_at": "2026-08-17",
             "body": "ça casse", "resolution": "corrigé"}]
    email.send_signal_digest_email("a@b.c", items=items, brand="oto")
    assert "vos agents" in sent["html"] and "your agents" not in sent["html"]
    assert "traité" in sent["html"]


# ── l'invitation se présente (invitations rangées en spam, 06/11/2026) ───────

def test_invite_nomme_l_hote_et_dit_ce_qu_est_le_produit(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/CODE", "alice@x.fr",
                            brand="oto", locale="en", inviter_name="Alice Martin")
    assert "<strong>Alice Martin (alice@x.fr)</strong> invites you" in sent["html"]
    assert "is the workspace where the acme team shares its data" in sent["html"]
    assert "not expecting this invitation?" in sent["html"]
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/CODE", "alice@x.fr",
                            brand="oto", inviter_name="Alice Martin")
    assert "<strong>Alice Martin (alice@x.fr)</strong> vous invite" in sent["html"]
    assert "est l'espace de travail où l'équipe acme" in sent["html"]
    assert "vous n'attendiez pas cette invitation" in sent["html"]


def test_un_nom_qui_n_est_qu_une_adresse_ne_double_pas_l_hote(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/CODE", "alice@x.fr",
                            brand="oto", locale="en", inviter_name="alice@x.fr")
    assert "<strong>alice@x.fr</strong> invites you" in sent["html"]


def test_l_apercu_nomme_l_hote_sans_bourrage_invisible(monkeypatch):
    sent = _captured(monkeypatch)
    email.send_invite_email("a@b.c", "acme", "https://x/invitation/CODE", "alice@x.fr",
                            brand="oto", locale="en", inviter_name="Alice Martin")
    debut = sent["html"].index('<div style="display:none')
    apercu = sent["html"][debut:sent["html"].index("</div>", debut)]
    assert "Alice Martin (alice@x.fr) invites you to join acme" in apercu
    assert "&#8203;" not in sent["html"] and "&#847;" not in sent["html"]
