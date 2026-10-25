FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    CKDU_DEMO_OUTPUT_DIR=/app/demo_output

WORKDIR /app

COPY requirements-docker.txt /app/requirements-docker.txt
RUN python -m pip install --no-cache-dir --disable-pip-version-check -r /app/requirements-docker.txt

# Copy the checked-out project as-is, including untracked demo modules and fixtures.
COPY . /app

EXPOSE 8501

CMD ["streamlit", "run", "dashboard/app.py", "--server.address=0.0.0.0", "--server.port=8501"]
