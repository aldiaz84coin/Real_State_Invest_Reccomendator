FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Las dependencias van en su propia capa para que un cambio de codigo no
# obligue a reinstalarlas en cada despliegue.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY scripts ./scripts

# Punto de montaje del volumen de Fly, donde vive la base SQLite.
RUN mkdir -p /data
ENV DATABASE_URL=sqlite:////data/investment.db

EXPOSE 8080

# Un solo worker: SQLite escribe mejor sin varios procesos compitiendo, y para
# esta carga sobra. Si algun dia se pasa a Postgres, se pueden subir.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
