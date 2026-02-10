import json
import re
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.views.decorators.http import require_POST
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from twilio.request_validator import RequestValidator
from apps.claims.models import Client, Case, CommunicationLog, InvolvedVehicle
from .flow import FlowManager
from .utils import WhatsAppClient
from .security import rate_limit, validate_and_rename_file, sanitize_text, get_session_key


@csrf_exempt
@require_POST
def whatsapp_webhook(request):
    """
    Webhook principal pentru WhatsApp (Twilio).
    """
    if not settings.DEBUG:
        auth_token = settings.TWILIO_AUTH_TOKEN
        if auth_token:
            validator = RequestValidator(auth_token)
            signature = request.META.get("HTTP_X_TWILIO_SIGNATURE", "")
            url = request.build_absolute_uri()
            post_vars = request.POST.dict()

            if not validator.validate(url, post_vars, signature):
                return HttpResponseForbidden("Invalid Twilio Signature")

    data = request.POST
    sender = data.get("From", "")
    msg_body = data.get("Body", "").strip()
    num_media = int(data.get("NumMedia", 0))

    phone_number = sender.replace("whatsapp:", "")

    if not phone_number:
        return HttpResponse("No sender", status=400)

    client, created = Client.objects.get_or_create(phone_number=phone_number)
    case = Case.objects.filter(client=client).exclude(stage=Case.Stage.CLOSED).last()

    wa = WhatsAppClient()

    if not case:
        case = Case.objects.create(client=client, stage=Case.Stage.GREETING)
        CommunicationLog.objects.create(case=case, direction="IN", content=msg_body)
        wa.send_buttons(
            sender,
            f"Salut {client.full_name or ''}! Bine ai venit la Asistentul de Daune.",
            ["Deschide Dosar de Daună", "Am altă problemă"],
        )
        return HttpResponse("OK")

    log_content = f"[MEDIA x{num_media}]" if num_media > 0 else msg_body
    CommunicationLog.objects.create(case=case, direction="IN", content=log_content)

    manager = FlowManager(case, sender, channel="WHATSAPP")

    if num_media > 0:
        media_files = []
        for i in range(num_media):
            url = data.get(f"MediaUrl{i}")
            ctype = data.get(f"MediaContentType{i}")
            if url:
                media_files.append((url, ctype))

        manager.process_message("image", msg_body, media_urls=media_files)
    else:
        manager.process_message("text", msg_body)

    return HttpResponse("OK")


# --- WEB CHAT API ---

@rate_limit(rate="10/m")  # Limitează login-urile de pe același IP
def chat_login(request):
    """
    Autentificare simplă prin nume, prenume, telefon și nr. înmatriculare.
    Returnează sesiunea (case_id setat in sesiune).
    CSRF protejat.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body)
        phone = data.get("phone", "").strip()
        first_name = data.get("first_name", "").strip()
        last_name = data.get("last_name", "").strip()
        plate_number = data.get("plate_number", "").strip()

        # 1. Validare
        if not phone:
            return JsonResponse({"error": "Numărul de telefon este obligatoriu."}, status=400)
        if not first_name or not last_name:
            return JsonResponse({"error": "Numele și Prenumele sunt obligatorii."}, status=400)
        if not plate_number:
            return JsonResponse({"error": "Numărul de înmatriculare avariat este obligatoriu."}, status=400)

        # Stergem spatii/caractere nedorite
        phone_clean = re.sub(r'[^0-9+]', '', phone)
        # Validare format romanesc
        if not re.match(r'^(\+40|0)7\d{8}$', phone_clean):
             return JsonResponse({"error": "Număr de telefon invalid. Folosiți formatul 07xxxxxxxx."}, status=400)

        # 2. Logica Client
        client, _ = Client.objects.get_or_create(phone_number=phone_clean)
        client.first_name = sanitize_text(first_name)
        client.last_name = sanitize_text(last_name)
        client.save()

        # 3. Logica Dosar (Case)
        case = Case.objects.filter(client=client).exclude(stage=Case.Stage.CLOSED).last()

        if not case:
            # Caz nou - inițiem flow-ul de Greeting
            case = Case.objects.create(client=client, stage=Case.Stage.GREETING)
            manager = FlowManager(case, phone_clean, channel="WEB")
            manager.process_message("text", "START_WEB_SESSION")

        # 4. Salvare Vehicul Avariat (VICTIM)
        # Căutăm dacă există deja un vehicul 'VICTIM' asociat dosarului
        victim_vehicle = InvolvedVehicle.objects.filter(case=case, role=InvolvedVehicle.Role.VICTIM).first()
        if not victim_vehicle:
            victim_vehicle = InvolvedVehicle(case=case, role=InvolvedVehicle.Role.VICTIM)

        # Actualizăm numărul de înmatriculare (uppercase, fără spații inutile)
        victim_vehicle.license_plate = sanitize_text(plate_number).upper().replace(" ", "")
        victim_vehicle.save()

        # 5. Setează Sesiunea (Secure)
        request.session['case_id'] = str(case.id)
        # Setează expirarea sesiunii la 10 ani (persistent)
        request.session.set_expiry(315360000)

        return JsonResponse({
            "success": True,
            # Nu mai returnam case_id si client_id direct pentru a nu le expune in JS daca nu e nevoie
            # Frontend-ul poate folosi cookie-ul de sesiune.
            # Dar pentru compatibilitate cu codul existent de frontend, am putea returna case_id?
            # Userul a cerut SECURITATE. Nu e bine sa expui UUID-ul daca sesiunea e suficienta.
            # Totusi, poll/send foloseau case_id in params. Vom schimba si acolo.
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@rate_limit(rate="60/m", key_func=get_session_key)
def chat_history(request):
    """
    Returnează istoricul conversației.
    Protejat prin Sesiune.
    """
    case_id = request.session.get('case_id')
    if not case_id:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    if not Case.objects.filter(id=case_id).exists():
        request.session.flush()
        return JsonResponse({"error": "Case invalid"}, status=401)

    logs = CommunicationLog.objects.filter(case_id=case_id).order_by("created_at")
    history = []
    for log in logs:
        history.append({
            "id": log.id,
            "direction": log.direction,
            "content": log.content,
            "timestamp": log.created_at.isoformat(),
            "metadata": log.metadata,
            "channel": log.channel
        })

    return JsonResponse({"messages": history})


@rate_limit(rate="30/m", key_func=get_session_key)
def chat_send(request):
    """
    Primește mesaje de la client (Web).
    Protejat prin Sesiune + Sanitizare + Validare Fișiere.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    case_id = request.session.get('case_id')
    if not case_id:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        case = Case.objects.get(id=case_id)
    except Case.DoesNotExist:
        request.session.flush()
        return JsonResponse({"error": "Case invalid"}, status=401)

    # Procesare mesaj text (Sanitizare)
    message = request.POST.get("message", "")
    message = sanitize_text(message)

    # Procesare fișiere uploadate
    media_urls = []
    if request.FILES:
        for key, file in request.FILES.items():
            try:
                # Validare & Redenumire
                valid_file = validate_and_rename_file(file)

                # Salvare
                path = default_storage.save(f"uploads/temp/{valid_file.name}", ContentFile(valid_file.read()))

                # URL
                domain = settings.APP_DOMAIN.rstrip("/")
                media_url_path = settings.MEDIA_URL.strip("/")
                full_url = f"{domain}/{media_url_path}/{path}"
                media_urls.append((full_url, valid_file.content_type)) # Content type original sau detectat? Pastram ce a venit din browser/validare.

            except ValidationError as ve:
                return JsonResponse({"error": str(ve)}, status=400)
            except Exception as e:
                return JsonResponse({"error": "Eroare la upload fisier"}, status=500)

    log_text = message
    if not log_text and media_urls:
        log_text = f"[Uploaded {len(media_urls)} files]"

    if not log_text and not media_urls:
        return JsonResponse({"error": "Empty message"}, status=400)

    CommunicationLog.objects.create(
        case=case,
        direction="IN",
        channel="WEB",
        content=log_text
    )

    manager = FlowManager(case, case.client.phone_number, channel="WEB")
    msg_type = "image" if media_urls else "text"
    manager.process_message(msg_type, message, media_urls=media_urls)

    return JsonResponse({"success": True})


@rate_limit(rate="120/m", key_func=get_session_key) # Polling rapid
def chat_poll(request):
    """
    Polling pentru mesaje noi.
    Protejat prin Sesiune.
    """
    case_id = request.session.get('case_id')
    last_id = request.GET.get("last_id", 0)

    if not case_id:
        return JsonResponse({"error": "Unauthorized"}, status=401)

    if not Case.objects.filter(id=case_id).exists():
        request.session.flush()
        return JsonResponse({"error": "Case invalid"}, status=401)

    logs = CommunicationLog.objects.filter(case_id=case_id, id__gt=last_id).order_by("created_at")
    new_messages = []
    for log in logs:
        new_messages.append({
            "id": log.id,
            "direction": log.direction,
            "content": log.content,
            "timestamp": log.created_at.isoformat(),
            "metadata": log.metadata,
            "channel": log.channel
        })

    return JsonResponse({"messages": new_messages})
