# Build from the repo root:  docker build -f backend/ec2/Dockerfile -t localstay .
FROM python:3.12-slim
WORKDIR /app
COPY backend/ec2/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/api/handler.py .
COPY backend/ec2/app.py .
COPY frontend/index.html .
RUN useradd -m app && chown -R app /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/health')"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
