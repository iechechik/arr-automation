FROM python:3.11-alpine

WORKDIR /app

RUN pip install --no-cache-dir aiohttp

CMD ["python", "-u", "/app/sonarr_proxy.py"]
