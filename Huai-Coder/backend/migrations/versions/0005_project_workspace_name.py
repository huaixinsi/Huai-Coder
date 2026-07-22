"""persist the bound folder name on each project

Revision ID: 0005_project_workspace_name
Revises: 0004_memory_context
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_project_workspace_name"
down_revision = "0004_memory_context"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projects", sa.Column("workspace_name", sa.String(255), nullable=True))


def downgrade():
    op.drop_column("projects", "workspace_name")
