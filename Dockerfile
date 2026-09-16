FROM python:3.13-slim

RUN useradd --system --uid 10001 exporter
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY exporter.py ./
USER exporter
EXPOSE 9828
ENTRYPOINT ["python", "/app/exporter.py"]
CMD ["--config", "/etc/vsr-exporter/config.yml"]
