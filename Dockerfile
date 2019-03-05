# vim:set ft=dockerfile:
FROM python:3.6-slim-stretch
MAINTAINER https://github.com/bird-house/finch
LABEL Description="Finch WPS" Vendor="Birdhouse" Version="0.1.0"

# Update Debian system
RUN apt-get update && apt-get install -y \
    build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /code

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn psycopg2-binary

COPY . .

RUN pip install --no-dependencies -e .

EXPOSE 5000

CMD ["gunicorn", "--bind=0.0.0.0:5000", "finch.wsgi:application"]
