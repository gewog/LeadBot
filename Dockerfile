FROM python:3.11-slim

WORKDIR /app

# Устанавливаем зависимости системы (по минимуму)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Копируем файлы зависимостей отдельно для кеширования
COPY requirements.txt /app/requirements.txt

RUN pip install --no-cache-dir -r requirements.txt

# Копируем остальной код проекта
COPY . /app

# .env не должен попадать в образ, он монтируется снаружи через docker-compose/env

CMD ["python", "bot.py"]

