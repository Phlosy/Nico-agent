"""SQLAlchemy declarative base and future session boundary.

Goal B intentionally has no domain tables. Goal C models will inherit from
this base and repository sessions will apply TenantContext before queries.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
