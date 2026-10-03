"""L'email d'activation, hors base : le réglage, le contenu, et les trois verrous —
drapeau fermé, pas d'envoi sans essai, une personne remise si le relais refuse.

L'audience elle-même est exercée sur le SQL réel dans `test_activation_audience_db.py`.
"""
from __future__ import annotations

import json

import pytest

from oto_mcp import activation

REGLAGE = {
    "acme": {
        "sender": "Acme <hello@acme.test>",
        "reply_to": "hello@acme.test",
        "cc": ["un@acme.test", "deux@acme.test"],
        "app_url": "https://app.acme.test/",
        "mcp_url": "https://mcp.acme.test/mcp",
        "help_url": "https://acme.test/help/connect",
        "exclude_domains": ["acme.test"],
    }
}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OTO_ACTIVATION", json.dumps(REGLAGE))
    monkeypatch.delenv("OTO_ACTIVATION_ENVOI", raising=False)
    monkeypatch.setattr(activation, "_nom_produit", lambda r: "Acme")
    monkeypatch.setattr(activation, "_lien_refus", lambda r, sub: f"https://x.test/o/u/{sub}")
    return monkeypatch


@pytest.fixture
def faux(env, monkeypatch):
    """Base et relais simulés : ce que le passage lit, écrit et envoie."""
    from oto_mcp.db import activation as db_act
    from oto_mcp.db import outreach as db_outreach

    etat = {"audience": {"connect": [{"sub": "acme:1", "email": "un@client.test"},
                                     {"sub": "acme:2", "email": "deux@client.test"}]},
            "essais": set(), "traces": [], "annules": [], "envois": [], "relais_ok": True}
    monkeypatch.setattr(db_act, "taille",
                        lambda **kw: len(etat["audience"].get(kw["etape"], [])))
    monkeypatch.setattr(db_act, "audience",
                        lambda **kw: etat["audience"].get(kw["etape"], [])[:kw["cap"]])
    monkeypatch.setattr(db_outreach, "locales_essayees", lambda **kw: etat["essais"])

    def trace(**kw):
        etat["traces"].append(kw)
        return True
    monkeypatch.setattr(db_outreach, "enregistre_envoi", trace)
    monkeypatch.setattr(db_outreach, "annule_envoi",
                        lambda **kw: etat["annules"].append(kw["sub"]))

    def envoyer(r, to, c, lien):
        etat["envois"].append({"to": to, "lien": lien})
        return etat["relais_ok"]
    monkeypatch.setattr(activation, "_envoyer", envoyer)
    return etat


def test_sans_reglage_rien_n_est_lu(monkeypatch):
    monkeypatch.delenv("OTO_ACTIVATION", raising=False)
    assert activation.reglages() == []
    assert activation.balayer()["tenants"] == []


def test_un_reglage_incomplet_est_refuse(monkeypatch):
    monkeypatch.setenv("OTO_ACTIVATION", json.dumps({"acme": {"sender": "a@b.test"}}))
    with pytest.raises(activation.ReglageInvalide, match="reply_to"):
        activation.reglages()


def test_le_contenu_donne_les_etapes_et_le_lien_du_connecteur(env):
    r = activation.reglages()[0]
    c = activation.contenu(r, "Acme")
    assert c["subject"] == "One step left to start using Acme"
    assert "1. On claude.ai, select Customize" in c["body"]
    assert "Enter the name Acme and this link: https://mcp.acme.test/mcp" in c["body"]
    assert "https://acme.test/help/connect" in c["body"]
    assert c["cta_url"] == "https://app.acme.test"
    assert "{" not in c["body"], "un gabarit non rempli partirait tel quel"


def test_l_empreinte_porte_l_expediteur_et_les_copies(env):
    r = activation.reglages()[0]
    c = activation.contenu(r, "Acme")
    autre = activation.Reglage(**{**r.__dict__, "cc": ("trois@acme.test",)})
    assert activation.empreinte(c, r) != activation.empreinte(c, autre)


def test_drapeau_ferme_rien_ne_part_ni_ne_s_ecrit(faux):
    faux["essais"] = {"en"}
    out = activation.balayer()
    assert out["a_blanc"] is True
    assert [l["dus"] for l in out["tenants"]] == [2, 0, 0]
    assert faux["envois"] == [] and faux["traces"] == []


def test_sans_essai_l_envoi_reel_est_refuse(faux, monkeypatch):
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    out = activation.balayer()
    assert "bloque" in out["tenants"][0]
    assert faux["envois"] == [] and faux["traces"] == []


def test_avec_essai_chaque_personne_recoit_une_fois(faux, monkeypatch):
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    faux["essais"] = {"en"}
    out = activation.balayer()
    assert out["tenants"][0]["envoyes"] == 2
    assert [e["to"] for e in faux["envois"]] == ["un@client.test", "deux@client.test"]
    assert [t["sub"] for t in faux["traces"]] == ["acme:1", "acme:2"]
    assert {t["campaign"] for t in faux["traces"]} == {"activation-connect:acme"}
    assert [l["etape"] for l in out["tenants"]] == ["connect", "first-process", "recurring"]
    assert {t.get("kind", "send") for t in faux["traces"]} == {"send"}
    assert {t["locale"] for t in faux["traces"]} == {"en"}


def test_un_refus_du_relais_remet_la_personne(faux, monkeypatch):
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    faux["essais"] = {"en"}
    faux["relais_ok"] = False
    out = activation.balayer()
    assert out["tenants"][0]["refuses"] == 2 and out["tenants"][0]["envoyes"] == 0
    assert faux["annules"] == ["acme:1", "acme:2"]


def test_le_plafond_borne_un_passage(faux, monkeypatch):
    reglage = {"acme": {**REGLAGE["acme"], "max_per_run": 1}}
    monkeypatch.setenv("OTO_ACTIVATION", json.dumps(reglage))
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    faux["essais"] = {"en"}
    out = activation.balayer()
    assert out["tenants"][0] == {**out["tenants"][0], "dus": 2, "lot": 1, "envoyes": 1}


def test_le_relais_recoit_expediteur_reponse_et_copies(env, monkeypatch):
    from oto_mcp import email as mailer
    vu = {}

    def send(to, subject, html, **kw):
        vu.update(to=to, subject=subject, html=html, **kw)
        return True
    monkeypatch.setattr(mailer, "_send", send)
    monkeypatch.setattr(mailer, "render_composed_email",
                        lambda body, **kw: f"<p>{body}</p><a>{kw['unsubscribe_url']}</a>")
    r = activation.reglages()[0]
    assert activation._envoyer(r, "un@client.test", activation.contenu(r, "Acme"),
                               "https://x.test/o/u/s?lang=en")
    assert vu["from_email"] == "Acme <hello@acme.test>"
    assert vu["reply_to"] == "hello@acme.test"
    assert vu["cc"] == ["un@acme.test", "deux@acme.test"]
    assert "?lang=en" in vu["html"]


def test_un_relais_declare_sans_jeton_n_envoie_pas(monkeypatch):
    from oto_mcp import email as mailer
    monkeypatch.setenv("OTO_MAILER_SEND_BEARER", "jeton-de-l-instance")
    appels = []
    monkeypatch.setattr("httpx.post", lambda *a, **k: appels.append(a))
    assert mailer._send("a@b.test", "s", "<p/>", mailer_url="https://relais.test",
                        bearer=None) is False
    assert appels == [], "le jeton de l'instance ne doit jamais partir vers un autre relais"


def test_les_copies_partent_dans_le_corps_de_l_appel(monkeypatch):
    from oto_mcp import email as mailer
    monkeypatch.setenv("OTO_MAILER_SEND_BEARER", "j")
    monkeypatch.setenv("OTO_MAILER_URL", "https://mailer.test/api/send")
    vu = {}

    class R:
        status_code = 200
        text = ""

    def post(url, headers, json, timeout):
        vu.update(url=url, json=json)
        return R()
    monkeypatch.setattr("httpx.post", post)
    assert mailer._send("a@b.test", "s", "<p/>", from_email="A <a@a.test>",
                        cc=["c@c.test\r\nBcc: x@x.test"])
    assert vu["json"]["cc"] == ["c@c.test Bcc: x@x.test"], "CR/LF neutralisés"


def test_le_rendu_porte_la_marque_du_tenant_pas_la_notre(env, monkeypatch):
    """Rendu réel : le pied nomme le tenant et le lien de refus mène à la page anglaise."""
    from oto_mcp import email as mailer
    monkeypatch.setenv("OTO_TENANT_PRIMAIRE_SLUG", "oto")
    r = activation.reglages()[0]
    c = activation.contenu(r, "Acme")
    html = mailer.render_composed_email(
        c["body"], cta_text=c["cta_label"], cta_url=c["cta_url"], brand=r.tenant,
        locale="en", unsubscribe_url="https://x.test/o/u/tok?lang=en")
    assert "you have a acme account" in html or "you have a Acme account" in html
    assert "https://x.test/o/u/tok?lang=en" in html
    assert "https://mcp.acme.test/mcp" in html


def test_sans_marque_declaree_rien_ne_part(faux, monkeypatch):
    """Le gabarit neutre signerait du slug (« your acme account ») : on refuse."""
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    monkeypatch.setattr(activation, "_nom_produit", lambda r: None)
    faux["essais"] = {"en"}
    out = activation.balayer()
    assert "bloque" in out["tenants"][0]
    assert faux["envois"] == [] and faux["traces"] == []


def test_le_lien_de_refus_est_sur_l_hote_du_tenant(monkeypatch):
    from oto_mcp import outreach_optout
    monkeypatch.setenv("OTO_MCP_OAUTH_STATE_SECRET", "s" * 32)
    monkeypatch.setenv("OTO_ACTIVATION", json.dumps(
        {"acme": {**REGLAGE["acme"], "link_base": "https://mcp.acme.test/"}}))
    r = activation.reglages()[0]
    lien = activation._lien_refus(r, "acme:1")
    assert lien.startswith("https://mcp.acme.test/o/u/") and lien.endswith("?lang=en")
    jeton = lien.split("/o/u/", 1)[1].split("?", 1)[0]
    assert outreach_optout.verify(jeton) == "acme:1"


def test_chaque_etape_a_son_texte_et_son_empreinte(env):
    r = activation.reglages()[0]
    contenus = {e: activation.contenu(r, "Acme", e) for e in activation.ETAPES}
    assert contenus["first-process"]["subject"] == "Your first Acme process"
    assert "read the onboarding guide and set up my first process" in \
        contenus["first-process"]["body"]
    assert "schedule the process I ran last to run every week" in \
        contenus["recurring"]["body"]
    assert len({activation.empreinte(c, r) for c in contenus.values()}) == 3
    for c in contenus.values():
        assert "{" not in c["body"]


def test_l_essai_d_une_etape_n_ouvre_pas_les_autres(faux, monkeypatch):
    """L'envoi d'une étape exige l'essai de SA campagne : la première reçue ne vaut
    pas pour les suivantes."""
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    from oto_mcp.db import outreach as db_outreach
    faux["audience"]["first-process"] = [{"sub": "acme:3", "email": "trois@client.test"}]
    monkeypatch.setattr(db_outreach, "locales_essayees",
                        lambda **kw: {"en"} if kw["campaign"].startswith(
                            "activation-connect") else set())
    out = activation.balayer()
    par_etape = {l["etape"]: l for l in out["tenants"]}
    assert par_etape["connect"]["envoyes"] == 2
    assert par_etape["first-process"]["envoyes"] == 0
    assert "bloque" in par_etape["first-process"]


def test_le_plafond_vaut_pour_le_passage_entier(faux, monkeypatch):
    reglage = {"acme": {**REGLAGE["acme"], "max_per_run": 2}}
    monkeypatch.setenv("OTO_ACTIVATION", json.dumps(reglage))
    monkeypatch.setenv("OTO_ACTIVATION_ENVOI", "1")
    faux["essais"] = {"en"}
    faux["audience"]["first-process"] = [{"sub": "acme:3", "email": "trois@client.test"}]
    out = activation.balayer()
    assert sum(l["envoyes"] for l in out["tenants"]) == 2
