from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.routing import Route

from app.config import settings
from app.metrics import get_metrics_content, http_request_duration_seconds, http_requests_total

# Database setup
engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class ItemDB(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Nullable to match the schema of databases created by earlier versions.
    title: Mapped[str | None] = mapped_column(index=True)
    description: Mapped[str | None]


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


# Pydantic models
class ItemCreate(BaseModel):
    title: str
    description: str


class Item(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    title: str
    description: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)


def route_label(request: Request) -> str:
    """The matched route pattern (e.g. /items/{item_id}), so metric labels stay bounded."""
    route = request.scope.get("route")
    return route.path if isinstance(route, Route) else "unmatched"


@app.middleware("http")
async def record_metrics(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = perf_counter()
    status = 500  # if the handler raises, the client gets a 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        endpoint = route_label(request)
        http_requests_total.labels(method=request.method, endpoint=endpoint, status=status).inc()
        http_request_duration_seconds.labels(method=request.method, endpoint=endpoint).observe(
            perf_counter() - start
        )


@app.get("/")
async def root() -> dict[str, str]:
    return {"message": "DevOps Platform API"}


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


# Endpoints that touch the database are plain `def`: FastAPI runs them in a threadpool,
# so a slow query doesn't block the event loop.
@app.get("/readyz")
def readyz(db: DB) -> dict[str, object]:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Database not available") from exc
    return {"status": "ready", "dependencies": {"database": "connected"}}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics() -> str:
    content, _ = get_metrics_content()
    return content.decode("utf-8")


def get_item_or_404(db: Session, item_id: int) -> ItemDB:
    db_item = db.get(ItemDB, item_id)
    if db_item is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return db_item


@app.post("/items", status_code=201)
def create_item(item: ItemCreate, db: DB) -> Item:
    db_item = ItemDB(title=item.title, description=item.description)
    db.add(db_item)
    db.commit()
    db.refresh(db_item)
    return Item.model_validate(db_item)


@app.get("/items")
def list_items(db: DB) -> list[Item]:
    return [Item.model_validate(item) for item in db.scalars(select(ItemDB))]


@app.get("/items/{item_id}")
def get_item(item_id: int, db: DB) -> Item:
    return Item.model_validate(get_item_or_404(db, item_id))


@app.put("/items/{item_id}")
def update_item(item_id: int, item: ItemCreate, db: DB) -> Item:
    db_item = get_item_or_404(db, item_id)
    db_item.title = item.title
    db_item.description = item.description
    db.commit()
    db.refresh(db_item)
    return Item.model_validate(db_item)


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int, db: DB) -> Response:
    db.delete(get_item_or_404(db, item_id))
    db.commit()
    return Response(status_code=204)
