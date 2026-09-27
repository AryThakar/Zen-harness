import pandas as pd
import pytest

from app.ingestion.reader import ReadError, normalize_name, read_file, unique_names


def test_title_lines_above_the_header_are_skipped(write_file):
    path = write_file(
        "My Shop Pvt Ltd\n"
        "Sales Register 01/03/2025 to 31/03/2025\n"
        "GSTIN: 27ABCDE1234F1Z5\n"
        "\n"
        "Bill No,Bill Date,Item,Qty,Rate\n"
        "B1,01/03/2025,Pen,2,10\n"
        "B2,02/03/2025,Ink,1,50\n"
    )
    raw = read_file(path)
    assert raw.header_row == 5
    assert list(raw.df.columns) == ["bill_no", "bill_date", "item", "qty", "rate"]
    assert raw.original_names["bill_date"] == "Bill Date"
    assert raw.df.index.tolist() == [6, 7]  # file line numbers


def test_semicolon_files_and_windows_encoding(write_file):
    path = write_file("Datum;Artikel;Menge;Preis\n01.03.2025;Café Crème;2;3,50\n", encoding="cp1252")
    raw = read_file(path)
    assert raw.delimiter == ";"
    assert raw.df.iloc[0]["artikel"] == "Café Crème"


def test_utf8_bom_is_removed(write_file):
    path = write_file("﻿Date,Item\n2025-01-01,Pen\n")
    assert list(read_file(path).df.columns) == ["date", "item"]


def test_blank_rows_and_empty_columns_are_dropped(write_file):
    path = write_file("Date,Item,,Qty\n2025-01-01,Pen,,1\n,,,\n\n2025-01-02,Ink,,2\n")
    raw = read_file(path)
    assert list(raw.df.columns) == ["date", "item", "qty"]
    assert raw.df.index.tolist() == [2, 5]


def test_rows_with_extra_fields_are_set_aside(write_file):
    path = write_file("Date,Item,Qty\n2025-01-01,Pen,1\n2025-01-02,Ink,2,EXTRA,MORE\n2025-01-03,Pad,3\n")
    raw = read_file(path)
    assert raw.df.index.tolist() == [2, 4]
    assert raw.bad_lines == [(3, ["2025-01-02", "Ink", "2", "EXTRA", "MORE"])]


def test_excel_with_title_rows(tmp_path):
    path = tmp_path / "sales.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame([["Monthly sales report"], [""]]).to_excel(writer, header=False, index=False)
        pd.DataFrame({"Invoice Date": pd.to_datetime(["2025-03-01", "2025-03-02"]),
                      "Product": ["Pen", "Ink"], "Qty": [2, 1], "Price": [10.0, 50.0]}
                     ).to_excel(writer, startrow=3, index=False)
    raw = read_file(path)
    assert raw.header_row == 4
    assert list(raw.df.columns) == ["invoice_date", "product", "qty", "price"]
    assert raw.df.index.tolist() == [5, 6]  # Excel row numbers
    assert raw.df.iloc[0]["invoice_date"].startswith("2025-03-01")


def test_empty_file_raises(write_file):
    with pytest.raises(ReadError):
        read_file(write_file("\n\n"))


def test_names_are_normalized_and_unique():
    assert normalize_name("Unit Price (₹)") == "unit_price"
    assert normalize_name("Customer ID") == "customer_id"
    assert unique_names(["Amount", "amount", "", "Amount"]) == ["amount", "amount_2", "column_3", "amount_3"]


def test_utf16_unicode_text_exports_are_read(tmp_path):
    path = tmp_path / "unicode.txt"  # Excel's "Unicode Text": UTF-16 with a byte-order mark, tab-separated
    path.write_bytes("Date\tItem\tQty\tPrice\n2025-01-01\tChai ₹\t1\t10\n".encode("utf-16"))
    raw = read_file(path)
    assert (raw.encoding, raw.delimiter) == ("utf-16", "\t")
    assert raw.df.iloc[0]["item"] == "Chai ₹"


def test_decimal_commas_dont_fool_the_delimiter_guess(write_file):
    path = write_file("Date;Item;Qty;Price\n01/01/2025;1,5;2,5;3,5\n02/01/2025;1,5;2,5;3,5\n")
    raw = read_file(path)
    assert raw.delimiter == ";" and list(raw.df.columns) == ["date", "item", "qty", "price"]


def test_row_numbers_stay_right_after_a_value_spanning_lines(write_file):
    path = write_file('Date,Item,Qty,Price\n2025-01-01,"Pen\nblue",1,10\n2025-01-02,Ink,x,5\n')
    raw = read_file(path)
    assert raw.df.index.tolist() == [2, 4]  # the second record starts on line 4
    assert raw.df.iloc[0]["item"] == "Pen\nblue"
