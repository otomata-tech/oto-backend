"""Réconciliation poll-and-bind Unipile (webhook hosted-auth v2 non livré).

Verrouille : on ne lie qu'un compte NON déjà lié, du bon provider, créé APRÈS le
pending du sub (le floor évite de rebinder un siège pré-existant) — et, depuis
oto#247, identifié SANS ambiguïté : l'`account_id` du retour, une ligne morte du sub,
ou un candidat vivant unique que personne d'autre n'attend. Sinon, refus nommé."""
import types
from datetime import datetime, timezone

from oto_mcp import unipile_connect as uc

PEND_TS = datetime(2026, 7, 16, 12, 41, tzinfo=timezone.utc)


def _pend(nonce="N", org=2, provider="LINKEDIN", seat=True, ts=PEND_TS):
    return {"nonce": nonce, "org_id": org, "provider": provider,
            "platform_seat": seat, "created_at": ts}


def _acc(aid, name, provider="linkedin", created="2026-07-16 12:45:00+00"):
    return {"id": aid, "name": name, "provider": provider, "created_at": created}


def _setup(monkeypatch, pendings, accounts, bound=None, dead=None, alive_ids=None):
    monkeypatch.setattr(uc.db, "list_unipile_pending_for_sub", lambda s: pendings)
    monkeypatch.setattr(uc.db, "bound_unipile_account_ids", lambda: set(bound or []))
    # La garde partagée (#559) lit les lignes d'autrui en base ; ici tout est stubé —
    # sans ce stub le fichier ne passe que si un test voisin a laissé DATABASE_URL.
    monkeypatch.setattr(uc.db, "foreign_unipile_account_ids", lambda s: set())
    monkeypatch.setattr(uc.db, "dead_unipile_account_ids_for",
                        lambda s, p="LINKEDIN": set(dead or []))
    monkeypatch.setattr(uc.access, "resolve_credential",
                        lambda *a, **k: types.SimpleNamespace(key="K", is_platform=True, config={}))
    import oto.tools.unipile as core
    # account_alive : par défaut TOUS vivants ; `alive_ids` restreint à ceux-là
    alive = (lambda aid: aid in alive_ids) if alive_ids is not None else (lambda aid: True)
    monkeypatch.setattr(core, "make_unipile_client",
                        lambda **k: types.SimpleNamespace(
                            list_accounts=lambda: accounts, account_alive=alive))
    calls = {"set": [], "resolved": []}
    monkeypatch.setattr(uc.db, "set_unipile_account",
                        lambda *a, **k: calls["set"].append((a, k)))
    monkeypatch.setattr(uc.db, "resolve_unipile_pending",
                        lambda n: calls["resolved"].append(n))
    return calls


def test_binds_newest_after_pending(monkeypatch):
    accounts = [_acc("acc_old", "Seat", created="2026-07-16 11:00:00+00"),
                _acc("acc_new", "Me", created="2026-07-16 12:45:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts)
    # Un siège antérieur à la demande ne se lie pas, même désigné.
    assert uc.reconcile_pending("sub1", account_id="acc_old")["bound"] is False
    assert calls["set"] == []
    out = uc.reconcile_pending("sub1", account_id="acc_new")
    assert out["bound"] is True
    assert out["accounts"][0]["account_id"] == "acc_new"
    assert calls["set"][0][0][:2] == ("sub1", "acc_new")  # (sub, account_id)
    assert calls["resolved"] == ["N"]


def test_excludes_already_bound(monkeypatch):
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_new", "Me")], bound={"acc_new"})
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and calls["set"] == []


def test_skips_dead_session_keeps_the_only_alive(monkeypatch):
    # deux candidats après le pending : le plus récent est MORT (401) → on prend le vivant
    accounts = [_acc("acc_alive", "Sain", created="2026-07-16 12:45:00+00"),
                _acc("acc_dead", "MortNé", created="2026-07-16 12:50:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts, alive_ids={"acc_alive"})
    assert uc.reconcile_pending("sub1", account_id="acc_dead")["reason"] == "candidates_dead"
    out = uc.reconcile_pending("sub1", account_id="acc_alive")
    assert out["accounts"][0]["account_id"] == "acc_alive"


def test_binds_nothing_when_all_dead(monkeypatch):
    accounts = [_acc("acc_dead", "MortNé", created="2026-07-16 12:50:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts, alive_ids=set())
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and calls["set"] == []


def test_excludes_account_before_floor(monkeypatch):
    # seul compte dispo est ANTÉRIEUR au pending (>5 min) → jamais rebindé (siège tiers)
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_old", "Seat", created="2026-07-16 11:00:00+00")])
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False


def test_un_siege_orphelin_sans_date_lisible_n_est_pas_lie(monkeypatch):
    """#580 : un siège présent sur l'abonnement partagé et lié à PERSONNE chez nous.
    Sans date de création lisible, rien ne prouve qu'il est né de CETTE demande : la
    réconciliation le gardait (« date illisible → on garde ») — la garde le refuse,
    même nommé par l'indice `account_id`."""
    for created in (None, "pas une date"):
        calls = _setup(monkeypatch, [_pend()], [_acc("acc_orphelin", "?", created=created)])
        assert uc.reconcile_pending("sub1")["bound"] is False
        assert uc.reconcile_pending("sub1", account_id="acc_orphelin")["bound"] is False
        assert calls["set"] == []


def test_sans_date_de_demande_seul_mon_compte_se_relie(monkeypatch):
    calls = _setup(monkeypatch, [_pend(ts=None)], [_acc("acc_new", "Neuf")])
    assert uc.reconcile_pending("sub1")["bound"] is False
    calls = _setup(monkeypatch, [_pend(ts=None)],
                   [_acc("acc_mine", "Moi", created="2026-07-16 11:00:00+00")],
                   bound={"acc_mine"}, dead={"acc_mine"})
    assert uc.reconcile_pending("sub1")["bound"] is True


def test_rebinds_own_dead_account_despite_floor(monkeypatch):
    # reconnexion : Unipile RÉUTILISE le compte (antérieur au pending) — la ligne
    # soft-déconnectée du sub est la preuve de propriété → rebind déterministe,
    # même si l'account_id figure dans bound (les morts y sont, anti-tiers).
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_mine", "Moi", created="2026-07-16 11:00:00+00")],
                   bound={"acc_mine"}, dead={"acc_mine"})
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is True
    assert out["accounts"][0]["account_id"] == "acc_mine"


def test_never_rebinds_dead_account_of_third_party(monkeypatch):
    # ligne morte d'un TIERS (dans bound, pas dans MES morts) + antérieure → intouchable
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_tiers", "Autre", created="2026-07-16 11:00:00+00")],
                   bound={"acc_tiers"}, dead=set())
    assert uc.reconcile_pending("sub1")["bound"] is False


def test_provider_mismatch_ignored(monkeypatch):
    calls = _setup(monkeypatch, [_pend(provider="LINKEDIN")],
                   [_acc("acc_wa", "WA", provider="whatsapp")])
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False


def test_no_pending_is_noop(monkeypatch):
    """No-op = rien de lié ET aucun appel au fournisseur.

    ⚠️ Ce test comparait la réponse ENTIÈRE à `{bound: False, accounts: []}`. Cette
    forme exacte était le MOYEN de vérifier le no-op, pas son objet — et elle a cassé
    dès que la réponse a gagné la raison du refus (#689). Réécrit sur ce que son nom
    annonce : le `set_unipile_account` non appelé et la liste des comptes jamais
    demandée sont ce qui fait qu'il ne se passe rien."""
    calls = _setup(monkeypatch, [], [])
    vus = []
    import oto.tools.unipile as core
    monkeypatch.setattr(core, "make_unipile_client",
                        lambda **k: vus.append("client") or types.SimpleNamespace(
                            list_accounts=lambda: [], account_alive=lambda a: True))
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and out["accounts"] == []
    assert calls["set"] == [] and calls["resolved"] == []
    assert vus == [], "sans pending, le fournisseur ne doit même pas être contacté"


# ── #689 : une réconciliation qui ne lie rien DIT pourquoi ────────────────────
#
# Vécu le 03/09 : un utilisateur suit le parcours hosted-auth DEUX fois, la seconde
# jusqu'à la redirection finale, attend plusieurs minutes — et lit `connected:false`.
# Rien, nulle part, ne lui disait ce qui avait manqué. Les six sorties de cette
# fonction rendaient toutes le même `{bound: False}`.
#
# C'est la doctrine que ce module écrit en tête, pour `BindOutcome`, et que sa voisine
# immédiate n'appliquait pas : « un refus muet est un refus que personne ne saura
# avoir eu ».

def test_sans_pending_la_raison_est_dite(monkeypatch):
    _setup(monkeypatch, [], [])
    out = uc.reconcile_pending("u1")
    assert out["bound"] is False and out["reason"] == "no_pending"
    # Et le message dit le GESTE, pas seulement l'état.
    assert "op=connect" in out["detail"]


def test_aucun_candidat_nomme_les_trois_causes_possibles(monkeypatch):
    """LE cas du signalement : le parcours s'est terminé côté fournisseur et pourtant
    aucun compte n'est éligible. Trois causes indiscernables jusqu'ici — compte
    jamais créé, compte antérieur au pending, compte appartenant à un tiers."""
    _setup(monkeypatch, [_pend()], [_acc("A", "vieux", created="2026-07-01 09:00:00+00")])
    out = uc.reconcile_pending("u1")
    assert out["bound"] is False and out["reason"] == "no_candidate"
    assert "older than the pending row" in out["detail"]
    assert "someone else" in out["detail"]
    # Le compte-rendu porte AUSSI le nonce : deux demandes en attente ne se
    # confondent pas dans une seule phrase.
    assert out["pendings"][0]["nonce"] == "N"


def test_candidats_tous_morts_dit_quoi_refaire(monkeypatch):
    """Un wizard avorté produit un compte que le fournisseur n'authentifie plus.
    L'utilisateur doit apprendre qu'il faut refaire le parcours, pas attendre."""
    _setup(monkeypatch, [_pend()], [_acc("A", "mort")], alive_ids=set())
    out = uc.reconcile_pending("u1")
    assert out["reason"] == "candidates_dead"
    assert "final redirect" in out["detail"]


def test_une_liaison_REUSSIE_ne_porte_aucune_raison(monkeypatch):
    """Pas d'écart, pas de bruit : le succès ne s'encombre pas d'un champ d'échec."""
    _setup(monkeypatch, [_pend()], [_acc("A", "bon")])
    monkeypatch.setattr(uc.db, "resolve_unipile_pending", lambda n: None)
    out = uc.reconcile_pending("u1", account_id="A")
    assert out["bound"] is True
    assert "reason" not in out and "detail" not in out


# ── Indice de retour et demandes doublons (retour de revue, 2026-09-14) ──────
# Sur une clé PARTAGÉE, « le plus récent compte non lié créé après le pending » peut
# être celui d'un tiers qui connecte dans la même heure. Le front qui reçoit le retour
# connaît l'`account_id` rendu par Unipile : il restreint la liaison à ce compte.

def test_account_id_restreint_la_liaison_a_ce_compte(monkeypatch):
    accounts = [_acc("acc_mine", "Moi", created="2026-07-16 12:45:00+00"),
                _acc("acc_theirs", "Un tiers", created="2026-07-16 12:50:00+00")]
    calls = _setup(monkeypatch, [_pend()], accounts)
    out = uc.reconcile_pending("sub1", account_id="acc_mine")
    assert out["bound"] is True and out["accounts"][0]["account_id"] == "acc_mine"
    assert calls["set"][0][0][:2] == ("sub1", "acc_mine")


def test_account_id_ne_leve_aucune_garde(monkeypatch):
    """Un identifiant forgé ne désigne qu'un compte que la sélection aurait pu retenir :
    déjà lié ou antérieur au pending, il reste refusé."""
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_x", "Pris")], bound={"acc_x"})
    assert uc.reconcile_pending("sub1", account_id="acc_x")["bound"] is False
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_old", "Vieux", created="2026-07-01 09:00:00+00")])
    assert uc.reconcile_pending("sub1", account_id="acc_old")["bound"] is False
    assert calls["set"] == []


def test_account_id_inconnu_ne_lie_rien(monkeypatch):
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_new", "Moi")])
    out = uc.reconcile_pending("sub1", account_id="acc_ailleurs")
    assert out["bound"] is False and out["reason"] == "no_candidate" and calls["set"] == []


def test_une_liaison_consomme_les_demandes_doublons_du_meme_canal(monkeypatch):
    """Double clic sur « Connecter » : deux pendings LinkedIn. Le premier lie ; le second
    ne doit pas rester une heure prêt à lier le prochain compte d'un tiers."""
    pendings = [_pend(nonce="N1"), _pend(nonce="N2"), _pend(nonce="W", provider="WHATSAPP")]
    calls = _setup(monkeypatch, pendings,
                   [_acc("acc_li", "Moi"), _acc("acc_wa", "+33", provider="whatsapp")])
    out = uc.reconcile_pending("sub1", account_id="acc_li")
    assert [a["account_id"] for a in out["accounts"]] == ["acc_li"]
    assert calls["resolved"].count("N1") == 1 and "N2" in calls["resolved"]
    # L'indice ne désigne que le compte LinkedIn : la demande WhatsApp attend le sien.
    assert "W" not in calls["resolved"]
    assert len([s for s in calls["set"] if s[0][1] == "acc_li"]) == 1


def test_le_motif_ne_revele_pas_le_nombre_de_comptes_de_la_cle(monkeypatch):
    _setup(monkeypatch, [_pend()], [_acc("A", "vieux", created="2026-07-01 09:00:00+00")] * 3)
    out = uc.reconcile_pending("u1")
    assert "account(s) at the provider" not in out["detail"]
    assert not any(ch.isdigit() for ch in out["detail"].split(":")[0])


def test_la_date_du_fournisseur_se_lit_sous_toutes_ses_formes():
    """#580 : une date illisible REFUSE la liaison. Les formes que sert le fournisseur
    doivent donc se lire sur toutes les versions de Python supportées — `Z` final et
    fraction de 3 chiffres échouaient en 3.10 et bloquaient toute connexion."""
    attendu = datetime(2026, 7, 16, 11, 0, 49, tzinfo=timezone.utc)
    for v in ("2026-07-16 11:00:49+00", "2026-07-16T11:00:49Z", "2026-07-16T11:00:49.000Z",
              "2026-07-16T11:00:49.0000000+00:00", "2026-07-16T13:00:49+02:00",
              1784199649, 1784199649000):
        assert uc._parse_dt(v).replace(microsecond=0) == attendu, v
    for v in (None, "", "pas une date", True):
        assert uc._parse_dt(v) is None, v


def test_une_date_iso_en_z_se_lie(monkeypatch):
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_new", "Moi", created="2026-07-16T12:45:00.123Z")])
    assert uc.reconcile_pending("sub1", account_id="acc_new")["bound"] is True


# ── oto#247 : sans preuve, on ne choisit pas entre comptes simultanément éligibles ──
#
# La clé est PARTAGÉE entre orgs. Deux personnes qui connectent dans la même fenêtre
# rendent leurs deux comptes candidats pour chacune ; « le plus récent vivant » a lié
# le compte de l'une à l'autre (occurrence réelle, 14/09). La face agent n'avait
# aucun moyen de passer l'indice qui l'aurait évité.

def _deux_connexions_simultanees():
    return [_acc("acc_a", "Personne A", created="2026-07-16 12:44:00+00"),
            _acc("acc_b", "Personne B", created="2026-07-16 12:47:00+00")]


def test_deux_candidats_vivants_sans_preuve_REFUS_nomme_rien_ecrit(monkeypatch):
    calls = _setup(monkeypatch, [_pend()], _deux_connexions_simultanees())
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and out["reason"] == "ambiguous_candidates"
    assert calls["set"] == [], "un compte a été lié alors que deux étaient candidats"
    # Le pending RESTE : l'appel porteur de l'`account_id` doit pouvoir lier ensuite.
    assert calls["resolved"] == []
    # Le refus dit le geste (repasser l'`account_id` du retour) sans révéler ni le
    # nombre de comptes de la clé ni un identifiant de compte.
    assert "account_id=" in out["detail"]
    assert "acc_a" not in str(out) and "acc_b" not in str(out)
    assert not any(ch.isdigit() for ch in out["detail"])


def test_deux_candidats_avec_account_id_lie_le_bon(monkeypatch):
    for mien in ("acc_a", "acc_b"):     # le plus ancien comme le plus récent
        calls = _setup(monkeypatch, [_pend()], _deux_connexions_simultanees())
        out = uc.reconcile_pending("sub1", account_id=mien)
        assert out["bound"] is True and out["accounts"][0]["account_id"] == mien
        assert [c[0][:2] for c in calls["set"]] == [("sub1", mien)]
        assert calls["resolved"] == ["N"]


def test_ligne_morte_du_sub_est_une_preuve(monkeypatch):
    """Reconnexion : Unipile réutilise le compte, la ligne morte du sub prouve qu'il est
    à lui — seul candidat vivant, il se relie sans indice."""
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_mine", "Moi", created="2026-07-16 11:00:00+00")],
                   bound={"acc_mine"}, dead={"acc_mine"})
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is True and out["accounts"][0]["account_id"] == "acc_mine"


def test_ligne_morte_ET_compte_neuf_vivants_REFUS(monkeypatch):
    """La ligne morte prouve que CE compte est à moi, pas que le compte neuf ne l'est
    pas (ni qu'il l'est) : rien ne départage, on refuse."""
    calls = _setup(monkeypatch, [_pend()],
                   [_acc("acc_mine", "Moi", created="2026-07-16 11:00:00+00"),
                    _acc("acc_neuf", "?", created="2026-07-16 12:45:00+00")],
                   bound={"acc_mine"}, dead={"acc_mine"})
    out = uc.reconcile_pending("sub1")
    assert out["reason"] == "ambiguous_candidates" and calls["set"] == []


def test_candidat_unique_sans_preuve_REFUS(monkeypatch):
    """Un seul compte neuf, personne d'autre n'attend, aucune preuve : refus. La demande
    dit qui a DEMANDÉ le lien, pas qui l'a parcouru — un lien transféré à quelqu'un
    d'autre rattachait SON compte à l'auteur du lien (même classe qu'ADA 3.2.2 pour
    OAuth). La preuve est l'`account_id` de la page de retour, que le front relit."""
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_a", "A")])
    out = uc.reconcile_pending("sub1")
    assert out["bound"] is False and out["reason"] == "proof_required"
    assert calls["set"] == [] and calls["resolved"] == []
    assert "account_id=" in out["detail"] and "acc_a" not in str(out)


def test_candidat_unique_avec_account_id_LIE(monkeypatch):
    calls = _setup(monkeypatch, [_pend()], [_acc("acc_a", "A")])
    out = uc.reconcile_pending("sub1", account_id="acc_a")
    assert out["bound"] is True and out["accounts"][0]["account_id"] == "acc_a"
