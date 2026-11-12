from fastapi import FastAPI

from app.routers import history, products

app = FastAPI(
    title="MoneyTor",
    version="0.3.0",
    description="Monitor de precios MoneyTor (etapa 3: revisión e historial de precios).",
)
app.include_router(products.router)
app.include_router(history.router)
