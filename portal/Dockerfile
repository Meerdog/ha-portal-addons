FROM python:3.12-alpine

RUN apk add --no-cache git tzdata \
 && git config --system --add safe.directory '*'

COPY app/ /app/
WORKDIR /app

ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/server.py"]
