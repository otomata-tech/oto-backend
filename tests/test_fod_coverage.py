"""Garde-fou anti-orphelin : couverture france-opendata → connecteurs (#79).

Le catalogue de connecteurs n'est PAS dérivé de la lib `france_opendata` (FOD) :
chaque module data doit être câblé à la main (`tools/*.py` + registre). Un module
ajouté à FOD restait donc orphelin par défaut, sans alarme (vécu 2026-06-30 :
`culture_spectacle` resté un temps non câblé ; `reglement` non exposé).

Ce test force une DÉCISION par module : tout module data de FOD (= qui expose une
classe `*Client`) doit être soit référencé par le code serveur, soit inscrit dans
une allowlist documentée avec sa raison de non-exposition. AUTO-MAINTENU : câbler
un nouveau module FOD (import dans `oto_mcp/`) le sort tout seul du radar ; il ne
casse que sur un module FOD ni câblé ni décidé, ou une entrée d'allowlist périmée.

Complète `test_tools_module_derivation_matches_filesystem` (drift tools ↔ registre),
qui n'attrape qu'un FICHIER tool orphelin, pas un module data FOD non câblé.
"""
import ast
import importlib
import inspect
import pathlib
import pkgutil

import pytest

# Décision explicite de NON-exposition, par module data FOD (la raison est le
# contrat : un module qu'on choisit de ne pas exposer se justifie ici, un oubli
# n'a pas de raison à donner → il casse le test).
# Clients foncier (données de site) consommés via le service FOD en proxy HTTP
# LIVE (oto_mcp/fod_foncier.py → /api/foncier/*, ADR 0028 extraction totale, B1) :
# le backend n'exécute plus ces appels in-process → plus d'import du client lib
# direct. Exposés à l'utilisateur (tools foncier_*), mais pas via la classe lib.
_FONCIER_VIA_FOD = (
    "client foncier consommé via le service FOD dédié en proxy HTTP live "
    "(oto_mcp/fod_foncier.py → /api/foncier/*, ADR 0028 extraction totale) — "
    "le tool foncier_* reste exposé, mais plus via le client lib in-process"
)

# Idem pour les données entreprise (B2a) : entreprises/BODACC/Egapro en proxy HTTP,
# INPI en DuckDB isolé sur FOD (parquet Signaux Faibles, hors event-loop backend).
_FR_VIA_FOD = (
    "client données entreprise consommé via le service FOD dédié "
    "(oto_mcp/fod_fr.py → /api/fr/*, ADR 0028 extraction totale — INPI = DuckDB "
    "parquet isolé) — le tool fr_* reste exposé, plus via le client lib in-process"
)

FOD_NOT_EXPOSED = {
    "judilibre": "client Judilibre (jurisprudence) = source d'INGESTION du service "
                 "FOD (fod-0, épopée DILA) ; le backend consomme la jurisprudence "
                 "via le service FOD (fr_juris_*, oto_mcp/fod_juris.py → HTTP), pas "
                 "le client lib direct",
    "legifrance": "client Légifrance/PISTE (codes consolidés, texte d'accords) = "
                  "source d'INGESTION du service FOD (fod-0) ; le backend consomme "
                  "les codes via le service FOD (fr_loi_*, oto_mcp/fod_loi.py → HTTP), "
                  "pas le client lib direct",
    "ban": _FONCIER_VIA_FOD,
    "apicarto": _FONCIER_VIA_FOD,
    "bdtopo": _FONCIER_VIA_FOD,
    "pvgis": _FONCIER_VIA_FOD,
    "enedis": _FONCIER_VIA_FOD,
    "dvf": _FONCIER_VIA_FOD,
    "dpe": _FONCIER_VIA_FOD,
    # Clients « fr » (données entreprise) consommés via le service FOD (B2a) :
    # entreprises/BODACC/Egapro = proxy HTTP live, INPI = DuckDB parquet isolé.
    # oto_mcp/fod_fr.py → /api/fr/*. INSEE SIRENE (keyé) reste, lui, au backend.
    "entreprises": _FR_VIA_FOD,
    "bodacc": _FR_VIA_FOD,
    "inpi": _FR_VIA_FOD,
    "egapro": _FR_VIA_FOD,
}

# Modules qui exposent un *Client mais ne sont PAS des sources de données :
# infrastructure/transport réutilisée par d'autres modules FOD. (Les utils purs
# sans *Client — geo, finance, reglement, acco, sirene_stock, *_ingest — sont
# auto-exclus par l'énumération.)
FOD_PURE_UTILS = {
    "opendatasoft",  # client générique Opendatasoft (transport), pas une source
}


def _fod_data_modules() -> dict[str, set[str]]:
    """Modules « source de données » de france_opendata : {module: {classes *Client
    définies dedans}}. Un sous-module non importable (extra absent de cet env) est
    ignoré — il ne peut pas non plus être câblé au runtime de cet env."""
    fod = pytest.importorskip("france_opendata")
    out: dict[str, set[str]] = {}
    for m in pkgutil.iter_modules(fod.__path__):
        try:
            mod = importlib.import_module(f"france_opendata.{m.name}")
        except Exception:
            continue
        clients = {n for n, o in vars(mod).items()
                   if inspect.isclass(o) and o.__module__ == mod.__name__
                   and n.endswith("Client")}
        if clients:
            out[m.name] = clients
    return out


def _fod_references() -> set[str]:
    """Tout ce que le code serveur importe de france_opendata : sous-modules ET
    noms (classes/fonctions/sous-modules via `from france_opendata import X`).
    Les imports `oto.tools.*` comptent aussi : oto-core RÉ-EXPORTE plusieurs
    clients FOD (ex. `oto.tools.sirene` → `france_opendata.sirene`) — le câblage
    passe alors par le wrapper, pas par un import FOD direct. Walk AST de
    `oto_mcp/**/*.py` — pas de faux positif docstring/commentaire."""
    root = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"
    refs: set[str] = set()
    for p in root.rglob("*.py"):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.name.startswith("france_opendata."):
                        refs.add(a.name.split(".")[1])
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module == "france_opendata":
                    refs.update(a.name for a in node.names)
                elif node.module.startswith("france_opendata."):
                    refs.add(node.module.split(".")[1])
                elif node.module.startswith("oto.tools"):
                    # wrappers oto-core : on matche par NOM de classe *Client
                    refs.update(a.name for a in node.names
                                if a.name.endswith("Client"))
    return refs


def _is_wired(module: str, clients: set[str], refs: set[str]) -> bool:
    """Un module data est câblé si le serveur importe : une de ses classes *Client,
    le module lui-même, OU un module frère de la même source (`<module>_ingest` —
    ex. boamp est exposé via l'ingest parquet `boamp_ingest`, pas via BoampClient)."""
    if clients & refs or module in refs:
        return True
    return any(r.startswith(f"{module}_") for r in refs)


def test_every_fod_data_module_is_wired_or_decided():
    modules = _fod_data_modules()
    refs = _fod_references()

    orphans = {
        m: sorted(clients) for m, clients in modules.items()
        if m not in FOD_NOT_EXPOSED and m not in FOD_PURE_UTILS
        and not _is_wired(m, clients, refs)
    }
    assert not orphans, (
        f"modules data france_opendata ORPHELINS (ni câblés ni décidés) : {orphans} "
        "— câbler dans un tools/*.py (+ registre providers.py), ou documenter la "
        "non-exposition dans FOD_NOT_EXPOSED avec sa raison")


def test_fod_allowlists_are_not_stale():
    """Une entrée d'allowlist doit rester vraie : le module existe encore dans FOD,
    et (pour FOD_NOT_EXPOSED) il n'est toujours PAS câblé — sinon la décision est
    périmée et l'entrée doit sauter (le test redevient le seul état des lieux)."""
    modules = _fod_data_modules()
    refs = _fod_references()

    unknown = (set(FOD_NOT_EXPOSED) | FOD_PURE_UTILS) - set(modules)
    assert not unknown, (
        f"entrées d'allowlist sans module data FOD correspondant : {sorted(unknown)} "
        "— module renommé/supprimé côté FOD (ou plus de *Client) : retirer l'entrée")

    now_wired = {m for m in FOD_NOT_EXPOSED if _is_wired(m, modules[m], refs)}
    assert not now_wired, (
        f"modules déclarés non exposés mais désormais câblés : {sorted(now_wired)} "
        "— retirer l'entrée de FOD_NOT_EXPOSED (la décision est caduque)")
