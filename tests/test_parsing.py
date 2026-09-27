import pandas as pd
import pytest

from app.ingestion.parsing import (
    clean_ids, detect_date_order, infer_type, parse_booleans, parse_dates, parse_numbers,
    unify_case,
)


@pytest.mark.parametrize(
    "text, value",
    [
        ("₹1,23,456.50", 123456.50),
        ("Rs. 500", 500.0),
        ("INR 1,200", 1200.0),
        ("$ 1 234.5", 1234.5),
        ("(500)", -500.0),
        ("-12", -12.0),
        ("1.234,50", 1234.50),
        ("3,50", 3.5),
        ("1,234", 1234.0),
        ("0.15", 0.15),
        (".5", 0.5),
    ],
)
def test_parse_numbers_reads_messy_amounts(text, value):
    values, bad, _ = parse_numbers(pd.Series([text]))
    assert values[0] == pytest.approx(value)
    assert not bad[0]


@pytest.mark.parametrize("text", ["abc", "SKU-884", "12 pcs", "1-2"])
def test_parse_numbers_flags_garbage_instead_of_guessing(text):
    values, bad, _ = parse_numbers(pd.Series([text]))
    assert pd.isna(values[0])
    assert bad[0]


@pytest.mark.parametrize("text", ["", "N/A", "-", "null", None])
def test_parse_numbers_treats_null_tokens_as_missing(text):
    values, bad, _ = parse_numbers(pd.Series([text], dtype=object))
    assert pd.isna(values[0])
    assert not bad[0]


def test_parse_numbers_marks_percentages():
    values, _, pct = parse_numbers(pd.Series(["10%", "10"]))
    assert values.tolist() == [10.0, 10.0]
    assert pct.tolist() == [True, False]


def test_iso_dates_are_never_read_day_first():
    # pandas reads '2025-03-04' as 4 March only if ISO is handled separately.
    out, bad = parse_dates(pd.Series(["2025-03-04", "13/03/2025"]), order="dayfirst")
    assert out[0] == pd.Timestamp("2025-03-04")
    assert out[1] == pd.Timestamp("2025-03-13")
    assert not bad.any()


def test_date_order_is_detected_from_the_data():
    assert detect_date_order(pd.Series(["01/03/2025", "25/03/2025"])) == "dayfirst"
    assert detect_date_order(pd.Series(["12/1/2010 8:26", "12/13/2010 9:00"])) == "monthfirst"
    assert detect_date_order(pd.Series(["01/02/2025", "03/04/2025"])) is None

    out, _ = parse_dates(pd.Series(["12/1/2010 8:26", "12/13/2010 9:00"]))
    assert out.tolist() == [pd.Timestamp("2010-12-01 08:26"), pd.Timestamp("2010-12-13 09:00")]


def test_ambiguous_dates_use_the_default_order():
    out, _ = parse_dates(pd.Series(["03/04/2025"]), default_order="monthfirst")
    assert out[0] == pd.Timestamp("2025-03-04")
    out, _ = parse_dates(pd.Series(["03/04/2025"]), default_order="dayfirst")
    assert out[0] == pd.Timestamp("2025-04-03")


def test_timezone_offsets_keep_local_time_and_excel_serials_work():
    out, bad = parse_dates(pd.Series(["2025-03-02 10:15:23 +0530", "45000", "2025-3-4"]))
    assert out.tolist() == [
        pd.Timestamp("2025-03-02 10:15:23"), pd.Timestamp("2023-03-15"), pd.Timestamp("2025-03-04"),
    ]
    assert not bad.any()


def test_bad_dates_are_flagged():
    out, bad = parse_dates(pd.Series(["35/03/2025", "soon", ""]))
    assert bad.tolist() == [True, True, False]
    assert out.isna().all()


def test_parse_booleans():
    values, bad = parse_booleans(pd.Series(["Yes", "n", "TRUE", "", "maybe"]))
    assert values.tolist()[:3] == [True, False, True]
    assert bad.tolist() == [False, False, False, False, True]


@pytest.mark.parametrize(
    "name, values, expected",
    [
        ("size", ["1", "2.5", ""], "DOUBLE"),
        ("ship_date", ["11/08/2016", "11/12/2016"], "TIMESTAMP"),
        ("gift_wrap", ["Yes", "no"], "BOOLEAN"),
        ("payment_mode", ["UPI", "Cash"], "VARCHAR"),
        ("row_id", ["1", "2"], "VARCHAR"),  # IDs stay text
        ("postal_code", ["01234", "10024"], "VARCHAR"),
        ("sku", ["SKU-1", "SKU-2"], "VARCHAR"),
        ("empty", ["", ""], "VARCHAR"),
    ],
)
def test_infer_type(name, values, expected):
    assert infer_type(name, pd.Series(values)) == expected


def test_unify_case_uses_the_most_common_spelling():
    s = pd.Series(["wireless mouse", "Wireless  Mouse", "Wireless Mouse", ""])
    assert unify_case(s).tolist() == ["Wireless Mouse", "Wireless Mouse", "Wireless Mouse", ""]


def test_clean_ids_undoes_float_formatting():
    assert clean_ids(pd.Series(["17850.0", " C-1 ", "", "nan"])).tolist() == ["17850", "C-1", None, None]
