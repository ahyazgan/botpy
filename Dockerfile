FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bot varsayılan olarak çalışır; API de açılırsa CMD değiştirin
CMD ["python", "arb_bot.py"]
