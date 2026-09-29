import io
import zipfile
from pathlib import Path

from conftest import image_bytes, upload_photo

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def new_batch(env):
    return env.api.post("/api/photo-batches").json()["id"]


def send(env, bid, files):
    r = env.api.post(f"/api/photo-batches/{bid}/upload",
                     files=[("files", (name, data, "application/octet-stream")) for name, data in files],
                     data={"relpaths": [name for name, _ in files]})
    assert r.status_code == 200, r.text
    return r.json()


def items(env, bid):
    return {i["original_filename"]: i for i in env.api.get(f"/api/photo-batches/{bid}").json()["items"]}


def test_matching_rules_and_commit(env):
    a = env.employee(rhc_id="RHC1001", full_name="Rohan Mehta")
    b = env.employee(rhc_id="RHC1002", full_name="Diya Soni")
    c = env.employee(rhc_id="RHC1003", full_name="Kabir Shah")
    bid = new_batch(env)
    rep = send(env, bid, [("RHC1001_anything.jpg", image_bytes((1, 2, 3))),
                          ("Diya Soni.jpg", image_bytes((4, 5, 6))),
                          ("IMG_0001.jpg", image_bytes((7, 8, 9))),
                          ("notes.txt", b"hello"),
                          ("fake.jpg", b"not an image"),
                          ("map.csv", b"filename,RHC ID\nIMG_0001.jpg,RHC1003\n")])
    assert rep["added"] == 3 and rep["invalid"] == 2 and rep["mappings"] == 1
    it = items(env, bid)
    assert it["RHC1001_anything.jpg"]["status"] == "ready" and it["RHC1001_anything.jpg"]["match_method"] == "rhc_filename"
    assert it["Diya Soni.jpg"]["status"] == "pending", "a name match needs confirmation"
    assert it["Diya Soni.jpg"]["suggested_employee_id"] == b["id"]
    assert it["IMG_0001.jpg"]["status"] == "ready" and it["IMG_0001.jpg"]["match_method"] == "mapping"
    env.api.post(f"/api/photo-batches/items/{it['Diya Soni.jpg']['id']}", {"action": "confirm"})
    r = env.api.post(f"/api/photo-batches/{bid}/commit").json()
    assert r["saved"] == 3
    prof = env.api.get(f"/api/employees/{a['id']}").json()
    assert prof["photos"][0]["original_filename"] == "RHC1001_anything.jpg"
    with env.conn() as conn:
        row = conn.execute("SELECT id, stored_path FROM photos WHERE employee_id=?", (a["id"],)).fetchone()
    assert row["stored_path"] == f"{a['id']}/{a['id']}_p{row['id']}.jpg", "stored under permanent IDs"
    assert (env.ctx.paths.images / row["stored_path"]).exists()
    assert env.api.get(f"/api/photos/{row['id']}/thumb").status_code == 200
    _ = c


def test_duplicates_and_same_as_current(env):
    e = env.employee(rhc_id="RHC7", full_name="Dup Check")
    data = image_bytes((50, 60, 70))
    bid = new_batch(env)
    send(env, bid, [("RHC7.jpg", data), ("copy/RHC7.jpg", data)])
    its = env.api.get(f"/api/photo-batches/{bid}").json()["items"]
    assert any("duplicate_in_batch" in i["flags"] for i in its)
    r = env.api.post(f"/api/photo-batches/{bid}/commit")
    assert r.status_code == 422 and "clashes" in r.json()["details"], "two photos for one employee blocked"
    env.api.post(f"/api/photo-batches/items/{its[1]['id']}", {"action": "skip"})
    assert env.api.post(f"/api/photo-batches/{bid}/commit").json()["saved"] == 1
    bid2 = new_batch(env)
    send(env, bid2, [("RHC7.jpg", data)])
    it = env.api.get(f"/api/photo-batches/{bid2}").json()["items"][0]
    assert "same_as_current" in it["flags"]
    assert env.api.post(f"/api/photo-batches/{bid2}/commit").json() == {"saved": 0, "unchanged": 1, "pending": 0}
    _ = e


def test_zip_safety(env):
    env.employee(rhc_id="Z1", full_name="Zip Person")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("photos/Z1.jpg", image_bytes())
        zf.writestr("../../evil.jpg", image_bytes())
        zf.writestr("/abs/evil2.jpg", image_bytes())
        zf.writestr("inner.zip", b"PK..")
        zf.writestr("__MACOSX/._Z1.jpg", b"junk")
        zf.writestr("bomb.png", b"\0" * 5_000_000)
    bid = new_batch(env)
    rep = send(env, bid, [("upload.zip", buf.getvalue())])
    its = env.api.get(f"/api/photo-batches/{bid}").json()["items"]
    errors = {i["source_name"]: i["error"] for i in its if i["status"] == "invalid"}
    assert rep["added"] == 1
    assert any("Unsafe path" in v for v in errors.values())
    assert any("ratio" in v for v in errors.values())
    assert any("nested" in v.lower() or "Not an image" in v for v in errors.values())
    assert not (env.ctx.paths.root.parent / "evil.jpg").exists()
    assert not any("MACOSX" in k for k in errors)


def test_sequence_mapping_requires_explicit_confirmation(env):
    a = env.employee(rhc_id="Q1", full_name="Seq One")
    b = env.employee(rhc_id="Q2", full_name="Seq Two")
    bid = new_batch(env)
    send(env, bid, [("scan_a.jpg", image_bytes((10, 0, 0))), ("scan_b.jpg", image_bytes((0, 10, 0)))])
    its = items(env, bid)
    order = [its["scan_b.jpg"]["id"], its["scan_a.jpg"]["id"]]
    p = env.api.post(f"/api/photo-batches/{bid}/sequence/preview", {"item_ids": order, "rhc_ids": ["Q1"]}).json()
    assert p["errors"], "lengths must match"
    p = env.api.post(f"/api/photo-batches/{bid}/sequence/preview", {"item_ids": order, "rhc_ids": ["q1", "Q2"]}).json()
    assert not p["errors"] and p["pairs"][0]["name"] == "Seq One"
    r = env.api.post(f"/api/photo-batches/{bid}/sequence/apply", {"item_ids": order, "rhc_ids": ["Q1", "Q2"]})
    assert r.status_code == 422, "must be confirmed"
    r = env.api.post(f"/api/photo-batches/{bid}/sequence/apply", {"item_ids": order, "rhc_ids": ["Q1", "Q2"],
                                                                  "confirmed": True})
    assert r.json()["assigned"] == 2
    its = items(env, bid)
    assert its["scan_b.jpg"]["assigned_employee_id"] == a["id"] and its["scan_a.jpg"]["assigned_employee_id"] == b["id"]


def test_pdf_extraction_is_reviewed_and_crop_works(env):
    for rhc, name in (("RHC1013", "Harsh Joshi"), ("RHC1014", "Kiara Mehta"), ("00715", "Vihaan Chauhan"),
                      ("RHC1016", "Yash Parikh")):
        env.employee(rhc_id=rhc, full_name=name)
    bid = new_batch(env)
    rep = send(env, bid, [("sheet.pdf", (SAMPLES / "sample_headshots.pdf").read_bytes())])
    assert rep["pdf_pages"] == 3 and rep["added"] == 5
    detail = env.api.get(f"/api/photo-batches/{bid}").json()
    pdf_items = [i for i in detail["items"] if "from_pdf" in i["flags"]]
    assert all(i["status"] == "pending" for i in pdf_items), "PDF photos are never assigned silently"
    suggested = {i["suggested_rhc"] for i in pdf_items if i["suggested_rhc"]}
    assert suggested == {"RHC1013", "RHC1014", "00715", "RHC1016"}
    scanned = [p for p in detail["pdf_pages"] if "scanned" in p["flags"]]
    assert len(scanned) == 1 and "no_text" in scanned[0]["flags"]
    page = env.api.get(f"/api/photo-batches/pages/{scanned[0]['id']}").json()
    assert env.api.get(f"/api/photo-batches/pages/{page['id']}/image").status_code == 200
    r = env.api.post(f"/api/photo-batches/pages/{page['id']}/crop", {"rect": [100, 150, 300, 400]})
    assert r.status_code == 200, r.text
    crop = [i for i in env.api.get(f"/api/photo-batches/{bid}").json()["items"] if i["source_kind"] == "pdf_crop"][0]
    assert crop["status"] == "pending" and crop["width"] > 300, "crop taken from a high-resolution render"
    assert env.api.post(f"/api/photo-batches/pages/{page['id']}/crop", {"rect": [0, 0, 3, 3]}).status_code == 422


def test_replace_photo_keeps_versions_and_missing_report(env):
    e = env.employee(rhc_id="V1", full_name="Version Person")
    env.employee(rhc_id="V2", full_name="No Photo")
    missing = {m["rhc_id"] for m in env.api.get("/api/photos/missing").json()}
    assert missing == {"V1", "V2"}
    p1 = upload_photo(env, e["id"], (1, 1, 1))
    p2 = upload_photo(env, e["id"], (200, 1, 1))
    prof = env.api.get(f"/api/employees/{e['id']}").json()
    assert [p["id"] for p in prof["photos"]] == [p2, p1] and prof["employee"]["photo"]["id"] == p2
    assert env.api.get(f"/api/photos/{p1}/file").status_code == 200, "old versions stay available"
    r = env.api.post(f"/api/employees/{e['id']}/photo", files={"file": ("x.jpg", b"garbage", "image/jpeg")})
    assert r.status_code == 422
    env.api.post(f"/api/photos/{p1}/make-current")
    assert env.api.get(f"/api/employees/{e['id']}").json()["employee"]["photo"]["id"] == p1
    assert {m["rhc_id"] for m in env.api.get("/api/photos/missing").json()} == {"V2"}
