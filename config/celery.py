import os
from celery import Celery

# Setăm setările default de Django pentru Celery
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')

# Folosim setările din settings.py care încep cu CELERY_
app.config_from_object('django.conf:settings', namespace='CELERY')

# Caută automat fișiere tasks.py în toate aplicațiile instalate
app.autodiscover_tasks()
