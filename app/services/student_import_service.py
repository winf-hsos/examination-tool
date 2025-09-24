from __future__ import annotations

from dataclasses import dataclass
from typing import IO, Dict, List, Tuple

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Student, StudentGroup


@dataclass
class ImportResult:
    created_groups: int
    created_students: int
    skipped_rows: int
    errors: List[str]


REQUIRED_COLUMNS = {"name"}
OPTIONAL_COLUMNS = {"partner", "group"}


def _normalise_columns(columns: List[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for column in columns:
        normalised = column.strip().lower()
        mapping[normalised] = column
    return mapping


def _extract_value(row: pd.Series, mapping: Dict[str, str], key: str) -> str | None:
    column = mapping.get(key)
    if column is None:
        return None
    value = row.get(column)
    if isinstance(value, float) and pd.isna(value):
        return None
    if pd.isna(value):
        return None
    if value is None:
        return None
    return str(value).strip()


def import_students_from_excel(db: Session, file: IO[bytes]) -> ImportResult:
    frame = pd.read_excel(file)
    if frame.empty:
        return ImportResult(created_groups=0, created_students=0, skipped_rows=0, errors=["Die Datei ist leer."])

    column_map = _normalise_columns(list(frame.columns))
    missing_columns = REQUIRED_COLUMNS - set(column_map)
    if missing_columns:
        return ImportResult(
            created_groups=0,
            created_students=0,
            skipped_rows=len(frame),
            errors=[
                "Die Datei muss mindestens eine Spalte 'Name' enthalten."
            ],
        )

    existing_groups = {group.label: group for group in db.scalars(select(StudentGroup)).all()}
    created_groups = 0
    created_students = 0
    skipped_rows = 0
    errors: List[str] = []

    processed_group_keys: Dict[Tuple[str, ...], StudentGroup] = {}

    for index, row in frame.iterrows():
        primary_name = _extract_value(row, column_map, "name")
        if not primary_name:
            skipped_rows += 1
            errors.append(f"Zeile {index + 2}: Kein Name angegeben.")
            continue

        partner_name = _extract_value(row, column_map, "partner")
        explicit_group_label = _extract_value(row, column_map, "group")

        participants = [primary_name]
        if partner_name and partner_name != primary_name:
            participants.append(partner_name)

        group_key = tuple(sorted(participants))
        label = explicit_group_label or " & ".join(group_key)

        group = existing_groups.get(label) or processed_group_keys.get(group_key)
        if not group:
            group = StudentGroup(label=label)
            db.add(group)
            db.flush()
            existing_groups[label] = group
            processed_group_keys[group_key] = group
            created_groups += 1

        # ensure each participant is present
        for participant in group_key:
            existing_student = next((s for s in group.students if s.full_name == participant), None)
            if existing_student:
                continue
            group.students.append(Student(full_name=participant))
            created_students += 1

    db.flush()
    return ImportResult(
        created_groups=created_groups,
        created_students=created_students,
        skipped_rows=skipped_rows,
        errors=errors,
    )
