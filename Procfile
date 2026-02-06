web: gunicorn config.wsgi:application --config gunicorn_config.py
worker: celery -A config worker --loglevel=info
