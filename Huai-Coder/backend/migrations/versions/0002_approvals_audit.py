"""add approvals and audit logs

Revision ID: 0002_approvals_audit
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_approvals_audit"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("approvals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(80), nullable=False), sa.Column("arguments", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False), sa.Column("risk_reason", sa.Text(), nullable=False),
        sa.Column("target_path", sa.String(500)), sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resolution_reason", sa.Text()), sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("resolved_at", sa.DateTime(timezone=True)))
    op.create_table("audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False), sa.Column("tool_name", sa.String(80)), sa.Column("details", sa.Text(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

def downgrade():
    op.drop_table("audit_logs")
    op.drop_table("approvals")
