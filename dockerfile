FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir luma.oled psutil
WORKDIR /app
COPY . .
CMD ["python", "-u", "main.py"]
