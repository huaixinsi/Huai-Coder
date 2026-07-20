"""add plan execute tables

Revision ID: 0003_plan_execute
Revises: 0002_approvals_audit
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_plan_execute"
down_revision = "0002_approvals_audit"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table("plans", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False), sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False), sa.Column("run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False), sa.Column("goal", sa.Text(), nullable=False), sa.Column("summary", sa.Text(), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()), sa.Column("confirmed_at", sa.DateTime(timezone=True)), sa.Column("cancelled_at", sa.DateTime(timezone=True)))
    op.create_table("tasks", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False), sa.Column("task_key", sa.String(100), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("description", sa.Text(), nullable=False), sa.Column("task_type", sa.String(50), nullable=False), sa.Column("status", sa.String(30), nullable=False), sa.Column("input_data", sa.Text(), nullable=False), sa.Column("output_data", sa.Text(), nullable=False), sa.Column("error_type", sa.String(40)), sa.Column("error_message", sa.Text()), sa.Column("retry_count", sa.Integer(), nullable=False), sa.Column("max_retries", sa.Integer(), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("finished_at", sa.DateTime(timezone=True)))
    op.create_table("task_dependencies", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="CASCADE"), nullable=False), sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False), sa.Column("depends_on_task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False), sa.Column("dependency_type", sa.String(30), nullable=False))
    op.add_column("agent_runs", sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="SET NULL")))
    op.add_column("approvals", sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="CASCADE")))
    op.add_column("approvals", sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE")))
    op.add_column("audit_logs", sa.Column("plan_id", sa.Integer(), sa.ForeignKey("plans.id", ondelete="CASCADE")))
    op.add_column("audit_logs", sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE")))

def downgrade():
    for table, columns in (("audit_logs", ("task_id", "plan_id")), ("approvals", ("task_id", "plan_id")), ("agent_runs", ("plan_id",))):
        for column in columns: op.drop_column(table, column)
    op.drop_table("task_dependencies"); op.drop_table("tasks"); op.drop_table("plans")
