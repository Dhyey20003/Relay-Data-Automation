"""Employee field catalogue, validation, normalisation and permission-aware output."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from . import db, settings_store, timeutil

EMPLOYMENT_STATUSES = ["Active", "Left", "Pending Review"]
ASSIGNMENT_STATUSES = ["Client", "Bench", "Internal", "Unknown"]
RECORD_TYPES = ["directory", "historical"]
CUSTOM_TYPES = ["text", "number", "date", "dropdown", "yesno"]

EMPLOYMENT_SYNONYMS = {
    "active": "Active", "working": "Active", "current": "Active", "employed": "Active",
    "left": "Left", "inactive": "Left", "resigned": "Left", "terminated": "Left", "exited": "Left",
    "separated": "Left", "relieved": "Left", "former": "Left",
    "pending review": "Pending Review", "pending": "Pending Review", "review": "Pending Review",
}
ASSIGNMENT_SYNONYMS = {
    "client": "Client", "billable": "Client", "deployed": "Client", "project": "Client",
    "bench": "Bench", "on bench": "Bench", "unassigned": "Bench",
    "internal": "Internal", "non-billable": "Internal", "non billable": "Internal",
    "unknown": "Unknown", "": "Unknown",
}
LOCATION_SYNONYMS = {
    "ahmedabad": "Ahmedabad", "amd": "Ahmedabad", "ahd": "Ahmedabad", "ahmadabad": "Ahmedabad",
    "vadodara": "Vadodara", "baroda": "Vadodara", "vdr": "Vadodara", "brc": "Vadodara",
}
YES = {"yes", "y", "true", "1", "x"}
NO = {"no", "n", "false", "0"}
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
RHC_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


@dataclass
class FieldDef:
    key: str
    label: str
    type: str  # text, email, date, enum, number, yesno, dropdown, computed, readonly, int
    editable: bool = True
    importable: bool = True
    required: bool = False
    options: list = field(default_factory=list)
    custom: bool = False
    restricted: bool = False
    width: int = 140

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "editable": self.editable,
                "importable": self.importable, "required": self.required, "options": list(self.options),
                "custom": self.custom, "restricted": self.restricted, "width": self.width}


DATE_FIELDS = ["date_of_joining", "date_of_birth", "last_working_date", "assignment_start", "assignment_end",
               "bench_start", "bench_end"]
ASSIGNMENT_FIELDS = ["assignment_status", "client_name", "assignment_start", "assignment_end", "bench_start",
                     "bench_end"]
# Fields stored as columns on the employees table and writable by users/imports.
WRITABLE_CORE = ["rhc_id", "full_name", "designation", "department", "official_email", "location",
                 "date_of_joining", "date_of_birth", "employment_status", "last_working_date",
                 "assignment_status", "client_name", "assignment_start", "assignment_end", "bench_start",
                 "bench_end", "notes"]


def core_fields(locations: list[str]) -> list[FieldDef]:
    return [
        FieldDef("id", "Internal ID", "int", editable=False, importable=False, width=90),
        FieldDef("rhc_id", "RHC ID", "text", required=True, width=110),
        FieldDef("full_name", "Full name", "text", required=True, width=200),
        FieldDef("designation", "Designation", "text", width=180),
        FieldDef("department", "Department", "text", width=150),
        FieldDef("official_email", "Official email", "email", width=220),
        FieldDef("location", "Location", "enum", options=list(locations), width=120),
        FieldDef("date_of_joining", "Date of joining", "date", width=130),
        FieldDef("date_of_birth", "Date of birth", "date", restricted=True, width=130),
        FieldDef("employment_status", "Employment status", "enum", options=EMPLOYMENT_STATUSES, width=150),
        FieldDef("last_working_date", "Last working date", "date", width=140),
        FieldDef("assignment_status", "Assignment status", "enum", options=ASSIGNMENT_STATUSES, width=150),
        FieldDef("client_name", "Client name", "text", width=160),
        FieldDef("assignment_start", "Assignment start", "date", width=140),
        FieldDef("assignment_end", "Assignment end", "date", width=140),
        FieldDef("bench_start", "Bench start", "date", width=130),
        FieldDef("bench_end", "Bench end", "date", width=130),
        FieldDef("bench_days", "Current bench days", "computed", editable=False, importable=False, width=120),
        FieldDef("photo", "Photo", "photo", editable=False, importable=False, width=70),
        FieldDef("notes", "Notes", "text", width=220),
        FieldDef("record_type", "Record type", "readonly", editable=False, importable=False, width=110),
        FieldDef("created_by", "Created by", "readonly", editable=False, importable=False, width=160),
        FieldDef("created_at", "Created", "timestamp", editable=False, importable=False, width=150),
        FieldDef("updated_by", "Updated by", "readonly", editable=False, importable=False, width=160),
        FieldDef("updated_at", "Updated", "timestamp", editable=False, importable=False, width=150),
    ]


def custom_field_defs(conn, include_inactive: bool = False) -> list[FieldDef]:
    sql = "SELECT * FROM custom_fields" + ("" if include_inactive else " WHERE is_active=1") + " ORDER BY sort_order, id"
    out = []
    for r in db.rows(conn, sql):
        ftype = r["field_type"]
        out.append(FieldDef(key="cf_" + r["key"], label=r["label"], type=ftype, options=db.jload(r["options"], []),
                            custom=True, restricted=bool(r["is_restricted"]), width=150))
    return out


def all_fields(conn) -> list[FieldDef]:
    return core_fields(settings_store.locations(conn)) + custom_field_defs(conn)


def field_map(conn) -> dict[str, FieldDef]:
    return {f.key: f for f in all_fields(conn)}


def token_allowed_fields(conn, requested: list[str]) -> set[str]:
    """Integration tokens never receive restricted fields (such as date of birth)."""
    fm = field_map(conn)
    allowed = {"id", "rhc_id", "full_name"}
    for key in requested:
        f = fm.get(key)
        if f and not f.restricted and f.type != "photo":
            allowed.add(key)
    return allowed


# ------------------------------------------------------------- normalising

class FieldError(ValueError):
    pass


def normalize_rhc_id(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    s = str(value).strip().upper().replace(" ", "")
    if not s:
        return None
    if not RHC_ID_RE.match(s):
        raise FieldError(f"RHC ID '{value}' contains unsupported characters (use letters, digits, - _ .)")
    return s


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (dt.datetime, dt.date)):
        value = value.isoformat()[:10]
    s = str(value).strip()
    return s or None


def normalize_value(fdef: FieldDef, value, locations: list[str], day_first: bool = True):
    """Convert user/spreadsheet input to the stored representation.

    Returns None for blank input. Raises FieldError on invalid values.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    t = fdef.type
    if fdef.key == "rhc_id":
        return normalize_rhc_id(value)
    if t == "date":
        try:
            return timeutil.parse_date(value, day_first).isoformat()
        except ValueError as exc:
            raise FieldError(str(exc)) from None
    if t == "email":
        s = _text(value).lower()
        if not EMAIL_RE.match(s):
            raise FieldError(f"'{s}' is not a valid email address")
        return s
    if fdef.key == "location":
        s = _text(value)
        for loc in locations:
            if loc.lower() == s.lower():
                return loc
        mapped = LOCATION_SYNONYMS.get(s.lower())
        if mapped and mapped in locations:
            return mapped
        raise FieldError(f"Location '{s}' is not one of: {', '.join(locations)}")
    if fdef.key == "employment_status":
        s = _text(value)
        mapped = EMPLOYMENT_SYNONYMS.get(s.lower())
        if not mapped:
            raise FieldError(f"Employment status '{s}' must be Active, Left or Pending Review")
        return mapped
    if fdef.key == "assignment_status":
        s = _text(value)
        mapped = ASSIGNMENT_SYNONYMS.get(s.lower())
        if not mapped:
            raise FieldError(f"Assignment status '{s}' must be Client, Bench, Internal or Unknown")
        return mapped
    if t == "number":
        if isinstance(value, bool):
            raise FieldError("Expected a number")
        if isinstance(value, (int, float)):
            return float(value) if not float(value).is_integer() else int(value)
        s = _text(value).replace(",", "")
        try:
            n = float(s)
        except ValueError:
            raise FieldError(f"'{value}' is not a number") from None
        return int(n) if n.is_integer() else n
    if t == "yesno":
        if isinstance(value, bool):
            return "Yes" if value else "No"
        s = _text(value).lower()
        if s in YES:
            return "Yes"
        if s in NO:
            return "No"
        raise FieldError(f"'{value}' must be Yes or No")
    if t == "dropdown":
        s = _text(value)
        for opt in fdef.options:
            if str(opt).lower() == s.lower():
                return opt
        raise FieldError(f"'{s}' is not one of the allowed options: {', '.join(map(str, fdef.options))}")
    return _text(value)


def cross_validate(record: dict) -> dict[str, str]:
    """Date-range and consistency checks on a complete (merged) record."""
    errors: dict[str, str] = {}

    def d(key):
        v = record.get(key)
        return dt.date.fromisoformat(v) if v else None

    pairs = [("assignment_start", "assignment_end", "Assignment end date is before the assignment start date"),
             ("bench_start", "bench_end", "Bench end date is before the bench start date"),
             ("date_of_joining", "last_working_date", "Last working date is before the date of joining")]
    for a, b, msg in pairs:
        if d(a) and d(b) and d(b) < d(a):
            errors[b] = msg
    dob, doj = d("date_of_birth"), d("date_of_joining")
    if dob and dob > timeutil.today():
        errors["date_of_birth"] = "Date of birth is in the future"
    if dob and doj and doj <= dob:
        errors["date_of_birth"] = "Date of birth must be before the date of joining"
    if not record.get("full_name"):
        errors["full_name"] = "Full name is required"
    if record.get("record_type", "directory") == "directory" and not record.get("rhc_id"):
        errors["rhc_id"] = "RHC ID is required"
    return errors


def mask_dob(value: str | None, access: str):
    if not value:
        return value
    if access == "full":
        return value
    if access == "day_month":
        return "--" + value[5:10]
    return None
