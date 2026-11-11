# MoneyTor

Monitor de precios de productos. El objetivo del proyecto es que una persona registre la URL de un
producto, y que el sistema —en etapas posteriores— obtenga su precio de fuentes permitidas, guarde el
historial, detecte cuándo se alcanza un precio objetivo y notifique.

> **Estado actual: etapa 1.** Hoy MoneyTor es una API REST para **registrar y consultar productos**
> (URL normalizada, nombre opcional y precio objetivo opcional) con persistencia en PostgreSQL,
> migraciones y pruebas automatizadas. **No obtiene precios todavía** (no hay scraping, historial,
> tareas programadas ni notificaciones). Ver [Estado y hoja de ruta](#estado-y-hoja-de-ruta).

## Stack

| Área | Tecnología |
|---|---|
| Lenguaje | Python 3.11+ (desarrollado con 3.12) |
| API | FastAPI + Pydantic v2 (documentación OpenAPI automática en `/docs`) |
| Base de datos | PostgreSQL (probado con 18.4) |
| Acceso a datos | SQLAlchemy 2.1 (síncrono, estilo 2.0) + psycopg 3 |
| Migraciones | Alembic |
| Configuración | pydantic-settings (`.env` / variables de entorno) |
| Calidad | pytest, pytest-cov, Ruff, GitHub Actions |

## Arquitectura

Deliberadamente simple: no hay capas que no tengan lógica propia que proteger.

```
app/
  main.py            FastAPI() + include_router
  config.py          Settings (lee DATABASE_URL / TEST_DATABASE_URL del entorno o de .env)
  db.py              engine, SessionLocal, Base (con convención de nombres), get_db()
  models.py          Product
  schemas.py         ProductCreate (valida y normaliza), ProductOut
  urls.py            normalize_url() y store_from_url(): lógica pura, sin red ni BD
  routers/products.py
alembic/             env.py y versions/0001_create_products.py
tests/               conftest.py, test_urls.py, test_products.py, test_migrations.py
.github/workflows/   ci.yml
```

Flujo de `POST /products`: Pydantic valida y normaliza la URL → el router deriva `store` →
`INSERT` → si salta el `UNIQUE(url)` de PostgreSQL se responde `409`. La unicidad la decide la base de
datos (no un `SELECT` previo), por lo que es segura ante peticiones concurrentes.

## Requisitos previos

- Windows con PowerShell (las instrucciones están verificadas en Windows; en Linux/macOS cambian solo las rutas).
- Python 3.11 o superior.
- PostgreSQL instalado localmente. En Windows, `psql.exe` suele estar en
  `C:\Program Files\PostgreSQL\<versión>\bin\` y normalmente **no** está en el `PATH`.
- No se necesita Docker.

## Instalación y ejecución (Windows / PowerShell)

### 1. Entorno virtual y dependencias

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
```

`requirements.txt` contiene solo lo necesario para ejecutar la API; `requirements-dev.txt` añade
pruebas, cobertura y Ruff. Todas las versiones están fijadas.

### 2. Configurar PostgreSQL

Se usa un **rol dedicado sin privilegios especiales** y dos bases independientes: `moneytor`
(desarrollo) y `moneytor_test` (pruebas). Las pruebas nunca tocan `moneytor`. No se modifica ninguna
otra base ni la configuración global del servidor.

Conéctate como administrador; `psql` pedirá su contraseña de forma interactiva:

```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" -h localhost -U postgres -d postgres
```

Dentro de `psql`:

```sql
CREATE ROLE moneytor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
\password moneytor
CREATE DATABASE moneytor      OWNER moneytor;
CREATE DATABASE moneytor_test OWNER moneytor;
\q
```

`\password moneytor` pide la contraseña **sin mostrarla** y la envía ya cifrada, de modo que no queda
escrita en comandos, historial ni registros. Elige una contraseña propia.

### 3. Variables de entorno

```powershell
Copy-Item .env.example .env
```

Edita `.env` y reemplaza `CAMBIAME` por la contraseña del rol `moneytor` (si contiene `@ : / %`,
codifícala como URL). `.env` está en `.gitignore` y nunca debe publicarse. En CI no se usa `.env`:
las mismas variables se definen en el entorno.

### 4. Migraciones y servidor

```powershell
.\.venv\Scripts\alembic upgrade head
.\.venv\Scripts\uvicorn app.main:app --reload
```

API en <http://127.0.0.1:8000> y documentación interactiva en <http://127.0.0.1:8000/docs>.

## API

| Método | Ruta | Respuestas |
|---|---|---|
| `POST` | `/products` | `201` creado · `409` URL ya registrada (incluye `existing_id`) · `422` datos inválidos |
| `GET` | `/products?limit=50&offset=0` | `200` (orden por `id`; `limit` 1–100) · `422` paginación inválida |
| `GET` | `/products/{id}` | `200` · `404` no existe · `422` id no válido |

Ejemplo (PowerShell):

```powershell
curl.exe -X POST http://127.0.0.1:8000/products -H "Content-Type: application/json" `
  -d '{\"url\":\"https://WWW.Tienda.com/producto/?utm_source=fb#reviews\",\"name\":\"Cafetera\",\"target_price\":\"1299.00\"}'
```

```json
{ "id": 1, "url": "https://tienda.com/producto", "name": "Cafetera", "store": "tienda.com",
  "target_price": "1299.00", "created_at": "2026-11-11T17:33:42.762134-06:00" }
```

Repetir el mismo producto (aunque cambien `utm_*`, el fragmento o el orden de los parámetros):

```json
{ "detail": { "message": "La URL ya está registrada", "existing_id": 1 } }
```

```powershell
curl.exe http://127.0.0.1:8000/products
curl.exe http://127.0.0.1:8000/products/1
```

### Reglas de entrada

- **url** (obligatoria): solo `http`/`https`, máx. 2048 caracteres, sin credenciales (`user:pass@`),
  sin `localhost`, IPs privadas/loopback/link-local ni hosts sin punto.
- **name** (opcional): se recortan los espacios; vacío → `null`; máx. 200 caracteres.
- **target_price** (opcional): decimal exacto > 0, hasta 12 dígitos y 2 decimales; se rechazan `NaN`,
  `Infinity`, negativos y más de 2 decimales. Se devuelve como texto (`"10.50"`).
- No se aceptan campos desconocidos.

### Normalización de URL

Conservadora (`app/urls.py`): esquema y host en minúsculas, sin `www.`, sin puerto por defecto,
IDN→punycode, sin fragmento, sin barra final, percent-encoding canónico, parámetros ordenados por
nombre y eliminación **solo** de `utm_*`, `gclid` y `fbclid`. El resto (incluido `ref`) se conserva:
un duplicado se limpia después; un parámetro de identidad borrado apuntaría a otro producto.

Además, PostgreSQL refuerza la integridad con `UNIQUE(url)` y `CHECK` sobre longitud de URL, `store`
no vacío, longitud de `name` y `target_price > 0`.

## Pruebas, calidad y cobertura

```powershell
.\.venv\Scripts\python -m pytest --cov          # pruebas + cobertura (umbral 90 %)
.\.venv\Scripts\ruff check .                    # análisis estático
.\.venv\Scripts\ruff format --check .           # formato
```

- Las pruebas usan **exclusivamente** `TEST_DATABASE_URL`. Si no apunta a una base llamada
  `moneytor_test`, o coincide con `DATABASE_URL`, la sesión aborta antes de tocar nada.
- El esquema de prueba se construye con las migraciones reales (`downgrade base` + `upgrade head`).
- Hay pruebas unitarias (normalización de URLs, incluidos rechazos de seguridad e idempotencia) y de
  integración (endpoints, validaciones, concurrencia real con 8 hilos, restricciones de PostgreSQL y
  ciclo de migraciones). Las advertencias de pytest se tratan como errores.
- Cobertura medida (con ramas): ~98 %. Lo único sin cubrir es `get_db()`, que las pruebas sustituyen a
  propósito por una sesión de la base de pruebas.

## Integración continua

`.github/workflows/ci.yml` ejecuta en cada `push` a `main` y en cada pull request: instalación de
dependencias, Ruff (análisis y formato), creación de la base de pruebas en un PostgreSQL efímero,
migraciones sobre base vacía y `pytest --cov`. No usa secretos ni depende de ninguna base local.

## Estado y hoja de ruta

| Etapa | Contenido | Estado |
|---|---|---|
| 0–1 | Registro y consulta de productos, normalización de URL, PostgreSQL, Alembic, pruebas | Implementada |
| 1.5 | Ruff, cobertura, CI | Configurada (el workflow aún no se ha ejecutado en GitHub) |
| 2 | Historial de precios + una fuente que permita acceso automatizado | Pendiente |
| 3 | Historial y estadísticas | Pendiente |
| 4 | Detección de precio objetivo | Pendiente |
| 5 | Revisiones periódicas | Pendiente |
| 6 | Notificaciones | Pendiente |
| 7+ | Navegador automatizado solo si una fuente permitida lo exige; multi-tienda | Pendiente |

Principio para el scraping futuro: respetar `robots.txt` y los términos de cada sitio, preferir APIs
autorizadas y datos estructurados, y **no** intentar evadir CAPTCHAs ni bloqueos.

## Limitaciones conocidas

- No hay autenticación ni usuarios; cualquiera con acceso a la API puede registrar productos.
- No existen `PUT`/`PATCH`/`DELETE`: los productos no se pueden modificar ni borrar desde la API.
- Quitar `www.` asume que `www.x.com` y `x.com` son el mismo sitio.
- La validación contra IPs privadas se hace sobre la URL escrita; no resuelve DNS (la API no hace
  peticiones salientes todavía). Cuando exista el scraping, la resolución DNS y las redirecciones
  deberán revalidarse en el momento de la petición y limitarse a tiendas soportadas.
- Sin precios ni moneda: `target_price` se guarda, pero aún no se compara con nada.
- Probado solo en Windows con PostgreSQL 18; el CI usa Linux con PostgreSQL 18 pero aún no se ha ejecutado.
