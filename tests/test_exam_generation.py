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
from app.models import (
    Category,
    ExamConfiguration,
    ExamConfigurationRequirement,
    Student,
    StudentGroup,
    Task,
    TaskImage,
)
from app.services.exam_service import generate_exam
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


def test_generate_exam_respects_dependencies_and_difficulty(session: Session) -> None:
    group = _seed_group(session)

    analysis = Category(name="Analysis")
    algebra = Category(name="Algebra")
    session.add_all([analysis, algebra])
    session.flush()

    configuration = ExamConfiguration(name="Klausur A", target_difficulty=2.5)
    session.add(configuration)
    session.flush()
    session.add_all(
        [
            ExamConfigurationRequirement(
                configuration_id=configuration.id,
                category_id=analysis.id,
                question_count=1,
                position=0,
            ),
            ExamConfigurationRequirement(
                configuration_id=configuration.id,
                category_id=algebra.id,
                question_count=1,
                position=1,
            ),
        ]
    )

    analysis_task = Task(
        title="Ableitung",
        category=analysis.name,
        subcategory=None,
        difficulty=2,
        statement_markdown="**Aufgabe**",
    )
    algebra_task = Task(
        title="Gruppentheorie",
        category=algebra.name,
        subcategory=None,
        difficulty=3,
        statement_markdown="Beweise XYZ",
    )
    algebra_task.dependencies.append(analysis_task)

    session.add_all([analysis_task, algebra_task])
    session.commit()

    exam = generate_exam(
        session,
        configuration_id=configuration.id,
        group_id=group.id,
        rng=random.Random(42),
    )
    session.commit()

    assert len(exam.assignments) == 2
    difficulties = [assignment.task.difficulty for assignment in exam.assignments]
    assert abs(sum(difficulties) / len(difficulties) - configuration.target_difficulty) <= 0.5

    categories = {assignment.category for assignment in exam.assignments}
    assert categories == {analysis.name, algebra.name}

    algebra_assignment = next(
        assignment for assignment in exam.assignments if assignment.category == algebra.name
    )
    assert algebra_assignment.task.dependencies[0].id == analysis_task.id


def test_generate_exam_allows_demo_mode_without_group(session: Session) -> None:
    analysis = Category(name="Analysis")
    session.add(analysis)
    session.flush()

    configuration = ExamConfiguration(name="Demo", target_difficulty=2.0)
    session.add(configuration)
    session.flush()
    session.add(
        ExamConfigurationRequirement(
            configuration_id=configuration.id,
            category_id=analysis.id,
            question_count=1,
            position=0,
        )
    )

    task = Task(
        title="Grenzwert",
        category=analysis.name,
        subcategory=None,
        difficulty=2,
        statement_markdown="Berechne den Grenzwert.",
    )
    session.add(task)
    session.commit()

    exam = generate_exam(
        session,
        configuration_id=configuration.id,
        group_id=None,
        rng=random.Random(3),
        demo_label="Testlauf",
    )

    assert exam.group is None
    assert exam.demo_label == "Testlauf"
    assert exam.configuration_id == configuration.id


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
    image_path = UPLOAD_DIR / "test_export_image.jpg"
    thumb_path = UPLOAD_DIR / "test_export_image_thumb.jpg"
    image_bytes = b"test-image"
    thumb_bytes = b"thumb"
    image_path.write_bytes(image_bytes)
    thumb_path.write_bytes(thumb_bytes)

    category = Category(name="Analysis")
    subcategory = Category(name="Differential", parent=category)
    session.add_all([category, subcategory])
    session.flush()

    configuration = ExamConfiguration(name="Export", target_difficulty=2.0)
    session.add(configuration)
    session.flush()
    session.add(
        ExamConfigurationRequirement(
            configuration_id=configuration.id,
            category_id=category.id,
            question_count=1,
            position=0,
        )
    )

    prerequisite = Task(
        title="Vorkurs",
        category="Analysis",
        subcategory=None,
        difficulty=1,
        statement_markdown="Vorbereitung",
    )
    task = Task(
        title="Analysis Hauptaufgabe",
        category="Analysis",
        subcategory="Differential",
        difficulty=2,
        statement_markdown="**Inhalt**",
    )
    session.add_all([prerequisite, task])
    session.commit()

    task.dependencies.append(prerequisite)
    session.add(
        TaskImage(
            task_id=task.id,
            file_path=f"uploads/{image_path.name}",
            thumbnail_path=f"uploads/{thumb_path.name}",
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
    encoded_thumb = main_task["images"][0]["thumbnail"]
    assert base64.b64decode(encoded_image) == image_bytes
    assert base64.b64decode(encoded_thumb) == thumb_bytes

    exported_configurations = {item["name"]: item for item in payload["configurations"]}
    assert "Export" in exported_configurations
    config_payload = exported_configurations["Export"]
    assert config_payload["requirements"][0]["category"] == "Analysis"

    image_path.unlink(missing_ok=True)
    thumb_path.unlink(missing_ok=True)


def test_import_tasks_creates_records(session: Session) -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    image_bytes = b"import-image"
    payload = {
        "categories": [
            {"name": "Analysis", "subcategories": [{"name": "Differential"}]}
        ],
        "tasks": [
            {
                "id": 1,
                "title": "Analysis Aufgabe",
                "category": "Analysis",
                "subcategory": "Differential",
                "difficulty": 3,
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
                "title": "Vorkurs",
                "category": "Analysis",
                "subcategory": None,
                "difficulty": 1,
                "statement_markdown": "Vorbereitung",
                "dependencies": [],
            },
        ],
        "configurations": [
            {
                "name": "Importierte Prüfung",
                "target_difficulty": 2.0,
                "requirements": [
                    {
                        "category": "Analysis",
                        "subcategory": "Differential",
                        "question_count": 1,
                        "position": 0,
                    }
                ],
            }
        ],
    }

    buffer = BytesIO(json.dumps(payload).encode("utf-8"))
    upload = UploadFile(filename="import.json", file=buffer)
    response = asyncio.run(import_tasks_data(upload=upload, db=session))

    assert response.status_code == 303

    tasks = session.scalars(select(Task)).unique().all()
    assert len(tasks) == 2
    main_task = next(task for task in tasks if task.title == "Analysis Aufgabe")
    assert main_task.dependencies
    assert main_task.difficulty == 3
    assert main_task.images
    configuration = session.scalars(select(ExamConfiguration)).first()
    assert configuration and configuration.name == "Importierte Prüfung"
