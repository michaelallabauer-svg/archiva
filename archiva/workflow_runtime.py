"""Workflow runtime service for starting and executing workflow instances."""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from archiva.models import (
    Document,
    WorkflowDefinition,
    WorkflowHistoryEvent,
    WorkflowInstance,
    WorkflowStepDefinition,
    WorkflowTask,
    WorkflowTransitionDefinition,
)

ACTIVE_WORKFLOW_STATUS = "active"
COMPLETED_WORKFLOW_STATUS = "completed"
CANCELLED_WORKFLOW_STATUS = "cancelled"
OPEN_TASK_STATUS = "open"
COMPLETED_TASK_STATUS = "completed"
CANCELLED_TASK_STATUS = "cancelled"


class WorkflowRuntimeError(ValueError):
    """Raised when a workflow runtime action is not valid."""


def active_instances_for_document(db: Session, document_id: UUID) -> list[WorkflowInstance]:
    return (
        db.query(WorkflowInstance)
        .where(
            WorkflowInstance.subject_kind == "document",
            WorkflowInstance.subject_id == document_id,
            WorkflowInstance.status == ACTIVE_WORKFLOW_STATUS,
        )
        .order_by(WorkflowInstance.started_at.desc(), WorkflowInstance.created_at.desc())
        .all()
    )


def _first_step(workflow_definition: WorkflowDefinition) -> WorkflowStepDefinition | None:
    sorted_steps = sorted(workflow_definition.steps, key=lambda item: (item.order, item.name.lower(), str(item.id)))
    return sorted_steps[0] if sorted_steps else None


def _close_open_tasks(db: Session, instance: WorkflowInstance, *, status: str, now: datetime) -> None:
    for task in (
        db.query(WorkflowTask)
        .where(WorkflowTask.workflow_instance_id == instance.id, WorkflowTask.status == OPEN_TASK_STATUS)
        .all()
    ):
        task.status = status
        task.completed_at = now
        db.add(task)


def _create_task_for_step(instance: WorkflowInstance, step: WorkflowStepDefinition, *, now: datetime) -> WorkflowTask:
    due_at = now + timedelta(days=step.due_in_days) if step.due_in_days else None
    task = WorkflowTask(
        workflow_instance_id=instance.id,
        step_id=step.id,
        assignment_target_id=step.assignment_target_id,
        status=OPEN_TASK_STATUS,
        due_at=due_at,
    )
    return task


def _add_history(
    db: Session,
    instance: WorkflowInstance,
    *,
    event_type: str,
    actor_label: str,
    comment: str | None = None,
    from_step_id: UUID | None = None,
    to_step_id: UUID | None = None,
    transition_id: UUID | None = None,
) -> WorkflowHistoryEvent:
    event = WorkflowHistoryEvent(
        workflow_instance_id=instance.id,
        event_type=event_type,
        from_step_id=from_step_id,
        to_step_id=to_step_id,
        transition_id=transition_id,
        comment=comment or None,
        actor_label=actor_label,
    )
    db.add(event)
    return event


def start_workflow_for_document(
    db: Session,
    *,
    document_id: UUID,
    workflow_definition_id: UUID,
    actor_label: str,
    comment: str | None = None,
) -> WorkflowInstance:
    document = db.query(Document).where(Document.id == document_id, Document.deleted_at.is_(None)).first()
    if not document:
        raise WorkflowRuntimeError("Dokument nicht gefunden")
    workflow = db.query(WorkflowDefinition).where(WorkflowDefinition.id == workflow_definition_id, WorkflowDefinition.is_active.is_(True)).first()
    if not workflow:
        raise WorkflowRuntimeError("Workflow nicht gefunden oder inaktiv")
    first_step = _first_step(workflow)
    if not first_step:
        raise WorkflowRuntimeError("Workflow hat noch keine Schritte")

    instance = WorkflowInstance(
        workflow_definition_id=workflow.id,
        subject_kind="document",
        subject_id=document.id,
        current_step_id=first_step.id,
        status=ACTIVE_WORKFLOW_STATUS,
        title=workflow.name,
    )
    db.add(instance)
    db.flush()
    db.add(_create_task_for_step(instance, first_step, now=datetime.utcnow()))
    _add_history(db, instance, event_type="started", actor_label=actor_label, comment=comment, to_step_id=first_step.id)
    db.commit()
    db.refresh(instance)
    return instance


def transition_workflow(
    db: Session,
    *,
    instance_id: UUID,
    transition_id: UUID,
    actor_label: str,
    comment: str | None = None,
) -> WorkflowInstance:
    instance = db.query(WorkflowInstance).where(WorkflowInstance.id == instance_id).first()
    if not instance or instance.status != ACTIVE_WORKFLOW_STATUS:
        raise WorkflowRuntimeError("Aktive Workflow-Instanz nicht gefunden")
    transition = db.query(WorkflowTransitionDefinition).where(WorkflowTransitionDefinition.id == transition_id).first()
    if not transition or transition.workflow_definition_id != instance.workflow_definition_id:
        raise WorkflowRuntimeError("Transition nicht gefunden")
    if transition.from_step_id != instance.current_step_id:
        raise WorkflowRuntimeError("Transition passt nicht zum aktuellen Schritt")

    now = datetime.utcnow()
    _close_open_tasks(db, instance, status=COMPLETED_TASK_STATUS, now=now)
    instance.current_step_id = transition.to_step_id
    db.add(instance)
    db.add(_create_task_for_step(instance, transition.to_step, now=now))
    _add_history(
        db,
        instance,
        event_type="transitioned",
        actor_label=actor_label,
        comment=comment,
        from_step_id=transition.from_step_id,
        to_step_id=transition.to_step_id,
        transition_id=transition.id,
    )
    db.commit()
    db.refresh(instance)
    return instance


def complete_workflow(
    db: Session,
    *,
    instance_id: UUID,
    actor_label: str,
    comment: str | None = None,
) -> WorkflowInstance:
    instance = db.query(WorkflowInstance).where(WorkflowInstance.id == instance_id).first()
    if not instance or instance.status != ACTIVE_WORKFLOW_STATUS:
        raise WorkflowRuntimeError("Aktive Workflow-Instanz nicht gefunden")
    now = datetime.utcnow()
    _close_open_tasks(db, instance, status=COMPLETED_TASK_STATUS, now=now)
    from_step_id = instance.current_step_id
    instance.status = COMPLETED_WORKFLOW_STATUS
    instance.completed_at = now
    instance.current_step_id = None
    db.add(instance)
    _add_history(db, instance, event_type="completed", actor_label=actor_label, comment=comment, from_step_id=from_step_id)
    db.commit()
    db.refresh(instance)
    return instance


def cancel_workflow(
    db: Session,
    *,
    instance_id: UUID,
    actor_label: str,
    comment: str | None = None,
) -> WorkflowInstance:
    instance = db.query(WorkflowInstance).where(WorkflowInstance.id == instance_id).first()
    if not instance or instance.status != ACTIVE_WORKFLOW_STATUS:
        raise WorkflowRuntimeError("Aktive Workflow-Instanz nicht gefunden")
    now = datetime.utcnow()
    _close_open_tasks(db, instance, status=CANCELLED_TASK_STATUS, now=now)
    from_step_id = instance.current_step_id
    instance.status = CANCELLED_WORKFLOW_STATUS
    instance.cancelled_at = now
    instance.current_step_id = None
    db.add(instance)
    _add_history(db, instance, event_type="cancelled", actor_label=actor_label, comment=comment, from_step_id=from_step_id)
    db.commit()
    db.refresh(instance)
    return instance
