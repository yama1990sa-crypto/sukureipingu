# Playwright(Chromium)がプリインストール済みの公式イメージを使用
FROM mcr.microsoft.com/playwright/python:v1.44.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render等のクラウドは PORT 環境変数でリッスンポートを指定してくる
ENV PORT=10000
EXPOSE 10000

CMD ["python", "app.py"]
