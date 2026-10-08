FROM python:3.11-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tv_jelly_launcher.py .

CMD ["python", "-u", "tv_jelly_launcher.py"]
