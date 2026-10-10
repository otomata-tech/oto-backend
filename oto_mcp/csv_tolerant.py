"""Tolerant reading of a CSV file for a table import.

Real files come from Excel, Google Sheets, Clay or a CRM export: `;` or tab
separators, a BOM, cp1252 or UTF-16, and headers written as column LABELS
("Company name") rather than keys (`company_name`). This module turns them into
rows keyed by column keys, and says how it did it.

Deterministic on purpose: the decision reads the file's first rows and the schema,
never the rows already in the table, so a monthly re-import targets the same columns.
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from itertools import islice
from typing import Optional

from .csv_formules import relire

SEPARATORS = (",", ";", "\t", "|")
_SAMPLE_ROWS = 20
_BINARY_PROBE = 8192

#: La plus grosse cellule acceptée, en caractères : le défaut du module `csv` (128 ko).
#: ⚠️ Appliquée ICI, cellule par cellule, et non confiée à `csv.field_size_limit()` : cette
#: limite est GLOBALE au processus, et n'importe quel code qui la relève (un script
#: d'archive relu par un banc, le 09/10/2026, #1111) désarmait ce refus sans un mot — un
#: fichier à cellule géante passait. Le refus ne dépend plus de ce qu'un autre a réglé.
TAILLE_MAX_CELLULE = 131_072


class CsvError(ValueError):
    """Unreadable file. `code` is the refusal token served to the caller."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Decoded:
    text: str
    encoding: str


@dataclass
class HeaderMap:
    """`{header: column key}` for every header, and how each was matched."""
    target: dict = field(default_factory=dict)
    by_label: dict = field(default_factory=dict)      # header → key, matched by label or case
    unmatched: list = field(default_factory=list)     # headers with no declared column


def key_for(name: str) -> str:
    """Same rule as the front's `keyFor`: accents folded, lowercase, `_` between words."""
    folded = unicodedata.normalize("NFD", str(name))
    folded = "".join(c for c in folded if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", "_", folded).strip("_")


def decode(data: bytes, *, allow_fallback: bool = True) -> Decoded:
    """UTF-16 (by BOM) first, since its NULs would read as binary; then UTF-8 (BOM
    stripped); then cp1252, the Excel default on Windows. Never latin-1: it never fails,
    so it would hide garbage."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return Decoded(data.decode("utf-16"), "utf-16")
        except UnicodeDecodeError:
            raise CsvError("not_utf8", "The file has a UTF-16 mark but does not decode.")
    if b"\x00" in data[:_BINARY_PROBE]:
        raise CsvError("binary_content",
                       "The file is binary, not CSV or NDJSON (an .xlsx? export it as CSV).")
    try:
        return Decoded(data.decode("utf-8-sig"), "utf-8")
    except UnicodeDecodeError:
        if not allow_fallback:
            raise CsvError("not_utf8", "The content must be UTF-8.")
    try:
        return Decoded(data.decode("cp1252"), "cp1252")
    except UnicodeDecodeError:
        raise CsvError("not_utf8", "The file is neither UTF-8, UTF-16 nor cp1252.")


def _cellule_trop_grosse(taille: int) -> CsvError:
    return CsvError("bad_csv", f"The file is not a readable CSV: a cell of {taille} "
                               f"characters is larger than the field limit "
                               f"({TAILLE_MAX_CELLULE}). Nothing was imported.")


def _bornee(cellules) -> None:
    """Refuse une cellule au-delà de TAILLE_MAX_CELLULE, quelle que soit la limite
    globale de `csv` (cf. sa déclaration)."""
    for c in cellules:
        if isinstance(c, list):
            _bornee(c)
        elif isinstance(c, str) and len(c) > TAILLE_MAX_CELLULE:
            raise _cellule_trop_grosse(len(c))


def _mal_forme(e: csv.Error) -> CsvError:
    """`csv.Error` (a cell over the field limit, a NUL byte…) is a refusal naming the
    file, never a 500."""
    return CsvError("bad_csv", f"The file is not a readable CSV: {e}. Nothing was "
                               "imported.")


def detect_separator(text: str) -> str:
    """The candidate giving the same field count (> 1) on the header and the next rows.
    Comma wins ties; a one-column file reads as comma."""
    best, best_width = ",", 1
    for sep in SEPARATORS:
        try:
            rows = [r for r in islice(csv.reader(io.StringIO(text), delimiter=sep),
                                      _SAMPLE_ROWS + 1) if r]
        except csv.Error as e:
            raise _mal_forme(e) from None
        for r in rows:
            _bornee(r)
        if not rows:
            continue
        width = len(rows[0])
        if width > 1 and all(len(r) == width for r in rows) and width > best_width:
            best, best_width = sep, width
    return best


def map_headers(schema: Optional[dict], headers: list) -> HeaderMap:
    """Header → column key: exact key, then the same name ignoring case and accents,
    then a declared LABEL. Dotted headers and annotations are left to the caller
    (`traduire_les_entetes`). Two headers on one column are refused, naming both."""
    from .datastore.declaration import _fields
    fields_ = [f for f in _fields(schema) if f.get("key")]
    keys = {f["key"] for f in fields_}
    index: dict = {}
    for f in fields_:
        index.setdefault(key_for(f["key"]), f["key"])
    for f in fields_:
        if f.get("label"):
            index.setdefault(key_for(f["label"]), f["key"])
    out = HeaderMap()
    taken: dict = {h: h for h in headers if h in keys}
    for h in headers:
        if not h:
            continue
        if h in keys:
            out.target[h] = h
            continue
        if "." in h:
            out.target[h] = h
            continue
        k = index.get(key_for(h))
        if k is None:
            out.target[h] = h
            out.unmatched.append(h)
            continue
        if k in taken:
            raise CsvError("entete_en_collision",
                           f"Headers `{taken[k]}` and `{h}` both map to column `{k}`. "
                           f"Nothing was imported: rename one in the file.")
        taken[k] = h
        out.target[h] = k
        out.by_label[h] = k
    return out


def read_rows(text: str, separator: str) -> tuple[list, list]:
    """`(headers, rows)`; rows are dicts keyed by header (headers trimmed). Cells past
    the header's width are dropped, never written under a `None` column."""
    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=separator)
        _bornee(reader.fieldnames or [])
        headers = [relire(h.strip()) if isinstance(h, str) else h
                   for h in (reader.fieldnames or [])]
        # Two identical headers: DictReader keeps only the last one — a column
        # would vanish without a word. Refused, naming it.
        vus: set = set()
        for h in headers:
            if h and h in vus:
                raise CsvError("entete_en_double",
                               f"Header `{h}` appears twice in the file. Nothing was "
                               f"imported: rename one of the two columns.")
            vus.add(h)
        reader.fieldnames = headers
        # Un export neutralisé contre les formules (`csv_formules`) se relit
        # tel qu'il est parti : `'+33…` redevient `+33…`.
        rows = []
        for r in reader:
            _bornee(r.values())
            rows.append({k: relire(v) if isinstance(v, str) else v
                         for k, v in r.items() if k is not None})
    except csv.Error as e:
        raise _mal_forme(e) from None
    return headers, rows
