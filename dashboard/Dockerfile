FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tributary/ tributary/
COPY dashboard/ dashboard/
RUN pip install --no-cache-dir ".[dashboard]"
EXPOSE 8080
CMD ["uvicorn", "dashboard.app:app", "--host", "0.0.0.0", "--port", "8080"]
