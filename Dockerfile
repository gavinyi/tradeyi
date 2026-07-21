FROM python:3.11-slim

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN uv pip install --system ".[postgres]"
COPY . /app
ENV PYTHONPATH=/app/src ENABLE_PAPER_ORDERS=false DATABASE_BACKEND=sqlite
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
