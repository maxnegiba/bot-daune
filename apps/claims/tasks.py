from celery import shared_task
from django.core.mail import EmailMessage
from django.conf import settings
from .models import Case, CaseDocument, Insurer, InvolvedVehicle
from .services import DocumentAnalyzer
from apps.bot.utils import WhatsAppClient


# --- TASK 1: Procesare Input (Documente & AI) ---
@shared_task
def analyze_document_task(document_id):
    try:
        print(f"--- [AI WORKER] Procesez Doc ID: {document_id} cu OpenAI ---")

        doc = CaseDocument.objects.get(id=document_id)
        case = doc.case

        # 1. Analiza OpenAI
        result = DocumentAnalyzer.analyze(doc.file.path)
        print(f"🤖 Rezultat AI: {result}")

        # 2. Salvare date OCR
        # NOTA: Acest save() va declanșa signals.py care populează vehiculele!
        doc.ocr_data = result

        # Mapăm tipul primit de la AI la Enum-ul din Django
        tip_ai = result.get("tip_document", "").upper()

        if "CI" in tip_ai or "BULETIN" in tip_ai:
            doc.doc_type = CaseDocument.DocType.ID_CARD
            case.has_id_card = True
            # Optional: Salvăm CNP pe client
            date = result.get("date_extrase", {})
            if date.get("cnp"):
                case.client.cnp = date.get("cnp")
                case.client.full_name = date.get("nume")
                case.client.save()

        elif "TALON" in tip_ai:
            doc.doc_type = CaseDocument.DocType.CAR_REGISTRATION
            case.has_car_coupon = True

        elif "AMIABILA" in tip_ai or "CONSTATARE" in tip_ai:
            doc.doc_type = CaseDocument.DocType.ACCIDENT_REPORT
            case.has_accident_report = True

        elif "PROCURA" in tip_ai:
            doc.doc_type = CaseDocument.DocType.POA_GENERATED

        # Salvăm documentul și dosarul (Flags updated)
        doc.save()
        case.save()

        # 3. Verificare Flux și Notificare
        check_status_and_notify(case)

    except Exception as e:
        print(f"--- [AI ERROR] {e} ---")


def check_status_and_notify(case):
    """
    Verifică ce documente lipsesc și notifică clientul pe WhatsApp.
    """
    wa = WhatsAppClient()
    phone = case.client.phone_number

    # Lista de verificare
    missing = []
    if not case.has_id_card:
        missing.append("Buletin (CI)")
    if not case.has_car_coupon:
        missing.append("Talon Auto")
    if not case.has_accident_report:
        missing.append("Amiabilă / Proces Verbal")

    # Verificăm stadiul curent pentru a nu trimite mesaje inutile
    if case.stage == Case.Stage.COLLECTING_DOCS:
        if not missing:
            # TOTUL E COMPLET -> Trecem la pasul următor
            case.stage = Case.Stage.SELECTING_RESOLUTION
            case.save()

            wa.send_buttons(
                phone,
                "✅ Am primit toate documentele necesare!\nCum dorești să soluționezi dosarul?",
                ["Regie Proprie", "Service Autorizat", "Dauna Totala"],
            )
        else:
            # Încă lipsesc acte
            doc_name = case.documents.last().get_doc_type_display()
            msg = f"👍 Am validat {doc_name}.\nMai am nevoie de:\n- " + "\n- ".join(
                missing
            )
            wa.send_text(phone, msg)


# --- TASK 2: Procesare Output (Trimitere Email Asigurator) ---
@shared_task
def send_claim_email_task(case_id):
    """
    1. Caută numele asiguratorului vinovatului (extras de AI sau din baza de date).
    2. Îl potrivește cu modelul Insurer (pentru a găsi emailul corect).
    3. Trimite email cu toate documentele atașate.
    """
    try:
        case = Case.objects.get(id=case_id)
        client = case.client

        print(f"📧 [EMAIL WORKER] Pregătesc trimiterea pentru dosar {case.id}")

        # --- PASUL 1: Identificare Asigurator ---
        target_email = "office@autodaune.ro"  # Fallback (default la noi dacă nu găsim)
        target_name = "Administrator"

        # Căutăm vehiculul vinovat
        # Ne uităm în câmpul 'insurance_company_name' populat de AI (via signals)
        guilty_vehicle = case.vehicles.filter(is_offender=True).first()

        # Dacă nu e marcat explicit, luăm vehiculul care NU e al clientului (Role != VICTIM)
        if not guilty_vehicle:
            guilty_vehicle = case.vehicles.exclude(
                role=InvolvedVehicle.Role.VICTIM
            ).first()

        detected_text = ""
        if guilty_vehicle and guilty_vehicle.insurance_company_name:
            detected_text = guilty_vehicle.insurance_company_name.lower()
            print(f"🔍 Text asigurator detectat de AI: '{detected_text}'")

        # Algoritm de Matching cu baza de date 'Insurer'
        if detected_text:
            all_insurers = Insurer.objects.all()
            for insurer in all_insurers:
                # Spargem identifierii: "allianz, tiriac" -> ['allianz', 'tiriac']
                keywords = [k.strip().lower() for k in insurer.identifiers.split(",")]
                for k in keywords:
                    if k and k in detected_text:
                        target_email = insurer.email_claims
                        target_name = insurer.name

                        # Salvăm în dosar ce am găsit
                        case.insurer_name = insurer.name
                        case.insurer_email = insurer.email_claims
                        case.save()

                        print(
                            f"✅ MATCH ASIGURATOR: '{detected_text}' -> {insurer.name} ({target_email})"
                        )
                        break
                if target_name != "Administrator":
                    break
        else:
            print("⚠️ Nu am detectat numele asiguratorului. Trimit la fallback.")

        # --- PASUL 2: Construire Email ---
        subject = f"Avizare Dauna Auto - {client.full_name} - Dosar {str(case.id)[:8]}"

        body = f"""
        Buna ziua,
        
        În atenția departamentului de daune {target_name},
        
        Prin prezenta, vă transmitem solicitarea de deschidere dosar de daună pentru clientul nostru:
        Nume: {client.full_name}
        CNP: {client.cnp or '-'}
        Telefon: {client.phone_number}
        
        Atașat regăsiți documentele necesare instrumentării dosarului (Mandat, Amiabilă, Acte, Foto).
        
        Vă rugăm să ne confirmați primirea și să ne comunicați numărul de dosar alocat prin Reply la acest email.
        
        Cu stimă,
        Echipa Auto Daune Bot
        """

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[target_email],
            cc=["office@autodaune.ro"],  # Copie către administrator
        )

        # --- PASUL 3: Atașare Documente ---
        docs = CaseDocument.objects.filter(case=case)
        count = 0
        for doc in docs:
            if doc.file:
                try:
                    # Determinăm tipul (PDF sau Imagine)
                    fname = doc.file.name.lower()
                    if fname.endswith(".pdf"):
                        content_type = "application/pdf"
                    elif fname.endswith(".png"):
                        content_type = "image/png"
                    else:
                        content_type = "image/jpeg"

                    # Nume fișier lizibil pentru atașament
                    doc_label = doc.get_doc_type_display().replace(" ", "_")
                    clean_name = f"{doc_label}_{count}.{fname.split('.')[-1]}"

                    # Citim și atașăm
                    email.attach(clean_name, doc.file.read(), content_type)
                    count += 1
                except Exception as e:
                    print(f"⚠️ Eroare atașare {doc.file.name}: {e}")

        # --- PASUL 4: Trimitere ---
        email.send()

        # Confirmăm pe consolă
        print(f"🚀 Email trimis cu succes la {target_email}")

        # Notă: Nu schimbăm 'stage' aici, rămâne PROCESSING_INSURER până răspund ei.

    except Exception as e:
        print(f"❌ EROARE CRITICĂ SEND EMAIL: {e}")
