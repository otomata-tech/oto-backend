"""Une version qui RETIRE des sections doit le dire (oto#61, signal 644).

Mesuré sur une procédure de 132 versions : 22 colonnes orphelines, trois coupes datées,
dont une de trois sections sans un mot dans un digest par ailleurs méticuleux. Le digest
raconte ce qu'on ajoute ; rien n'obligeait à dire ce qu'on retire.

Ces bancs tiennent les deux moitiés : le retrait se voit, et **ce qui n'est pas un
retrait ne fabrique aucun bruit** — un avertissement qu'on reçoit toujours cesse d'être
lu, ce qui est précisément le sort du journal d'appels que ce lot ne veut pas répéter.
"""
from __future__ import annotations

from oto_mcp import procedure_retrait as R

AVANT = """# Enrichir une fiche

## Goal
Compléter les fiches du vivier.

## Étapes
1. lire la fiche

## Nombre d'établissements
Le compter depuis le registre.

## Appartenance de groupe
La chercher, gérer les homonymes.

## Sortie
Écrire la ligne.
"""


def test_une_coupe_est_nommee_section_par_section():
    """Le cas mesuré : deux sections partent, le reste est conservé. C'est ce que le
    digest de la version fautive ne disait pas."""
    apres = AVANT.replace("""## Nombre d'établissements
Le compter depuis le registre.

""", "").replace("""## Appartenance de groupe
La chercher, gérer les homonymes.

""", "")
    w = R.retrait_check(AVANT, apres)["retrait_warning"]
    assert w and "Nombre d'établissements" in w and "Appartenance de groupe" in w
    assert "2 section" in w
    assert "Étapes" not in w, "une section conservée ne doit pas être annoncée partie"


def test_une_version_qui_n_enleve_rien_ne_dit_rien():
    """La moitié qui décide si l'avertissement sera lu : ajouter du contenu, corriger
    une phrase, réordonner — rien de tout ça n'est un retrait."""
    apres = AVANT + "\n## Contrôle\nVérifier le compte.\n"
    assert R.retrait_check(AVANT, apres)["retrait_warning"] is None


def test_reformuler_le_corps_sans_toucher_aux_titres_est_muet():
    """Un diff de prose produirait du bruit à chaque reformulation. On regarde les
    titres, exprès."""
    apres = AVANT.replace("Compléter les fiches du vivier.",
                          "Compléter chaque fiche du vivier, sans doublon.")
    assert R.retrait_check(AVANT, apres)["retrait_warning"] is None


def test_la_casse_et_les_espaces_ne_font_pas_un_retrait():
    """Signaler « ## Étapes » → « ## étapes » apprendrait à ignorer l'avertissement."""
    apres = AVANT.replace("## Étapes", "##   étapes  ")
    assert R.retrait_check(AVANT, apres)["retrait_warning"] is None


def test_un_titre_dans_un_bloc_de_code_n_est_pas_une_section():
    """`# commentaire` dans un exemple shell n'est pas une consigne : le compter ferait
    crier au retrait à chaque exemple modifié."""
    avant = AVANT + "\n```bash\n# compter les lignes\nwc -l fichier\n```\n"
    apres = AVANT + "\n```bash\n# compter les fiches\nwc -l fichier\n```\n"
    assert R.retrait_check(avant, apres)["retrait_warning"] is None


def test_une_creation_ne_retire_rien():
    """Pas d'ancien corps : il n'y a rien dont on puisse dire qu'il a été retiré."""
    assert R.retrait_check("", AVANT)["retrait_warning"] is None
    assert R.retrait_check("   ", AVANT)["retrait_warning"] is None


def test_une_reecriture_complete_compte_au_lieu_d_enumerer():
    """Un avertissement qui déroule quarante noms ne se lit pas — et une réécriture
    complète n'a pas besoin d'un inventaire pour être reconnue par son auteur."""
    avant = "\n\n".join(f"## Section {i}\ntexte" for i in range(20))
    w = R.retrait_check(avant, "## Tout neuf\ntexte")["retrait_warning"]
    assert w and "20 section" in w and "de plus" in w


def test_le_check_ne_leve_jamais():
    """Best-effort, comme ses trois voisins : un contrôle de forme ne casse pas une
    écriture. Un `None` ne dit d'ailleurs pas « rien n'a été retiré », seulement
    « aucune section entière n'a disparu »."""
    assert R.retrait_check(None, None)["retrait_warning"] is None
    assert "retrait_warning" in R.retrait_check(AVANT, "")


# ── la remontée par la SURFACE, pas seulement par la fonction ────────────────
# Un check qui marche et qu'aucune face ne sert ne prévient personne. C'est le
# trajet complet qui compte : l'écriture lit le corps précédent, compare, et pose
# l'avertissement dans sa réponse.
import asyncio  # noqa: E402

from oto_mcp.capabilities.orgs import instructions as oi  # noqa: E402


class _Ctx:
    sub = "u1"
    org_id = 7


class _Inp:
    slug = "ma-procedure"
    title = None
    description = None
    from_version = None
    slots = None
    org = None

    def __init__(self, body_md):
        self.body_md = body_md


def _ecrire(monkeypatch, precedent: str, nouveau: str, must_create: bool = False):
    monkeypatch.setattr(oi.org_store, "set_instruction", lambda *a, **k: 3)
    monkeypatch.setattr(oi.org_store, "get_instruction",
                        lambda *a, **k: {"slots": [], "body_md": precedent})

    async def _wc(body_md, **k):
        return {"referenced_tools": [], "unresolved_tools": []}
    monkeypatch.setattr(oi.tool_registry, "write_check", _wc)
    return asyncio.run(oi._set_instruction(_Ctx(), _Inp(nouveau), must_create=must_create))


def test_l_ecriture_remonte_l_avertissement_de_retrait(monkeypatch):
    out = _ecrire(monkeypatch, AVANT, "# Enrichir une fiche\n\n## Goal\nCompléter.\n")
    assert out["retrait_warning"], "l'écriture doit servir l'avertissement"
    assert "Étapes" in out["retrait_warning"]
    assert out["ok"] is True and out["version"] == 3, (
        "non bloquant, comme ses trois voisins : l'écriture a bien eu lieu")


def test_l_ecriture_est_muette_quand_rien_ne_part(monkeypatch):
    out = _ecrire(monkeypatch, AVANT, AVANT + "\n## Contrôle\nVérifier.\n")
    assert out["retrait_warning"] is None


def test_une_creation_ne_lit_pas_le_corps_precedent(monkeypatch):
    """`must_create` : il n'y a rien avant, donc rien à comparer — et pas de lecture
    à payer."""
    out = _ecrire(monkeypatch, AVANT, "## Neuf\ntexte", must_create=True)
    assert out["retrait_warning"] is None


def test_un_corps_precedent_illisible_ne_casse_pas_l_ecriture(monkeypatch):
    """Best-effort : la panne de CE contrôle n'a pas à faire échouer le geste qu'il
    observe. On fait échouer la seule lecture du retrait — la suivante (les slots
    effectifs) répond normalement.

    ⚠️ Ce que ce banc NE prouve pas, et il ne faut pas le lui faire dire : si la base
    est réellement indisponible, l'écriture échouera de toute façon plus loin — la
    relecture des slots effectifs, elle, n'est pas protégée (comportement préexistant,
    hors de ce lot). Ce qui est vérifié ici est que le contrôle de retrait n'AJOUTE
    aucun mode de panne."""
    appels = {"n": 0}

    def _lecture(*a, **k):
        appels["n"] += 1
        if appels["n"] == 1:
            raise RuntimeError("corps précédent illisible")
        return {"slots": []}
    monkeypatch.setattr(oi.org_store, "set_instruction", lambda *a, **k: 3)
    monkeypatch.setattr(oi.org_store, "get_instruction", _lecture)

    async def _wc(body_md, **k):
        return {"referenced_tools": [], "unresolved_tools": []}
    monkeypatch.setattr(oi.tool_registry, "write_check", _wc)
    out = asyncio.run(oi._set_instruction(_Ctx(), _Inp("## Neuf\ntexte")))
    assert out["ok"] is True and out["retrait_warning"] is None
    assert appels["n"] >= 1, "la lecture du corps précédent doit bien avoir été tentée"


def test_le_champ_est_declare_dans_le_modele_servi():
    """Un champ absent du modèle serait filtré à la sérialisation : le check
    tournerait et personne ne le verrait."""
    assert "retrait_warning" in oi.InstructionWritten.model_fields
