from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings
from twilio.request_validator import RequestValidator
from apps.claims.models import Client, Case, CommunicationLog
from .flow import FlowManager
from .utils import WhatsAppClient


@csrf_exempt
@require_POST
def whatsapp_webhook(request):
    """
    Webhook principal pentru WhatsApp (Twilio).
    Gestionează intrarea și deleagă logica către FlowManager.
    Include validare de securitate a semnăturii Twilio în producție.
    """
    # 0. Validare Securitate (Doar în producție)
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

    # Curățare număr telefon (scoatem 'whatsapp:')
    phone_number = sender.replace("whatsapp:", "")

    if not phone_number:
        return HttpResponse("No sender", status=400)

    # 1. Identificare Client
    client, created = Client.objects.get_or_create(phone_number=phone_number)

    # 2. Identificare Dosar Activ
    # Căutăm un dosar care NU este închis
    case = Case.objects.filter(client=client).exclude(stage=Case.Stage.CLOSED).last()

    wa = WhatsAppClient()

    # SCENARIUL 0: Nu există dosar activ
    if not case:
        # Dacă userul scrie orice, îl întâmpinăm și creăm un dosar "Greeting"
        case = Case.objects.create(client=client, stage=Case.Stage.GREETING)

        # Logăm mesajul
        CommunicationLog.objects.create(case=case, direction="IN", content=msg_body)

        # Trimitem meniul de start
        wa.send_buttons(
            sender,
            f"Salut {client.full_name or ''}! Bine ai venit la Asistentul de Daune.",
            ["Deschide Dosar de Daună", "Am altă problemă"],
        )
        return HttpResponse("OK")

    # 3. Logging Mesaj Intrat
    log_content = f"[MEDIA x{num_media}]" if num_media > 0 else msg_body
    CommunicationLog.objects.create(case=case, direction="IN", content=log_content)

    # 4. Delegare către FlowManager
    manager = FlowManager(case, sender)

    if num_media > 0:
        # Colectăm toate URL-urile imaginilor
        media_files = []
        for i in range(num_media):
            url = data.get(f"MediaUrl{i}")
            ctype = data.get(f"MediaContentType{i}")
            if url:
                media_files.append((url, ctype))

        manager.process_message("image", msg_body, media_urls=media_files)
    else:
        # Mesaj text (sau răspuns la buton)
        manager.process_message("text", msg_body)

    return HttpResponse("OK")
