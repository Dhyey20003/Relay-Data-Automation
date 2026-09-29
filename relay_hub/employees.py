"""Employee records: queries, edits with version checks, history and exports."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import audit, db, fields, settings_store, timeutil
from .errors import Conflict, Forbidden, NotFound, ValidationFailed
from .fields import FieldDef, FieldError
from .security import Principal

VIEWS = {
    "active": "e.record_type='directory' AND e.employment_status='Active'",
    "current": "e.record_type='directory' AND e.employment_status IN ('Active','Pending Review')",
    "pending": "e.record_type='directory' AND e.employment_status='Pending Review'",
    "left": "e.record_type='directory' AND e.employment_status='Left'",
    "directory": "e.record_type='directory'",
    "historical": "e.record_type='historical'",
    "everything": "1=1",
}
SORTABLE_CORE = set(fields.WRITABLE_CORE) | {"id", "record_type", "created_at", "updated_at", "created_by",
                                             "updated_by"}
FILTER_OPS = {"contains", "equals", "not_equals", "empty", "not_empty", "before", "after", "gt", "lt"}


def to_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


# ------------------------------------------------------------ serialisation

def raw_employee(conn, emp_id: int) -> dict | None:
    return db.one(conn, "SELECT * FROM employees WHERE id=?", (emp_id,))


def flat_values(emp: dict) -> dict:
    """Stored column values plus custom fields flattened as cf_<key>."""
    out = {k: emp.get(k) for k in fields.WRITABLE_CORE}
    for k, v in (db.jload(emp.get("custom"), {}) or {}).items():
        out["cf_" + k] = v
    out["record_type"] = emp.get("record_type")
    return out


def serialize(emp: dict, principal: Principal, fdefs: list[FieldDef], on: dt.date | None = None) -> dict:
    custom = db.jload(emp.get("custom"), {}) or {}
    out: dict = {"id": emp["id"], "version": emp["version"]}
    for f in fdefs:
        if f.key == "id" or not principal.field_visible(f.key):
            continue
        if f.custom:
            out[f.key] = custom.get(f.key[3:])
        elif f.key == "bench_days":
            out[f.key] = timeutil.bench_days(emp["assignment_status"], emp["bench_start"], emp["bench_end"], on)
        elif f.key == "photo":
            if principal.can("photos.view") or principal.include_photos:
                pid = emp.get("current_photo_id")
                out["photo"] = {"id": pid, "thumb": f"/api/photos/{pid}/thumb", "file": f"/api/photos/{pid}/file"} \
                    if pid else None
        elif f.key == "date_of_birth":
            out[f.key] = fields.mask_dob(emp.get(f.key), principal.dob_access)
        else:
            out[f.key] = emp.get(f.key)
    return out


# ------------------------------------------------------------------ queries

@dataclass
class ListQuery:
    view: str = "active"
    search: str = ""
    location: str = ""
    employment_status: str = ""
    assignment_status: str = ""
    department: str = ""
    has_photo: str = ""  # "", "yes", "no"
    filters: list = field(default_factory=list)  # [{field, op, value}]
    sort: str = "full_name"
    direction: str = "asc"
    page: int = 1
    page_size: int = 50


def _column_expr(key: str, fm: dict[str, FieldDef]) -> str:
    f = fm.get(key)
    if f is None:
        raise ValidationFailed(f"Unknown field '{key}'")
    if f.custom:
        return f"json_extract(e.custom, '$.{key[3:]}')"
    if key == "bench_days":
        return "CASE WHEN e.assignment_status='Bench' THEN e.bench_start END"
    if key == "photo":
        return "e.current_photo_id"
    if key in SORTABLE_CORE:
        return f"e.{key}"
    raise ValidationFailed(f"Field '{key}' cannot be used here")


def build_where(conn, principal: Principal, q: ListQuery, fm: dict[str, FieldDef]) -> tuple[str, list]:
    where = [VIEWS.get(q.view, VIEWS["active"])]
    params: list = []
    if principal.locations is not None:
        where.append("e.location IN (%s)" % ",".join("?" * len(principal.locations)))
        params += principal.locations
    if q.search:
        s = f"%{q.search.strip()}%"
        clauses = ["e.full_name LIKE ?", "e.rhc_id LIKE ?"]
        params += [s, s]
        if principal.field_visible("official_email"):
            clauses.append("e.official_email LIKE ?")
            params.append(s)
        where.append("(" + " OR ".join(clauses) + ")")
    for key in ("location", "employment_status", "assignment_status"):
        val = getattr(q, key)
        if val:
            where.append(f"e.{key} = ?")
            params.append(val)
    if q.department:
        where.append("e.department LIKE ?")
        params.append(f"%{q.department}%")
    if q.has_photo == "yes":
        where.append("e.current_photo_id IS NOT NULL")
    elif q.has_photo == "no":
        where.append("e.current_photo_id IS NULL")
    for flt in q.filters or []:
        key, op, value = flt.get("field"), flt.get("op", "contains"), flt.get("value")
        if op not in FILTER_OPS or not principal.field_visible(key):
            raise ValidationFailed(f"Filter on '{key}' is not allowed")
        if key == "date_of_birth" and principal.dob_access != "full":
            raise ValidationFailed("Filtering by full date of birth is not permitted for your account")
        col = _column_expr(key, fm)
        if key == "bench_days":
            # Filter on computed bench days via bench start date arithmetic.
            col = "CAST(julianday(?) - julianday(e.bench_start) AS INTEGER)"
            params.append(timeutil.today().isoformat())
            where.append("e.assignment_status='Bench'")
        if op == "empty":
            where.append(f"({col} IS NULL OR {col} = '')")
        elif op == "not_empty":
            where.append(f"({col} IS NOT NULL AND {col} != '')")
        elif op == "contains":
            where.append(f"{col} LIKE ?")
            params.append(f"%{value}%")
        elif op == "equals":
            where.append(f"LOWER(CAST({col} AS TEXT)) = LOWER(?)")
            params.append(str(value))
        elif op == "not_equals":
            where.append(f"({col} IS NULL OR LOWER(CAST({col} AS TEXT)) != LOWER(?))")
            params.append(str(value))
        elif op in ("before", "lt"):
            where.append(f"{col} < ?")
            params.append(_filter_value(value, fm[key]))
        elif op in ("after", "gt"):
            where.append(f"{col} > ?")
            params.append(_filter_value(value, fm[key]))
    return " AND ".join(where), params


def _filter_value(value, f: FieldDef):
    if f.type == "date":
        try:
            return timeutil.parse_date(value).isoformat()
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from None
    if f.type in ("number", "computed", "int"):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValidationFailed(f"'{value}' is not a number") from None
    return value


def list_employees(conn, principal: Principal, q: ListQuery, paginate: bool = True) -> dict:
    fdefs = fields.all_fields(conn)
    fm = {f.key: f for f in fdefs}
    where, params = build_where(conn, principal, q, fm)
    sort_key = q.sort if q.sort in fm and principal.field_visible(q.sort) else "full_name"
    if sort_key == "date_of_birth" and principal.dob_access == "day_month":
        order_col = "substr(e.date_of_birth, 6, 5)"
    else:
        order_col = _column_expr(sort_key, fm)
    direction = "DESC" if q.direction.lower() == "desc" else "ASC"
    if sort_key == "bench_days":  # more days == earlier start
        direction = "ASC" if direction == "DESC" else "DESC"
    order = f"({order_col} IS NULL) ASC, {order_col} COLLATE NOCASE {direction}, e.id ASC"
    total = db.scalar(conn, f"SELECT COUNT(*) FROM employees e WHERE {where}", params)
    sql = f"SELECT e.* FROM employees e WHERE {where} ORDER BY {order}"
    page_size = max(1, min(int(q.page_size or 50), 1000))
    page = max(1, int(q.page or 1))
    if paginate:
        sql += " LIMIT ? OFFSET ?"
        params = params + [page_size, (page - 1) * page_size]
    items = [serialize(r, principal, fdefs) for r in db.rows(conn, sql, params)]
    return {"total": total, "page": page, "page_size": page_size, "items": items}


def get_employee(conn, principal: Principal, emp_id: int) -> dict:
    emp = raw_employee(conn, emp_id)
    if not emp or not principal.location_ok(emp["location"]):
        raise NotFound("Employee not found or outside your location scope")
    return emp


def profile(conn, principal: Principal, emp_id: int) -> dict:
    emp = get_employee(conn, principal, emp_id)
    fdefs = fields.all_fields(conn)
    data = serialize(emp, principal, fdefs)
    out = {"employee": data}
    out["history"] = db.rows(conn, "SELECT * FROM employee_history WHERE employee_id=? ORDER BY id DESC", (emp_id,))
    for h in out["history"]:
        h["details"] = db.jload(h["details"], {})
    changes = db.rows(conn, "SELECT * FROM field_changes WHERE employee_id=? ORDER BY id DESC LIMIT 300", (emp_id,))
    visible = []
    for c in changes:
        if not principal.field_visible(c["field"]):
            continue
        if c["field"] == "date_of_birth" and principal.dob_access != "full":
            c["old_value"] = fields.mask_dob(c["old_value"], principal.dob_access)
            c["new_value"] = fields.mask_dob(c["new_value"], principal.dob_access)
        visible.append(c)
    out["changes"] = visible
    if principal.can("photos.view"):
        out["photos"] = db.rows(conn, "SELECT id, original_filename, width, height, format, file_size, source, "
                                      "is_current, uploaded_at, uploaded_by, replaced_at FROM photos "
                                      "WHERE employee_id=? ORDER BY id DESC", (emp_id,))
    return out


# ------------------------------------------------------------------ writing

def clean_changes(conn, principal: Principal | None, changes: dict, fm: dict[str, FieldDef],
                  locations: list[str], day_first: bool, for_import: bool = False) -> tuple[dict, dict]:
    cleaned, errors = {}, {}
    for key, value in changes.items():
        f = fm.get(key)
        if f is None:
            errors[key] = f"Unknown field '{key}'"
            continue
        if not f.editable or (for_import and not f.importable):
            errors[key] = f"{f.label} cannot be changed"
            continue
        if principal is not None and not principal.field_editable(key):
            errors[key] = f"You do not have permission to change {f.label}"
            continue
        try:
            cleaned[key] = fields.normalize_value(f, value, locations, day_first)
        except FieldError as exc:
            errors[key] = str(exc)
    return cleaned, errors


def check_unique(conn, emp_id: int | None, merged: dict, errors: dict) -> None:
    if merged.get("rhc_id"):
        other = db.one(conn, "SELECT id, full_name FROM employees WHERE rhc_id=? AND id IS NOT ?",
                       (merged["rhc_id"], emp_id))
        if other:
            errors["rhc_id"] = f"RHC ID {merged['rhc_id']} is already used by {other['full_name']} (ID {other['id']})"
    if merged.get("official_email"):
        other = db.one(conn, "SELECT id, full_name FROM employees WHERE official_email=? AND id IS NOT ?",
                       (merged["official_email"], emp_id))
        if other:
            errors["official_email"] = (f"Email {merged['official_email']} is already used by "
                                        f"{other['full_name']} (ID {other['id']})")


def relevant_cross_errors(merged: dict, changed: set) -> dict:
    related = {
        "assignment_end": {"assignment_start", "assignment_end"},
        "bench_end": {"bench_start", "bench_end"},
        "last_working_date": {"date_of_joining", "last_working_date"},
        "date_of_birth": {"date_of_birth", "date_of_joining"},
        "full_name": {"full_name"},
        "rhc_id": {"rhc_id", "record_type"},
    }
    errs = fields.cross_validate(merged)
    return {k: v for k, v in errs.items() if related.get(k, {k}) & changed}


def split_custom(values: dict) -> tuple[dict, dict]:
    core = {k: v for k, v in values.items() if not k.startswith("cf_")}
    custom = {k[3:]: v for k, v in values.items() if k.startswith("cf_")}
    return core, custom


def write_changes(conn, emp: dict, cleaned: dict, actor: str, source: str, batch_id: int | None = None,
                  expected_version: int | None = None, extra_columns: dict | None = None) -> tuple[dict, list[int]]:
    """Apply already-validated values. Must run inside a transaction.

    Records field-level history, assignment/employment history, and bumps the
    record version. Raises Conflict if expected_version no longer matches.
    """
    before = flat_values(emp)
    diffs = {k: v for k, v in cleaned.items() if to_text(before.get(k)) != to_text(v)}
    extra_columns = dict(extra_columns or {})
    extra_diffs = {k: v for k, v in extra_columns.items() if to_text(emp.get(k)) != to_text(v)}
    if not diffs and not extra_diffs:
        return emp, []
    core, custom = split_custom(diffs)
    sets, params = [], []
    for k, v in {**core, **extra_diffs}.items():
        sets.append(f"{k}=?")
        params.append(v)
    if custom:
        merged_custom = db.jload(emp.get("custom"), {}) or {}
        for k, v in custom.items():
            if v is None:
                merged_custom.pop(k, None)
            else:
                merged_custom[k] = v
        sets.append("custom=?")
        params.append(db.jdump(merged_custom))
    now = timeutil.iso_utc()
    sets += ["version=version+1", "updated_at=?", "updated_by=?"]
    params += [now, actor]
    version = emp["version"] if expected_version is None else expected_version
    cur = conn.execute(f"UPDATE employees SET {', '.join(sets)} WHERE id=? AND version=?",
                       params + [emp["id"], version])
    if cur.rowcount == 0:
        raise Conflict("This record was changed by someone else. Review the latest values and try again.",
                       {"current_version": db.scalar(conn, "SELECT version FROM employees WHERE id=?", (emp["id"],))})
    change_ids = []
    for k, v in {**diffs, **extra_diffs}.items():
        old = before.get(k) if k in diffs else emp.get(k)
        c = conn.execute(
            "INSERT INTO field_changes(employee_id, field, old_value, new_value, changed_at, changed_by, source, "
            "import_batch_id) VALUES(?,?,?,?,?,?,?,?)",
            (emp["id"], k, to_text(old), to_text(v), now, actor, source, batch_id))
        change_ids.append(c.lastrowid)
    _record_history(conn, emp, diffs, actor, source, now)
    return raw_employee(conn, emp["id"]), change_ids


def _record_history(conn, old: dict, diffs: dict, actor: str, source: str, now: str) -> None:
    if any(k in diffs for k in fields.ASSIGNMENT_FIELDS):
        had = old.get("assignment_status") not in (None, "Unknown") or any(
            old.get(k) for k in ("client_name", "assignment_start", "assignment_end", "bench_start", "bench_end"))
        if had:
            status = old.get("assignment_status")
            start = old.get("bench_start") if status == "Bench" else old.get("assignment_start")
            end = old.get("bench_end") if status == "Bench" else old.get("assignment_end")
            conn.execute(
                "INSERT INTO employee_history(employee_id, kind, status, client_name, start_date, end_date, details, "
                "recorded_at, recorded_by, source) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (old["id"], "bench" if status == "Bench" else "assignment", status, old.get("client_name"), start, end,
                 db.jdump({k: old.get(k) for k in fields.ASSIGNMENT_FIELDS}), now, actor, source))
    if "employment_status" in diffs:
        conn.execute(
            "INSERT INTO employee_history(employee_id, kind, status, start_date, end_date, details, recorded_at, "
            "recorded_by, source) VALUES(?,?,?,?,?,?,?,?,?)",
            (old["id"], "employment", old.get("employment_status"), old.get("date_of_joining"),
             old.get("last_working_date"),
             db.jdump({"from": old.get("employment_status"), "to": diffs["employment_status"]}), now, actor, source))


def create_employee(conn, principal: Principal, data: dict, source: str = "form") -> dict:
    if not principal.can("employees.edit"):
        raise Forbidden("You do not have permission to add employees")
    fm = fields.field_map(conn)
    locations = settings_store.locations(conn)
    cleaned, errors = clean_changes(conn, principal, data, fm, locations,
                                    settings_store.get(conn, "date_day_first"))
    cleaned.setdefault("employment_status", "Active")
    cleaned["employment_status"] = cleaned["employment_status"] or "Active"
    cleaned["assignment_status"] = cleaned.get("assignment_status") or "Unknown"
    merged = {**cleaned, "record_type": "directory"}
    errors.update(fields.cross_validate(merged))
    check_unique(conn, None, merged, errors)
    if not principal.location_ok(cleaned.get("location")):
        errors["location"] = "You can only add employees in your permitted locations"
    if errors:
        raise ValidationFailed("Please correct the highlighted fields.", {"fields": errors})
    with db.transaction(conn):
        emp_id = insert_employee(conn, cleaned, principal.label, "directory")
        audit.log(conn, principal.label, "employee.create", "employee", emp_id,
                  f"Added {cleaned.get('full_name')} ({cleaned.get('rhc_id')})", {"source": source})
    return raw_employee(conn, emp_id)


def insert_employee(conn, cleaned: dict, actor: str, record_type: str) -> int:
    core, custom = split_custom(cleaned)
    now = timeutil.iso_utc()
    core.setdefault("employment_status", "Active")
    core["employment_status"] = core.get("employment_status") or "Active"
    core["assignment_status"] = core.get("assignment_status") or "Unknown"
    cols = [k for k in fields.WRITABLE_CORE if k in core]
    values = [core[k] for k in cols]
    cols += ["record_type", "custom", "created_at", "created_by", "updated_at", "updated_by"]
    values += [record_type, db.jdump({k: v for k, v in custom.items() if v is not None}), now, actor, now, actor]
    cur = conn.execute(f"INSERT INTO employees({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})", values)
    return cur.lastrowid


def update_employee(conn, principal: Principal, emp_id: int, changes: dict, expected_version: int,
                    source: str = "edit") -> dict:
    if not principal.can("employees.edit"):
        raise Forbidden("You do not have permission to edit employees")
    fm = fields.field_map(conn)
    locations = settings_store.locations(conn)
    with db.transaction(conn):
        emp = get_employee(conn, principal, emp_id)
        if emp["version"] != expected_version:
            raise Conflict("This record was changed by someone else since you opened it.",
                           {"current": serialize(emp, principal, fields.all_fields(conn))})
        if "employment_status" in changes and changes.get("employment_status") == "Left" \
                and not principal.can("employees.archive"):
            raise Forbidden("You do not have permission to archive employees")
        cleaned, errors = clean_changes(conn, principal, changes, fm, locations,
                                        settings_store.get(conn, "date_day_first"))
        merged = {**flat_values(emp), **cleaned}
        if "rhc_id" in cleaned and not cleaned["rhc_id"] and emp["record_type"] == "directory":
            errors["rhc_id"] = "RHC ID is required"
        errors.update(relevant_cross_errors(merged, set(cleaned)))
        check_unique(conn, emp_id, {k: cleaned.get(k) for k in ("rhc_id", "official_email")}, errors)
        if "location" in cleaned and not principal.location_ok(cleaned["location"]):
            errors["location"] = "You cannot move an employee outside your permitted locations"
        if errors:
            raise ValidationFailed("Please correct the highlighted fields.", {"fields": errors})
        updated, change_ids = write_changes(conn, emp, cleaned, principal.label, source,
                                            expected_version=expected_version)
        if change_ids:
            audit.log(conn, principal.label, "employee.update", "employee", emp_id,
                      f"Updated {updated['full_name']}: {', '.join(sorted(k for k in cleaned if to_text(flat_values(emp).get(k)) != to_text(cleaned[k])))}",
                      {"source": source})
    return updated


def archive_employee(conn, principal: Principal, emp_id: int, expected_version: int,
                     last_working_date: str | None) -> dict:
    if not principal.can("employees.archive"):
        raise Forbidden("You do not have permission to archive employees")
    changes = {"employment_status": "Left"}
    if last_working_date:
        changes["last_working_date"] = last_working_date
    return update_employee(conn, principal, emp_id, changes, expected_version, source="archive")


def restore_employee(conn, principal: Principal, emp_id: int, expected_version: int) -> dict:
    return update_employee(conn, principal, emp_id, {"employment_status": "Active"}, expected_version,
                           source="reactivate")


# ------------------------------------------------------------ bulk paste

def bulk_validate(conn, principal: Principal, rows: list[dict]) -> dict:
    """Validate multirow paste: rows = [{id, version, changes: {field: value}}]."""
    if not principal.can("employees.edit"):
        raise Forbidden("You do not have permission to edit employees")
    fm = fields.field_map(conn)
    fdefs = list(fm.values())
    locations = settings_store.locations(conn)
    day_first = settings_store.get(conn, "date_day_first")
    results, blocking = [], 0
    seen_rhc: dict[str, int] = {}
    for r in rows:
        emp = raw_employee(conn, int(r["id"]))
        item = {"id": r["id"], "row": r.get("row"), "changes": [], "errors": {}, "version": r.get("version")}
        if not emp or not principal.location_ok(emp["location"]):
            item["errors"]["_row"] = "Employee not found or outside your scope"
            results.append(item)
            blocking += 1
            continue
        item["name"] = emp["full_name"]
        if r.get("version") is not None and emp["version"] != int(r["version"]):
            item["errors"]["_row"] = "This record changed since the page was loaded; refresh and paste again"
        cleaned, errors = clean_changes(conn, principal, r.get("changes", {}), fm, locations, day_first)
        if "employment_status" in cleaned and cleaned["employment_status"] == "Left" and \
                not principal.can("employees.archive"):
            errors["employment_status"] = "You do not have permission to archive employees"
        before = flat_values(emp)
        merged = {**before, **cleaned}
        errors.update(relevant_cross_errors(merged, set(cleaned)))
        check_unique(conn, emp["id"], {k: cleaned.get(k) for k in ("rhc_id", "official_email")}, errors)
        if cleaned.get("rhc_id"):
            if cleaned["rhc_id"] in seen_rhc:
                errors["rhc_id"] = f"RHC ID {cleaned['rhc_id']} is pasted twice"
            seen_rhc[cleaned["rhc_id"]] = emp["id"]
        if "location" in cleaned and not principal.location_ok(cleaned["location"]):
            errors["location"] = "Outside your permitted locations"
        item["errors"].update(errors)
        for k, v in cleaned.items():
            if to_text(before.get(k)) != to_text(v):
                old = before.get(k)
                if k == "date_of_birth":
                    old = fields.mask_dob(old, principal.dob_access)
                item["changes"].append({"field": k, "label": fm[k].label, "old": old, "new": v})
        item["cleaned"] = cleaned
        if item["errors"]:
            blocking += 1
        results.append(item)
    _ = fdefs
    return {"rows": results, "blocking": blocking,
            "change_count": sum(len(r["changes"]) for r in results)}


def bulk_apply(conn, principal: Principal, rows: list[dict]) -> dict:
    """Validate again and apply all rows atomically; any error aborts everything."""
    check = bulk_validate(conn, principal, rows)
    if check["blocking"]:
        raise ValidationFailed("Some pasted rows have errors. Nothing was changed.", check)
    updated = 0
    with db.transaction(conn):
        for item in check["rows"]:
            if not item["changes"]:
                continue
            emp = raw_employee(conn, int(item["id"]))
            _, ids = write_changes(conn, emp, item["cleaned"], principal.label, "paste",
                                   expected_version=int(item["version"]) if item["version"] is not None else None)
            updated += 1 if ids else 0
        audit.log(conn, principal.label, "employee.bulk_paste", "employee", None,
                  f"Pasted changes into {updated} employee records", {"count": updated})
    return {"updated": updated, "change_count": check["change_count"]}
