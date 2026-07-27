# Reproducible application image. Dependencies come from requirements.lock
# (full pinned closure) so every build installs identical versions.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.lock ./
RUN pip install -r requirements.lock

COPY . .

# Static files are collected at build time into /app/staticfiles; serve them
# from the reverse proxy, object storage, or a CDN — never from Gunicorn.
RUN DJANGO_ENV=dev python manage.py collectstatic --noinput

# Run as an unprivileged user; the app writes nothing to disk (stateless)
RUN adduser --disabled-password --gecos "" eve \
    && chown -R eve:eve /app
USER eve

EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "eve.wsgi:application"]
