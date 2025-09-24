from __future__ import annotations

import asyncio
import base64
import json
import random
import sys
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.datastructures import UploadFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))

from app.db import Base
from app.main import UPLOAD_DIR, export_tasks_data, import_tasks_data
from app.models import Category, CategoryRequirement, Student, StudentGroup, Task, TaskImage
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


def test_export_tasks_includes_categories_and_images(session: Session) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_path = UPLOAD_DIR / "test_export_image.png"
    image_bytes = b"test-image"
    image_path.write_bytes(image_bytes)

    category = Category(name="Analysis")
    subcategory = Category(name="Differential", parent=category)
    session.add_all([category, subcategory])
    session.add(CategoryRequirement(category="Analysis", required_count=2))

    prerequisite = Task(
        title="Vorkurs",
        category="Analysis",
        subcategory=None,
        statement_markdown="Vorbereitung",
    )
    task = Task(
        title="Analysis Hauptaufgabe",
        category="Analysis",
        subcategory="Differential",
        statement_markdown="**Inhalt**",
    )
    session.add_all([prerequisite, task])
    session.commit()

    task.dependencies.append(prerequisite)
    session.add(
        TaskImage(
            task_id=task.id,
            file_path=f"uploads/{image_path.name}",
            original_filename="grafik.png",
            mime_type="image/png",
            position=0,
        )
    )
    session.commit()

    response = export_tasks_data(db=session)
    payload = json.loads(response.body)

    assert payload["categories"]
    exported_category = payload["categories"][0]
    assert exported_category["name"] == "Analysis"
    assert exported_category["subcategories"][0]["name"] == "Differential"

    exported_tasks = {item["title"]: item for item in payload["tasks"]}
    assert "Analysis Hauptaufgabe" in exported_tasks
    main_task = exported_tasks["Analysis Hauptaufgabe"]
    assert main_task["dependencies"] == [prerequisite.id]
    assert main_task["images"]
    encoded_image = main_task["images"][0]["data"]
    assert base64.b64decode(encoded_image) == image_bytes

    image_path.unlink(missing_ok=True)


def test_import_tasks_creates_records(session: Session) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_bytes = b"import-image"
    payload = {
        "categories": [
            {
                "name": "Analysis",
                "required_count": 2,
                "subcategories": [{"name": "Differential"}],
            }
        ],
        "tasks": [
            {
                "id": 1,
                "title": "Analysis Aufgabe",
                "category": "Analysis",
                "subcategory": "Differential",
                "statement_markdown": "Aufgabe",
                "dependencies": [2],
                "images": [
                    {
                        "original_filename": "bild.png",
                        "mime_type": "image/png",
                        "position": 0,
                        "data": base64.b64encode(image_bytes).decode("utf-8"),
                    }
                ],
            },
            {
                "id": 2,
                "title": "Grundaufgabe",
                "category": "Analysis",
                "subcategory": None,
                "statement_markdown": "Vorbereitung",
            },
        ],
    }

    upload = UploadFile(
        filename="tasks.json",
        file=BytesIO(json.dumps(payload).encode("utf-8")),
    )

    response = asyncio.run(import_tasks_data(upload=upload, db=session))
    assert response.status_code == 303

    tasks = session.scalars(select(Task).order_by(Task.title)).unique().all()
    assert len(tasks) == 2
    main_task = next(task for task in tasks if task.title == "Analysis Aufgabe")
    dependency_task = next(task for task in tasks if task.title == "Grundaufgabe")
    assert dependency_task in main_task.dependencies

    requirement = session.scalars(select(CategoryRequirement)).one()
    assert requirement.required_count == 2

    stored_image = session.scalars(select(TaskImage)).first()
    assert stored_image is not None
    stored_file = (Path("app/static") / stored_image.file_path)
    assert stored_file.exists()
    assert stored_file.read_bytes() == image_bytes

    stored_file.unlink(missing_ok=True)
