# Ghid de Deploiere (Production Ready)

Acest ghid detaliază pașii necesari pentru a pune aplicația în producție pe un VPS (Ubuntu 20.04/22.04) folosind Nginx, Gunicorn și PostgreSQL.

## 1. Cerințe de Sistem

Instalează pachetele necesare:

```bash
sudo apt update
sudo apt install python3-pip python3-dev libpq-dev postgresql postgresql-contrib nginx curl git redis-server
```

### Pachete pentru WeasyPrint (PDF Generation)
WeasyPrint necesită biblioteci grafice specifice:

```bash
sudo apt install build-essential python3-dev python3-pip python3-setuptools python3-wheel python3-cffi libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 libffi-dev shared-mime-info
```

## 2. Configurare Bază de Date (PostgreSQL)

```bash
sudo -u postgres psql
```

În consola SQL:
```sql
CREATE DATABASE autodaune;
CREATE USER myprojectuser WITH PASSWORD 'password';
ALTER ROLE myprojectuser SET client_encoding TO 'utf8';
ALTER ROLE myprojectuser SET default_transaction_isolation TO 'read committed';
ALTER ROLE myprojectuser SET timezone TO 'UTC';
GRANT ALL PRIVILEGES ON DATABASE autodaune TO myprojectuser;
\q
```

## 3. Instalare Aplicație

Clonează repository-ul în `/var/www/autodaune`:

```bash
cd /var/www
sudo git clone <URL_REPO> autodaune
cd autodaune
```

Crează mediul virtual și instalează dependențele:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 4. Configurare Variabile de Mediu (.env)

Crează fișierul `.env`:

```bash
nano .env
```

Adaugă variabilele (actualizează cu valorile reale):

```ini
DEBUG=False
SECRET_KEY=genereaza-o-cheie-lunga-si-sigura
ALLOWED_HOSTS=domeniul-tau.com,www.domeniul-tau.com,IP-ul-tau
APP_DOMAIN=https://domeniul-tau.com
CSRF_TRUSTED_ORIGINS=https://domeniul-tau.com

# Database
DB_ENGINE=django.db.backends.postgresql
DB_NAME=autodaune
DB_USER=myprojectuser
DB_PASSWORD=password
DB_HOST=localhost
DB_PORT=5432

# Redis (Celery)
REDIS_URL=redis://localhost:6379/0

# Twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_NUMBER=whatsapp:+14155238886

# OpenAI
OPENAI_API_KEY=sk-...

# Email (SMTP SendGrid)
EMAIL_HOST=smtp.sendgrid.net
EMAIL_PORT=587
EMAIL_HOST_USER=apikey
EMAIL_HOST_PASSWORD=...
DEFAULT_FROM_EMAIL=office@autodaune.ro

# Email (IMAP Receiving - Opțional, dacă diferă de SMTP)
IMAP_HOST=imap.ionos.com

# Security
SECURE_SSL_REDIRECT=True
```

## 5. Inițializare Django

```bash
# Rulează migrările
python manage.py migrate

# Colectează fișierele statice
python manage.py collectstatic --noinput

# Crează superuser
python manage.py createsuperuser
```

## 6. Configurare Gunicorn & Systemd

Copiază fișierul de serviciu și editează-l dacă calea diferă:

```bash
sudo cp deploy/gunicorn.service /etc/systemd/system/
sudo systemctl start gunicorn
sudo systemctl enable gunicorn
```

Verifică statusul:
```bash
sudo systemctl status gunicorn
```

## 7. Configurare Nginx

Editează `deploy/nginx.conf` și pune numele domeniului tău, apoi copiază-l:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/autodaune
sudo ln -s /etc/nginx/sites-available/autodaune /etc/nginx/sites-enabled
sudo nginx -t
sudo systemctl restart nginx
```

## 8. Configurare Celery (Worker)

Este recomandat să rulezi Celery ca un serviciu systemd separat (similar cu Gunicorn) pentru a procesa task-urile asincrone (PDF-uri, email-uri).

Exemplu `/etc/systemd/system/celery.service`:

```ini
[Unit]
Description=Celery Service
After=network.target

[Service]
Type=forking
User=root
Group=www-data
WorkingDirectory=/var/www/autodaune
ExecStart=/var/www/autodaune/venv/bin/celery -A config multi start worker1 --loglevel=INFO --logfile=/var/log/celery.log
ExecStop=/var/www/autodaune/venv/bin/celery multi stopwait worker1 --pidfile=/var/www/autodaune/celerybeat.pid
Restart=always

[Install]
WantedBy=multi-user.target
```

## 9. HTTPS (SSL)

Instalează Certbot și activează HTTPS:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d domeniul-tau.com
```
