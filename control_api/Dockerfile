FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY control_api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY control_api ./control_api
COPY runtime_api ./runtime_api

EXPOSE 8080

CMD ["python", "-m", "control_api.main"]
