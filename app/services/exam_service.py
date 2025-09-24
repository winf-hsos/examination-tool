from __future__ import annotations

import random
from typing import List, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from ..models import (
    CategoryRequirement,
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


def _collect_tasks_by_category(tasks: Sequence[Task]) -> dict[str, List[Task]]:
    tasks_by_category: dict[str, List[Task]] = {}
    for task in tasks:
        tasks_by_category.setdefault(task.category, []).append(task)
    return tasks_by_category


def _choose_tasks_for_requirements(
    requirements: Sequence[CategoryRequirement],
    tasks_by_category: dict[str, List[Task]],
    rng: random.Random,
) -> List[Task]:
    remaining = list(requirements)
    rng.shuffle(remaining)
    selected: List[Task] = []
    selected_ids: set[int] = set()

    progress = True
    while remaining and progress:
        progress = False
        for requirement in list(remaining):
            candidates = [
                task
                for task in tasks_by_category.get(requirement.category, [])
                if task.id not in selected_ids and _dependencies_satisfied(task, selected_ids)
            ]
            if len(candidates) < requirement.required_count:
                continue

            chosen = rng.sample(candidates, requirement.required_count)
            selected.extend(chosen)
            selected_ids.update(task.id for task in chosen)
            remaining.remove(requirement)
            progress = True

    if remaining:
        missing = ", ".join(req.category for req in remaining)
        raise ExamGenerationError(
            "Nicht genügend Aufgaben verfügbar, um die Anforderungen zu erfüllen (fehlende Kategorien:"
            f" {missing})."
        )

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


def generate_exam_for_group(db: Session, group_id: int, rng: random.Random | None = None) -> ExamSession:
    rng = rng or random.Random()

    group = db.get(StudentGroup, group_id)
    if not group:
        raise ValueError("Die ausgewählte Gruppe existiert nicht.")

    requirements = db.scalars(select(CategoryRequirement)).all()
    if not requirements:
        raise ExamGenerationError("Es wurden noch keine Kategorienanforderungen definiert.")

    tasks = (
        db.scalars(
            select(Task).options(
                joinedload(Task.dependencies),
                joinedload(Task.images),
            )
        )
        .unique()
        .all()
    )
    tasks_by_category = _collect_tasks_by_category(tasks)

    selected_tasks = _choose_tasks_for_requirements(requirements, tasks_by_category, rng)

    exam = ExamSession(group_id=group_id)
    db.add(exam)
    db.flush()

    _apply_tasks_to_exam(db, exam, selected_tasks)
    return exam


def regenerate_exam(db: Session, exam_id: int, rng: random.Random | None = None) -> ExamSession:
    rng = rng or random.Random()
    exam = db.get(ExamSession, exam_id)
    if not exam:
        raise ValueError("Die Prüfungssitzung wurde nicht gefunden.")

    requirements = db.scalars(select(CategoryRequirement)).all()
    tasks = (
        db.scalars(
            select(Task).options(
                joinedload(Task.dependencies),
                joinedload(Task.images),
            )
        )
        .unique()
        .all()
    )
    tasks_by_category = _collect_tasks_by_category(tasks)
    selected_tasks = _choose_tasks_for_requirements(requirements, tasks_by_category, rng)
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

    group_payload = {
        "id": exam.group.id,
        "label": exam.group.label,
        "students": [
            {"id": student.id, "full_name": student.full_name} for student in exam.group.students
        ],
    }

    return {
        "exam_id": exam.id,
        "group": group_payload,
        "tasks": tasks_payload,
        "started_at": exam.started_at,
    }
