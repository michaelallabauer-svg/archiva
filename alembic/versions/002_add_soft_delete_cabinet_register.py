"""add soft-delete metadata columns

Revision ID: 002_add_soft_delete_cabinet_register
Revises: 3d584ba10ddc
Create Date: 2026-04-30

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "002_add_soft_delete_cabinet_register"
down_revision: Union[str, None] = "3d584ba10ddc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = ("cabinets", "registers", "documents")


def upgrade() -> None:
    for table_name in TABLES:
        op.add_column(table_name, sa.Column("deleted_at", sa.DateTime(), nullable=True))
        op.add_column(
            table_name,
            sa.Column(
                "deleted_by_user_id",
                sa.UUID(),
                sa.ForeignKey("users.id", name=f"fk_{table_name}_deleted_by_user_id"),
                nullable=True,
            ),
        )
        op.add_column(table_name, sa.Column("deleted_by_label", sa.String(length=255), nullable=True))
        op.create_index(f"ix_{table_name}_deleted_at", table_name, ["deleted_at"], unique=False)
        op.create_index(
            f"ix_{table_name}_deleted_by_user_id",
            table_name,
            ["deleted_by_user_id"],
            unique=False,
        )


def downgrade() -> None:
    for table_name in reversed(TABLES):
        op.drop_index(f"ix_{table_name}_deleted_by_user_id", table_name=table_name)
        op.drop_index(f"ix_{table_name}_deleted_at", table_name=table_name)
        op.drop_column(table_name, "deleted_by_label")
        op.drop_column(table_name, "deleted_by_user_id")
        op.drop_column(table_name, "deleted_at")
