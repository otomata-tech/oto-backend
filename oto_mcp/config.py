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
