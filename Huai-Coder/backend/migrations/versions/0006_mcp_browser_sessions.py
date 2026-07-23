"""persist logical browser MCP sessions

Revision ID: 0006_mcp_browser_sessions
Revises: 0005_project_workspace_name
"""
from alembic import op
import sqlalchemy as sa


revision = "0006_mcp_browser_sessions"
down_revision = "0005_project_workspace_name"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "browser_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_key", sa.String(100), nullable=False, unique=True),
        sa.Column("server_id", sa.String(64), nullable=False),
        sa.Column("linked_session_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("process_id", sa.String(100), nullable=True),
        sa.Column("gateway_session_id", sa.String(200), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="created"),
        sa.Column("current_url", sa.String(1000), nullable=True),
        sa.Column("persistent_profile", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["linked_session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="SET NULL"),
    )


def downgrade():
    op.drop_table("browser_sessions")
