"""store run-level offsets for S3 range reads

Revision ID: b7f0f5e2b3a1
Revises: 26a9efc758a0
Create Date: 2026-01-16 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b7f0f5e2b3a1"
down_revision: Union[str, None] = "26a9efc758a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "runs",
        sa.Column("s3_key", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "runs",
        sa.Column("start_offset", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "runs",
        sa.Column("end_offset", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.alter_column("runs", "s3_key", server_default=None)
    op.alter_column("runs", "start_offset", server_default=None)
    op.alter_column("runs", "end_offset", server_default=None)

    op.drop_column("runs", "inputs")
    op.drop_column("runs", "outputs")
    op.drop_column("runs", "metadata")


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column("runs", sa.Column("metadata", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("outputs", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("inputs", sa.Text(), nullable=True))

    op.drop_column("runs", "end_offset")
    op.drop_column("runs", "start_offset")
    op.drop_column("runs", "s3_key")
