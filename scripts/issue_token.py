"""Émet un API token pour un user. Bootstrap : à utiliser via SSH côté serveur.

Le token brut est imprimé une seule fois sur stdout. À copier dans le SOPS
du user comme `OTO_API_KEY`.

Usage :
    ssh tuls.me ".venv/bin/python -m scripts.issue_token <sub> [label]"
"""
from __future__ import annotations

import sys

from oto_mcp import db


def main():
    if len(sys.argv) < 2:
        print("usage: issue_token <sub> [label]", file=sys.stderr)
        sys.exit(2)
    sub = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else "cli"
    db.init_db()
    token = db.create_api_token(sub, label=label)
    print(token)


if __name__ == "__main__":
    main()
