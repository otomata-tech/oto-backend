"""Env-var helper. Keep secrets out of the repo."""
import os


def require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing env var '{name}'. Set it in the process environment "
            f"(systemd EnvironmentFile in prod, .env in dev)."
        )
    return val


def project_domain() -> str:
    """Domaine racine des endpoints de PROJET publiés — `<slug>.mcp.<D>` (annuaire, mode
    anonymous) et `<slug>.share.<D>` (partage navigable, mode secret). **PROD = `oto.cx`,
    PREPROD = `oto.ninja`** (cutover ADR 0040) : sans ça le routing par Host et les URLs
    dérivées restaient figés sur la prod (`.oto.cx`), rendant les endpoints de projet
    injoignables en preprod. Env `OTO_PROJECT_DOMAIN` (défaut `oto.cx`)."""
    return os.environ.get("OTO_PROJECT_DOMAIN", "oto.cx").strip().lower().lstrip(".")


def mcp_audience_alts() -> frozenset[str]:
    """Audiences MCP canoniques SECONDAIRES (coexistence multi-domaine, ex.
    `https://mcp.oto.cx/mcp` en plus de `MCP_AUDIENCE`=`https://mcp.oto.ninja/mcp`).

    Env `MCP_AUDIENCE_ALT` = liste séparée par des virgules (resource indicators
    complets, sans slash final). Vide/absent = frozenset vide → **no-op** (le
    comportement mono-audience de mcp.oto.ninja est byte-à-byte inchangé)."""
    raw = os.environ.get("MCP_AUDIENCE_ALT", "")
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def mcp_audience_alt_hosts() -> frozenset[str]:
    """Les HOSTS des audiences alt — pour le PRM Host-aware (un client qui tape
    `mcp.oto.cx` doit se voir annoncer `resource=https://mcp.oto.cx/mcp`)."""
    from urllib.parse import urlparse
    hosts = (urlparse(a).hostname for a in mcp_audience_alts())
    return frozenset(h for h in hosts if h)
