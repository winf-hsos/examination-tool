from __future__ import annotations

from datetime import datetime
from typing import List, Sequence

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


task_dependencies = Table(
    "task_dependencies",
    Base.metadata,
    Column("task_id", ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "depends_on_task_id",
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class StudentGroup(Base):
    __tablename__ = "student_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    students: Mapped[List[Student]] = relationship(
        "Student",
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="joined",
    )
    exams: Mapped[List[ExamSession]] = relationship(
        "ExamSession", back_populates="group", cascade="all, delete-orphan"
    )


class Student(Base):
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    group_id: Mapped[int] = mapped_column(ForeignKey("student_groups.id", ondelete="CASCADE"))

    group: Mapped[StudentGroup] = relationship("StudentGroup", back_populates="students")


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    subcategory: Mapped[str | None] = mapped_column(String(255))
    statement_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    hints_markdown: Mapped[str | None] = mapped_column(Text)
    solution_markdown: Mapped[str | None] = mapped_column(Text)

    dependencies: Mapped[List[Task]] = relationship(
        "Task",
        secondary=task_dependencies,
        primaryjoin=id == task_dependencies.c.task_id,
        secondaryjoin=id == task_dependencies.c.depends_on_task_id,
        backref="dependents",
        lazy="joined",
    )
    images: Mapped[List["TaskImage"]] = relationship(
        "TaskImage",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskImage.position",
    )


class TaskImage(Base):
    __tablename__ = "task_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    task: Mapped["Task"] = relationship("Task", back_populates="images")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("name", "parent_id", name="uq_category_parent"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("categories.id", ondelete="CASCADE"),
        nullable=True,
    )

    parent: Mapped["Category"] = relationship(
        "Category",
        remote_side="Category.id",
        back_populates="children",
    )
    children: Mapped[List["Category"]] = relationship(
        "Category",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="Category.name",
    )


class CategoryRequirement(Base):
    __tablename__ = "category_requirements"
    __table_args__ = (UniqueConstraint("category", name="uq_category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    required_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class ExamSession(Base):
    __tablename__ = "exam_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("student_groups.id", ondelete="CASCADE"))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    group: Mapped[StudentGroup] = relationship("StudentGroup", back_populates="exams")
    assignments: Mapped[List[ExamTaskAssignment]] = relationship(
        "ExamTaskAssignment",
        back_populates="exam",
        order_by="ExamTaskAssignment.position",
        cascade="all, delete-orphan",
    )


class ExamTaskAssignment(Base):
    __tablename__ = "exam_task_assignments"
    __table_args__ = (UniqueConstraint("exam_id", "task_id", name="uq_exam_task"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exam_sessions.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    category: Mapped[str] = mapped_column(String(255), nullable=False)

    exam: Mapped[ExamSession] = relationship("ExamSession", back_populates="assignments")
    task: Mapped[Task] = relationship("Task")


__all__: Sequence[str] = (
    "Student",
    "StudentGroup",
    "Task",
    "TaskImage",
    "ExamSession",
    "ExamTaskAssignment",
    "CategoryRequirement",
    "Category",
    "task_dependencies",
)
