FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

COPY agent_b/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runtime_api ./runtime_api
COPY agent_b .

CMD ["python", "worker.py"]
