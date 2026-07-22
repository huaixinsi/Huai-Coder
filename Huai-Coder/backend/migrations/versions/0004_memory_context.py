"""add long-term memories and conversation summaries

Revision ID: 0004_memory_context
Revises: 0003_plan_execute
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_memory_context"
down_revision = "0003_plan_execute"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.Integer(), nullable=False),
        sa.Column("memory_type", sa.String(30), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False, server_default="0.700"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("source_session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="SET NULL")),
        sa.Column("source_message_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("embedding", sa.Text(), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("superseded_by", sa.Integer(), sa.ForeignKey("memories.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_memories_scope_status", "memories", ["scope_type", "scope_id", "status"])
    op.create_index("ix_memories_expiry", "memories", ["expires_at"])
    op.create_table(
        "conversation_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("covered_until_message_id", sa.Integer(), nullable=False),
        sa.Column("summary_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_name", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_conversation_summaries_session", "conversation_summaries", ["session_id", "summary_version"])
    op.create_table(
        "memory_audits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("memory_id", sa.Integer(), sa.ForeignKey("memories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("before_content", sa.Text()),
        sa.Column("after_content", sa.Text()),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade():
    op.drop_table("memory_audits")
    op.drop_index("ix_conversation_summaries_session", table_name="conversation_summaries")
    op.drop_table("conversation_summaries")
    op.drop_index("ix_memories_expiry", table_name="memories")
    op.drop_index("ix_memories_scope_status", table_name="memories")
    op.drop_table("memories")
