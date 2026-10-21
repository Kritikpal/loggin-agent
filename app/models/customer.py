from sqlalchemy import Boolean, String, false
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAt, UuidPk


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[UuidPk]
    name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255), unique=True)  # NULLs never collide
    marketing_consent: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    created_at: Mapped[CreatedAt]
