FROM python:3.11-alpine

WORKDIR /app

RUN pip install --no-cache-dir aiohttp

CMD ["python", "-u", "/app/radarr_proxy.py"]
