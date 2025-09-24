from __future__ import annotations

import random
import sys
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.db import Base
from app.models import CategoryRequirement, Student, StudentGroup, Task
from app.services.exam_service import ExamGenerationError, generate_exam_for_group
from app.services.student_import_service import import_students_from_excel


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _seed_group(session: Session) -> StudentGroup:
    group = StudentGroup(label="Team Alpha")
    student = Student(full_name="Max Mustermann", group=group)
    session.add_all([group, student])
    session.commit()
    return group


def test_generate_exam_respects_dependencies(session: Session) -> None:
    group = _seed_group(session)
    session.add_all(
        [
            CategoryRequirement(category="Analysis", required_count=1),
            CategoryRequirement(category="Algebra", required_count=1),
        ]
    )

    analysis_task = Task(
        title="Analysis 1",
        category="Analysis",
        subcategory="Differential",
        statement_markdown="**Aufgabe**",
    )
    algebra_task = Task(
        title="Algebra 1",
        category="Algebra",
        subcategory="Gruppen",
        statement_markdown="Beweise XYZ",
    )
    algebra_task.dependencies.append(analysis_task)

    session.add_all([analysis_task, algebra_task])
    session.commit()

    exam = generate_exam_for_group(session, group.id, rng=random.Random(42))
    session.commit()

    assert len(exam.assignments) == 2
    categories = {assignment.category for assignment in exam.assignments}
    assert categories == {"Analysis", "Algebra"}

    algebra_assignment = next(assignment for assignment in exam.assignments if assignment.category == "Algebra")
    assert algebra_assignment.task.dependencies[0].id == analysis_task.id


def test_generate_exam_fails_when_insufficient_tasks(session: Session) -> None:
    group = _seed_group(session)
    session.add(CategoryRequirement(category="Analysis", required_count=1))
    session.commit()

    with pytest.raises(ExamGenerationError):
        generate_exam_for_group(session, group.id, rng=random.Random(1))


def test_import_students_from_excel(session: Session) -> None:
    data = pd.DataFrame(
        {
            "Name": ["Anna", "Bernd"],
            "Partner": ["Bernd", "Anna"],
        }
    )
    buffer = BytesIO()
    data.to_excel(buffer, index=False)
    buffer.seek(0)

    result = import_students_from_excel(session, buffer)
    session.commit()

    assert result.created_groups == 1
    assert result.created_students == 2
    groups = session.scalars(select(StudentGroup)).unique().all()
    assert len(groups) == 1
    assert sorted(student.full_name for student in groups[0].students) == ["Anna", "Bernd"]
