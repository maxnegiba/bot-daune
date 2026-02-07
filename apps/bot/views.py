import json
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from twilio.request_validator import RequestValidator
from apps.claims.models import Client, Case, CommunicationLog
from .flow import FlowManager
from .utils import WhatsAppClient


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

@csrf_exempt
def chat_login(request):
    """
    Autentificare simplă prin nume și telefon.
    Returnează sesiunea (case_id).
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    try:
        data = json.loads(request.body)
        phone = data.get("phone", "").strip()
        name = data.get("name", "").strip()

        if not phone:
            return JsonResponse({"error": "Phone number is required"}, status=400)

        client, _ = Client.objects.get_or_create(phone_number=phone)
        if name:
            client.full_name = name
            client.save()

        case = Case.objects.filter(client=client).exclude(stage=Case.Stage.CLOSED).last()

        if not case:
            # Caz nou - inițiem flow-ul de Greeting
            case = Case.objects.create(client=client, stage=Case.Stage.GREETING)
            # Declansăm mesajul de bun venit
            manager = FlowManager(case, phone, channel="WEB")
            # Trimitem un mesaj fals pentru a declanșa logica din _handle_greeting (else branch)
            manager.process_message("text", "START_WEB_SESSION")

        return JsonResponse({
            "success": True,
            "case_id": str(case.id),
            "client_id": str(client.id)
        })
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)


@csrf_exempt
def chat_history(request):
    """
    Returnează istoricul conversației.
    """
    case_id = request.GET.get("case_id")
    if not case_id:
        return JsonResponse({"error": "Missing case_id"}, status=400)

    logs = CommunicationLog.objects.filter(case_id=case_id).order_by("created_at")
    history = []
    for log in logs:
        # Nu trimitem metadatele interne sensibile, dar trimitem butoanele
        history.append({
            "id": log.id,
            "direction": log.direction,
            "content": log.content,
            "timestamp": log.created_at.isoformat(),
            "metadata": log.metadata,
            "channel": log.channel
        })

    return JsonResponse({"messages": history})


@csrf_exempt
def chat_send(request):
    """
    Primește mesaje de la client (Web).
    Suportă text și upload fișiere.
    """
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed"}, status=405)

    case_id = request.POST.get("case_id")
    message = request.POST.get("message", "")

    if not case_id:
        return JsonResponse({"error": "Missing case_id"}, status=400)

    try:
        case = Case.objects.get(id=case_id)
    except Case.DoesNotExist:
        return JsonResponse({"error": "Case not found"}, status=404)

    # Procesare fișiere uploadate
    media_urls = []
    if request.FILES:
        for key, file in request.FILES.items():
            # Salvăm temporar fișierul pentru a avea un URL
            # Îl salvăm în 'uploads/temp/'
            path = default_storage.save(f"uploads/temp/{file.name}", ContentFile(file.read()))

            # Construim URL-ul complet accesibil
            # Atenție: APP_DOMAIN trebuie să fie accesibil de server (requests.get)
            domain = settings.APP_DOMAIN
            if not domain.endswith("/"):
                domain += "/"

            # MEDIA_URL e de obicei /media/
            media_url = settings.MEDIA_URL
            if media_url.startswith("/"):
                media_url = media_url[1:]

            full_url = f"{domain}{media_url}{path}"
            media_urls.append((full_url, file.content_type))

    # Logăm mesajul IN (Web)
    # Dacă e doar upload, punem un text placeholder
    log_text = message
    if not log_text and media_urls:
        log_text = f"[Uploaded {len(media_urls)} files]"

    CommunicationLog.objects.create(
        case=case,
        direction="IN",
        channel="WEB",
        content=log_text
    )

    # Inițiem FlowManager pe canalul WEB
    manager = FlowManager(case, case.client.phone_number, channel="WEB")

    msg_type = "image" if media_urls else "text"
    manager.process_message(msg_type, message, media_urls=media_urls)

    return JsonResponse({"success": True})


@csrf_exempt
def chat_poll(request):
    """
    Polling pentru mesaje noi.
    Clientul trimite last_id (id-ul ultimului mesaj pe care îl are).
    """
    case_id = request.GET.get("case_id")
    last_id = request.GET.get("last_id", 0)

    if not case_id:
        return JsonResponse({"error": "Missing case_id"}, status=400)

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
