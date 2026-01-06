FROM python:3.11-slim

# Создаем пользователя для приложения
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файл зависимостей
COPY requirements.txt .

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    libnss3 \
    libxss1 \
    libasound2 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libdrm2 \
    libgbm1 \
    libu2f-udev \
    libxshmfence1 \
    fonts-liberation \
    fonts-dejavu-core \
    ca-certificates \
    curl \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код приложения
COPY . .

# Создаем необходимые директории
RUN mkdir -p /app/instance
RUN mkdir -p /app/static/uploads/screenshots

# Устанавливаем права доступа к папкам
RUN chmod -R 755 /app/instance
RUN chmod -R 755 /app/static/uploads

# Устанавливаем права на запись для директории instance
RUN chmod 775 /app/instance
# Создаем файл-заглушку для установки прав по умолчанию
RUN touch /app/instance/.permissions && chmod 664 /app/instance/.permissions

# Передаем права пользователю appuser (ПОСЛЕ создания файлов)
RUN chown -R appuser:appuser /app/instance
RUN chown -R appuser:appuser /app/static/uploads
RUN chown -R appuser:appuser /app



# Открываем порт
EXPOSE 5000

# Устанавливаем переменные окружения
ENV FLASK_APP=app.py
ENV FLASK_ENV=development

# Переключаемся на пользователя appuser
USER appuser

# Запускаем приложение с правильным umask
CMD ["sh", "-c", "umask 002 && python -u app.py"]
# Создаем домашнюю директорию для пользователя и права
USER root
RUN mkdir -p /home/appuser && chown -R appuser:appuser /home/appuser
ENV HOME=/home/appuser
ENV XDG_RUNTIME_DIR=/tmp
USER appuser
