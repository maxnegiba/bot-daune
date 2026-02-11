import base64
import os
from django.conf import settings
from django.contrib.staticfiles import finders
from django.shortcuts import render, get_object_or_404
from django.http import JsonResponse, HttpResponse
from django.core.files.base import ContentFile
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

# Librărie PDF
try:
    from weasyprint import HTML
except ImportError:
    HTML = None

from apps.claims.models import Case, CaseDocument, InvolvedVehicle
from apps.bot.utils import WhatsAppClient, WebChatClient
from apps.claims.tasks import send_claim_email_task  # <--- IMPORT TASK EMAIL


def sign_mandate_view(request, case_id):
    """
    Pagina unde clientul vede mandatul si semneaza.
    """
    case = get_object_or_404(Case, id=case_id)

    # 1. VERIFICARE (Folosind câmpul din Etapa 1)
    if case.has_mandate_signed:
        return HttpResponse("Acest mandat a fost deja semnat. Mulțumim!")

    if request.method == "POST":
        return _handle_signature_submission(request, case)

    # Căutăm vehiculul clientului (VICTIM)
    vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()

    context = {
        "case": case,
        "client": case.client,
        "vehicle": vehicle,
        "date": timezone.now().strftime("%d.%m.%Y"),
    }
    return render(request, "signatures/signing_page.html", context)


def _handle_signature_submission(request, case):
    """
    Procesează semnătura, generează PDF-ul și declanșează trimiterea pe email.
    """
    signature_data = request.POST.get("signature")

    if not signature_data:
        return JsonResponse(
            {"status": "error", "message": "Lipsă semnătură"}, status=400
        )

    # --- A. Pregătire Imagini pentru PDF (Logo & Ștampilă) ---
    # Căutăm imaginile în folderul static pentru a le pune în PDF
    logo_path = finders.find("signatures/img/logo.png")
    stampila_path = finders.find("signatures/img/stampila.png")

    # Convertim în URL-uri de tip file:// pentru WeasyPrint
    logo_url = f"file://{logo_path}" if logo_path else ""
    stampila_url = f"file://{stampila_path}" if stampila_path else ""

    # Căutăm vehiculul clientului (VICTIM)
    vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()

    # --- B. Generare PDF ---
    pdf_context = {
        "case": case,
        "client": case.client,
        "vehicle": vehicle,
        "date": timezone.now().strftime("%d.%m.%Y"),
        "signature_client": signature_data,  # Semnătura clientului (Base64)
        "logo_url": logo_url,  # Sigla Asociației
        "stampila_url": stampila_url,  # Ștampila Asociației
    }

    html_string = render_to_string("signatures/mandate_pdf.html", pdf_context)

    pdf_file = None
    if HTML:
        # base_url=str(settings.BASE_DIR) ajută WeasyPrint să rezolve căile relative
        pdf_bin = HTML(string=html_string, base_url=str(settings.BASE_DIR)).write_pdf()
        pdf_file = ContentFile(
            pdf_bin, name=f"Mandat_{case.client.full_name.replace(' ', '_')}.pdf"
        )
    else:
        # Fallback dacă WeasyPrint nu e instalat
        return JsonResponse(
            {"status": "error", "message": "Eroare: WeasyPrint lipsește"}, status=500
        )

    # --- C. Salvare Document ---
    CaseDocument.objects.create(
        case=case,
        doc_type=CaseDocument.DocType.MANDATE_SIGNED,
        file=pdf_file,
        ocr_data={"status": "generated_signed", "generated_at": str(timezone.now())},
    )

    # --- D. Actualizare Dosar ---
    case.has_mandate_signed = True
    case.stage = (
        Case.Stage.PROCESSING_INSURER
    )  # Trecem la etapa de discuție cu asiguratorul
    case.save()

    # --- E. Declanșare Trimitere Email (Asincron) ---
    print(f"🚀 [VIEW] Declanșez task-ul de email pentru dosar {case.id}")
    send_claim_email_task.delay(case.id)

    # --- F. Notificare WhatsApp ---
    try:
        wa = WhatsAppClient()
        wa.send_text(
            case.client.phone_number,
            "✅ Am primit mandatul semnat! Dosarul complet (acte + mandat) a fost trimis automat către asigurator. Te vom anunța când primim numărul de dosar.",
        )
    except Exception as e:
        print(f"Eroare WhatsApp: {e}")

    # --- G. Notificare WebChat ---
    try:
        wc = WebChatClient()
        wc.send_text(
            case,
            "✅ Am primit mandatul semnat! Dosarul complet (acte + mandat) a fost trimis automat către asigurator. Te vom anunța când primim numărul de dosar.",
        )
    except Exception as e:
        print(f"Eroare WebChat: {e}")

    return JsonResponse({"status": "success"})
