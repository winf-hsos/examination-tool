from __future__ import annotations

import random
from typing import List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    ExamConfiguration,
    ExamConfigurationRequirement,
    ExamSession,
    ExamTaskAssignment,
    StudentGroup,
    Task,
)
from .markdown_service import render_markdown


class ExamGenerationError(RuntimeError):
    """Raised when an exam could not be generated."""


def _dependencies_satisfied(task: Task, selected_ids: set[int]) -> bool:
    return all(dependency.id in selected_ids for dependency in task.dependencies)


def _requirement_key(requirement: ExamConfigurationRequirement) -> tuple[str, str | None]:
    category = requirement.category.name
    subcategory = requirement.subcategory.name if requirement.subcategory else None
    return category, subcategory


def _collect_tasks_by_requirement(tasks: Sequence[Task]) -> dict[tuple[str, str | None], List[Task]]:
    tasks_by_category: dict[tuple[str, str | None], List[Task]] = {}
    for task in tasks:
        key = (task.category, task.subcategory)
        tasks_by_category.setdefault(key, []).append(task)
        if task.subcategory:
            parent_key = (task.category, None)
            tasks_by_category.setdefault(parent_key, []).append(task)
    return tasks_by_category


def _choose_tasks_for_requirements(
    requirements: Sequence[ExamConfigurationRequirement],
    tasks_by_category: dict[tuple[str, str | None], List[Task]],
    rng: random.Random,
    target_difficulty: float,
) -> List[Task]:
    ordered_requirements = sorted(requirements, key=lambda item: item.position)
    selected: List[Task] = []
    selected_ids: set[int] = set()
    total_difficulty = 0.0

    for requirement in ordered_requirements:
        key = _requirement_key(requirement)
        available = [
            task
            for task in tasks_by_category.get(key, [])
            if task.id not in selected_ids
        ]
        if not available:
            raise ExamGenerationError(
                "Für die Kategorie {0} sind keine Aufgaben vorhanden.".format(
                    requirement.category.name
                )
            )

        for _ in range(requirement.question_count):
            candidates = [
                task
                for task in available
                if task.id not in selected_ids
                and _dependencies_satisfied(task, selected_ids)
            ]
            if not candidates:
                raise ExamGenerationError(
                    "Nicht genügend Aufgaben in Kategorie {0}, um die Prüfung zu bestücken.".format(
                        requirement.category.name
                    )
                )

            def _score(task: Task) -> float:
                new_total = total_difficulty + task.difficulty
                new_count = len(selected) + 1
                return abs(new_total / new_count - target_difficulty)

            candidates.sort(key=_score)
            top_candidates = candidates[: min(3, len(candidates))]
            chosen = rng.choice(top_candidates)
            selected.append(chosen)
            selected_ids.add(chosen.id)
            total_difficulty += chosen.difficulty

    return selected


def _apply_tasks_to_exam(db: Session, exam: ExamSession, tasks: Sequence[Task]) -> None:
    for assignment in list(exam.assignments):
        db.delete(assignment)
    db.flush()

    for position, task in enumerate(tasks, start=1):
        assignment = ExamTaskAssignment(
            exam_id=exam.id,
            task_id=task.id,
            position=position,
            category=task.category,
        )
        db.add(assignment)

    db.flush()
    db.refresh(exam)


def _resolve_group(db: Session, group_id: int | None) -> StudentGroup | None:
    if group_id is None:
        return None
    return db.get(StudentGroup, group_id)


def _load_tasks(db: Session) -> list[Task]:
    return (
        db.scalars(
            select(Task).options(
                joinedload(Task.dependencies),
                joinedload(Task.images),
            )
        )
        .unique()
        .all()
    )


def _get_configuration(db: Session, configuration_id: int) -> ExamConfiguration:
    configuration = db.get(ExamConfiguration, configuration_id)
    if not configuration:
        raise ValueError("Die ausgewählte Prüfungskonfiguration existiert nicht.")
    if not configuration.requirements:
        raise ExamGenerationError(
            "Die gewählte Prüfungskonfiguration enthält keine Kategorienzuordnungen."
        )
    return configuration


def generate_exam(
    db: Session,
    configuration_id: int,
    group_id: int | None = None,
    rng: random.Random | None = None,
    demo_label: str | None = None,
) -> ExamSession:
    rng = rng or random.Random()

    configuration = _get_configuration(db, configuration_id)
    group = _resolve_group(db, group_id)
    if group_id is not None and not group:
        raise ValueError("Die ausgewählte Gruppe existiert nicht.")

    tasks = _load_tasks(db)
    tasks_by_category = _collect_tasks_by_requirement(tasks)

    selected_tasks = _choose_tasks_for_requirements(
        configuration.requirements,
        tasks_by_category,
        rng,
        configuration.target_difficulty,
    )

    exam = ExamSession(
        group_id=group.id if group else None,
        configuration_id=configuration.id,
        demo_label=demo_label,
    )
    db.add(exam)
    db.flush()

    _apply_tasks_to_exam(db, exam, selected_tasks)
    return exam


def regenerate_exam(db: Session, exam_id: int, rng: random.Random | None = None) -> ExamSession:
    rng = rng or random.Random()
    exam = db.get(ExamSession, exam_id)
    if not exam:
        raise ValueError("Die Prüfungssitzung wurde nicht gefunden.")

    if not exam.configuration:
        raise ExamGenerationError("Die Prüfungssitzung ist keiner Konfiguration zugeordnet.")

    tasks = _load_tasks(db)
    tasks_by_category = _collect_tasks_by_requirement(tasks)
    selected_tasks = _choose_tasks_for_requirements(
        exam.configuration.requirements,
        tasks_by_category,
        rng,
        exam.configuration.target_difficulty,
    )
    _apply_tasks_to_exam(db, exam, selected_tasks)
    return exam


def build_exam_payload(exam: ExamSession, include_solutions: bool) -> dict:
    tasks_payload = []
    sorted_assignments = sorted(exam.assignments, key=lambda a: a.position)
    for assignment in sorted_assignments:
        task = assignment.task
        tasks_payload.append(
            {
                "task_id": task.id,
                "title": task.title,
                "category": task.category,
                "subcategory": task.subcategory,
                "statement_html": render_markdown(task.statement_markdown) or "",
                "hints_html": render_markdown(task.hints_markdown) if include_solutions else None,
                "solution_html": render_markdown(task.solution_markdown)
                if include_solutions
                else None,
                "position": assignment.position,
                "image_urls": [f"/static/{image.file_path}" for image in task.images],
            }
        )

    if exam.group:
        group_payload = {
            "id": exam.group.id,
            "label": exam.group.label,
            "students": [
                {"id": student.id, "full_name": student.full_name}
                for student in exam.group.students
            ],
        }
    else:
        group_payload = {
            "id": None,
            "label": exam.demo_label or "Testmodus",
            "students": [],
        }

    return {
        "exam_id": exam.id,
        "group": group_payload,
        "tasks": tasks_payload,
        "started_at": exam.started_at,
    }
