FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1

WORKDIR /app

# Install the shared runtime dependency once for both tools.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Package both interactive clients into the same image.
COPY publisher /app/publisher
COPY subscriber /app/subscriber