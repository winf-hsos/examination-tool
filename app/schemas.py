from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class StudentBase(BaseModel):
    full_name: str = Field(..., min_length=1, max_length=255)


class StudentRead(StudentBase):
    id: int

    class Config:
        orm_mode = True


class StudentGroupRead(BaseModel):
    id: int
    label: str
    students: List[StudentRead]

    class Config:
        orm_mode = True


class TaskBase(BaseModel):
    title: str
    category: str
    subcategory: Optional[str]
    statement_markdown: str
    hints_markdown: Optional[str]
    solution_markdown: Optional[str]
    dependency_ids: List[int] = []


class TaskCreate(TaskBase):
    pass


class TaskUpdate(TaskBase):
    pass


class TaskRead(BaseModel):
    id: int
    title: str
    category: str
    subcategory: Optional[str]
    statement_markdown: str
    hints_markdown: Optional[str]
    solution_markdown: Optional[str]
    dependencies: List[int]

    class Config:
        orm_mode = True


class CategoryRequirementBase(BaseModel):
    category: str
    required_count: int


class CategoryRequirementCreate(CategoryRequirementBase):
    pass


class CategoryRequirementRead(CategoryRequirementBase):
    id: int

    class Config:
        orm_mode = True


class ExamTaskOut(BaseModel):
    task_id: int
    title: str
    category: str
    subcategory: Optional[str]
    statement_html: str
    hints_html: Optional[str]
    solution_html: Optional[str]
    position: int


class ExamSessionOut(BaseModel):
    exam_id: int
    group: StudentGroupRead
    tasks: List[ExamTaskOut]
    started_at: datetime

