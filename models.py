from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
  pass


class User(Base):
  __tablename__ = "users"

  id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram ID
  username: Mapped[str | None] = mapped_column(String(32), nullable=True)
  balance: Mapped[float] = mapped_column(Float, default=0.0)
  referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
  created_at: Mapped[datetime] = mapped_column(
      DateTime, default=datetime.utcnow
  )


class Platform(Base):
  __tablename__ = "platforms"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  name: Mapped[str] = mapped_column(
      String(50), unique=True
  )  # Например: "Android", "iOS"

  softwares = relationship(
      "Software", back_populates="platform", cascade="all, delete-orphan"
  )


class Software(Base):
  __tablename__ = "softwares"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  platform_id: Mapped[int] = mapped_column(ForeignKey("platforms.id"))
  name: Mapped[str] = mapped_column(
      String(100)
  )  # Название софта, например: "Flexnote"

  platform = relationship("Platform", back_populates="softwares")
  products = relationship(
      "Product", back_populates="software", cascade="all, delete-orphan"
  )


class Product(Base):
  __tablename__ = "products"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  software_id: Mapped[int] = mapped_column(ForeignKey("softwares.id"))
  duration: Mapped[str] = mapped_column(
      String(50)
  )  # Тариф, например: "30 дней"
  price: Mapped[float] = mapped_column(Float)  # Цена в рублях

  software = relationship("Software", back_populates="products")
  keys = relationship(
      "LicenseKey", back_populates="product", cascade="all, delete-orphan"
  )


class LicenseKey(Base):
  __tablename__ = "license_keys"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
  key_string: Mapped[str] = mapped_column(String(255), unique=True)
  is_sold: Mapped[bool] = mapped_column(Boolean, default=False)

  product = relationship("Product", back_populates="keys")


class Purchase(Base):
  __tablename__ = "purchases"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
  product_name: Mapped[str] = mapped_column(String(100))
  key_issued: Mapped[str] = mapped_column(String(255))
  purchased_at: Mapped[datetime] = mapped_column(
      DateTime, default=datetime.utcnow
  )


class DepositRequest(Base):
  __tablename__ = "deposit_requests"

  id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
  user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
  amount: Mapped[float] = mapped_column(Float)
  status: Mapped[str] = mapped_column(
      String(20), default="pending"
  )  # pending, approved, rejected
  created_at: Mapped[datetime] = mapped_column(
      DateTime, default=datetime.utcnow
  )

