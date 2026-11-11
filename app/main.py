from fastapi import FastAPI

from app.routers import products

app = FastAPI(
    title="MoneyTor",
    version="0.1.0",
    description="Registro de productos para el monitor de precios MoneyTor (etapa 1).",
)
app.include_router(products.router)
