import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.db.connection import Database
from app.main import create_app

from .conftest import SAMPLES


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Database(), tmp_path / "staging")) as c:
        yield c


def preview(client, path, name=None, **data):
    with open(path, "rb") as f:
        return client.post("/uploads/preview", files={"file": (name or path.name, f)}, data=data)


def upload(client, path, **body):
    p = preview(client, path).json()
    return client.post("/uploads", json={"file_id": p["file_id"], **body})


def test_health(client):
    assert client.get("/health").json() == {"status": "ok", "sales_rows": 0, "uploads": 0}


def test_preview_suggests_a_mapping(client):
    r = preview(client, SAMPLES / "shopify_orders.csv")
    assert r.status_code == 200
    p = r.json()
    assert len(p["file_id"]) == 32
    assert p["profile"] == "Shopify orders export"
    assert p["mapping"]["fields"]["order_id"] == "name"
    assert p["missing_required"] == []
    assert {f["name"] for f in p["fields"]} >= {"order_date", "product", "amount"}
    email = next(c for c in p["columns"] if c["name"] == "email")
    assert email["original"] == "Email" and email["maps_to"] == "customer_id"


def test_confirm_with_the_suggested_mapping(client):
    r = upload(client, SAMPLES / "retail_pos_messy.csv")
    assert r.status_code == 201
    report = r.json()
    assert (report["rows_loaded"], report["rows_rejected"]) == (16, 5)
    assert report["summary"].startswith("16 loaded")
    assert client.get("/health").json()["sales_rows"] == 16


def test_confirm_with_an_edited_mapping_and_profile(client):
    p = preview(client, SAMPLES / "retail_pos_messy.csv").json()
    mapping = p["mapping"]
    mapping["custom"] = {"payment_mode": "paid_by"}
    mapping["descriptions"] = {"paid_by": "Payment method"}
    r = client.post("/uploads", json={"file_id": p["file_id"], "mapping": mapping, "save_profile_as": "Shop POS"})
    assert r.status_code == 201
    assert r.json()["custom_columns"] == {"paid_by": "VARCHAR"}
    assert "Shop POS" in [x["name"] for x in client.get("/profiles").json()]
    schema = {c["column_name"]: c for c in client.get("/schema").json()}
    assert schema["paid_by"]["description"] == "Payment method"
    # the next export from the same system is recognised
    assert preview(client, SAMPLES / "retail_pos_messy.csv").json()["profile"] == "Shop POS"


def test_staged_file_is_single_use(client):
    p = preview(client, SAMPLES / "agency_invoices.csv").json()
    assert client.post("/uploads", json={"file_id": p["file_id"]}).status_code == 201
    assert client.post("/uploads", json={"file_id": p["file_id"]}).status_code == 404


def test_upload_errors(client, tmp_path):
    r = client.post("/uploads/preview", files={"file": ("report.pdf", b"%PDF-1.4")})
    assert r.status_code == 415

    r = client.post("/uploads/preview", files={"file": ("empty.csv", b"\n\n")})
    assert r.status_code == 422 and "empty" in r.json()["detail"]

    assert client.post("/uploads", json={"file_id": "not-an-id"}).status_code == 422
    assert client.post("/uploads", json={"file_id": "0" * 32}).status_code == 404

    p = client.post("/uploads/preview", files={"file": ("x.csv", b"Item,Qty,Price\nPen,1,10\n")}).json()
    assert p["missing_required"] == ["order_date"]
    r = client.post("/uploads", json={"file_id": p["file_id"]})
    assert r.status_code == 422 and "order_date" in r.json()["detail"]


def test_too_large_files_are_refused(tmp_path):
    app = create_app(Database(), tmp_path)
    with TestClient(app) as c:
        app.state.staging.max_bytes = 10
        r = c.post("/uploads/preview", files={"file": ("big.csv", b"Date,Item\n2025-01-01,Pen\n")})
    assert r.status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_excel_sheets_are_listed_and_selectable(client, tmp_path):
    path = tmp_path / "book.xlsx"
    with pd.ExcelWriter(path) as w:
        pd.DataFrame({"Note": ["cover page"]}).to_excel(w, sheet_name="Cover", index=False)
        pd.DataFrame({"Date": ["2025-03-01"], "Item": ["Pen"], "Qty": [1], "Price": [10]}).to_excel(
            w, sheet_name="Sales", index=False)
    first = preview(client, path).json()
    assert first["sheets"] == ["Cover", "Sales"]
    assert first["missing_required"]
    chosen = preview(client, path, sheet="Sales").json()
    assert chosen["sheet"] == "Sales" and chosen["missing_required"] == []
    assert client.post("/uploads", json={"file_id": chosen["file_id"]}).json()["rows_loaded"] == 1


def test_upload_history_rejected_rows_and_undo(client):
    upload_id = upload(client, SAMPLES / "retail_pos_messy.csv").json()["upload_id"]
    history = client.get("/uploads").json()
    assert [u["upload_id"] for u in history] == [upload_id]

    detail = client.get(f"/uploads/{upload_id}").json()
    assert detail["rejected_reasons"]["summary row"] == 1
    assert detail["mapping"]["fields"]["order_id"] == "bill_no"

    page = client.get(f"/uploads/{upload_id}/rejected", params={"limit": 2, "offset": 1}).json()
    assert page["total"] == 5
    assert [r["row_number"] for r in page["rows"]] == [20, 21]
    assert page["rows"][0]["raw"]["Qty"] == "0"

    assert client.delete(f"/uploads/{upload_id}").status_code == 204
    assert client.get("/health").json() == {"status": "ok", "sales_rows": 0, "uploads": 0}
    assert client.get(f"/uploads/{upload_id}").status_code == 404
    assert client.delete(f"/uploads/{upload_id}").status_code == 404


def test_demo_data(client):
    r = client.post("/uploads/demo", json={"months": 2, "orders_per_day": 5})
    assert r.status_code == 201
    assert r.json()["rows_rejected"] == 0
    assert r.json()["filename"] == "demo data (synthetic)"
    assert client.post("/uploads/demo", json={"months": 99}).status_code == 422


def test_dashboard_endpoints(client):
    empty = client.get("/dashboard/summary").json()
    assert empty["has_data"] is False
    assert client.get("/dashboard/trend").json()["points"] == []

    upload(client, SAMPLES / "superstore_sample.csv")
    s = client.get("/dashboard/summary", params={"start": "2024-01-01", "end": "2024-12-31"}).json()
    assert s["has_data"] and s["period"]["days"] == 366
    assert s["comparison"] == "none"  # nothing before 2024, so no % changes
    assert s["kpis"]["revenue"]["change_pct"] is None
    assert s["kpis"]["revenue"]["value"] == pytest.approx(1506.192)
    assert s["kpis"]["orders"]["value"] == 7
    assert s["locked"] == {"channel": ["channel comparison"]}

    t = client.get("/dashboard/trend", params={"start": "2024-01-01", "end": "2024-12-31", "grain": "month"}).json()
    assert len(t["points"]) == 12
    assert sum(p["revenue"] for p in t["points"]) == pytest.approx(1506.192)

    b = client.get("/dashboard/breakdown", params={"by": "category", "start": "2024-01-01"}).json()
    assert [i["label"] for i in b["items"]] == ["Furniture", "Technology", "Office Supplies"]
    assert "segment" in b["dimensions"]

    assert client.get("/dashboard/breakdown", params={"by": "revenue"}).status_code == 422
    assert client.get("/dashboard/summary", params={"start": "2025-01-02", "end": "2025-01-01"}).status_code == 422
    assert client.get("/dashboard/trend", params={"grain": "hour"}).status_code == 422


def test_sales_browsing(client):
    upload(client, SAMPLES / "retail_pos_messy.csv")
    page = client.get("/sales", params={"limit": 3}).json()
    assert page["total"] == 16
    assert len(page["rows"]) == 3
    assert "row_hash" not in page["columns"] and "payment_mode" in page["columns"]
    dates = [r["order_date"] for r in page["rows"]]
    assert dates == sorted(dates, reverse=True)

    assert client.get("/sales", params={"q": "mouse"}).json()["total"] == 2
    assert client.get("/sales", params={"category": "Electronics"}).json()["total"] == 7
    assert client.get("/sales", params={"returns": True}).json()["total"] == 1
    assert client.get("/sales", params={"start": "2025-03-20", "end": "2025-03-22"}).json()["total"] == 2
    by_revenue = client.get("/sales", params={"sort": "revenue", "desc": True, "limit": 1}).json()
    assert by_revenue["rows"][0]["revenue"] == 16198.2
    assert client.get("/sales", params={"sort": "row_hash"}).status_code == 422


def test_schema_descriptions_and_profiles(client):
    r = client.patch("/schema/product", json={"description": "Item sold"})
    assert r.status_code == 200 and r.json()["description"] == "Item sold"
    assert client.patch("/schema/nope", json={"description": "x"}).status_code == 404

    names = {p["name"]: p["builtin"] for p in client.get("/profiles").json()}
    assert names["Shopify orders export"] is True
    assert client.delete("/profiles/Shopify orders export").status_code == 409
    assert client.delete("/profiles/missing").status_code == 404

    upload(client, SAMPLES / "agency_invoices.csv", save_profile_as="Agency")
    assert client.delete("/profiles/Agency").status_code == 204


def test_openapi_docs_are_served(client):
    spec = client.get("/openapi.json").json()
    assert "/uploads/preview" in spec["paths"] and "/dashboard/summary" in spec["paths"]


def test_csv_upload_from_memory(client):
    data = io.BytesIO(b"Date;Item;Qty;Price\n01.03.2025;Pen;2;3,50\n")
    p = client.post("/uploads/preview", files={"file": ("eu.csv", data)}).json()
    assert p["delimiter"] == ";"
    report = client.post("/uploads", json={"file_id": p["file_id"]}).json()
    assert report["rows_loaded"] == 1
    assert client.get("/sales").json()["rows"][0]["revenue"] == 7.0


def test_mapping_check_gives_actionable_problems(client):
    r = client.post("/uploads/check", json={"fields": {"product": "item", "unit_price": "price"},
                                            "custom": {"a": "revenue"}})
    body = r.json()
    assert r.status_code == 200
    assert any("sale date" in p for p in body["problems"])
    assert any("quantity" in p for p in body["problems"])
    assert any("Invalid custom column names: revenue" in p for p in body["problems"])
    assert "customer_id" in body["locked_features"]
    ok = client.post("/uploads/check", json={"fields": {"order_date": "d", "product": "i", "amount": "a"}}).json()
    assert ok["problems"] == []


def test_preview_gives_every_column_a_default_name(client):
    p = preview(client, SAMPLES / "retail_pos_messy.csv").json()
    names = {c["original"]: c["default_name"] for c in p["columns"]}
    assert names["Disc %"] == "disc"
    assert names["Payment Mode"] == "payment_mode"


def test_range_and_dimensions(client):
    assert client.get("/dashboard/range").json() == {"has_data": False, "start": None, "end": None}
    upload(client, SAMPLES / "superstore_sample.csv")
    assert client.get("/dashboard/range").json() == {"has_data": True, "start": "2024-01-28", "end": "2024-09-07"}
    dims = client.get("/dashboard/dimensions").json()
    assert "category" in dims and "segment" in dims and "revenue" not in dims
