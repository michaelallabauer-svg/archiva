"""add workflow runtime tables

Revision ID: 003_add_workflow_runtime
Revises: 002_add_soft_delete_cabinet_register
Create Date: 2026-05-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "003_add_workflow_runtime"
down_revision: Union[str, None] = "002_add_soft_delete_cabinet_register"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "workflow_instances",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_definition_id", sa.UUID(), nullable=False),
        sa.Column("subject_kind", sa.String(length=50), nullable=False, server_default="document"),
        sa.Column("subject_id", sa.UUID(), nullable=False),
        sa.Column("current_step_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="active"),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["current_step_id"], ["workflow_step_definitions.id"]),
        sa.ForeignKeyConstraint(["workflow_definition_id"], ["workflow_definitions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_instances_subject", "workflow_instances", ["subject_kind", "subject_id"])
    op.create_index("ix_workflow_instances_status", "workflow_instances", ["status"])
    op.create_index("ix_workflow_instances_workflow_definition_id", "workflow_instances", ["workflow_definition_id"])
    op.create_index("ix_workflow_instances_current_step_id", "workflow_instances", ["current_step_id"])

    op.create_table(
        "workflow_tasks",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_instance_id", sa.UUID(), nullable=False),
        sa.Column("step_id", sa.UUID(), nullable=False),
        sa.Column("assignment_target_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="open"),
        sa.Column("due_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["assignment_target_id"], ["assignment_targets.id"]),
        sa.ForeignKeyConstraint(["step_id"], ["workflow_step_definitions.id"]),
        sa.ForeignKeyConstraint(["workflow_instance_id"], ["workflow_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_tasks_instance_id", "workflow_tasks", ["workflow_instance_id"])
    op.create_index("ix_workflow_tasks_step_id", "workflow_tasks", ["step_id"])
    op.create_index("ix_workflow_tasks_status", "workflow_tasks", ["status"])
    op.create_index("ix_workflow_tasks_assignment_target_id", "workflow_tasks", ["assignment_target_id"])

    op.create_table(
        "workflow_history_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workflow_instance_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("from_step_id", sa.UUID(), nullable=True),
        sa.Column("to_step_id", sa.UUID(), nullable=True),
        sa.Column("transition_id", sa.UUID(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.ForeignKeyConstraint(["from_step_id"], ["workflow_step_definitions.id"]),
        sa.ForeignKeyConstraint(["to_step_id"], ["workflow_step_definitions.id"]),
        sa.ForeignKeyConstraint(["transition_id"], ["workflow_transition_definitions.id"]),
        sa.ForeignKeyConstraint(["workflow_instance_id"], ["workflow_instances.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_workflow_history_events_instance_id", "workflow_history_events", ["workflow_instance_id"])
    op.create_index("ix_workflow_history_events_event_type", "workflow_history_events", ["event_type"])
    op.create_index("ix_workflow_history_events_created_at", "workflow_history_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_history_events_created_at", table_name="workflow_history_events")
    op.drop_index("ix_workflow_history_events_event_type", table_name="workflow_history_events")
    op.drop_index("ix_workflow_history_events_instance_id", table_name="workflow_history_events")
    op.drop_table("workflow_history_events")

    op.drop_index("ix_workflow_tasks_assignment_target_id", table_name="workflow_tasks")
    op.drop_index("ix_workflow_tasks_status", table_name="workflow_tasks")
    op.drop_index("ix_workflow_tasks_step_id", table_name="workflow_tasks")
    op.drop_index("ix_workflow_tasks_instance_id", table_name="workflow_tasks")
    op.drop_table("workflow_tasks")

    op.drop_index("ix_workflow_instances_current_step_id", table_name="workflow_instances")
    op.drop_index("ix_workflow_instances_workflow_definition_id", table_name="workflow_instances")
    op.drop_index("ix_workflow_instances_status", table_name="workflow_instances")
    op.drop_index("ix_workflow_instances_subject", table_name="workflow_instances")
    op.drop_table("workflow_instances")
