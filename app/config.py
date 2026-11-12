from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DATABASE_URL: str
    # Solo la usan las pruebas; la API nunca se conecta a esta base.
    TEST_DATABASE_URL: str | None = None

    # Obtención de precios (etapa 2).
    HTTP_CONNECT_TIMEOUT: float = 5.0
    HTTP_TIMEOUT: float = 10.0  # lectura/escritura por operación
    HTTP_TOTAL_TIMEOUT: float = 15.0  # plazo total de la petición (anti slow-drip)
    MAX_RESPONSE_BYTES: int = 1_048_576
    USER_AGENT: str = "MoneyTor/0.2 (+https://github.com/Carflox621/MoneyTor-)"
    CHECK_MIN_INTERVAL_SECONDS: int = 60

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
