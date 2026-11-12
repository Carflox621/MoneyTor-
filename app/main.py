from fastapi import FastAPI

from app.routers import products

app = FastAPI(
    title="MoneyTor",
    version="0.2.0",
    description="Monitor de precios MoneyTor (etapa 2: productos y revisión de precios).",
)
app.include_router(products.router)
