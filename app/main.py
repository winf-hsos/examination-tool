from __future__ import annotations

from io import BytesIO
import base64
import json
import mimetypes
from pathlib import Path
from typing import List, Optional
from uuid import uuid4

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
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from .db import get_db, init_db, session_scope
from .models import (
    Category,
    CategoryRequirement,
    ExamSession,
    ExamTaskAssignment,
    StudentGroup,
    Task,
    TaskImage,
)
from .services.exam_service import (
    ExamGenerationError,
    build_exam_payload,
    generate_exam_for_group,
    regenerate_exam,
)
from .services.markdown_service import render_markdown
from .services.student_import_service import import_students_from_excel
from .schemas import MarkdownPreviewRequest

STATIC_DIR = Path("app/static")
UPLOAD_DIR = STATIC_DIR / "uploads"

app = FastAPI(title="Examination Tool", default_response_class=HTMLResponse)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


def _sync_category_nodes(db: Session) -> None:
    existing = {
        category.name: category
        for category in db.scalars(
            select(Category).where(Category.parent_id.is_(None))
        )
    }
    requirements = db.scalars(select(CategoryRequirement)).all()
    for requirement in requirements:
        if requirement.category not in existing:
            node = Category(name=requirement.category)
            db.add(node)
            existing[requirement.category] = node
    db.flush()


def _get_category_tree(db: Session) -> List[Category]:
    return (
        db.scalars(
            select(Category)
            .where(Category.parent_id.is_(None))
            .options(selectinload(Category.children))
            .order_by(Category.name)
        )
        .all()
    )


def _serialize_category_options(categories: List[Category]) -> List[dict]:
    return [
        {
            "id": category.id,
            "name": category.name,
            "subcategories": [
                {"id": sub.id, "name": sub.name} for sub in category.children
            ],
        }
        for category in categories
    ]


def _get_category_selection_for_task(
    db: Session, task: Task
) -> tuple[Optional[int], Optional[int]]:
    if not task:
        return None, None

    category_id: Optional[int] = None
    subcategory_id: Optional[int] = None
    if task.category:
        category = db.scalars(
            select(Category)
            .where(Category.name == task.category)
            .where(Category.parent_id.is_(None))
        ).first()
        if category:
            category_id = category.id
            if task.subcategory:
                subcategory = db.scalars(
                    select(Category)
                    .where(Category.name == task.subcategory)
                    .where(Category.parent_id == category.id)
                ).first()
                if subcategory:
                    subcategory_id = subcategory.id
    return category_id, subcategory_id


def _resolve_category_selection(
    db: Session, category_id: int, subcategory_id: Optional[int]
) -> tuple[Category, Optional[Category]]:
    category = db.get(Category, category_id)
    if not category or category.parent_id is not None:
        raise HTTPException(status_code=400, detail="Ungültige Kategorie ausgewählt.")

    subcategory: Category | None = None
    if subcategory_id:
        subcategory = db.get(Category, subcategory_id)
        if not subcategory or subcategory.parent_id != category.id:
            raise HTTPException(
                status_code=400, detail="Ungültige Unterkategorie ausgewählt."
            )

    return category, subcategory


def _delete_image_file(image: TaskImage) -> None:
    file_path = STATIC_DIR / image.file_path
    try:
        file_path.unlink()
    except FileNotFoundError:
        pass


def _guess_file_suffix(original_filename: str, mime_type: str | None) -> str:
    suffix = Path(original_filename).suffix
    if suffix:
        return suffix
    if mime_type:
        guessed = mimetypes.guess_extension(mime_type)
        if guessed:
            return guessed
    return ""


async def _save_uploaded_images(task: Task, images: List[UploadFile]) -> None:
    if not images:
        return

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    existing_count = len(task.images)
    for index, upload in enumerate(images):
        if not upload.filename:
            continue
        if not upload.content_type or not upload.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Nur Bilddateien sind erlaubt.")

        suffix = _guess_file_suffix(upload.filename, upload.content_type)
        unique_name = f"{uuid4().hex}{suffix}"
        destination = UPLOAD_DIR / unique_name
        content = await upload.read()
        destination.write_bytes(content)

        image = TaskImage(
            task=task,
            file_path=f"uploads/{unique_name}",
            original_filename=upload.filename,
            mime_type=upload.content_type or "application/octet-stream",
            position=existing_count + index,
        )
        task.images.append(image)


def _normalize_image_positions(task: Task) -> None:
    for position, image in enumerate(sorted(task.images, key=lambda item: item.position)):
        image.position = position


def _mark_exam_as_active(db: Session, exam: ExamSession) -> None:
    db.execute(update(ExamSession).values(is_active=False))
    exam.is_active = True
    db.flush()


def _encode_task_image(image: TaskImage) -> Optional[dict]:
    file_path = STATIC_DIR / image.file_path
    if not file_path.exists():
        return None
    data = base64.b64encode(file_path.read_bytes()).decode("utf-8")
    return {
        "original_filename": image.original_filename,
        "mime_type": image.mime_type,
        "data": data,
        "position": image.position,
    }


def _store_imported_image(data: dict) -> tuple[str, str, str]:
    encoded = data.get("data")
    if not encoded:
        raise HTTPException(
            status_code=400, detail="Bilddaten fehlen im Import."  # pragma: no cover
        )
    try:
        content = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail="Ungültige Bilddaten im Import.") from exc

    original_filename = data.get("original_filename", "attachment")
    mime_type = data.get("mime_type", "application/octet-stream")
    suffix = _guess_file_suffix(original_filename, mime_type)
    unique_name = f"{uuid4().hex}{suffix}"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOAD_DIR / unique_name
    destination.write_bytes(content)
    return unique_name, original_filename, mime_type
@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with session_scope() as db:
        _sync_category_nodes(db)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    groups = (
        db.scalars(select(StudentGroup).order_by(StudentGroup.label)).unique().all()
    )
    category_requirements = db.scalars(select(CategoryRequirement).order_by(CategoryRequirement.category)).all()
    requirements_map = {requirement.category: requirement for requirement in category_requirements}
    tasks_count = db.scalar(select(func.count()).select_from(Task)) or 0
    latest_exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group).joinedload(StudentGroup.students))
        .order_by(ExamSession.started_at.desc())
        .limit(1)
    ).unique().first()
    category_tree = _get_category_tree(db)
    active_exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group))
        .where(ExamSession.is_active.is_(True))
        .order_by(ExamSession.started_at.desc())
        .limit(1)
    ).unique().first()
    student_live_url = request.url_for("show_exam_live")

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "groups": groups,
            "category_requirements": category_requirements,
            "category_tree": category_tree,
            "requirements_map": requirements_map,
            "tasks_count": tasks_count,
            "latest_exam": latest_exam,
            "active_exam": active_exam,
            "student_live_url": student_live_url,
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
        db.scalars(
            select(Task)
            .options(joinedload(Task.dependencies), joinedload(Task.images))
            .order_by(Task.category, Task.subcategory, Task.title)
        )
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
    import_summary = None
    if request.query_params.get("import_status") == "success":
        try:
            import_summary = {
                "tasks": int(request.query_params.get("tasks", "0")),
                "categories": int(request.query_params.get("categories", "0")),
                "subcategories": int(request.query_params.get("subcategories", "0")),
                "images": int(request.query_params.get("images", "0")),
            }
        except ValueError:
            import_summary = None
    return templates.TemplateResponse(
        "tasks_list.html",
        {"request": request, "tasks": rendered_tasks, "import_summary": import_summary},
    )


@app.get("/tasks/new", response_class=HTMLResponse)
def new_task_form(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    all_tasks = db.scalars(select(Task).order_by(Task.title)).unique().all()
    categories = _get_category_tree(db)
    category_options_json = json.dumps(
        _serialize_category_options(categories), ensure_ascii=False
    )
    return templates.TemplateResponse(
        "tasks_form.html",
        {
            "request": request,
            "task": None,
            "all_tasks": all_tasks,
            "categories": categories,
            "category_options_json": category_options_json,
            "selected_category_id": None,
            "selected_subcategory_id": None,
        },
    )


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
def edit_task_form(task_id: int, request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")

    all_tasks = db.scalars(select(Task).order_by(Task.title)).unique().all()
    categories = _get_category_tree(db)
    category_options_json = json.dumps(
        _serialize_category_options(categories), ensure_ascii=False
    )
    selected_category_id, selected_subcategory_id = _get_category_selection_for_task(
        db, task
    )
    return templates.TemplateResponse(
        "tasks_form.html",
        {
            "request": request,
            "task": task,
            "all_tasks": all_tasks,
            "categories": categories,
            "category_options_json": category_options_json,
            "selected_category_id": selected_category_id,
            "selected_subcategory_id": selected_subcategory_id,
        },
    )


@app.post("/tasks", response_class=HTMLResponse)
async def create_task(
    request: Request,
    title: str = Form(...),
    category_id: int = Form(...),
    subcategory_id: Optional[str] = Form(None),
    statement_markdown: str = Form(...),
    hints_markdown: Optional[str] = Form(None),
    solution_markdown: Optional[str] = Form(None),
    dependency_ids: Optional[str] = Form(None),
    images: List[UploadFile] = File([]),
    db: Session = Depends(get_db),
) -> Response:
    subcategory_pk = int(subcategory_id) if subcategory_id else None
    category_node, subcategory_node = _resolve_category_selection(
        db, category_id, subcategory_pk
    )
    task = Task(
        title=title.strip(),
        category=category_node.name,
        subcategory=subcategory_node.name if subcategory_node else None,
        statement_markdown=statement_markdown,
        hints_markdown=hints_markdown,
        solution_markdown=solution_markdown,
    )
    db.add(task)
    db.flush()

    if dependency_ids:
        dependencies = _parse_dependency_ids(dependency_ids, db, task.id)
        task.dependencies = dependencies

    await _save_uploaded_images(task, images)
    _normalize_image_positions(task)
    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.post("/tasks/{task_id}", response_class=HTMLResponse)
async def update_task(
    task_id: int,
    request: Request,
    title: str = Form(...),
    category_id: int = Form(...),
    subcategory_id: Optional[str] = Form(None),
    statement_markdown: str = Form(...),
    hints_markdown: Optional[str] = Form(None),
    solution_markdown: Optional[str] = Form(None),
    dependency_ids: Optional[str] = Form(None),
    remove_image_ids: List[str] = Form([]),
    images: List[UploadFile] = File([]),
    db: Session = Depends(get_db),
) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")

    subcategory_pk = int(subcategory_id) if subcategory_id else None
    category_node, subcategory_node = _resolve_category_selection(
        db, category_id, subcategory_pk
    )
    task.title = title.strip()
    task.category = category_node.name
    task.subcategory = subcategory_node.name if subcategory_node else None
    task.statement_markdown = statement_markdown
    task.hints_markdown = hints_markdown
    task.solution_markdown = solution_markdown

    if dependency_ids is not None:
        task.dependencies = _parse_dependency_ids(dependency_ids, db, task.id)

    remove_ids: List[int] = []
    for value in remove_image_ids or []:
        try:
            remove_ids.append(int(value))
        except (TypeError, ValueError):
            continue

    for image in list(task.images):
        if image.id in remove_ids:
            _delete_image_file(image)
            db.delete(image)

    await _save_uploaded_images(task, images)
    _normalize_image_positions(task)

    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.get("/tasks/export")
def export_tasks_data(db: Session = Depends(get_db)) -> JSONResponse:
    categories = _get_category_tree(db)
    requirements = {
        requirement.category: requirement.required_count
        for requirement in db.scalars(select(CategoryRequirement)).all()
    }
    categories_payload = [
        {
            "name": category.name,
            "required_count": requirements.get(category.name, 0),
            "subcategories": [
                {"name": subcategory.name} for subcategory in category.children
            ],
        }
        for category in categories
    ]

    tasks = (
        db.scalars(
            select(Task)
            .options(joinedload(Task.dependencies), joinedload(Task.images))
            .order_by(Task.category, Task.subcategory, Task.title)
        )
        .unique()
        .all()
    )
    tasks_payload = []
    for task in tasks:
        task_payload = {
            "id": task.id,
            "title": task.title,
            "category": task.category,
            "subcategory": task.subcategory,
            "statement_markdown": task.statement_markdown,
            "hints_markdown": task.hints_markdown,
            "solution_markdown": task.solution_markdown,
            "dependencies": [dependency.id for dependency in task.dependencies],
        }
        images_payload = []
        for image in task.images:
            encoded = _encode_task_image(image)
            if encoded:
                images_payload.append(encoded)
        if images_payload:
            task_payload["images"] = images_payload
        tasks_payload.append(task_payload)

    payload = {"categories": categories_payload, "tasks": tasks_payload}
    headers = {
        "Content-Disposition": "attachment; filename=examination_tasks_export.json"
    }
    return JSONResponse(payload, headers=headers)


@app.post("/tasks/import")
async def import_tasks_data(
    upload: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> Response:
    if not upload.filename:
        raise HTTPException(status_code=400, detail="Keine Datei hochgeladen.")
    data = await upload.read()
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Ungültige JSON-Datei.") from exc

    categories_data = payload.get("categories", []) or []
    tasks_data = payload.get("tasks", []) or []

    created_categories = 0
    created_subcategories = 0
    imported_images = 0

    for category_entry in categories_data:
        name = (category_entry.get("name") or "").strip()
        if not name:
            continue
        required_count = int(category_entry.get("required_count", 0) or 0)
        requirement = db.scalars(
            select(CategoryRequirement).where(CategoryRequirement.category == name)
        ).first()
        if requirement:
            requirement.required_count = required_count
        else:
            requirement = CategoryRequirement(category=name, required_count=required_count)
            db.add(requirement)
            created_categories += 1

        category_node = db.scalars(
            select(Category)
            .where(Category.name == name)
            .where(Category.parent_id.is_(None))
        ).first()
        if not category_node:
            category_node = Category(name=name)
            db.add(category_node)
            db.flush()
            created_categories += 1
        else:
            db.flush()

        for sub_entry in category_entry.get("subcategories", []) or []:
            sub_name = (sub_entry.get("name") or "").strip()
            if not sub_name:
                continue
            existing_sub = db.scalars(
                select(Category)
                .where(Category.parent_id == category_node.id)
                .where(Category.name == sub_name)
            ).first()
            if existing_sub:
                continue
            subcategory = Category(name=sub_name, parent=category_node)
            db.add(subcategory)
            created_subcategories += 1

    db.flush()

    id_mapping: dict[int, Task] = {}
    for task_entry in tasks_data:
        title = (task_entry.get("title") or "").strip()
        if not title:
            continue
        statement_markdown = task_entry.get("statement_markdown") or ""
        task = Task(
            title=title,
            category=(task_entry.get("category") or "").strip(),
            subcategory=(task_entry.get("subcategory") or None),
            statement_markdown=statement_markdown,
            hints_markdown=task_entry.get("hints_markdown"),
            solution_markdown=task_entry.get("solution_markdown"),
        )
        db.add(task)
        db.flush()
        original_id = task_entry.get("id")
        if isinstance(original_id, int):
            id_mapping[original_id] = task

        images = task_entry.get("images", []) or []
        for position, image_entry in enumerate(images):
            try:
                unique_name, original_filename, mime_type = _store_imported_image(
                    image_entry
                )
            except HTTPException:
                continue
            image = TaskImage(
                task=task,
                file_path=f"uploads/{unique_name}",
                original_filename=original_filename,
                mime_type=mime_type,
                position=image_entry.get("position", position),
            )
            db.add(image)
            imported_images += 1
        _normalize_image_positions(task)

    db.flush()

    for task_entry in tasks_data:
        original_id = task_entry.get("id")
        if not isinstance(original_id, int):
            continue
        task = id_mapping.get(original_id)
        if not task:
            continue
        dependency_ids = task_entry.get("dependencies", []) or []
        dependencies: List[Task] = []
        for dep_id in dependency_ids:
            mapped = id_mapping.get(dep_id)
            if mapped and mapped.id != task.id:
                dependencies.append(mapped)
        task.dependencies = dependencies

    db.commit()

    params = (
        "?import_status=success"
        f"&tasks={len(tasks_data)}"
        f"&categories={created_categories}"
        f"&subcategories={created_subcategories}"
        f"&images={imported_images}"
    )
    return RedirectResponse(url=f"/tasks{params}", status_code=303)


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: int, db: Session = Depends(get_db)) -> Response:
    task = db.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    for image in list(task.images):
        _delete_image_file(image)
    db.delete(task)
    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.get("/categories", response_class=HTMLResponse)
def list_categories(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    requirements = {
        requirement.category: requirement
        for requirement in db.scalars(
            select(CategoryRequirement).order_by(CategoryRequirement.category)
        ).all()
    }
    categories = _get_category_tree(db)
    return templates.TemplateResponse(
        "categories.html",
        {
            "request": request,
            "categories": categories,
            "requirements": requirements,
        },
    )


@app.post("/categories")
async def save_category(
    category: str = Form(...),
    required_count: int = Form(...),
    category_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    category_name = category.strip()
    if category_id:
        requirement = db.get(CategoryRequirement, category_id)
        if not requirement:
            raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
        requirement.category = category_name
        requirement.required_count = required_count
        category_node = db.scalars(
            select(Category)
            .where(Category.name == category_name)
            .where(Category.parent_id.is_(None))
        ).first()
        if not category_node:
            db.add(Category(name=category_name))
    else:
        requirement = CategoryRequirement(
            category=category_name, required_count=required_count
        )
        db.add(requirement)
        existing = db.scalars(
            select(Category)
            .where(Category.name == category_name)
            .where(Category.parent_id.is_(None))
        ).first()
        if not existing:
            db.add(Category(name=category_name))

    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{category_id}/delete")
def delete_category(category_id: int, db: Session = Depends(get_db)) -> Response:
    requirement = db.get(CategoryRequirement, category_id)
    if not requirement:
        raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
    tasks_in_category = db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.category == requirement.category)
    )
    if tasks_in_category:
        raise HTTPException(
            status_code=400,
            detail="Kategorie kann nicht gelöscht werden, solange ihr Aufgaben zugeordnet sind.",
        )
    category_node = db.scalars(
        select(Category)
        .where(Category.name == requirement.category)
        .where(Category.parent_id.is_(None))
    ).first()
    if category_node:
        db.delete(category_node)
    db.delete(requirement)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{category_id}/subcategories")
async def add_subcategory(
    category_id: int,
    name: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    parent = db.get(Category, category_id)
    if not parent or parent.parent_id is not None:
        raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
    subcategory_name = name.strip()
    if not subcategory_name:
        raise HTTPException(status_code=400, detail="Unterkategorie benötigt einen Namen.")
    existing = db.scalars(
        select(Category)
        .where(Category.parent_id == parent.id)
        .where(Category.name == subcategory_name)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Unterkategorie existiert bereits.")

    subcategory = Category(name=subcategory_name, parent=parent)
    db.add(subcategory)
    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{category_id}/subcategories/{subcategory_id}/delete")
def delete_subcategory(
    category_id: int,
    subcategory_id: int,
    db: Session = Depends(get_db),
) -> Response:
    parent = db.get(Category, category_id)
    if not parent or parent.parent_id is not None:
        raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
    subcategory = db.get(Category, subcategory_id)
    if not subcategory or subcategory.parent_id != parent.id:
        raise HTTPException(status_code=404, detail="Unterkategorie nicht gefunden")

    db.delete(subcategory)
    db.flush()
    tasks_to_update = db.scalars(
        select(Task)
        .where(Task.category == parent.name)
        .where(Task.subcategory == subcategory.name)
    ).all()
    for task in tasks_to_update:
        task.subcategory = None

    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/exams")
def create_exam(group_id: int = Form(...), db: Session = Depends(get_db)) -> Response:
    try:
        exam = generate_exam_for_group(db, group_id)
        _mark_exam_as_active(db, exam)
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
        _mark_exam_as_active(db, exam)
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
        .options(
            joinedload(ExamSession.assignments).joinedload(ExamTaskAssignment.task).joinedload(
                Task.images
            )
        )
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
        {"request": request, "exam_id": exam_id, "live_mode": False},
    )


@app.get("/exams/live", response_class=HTMLResponse, name="show_exam_live")
def show_exam_live(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "exam_student.html",
        {"request": request, "exam_id": None, "live_mode": True},
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
        .options(
            joinedload(ExamSession.assignments).joinedload(ExamTaskAssignment.task).joinedload(
                Task.images
            )
        )
        .where(ExamSession.id == exam_id)
    ).unique().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Prüfungssitzung nicht gefunden")
    payload = build_exam_payload(exam, include_solutions=include_solutions)
    return JSONResponse(payload)


@app.get("/api/exams/active", response_class=JSONResponse)
def get_active_exam(db: Session = Depends(get_db)) -> JSONResponse:
    exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group).joinedload(StudentGroup.students))
        .options(
            joinedload(ExamSession.assignments).joinedload(ExamTaskAssignment.task).joinedload(
                Task.images
            )
        )
        .where(ExamSession.is_active.is_(True))
        .order_by(ExamSession.started_at.desc())
        .limit(1)
    ).unique().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Aktuell ist keine Prüfung aktiv.")
    payload = build_exam_payload(exam, include_solutions=False)
    return JSONResponse(payload)


@app.post("/api/markdown/preview", response_class=JSONResponse)
async def markdown_preview(payload: MarkdownPreviewRequest) -> JSONResponse:
    html = render_markdown(payload.content) or ""
    return JSONResponse({"html": html})


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
