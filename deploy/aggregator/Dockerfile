FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY processing.py worker.py ./
RUN useradd -r -u 10001 app && chown -R app /srv
USER app
EXPOSE 9100
CMD ["python", "worker.py"]
