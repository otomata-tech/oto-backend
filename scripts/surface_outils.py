"""The frozen record of the served tool surface: `tests/surface_outils.txt` (decided 05/11/2026).

The generic reader of descriptions (`tools/decrits.py`) changes nothing it does not serve: no existing tool changes
its name, its ops or its input schema. This record says, per tool the server mounts (the reader's aside), its name,
its ops and the fingerprint of its input schema; `tests/test_surface_outils_figee.py` compares it to the server built.

The fingerprint is a SHA-256 of the schema in canonical JSON, every `enum` SORTED: the order of an enum built from a
set (`nextmotion_statistics`) changes from one build to another, and says nothing of the surface.

    python scripts/surface_outils.py            # rewrite the record (a surface change, reviewed as such)
    python scripts/surface_outils.py --check    # exit 1 when the server differs from the record
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys

RELEVE = pathlib.Path(__file__).resolve().parents[1] / "tests" / "surface_outils.txt"
ENTETE = ("# The served tool surface (scripts/surface_outils.py): name, ops, input schema fingerprint.\n"
          "# Tools of the generic reader of descriptions (tools/decrits.py) are not recorded.\n")


def _canonique(valeur):
    if isinstance(valeur, dict):
        return {k: (sorted(v, key=json.dumps) if k == "enum" and isinstance(v, list) else _canonique(v))
                for k, v in valeur.items()}
    if isinstance(valeur, list):
        return [_canonique(v) for v in valeur]
    return valeur


def empreinte(schema: dict) -> str:
    texte = json.dumps(_canonique(schema), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()[:16]


def _ops(parametres: dict) -> list[str]:
    op = (parametres.get("properties") or {}).get("op") or {}
    if "enum" in op:
        return sorted(op["enum"])
    if "const" in op:
        return [op["const"]]
    return sorted(v for b in op.get("anyOf", ()) for v in b.get("enum", ()))


def lignes(outils) -> list[str]:
    """One line per tool, sorted by name: `name<TAB>ops<TAB>fingerprint`, the reader's tools aside."""
    from oto_mcp.tools.decrits import OutilDecrit
    return sorted(f"{t.name}\t{' '.join(_ops(t.parameters)) or '-'}\t{empreinte(t.parameters)}"
                  for t in outils if not isinstance(t, OutilDecrit))


def releve(mcp) -> str:
    return ENTETE + "\n".join(lignes(asyncio.run(mcp.list_tools(run_middleware=False)))) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    from oto_mcp.server import _build_mcp
    texte = releve(_build_mcp("noauth"))
    if args.check:
        if RELEVE.read_text(encoding="utf-8") != texte:
            print(f"{RELEVE} differs from the served surface", file=sys.stderr)
            return 1
        return 0
    RELEVE.write_text(texte, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
