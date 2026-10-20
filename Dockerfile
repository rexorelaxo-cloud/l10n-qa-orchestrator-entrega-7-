FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY fixtures ./fixtures
COPY scripts ./scripts

# Usuario sin privilegios; /app/salida recibe los manifiestos de la importación simulada.
RUN useradd --create-home appuser && mkdir -p /app/salida && chown -R appuser /app/salida
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
