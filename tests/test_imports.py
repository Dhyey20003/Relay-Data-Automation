import csv
import datetime as dt
import io

from openpyxl import Workbook

HEAD = ["RHC ID", "Full Name", "Designation", "Official Email", "Location", "Date of Joining", "Employment Status"]


def xlsx(rows, headers=HEAD, title_rows=0, formula_cell=None, numeric_ids=False):
    wb = Workbook()
    ws = wb.active
    for _ in range(title_rows):
        ws.append(["Report title"])
    ws.append(headers)
    for r in rows:
        ws.append(list(r))
    if formula_cell:
        ws[formula_cell[0]] = formula_cell[1]
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def csv_bytes(rows, headers=HEAD):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def upload(env, data, name="data.xlsx", api=None):
    api = api or env.api
    r = api.post("/api/imports/upload", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def config(env, batch, purpose, header_row=1, options=None, scope=None, mapping=None):
    ins = env.api.get(f"/api/imports/{batch['batch_id']}/inspect?header_row={header_row}").json()
    return {"purpose": purpose, "sheet": ins["sheet"], "header_row": header_row,
            "mapping": mapping or ins["suggested_mapping"], "options": options or {}, "scope": scope or []}


def preview(env, batch, cfg, decisions=None, api=None):
    api = api or env.api
    r = api.post(f"/api/imports/{batch['batch_id']}/preview", {"config": cfg, "decisions": decisions or {}})
    assert r.status_code == 200, r.text
    return r.json()


def commit(env, batch, cfg, decisions=None, api=None, **kw):
    api = api or env.api
    return api.post(f"/api/imports/{batch['batch_id']}/commit", {"config": cfg, "decisions": decisions or {}, **kw})


def run(env, data, purpose, name="data.xlsx", options=None, scope=None, header_row=1, decisions=None, **kw):
    b = upload(env, data, name)
    cfg = config(env, b, purpose, header_row, options, scope)
    p = preview(env, b, cfg, decisions)
    r = commit(env, b, cfg, decisions, **kw)
    return b, cfg, p, r


def emp(env, rhc):
    items = env.api.get(f"/api/employees?view=everything&search={rhc}").json()["items"]
    return [i for i in items if i["rhc_id"] == rhc][0]


def test_leading_zeros_from_csv_and_text_xlsx(env):
    rows = [["00123", "Csv Zero", "Dev", "", "Ahmedabad", "2024-01-05", "Active"]]
    _, _, p, r = run(env, csv_bytes(rows), "roster", name="roster.csv")
    assert r.status_code == 200, r.text
    assert emp(env, "00123")["full_name"] == "Csv Zero"
    # Numeric ID cells in Excel are flagged because Excel may already have dropped zeros.
    b = upload(env, xlsx([[123, "Numeric Id", "Dev", "", "Ahmedabad", "2024-01-05", "Active"]]))
    p = preview(env, b, config(env, b, "partial", options={"create_missing": True}))
    assert any("leading zeros" in w["message"] for w in p["warnings"])


def test_duplicates_and_ambiguous_rows_block_commit(env):
    env.employee(rhc_id="E1", full_name="Email One", official_email="shared@x.com")
    rows = [["D1", "Dup One", "", "", "Ahmedabad", "", ""], ["d1", "Dup One Again", "", "", "Ahmedabad", "", ""],
            ["", "No Id", "", "shared@x.com", "Ahmedabad", "", ""], ["D3", "Bad Date", "", "", "Ahmedabad", "40/40/2020", ""]]
    b = upload(env, xlsx(rows))
    cfg = config(env, b, "partial", options={"match_by_email": True, "create_missing": True})
    p = preview(env, b, cfg)
    assert p["summary"]["duplicates"] == 2 and p["summary"]["errors"] == 1
    assert p["summary"]["updates"] == 1 and p["updates"][0]["rhc_id"] == "E1", "unique email fallback matched E1"
    assert commit(env, b, cfg).status_code == 422
    # Resolve: skip one duplicate and the bad row, then re-preview and commit.
    dec = {"3": "skip", "5": "skip"}
    p = preview(env, b, cfg, dec)
    assert p["summary"]["blocking"] == 0
    r = commit(env, b, cfg, dec)
    assert r.status_code == 200, r.text
    assert emp(env, "D1")["full_name"] == "Dup One"


def test_email_fallback_only_when_enabled_and_unique(env):
    env.employee(rhc_id="M1", full_name="Mail A", official_email="a@x.com")
    rows = [["", "Mail A", "Lead", "a@x.com", "", "", ""]]
    b = upload(env, xlsx(rows))
    p = preview(env, b, config(env, b, "partial"))
    assert p["summary"]["unmatched"] == 1 and p["summary"]["updates"] == 0, "no RHC ID and no fallback: no match"
    b = upload(env, xlsx(rows))
    p = preview(env, b, config(env, b, "partial", options={"match_by_email": True}))
    assert p["summary"]["updates"] == 1 and p["updates"][0]["match_method"] == "Official email"


def test_name_match_is_only_a_suggestion(env):
    env.employee(rhc_id="N1", full_name="Riya Shah")
    rows = [["N2", "Riya  shah", "", "", "Ahmedabad", "", ""]]
    b = upload(env, xlsx(rows))
    cfg = config(env, b, "roster")
    p = preview(env, b, cfg)
    assert p["summary"]["suggestions"] == 1 and p["suggestions"][0]["possible_matches"][0]["rhc_id"] == "N1"
    assert commit(env, b, cfg).status_code == 422
    p = preview(env, b, cfg, {"2": "create"})
    assert p["summary"]["new"] == 1
    r = commit(env, b, cfg, {"2": "create"}, deactivate_ids=[])
    assert r.status_code == 200
    assert emp(env, "N1")["employment_status"] == "Active", "never merged or deactivated silently"
    assert emp(env, "N2")["full_name"] == "Riya  shah"


def test_historical_then_current_roster(env):
    hist = [["H1", "Hist Current", "Associate", "h1@x.com", "Ahmedabad", "2019-01-01", "Active"],
            ["H2", "Hist Left", "Associate", "h2@x.com", "Vadodara", "2018-01-01", "Left"],
            ["H3", "Hist Vad", "Associate", "", "Vadodara", "2020-01-01", ""]]
    _, _, p, r = run(env, xlsx(hist), "historical")
    assert r.status_code == 200 and p["summary"]["new"] == 3
    assert env.api.get("/api/employees").json()["total"] == 0, "historical-only records stay outside the directory"
    assert env.api.get("/api/employees?view=historical").json()["total"] == 3
    roster = [["H1", "Hist Current", "Senior Associate", "h1@x.com", "Ahmedabad", "", ""],
              ["R9", "Brand New", "Analyst", "", "Ahmedabad", "2026-09-01", ""]]
    _, _, p, r = run(env, xlsx(roster), "roster", scope=["Ahmedabad"])
    assert r.status_code == 200, r.text
    h1 = emp(env, "H1")
    assert h1["record_type"] == "directory" and h1["employment_status"] == "Active"
    assert h1["designation"] == "Senior Associate"
    assert h1["date_of_joining"] == "2019-01-01", "historical value kept (blank roster cell did not erase it)"
    assert emp(env, "H3")["record_type"] == "historical", "outside the roster scope: untouched"
    assert {i["rhc_id"] for i in env.api.get("/api/employees").json()["items"]} == {"H1", "R9"}
    # A later historical import enriches matched current employees with approved blank fields only.
    enrich = [["R9", "Brand New", "Old Title", "r9@x.com", "Ahmedabad", "2020-01-01", "Left"]]
    _, _, p, r = run(env, xlsx(enrich), "historical")
    r9 = emp(env, "R9")
    assert r9["official_email"] == "r9@x.com", "blank approved field filled"
    assert r9["date_of_joining"] == "2026-09-01" and r9["designation"] == "Analyst" and r9["employment_status"] == "Active"


def test_roster_missing_employees_need_explicit_confirmation(env):
    keep = env.employee(rhc_id="K1", full_name="Keep", location="Ahmedabad")
    gone = env.employee(rhc_id="G1", full_name="Gone", location="Ahmedabad")
    other = env.employee(rhc_id="O1", full_name="Other City", location="Vadodara")
    rows = [["K1", "Keep", "", "", "Ahmedabad", "", ""]]
    b = upload(env, xlsx(rows))
    cfg = config(env, b, "roster", scope=["Ahmedabad"])
    p = preview(env, b, cfg)
    assert [d["rhc_id"] for d in p["deactivations"]] == ["G1"]
    r = commit(env, b, cfg, deactivate_ids=[gone["id"]], confirm_deactivation=False)
    assert r.status_code == 422, "needs the explicit confirmation flag"
    r = commit(env, b, cfg, deactivate_ids=[gone["id"]], confirm_deactivation=True, last_working_date="2026-09-15")
    assert r.status_code == 200, r.text
    g = emp(env, "G1")
    assert g["employment_status"] == "Left" and g["last_working_date"] == "2026-09-15"
    assert emp(env, "O1")["employment_status"] == "Active"
    assert emp(env, "K1")["employment_status"] == "Active"
    _ = keep, other


def test_partial_update_never_deactivates_and_blank_cells_do_not_erase(env):
    a = env.employee(rhc_id="PA", full_name="Part A", designation="Keep Me", official_email="pa@x.com")
    env.employee(rhc_id="PB", full_name="Part B")
    rows = [["PA", "Part A", "", "", "", "", ""]]
    _, _, p, r = run(env, xlsx(rows), "partial")
    assert r.status_code == 200 and p["summary"]["deactivations"] == 0
    assert emp(env, "PA")["designation"] == "Keep Me" and emp(env, "PB")["employment_status"] == "Active"
    # Explicitly selected clearing.
    _, _, p, r = run(env, xlsx(rows), "partial", options={"clear_blank_fields": ["designation"]},
                     acknowledge_repeat=True)
    assert r.status_code == 200, r.text
    assert emp(env, "PA")["designation"] is None
    assert emp(env, "PA")["official_email"] == "pa@x.com"
    _ = a


def test_designation_and_selected_field_imports(env):
    env.employee(rhc_id="S1", full_name="Sel One", official_email="s1@x.com")
    rows = [["S1", "Renamed", "Manager", "changed@x.com", "Vadodara", "", ""]]
    _, _, p, r = run(env, xlsx(rows), "designation")
    assert r.status_code == 200 and "Location" in p["ignored_columns"]
    e = emp(env, "S1")
    assert e["designation"] == "Manager" and e["full_name"] == "Sel One" and e["location"] == "Ahmedabad"
    run(env, xlsx(rows), "selected", options={"selected_fields": ["official_email"]}, acknowledge_repeat=True)
    e = emp(env, "S1")
    assert e["official_email"] == "changed@x.com" and e["full_name"] == "Sel One"


def test_repeat_import_detected(env):
    data = csv_bytes([["RP1", "Repeat", "", "", "Ahmedabad", "", ""]])
    run(env, data, "roster", name="r.csv")
    b, cfg, p, r = run(env, data, "partial", name="r.csv")
    assert p["repeats"] and r.status_code == 422
    assert commit(env, b, cfg, acknowledge_repeat=True).status_code == 200


def test_commit_refused_if_records_change_after_preview(env):
    e = env.employee(rhc_id="CH1", full_name="Changing")
    b = upload(env, xlsx([["CH1", "Changing", "Lead", "", "", "", ""]]))
    cfg = config(env, b, "partial")
    preview(env, b, cfg)
    env.api.patch(f"/api/employees/{e['id']}", {"version": 1, "changes": {"notes": "edited meanwhile"}})
    r = commit(env, b, cfg)
    assert r.status_code == 409


def test_undo_and_undo_conflicts(env):
    env.employee(rhc_id="U1", full_name="Undo One", designation="Before")
    rows = [["U1", "Undo One", "After", "", "", "", ""], ["U2", "Undo New", "Dev", "", "Ahmedabad", "", ""]]
    b, *_ = run(env, xlsx(rows), "partial", options={"create_missing": True})
    assert emp(env, "U1")["designation"] == "After"
    assert env.api.get(f"/api/imports/{b['batch_id']}/undo-check").json()["conflicts"] == []
    r = env.api.post(f"/api/imports/{b['batch_id']}/undo")
    assert r.status_code == 200 and r.json() == {"reverted": 1, "removed": 1}
    assert emp(env, "U1")["designation"] == "Before"
    assert env.api.get("/api/employees?view=everything&search=U2").json()["total"] == 0
    # Second import, then a later manual edit: undo must refuse and change nothing.
    b2, *_ = run(env, xlsx([["U1", "Undo One", "After Again", "", "", "", ""]]), "partial")
    u1 = emp(env, "U1")
    env.api.patch(f"/api/employees/{u1['id']}", {"version": u1["version"], "changes": {"designation": "Manual"}})
    r = env.api.post(f"/api/imports/{b2['batch_id']}/undo")
    assert r.status_code == 409 and r.json()["details"]["conflicts"][0]["field"] == "designation"
    assert emp(env, "U1")["designation"] == "Manual"


def test_formulas_are_not_executed_and_scope_filters(env):
    rows = [["F1", "Formula Row", "x", "", "Ahmedabad", "", ""], ["F2", "Vad Row", "", "", "Vadodara", "", ""]]
    data = xlsx(rows, formula_cell=("C2", "=1+1"))
    b = upload(env, data)
    p = preview(env, b, config(env, b, "roster", scope=["Ahmedabad"]))
    assert any("formula" in w["message"] for w in p["warnings"])
    assert p["summary"]["out_of_scope"] == 1


def test_header_row_selection_and_template_reuse(env):
    rows = [["T1", "Title Row", "", "", "Ahmedabad", "01-Oct-2026", ""]]
    b = upload(env, xlsx(rows, title_rows=2))
    cfg = config(env, b, "roster", header_row=3)
    assert cfg["mapping"]["RHC ID"] == "rhc_id"
    t = env.api.post("/api/import-templates", {"name": "HR monthly", "purpose": "roster", "header_row": 3,
                                               "mapping": cfg["mapping"], "options": {}, "scope": ["Ahmedabad"]})
    assert t.status_code == 200
    assert env.api.get("/api/import-templates").json()[0]["name"] == "HR monthly"
    p = preview(env, b, cfg)
    assert p["new"][0]["values"]["Date of joining"] == "2026-10-01"


def test_error_report_download(env):
    b = upload(env, xlsx([["", "No Id", "", "", "Ahmedabad", "", ""]]))
    preview(env, b, config(env, b, "partial", options={"create_missing": True}))
    r = env.api.get(f"/api/imports/{b['batch_id']}/report")
    assert r.status_code == 200 and r.content[:2] == b"PK"


def test_date_parsing_consistency():
    from relay_hub.timeutil import parse_date
    assert parse_date("03/04/2026") == dt.date(2026, 4, 3)
    assert parse_date("03/04/2026", day_first=False) == dt.date(2026, 3, 4)
    assert parse_date("2026-04-03") == dt.date(2026, 4, 3)
    assert parse_date("3-Apr-26") == dt.date(2026, 4, 3)
    assert parse_date(46115) == dt.date(2026, 4, 3)
    assert parse_date(dt.datetime(2026, 4, 3, 9, 0)) == dt.date(2026, 4, 3)
