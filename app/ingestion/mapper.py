"""Match an uploaded file's columns to the canonical sales format.

Order of evidence: a saved/built-in format profile, then the alias list, then
fuzzy matching. Everything left over becomes a custom column. The user confirms
the suggestion before anything is loaded.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from rapidfuzz import fuzz

from .fields import DERIVED_COLUMNS, FIELDS, FIELDS_BY_NAME, SALES_COLUMNS

FUZZY_THRESHOLD = 88
DATE_ORDERS = ("auto", "dayfirst", "monthfirst")
DISCOUNT_TYPES = ("auto", "percent", "amount")

# Names a custom column may not take because the sales table already uses them.
RESERVED_NAMES = set(SALES_COLUMNS) | set(DERIVED_COLUMNS) | {"sale_id", "row_hash"}


class MappingError(ValueError):
    pass


@dataclass
class Mapping:
    fields: dict[str, str] = field(default_factory=dict)  # canonical field -> source column
    custom: dict[str, str] = field(default_factory=dict)  # source column -> custom column name
    ignore: list[str] = field(default_factory=list)  # source columns to drop
    date_order: str = "auto"
    discount_type: str = "auto"
    descriptions: dict[str, str] = field(default_factory=dict)  # custom column -> description

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Mapping:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class FieldMatch:
    field: str
    source: str
    method: str  # "profile" | "alias" | "fuzzy"
    score: float


@dataclass
class Profile:
    name: str
    mapping: Mapping
    builtin: bool = False


@dataclass
class Suggestion:
    mapping: Mapping
    matches: list[FieldMatch]
    profile: str | None
    missing_required: list[str]
    locked_features: dict[str, list[str]]  # missing optional column -> features it would unlock


def custom_column_name(source: str) -> str:
    name = re.sub(r"[^0-9a-z_]", "_", source.lower()).strip("_") or "column"
    if name[0].isdigit():
        name = f"c_{name}"
    return f"{name}_custom" if name in RESERVED_NAMES else name


def missing_required(fields: dict[str, str]) -> list[str]:
    missing = [f for f in ("order_date", "product") if f not in fields]
    if "unit_price" in fields:
        if "quantity" not in fields:
            missing.append("quantity")
    elif "amount" not in fields:
        missing.append("unit_price (or a line total / amount)")
    return missing


def locked_features(fields: dict[str, str]) -> dict[str, list[str]]:
    has_cost = any(f in fields for f in ("unit_cost", "line_cost", "line_profit"))
    locked = {}
    for f in FIELDS:
        if f.kind != "optional" or f.name in fields:
            continue
        if f.name == "unit_cost" and has_cost:
            continue
        locked[f.name] = list(f.unlocks)
    return locked


def match_profile(columns: list[str], profiles: list[Profile]) -> Profile | None:
    """The profile with the most mapped columns whose field sources all exist in the file."""
    present = set(columns)
    fits = [p for p in profiles if p.mapping.fields and set(p.mapping.fields.values()) <= present]
    # Prefer more specific profiles, then user-saved over built-in.
    return max(fits, key=lambda p: (len(p.mapping.fields), not p.builtin), default=None)


def suggest_mapping(
    columns: list[str],
    profiles: list[Profile] | None = None,
    original_names: dict[str, str] | None = None,
) -> Suggestion:
    """Suggest a mapping for normalized ``columns`` (``original_names`` gives the
    headers as written, used for hints that normalization removes, like '%')."""
    fields: dict[str, str] = {}
    matches: list[FieldMatch] = []
    mapping = Mapping()
    available = list(columns)

    profile = match_profile(columns, profiles or [])
    if profile:
        for fname, source in profile.mapping.fields.items():
            fields[fname] = source
            matches.append(FieldMatch(fname, source, "profile", 100.0))
            available.remove(source)
        mapping.date_order = profile.mapping.date_order
        mapping.discount_type = profile.mapping.discount_type
        mapping.ignore = [c for c in profile.mapping.ignore if c in columns]
        available = [c for c in available if c not in mapping.ignore]

    for f in FIELDS:
        if f.name in fields:
            continue
        source = next((a for a in f.aliases if a in available), None)
        if source:
            fields[f.name] = source
            matches.append(FieldMatch(f.name, source, "alias", 100.0))
            available.remove(source)

    for f in FIELDS:
        if f.name in fields or not available:
            continue
        score, source = max(
            (max(fuzz.ratio(col, alias) for alias in f.aliases), col) for col in available
        )
        if score >= FUZZY_THRESHOLD:
            fields[f.name] = source
            matches.append(FieldMatch(f.name, source, "fuzzy", round(score, 1)))
            available.remove(source)

    # Helper inputs are only needed when the real column is missing.
    redundant = []
    if "unit_price" in fields:
        redundant.append("amount")
    if "unit_cost" in fields:
        redundant += ["line_cost", "line_profit"]
    elif "line_cost" in fields:
        redundant.append("line_profit")
    for fname in redundant:
        if fname in fields:
            available.append(fields.pop(fname))
            matches = [m for m in matches if m.field != fname]

    mapping.fields = fields
    if "discount" in fields and mapping.discount_type == "auto":
        header = (original_names or {}).get(fields["discount"], fields["discount"]).lower()
        if "%" in header or re.search(r"pct|percent", header):
            mapping.discount_type = "percent"
    saved_custom = profile.mapping.custom if profile else {}
    mapping.custom = {c: saved_custom.get(c) or custom_column_name(c) for c in columns if c in available}
    if profile:
        mapping.descriptions = {
            name: desc for name, desc in profile.mapping.descriptions.items()
            if name in mapping.custom.values()
        }

    order = {f.name: i for i, f in enumerate(FIELDS)}
    matches.sort(key=lambda m: order[m.field])
    return Suggestion(
        mapping=mapping,
        matches=matches,
        profile=profile.name if profile else None,
        missing_required=missing_required(fields),
        locked_features=locked_features(fields),
    )


def validate_mapping(mapping: Mapping, columns: list[str]) -> None:
    """Raise MappingError if the mapping can't be applied to a file with ``columns``."""
    problems = []
    unknown_fields = set(mapping.fields) - set(FIELDS_BY_NAME)
    if unknown_fields:
        problems.append(f"unknown fields: {', '.join(sorted(unknown_fields))}")
    sources = list(mapping.fields.values()) + list(mapping.custom)
    missing_sources = [s for s in sources if s not in columns]
    if missing_sources:
        problems.append(f"columns not in the file: {', '.join(missing_sources)}")
    used_twice = {s for s in sources if sources.count(s) > 1}
    if used_twice:
        problems.append(f"columns mapped more than once: {', '.join(sorted(used_twice))}")
    names = list(mapping.custom.values())
    repeated = {n for n in names if names.count(n) > 1}
    if repeated:
        problems.append(f"custom column names used more than once: {', '.join(sorted(repeated))}")
    bad_names = [n for n in mapping.custom.values()
                 if n in RESERVED_NAMES or not re.fullmatch(r"[a-z][a-z0-9_]*", n)]
    if bad_names:
        problems.append(f"invalid custom column names: {', '.join(bad_names)}")
    if mapping.date_order not in DATE_ORDERS:
        problems.append(f"date_order must be one of {DATE_ORDERS}")
    if mapping.discount_type not in DISCOUNT_TYPES:
        problems.append(f"discount_type must be one of {DISCOUNT_TYPES}")
    missing = missing_required(mapping.fields)
    if missing:
        problems.append(f"required columns not mapped: {', '.join(missing)}")
    if problems:
        raise MappingError("; ".join(problems))
