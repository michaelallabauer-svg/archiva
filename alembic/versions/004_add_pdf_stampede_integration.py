"""add pdf stampede integration fields

Revision ID: 004_add_pdf_stampede_integration
Revises: 003_add_workflow_runtime
Create Date: 2026-05-06
"""

from alembic import op
import sqlalchemy as sa


revision = "004_add_pdf_stampede_integration"
down_revision = "003_add_workflow_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_types", sa.Column("file_type", sa.String(length=50), nullable=True))
    op.add_column("document_types", sa.Column("pdf_stampede_auto_stamp", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("document_types", sa.Column("pdf_stampede_template_id", sa.String(length=255), nullable=True))
    op.alter_column("document_types", "pdf_stampede_auto_stamp", server_default=None)

    op.add_column("documents", sa.Column("stamped_pdf_storage_path", sa.String(length=1000), nullable=True))
    op.add_column("documents", sa.Column("stamp_status", sa.String(length=50), nullable=True))
    op.add_column("documents", sa.Column("stamp_template_id", sa.String(length=255), nullable=True))
    op.add_column("documents", sa.Column("stamp_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "stamp_error")
    op.drop_column("documents", "stamp_template_id")
    op.drop_column("documents", "stamp_status")
    op.drop_column("documents", "stamped_pdf_storage_path")

    op.drop_column("document_types", "pdf_stampede_template_id")
    op.drop_column("document_types", "pdf_stampede_auto_stamp")
    op.drop_column("document_types", "file_type")
