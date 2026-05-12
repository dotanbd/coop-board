"""add user_id to assignments

Revision ID: 7884d67d7e28
Revises: 
Create Date: 2026-05-07 18:33:29.327386

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7884d67d7e28'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # copy the data, add the Foreign Key constraint, and swap the tables
    with op.batch_alter_table('assignments', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_assignments_users_id', 'users', ['user_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('assignments', schema=None) as batch_op:
        batch_op.drop_constraint('fk_assignments_users_id', type_='foreignkey')
        batch_op.drop_column('user_id')
