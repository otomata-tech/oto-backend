"""Premier temps du préavis sur la couche `origine` (oto#70 lot 2).

L'origine est la valeur du départ, à l'import. La poser SANS LE DÉCLARER va devenir
refusé — mais on ne coupe pas d'abord et on compte ensuite : **on prévient, on mesure,
puis on refuse à une date annoncée.**

⚠️ **Ce n'est pas un droit qu'on réserve, c'est une déclaration qu'on exige** (décision
d'Alexis, 05/09/2026 : « c'est notre modèle d'agent experience »). Écrire l'origine
restera possible pour tout le monde ; ce qui disparaît, c'est de le faire en silence.
Le remplaçant est donc un PARAMÈTRE nommé, absent par défaut, dont la présence engage
celui qui l'envoie — rien à demander à personne, rien à provisionner.

⚠️ **Ce premier temps EST l'instrument.** Le journal d'appels ne peut pas dire qui écrit
une couche : il ne garde que les clés de premier niveau et tronque les arguments. Seuls
les écrivains peuvent donc nous dire combien ils sont — l'avertissement les fait se
déclarer, le relevé les compte.

Mesuré avant d'écrire : 64 cellules portent une origine sur une colonne sans format
déclaré, contre 15 688 avec — et le flux **accélère** (7 lignes la semaine du 10 août,
52 celle du 31), chaque ligne ayant été réécrite après sa création. Ce n'est pas un
import figé : quelque chose repasse dessus.
"""
from __future__ import annotations

import pytest

from oto_mcp.datastore import schema as dsv2


# ── ce que l'appel POSE, indépendamment du format ─────────────────────────────

def test_poser_une_origine_est_releve():
    assert dsv2.origine_posee({"col": {"valeur": "x", "origine": "forgé"}}) == ["col"]


def test_reecrire_la_MEME_origine_ne_releve_rien():
    """⚠️ Relire puis repousser tel quel est un geste banal. Le compter ferait crier
    l'avertissement sur des appels qui ne changent rien — et un avertissement qu'on
    reçoit toujours cesse d'être lu, ce qui détruirait l'instrument."""
    avant = {"col": {"valeur": "B", "origine": "A"}}
    assert dsv2.origine_posee({"col": {"valeur": "x", "origine": "A"}}, avant) == []


def test_ecrire_la_valeur_seule_ne_releve_rien():
    """Le geste qu'on recommande ne doit surtout pas déclencher l'avertissement."""
    assert dsv2.origine_posee({"col": "x"}, {"col": {"valeur": "B", "origine": "A"}}) == []


def test_effacer_une_origine_est_releve_aussi():
    """`{"origine": null}` retire une origine en place : c'est une modification de la
    couche, pas une abstention."""
    avant = {"col": {"valeur": "B", "origine": "A"}}
    assert dsv2.origine_posee({"col": {"origine": None}}, avant) == ["col"]


# ── la phrase servie ──────────────────────────────────────────────────────────

def test_l_avertissement_VOUVOIE_et_nomme_les_DEUX_gestes(monkeypatch):
    """Elle est lue par une personne, et un avertissement qui ne dit pas quoi faire à
    la place ne fait que gêner. Deux gestes, parce que deux situations : celui qui n'a
    pas besoin d'écrire l'origine ne doit pas ajouter un paramètre pour rien, et celui
    qui en a besoin ne doit pas réécrire son import."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    texte = dsv2.avertissement_origine(["prio"])
    assert "écrivez la valeur seule" in texte.lower()
    assert f"`{dsv2.PARAMETRE_ORIGINE}: true`" in texte
    assert "`prio`" in texte
    import re
    assert not re.search(r"\b(ton|ta|tes|tu|écris)\b", texte, re.I), texte


def test_l_avertissement_NOMME_le_parametre_et_le_lit_dans_la_constante(monkeypatch):
    """⚠️ **Cette phrase est la SEULE annonce** : décision d'Alexis (05/09/2026), aucun
    client ne sera prévenu par un envoi. Pas de courriel, pas de note de version — ce
    texte, répété à chaque écriture, est tout ce que l'écrivain aura. Il doit donc porter
    le geste EXACT, pas une démarche à entreprendre.

    La substitution est ce qui rend ce banc utile : si la phrase recopiait le nom en
    littéral, renommer la constante servirait un paramètre qui n'existe pas."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    avant = dsv2.avertissement_origine(["prio"])
    assert dsv2.PARAMETRE_ORIGINE in avant
    monkeypatch.setattr(dsv2, "PARAMETRE_ORIGINE", "zzz_sentinelle")
    apres = dsv2.avertissement_origine(["prio"])
    assert apres != avant, "AUCUNE substitution : la phrase recopie un littéral"
    assert "zzz_sentinelle" in apres


def test_l_avertissement_ne_renvoie_vers_PERSONNE(monkeypatch):
    """Il n'y a rien à demander, et le dire autrement enverrait un agent attendre une
    réponse qui ne viendra jamais — ni de nous, ni de son propriétaire."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    import re
    texte = dsv2.avertissement_origine(["prio"])
    interdits = r"\b(demandez|permission|autorisation|habilitation|contactez)\b"
    assert not re.search(interdits, texte, re.I), texte


def test_l_avertissement_dit_que_l_ecriture_RESTE_possible(monkeypatch):
    """Le barreau 1 annonçait un droit à venir ; il annonce désormais une déclaration.
    Un avertissement qui laisse croire que la capacité disparaît ferait réécrire des
    imports qui n'ont rien à changer."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    texte = dsv2.avertissement_origine(["prio"])
    assert "reste possible" in texte
    assert "SANS LE DIRE" in texte


def test_la_date_de_refus_vient_d_un_REGLAGE_pas_du_code(monkeypatch):
    """⚠️ La fenêtre bougera si un écrivain se manifeste. Une date gravée dans le code
    demanderait un déploiement pour se déplacer — et on la déplacerait donc moins."""
    monkeypatch.delenv(dsv2.ENV_ORIGINE_REFUS_LE, raising=False)
    assert "prochainement" in dsv2.avertissement_origine(["c"])
    monkeypatch.setenv(dsv2.ENV_ORIGINE_REFUS_LE, "1er octobre 2026")
    assert "1er octobre 2026" in dsv2.avertissement_origine(["c"])


def test_l_avertissement_part_a_CHAQUE_ecriture_pas_une_seule_fois():
    """Un écrivain qui repasse sur une ligne par semaine ne verrait jamais un message
    servi une seule fois — et c'est exactement le profil mesuré."""
    from oto_mcp.datastore.core import DatastorePg

    st = DatastorePg("u1")
    assert st.off_schema_report() == {}
    st._origine_posee.add("prio")
    for _ in range(3):
        assert "origine_warning" in st.off_schema_report()


def test_rien_ne_BLOQUE_au_premier_temps():
    """On prévient, on ne refuse pas : `origine_posee` ne lève jamais, et le relevé
    n'a aucun pouvoir d'arrêt. Le refus est le barreau suivant."""
    gros = {f"c{i}": {"valeur": "x", "origine": "f"} for i in range(50)}
    assert len(dsv2.origine_posee(gros)) == 50   # relevé, pas refusé


def test_le_releve_SEPARE_le_cas_suspect(monkeypatch):
    """⚠️ LE discriminant. Sur une colonne sans format, la plateforme ne pose jamais
    d'origine : celle-ci vient forcément de l'écrivain. Fondre les deux populations
    ferait disparaître celle qu'on cherche (64) dans celle qui l'entoure (15 688)."""
    from oto_mcp.datastore import core as C

    releves: list = []
    monkeypatch.setattr("oto_mcp.db.origine_ecritures.relever",
                        lambda **kw: releves.append(kw) or len(kw.get("colonnes") or []))

    class _Store:
        sub, acting_org = "u1", None

        def __init__(self):
            self._origine_posee = set()

    schema = {"fields": [{"key": "declaree", "origine": "system"}, {"key": "libre"}]}
    C._relever_origine_module(
        _Store(), 42,
        {"declaree": {"valeur": "x", "origine": "a"},
         "libre": {"valeur": "y", "origine": "b"}},
        schema=schema)
    par_cas = {r["format_declare"]: r["colonnes"] for r in releves if r["colonnes"]}
    assert par_cas[False] == ["libre"], "le cas SUSPECT doit être relevé à part"
    assert par_cas[True] == ["declaree"]
