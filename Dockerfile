FROM python:3.11-slim

# tzdata нужен для расчёта времени по Москве, procps — для healthcheck через pgrep.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -ms /bin/bash appuser
WORKDIR /app

# Зависимости отдельным слоем — правка кода не пересобирает pip install.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN chown -R appuser:appuser /app
USER appuser

CMD ["python", "-u", "main.py"]
