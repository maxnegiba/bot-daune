import os
from celery import Celery
from celery.schedules import crontab

# Setăm setările default de Django pentru Celery
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')

# Folosim setările din settings.py care încep cu CELERY_
app.config_from_object('django.conf:settings', namespace='CELERY')

# Caută automat fișiere tasks.py în toate aplicațiile instalate
app.autodiscover_tasks()

# --- Programare Task-uri Periodice (Celery Beat) ---
app.conf.beat_schedule = {
    'check-email-replies-every-5-minutes': {
        'task': 'apps.claims.tasks.check_email_replies_task',
        'schedule': crontab(minute='*/5'),  # Execută la fiecare 5 minute
    },
}
