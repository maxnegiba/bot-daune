from celery import shared_task
from django.core.mail import EmailMessage
from django.conf import settings
from .models import Case, CaseDocument, Insurer, InvolvedVehicle
from .services import DocumentAnalyzer
from apps.bot.utils import WhatsAppClient, WebChatClient
import imaplib
import email
from email.header import decode_header
import re
import os
import requests
import tempfile
import shutil


# --- TASK 1: Procesare Input (Documente & AI) ---
@shared_task
def analyze_document_task(document_id):
    doc = None
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

                raw_name = date.get("nume", "").strip()
                if raw_name:
                    # Încercăm o separare simplă (primul cuvânt = Nume, restul = Prenume)
                    # De obicei pe CI e Numele primul.
                    parts = raw_name.split()
                    if len(parts) >= 2:
                        case.client.last_name = parts[0]
                        case.client.first_name = " ".join(parts[1:])
                    else:
                        case.client.last_name = raw_name

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
        check_status_and_notify(case, processed_doc=doc)

    except Exception as e:
        print(f"--- [AI ERROR] {e} ---")
        if doc:
            try:
                client = get_client(doc.case)
                client.send_text(
                    doc.case,
                    "⚠️ A apărut o eroare la procesarea documentului. Te rog încearcă din nou sau încarcă o poză mai clară.",
                )
            except Exception:
                pass


def get_client(case):
    # Detectare canal preferat bazat pe ultimul mesaj primit
    last_log = case.logs.filter(direction="IN").order_by("-created_at").first()
    if last_log and last_log.channel == "WEB":
        return WebChatClient()
    return WhatsAppClient()


def check_status_and_notify(case, processed_doc=None):
    """
    Verifică ce documente lipsesc și notifică clientul pe WhatsApp/Web.
    """
    # Dacă dosarul este pe mod manual (ex: Service RAR), nu trimitem notificări automate
    if case.is_human_managed:
        return

    client = get_client(case)
    # Pentru WebChatClient, phone_number nu e folosit daca pasam case, dar il pastram pentru WhatsAppClient
    recipient = case  # Folosim obiectul Case pentru a suporta ambele canale

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
            # TOTUL E COMPLET (DOCUMENTE)
            # Dacă rezoluția este deja aleasă, trecem direct la Mandat
            if case.resolution_choice != Case.Resolution.UNDECIDED:
                 case.stage = Case.Stage.SIGNING_MANDATE
                 case.save()

                 # Trimitem link semnare
                 domain = settings.APP_DOMAIN
                 link = f"{domain}/mandat/semneaza/{case.id}/"
                 msg = (
                    "📝 Dosar complet! Mai avem un singur pas: Semnarea Mandatului.\n"
                    f"Te rog intră aici și semnează:\n{link}"
                 )
                 client.send_text(recipient, msg)
            else:
                # Nu avem rezoluția, întrebăm din nou. Nu schimbăm stadiul încă.
                client.send_buttons(
                    recipient,
                    "✅ Am primit toate documentele necesare! Cum dorești să soluționezi dosarul?",
                    ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"],
                )
        else:
            # Încă lipsesc acte
            doc_obj = processed_doc or case.documents.last()
            doc_name = doc_obj.get_doc_type_display() if doc_obj else "Documentul"
            msg = f"👍 Am validat {doc_name}.\nMai am nevoie de:\n- " + "\n- ".join(
                missing
            )
            client.send_text(recipient, msg)


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

        # Creăm un director temporar unic pentru acest task
        task_tmp_dir = tempfile.mkdtemp()

        try:
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

                        # Calea unică în directorul temporar
                        tmp_path = os.path.join(task_tmp_dir, clean_name)

                        # Copiem de la source path la tmp_path
                        # doc.file.path e calea locală
                        shutil.copy(doc.file.path, tmp_path)

                        # Atașăm
                        email.attach_file(tmp_path, content_type)

                        count += 1
                    except Exception as e:
                        print(f"⚠️ Eroare atașare {doc.file.name}: {e}")

            # --- PASUL 4: Trimitere ---
            email.send()

        finally:
            # Curățăm directorul temporar recursiv
            if os.path.exists(task_tmp_dir):
                shutil.rmtree(task_tmp_dir)

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
    # Folosim IMAP_HOST dacă e definit (pentru separare de SMTP), altfel fallback la EMAIL_HOST
    IMAP_HOST = os.getenv("IMAP_HOST", os.getenv("EMAIL_HOST", "imap.gmail.com"))
    # Preferăm variabile dedicate pentru IMAP, altfel fallback la cele de email general
    IMAP_USER = os.getenv("IMAP_USER", os.getenv("EMAIL_HOST_USER"))
    IMAP_PASS = os.getenv("IMAP_PASSWORD", os.getenv("EMAIL_HOST_PASSWORD"))

    if not IMAP_USER or not IMAP_PASS:
        print("❌ Lipsă credențiale IMAP")
        return

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST)
        mail.login(IMAP_USER, IMAP_PASS)
        mail.select("inbox")

        # Căutăm mesaje necitite care conțin "Dosar" în subiect
        # Optimizare: nu procesăm spam-ul sau alte emailuri
        status, messages = mail.search(None, '(UNSEEN SUBJECT "Dosar")')
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
                                # Salvăm Message-ID pentru Reply
                                msg_id = msg.get("Message-ID")
                                if msg_id:
                                    case.last_email_message_id = msg_id
                                    case.save()

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

                                client = get_client(case)
                                recipient = case

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

                                    client.send_buttons(
                                        recipient,
                                        f"📢 Am primit o OFERTĂ de la asigurator!\n\nDin textul emailului: {body[:300]}...\n\nCe dorești să faci?",
                                        ["Accept Oferta", "Schimb Optiunea"] # Max 3 buttons usually.
                                    )
                                else:
                                    # Forwardăm mesajul către client (Relay)
                                    print(f"ℹ️ Mesaj info pentru {case.id} -> Forward WhatsApp")
                                    msg_forward = (
                                        f"📩 Mesaj nou de la asigurator:\n\n{body[:800]}...\n\n"
                                        "👉 Răspunde aici (text sau poze) și voi trimite răspunsul tău direct la asigurator."
                                    )
                                    client.send_text(recipient, msg_forward)

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

        # Detalii Auto
        victim_vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()
        auto_details = f"Auto: {victim_vehicle.license_plate} (VIN: {victim_vehicle.vin_number})" if victim_vehicle else ""

        body = f"""
        Buna ziua,

        Ref: Dosar de dauna {case.insurer_claim_number or str(case.id)[:8]}
        {auto_details}

        CERERE DE DESPĂGUBIRE

        Subsemnatul {case.client.full_name}, având CNP {case.client.cnp},
        prin prezenta ACCEPT oferta de despăgubire în valoare de {offer_val}.

        Vă rog să efectuați plata în contul:{iban_info}

        Solicităm închiderea dosarului după efectuarea plății.

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


# --- TASK 6: Relay WhatsApp -> Email ---
@shared_task
def relay_message_to_insurer_task(case_id, message_text, media_urls=None):
    try:
        case = Case.objects.get(id=case_id)
        if not case.insurer_email:
            return

        print(f"📧 [RELAY] Trimit reply la asigurator pentru dosar {case.id}")

        subject = f"Re: Avizare Dauna Auto - {case.client.full_name} - Dosar {str(case.id)[:8]}"

        body = f"""
        Buna ziua,

        Clientul a transmis urmatorul raspuns/documente:

        "{message_text}"

        Cu stima,
        Echipa Auto Daune
        """

        headers = {}
        if case.last_email_message_id:
            headers["In-Reply-To"] = case.last_email_message_id
            headers["References"] = case.last_email_message_id

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[case.insurer_email],
            headers=headers,
            cc=["office@autodaune.ro"]
        )

        # Download and attach media if any
        temp_files_to_cleanup = []
        if media_urls:
            for url, mime_type in media_urls:
                try:
                    r = requests.get(url, timeout=15, stream=True)
                    if r.status_code == 200:
                        fname = url.split("/")[-1]
                        # Extensii
                        if "image" in mime_type:
                             if not fname.endswith((".jpg", ".png", ".jpeg")):
                                 fname += ".jpg"
                        elif "pdf" in mime_type:
                             if not fname.endswith(".pdf"):
                                 fname += ".pdf"

                        # Salvăm în temp file
                        tmp_fd, tmp_path = tempfile.mkstemp(suffix=f"_{fname}")
                        os.close(tmp_fd)

                        with open(tmp_path, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)

                        email.attach_file(tmp_path, mime_type)
                        temp_files_to_cleanup.append(tmp_path)

                except Exception as e:
                    print(f"⚠️ Eroare download relay {url}: {e}")

        try:
            email.send()
        finally:
             for p in temp_files_to_cleanup:
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except:
                    pass
        print(f"✅ Email relay trimis!")

    except Exception as e:
        print(f"Eroare relay email: {e}")
