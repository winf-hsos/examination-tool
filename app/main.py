from __future__ import annotations

from io import BytesIO
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from .db import get_db, init_db
from .models import (
    CategoryRequirement,
    ExamSession,
    ExamTaskAssignment,
    StudentGroup,
    Task,
)
from .services.exam_service import (
    ExamGenerationError,
    build_exam_payload,
    generate_exam_for_group,
    regenerate_exam,
)
from .services.markdown_service import render_markdown
from .services.student_import_service import import_students_from_excel

app = FastAPI(title="Examination Tool", default_response_class=HTMLResponse)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.on_event("startup")
async def on_startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    groups = (
        db.scalars(select(StudentGroup).order_by(StudentGroup.label)).unique().all()
    )
    category_requirements = db.scalars(select(CategoryRequirement).order_by(CategoryRequirement.category)).all()
    tasks_count = db.scalar(select(func.count()).select_from(Task)) or 0
    latest_exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group).joinedload(StudentGroup.students))
        .order_by(ExamSession.started_at.desc())
        .limit(1)
    ).unique().first()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "groups": groups,
            "category_requirements": category_requirements,
            "tasks_count": tasks_count,
            "latest_exam": latest_exam,
        },
    )


@app.get("/students/import", response_class=HTMLResponse)
def import_students_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("students_import.html", {"request": request, "result": None})


@app.post("/students/import", response_class=HTMLResponse)
async def import_students(
    request: Request,
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not upload.filename:
        raise HTTPException(status_code=400, detail="Keine Datei hochgeladen.")

    data = await upload.read()
    buffer = BytesIO(data)
    result = import_students_from_excel(db, buffer)
    db.commit()

    return templates.TemplateResponse(
        "students_import.html",
        {
            "request": request,
            "result": result,
            "filename": upload.filename,
        },
    )


@app.get("/tasks", response_class=HTMLResponse)
def list_tasks(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    tasks = (
        db.scalars(select(Task).order_by(Task.category, Task.subcategory, Task.title))
        .unique()
        .all()
    )
    rendered_tasks = [
        {
            "task": task,
            "statement_html": render_markdown(task.statement_markdown),
            "hints_html": render_markdown(task.hints_markdown),
            "solution_html": render_markdown(task.solution_markdown),
        }
        for task in tasks
    ]
    return templates.TemplateResponse(
        "tasks_list.html",
        {"request": request, "tasks": rendered_tasks},
    )


@app.get("/tasks/new", response_class=HTMLResponse)
def new_task_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    all_tasks = db.scalars(select(Task).order_by(Task.title)).unique().all()
    return templates.TemplateResponse(
        "tasks_form.html",
        {
            "request": request,
            "task": None,
            "all_tasks": all_tasks,
        },
    )


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_task_form(task_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")

    all_tasks = db.scalars(select(Task).order_by(Task.title)).unique().all()
    return templates.TemplateResponse(
        "tasks_form.html",
        {
            "request": request,
            "task": task,
            "all_tasks": all_tasks,
        },
    )


@app.post("/tasks", response_class=HTMLResponse)
async def create_task(
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    subcategory: Optional[str] = Form(None),
    statement_markdown: str = Form(...),
    hints_markdown: Optional[str] = Form(None),
    solution_markdown: Optional[str] = Form(None),
    dependency_ids: Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    task = Task(
        title=title.strip(),
        category=category.strip(),
        subcategory=subcategory.strip() if subcategory else None,
        statement_markdown=statement_markdown,
        hints_markdown=hints_markdown,
        solution_markdown=solution_markdown,
    )
    db.add(task)
    db.flush()

    if dependency_ids:
        dependencies = _parse_dependency_ids(dependency_ids, db, task.id)
        task.dependencies = dependencies

    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/tasks/{task_id}", response_class=HTMLResponse)
async def update_task(
    task_id: int,
    request: Request,
    title: str = Form(...),
    category: str = Form(...),
    subcategory: Optional[str] = Form(None),
    statement_markdown: str = Form(...),
    hints_markdown: Optional[str] = Form(None),
    solution_markdown: Optional[str] = Form(None),
    dependency_ids: Optional[str] = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")

    task.title = title.strip()
    task.category = category.strip()
    task.subcategory = subcategory.strip() if subcategory else None
    task.statement_markdown = statement_markdown
    task.hints_markdown = hints_markdown
    task.solution_markdown = solution_markdown

    if dependency_ids is not None:
        task.dependencies = _parse_dependency_ids(dependency_ids, db, task.id)

    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: int, db: Session = Depends(get_db)) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    db.delete(task)
    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.get("/categories", response_class=HTMLResponse)
def list_categories(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    categories = (
        db.scalars(select(CategoryRequirement).order_by(CategoryRequirement.category)).all()
    )
    return templates.TemplateResponse(
        "categories.html",
        {"request": request, "categories": categories},
    )


@app.post("/categories")
async def save_category(
    category: str = Form(...),
    required_count: int = Form(...),
    category_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    if category_id:
        requirement = db.get(CategoryRequirement, category_id)
        if not requirement:
            raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
        requirement.category = category.strip()
        requirement.required_count = required_count
    else:
        requirement = CategoryRequirement(category=category.strip(), required_count=required_count)
        db.add(requirement)

    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{category_id}/delete")
def delete_category(category_id: int, db: Session = Depends(get_db)) -> Response:
    requirement = db.get(CategoryRequirement, category_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
    db.delete(requirement)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/exams")
def create_exam(group_id: int = Form(...), db: Session = Depends(get_db)) -> Response:
    try:
        exam = generate_exam_for_group(db, group_id)
        db.commit()
    except ExamGenerationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RedirectResponse(url=f"/exams/{exam.id}/teacher", status_code=303)


@app.post("/exams/{exam_id}/regenerate")
def regenerate_exam_tasks(exam_id: int, db: Session = Depends(get_db)) -> Response:
    try:
        exam = regenerate_exam(db, exam_id)
        db.commit()
    except ExamGenerationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return RedirectResponse(url=f"/exams/{exam.id}/teacher", status_code=303)


@app.get("/exams/{exam_id}/teacher", response_class=HTMLResponse)
def show_exam_teacher(exam_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group).joinedload(StudentGroup.students))
        .options(joinedload(ExamSession.assignments).joinedload(ExamTaskAssignment.task))
        .where(ExamSession.id == exam_id)
    ).unique().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Prüfungssitzung nicht gefunden")

    payload = build_exam_payload(exam, include_solutions=True)
    return templates.TemplateResponse(
        "exam_teacher.html",
        {"request": request, "exam": payload},
    )


@app.get("/exams/{exam_id}/student", response_class=HTMLResponse)
def show_exam_student(exam_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    exam = db.get(ExamSession, exam_id)
    if not exam:
        raise HTTPException(status_code=404, detail="Prüfungssitzung nicht gefunden")
    return templates.TemplateResponse(
        "exam_student.html",
        {"request": request, "exam_id": exam_id},
    )


@app.get("/api/exams/{exam_id}", response_class=JSONResponse)
def get_exam_data(
    exam_id: int,
    include_solutions: bool = Query(False),
    db: Session = Depends(get_db),
) -> JSONResponse:
    exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group).joinedload(StudentGroup.students))
        .options(joinedload(ExamSession.assignments).joinedload(ExamTaskAssignment.task))
        .where(ExamSession.id == exam_id)
    ).unique().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Prüfungssitzung nicht gefunden")
    payload = build_exam_payload(exam, include_solutions=include_solutions)
    return JSONResponse(payload)


def _parse_dependency_ids(raw_value: str, db: Session, current_task_id: Optional[int]) -> List[Task]:
    dependency_ids: List[int] = []
    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            dependency_id = int(item)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Ungültige Abhängigkeits-ID: {item}") from exc
        if dependency_id == current_task_id:
            raise HTTPException(status_code=400, detail="Eine Aufgabe kann nicht von sich selbst abhängen.")
        dependency_ids.append(dependency_id)

    dependencies = []
    for dep_id in dependency_ids:
        task = db.get(Task, dep_id)
        if not task:
            raise HTTPException(status_code=404, detail=f"Abhängige Aufgabe mit ID {dep_id} wurde nicht gefunden.")
        dependencies.append(task)
    return dependencies
