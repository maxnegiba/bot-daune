from celery import shared_task
from django.core.mail import EmailMessage
from django.conf import settings
from .models import Case, CaseDocument, Insurer, InvolvedVehicle
from .services import DocumentAnalyzer
from apps.bot.utils import WhatsAppClient
import imaplib
import email
from email.header import decode_header
import re
import os


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
            doc.doc_type = CaseDocument.DocType.MANDATE_UNSIGNED

        elif "EXTRAS" in tip_ai:
            doc.doc_type = CaseDocument.DocType.BANK_STATEMENT
            case.has_bank_statement = True
            # Optional: Save IBAN
            iban = result.get("date_extrase", {}).get("iban")
            if iban:
                case.client.iban = iban
                case.client.save()

        elif "ACTE_VINOVAT" in tip_ai:
            doc.doc_type = CaseDocument.DocType.GUILTY_PARTY_DOCS
            case.has_guilty_docs = True

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
        missing.append("Buletin (obligatoriu)")
    if not case.has_car_coupon:
        missing.append("Talon Auto (obligatoriu)")
    if not case.has_accident_report:
        missing.append("Amiabilă / PV Politie (obligatoriu)")
    if not case.has_scene_video:
        missing.append("Video 360 Grade (obligatoriu)")

    # Condiție Extras Cont
    if case.resolution_choice == Case.Resolution.OWN_REGIME:
        if not case.has_bank_statement:
                missing.append("Extras Cont Bancar (pt. Regie Proprie)")

    # Verificăm stadiul curent pentru a nu trimite mesaje inutile
    if case.stage == Case.Stage.COLLECTING_DOCS:
        if not missing:
            # TOTUL E COMPLET -> Trecem la pasul următor
            case.stage = Case.Stage.SELECTING_RESOLUTION
            case.save()

            # Dacă rezoluția nu e aleasă, întrebăm din nou (sau prima dată dacă task-ul a terminat ultimul doc)
            if case.resolution_choice == Case.Resolution.UNDECIDED:
                wa.send_buttons(
                    phone,
                    "✅ Am primit toate documentele necesare!\nCum dorești să soluționezi dosarul?",
                    ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"],
                )
            else:
                 # Totul e gata -> Mandat
                 case.stage = Case.Stage.SIGNING_MANDATE
                 case.save()

                 # Trimitem link semnare
                 # Trebuie să duplicăm logica de trimitere link sau să apelăm o funcție comună?
                 # Pentru simplitate, trimitem textul aici.
                 domain = "http://127.0.0.1:8000"
                 link = f"{domain}/mandat/semneaza/{case.id}/"
                 msg = (
                    "📝 Dosar complet! Mai avem un singur pas: Semnarea Mandatului.\n"
                    f"Te rog intră aici și semnează:\n{link}"
                 )
                 wa.send_text(phone, msg)
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
                    # Determinăm tipul (PDF, Imagine, Video)
                    fname = doc.file.name.lower()
                    if fname.endswith(".pdf"):
                        content_type = "application/pdf"
                    elif fname.endswith(".png"):
                        content_type = "image/png"
                    elif fname.endswith(".jpg") or fname.endswith(".jpeg"):
                         content_type = "image/jpeg"
                    elif fname.endswith(".mp4"):
                        content_type = "video/mp4"
                    elif fname.endswith(".mov"):
                         content_type = "video/quicktime"
                    else:
                        content_type = "application/octet-stream"

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


# --- TASK 3: Monitorizare Email (IMAP) ---
@shared_task
def check_email_replies_task():
    """
    Verifică inboxul pentru reply-uri de la asiguratori.
    Identifică dosarul după ID-ul din subiect.
    Dacă e ofertă -> Declansază OFFER_DECISION.
    Altfel -> Forward la client pe WhatsApp.
    """
    IMAP_HOST = os.getenv("EMAIL_HOST", "imap.gmail.com")
    IMAP_USER = os.getenv("EMAIL_HOST_USER")
    IMAP_PASS = os.getenv("EMAIL_HOST_PASSWORD")

    if not IMAP_USER or not IMAP_PASS:
        print("❌ Lipsă credențiale IMAP")
        return

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("inbox")

        # Căutăm mesaje necitite
        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            return

        msg_ids = messages[0].split()
        for num in msg_ids:
            try:
                # Fetch headers only first? No, we need body too.
                _, msg_data = mail.fetch(num, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding or "utf-8")

                        sender = msg.get("From")
                        print(f"📧 Mesaj nou: {subject} de la {sender}")

                        # 1. Căutăm ID Dosar
                        # Pattern: "Dosar ([a-f0-9]{8})"
                        match = re.search(r"Dosar ([a-f0-9]{8})", subject)
                        if match:
                            case_id_prefix = match.group(1)
                            # Căutăm dosarul (startsWith)
                            case = Case.objects.filter(id__startswith=case_id_prefix).first()

                            if case:
                                # Parsăm body
                                body = ""
                                if msg.is_multipart():
                                    for part in msg.walk():
                                        if part.get_content_type() == "text/plain":
                                            payload = part.get_payload(decode=True)
                                            if payload:
                                                body = payload.decode(errors="ignore")
                                                break
                                else:
                                    payload = msg.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode(errors="ignore")

                                # 2. Analizăm conținutul
                                body_lower = body.lower()
                                keywords_offer = ["oferta", "propunere", "despagubire", "suma de", "acceptul"]
                                is_offer = any(k in body_lower for k in keywords_offer)

                                wa = WhatsAppClient()

                                if is_offer:
                                    print(f"💰 OFERTA DETECTATA pentru {case.id}")
                                    case.stage = Case.Stage.OFFER_DECISION

                                    # Încercăm să extragem suma (simplistic)
                                    # Ex: "suma de 1200 RON"
                                    amount_match = re.search(r"(\d+([.,]\d+)?)\s*(ron|lei)", body_lower)
                                    if amount_match:
                                        val = amount_match.group(1).replace(",", ".")
                                        try:
                                            case.settlement_offer_value = float(val)
                                        except:
                                            pass

                                    case.save()

                                    wa.send_buttons(
                                        case.client.phone_number,
                                        f"📢 Am primit o OFERTĂ de la asigurator!\n\nDin textul emailului: {body[:300]}...\n\nCe dorești să faci?",
                                        ["Accept Oferta", "Schimb Optiunea"] # Max 3 buttons usually.
                                    )
                                else:
                                    # Doar informare
                                    print(f"ℹ️ Mesaj info pentru {case.id}")
                                    wa.send_text(
                                        case.client.phone_number,
                                        f"📩 Mesaj nou de la asigurator:\n\n{body[:500]}..."
                                    )

            except Exception as e_inner:
                print(f"Eroare procesare email {num}: {e_inner}")

        mail.close()
        mail.logout()

    except Exception as e:
        print(f"Eroare IMAP: {e}")


# --- TASK 4: Email de Acceptare Oferta ---
@shared_task
def send_offer_acceptance_email_task(case_id):
    try:
        case = Case.objects.get(id=case_id)
        if not case.insurer_email:
            print("⚠️ Nu am emailul asiguratorului salvat.")
            return

        subject = f"Acceptare Oferta - Dosar {str(case.id)[:8]} - {case.client.full_name}"

        # Detalii bancare
        iban_info = ""
        if case.resolution_choice == Case.Resolution.OWN_REGIME and case.client.iban:
            iban_info = f"\nCont IBAN: {case.client.iban}\nTitular Cont: {case.client.full_name}"

        offer_val = f"{case.settlement_offer_value} RON" if case.settlement_offer_value else "(Conform ofertei transmise)"

        body = f"""
        Buna ziua,

        Ref: Dosar de dauna {case.insurer_claim_number or str(case.id)[:8]}

        Prin prezenta, clientul nostru {case.client.full_name} (CNP: {case.client.cnp}) ACCEPTĂ oferta de despăgubire în valoare de {offer_val}.

        Vă rugăm să procedați la plata despăgubirii.{iban_info}

        Așteptăm confirmarea plății / închiderii dosarului.

        Cu stimă,
        Echipa Auto Daune
        """

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[case.insurer_email],
            cc=["office@autodaune.ro"]
        )
        email.send()
        print(f"✅ Email acceptare trimis pentru dosar {case.id}")

    except Exception as e:
        print(f"Eroare email acceptare: {e}")

# --- TASK 5: Email Schimbare Optiune ---
@shared_task
def send_option_change_email_task(case_id, new_option_label):
    try:
        case = Case.objects.get(id=case_id)
        if not case.insurer_email:
            return

        subject = f"Modificare Optiune Despagubire - Dosar {str(case.id)[:8]}"

        body = f"""
        Buna ziua,

        Clientul nostru {case.client.full_name} dorește să MODIFICE opțiunea de despăgubire.

        Noua opțiune aleasă: {new_option_label}

        Vă rugăm să ne comunicați pașii următori sau noua ofertă/calculație aferentă acestei opțiuni.

        Cu stimă,
        Echipa Auto Daune
        """

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[case.insurer_email],
            cc=["office@autodaune.ro"]
        )
        email.send()
        print(f"✅ Email schimbare optiune trimis pentru dosar {case.id}")

    except Exception as e:
        print(f"Eroare email schimbare optiune: {e}")
