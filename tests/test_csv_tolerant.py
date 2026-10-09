"""Tolerant CSV import: separator, encoding and headers read the way real files are
written (Excel, Google Sheets, CRM exports), without changing a clean comma+key file."""
from __future__ import annotations

import pytest

from oto_mcp import csv_tolerant as ct
from oto_mcp import upload_tokens as ut

SCHEMA = {"fields": [{"key": "company_name", "type": "text", "label": "Raison sociale"},
                     {"key": "email", "type": "text"},
                     {"key": "siren", "type": "text"}]}


@pytest.mark.parametrize("sep", [",", ";", "\t", "|"])
def test_separator_is_detected(sep):
    text = sep.join(["a", "b", "c"]) + "\n" + sep.join(["1", "2", "3"]) + "\n"
    assert ct.detect_separator(text) == sep


def test_comma_wins_a_tie_and_a_single_column_reads_as_comma():
    assert ct.detect_separator("a,b;c\n1,2;3\n") == ","
    assert ct.detect_separator("only\n1\n2\n") == ","


def test_quoted_separators_and_newlines_do_not_break_detection():
    text = 'name;notes\n"ACME";"a, b; c\nsecond line"\n"B";"x"\n'
    assert ct.detect_separator(text) == ";"
    _, rows = ct.read_rows(text, ";")
    assert rows[0]["notes"] == "a, b; c\nsecond line"


def test_utf8_bom_is_stripped():
    d = ct.decode("﻿email\nx@y\n".encode("utf-8"))
    assert d.text.startswith("email") and d.encoding == "utf-8"


def test_cp1252_is_accepted_and_said():
    d = ct.decode("société\nÉtoile\n".encode("cp1252"))
    assert d.text == "société\nÉtoile\n" and d.encoding == "cp1252"


def test_excel_unicode_text_is_utf16_with_tabs_not_binary():
    data = "﻿email\tsiren\nx@y\t1\n".encode("utf-16-le")
    parsed = ut.parse_import(b"\xff\xfe" + data[2:], "csv", SCHEMA)
    assert parsed["info"]["encoding"] == "utf-16"
    assert parsed["info"]["separator"] == "tab"
    assert parsed["rows"] == [{"email": "x@y", "siren": "1"}]


def test_binary_is_refused_by_name():
    with pytest.raises(ut.UploadError) as e:
        ut.parse_import(b"PK\x03\x04\x00\x00xlsx", "csv", SCHEMA)
    assert e.value.code == "binary_content"


def test_headers_match_keys_ignoring_case_and_accents_then_labels():
    parsed = ut.parse_import("EMAIL,Raison Sociale,SIREN\nx@y,ACME,1\n".encode(),
                             "csv", SCHEMA)
    assert parsed["rows"] == [{"email": "x@y", "company_name": "ACME", "siren": "1"}]
    assert parsed["info"]["matched_by_label"] == {
        "EMAIL": "email", "Raison Sociale": "company_name", "SIREN": "siren"}


def test_two_headers_on_one_column_are_refused_before_any_write():
    with pytest.raises(ut.UploadError) as e:
        ut.parse_import(b"email,Email\na,b\n", "csv", SCHEMA)
    assert e.value.code == "entete_en_collision"


def test_unknown_headers_are_reported_on_the_signed_put_path():
    parsed = ut.parse_import(b"email,Notes\nx@y,hi\n", "csv", SCHEMA)
    assert parsed["info"]["unmatched_headers"] == ["Notes"]
    assert parsed["rows"] == [{"email": "x@y", "Notes": "hi"}]
    assert parsed["new_columns"] == []


def test_declare_columns_slugs_new_headers_and_keeps_the_label():
    parsed = ut.parse_import("email,Date de création\nx@y,2026\n".encode(), "csv",
                             SCHEMA, declare_columns=True)
    assert parsed["new_columns"] == [
        {"key": "date_de_creation", "label": "Date de création", "type": "text"}]
    assert parsed["rows"] == [{"email": "x@y", "date_de_creation": "2026"}]
    assert "unmatched_headers" not in parsed["info"]


def test_declare_columns_refuses_two_headers_with_the_same_slug():
    with pytest.raises(ut.UploadError) as e:
        ut.parse_import(b"Notes,notes!\na,b\n", "csv", None, declare_columns=True)
    assert e.value.code == "entete_en_collision"


def test_an_explicit_separator_wins():
    parsed = ut.parse_import(b"email;siren\nx@y;1\n", "csv", SCHEMA, separator=",")
    assert list(parsed["rows"][0]) == ["email;siren"]


def test_a_clean_comma_key_file_reads_exactly_as_before():
    rows, traduits = ut._parse_rows(b"email,siren\na@x,1\nb@y,2\n", "csv", SCHEMA)
    assert rows == [{"email": "a@x", "siren": "1"}, {"email": "b@y", "siren": "2"}]
    assert traduits == {}


def test_cells_past_the_header_are_dropped_not_written_under_none():
    parsed = ut.parse_import(b"email\nx@y,extra\n", "csv", SCHEMA)
    assert parsed["rows"] == [{"email": "x@y"}]


SHEET = ("Buy,,acme.company,acme-demo.com,acme-tool.com,Site,Site.comment\n"
         "yes,junk,ACME,1,2,a.fr,checked\n").encode()


def test_every_final_key_is_a_declared_slug_on_oto_import():
    """A real public sheet: dotted and hyphenated headers, one empty header."""
    parsed = ut.parse_import(SHEET, "csv", None, declare_columns=True)
    new = {c["key"]: c["label"] for c in parsed["new_columns"]}
    assert new == {"buy": "Buy", "acme_company": "acme.company",
                   "acme_demo_com": "acme-demo.com", "acme_tool_com": "acme-tool.com",
                   "site": "Site"}
    row = parsed["rows"][0]
    assert set(row) == set(new) | {"site.comment"}, "an annotation rides on its column"
    assert parsed["info"]["dropped_empty_headers"] == [2]
    assert "junk" not in row.values()


def test_the_signed_put_keeps_its_dotted_translation():
    parsed = ut.parse_import(SHEET, "csv", None)
    assert parsed["traduits"]["acme.company"] == "acme_company"
    assert parsed["new_columns"] == []
    assert parsed["info"]["dropped_empty_headers"] == [2]


# --- fichier illisible : un refus nommé, jamais un 500 ---------------------------

def _enorme_cellule() -> str:
    return "email,note\nx@y," + "z" * (200 * 1024) + "\n"


def test_cell_over_the_field_limit_is_a_named_refusal():
    with pytest.raises(ct.CsvError) as ei:
        ct.read_rows(_enorme_cellule(), ",")
    assert ei.value.code == "bad_csv"


def test_detect_separator_on_a_huge_cell_is_a_named_refusal():
    with pytest.raises(ct.CsvError) as ei:
        ct.detect_separator(_enorme_cellule())
    assert ei.value.code == "bad_csv"


def test_parse_import_turns_bad_csv_into_a_400():
    with pytest.raises(ut.UploadError) as ei:
        ut.parse_import(_enorme_cellule().encode(), "csv", SCHEMA)
    assert (ei.value.status, ei.value.code) == (400, "bad_csv")


def test_the_cell_limit_holds_even_when_the_global_csv_limit_was_raised():
    """`csv.field_size_limit` est GLOBAL au processus : un code qui la relève (un script
    d'archive, le 09/10/2026, #1111) ne doit pas désarmer ce refus."""
    import csv
    avant = csv.field_size_limit(10_000_000)
    try:
        for lire in (lambda t: ct.read_rows(t, ","), ct.detect_separator):
            with pytest.raises(ct.CsvError) as ei:
                lire(_enorme_cellule())
            assert ei.value.code == "bad_csv"
        with pytest.raises(ut.UploadError) as ei:
            ut.parse_import(_enorme_cellule().encode(), "csv", SCHEMA)
        assert (ei.value.status, ei.value.code) == (400, "bad_csv")
    finally:
        csv.field_size_limit(avant)


def test_a_cell_at_the_limit_is_read():
    texte = "email,note\nx@y," + "z" * ct.TAILLE_MAX_CELLULE + "\n"
    _, rows = ct.read_rows(texte, ",")
    assert len(rows[0]["note"]) == ct.TAILLE_MAX_CELLULE


def test_duplicate_headers_refused_by_name():
    """DictReader garde la dernière : une colonne disparaîtrait sans un mot."""
    with pytest.raises(ct.CsvError) as ei:
        ct.read_rows("email,email\na@b,c@d\n", ",")
    assert ei.value.code == "entete_en_double" and "`email`" in str(ei.value)


def test_duplicate_headers_through_parse_import():
    with pytest.raises(ut.UploadError) as ei:
        ut.parse_import(b"email;siren;email\nx;1;y\n", "csv", SCHEMA)
    assert (ei.value.status, ei.value.code) == (400, "entete_en_double")


def test_un_export_neutralise_se_relit_tel_qu_il_est_parti():
    """`csv_formules` préfixe d'une apostrophe un texte qui s'ouvrirait en formule ;
    la lecture la retire. Une apostrophe devant autre chose est une donnée."""
    from oto_mcp.csv_tolerant import read_rows

    headers, rows = read_rows("'=nom,tel,note\n'=SUM(1),'+33123456789,'bonjour\n", ",")

    assert headers == ["=nom", "tel", "note"]
    assert rows == [{"=nom": "=SUM(1)", "tel": "+33123456789", "note": "'bonjour"}]
