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

        # Folosim update() atomic pentru a evita Race Condition pe flag-uri
        updates = {}

        if "CI" in tip_ai or "BULETIN" in tip_ai:
            doc.doc_type = CaseDocument.DocType.ID_CARD
            updates["has_id_card"] = True

            # Optional: Salvăm CNP pe client
            date = result.get("date_extrase", {})
            if date.get("cnp"):
                # Refresh client before save just in case
                case.client.refresh_from_db()
                case.client.cnp = date.get("cnp")

                raw_name = date.get("nume", "").strip()
                if raw_name:
                    # Încercăm o separare simplă (primul cuvânt = Nume, restul = Prenume)
                    parts = raw_name.split()
                    if len(parts) >= 2:
                        case.client.last_name = parts[0]
                        case.client.first_name = " ".join(parts[1:])
                    else:
                        case.client.last_name = raw_name
                case.client.save()

        elif "TALON" in tip_ai:
            doc.doc_type = CaseDocument.DocType.CAR_REGISTRATION
            updates["has_car_coupon"] = True

        elif "CIV" in tip_ai:
            doc.doc_type = CaseDocument.DocType.CAR_IDENTITY
            updates["has_car_identity"] = True

        elif "RCA_PAGUBIT" in tip_ai:
            doc.doc_type = CaseDocument.DocType.VICTIM_RCA
            updates["has_victim_rca"] = True

        elif "AMIABILA" in tip_ai or "CONSTATARE" in tip_ai:
            doc.doc_type = CaseDocument.DocType.ACCIDENT_REPORT
            updates["has_accident_report"] = True

        elif "PROCURA" in tip_ai:
            doc.doc_type = CaseDocument.DocType.MANDATE_UNSIGNED

        elif "EXTRAS" in tip_ai:
            doc.doc_type = CaseDocument.DocType.BANK_STATEMENT
            updates["has_bank_statement"] = True
            # Optional: Save IBAN
            iban = result.get("date_extrase", {}).get("iban")
            if iban:
                case.client.refresh_from_db()
                case.client.iban = iban
                case.client.save()

        elif "ACTE_VINOVAT" in tip_ai:
            doc.doc_type = CaseDocument.DocType.GUILTY_PARTY_DOCS
            updates["has_guilty_docs"] = True

        elif "FOTO_AUTO" in tip_ai:
            doc.doc_type = CaseDocument.DocType.DAMAGE_PHOTO

        # Salvăm documentul (local changes to doc instance)
        doc.save()

        # Aplicăm update-urile atomice pe Case
        if updates:
            Case.objects.filter(pk=case.pk).update(**updates)

        # 3. Verificare Flux și Notificare (Consolidată)
        # Verificăm dacă mai sunt alte documente în procesare pentru acest dosar
        from django.utils import timezone
        import datetime

        recent_threshold = timezone.now() - datetime.timedelta(minutes=5)

        # Numărăm documentele pending (excluzând cel curent, deși el e deja salvat cu ocr_data deci nu mai e pending)
        # Atenție: JSONField gol poate fi 'null' sau '{}'. FlowManager pune '{}'.
        pending_count = (
            CaseDocument.objects.filter(
                case=case, uploaded_at__gte=recent_threshold, ocr_data__exact={}
            )
            .exclude(id=doc.id)
            .count()
        )

        if pending_count == 0:
            # Suntem ultimul task din "lot". Notificăm.
            check_status_and_notify(case)
        else:
            print(f"⏳ Încă {pending_count} documente în procesare. Aștept.")

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
    # 0. Refresh Case pentru a vedea flag-urile actualizate de alte task-uri
    try:
        case.refresh_from_db()
    except Exception:
        pass

    # Dacă dosarul este pe mod manual (ex: Service RAR), nu trimitem notificări automate
    if case.is_human_managed:
        return

    client = get_client(case)
    recipient = case

    # 1. Identificare Documente Procesate Recent (Lotul curent)
    # Căutăm documente procesate (cu ocr_data) în ultimele 5 minute
    from django.utils import timezone
    import datetime

    # Folosim uploaded_at ca proxy pentru "batch"
    recent_threshold = timezone.now() - datetime.timedelta(minutes=5)

    # Excludem documentele vechi care au fost deja validate in trecut
    recent_docs = CaseDocument.objects.filter(
        case=case, uploaded_at__gte=recent_threshold
    ).exclude(ocr_data__exact={})

    # Construim lista de documente validate și erori
    validated_names = []
    error_messages = []

    for d in recent_docs:
        # Verificăm dacă e un tip valid sau Unknown
        if d.doc_type == CaseDocument.DocType.UNKNOWN:
            # E posibil să fie un document nerelevant sau eroare AI
            fname = os.path.basename(d.file.name)
            error_messages.append(
                f"⚠️ Nu am putut identifica documentul '{fname}'. Te rog încarcă doar: Buletin, Talon, Amiabilă sau Video."
            )
        else:
            validated_names.append(d.get_doc_type_display())

    # De-duplicate names
    validated_names = sorted(list(set(validated_names)))

    # 2. Lista de verificare (Ce mai lipsește?)
    missing = []
    if not case.has_id_card:
        missing.append("Buletin (obligatoriu)")
    if not case.has_car_coupon:
        missing.append("Talon Auto (obligatoriu)")
    if not case.has_car_identity:
        missing.append("Cartea Mașinii - CIV (obligatoriu)")
    if not case.has_victim_rca:
        missing.append("Polița RCA Păgubit (obligatoriu)")
    if not case.has_accident_report:
        missing.append("Amiabilă / PV Politie (obligatoriu)")

    # Conditie: Video 360 SAU Minim 4 Poze
    damage_photos_count = CaseDocument.objects.filter(
        case=case, doc_type=CaseDocument.DocType.DAMAGE_PHOTO
    ).count()

    if not case.has_scene_video and damage_photos_count < 4:
        missing.append(
            f"Video 360 Grade SAU minim 4 Poze Auto (ai trimis {damage_photos_count})"
        )

    # Condiție Extras Cont
    if case.resolution_choice == Case.Resolution.OWN_REGIME:
        if not case.has_bank_statement:
            missing.append("Extras Cont Bancar (pt. Regie Proprie)")

    # Verificăm stadiul curent pentru a nu trimite mesaje inutile
    if case.stage == Case.Stage.COLLECTING_DOCS:
        if not missing:
            # TOTUL E COMPLET (DOCUMENTE)
            if case.resolution_choice != Case.Resolution.UNDECIDED:
                case.stage = Case.Stage.SIGNING_MANDATE
                case.save()

                domain = settings.APP_DOMAIN
                link = f"{domain}/mandat/semneaza/{case.id}/"
                msg = (
                    "📝 Dosar complet! Mai avem un singur pas: Semnarea Mandatului.\n"
                    f"Te rog intră aici și semnează:\n{link}"
                )
                client.send_text(recipient, msg)
            else:
                client.send_buttons(
                    recipient,
                    "✅ Am primit toate documentele necesare! Cum dorești să soluționezi dosarul?",
                    ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"],
                )
        else:
            # Încă lipsesc acte. Construim mesajul consolidat.
            parts = []

            # A. Validari (Dacă avem ceva validat recent)
            if validated_names:
                doc_list_str = ", ".join(validated_names)
                parts.append(f"👍 Am validat: {doc_list_str}.")

            # B. Erori
            if error_messages:
                parts.extend(error_messages)

            # C. Missing
            parts.append("Mai am nevoie de:\n- " + "\n- ".join(missing))

            full_msg = "\n".join(parts)
            client.send_text(recipient, full_msg)


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
        Echipa ASociatia PAgubitilor RCA
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
                        doc_label = doc.get_doc_type_display().replace("/", "_").replace(" ", "_")
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
                            case = Case.objects.filter(
                                id__startswith=case_id_prefix
                            ).first()

                            if case:
                                # Salvăm Message-ID pentru Reply
                                msg_id = msg.get("Message-ID")
                                if msg_id:
                                    case.last_email_message_id = msg_id
                                    case.save()

                                # Parsăm body și extragem atașamente
                                body = ""
                                downloaded_attachments = []

                                if msg.is_multipart():
                                    for part in msg.walk():
                                        content_type = part.get_content_type()
                                        content_disposition = str(part.get("Content-Disposition"))

                                        # Extragere Text
                                        if content_type == "text/plain" and "attachment" not in content_disposition:
                                            payload = part.get_payload(decode=True)
                                            if payload:
                                                body = payload.decode(errors="ignore")

                                        # Extragere Atașamente
                                        elif "attachment" in content_disposition or part.get_filename():
                                            filename = part.get_filename()
                                            if filename:
                                                payload = part.get_payload(decode=True)
                                                if payload:
                                                    from django.core.files.base import ContentFile

                                                    # Salvăm în model
                                                    doc = CaseDocument.objects.create(
                                                        case=case,
                                                        doc_type=CaseDocument.DocType.UNKNOWN,
                                                        ocr_data={}
                                                    )

                                                    # Nume fișier unic
                                                    clean_name = f"email_{case.id}_{filename}".replace(" ", "_")
                                                    doc.file.save(clean_name, ContentFile(payload))

                                                    downloaded_attachments.append(doc)

                                                    # Declanșăm OCR
                                                    analyze_document_task.delay(doc.id)
                                else:
                                    payload = msg.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode(errors="ignore")

                                # 2. Analizăm conținutul
                                body_lower = body.lower()
                                keywords_offer = [
                                    "oferta",
                                    "propunere",
                                    "despagubire",
                                    "suma de",
                                    "acceptul",
                                ]
                                is_offer = any(k in body_lower for k in keywords_offer)

                                client = get_client(case)
                                recipient = case

                                if is_offer:
                                    print(f"💰 OFERTA DETECTATA pentru {case.id}")
                                    case.stage = Case.Stage.OFFER_DECISION

                                    # Încercăm să extragem suma (simplistic)
                                    # Ex: "suma de 1200 RON"
                                    amount_match = re.search(
                                        r"(\d+([.,]\d+)?)\s*(ron|lei)", body_lower
                                    )
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
                                        [
                                            "Accept Oferta",
                                            "Schimb Optiunea",
                                        ],  # Max 3 buttons usually.
                                    )
                                else:
                                    # Forwardăm mesajul către client (Relay)
                                    print(
                                        f"ℹ️ Mesaj info pentru {case.id} -> Forward WhatsApp"
                                    )
                                    # Generare Link-uri pt atașamente dacă e cazul
                                    attachments_info = ""
                                    if downloaded_attachments:
                                        attachments_info = "\n\n📄 **Documente atașate:**\n"
                                        domain = settings.APP_DOMAIN.rstrip("/")
                                        media_url_path = settings.MEDIA_URL.strip("/")
                                        for d in downloaded_attachments:
                                            url = f"{domain}/{media_url_path}/{d.file.name}"
                                            attachments_info += f"- {url}\n"

                                    msg_forward = (
                                        f"📩 Mesaj nou de la asigurator:\n\n{body[:800]}...\n"
                                        f"{attachments_info}\n"
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

        subject = (
            f"Acceptare Oferta - Dosar {str(case.id)[:8]} - {case.client.full_name}"
        )

        # Detalii bancare
        iban_info = ""
        if case.resolution_choice == Case.Resolution.OWN_REGIME and case.client.iban:
            iban_info = f"\nCont IBAN: {case.client.iban}\nTitular Cont: {case.client.full_name}"

        offer_val = (
            f"{case.settlement_offer_value} RON"
            if case.settlement_offer_value
            else "(Conform ofertei transmise)"
        )

        # Detalii Auto
        victim_vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()
        auto_details = (
            f"Auto: {victim_vehicle.license_plate} (VIN: {victim_vehicle.vin_number})"
            if victim_vehicle
            else ""
        )

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
            cc=["office@autodaune.ro"],
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
            cc=["office@autodaune.ro"],
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
            cc=["office@autodaune.ro"],
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

                        with open(tmp_path, "wb") as f:
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

@shared_task
def send_admin_new_case_email_task(case_id):
    """
    Trimite un email catre office@aprca.ro cand se deschide un dosar nou.
    """
    try:
        case = Case.objects.get(id=case_id)
        client = case.client
        target_email = "office@aprca.ro"

        # Cautam vehiculul pentru a lua numarul de inmatriculare, daca exista deja
        victim_vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()
        plate_info = f"Număr Auto: {victim_vehicle.license_plate}" if victim_vehicle and victim_vehicle.license_plate else "Număr Auto: N/A"

        subject = f"Notificare: Dosar nou deschis de {client.full_name or client.phone_number}"

        body = f"""
        Salut,

        Un dosar nou a fost deschis in sistem.

        Detalii client:
        Nume: {client.full_name or '-'}
        Telefon: {client.phone_number}
        {plate_info}

        Poti vizualiza dosarul in panoul de administrare.

        Cu stima,
        Echipa Auto Daune
        """

        email = EmailMessage(
            subject=subject,
            body=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[target_email]
        )
        email.send()
        print(f"✅ Email de notificare dosar nou trimis pentru dosar {case.id}")

    except Exception as e:
        print(f"❌ Eroare la trimiterea emailului de notificare dosar nou: {e}")
