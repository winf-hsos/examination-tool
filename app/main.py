from __future__ import annotations

from io import BytesIO
import base64
import json
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
from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from .db import get_db, init_db
from .models import (
    Category,
    ExamConfiguration,
    ExamConfigurationRequirement,
    ExamSession,
    ExamTaskAssignment,
    StudentGroup,
    Task,
    TaskImage,
)
from .services.exam_service import (
    ExamGenerationError,
    build_exam_payload,
    generate_exam,
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
    if image.thumbnail_path:
        try:
            (STATIC_DIR / image.thumbnail_path).unlink()
        except FileNotFoundError:
            pass


TARGET_IMAGE_SIZE = (1200, 900)
THUMBNAIL_SIZE = (320, 240)


def _sanitize_crop_data(raw: dict | None) -> dict | None:
    if not raw:
        return None
    try:
        x = float(raw.get("x", 0))
        y = float(raw.get("y", 0))
        width = float(raw.get("width", 0))
        height = float(raw.get("height", 0))
    except (TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _ensure_ratio(image: Image.Image, crop_data: dict | None) -> Image.Image:
    working = image
    if crop_data:
        left = max(0, int(round(crop_data["x"])))
        upper = max(0, int(round(crop_data["y"])))
        right = min(image.width, left + int(round(crop_data["width"])))
        lower = min(image.height, upper + int(round(crop_data["height"])))
        if right > left and lower > upper:
            working = image.crop((left, upper, right, lower))
    target_ratio = TARGET_IMAGE_SIZE[0] / TARGET_IMAGE_SIZE[1]
    current_ratio = working.width / working.height
    if abs(current_ratio - target_ratio) < 0.01:
        return working
    if current_ratio > target_ratio:
        new_width = int(working.height * target_ratio)
        offset = max(0, (working.width - new_width) // 2)
        return working.crop((offset, 0, offset + new_width, working.height))
    new_height = int(working.width / target_ratio)
    offset = max(0, (working.height - new_height) // 2)
    return working.crop((0, offset, working.width, offset + new_height))


def _resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.resize(size, Image.LANCZOS)


def _prepare_image_bytes(content: bytes, crop_data: dict | None) -> tuple[bytes, bytes]:
    try:
        with Image.open(BytesIO(content)) as original:
            original = original.convert("RGB")
            framed = _ensure_ratio(original, crop_data)
            resized = _resize(framed, TARGET_IMAGE_SIZE)
            buffer = BytesIO()
            resized.save(buffer, format="JPEG", quality=90)
            main_bytes = buffer.getvalue()

            thumb = _resize(resized, THUMBNAIL_SIZE)
            thumb_buffer = BytesIO()
            thumb.save(thumb_buffer, format="JPEG", quality=85)
            return main_bytes, thumb_buffer.getvalue()
    except UnidentifiedImageError:
        # The imported payload may contain historic binary attachments that are not valid
        # images. Preserve the original content without transformation so legacy exports
        # can still be restored.
        return content, content


def _parse_crop_metadata(raw: str | None) -> dict[str, dict]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail="Ungültige Daten zum Bildzuschnitt.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ungültige Daten zum Bildzuschnitt.")
    result: dict[str, dict] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            result[key] = value
    return result

async def _save_uploaded_images(
    task: Task, images: List[UploadFile], crop_metadata: dict[str, dict] | None
) -> None:
    if not images:
        return

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    existing_count = len(task.images)
    crop_metadata = crop_metadata or {}
    for index, upload in enumerate(images):
        if not upload.filename:
            continue
        if not upload.content_type or not upload.content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="Nur Bilddateien sind erlaubt.")

        crop_data = _sanitize_crop_data(crop_metadata.get(upload.filename))
        content = await upload.read()
        main_bytes, thumb_bytes = _prepare_image_bytes(content, crop_data)
        base_name = uuid4().hex
        image_name = f"{base_name}.jpg"
        thumb_name = f"{base_name}_thumb.jpg"
        (UPLOAD_DIR / image_name).write_bytes(main_bytes)
        (UPLOAD_DIR / thumb_name).write_bytes(thumb_bytes)

        image = TaskImage(
            task=task,
            file_path=f"uploads/{image_name}",
            thumbnail_path=f"uploads/{thumb_name}",
            original_filename=upload.filename,
            mime_type="image/jpeg",
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
    thumbnail_data = None
    if image.thumbnail_path:
        thumbnail_file = STATIC_DIR / image.thumbnail_path
        if thumbnail_file.exists():
            thumbnail_data = base64.b64encode(thumbnail_file.read_bytes()).decode("utf-8")
    return {
        "original_filename": image.original_filename,
        "mime_type": image.mime_type,
        "data": data,
        "position": image.position,
        "thumbnail": thumbnail_data,
    }


def _store_imported_image(data: dict) -> tuple[str, str, str, str]:
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
    crop_data = _sanitize_crop_data(data.get("crop"))
    main_bytes, thumb_bytes = _prepare_image_bytes(content, crop_data)
    base_name = uuid4().hex
    unique_name = f"{base_name}.jpg"
    thumb_name = f"{base_name}_thumb.jpg"
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = UPLOAD_DIR / unique_name
    destination.write_bytes(main_bytes)
    (UPLOAD_DIR / thumb_name).write_bytes(thumb_bytes)
    return unique_name, thumb_name, original_filename, "image/jpeg"
@app.on_event("startup")
async def on_startup() -> None:
    init_db()
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    groups = (
        db.scalars(select(StudentGroup).order_by(StudentGroup.label)).unique().all()
    )
    tasks_count = db.scalar(select(func.count()).select_from(Task)) or 0
    latest_exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group).joinedload(StudentGroup.students))
        .order_by(ExamSession.started_at.desc())
        .limit(1)
    ).unique().first()
    category_tree = _get_category_tree(db)
    configurations = (
        db.scalars(
            select(ExamConfiguration)
            .options(
                selectinload(ExamConfiguration.requirements)
                .selectinload(ExamConfigurationRequirement.category)
            )
            .options(
                selectinload(ExamConfiguration.requirements)
                .selectinload(ExamConfigurationRequirement.subcategory)
            )
            .order_by(ExamConfiguration.name)
        )
        .unique()
        .all()
    )
    active_exam = db.scalars(
        select(ExamSession)
        .options(joinedload(ExamSession.group))
        .options(joinedload(ExamSession.configuration))
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
            "category_tree": category_tree,
            "configurations": configurations,
            "tasks_count": tasks_count,
            "latest_exam": latest_exam,
            "active_exam": active_exam,
            "student_live_url": student_live_url,
            "has_groups": bool(groups),
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
    difficulty: int = Form(2),
    statement_markdown: str = Form(...),
    hints_markdown: Optional[str] = Form(None),
    solution_markdown: Optional[str] = Form(None),
    dependency_ids: Optional[str] = Form(None),
    crop_metadata: str = Form("{}"),
    images: List[UploadFile] = File([]),
    db: Session = Depends(get_db),
) -> Response:
    subcategory_pk = int(subcategory_id) if subcategory_id else None
    category_node, subcategory_node = _resolve_category_selection(
        db, category_id, subcategory_pk
    )
    difficulty_value = max(1, min(3, int(difficulty)))
    crop_data = _parse_crop_metadata(crop_metadata)
    task = Task(
        title=title.strip(),
        category=category_node.name,
        subcategory=subcategory_node.name if subcategory_node else None,
        difficulty=difficulty_value,
        statement_markdown=statement_markdown,
        hints_markdown=hints_markdown,
        solution_markdown=solution_markdown,
    )
    db.add(task)
    db.flush()

    if dependency_ids:
        dependencies = _parse_dependency_ids(dependency_ids, db, task.id)
        task.dependencies = dependencies

    await _save_uploaded_images(task, images, crop_data)
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
    difficulty: int = Form(2),
    statement_markdown: str = Form(...),
    hints_markdown: Optional[str] = Form(None),
    solution_markdown: Optional[str] = Form(None),
    dependency_ids: Optional[str] = Form(None),
    remove_image_ids: List[str] = Form([]),
    crop_metadata: str = Form("{}"),
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
    difficulty_value = max(1, min(3, int(difficulty)))
    crop_data = _parse_crop_metadata(crop_metadata)
    task.title = title.strip()
    task.category = category_node.name
    task.subcategory = subcategory_node.name if subcategory_node else None
    task.difficulty = difficulty_value
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

    await _save_uploaded_images(task, images, crop_data)
    _normalize_image_positions(task)

    db.commit()
    return RedirectResponse(url="/tasks", status_code=303)


@app.get("/tasks/export")
def export_tasks_data(db: Session = Depends(get_db)) -> JSONResponse:
    categories = _get_category_tree(db)
    categories_payload = [
        {
            "name": category.name,
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
            "difficulty": task.difficulty,
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

    configurations = (
        db.scalars(
            select(ExamConfiguration)
            .options(
                selectinload(ExamConfiguration.requirements)
                .selectinload(ExamConfigurationRequirement.category)
            )
            .options(
                selectinload(ExamConfiguration.requirements)
                .selectinload(ExamConfigurationRequirement.subcategory)
            )
            .order_by(ExamConfiguration.name)
        )
        .unique()
        .all()
    )
    configurations_payload = []
    for configuration in configurations:
        requirements_payload = []
        for requirement in configuration.requirements:
            requirements_payload.append(
                {
                    "category": requirement.category.name,
                    "subcategory": requirement.subcategory.name if requirement.subcategory else None,
                    "question_count": requirement.question_count,
                    "position": requirement.position,
                }
            )
        configurations_payload.append(
            {
                "name": configuration.name,
                "target_difficulty": configuration.target_difficulty,
                "requirements": requirements_payload,
            }
        )

    payload = {
        "categories": categories_payload,
        "tasks": tasks_payload,
        "configurations": configurations_payload,
    }
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
    configurations_data = payload.get("configurations", []) or []

    created_categories = 0
    created_subcategories = 0
    imported_images = 0
    processed_configurations = 0

    for category_entry in categories_data:
        name = (category_entry.get("name") or "").strip()
        if not name:
            continue
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
        try:
            difficulty_value = int(task_entry.get("difficulty", 2) or 2)
        except (TypeError, ValueError):
            difficulty_value = 2
        difficulty_value = max(1, min(3, difficulty_value))
        task = Task(
            title=title,
            category=(task_entry.get("category") or "").strip(),
            subcategory=(task_entry.get("subcategory") or None),
            difficulty=difficulty_value,
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
                unique_name, thumb_name, original_filename, mime_type = (
                    _store_imported_image(image_entry)
                )
            except HTTPException:
                continue
            image = TaskImage(
                task=task,
                file_path=f"uploads/{unique_name}",
                thumbnail_path=f"uploads/{thumb_name}",
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

    db.flush()

    for config_entry in configurations_data:
        name = (config_entry.get("name") or "").strip()
        if not name:
            continue
        try:
            target = float(config_entry.get("target_difficulty", 2.0) or 2.0)
        except (TypeError, ValueError):
            target = 2.0
        configuration = db.scalars(
            select(ExamConfiguration).where(ExamConfiguration.name == name)
        ).first()
        if not configuration:
            configuration = ExamConfiguration(name=name, target_difficulty=target)
            db.add(configuration)
            db.flush()
        else:
            configuration.target_difficulty = target
            configuration.requirements.clear()
        processed_configurations += 1

        requirements_data = config_entry.get("requirements", []) or []
        for position, requirement_entry in enumerate(requirements_data):
            category_name = (requirement_entry.get("category") or "").strip()
            if not category_name:
                continue
            category = db.scalars(
                select(Category)
                .where(Category.parent_id.is_(None))
                .where(Category.name == category_name)
            ).first()
            if not category:
                continue
            subcategory_name = (requirement_entry.get("subcategory") or "").strip()
            subcategory = None
            if subcategory_name:
                subcategory = db.scalars(
                    select(Category)
                    .where(Category.parent_id == category.id)
                    .where(Category.name == subcategory_name)
                ).first()
            try:
                question_count = int(requirement_entry.get("question_count", 1) or 1)
            except (TypeError, ValueError):
                question_count = 1
            question_count = max(1, question_count)
            configuration.requirements.append(
                ExamConfigurationRequirement(
                    category_id=category.id,
                    subcategory_id=subcategory.id if subcategory else None,
                    question_count=question_count,
                    position=position,
                )
            )

    db.commit()

    params = (
        "?import_status=success"
        f"&tasks={len(tasks_data)}"
        f"&categories={created_categories}"
        f"&subcategories={created_subcategories}"
        f"&images={imported_images}"
        f"&configurations={processed_configurations}"
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


@app.post("/tasks/{task_id}/copy")
def copy_task(task_id: int, db: Session = Depends(get_db)) -> Response:
    task = db.scalars(
        select(Task)
        .options(joinedload(Task.dependencies), joinedload(Task.images))
        .where(Task.id == task_id)
    ).unique().first()
    if not task:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    new_task = Task(
        title=f"Kopie von {task.title}",
        category=task.category,
        subcategory=task.subcategory,
        difficulty=task.difficulty,
        statement_markdown=task.statement_markdown,
        hints_markdown=task.hints_markdown,
        solution_markdown=task.solution_markdown,
    )
    db.add(new_task)
    db.flush()

    new_task.dependencies = list(task.dependencies)

    for image in task.images:
        source = STATIC_DIR / image.file_path
        if not source.exists():
            continue
        base_name = uuid4().hex
        suffix = Path(source.name).suffix or ".jpg"
        new_main_name = f"{base_name}{suffix}"
        (UPLOAD_DIR / new_main_name).write_bytes(source.read_bytes())

        new_thumb_path: Optional[str] = None
        if image.thumbnail_path:
            thumb_source = STATIC_DIR / image.thumbnail_path
            if thumb_source.exists():
                thumb_suffix = Path(thumb_source.name).suffix or ".jpg"
                new_thumb_name = f"{base_name}_thumb{thumb_suffix}"
                (UPLOAD_DIR / new_thumb_name).write_bytes(thumb_source.read_bytes())
                new_thumb_path = f"uploads/{new_thumb_name}"

        copied_image = TaskImage(
            task=new_task,
            file_path=f"uploads/{new_main_name}",
            thumbnail_path=new_thumb_path,
            original_filename=image.original_filename,
            mime_type=image.mime_type,
            position=image.position,
        )
        db.add(copied_image)

    _normalize_image_positions(new_task)
    db.commit()
    return RedirectResponse(url=f"/tasks/{new_task.id}/edit", status_code=303)


@app.get("/categories", response_class=HTMLResponse)
def list_categories(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    categories = _get_category_tree(db)
    return templates.TemplateResponse(
        "categories.html",
        {
            "request": request,
            "categories": categories,
        },
    )


@app.get("/exam-configurations/new", response_class=HTMLResponse)
def new_exam_configuration(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    categories = _get_category_tree(db)
    category_options_json = json.dumps(
        _serialize_category_options(categories), ensure_ascii=False
    )
    return templates.TemplateResponse(
        "exam_configuration_form.html",
        {
            "request": request,
            "categories": categories,
            "category_options_json": category_options_json,
        },
    )


@app.post("/exam-configurations")
async def create_exam_configuration(
    name: str = Form(...),
    target_difficulty: float = Form(...),
    requirements_json: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    configuration_name = name.strip()
    if not configuration_name:
        raise HTTPException(status_code=400, detail="Die Konfiguration benötigt einen Namen.")

    existing = db.scalars(
        select(ExamConfiguration).where(ExamConfiguration.name == configuration_name)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Eine Konfiguration mit diesem Namen existiert bereits.")

    try:
        requirements_payload = json.loads(requirements_json)
    except json.JSONDecodeError as exc:  # pragma: no cover
        raise HTTPException(status_code=400, detail="Ungültige Kategorienauswahl.") from exc
    if not isinstance(requirements_payload, list) or not requirements_payload:
        raise HTTPException(status_code=400, detail="Es muss mindestens eine Kategorie ausgewählt werden.")

    difficulty_value = max(1.0, min(3.0, float(target_difficulty)))

    configuration = ExamConfiguration(
        name=configuration_name,
        target_difficulty=difficulty_value,
    )
    db.add(configuration)
    db.flush()

    for position, requirement in enumerate(requirements_payload):
        category_id = requirement.get("category_id")
        if category_id is None:
            continue
        category = db.get(Category, int(category_id))
        if not category or category.parent_id is not None:
            raise HTTPException(status_code=400, detail="Ungültige Kategorie ausgewählt.")

        subcategory_id = requirement.get("subcategory_id")
        subcategory = None
        if subcategory_id:
            subcategory = db.get(Category, int(subcategory_id))
            if not subcategory or subcategory.parent_id != category.id:
                raise HTTPException(status_code=400, detail="Ungültige Unterkategorie ausgewählt.")

        try:
            question_count = int(requirement.get("question_count", 1) or 1)
        except (TypeError, ValueError):
            question_count = 1
        question_count = max(1, question_count)

        configuration.requirements.append(
            ExamConfigurationRequirement(
                category_id=category.id,
                subcategory_id=subcategory.id if subcategory else None,
                question_count=question_count,
                position=position,
            )
        )

    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/exam-configurations/{configuration_id}/delete")
def delete_exam_configuration(
    configuration_id: int, db: Session = Depends(get_db)
) -> Response:
    configuration = db.get(ExamConfiguration, configuration_id)
    if not configuration:
        raise HTTPException(status_code=404, detail="Konfiguration nicht gefunden.")
    active_sessions = db.scalar(
        select(func.count())
        .select_from(ExamSession)
        .where(ExamSession.configuration_id == configuration_id)
    )
    if active_sessions:
        raise HTTPException(
            status_code=400,
            detail="Die Konfiguration wird noch von Prüfungen verwendet und kann nicht gelöscht werden.",
        )
    db.delete(configuration)
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/categories")
async def save_category(
    name: str = Form(...),
    category_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
) -> Response:
    category_name = name.strip()
    if not category_name:
        raise HTTPException(status_code=400, detail="Die Kategorie benötigt einen Namen.")

    if category_id:
        category = db.get(Category, category_id)
        if not category or category.parent_id is not None:
            raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
        existing = db.scalars(
            select(Category)
            .where(Category.parent_id.is_(None))
            .where(Category.name == category_name)
            .where(Category.id != category.id)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Kategorie existiert bereits.")
        if category.name != category_name:
            old_name = category.name
            for task in db.scalars(select(Task).where(Task.category == old_name)).all():
                task.category = category_name
            category.name = category_name
    else:
        existing = db.scalars(
            select(Category)
            .where(Category.parent_id.is_(None))
            .where(Category.name == category_name)
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Kategorie existiert bereits.")
        db.add(Category(name=category_name))

    db.commit()
    return RedirectResponse(url="/categories", status_code=303)


@app.post("/categories/{category_id}/delete")
def delete_category(category_id: int, db: Session = Depends(get_db)) -> Response:
    category = db.get(Category, category_id)
    if not category or category.parent_id is not None:
        raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
    tasks_in_category = db.scalar(
        select(func.count()).select_from(Task).where(Task.category == category.name)
    )
    if tasks_in_category:
        raise HTTPException(
            status_code=400,
            detail="Kategorie kann nicht gelöscht werden, solange ihr Aufgaben zugeordnet sind.",
        )
    db.delete(category)
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


@app.post("/categories/{category_id}/subcategories/{subcategory_id}/rename")
def rename_subcategory(
    category_id: int,
    subcategory_id: int,
    name: str = Form(...),
    db: Session = Depends(get_db),
) -> Response:
    parent = db.get(Category, category_id)
    if not parent or parent.parent_id is not None:
        raise HTTPException(status_code=404, detail="Kategorie nicht gefunden")
    subcategory = db.get(Category, subcategory_id)
    if not subcategory or subcategory.parent_id != parent.id:
        raise HTTPException(status_code=404, detail="Unterkategorie nicht gefunden")
    new_name = name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Unterkategorie benötigt einen Namen.")
    existing = db.scalars(
        select(Category)
        .where(Category.parent_id == parent.id)
        .where(Category.name == new_name)
        .where(Category.id != subcategory.id)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Unterkategorie existiert bereits.")
    if subcategory.name != new_name:
        old_name = subcategory.name
        for task in db.scalars(
            select(Task)
            .where(Task.category == parent.name)
            .where(Task.subcategory == old_name)
        ).all():
            task.subcategory = new_name
        subcategory.name = new_name

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
def create_exam(
    configuration_id: int = Form(...),
    group_id: Optional[str] = Form(None),
    demo_label: str = Form("Testmodus"),
    db: Session = Depends(get_db),
) -> Response:
    try:
        group_pk = int(group_id) if group_id else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Ungültige Gruppe ausgewählt.")

    demo_name = demo_label.strip() or "Testmodus"
    try:
        exam = generate_exam(
            db,
            configuration_id=configuration_id,
            group_id=group_pk,
            demo_label=demo_name if group_pk is None else None,
        )
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
