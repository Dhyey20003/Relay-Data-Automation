"""Excel/CSV import engine.

Workflow: upload -> inspect (sheet, header row, mapping) -> preview -> resolve
blocking issues -> commit atomically -> results / error report -> optional undo.

Matching rules:
  1. RHC ID (case-insensitive, leading zeros preserved).
  2. Exact, unique official email, only when the fallback is switched on.
  3. Name matches are suggestions only: a row that would create a new record
     while an existing employee has the same name must be explicitly resolved
     ("create as new" or "skip"). Records are never merged by name.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from collections import defaultdict
from pathlib import Path

from . import audit, db, employees, fields, settings_store, sheets, timeutil
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .security import Principal

PURPOSES = {
    "historical": "Historical employee data",
    "roster": "Complete current employee roster",
    "partial": "Partial employee update",
    "designation": "Designation update",
    "selected": "Selected field update",
}
ALLOWED_EXT = {".xlsx", ".csv"}
VIRTUAL_FIELDS = {"_first_name": "First name (combined into Full name)",
                  "_last_name": "Last name (combined into Full name)"}

SYNONYMS = {
    "rhc_id": ["rhc id", "rhcid", "rhc", "rhc no", "rhc number", "employee id", "emp id", "empid", "emp code",
               "employee code", "employee no", "emp no", "employee number"],
    "full_name": ["full name", "name", "employee name", "emp name", "employee full name"],
    "_first_name": ["first name", "firstname", "given name"],
    "_last_name": ["last name", "lastname", "surname", "family name"],
    "designation": ["designation", "title", "job title", "position", "role"],
    "department": ["department", "dept", "team", "function"],
    "official_email": ["official email", "email", "email id", "work email", "office email", "company email",
                       "official email id", "e-mail", "mail id"],
    "location": ["location", "city", "office", "branch", "base location", "work location"],
    "date_of_joining": ["date of joining", "doj", "joining date", "join date", "date joined"],
    "date_of_birth": ["date of birth", "dob", "birth date", "birthdate"],
    "employment_status": ["employment status", "status", "emp status", "employee status"],
    "last_working_date": ["last working date", "lwd", "last working day", "exit date", "relieving date",
                          "date of leaving"],
    "assignment_status": ["assignment status", "assignment", "billing status", "deployment status"],
    "client_name": ["client", "client name", "customer", "customer name"],
    "assignment_start": ["assignment start", "assignment start date", "client start date", "deployment date"],
    "assignment_end": ["assignment end", "assignment end date", "client end date"],
    "bench_start": ["bench start", "bench start date", "bench since", "on bench since"],
    "bench_end": ["bench end", "bench end date"],
    "notes": ["notes", "remarks", "comments", "comment", "remark"],
}


def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(h or "").lower()).strip()


def _norm_name(n: str | None) -> str:
    return re.sub(r"\s+", " ", str(n or "").strip().lower())


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ upload

def upload(conn, paths, principal: Principal, filename: str, data: bytes) -> dict:
    if not principal.can("imports.run"):
        raise Forbidden("You do not have permission to import data")
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValidationFailed("Only .xlsx and .csv files can be imported. Save older .xls files as .xlsx first.")
    limits = settings_store.get(conn, "import_limits")
    if len(data) > limits["max_file_mb"] * 1024 * 1024:
        raise ValidationFailed(f"The file is larger than the {limits['max_file_mb']} MB limit.")
    if not data:
        raise ValidationFailed("The file is empty.")
    folder = paths.staging / "imports"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename).name)[:120]
    digest = hashlib.sha256(data).hexdigest()
    with db.transaction(conn):
        cur = conn.execute(
            "INSERT INTO import_batches(filename, file_hash, file_path, status, operator, created_at) "
            "VALUES(?,?,?,?,?,?)", (Path(filename).name, digest, "", "uploaded", principal.label, timeutil.iso_utc()))
        batch_id = cur.lastrowid
        target = folder / f"{batch_id:06d}_{safe}"
        target.write_bytes(data)
        conn.execute("UPDATE import_batches SET file_path=? WHERE id=?", (str(target), batch_id))
    try:
        sheet_names = sheets.list_sheets(target)
    except Exception as exc:  # corrupt or unsupported workbook
        with db.transaction(conn):
            conn.execute("UPDATE import_batches SET status='failed', summary=? WHERE id=?",
                         (db.jdump({"error": str(exc)}), batch_id))
        raise ValidationFailed(f"The file could not be read: {exc}") from None
    return {"batch_id": batch_id, "filename": Path(filename).name, "sheets": sheet_names,
            "repeats": repeat_imports(conn, digest, batch_id)}


def repeat_imports(conn, digest: str, exclude_id: int | None = None) -> list[dict]:
    return db.rows(conn, "SELECT id, filename, purpose, status, operator, created_at, committed_at FROM import_batches "
                         "WHERE file_hash=? AND id IS NOT ? AND status IN ('committed','undone') ORDER BY id DESC",
                   (digest, exclude_id))


def get_batch(conn, batch_id: int) -> dict:
    b = db.one(conn, "SELECT * FROM import_batches WHERE id=?", (batch_id,))
    if not b:
        raise NotFound("Import batch not found")
    return b


def inspect(conn, batch_id: int, sheet: str | None, header_row: int) -> dict:
    b = get_batch(conn, batch_id)
    path = Path(b["file_path"])
    names = sheets.list_sheets(path)
    sheet = sheet if sheet in names else names[0]
    raw = sheets.preview(path, None if path.suffix.lower() == ".csv" else sheet)
    header_row = max(1, int(header_row or 1))
    headers = unique_headers(raw[header_row - 1] if len(raw) >= header_row else [])
    return {"sheets": names, "sheet": sheet, "header_row": header_row, "rows": raw, "headers": headers,
            "suggested_mapping": suggest_mapping(conn, headers)}


def unique_headers(row: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, h in enumerate(row):
        h = str(h).strip() or f"Column {sheets._col_letter(i)}"
        if h.lower() in seen:
            seen[h.lower()] += 1
            h = f"{h} ({seen[h.lower()]})"
        else:
            seen[h.lower()] = 1
        out.append(h)
    return out


def suggest_mapping(conn, headers: list[str]) -> dict[str, str]:
    lookup = {}
    for key, words in SYNONYMS.items():
        for w in words:
            lookup.setdefault(_norm_header(w), key)
    for f in fields.custom_field_defs(conn):
        lookup.setdefault(_norm_header(f.label), f.key)
        lookup.setdefault(_norm_header(f.key[3:]), f.key)
    for f in fields.core_fields([]):
        lookup.setdefault(_norm_header(f.label), f.key)
    out, used = {}, set()
    for h in headers:
        key = lookup.get(_norm_header(h), "")
        if key and key not in used:
            out[h] = key
            used.add(key)
        else:
            out[h] = ""
    return out


def mapping_targets(conn) -> list[dict]:
    out = [{"key": f.key, "label": f.label, "custom": f.custom} for f in fields.all_fields(conn) if f.importable]
    out += [{"key": k, "label": v, "custom": False} for k, v in VIRTUAL_FIELDS.items()]
    return out


# --------------------------------------------------------------- analysis

class Config:
    def __init__(self, conn, principal: Principal, raw: dict):
        self.purpose = raw.get("purpose")
        if self.purpose not in PURPOSES:
            raise ValidationFailed("Choose what this import is for.")
        self.sheet = raw.get("sheet")
        self.header_row = max(1, int(raw.get("header_row") or 1))
        self.mapping: dict[str, str] = {h: k for h, k in (raw.get("mapping") or {}).items() if k}
        opts = raw.get("options") or {}
        self.match_by_email = bool(opts.get("match_by_email"))
        self.clear_blank_fields = set(opts.get("clear_blank_fields") or [])
        self.selected_fields = set(opts.get("selected_fields") or [])
        self.create_missing = bool(opts.get("create_missing"))
        self.historical_overwrite = bool(opts.get("historical_overwrite"))
        self.options = opts
        all_locs = settings_store.locations(conn)
        scope = [s for s in (raw.get("scope") or []) if s in all_locs]
        self.scope = scope or list(all_locs)
        self.template_id = raw.get("template_id")
        self.raw = raw


def _allowed_fields(conn, cfg: Config) -> set[str]:
    importable = {f.key for f in fields.all_fields(conn) if f.importable} | set(VIRTUAL_FIELDS)
    if cfg.purpose == "designation":
        return {"rhc_id", "official_email", "full_name", "designation"}
    if cfg.purpose == "selected":
        return {"rhc_id", "official_email", "full_name"} | (cfg.selected_fields & importable)
    return importable


def analyze(conn, principal: Principal, batch: dict, cfg: Config, decisions: dict | None = None) -> dict:
    decisions = {str(k): v for k, v in (decisions or {}).items()}
    fm = fields.field_map(conn)
    locations = settings_store.locations(conn)
    day_first = settings_store.get(conn, "date_day_first")
    path = Path(batch["file_path"])
    raw_rows = sheets.read_rows(path, None if path.suffix.lower() == ".csv" else cfg.sheet)
    if len(raw_rows) < cfg.header_row:
        raise ValidationFailed("The selected header row is beyond the end of the sheet.")
    headers = unique_headers([sheets.display(c.value) for c in raw_rows[cfg.header_row - 1]])
    limits = settings_store.get(conn, "import_limits")
    data_rows = raw_rows[cfg.header_row:]
    if len(data_rows) > limits["max_rows"]:
        raise ValidationFailed(f"The sheet has more than {limits['max_rows']} rows.")

    # ---- mapping validation
    allowed = _allowed_fields(conn, cfg)
    col_for: dict[str, int] = {}
    problems = []
    ignored_columns = []
    for h, key in cfg.mapping.items():
        if h not in headers:
            continue  # template column not present in this file: ignored
        if key not in allowed and cfg.purpose in ("designation", "selected"):
            ignored_columns.append(h)  # this purpose only updates its own fields
            continue
        if key not in allowed:
            problems.append(f"Column '{h}' is mapped to '{fm[key].label if key in fm else key}', which this "
                            f"import purpose does not update. Set it to Ignore.")
            continue
        if key in col_for:
            problems.append(f"Two columns are mapped to {fm[key].label if key in fm else key}.")
            continue
        if key in fm and not principal.field_editable(key):
            problems.append(f"You do not have permission to import {fm[key].label}.")
            continue
        col_for[key] = headers.index(h)
    if "rhc_id" not in col_for and not (cfg.match_by_email and "official_email" in col_for):
        problems.append("Map a column to RHC ID (or map Official email and switch on the email fallback).")
    if cfg.purpose == "designation" and "designation" not in col_for:
        problems.append("Map a column to Designation.")
    if cfg.purpose == "selected" and not (set(col_for) & cfg.selected_fields):
        problems.append("Choose the fields to update and map their columns.")
    if cfg.purpose == "roster" and "location" not in col_for and len(cfg.scope) != 1:
        problems.append("A complete roster needs a Location column, or a scope of exactly one location.")
    if cfg.purpose == "roster" and not principal.can("imports.roster"):
        problems.append("Your account cannot run complete-roster imports.")
    if principal.locations is not None and set(cfg.scope) - set(principal.locations):
        problems.append("The location scope includes locations outside your permission.")
    if problems:
        raise ValidationFailed("The column mapping needs attention.", {"mapping_problems": problems})

    update_fields = [k for k in col_for if k not in ("_first_name", "_last_name")]
    if cfg.purpose == "designation":
        update_fields = ["designation"]
    elif cfg.purpose == "selected":
        update_fields = [k for k in update_fields if k in cfg.selected_fields]

    # ---- employees index
    all_emps = db.rows(conn, "SELECT * FROM employees")
    by_rhc = {e["rhc_id"].upper(): e for e in all_emps if e["rhc_id"]}
    by_email = defaultdict(list)
    by_name = defaultdict(list)
    for e in all_emps:
        if e["official_email"]:
            by_email[e["official_email"].lower()].append(e)
        by_name[_norm_name(e["full_name"])].append(e)

    result = {k: [] for k in ("new", "updates", "unchanged", "errors", "duplicates", "suggestions", "unmatched",
                              "out_of_scope", "deactivations", "skipped", "warnings")}
    parsed = []
    for idx, row in enumerate(data_rows):
        row_no = cfg.header_row + idx + 1
        cells = {k: (row[i] if i < len(row) else sheets.CellValue(None)) for k, i in col_for.items()}
        if all(c.value is None or str(c.value).strip() == "" for c in cells.values()):
            continue
        values, errs, warns = {}, {}, []
        for key, cell in cells.items():
            if cell.formula:
                warns.append(f"{_label(fm, key)} comes from a formula; the saved value was used and the formula "
                             "was not run.")
            if key in ("_first_name", "_last_name"):
                values[key] = sheets.display(cell.value).strip() or None
                continue
            if key == "rhc_id" and cell.numeric:
                warns.append("RHC ID was stored as a number in the spreadsheet; any leading zeros may already have "
                             "been lost in Excel. Format the column as Text to keep them.")
            try:
                values[key] = fields.normalize_value(fm[key], cell.value, locations, day_first)
            except fields.FieldError as exc:
                errs[key] = str(exc)
        if "full_name" not in col_for and (values.get("_first_name") or values.get("_last_name")):
            values["full_name"] = " ".join(v for v in (values.get("_first_name"), values.get("_last_name")) if v)
        values.pop("_first_name", None)
        values.pop("_last_name", None)
        if cfg.purpose == "roster" and "location" not in col_for and len(cfg.scope) == 1:
            values["location"] = cfg.scope[0]
        parsed.append({"row": row_no, "values": values, "errors": errs, "warnings": warns})

    # ---- duplicates within the file
    groups = defaultdict(list)
    for p in parsed:
        if p["values"].get("rhc_id"):
            groups[("RHC ID", p["values"]["rhc_id"])].append(p["row"])
        elif cfg.match_by_email and p["values"].get("official_email"):
            groups[("email", p["values"]["official_email"])].append(p["row"])
    dup_rows = {}
    for (kind, key), rws in groups.items():
        active = [r for r in rws if decisions.get(str(r)) != "skip"]
        if len(rws) > 1:
            for r in rws:
                dup_rows[r] = (kind, key, [x for x in rws if x != r], len(active) > 1)

    matched_ids: set[int] = set()
    preview_versions: dict[int, int] = {}
    new_rhc: set[str] = set()
    new_emails: set[str] = set()
    for p in parsed:
        row_no, values = p["row"], p["values"]
        ident = {"row": row_no, "rhc_id": values.get("rhc_id"), "full_name": values.get("full_name")}
        for w in p["warnings"]:
            result["warnings"].append({**ident, "message": w})
        if decisions.get(str(row_no)) == "skip":
            result["skipped"].append({**ident, "reason": "Skipped by operator"})
            continue
        if row_no in dup_rows:
            kind, key, others, still_dup = dup_rows[row_no]
            if still_dup:
                result["duplicates"].append({**ident, "key": key, "kind": kind, "other_rows": others,
                                             "message": f"{kind} {key} appears on more than one row "
                                                        f"(rows {', '.join(map(str, sorted(others + [row_no])))}). "
                                                        "Skip the extra rows or fix the file."})
                continue
        if p["errors"]:
            result["errors"].append({**ident, "fields": p["errors"],
                                     "messages": [f"{_label(fm, k)}: {v}" for k, v in p["errors"].items()]})
            continue

        # ---- matching
        emp, method, match_err = None, None, None
        rhc = values.get("rhc_id")
        email = values.get("official_email")
        if rhc:
            emp = by_rhc.get(rhc.upper())
            method = "RHC ID" if emp else None
        if emp is None and cfg.match_by_email and email:
            cands = by_email.get(email.lower(), [])
            if len(cands) == 1:
                cand = cands[0]
                if rhc and cand["rhc_id"] and cand["rhc_id"].upper() != rhc.upper():
                    match_err = (f"Email {email} belongs to {cand['full_name']} with RHC ID {cand['rhc_id']}, "
                                 f"but this row says {rhc}. Check which is correct.")
                else:
                    emp, method = cand, "Official email"
            elif len(cands) > 1:
                match_err = f"Email {email} matches more than one employee; it cannot be used to match."
        if match_err:
            result["errors"].append({**ident, "fields": {"official_email": match_err}, "messages": [match_err]})
            continue

        # ---- scope
        row_loc = values.get("location") or (emp["location"] if emp else None)
        if row_loc and row_loc not in cfg.scope:
            result["out_of_scope"].append({**ident, "location": row_loc})
            continue
        if not row_loc and cfg.purpose == "roster":
            result["errors"].append({**ident, "fields": {"location": "Location is required"},
                                     "messages": ["Location: a roster row needs a location"]})
            continue
        if emp and not principal.location_ok(emp["location"]):
            result["errors"].append({**ident, "fields": {"location": "outside your scope"},
                                     "messages": ["This employee is outside your permitted locations."]})
            continue
        if row_loc and not principal.location_ok(row_loc):
            result["errors"].append({**ident, "fields": {"location": "outside your scope"},
                                     "messages": [f"Location {row_loc} is outside your permitted locations."]})
            continue

        if emp:
            if emp["id"] in matched_ids:
                msg = (f"This row matches {emp['full_name']} ({emp['rhc_id']}), which another row already matched "
                       "(for example by email). Skip one of the rows.")
                result["duplicates"].append({**ident, "key": emp["rhc_id"], "kind": "employee", "other_rows": [],
                                             "message": msg})
                continue
            matched_ids.add(emp["id"])
            preview_versions[emp["id"]] = emp["version"]
            change = _plan_update(conn, cfg, fm, emp, values, update_fields, principal)
            change.update({"row": row_no, "employee_id": emp["id"], "name": emp["full_name"],
                           "rhc_id": emp["rhc_id"], "match_method": method, "version": emp["version"]})
            if change["errors"]:
                result["errors"].append({**ident, "fields": change["errors"],
                                         "messages": [f"{_label(fm, k)}: {v}" for k, v in change["errors"].items()]})
            elif change["changes"] or change.get("extra"):
                result["updates"].append(change)
            else:
                result["unchanged"].append({**ident, "employee_id": emp["id"], "name": emp["full_name"],
                                            "note": change.get("note")})
            continue

        # ---- unmatched row
        creates = cfg.purpose in ("roster", "historical") or (cfg.purpose == "partial" and cfg.create_missing)
        if not creates:
            result["unmatched"].append({**ident, "message": "No employee with this RHC ID"
                                        + (" or email" if cfg.match_by_email else "") + "; row not imported."})
            continue
        new_vals = {k: v for k, v in values.items() if v is not None}
        record_type = "historical" if cfg.purpose == "historical" else "directory"
        if record_type == "historical":
            new_vals.setdefault("employment_status", "Left")
        else:
            new_vals.setdefault("employment_status", "Active")
        errs = fields.cross_validate({**new_vals, "record_type": "directory"})
        if not new_vals.get("rhc_id"):
            errs["rhc_id"] = "RHC ID is required to create a new record"
        if new_vals.get("official_email"):
            e2 = by_email.get(new_vals["official_email"].lower())
            if e2 or new_vals["official_email"] in new_emails:
                errs["official_email"] = f"Email {new_vals['official_email']} is already used by another employee"
            new_emails.add(new_vals["official_email"])
        if new_vals.get("rhc_id"):
            new_rhc.add(new_vals["rhc_id"])
        if errs:
            result["errors"].append({**ident, "fields": errs,
                                     "messages": [f"{_label(fm, k)}: {v}" for k, v in errs.items()]})
            continue
        sugg = [e for e in by_name.get(_norm_name(new_vals.get("full_name")), [])]
        item = {**ident, "values": _display_values(fm, new_vals, principal), "record_type": record_type,
                "_values": new_vals}
        if sugg and decisions.get(str(row_no)) != "create":
            item["possible_matches"] = [{"employee_id": s["id"], "name": s["full_name"], "rhc_id": s["rhc_id"],
                                         "location": s["location"], "status": s["employment_status"],
                                         "record_type": s["record_type"]} for s in sugg]
            item["message"] = ("An existing employee has the same name. Names are never merged automatically: "
                               "check the RHC ID, then choose 'Create as new' or 'Skip row'.")
            result["suggestions"].append(item)
            continue
        if sugg:
            item["note"] = "Created as new despite a name match (operator decision)."
        result["new"].append(item)

    # ---- proposed deactivations (complete roster only)
    if cfg.purpose == "roster":
        present_ids = matched_ids | {u["employee_id"] for u in result["updates"]}
        for e in all_emps:
            if e["record_type"] != "directory" or e["employment_status"] not in ("Active", "Pending Review"):
                continue
            if e["location"] not in cfg.scope or e["id"] in present_ids:
                continue
            if not principal.location_ok(e["location"]):
                continue
            skipped_here = any(s.get("rhc_id") and e["rhc_id"] and s["rhc_id"].upper() == e["rhc_id"].upper()
                               for s in result["skipped"] + result["errors"] + result["duplicates"]
                               + result["out_of_scope"])
            result["deactivations"].append({"employee_id": e["id"], "name": e["full_name"], "rhc_id": e["rhc_id"],
                                            "location": e["location"], "status": e["employment_status"],
                                            "version": e["version"], "row_problem": skipped_here})
            preview_versions[e["id"]] = e["version"]

    blocking = len(result["errors"]) + len(result["duplicates"]) + len(result["suggestions"])
    summary = {k: len(v) for k, v in result.items()}
    summary["blocking"] = blocking
    summary["rows_read"] = len(parsed)
    result["summary"] = summary
    result["versions"] = {str(k): v for k, v in preview_versions.items()}
    result["headers"] = headers
    result["update_fields"] = update_fields
    result["ignored_columns"] = ignored_columns
    return result


def _label(fm, key):
    if key in fm:
        return fm[key].label
    return VIRTUAL_FIELDS.get(key, key)


def _display_values(fm, values: dict, principal: Principal) -> dict:
    out = {}
    for k, v in values.items():
        if k == "date_of_birth":
            v = fields.mask_dob(v, principal.dob_access)
        out[_label(fm, k)] = v
    return out


def _plan_update(conn, cfg: Config, fm, emp: dict, values: dict, update_fields: list[str],
                 principal: Principal) -> dict:
    before = employees.flat_values(emp)
    planned: dict = {}
    extra: dict = {}
    note = None
    if cfg.purpose == "historical" and emp["record_type"] == "directory":
        # Enrich a current employee with approved historical fields only.
        approved = set(settings_store.get(conn, "historical_enrich_fields") or [])
        for k in update_fields:
            v = values.get(k)
            if k not in approved or v is None:
                continue
            if before.get(k) in (None, "") or cfg.historical_overwrite:
                planned[k] = v
        note = "Current employee: only approved historical fields that are blank were filled."
        kind = "enrich"
    else:
        for k in update_fields:
            if k not in values:
                continue
            v = values[k]
            if v is None:
                if k in cfg.clear_blank_fields and k != "rhc_id" and k != "full_name":
                    planned[k] = None
                continue
            planned[k] = v
        kind = "update"
        if cfg.purpose == "roster":
            if emp["record_type"] == "historical":
                extra["record_type"] = "directory"
                kind = "promote"
            if "employment_status" not in planned and emp["employment_status"] != "Active":
                planned["employment_status"] = "Active"
    diffs = {k: v for k, v in planned.items() if employees.to_text(before.get(k)) != employees.to_text(v)}
    merged = {**before, **diffs, **extra}
    errors = employees.relevant_cross_errors(merged, set(diffs))
    if merged.get("record_type") == "directory" and not merged.get("rhc_id"):
        errors["rhc_id"] = "RHC ID is required for a directory employee"
    if "official_email" in diffs and diffs["official_email"]:
        other = db.one(conn, "SELECT id, full_name FROM employees WHERE official_email=? AND id != ?",
                       (diffs["official_email"], emp["id"]))
        if other:
            errors["official_email"] = f"already used by {other['full_name']}"
    for k in diffs:
        if not principal.field_editable(k):
            errors[k] = "You do not have permission to change this field"
    changes = []
    for k, v in diffs.items():
        old = before.get(k)
        new = v
        if k == "date_of_birth":
            old, new = fields.mask_dob(old, principal.dob_access), fields.mask_dob(new, principal.dob_access)
        changes.append({"field": k, "label": _label(fm, k), "old": old, "new": new})
    for k, v in extra.items():
        changes.append({"field": k, "label": "Record type", "old": emp.get(k), "new": v})
    return {"changes": changes, "errors": errors, "kind": kind, "note": note, "_diffs": diffs, "extra": extra}


def _public(result: dict) -> dict:
    """Strip internal keys before returning or storing a preview."""
    out = {}
    for k, v in result.items():
        if isinstance(v, list):
            out[k] = [{kk: vv for kk, vv in item.items() if not kk.startswith("_")} if isinstance(item, dict) else item
                      for item in v]
        else:
            out[k] = v
    return out


def run_preview(conn, principal: Principal, batch_id: int, raw_cfg: dict, decisions: dict | None = None) -> dict:
    if not principal.can("imports.run"):
        raise Forbidden("You do not have permission to import data")
    batch = get_batch(conn, batch_id)
    if batch["status"] not in ("uploaded", "previewed"):
        raise Conflict(f"This import is already {batch['status']}.")
    cfg = Config(conn, principal, raw_cfg)
    result = analyze(conn, principal, batch, cfg, decisions)
    public = _public(result)
    public["repeats"] = repeat_imports(conn, batch["file_hash"], batch_id)
    public["purpose"] = cfg.purpose
    public["scope"] = cfg.scope
    with db.transaction(conn):
        conn.execute(
            "UPDATE import_batches SET purpose=?, template_id=?, scope=?, sheet_name=?, header_row=?, mapping=?, "
            "options=?, status='previewed', previewed_at=?, preview=?, decisions=? WHERE id=?",
            (cfg.purpose, cfg.template_id, db.jdump(cfg.scope), cfg.sheet, cfg.header_row, db.jdump(cfg.mapping),
             db.jdump(cfg.options), timeutil.iso_utc(), db.jdump(public), db.jdump(decisions or {}), batch_id))
    return public


# ------------------------------------------------------------------ commit

def commit(conn, principal: Principal, batch_id: int, raw_cfg: dict, decisions: dict, deactivate_ids: list[int],
           acknowledge_repeat: bool, last_working_date: str | None, confirm_deactivation: bool) -> dict:
    if not principal.can("imports.run"):
        raise Forbidden("You do not have permission to import data")
    batch = get_batch(conn, batch_id)
    if batch["status"] != "previewed":
        raise Conflict("Preview the import before committing it." if batch["status"] == "uploaded"
                       else f"This import is already {batch['status']}.")
    cfg = Config(conn, principal, raw_cfg)
    stored_preview = db.jload(batch["preview"], {})
    repeats = repeat_imports(conn, batch["file_hash"], batch_id)
    if repeats and not acknowledge_repeat:
        raise ValidationFailed("This exact file was imported before. Confirm that you want to import it again.",
                               {"repeats": repeats})
    lwd = None
    if last_working_date:
        try:
            lwd = timeutil.parse_date(last_working_date).isoformat()
        except ValueError as exc:
            raise ValidationFailed(f"Last working date: {exc}") from None
    with db.transaction(conn):
        result = analyze(conn, principal, batch, cfg, decisions)
        if result["summary"]["blocking"]:
            raise ValidationFailed("Resolve the blocking issues before committing.", {"preview": _public(result)})
        # Anything changed since the preview the operator approved? Then re-preview.
        approved_versions = stored_preview.get("versions", {})
        changed = [int(eid) for eid, v in result["versions"].items()
                   if str(eid) in approved_versions and approved_versions[str(eid)] != v]
        unexpected = [eid for eid in result["versions"] if eid not in approved_versions]
        if changed or unexpected or db.jload(batch["decisions"], {}) != {str(k): v for k, v in decisions.items()}:
            raise Conflict("Employee records or decisions changed after the preview. Run the preview again and "
                           "review the updated changes.", {"changed_employee_ids": changed})
        proposed = {d["employee_id"]: d for d in result["deactivations"]}
        deactivate_ids = [int(i) for i in (deactivate_ids or [])]
        if deactivate_ids:
            if cfg.purpose != "roster":
                raise ValidationFailed("Only a complete roster import can mark employees as Left.")
            if not confirm_deactivation:
                raise ValidationFailed("Tick the confirmation to mark the selected employees as Left.")
            if not principal.can("imports.roster") or not principal.can("employees.archive"):
                raise Forbidden("You do not have permission to mark employees as Left.")
            bad = [i for i in deactivate_ids if i not in proposed]
            if bad:
                raise ValidationFailed("Some selected employees are not in the list of missing employees.")

        actor = principal.label
        counts = defaultdict(int)
        for item in result["new"]:
            vals = item["_values"]
            emp_id = employees.insert_employee(conn, vals, actor, item["record_type"])
            conn.execute("INSERT INTO import_changes(batch_id, employee_id, row_no, action) VALUES(?,?,?,?)",
                         (batch_id, emp_id, item["row"], "create"))
            conn.execute("INSERT INTO field_changes(employee_id, field, old_value, new_value, changed_at, changed_by, "
                         "source, import_batch_id) VALUES(?,?,?,?,?,?,?,?)",
                         (emp_id, "_created", None, item["record_type"], timeutil.iso_utc(), actor, "import",
                          batch_id))
            counts["created_historical" if item["record_type"] == "historical" else "created"] += 1
        for upd in result["updates"]:
            emp = employees.raw_employee(conn, upd["employee_id"])
            _, change_ids = employees.write_changes(conn, emp, upd["_diffs"], actor, "import", batch_id,
                                                    extra_columns=upd.get("extra"))
            _log_changes(conn, batch_id, upd["employee_id"], upd["row"], upd["kind"], change_ids)
            counts[{"enrich": "enriched", "promote": "promoted"}.get(upd["kind"], "updated")] += 1
        for eid in deactivate_ids:
            emp = employees.raw_employee(conn, eid)
            vals = {"employment_status": "Left"}
            if lwd and not emp["last_working_date"]:
                vals["last_working_date"] = lwd
            _, change_ids = employees.write_changes(conn, emp, vals, actor, "import", batch_id)
            _log_changes(conn, batch_id, eid, None, "deactivate", change_ids)
            counts["marked_left"] += 1
        summary = {**result["summary"], **counts, "deactivations_proposed": len(result["deactivations"]),
                   "deactivations_confirmed": len(deactivate_ids)}
        public = _public(result)
        conn.execute("UPDATE import_batches SET status='committed', committed_at=?, committed_by=?, summary=?, "
                     "preview=? WHERE id=?",
                     (timeutil.iso_utc(), actor, db.jdump(summary), db.jdump(public), batch_id))
        audit.log(conn, actor, "import.commit", "import", batch_id,
                  f"Imported {batch['filename']} ({PURPOSES[cfg.purpose]})",
                  {"filename": batch["filename"], "file_hash": batch["file_hash"], "purpose": cfg.purpose,
                   "template_id": cfg.template_id, "scope": cfg.scope, "summary": summary})
    return {"batch_id": batch_id, "summary": summary}


def _from_text(f, value):
    """field_changes stores text; restore numbers for number custom fields."""
    if value is None or f is None or f.type != "number":
        return value
    n = float(value)
    return int(n) if n.is_integer() else n


def _log_changes(conn, batch_id, emp_id, row_no, action, change_ids):
    for cid in change_ids:
        fc = db.one(conn, "SELECT field, old_value, new_value FROM field_changes WHERE id=?", (cid,))
        conn.execute("INSERT INTO import_changes(batch_id, employee_id, row_no, action, field, old_value, new_value, "
                     "field_change_id) VALUES(?,?,?,?,?,?,?,?)",
                     (batch_id, emp_id, row_no, action, fc["field"], fc["old_value"], fc["new_value"], cid))


# -------------------------------------------------------------------- undo

def undo_conflicts(conn, batch_id: int) -> list[dict]:
    conflicts = []
    changes = db.rows(conn, "SELECT * FROM import_changes WHERE batch_id=? ORDER BY id DESC", (batch_id,))
    for c in changes:
        emp = employees.raw_employee(conn, c["employee_id"])
        name = emp["full_name"] if emp else f"ID {c['employee_id']}"
        if emp is None:
            conflicts.append({"employee_id": c["employee_id"], "name": name, "field": c["field"],
                              "message": "The employee record no longer exists."})
            continue
        if c["action"] == "create":
            later = db.scalar(conn, "SELECT COUNT(*) FROM field_changes WHERE employee_id=? AND field != '_created' "
                                    "AND (import_batch_id IS NOT ? OR import_batch_id IS NULL)",
                              (c["employee_id"], batch_id))
            photos = db.scalar(conn, "SELECT COUNT(*) FROM photos WHERE employee_id=?", (c["employee_id"],))
            if later or photos or emp["version"] != 1:
                conflicts.append({"employee_id": emp["id"], "name": name, "field": None,
                                  "message": "This record was created by the import and has been changed since "
                                             "(or has photos), so it cannot be removed."})
            continue
        current = employees.flat_values(emp).get(c["field"]) if c["field"] != "record_type" else emp["record_type"]
        later = db.one(conn, "SELECT changed_by, changed_at, new_value FROM field_changes WHERE employee_id=? AND "
                             "field=? AND id > ? ORDER BY id DESC LIMIT 1",
                       (c["employee_id"], c["field"], c["field_change_id"]))
        if later or employees.to_text(current) != c["new_value"]:
            conflicts.append({"employee_id": emp["id"], "name": name, "field": c["field"],
                              "import_value": c["new_value"], "current_value": employees.to_text(current),
                              "changed_by": later["changed_by"] if later else None,
                              "changed_at": later["changed_at"] if later else None,
                              "message": "Changed after this import; undo would overwrite a later change."})
    return conflicts


def undo(conn, principal: Principal, batch_id: int) -> dict:
    if not principal.can("imports.undo"):
        raise Forbidden("You do not have permission to undo imports")
    with db.transaction(conn):
        batch = get_batch(conn, batch_id)
        if batch["status"] != "committed":
            raise Conflict(f"Only committed imports can be undone (this one is {batch['status']}).")
        conflicts = undo_conflicts(conn, batch_id)
        if conflicts:
            raise Conflict("Undo was refused because later changes would be overwritten. Nothing was changed.",
                           {"conflicts": conflicts})
        changes = db.rows(conn, "SELECT * FROM import_changes WHERE batch_id=? ORDER BY id DESC", (batch_id,))
        per_emp = defaultdict(dict)
        extra = defaultdict(dict)
        removed = 0
        for c in changes:
            if c["action"] == "create":
                conn.execute("DELETE FROM employee_history WHERE employee_id=?", (c["employee_id"],))
                conn.execute("DELETE FROM employees WHERE id=?", (c["employee_id"],))
                conn.execute("INSERT INTO field_changes(employee_id, field, old_value, new_value, changed_at, "
                             "changed_by, source) VALUES(?,?,?,?,?,?,?)",
                             (c["employee_id"], "_deleted", None, f"undo of import {batch_id}", timeutil.iso_utc(),
                              principal.label, "undo"))
                removed += 1
            elif c["field"] == "record_type":
                extra[c["employee_id"]][c["field"]] = c["old_value"]
            else:
                per_emp[c["employee_id"]][c["field"]] = c["old_value"]
        fm = fields.field_map(conn)
        reverted = 0
        for emp_id in set(per_emp) | set(extra):
            emp = employees.raw_employee(conn, emp_id)
            vals = {k: _from_text(fm.get(k), v) for k, v in per_emp.get(emp_id, {}).items()}
            employees.write_changes(conn, emp, vals, principal.label, "undo", extra_columns=extra.get(emp_id))
            reverted += 1
        conn.execute("UPDATE import_batches SET status='undone', undone_at=?, undone_by=? WHERE id=?",
                     (timeutil.iso_utc(), principal.label, batch_id))
        audit.log(conn, principal.label, "import.undo", "import", batch_id,
                  f"Undid import {batch['filename']}: {reverted} records reverted, {removed} created records removed")
    return {"reverted": reverted, "removed": removed}


# ------------------------------------------------------------ misc helpers

def error_report(conn, paths, batch_id: int) -> Path:
    batch = get_batch(conn, batch_id)
    preview = db.jload(batch["preview"], {}) or {}
    rows = []
    for cat, label in (("errors", "Error"), ("duplicates", "Duplicate"), ("suggestions", "Name match - needs decision"),
                       ("unmatched", "Unmatched"), ("out_of_scope", "Outside location scope"),
                       ("skipped", "Skipped"), ("warnings", "Warning")):
        for it in preview.get(cat, []):
            msg = it.get("message") or "; ".join(it.get("messages", [])) or it.get("reason") or \
                (f"Location {it.get('location')}" if cat == "out_of_scope" else "")
            rows.append([it.get("row"), it.get("rhc_id"), it.get("full_name"), label, msg])
    for d in preview.get("deactivations", []):
        rows.append([None, d.get("rhc_id"), d.get("name"), "Missing from roster (proposed Left)",
                     f"{d.get('location')} - currently {d.get('status')}"])
    out = paths.exports / "reports" / f"import_{batch_id}_issues.xlsx"
    sheets.write_xlsx(out, [("Issues", ["Sheet row", "RHC ID", "Name", "Category", "Details"], rows)])
    return out


def list_batches(conn, limit: int = 100) -> list[dict]:
    items = db.rows(conn, "SELECT id, filename, file_hash, purpose, template_name, template_id, scope, status, operator, "
                          "created_at, committed_at, committed_by, undone_at, undone_by, summary "
                          "FROM import_batches ORDER BY id DESC LIMIT ?", (limit,))
    for it in items:
        it["summary"] = db.jload(it["summary"], {})
        it["scope"] = db.jload(it["scope"], [])
        it["purpose_label"] = PURPOSES.get(it["purpose"] or "", "")
    return items


def cancel(conn, principal: Principal, batch_id: int) -> None:
    with db.transaction(conn):
        batch = get_batch(conn, batch_id)
        if batch["status"] in ("uploaded", "previewed"):
            conn.execute("UPDATE import_batches SET status='cancelled' WHERE id=?", (batch_id,))
            audit.log(conn, principal.label, "import.cancel", "import", batch_id, f"Cancelled {batch['filename']}")


def save_template(conn, principal: Principal, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValidationFailed("Give the template a name.")
    if data.get("purpose") not in PURPOSES:
        raise ValidationFailed("Choose the import purpose for the template.")
    now = timeutil.iso_utc()
    options = dict(data.get("options") or {})
    options["scope"] = data.get("scope") or []
    with db.transaction(conn):
        existing = db.one(conn, "SELECT id FROM import_templates WHERE name=?", (name,))
        if existing and data.get("id") != existing["id"] and not data.get("overwrite"):
            raise Conflict(f"A template called '{name}' already exists. Choose another name or overwrite it.")
        if existing:
            conn.execute("UPDATE import_templates SET purpose=?, sheet_name=?, header_row=?, mapping=?, options=?, "
                         "updated_at=?, updated_by=? WHERE id=?",
                         (data["purpose"], data.get("sheet"), int(data.get("header_row") or 1),
                          db.jdump(data.get("mapping") or {}), db.jdump(options), now, principal.label,
                          existing["id"]))
            tid = existing["id"]
        else:
            tid = conn.execute("INSERT INTO import_templates(name, purpose, sheet_name, header_row, mapping, options, "
                               "created_at, created_by) VALUES(?,?,?,?,?,?,?,?)",
                               (name, data["purpose"], data.get("sheet"), int(data.get("header_row") or 1),
                                db.jdump(data.get("mapping") or {}), db.jdump(options), now,
                                principal.label)).lastrowid
        audit.log(conn, principal.label, "import.template_save", "import_template", tid, f"Saved template {name}")
    return db.one(conn, "SELECT * FROM import_templates WHERE id=?", (tid,))


def list_templates(conn) -> list[dict]:
    items = db.rows(conn, "SELECT * FROM import_templates ORDER BY name")
    for t in items:
        t["mapping"] = db.jload(t["mapping"], {})
        t["options"] = db.jload(t["options"], {})
    return items


def delete_template(conn, principal: Principal, tid: int) -> None:
    with db.transaction(conn):
        t = db.one(conn, "SELECT name FROM import_templates WHERE id=?", (tid,))
        if not t:
            raise NotFound("Template not found")
        conn.execute("DELETE FROM import_templates WHERE id=?", (tid,))
        audit.log(conn, principal.label, "import.template_delete", "import_template", tid, f"Deleted template {t['name']}")


def cleanup_staging(paths, keep_days: int = 60) -> None:
    """Remove abandoned staging files older than keep_days (committed files are kept for audit)."""
    folder = paths.staging / "imports"
    if not folder.exists():
        return
    cutoff = timeutil.now_utc().timestamp() - keep_days * 86400
    for p in folder.iterdir():
        try:
            if p.stat().st_mtime < cutoff and p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
