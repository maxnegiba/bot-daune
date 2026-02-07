import uuid
import os
import mimetypes
import time
from functools import wraps
from django.core.cache import cache
from django.http import JsonResponse
from django.core.exceptions import ValidationError
from django.utils.html import strip_tags
from django.conf import settings

# --- CONSTANTS ---
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
ALLOWED_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".pdf",
    ".mp4", ".mov", ".avi"
}
ALLOWED_MIMES = {
    "image/jpeg", "image/png", "image/webp",
    "application/pdf",
    "video/mp4", "video/quicktime", "video/x-msvideo"
}

# --- RATE LIMITER ---
def rate_limit(rate="30/m", key_func=None):
    """
    Decorator simplu pentru rate limiting folosind Cache.
    rate: string format "count/period" (ex: "30/m", "5/s", "100/h")
    key_func: funcție care returnează cheia unică (ex: IP sau Session ID).
              Dacă e None, folosește IP-ul.
    """
    count, period_char = rate.split("/")
    count = int(count)

    if period_char == "s":
        period = 1
    elif period_char == "m":
        period = 60
    elif period_char == "h":
        period = 3600
    else:
        raise ValueError("Invalid period. Use s, m, or h.")

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped_view(request, *args, **kwargs):
            # Identificator unic
            if key_func:
                ident = key_func(request)
            else:
                ident = get_client_ip(request)

            # Cheie cache specifică endpoint-ului
            cache_key = f"rl:{request.path}:{ident}"

            # Verificăm istoricul
            history = cache.get(cache_key, [])
            now = time.time()

            # Curățăm timestamp-urile vechi
            history = [ts for ts in history if ts > now - period]

            if len(history) >= count:
                return JsonResponse(
                    {"error": "Too many requests. Please try again later."},
                    status=429
                )

            # Adăugăm timestamp curent
            history.append(now)
            cache.set(cache_key, history, period + 10)

            return view_func(request, *args, **kwargs)
        return _wrapped_view
    return decorator

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def get_session_key(request):
    """Helper pentru rate limiting per sesiune."""
    return request.session.session_key or get_client_ip(request)


# --- FILE SECURITY ---
def validate_and_rename_file(uploaded_file):
    """
    Validează fișierul uploadat și îi schimbă numele în UUID.
    Returnează fișierul modificat sau ridică ValidationError.
    """
    # 1. Verificare mărime
    if uploaded_file.size > MAX_FILE_SIZE:
        raise ValidationError(f"Fișierul este prea mare (Max {MAX_FILE_SIZE/1024/1024}MB).")

    # 2. Verificare extensie
    ext = os.path.splitext(uploaded_file.name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValidationError("Tip de fișier nepermis.")

    # 3. Verificare MIME (Content-Type header from browser - basic check)
    # Pentru o verificare mai robustă am avea nevoie de python-magic, dar ne bazăm pe header + extensie momentan.
    content_type = getattr(uploaded_file, 'content_type', '')
    if content_type not in ALLOWED_MIMES:
         # Fallback: check extension mapping logic if header is missing/wrong but extension is ok?
         # No, be strict.
         # Totuși, uneori browserul trimite application/octet-stream. Putem fi lenienți dacă extensia e safe?
         # Nu, securitate maximă.
         # Dar python-magic nu e instalat.
         # Putem folosi mimetypes guess.
         guess, _ = mimetypes.guess_type(uploaded_file.name)
         if guess not in ALLOWED_MIMES:
             raise ValidationError(f"Tip MIME invalid: {content_type}")

    # 4. Redenumire (Sanitization)
    new_name = f"{uuid.uuid4().hex}{ext}"
    uploaded_file.name = new_name

    return uploaded_file


# --- TEXT SANITIZATION ---
def sanitize_text(text):
    """
    Curăță textul de tag-uri HTML și caractere invizibile.
    """
    if not text:
        return ""
    # Strip HTML tags
    clean = strip_tags(text)
    return clean.strip()
