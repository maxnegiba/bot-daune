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

        # Mapăm tipul primit de la AI la Enum-ul din Django
        tip_ai = result.get("tip_document", "").upper()

        # Treat 'UNKNOWN' as failure and queue for multi-image logic
        if tip_ai == "UNKNOWN":
            from django.core.cache import cache

            # Save empty data to indicate processing finished but failed
            doc.ocr_data = {}
            doc.doc_type = CaseDocument.DocType.UNKNOWN
            doc.save()

            cache_key = f"civ_wait_notified_{case.id}"

            # Check if we already notified the user recently
            if not cache.get(cache_key):
                client = get_client(case)
                client.send_text(
                    case,
                    "⚠️ Nu am putut identifica complet acest document. Dacă are mai multe pagini (cum ar fi CIV), te rog încarcă restul acum. Voi aștepta 15 secunde..."
                )
                # Set cache to avoid spamming the user
                cache.set(cache_key, True, timeout=120)

            # Queue the multi-image fallback check to run in 15 seconds
            # Use import delay locally to avoid circular dependencies if any
            from apps.claims.tasks import process_grouped_unknowns_task
            process_grouped_unknowns_task.apply_async(args=[case.id], countdown=15)

            return

        # 2. Salvare date OCR
        # NOTA: Acest save() va declanșa signals.py care populează vehiculele!
        doc.ocr_data = result

        # Folosim update() atomic pentru a evita Race Condition pe flag-uri
        updates = {}

        if "CIV" in tip_ai:
            doc.doc_type = CaseDocument.DocType.CAR_IDENTITY
            updates["has_car_identity"] = True

        elif "CI" in tip_ai or "BULETIN" in tip_ai:
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
            # Check cache to avoid spamming the generic error if the user is already waiting
            # for the multi-image processor to give a final verdict.
            from django.core.cache import cache
            cache_key = f"civ_wait_notified_{case.id}"

            # If the wait notification is currently active, we suppress the immediate rejection message.
            if not cache.get(cache_key):
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
            # Acum, inainte de a merge la Semnatura/Rezolutie, trebuie sa aflam asiguratorul vinovatului
            guilty_vehicle = case.vehicles.filter(is_offender=True).first()
            if not guilty_vehicle:
                # Trecem in stadiul de selectare a asiguratorului vinovatului
                case.stage = Case.Stage.SELECTING_GUILTY_INSURER
                case.save()

                # Extragem toti asiguratorii din baza de date pentru a-i afisa utilizatorului
                from apps.claims.models import Insurer
                all_sys_insurers = Insurer.objects.all().order_by('name')

                if all_sys_insurers.exists():
                    msg_parts = [
                        "✅ Am primit toate documentele necesare!\n",
                        "Te rog să îmi spui: **Care este asiguratorul vinovatului?**\n",
                        "Răspunde cu **numele** asiguratorului de mai jos:\n"
                    ]
                    for idx, sys_in in enumerate(all_sys_insurers, 1):
                        msg_parts.append(f"- {sys_in.name}")

                    msg = "\n".join(msg_parts)
                    client.send_text(recipient, msg)
                else:
                    msg = "✅ Am primit toate documentele necesare!\n\nTe rog să îmi scrii: **Care este asiguratorul vinovatului?**"
                    client.send_text(recipient, msg)

            elif case.resolution_choice != Case.Resolution.UNDECIDED:
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


# --- TASK 1.5: Fallback Multi-Image Processing for CIV/Unknowns ---
@shared_task
def process_grouped_unknowns_task(case_id):
    """
    Called 15 seconds after an UNKNOWN document is uploaded.
    Collects all UNKNOWN documents uploaded in the last few minutes and analyzes them together.
    """
    from django.utils import timezone
    import datetime
    from django.core.cache import cache

    try:
        case = Case.objects.get(id=case_id)

        # Căutăm documente "UNKNOWN" recente
        recent_threshold = timezone.now() - datetime.timedelta(minutes=5)
        unknown_docs = CaseDocument.objects.filter(
            case=case,
            doc_type=CaseDocument.DocType.UNKNOWN,
            uploaded_at__gte=recent_threshold
        )

        if unknown_docs.count() < 2:
            # Clear cache so normal error messages resume if it was just one bad photo
            cache.delete(f"civ_wait_notified_{case.id}")
            # Do nothing if there's only 1 document (it was already analyzed individually and failed)
            return

        print(f"--- [AI WORKER MULTI-IMAGE] Analizez {unknown_docs.count()} poze pentru dosar {case.id} ---")

        image_paths = [doc.file.path for doc in unknown_docs if doc.file]

        if not image_paths:
            return

        result = DocumentAnalyzer.analyze_multiple(image_paths)
        print(f"🤖 Rezultat AI Multi-Image: {result}")

        tip_ai = result.get("tip_document", "").upper()

        # Dacă a reușit să extragă CIV (sau altceva)
        if tip_ai != "UNKNOWN":
            updates = {}
            if "CIV" in tip_ai:
                doc_type = CaseDocument.DocType.CAR_IDENTITY
                updates["has_car_identity"] = True
            elif "AMIABILA" in tip_ai or "CONSTATARE" in tip_ai:
                doc_type = CaseDocument.DocType.ACCIDENT_REPORT
                updates["has_accident_report"] = True
            else:
                # Putem trata și alte documente dacă AI-ul le recunoaște
                doc_type = CaseDocument.DocType.UNKNOWN # Fallback sigur

            # Update all unknown docs to the new type so they don't get re-processed
            # We save the OCR data only to the first doc for simplicity
            first_doc = unknown_docs.first()
            if doc_type != CaseDocument.DocType.UNKNOWN:
                for doc in unknown_docs:
                    doc.doc_type = doc_type
                    if doc.id == first_doc.id:
                        doc.ocr_data = result
                    else:
                        doc.ocr_data = {"note": "Atașat la documentul principal."}
                    doc.save()

            if updates:
                Case.objects.filter(pk=case.pk).update(**updates)

            # Clear cache so we can resume normal flow
            cache.delete(f"civ_wait_notified_{case.id}")

            # Notificăm succesul/statusul general
            check_status_and_notify(case)
        else:
            # Eșec și după analiza combinată
            cache.delete(f"civ_wait_notified_{case.id}")
            client = get_client(case)
            client.send_text(
                case,
                "⚠️ Analiza combinată a pozelor nu a reușit să identifice un document valid (CIV etc.). Te rog reîncearcă cu poze mai clare."
            )

    except Exception as e:
        print(f"--- [AI MULTI-IMAGE ERROR] {e} ---")
        cache.delete(f"civ_wait_notified_{case_id}")


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

        # --- PASUL 1.5: Verificare Date Vehicul Victimă ---
        victim_vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()

        if not victim_vehicle or not victim_vehicle.make or not victim_vehicle.license_plate:
            # Lipsesc date despre victimă. Notificăm adminul și oprim trimiterea la asigurator
            print(f"⚠️ [EMAIL WORKER] Date lipsă pentru vehiculul victimei. Trimit alertă către admin.")

            missing_details = []
            if not victim_vehicle:
                missing_details.append("Vehiculul victimei nu este definit (role=VICTIM)")
            else:
                if not victim_vehicle.make:
                    missing_details.append("Marca vehiculului lipsă")
                if not victim_vehicle.license_plate:
                    missing_details.append("Numărul de înmatriculare lipsă")

            alert_subject = f"⚠️ Intervenție Umană Necesară: Dosar {str(case.id)[:8]}"
            alert_body = f"""Salut,

Dosarul {str(case.id)[:8]} (Client: {client.full_name}) nu a putut fi trimis la asigurator din cauza lipsei următoarelor informații esențiale:

{chr(10).join(['- ' + detail for detail in missing_details])}

Te rugăm să completezi aceste date în baza de date și să re-inițiezi trimiterea emailului.
"""
            EmailMessage(
                subject=alert_subject,
                body=alert_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=["office@autodaune.ro"],
            ).send()

            return  # Stop execution, nu mai trimitem la asigurator

        # --- PASUL 2: Construire Email ---
        subject = f"Avizare Dauna Auto - {client.full_name} - Dosar {str(case.id)[:8]}"

        body = f"""Buna ziua,

Subscrisa, Asociația Păgubiților RCA, prin președinte Munteanu Leonard Petre, in calitate de reprezentant al păgubitului(ei) {client.full_name}, conform mandatului din atașament, in temeiul prevederilor Art. 2 Alin (7) din L132/2017 va remitem prezenta,

___________AVIZARE DAUNA RCA__________

prin care va solicitam respectuos sa dispuneți efectuarea constatării avariilor si prejudiciilor la AUTO avariat marca {victim_vehicle.make}, cu nr. înmatriculare {victim_vehicle.license_plate}, cf. documentelor din attach, in conformitate cu prevederile Art. 18 Alin (4) si Alin (5) din N20/2017ASF.
Solicităm constatarea ONLINE.

Contact: {client.phone_number}"""

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

        # Tracking timpul primului mail catre asigurator
        from django.utils import timezone
        case.last_message_to_insurer_at = timezone.now()
        case.save()

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
        status, messages = mail.search(None, "(UNSEEN)")
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

                        # 0. Parsăm body și extragem atașamente
                        body = ""
                        downloaded_attachments_data = [] # To be created as CaseDocument later when we have a case

                        if msg.is_multipart():
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disposition = str(part.get("Content-Disposition"))

                                # Extragere Text
                                if content_type == "text/plain" and "attachment" not in content_disposition:
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        body = payload.decode(errors="ignore")

                                # Extragere Atașamente temporară în memorie (sau extragem doar payload-ul)
                                elif "attachment" in content_disposition or part.get_filename():
                                    filename = part.get_filename()
                                    if filename:
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            downloaded_attachments_data.append({
                                                "filename": filename,
                                                "payload": payload
                                            })
                        else:
                            payload = msg.get_payload(decode=True)
                            if payload:
                                body = payload.decode(errors="ignore")

                        # 1. Căutăm Dosarul robust (prioritate: ID -> NrDosar -> CNP -> NrInmatriculare -> NumePăgubit)
                        case = None

                        full_text = f"{subject} {body}"
                        full_text_lower = full_text.lower()

                        # A) ID Dosar din subiect (Pattern: "Dosar [a-f0-9]{8}")
                        match = re.search(r"Dosar ([a-f0-9]{8})", subject, re.IGNORECASE)
                        if match:
                            case_id_prefix = match.group(1).lower()
                            case = Case.objects.filter(id__startswith=case_id_prefix).order_by('-created_at').first()

                        # B) Număr Dosar Asigurator (dacă există)
                        if not case:
                            # Use iterator and only fetch necessary fields to prevent OOM
                            cases_with_insurer_id = Case.objects.exclude(insurer_claim_number__isnull=True).exclude(insurer_claim_number__exact='').only('id', 'insurer_claim_number').order_by('-created_at').iterator()
                            for c in cases_with_insurer_id:
                                if c.insurer_claim_number.lower() in full_text_lower:
                                    case = Case.objects.get(id=c.id) # get full object
                                    break

                        # C) CNP Păgubit
                        if not case:
                            cnp_match = re.search(r"\b([1-9][0-9]{12})\b", full_text)
                            if cnp_match:
                                extracted_cnp = cnp_match.group(1)
                                case = Case.objects.filter(client__cnp=extracted_cnp).order_by('-created_at').first()

                        # D) Număr Înmatriculare Păgubit
                        if not case:
                            # Normalize text for plates (remove spaces/dashes)
                            normalized_text = re.sub(r'[\s\-]', '', full_text_lower)
                            # Get distinct valid plates for victim vehicles
                            victim_plates = InvolvedVehicle.objects.filter(role=InvolvedVehicle.Role.VICTIM).exclude(license_plate__isnull=True).exclude(license_plate__exact='').values_list('license_plate', flat=True).distinct()

                            for plate in victim_plates:
                                normalized_plate = re.sub(r'[\s\-]', '', plate.lower())
                                if normalized_plate and normalized_plate in normalized_text:
                                    # Found plate, find the newest case for it
                                    case = Case.objects.filter(vehicles__role=InvolvedVehicle.Role.VICTIM, vehicles__license_plate=plate).order_by('-created_at').first()
                                    if case:
                                        break

                        # E) Nume și Prenume Păgubit
                        if not case:
                            # Use iterator and only necessary fields
                            clients = Client.objects.exclude(first_name__isnull=True).exclude(last_name__isnull=True).exclude(first_name__exact='').exclude(last_name__exact='').only('id', 'first_name', 'last_name').order_by('-created_at').iterator()
                            for c in clients:
                                fn = c.first_name.strip().lower()
                                ln = c.last_name.strip().lower()

                                # Use word boundaries to prevent false positives (e.g. 'Ion' matching 'Action')
                                # Escape the names in case they have special regex characters
                                fn_escaped = re.escape(fn)
                                ln_escaped = re.escape(ln)

                                # Check if both parts are in the text as distinct words
                                if fn and ln:
                                    fn_match = re.search(r'\b' + fn_escaped + r'\b', full_text_lower)
                                    ln_match = re.search(r'\b' + ln_escaped + r'\b', full_text_lower)

                                    if fn_match and ln_match:
                                        case = Case.objects.filter(client_id=c.id).order_by('-created_at').first()
                                        if case:
                                            break

                        if case:
                            # Salvăm Message-ID pentru Reply
                            msg_id = msg.get("Message-ID")
                            if msg_id:
                                case.last_email_message_id = msg_id
                                case.save()

                            downloaded_attachments = []
                            from django.core.files.base import ContentFile

                            for att_data in downloaded_attachments_data:
                                doc = CaseDocument.objects.create(
                                    case=case,
                                    doc_type=CaseDocument.DocType.UNKNOWN,
                                    ocr_data={}
                                )
                                clean_name = f"email_{case.id}_{att_data['filename']}".replace(" ", "_")
                                doc.file.save(clean_name, ContentFile(att_data["payload"]))
                                downloaded_attachments.append(doc)
                                analyze_document_task.delay(doc.id)

                            from django.utils import timezone
                            case.last_message_from_insurer_at = timezone.now()
                            case.save()

                            client = get_client(case)
                            recipient = case

                            print(f"ℹ️ Mesaj de la asigurator pentru {case.id} -> Forward WhatsApp")

                            # Generare Link-uri pt atașamente dacă e cazul
                            attachments_info = ""
                            if downloaded_attachments:
                                attachments_info = "\n\n📄 **Documente atașate:**\n"
                                domain = settings.APP_DOMAIN.rstrip("/")
                                media_url_path = settings.MEDIA_URL.strip("/")
                                for d in downloaded_attachments:
                                    url = f"{domain}/{media_url_path}/{d.file.name}"
                                    attachments_info += f"- {url}\n"

                            # Trimitem ca o poștă
                            msg_forward = (
                                f"Asigurătorul vă transmite următoarele informații:\n\n"
                                f"{body[:1000]}...\n"
                                f"{attachments_info}\n"
                                "Ce doriți să îi răspundeți? (Scrieți un mesaj sau încărcați documente, iar noi le vom trimite mai departe).\n\n"
                                "Dacă sunteți de acord cu oferta/răspunsul și doriți să finalizăm cazul, apăsați pe butoanele de mai jos."
                            )

                            client.send_buttons(
                                recipient,
                                msg_forward,
                                ["Accept Oferta", "Service RAR", "Dauna Totala"]
                            )

                        else:
                            print(f"⚠️ Nu am putut asocia emailul '{subject}' niciunui dosar existent. Ignorat.")
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
def trigger_delayed_relay_task(case_id):
    """
    Called when a user sends a message in PROCESSING_INSURER stage.
    Instead of sending immediately, we wait 30 minutes, gather all user messages from the last 30 minutes,
    and send them as a single email.
    """
    from .models import CommunicationLog
    from django.utils import timezone
    import datetime

    try:
        case = Case.objects.get(id=case_id)
        if not case.insurer_email:
            return

        # If the case is no longer in the processing phase, abort the relay
        if case.stage != Case.Stage.PROCESSING_INSURER:
            return

        # Check if 30 minutes have passed since the LAST message the user sent
        # We can just check the newest incoming message in the last 30 minutes.
        threshold = timezone.now() - datetime.timedelta(minutes=30)

        # If there is a message newer than threshold, it means the user is still active.
        # We should wait. We'll reschedule this task.
        # Actually, if we just delay the task by 30 mins when called,
        # when it runs, we check if any message was sent in the last 30 mins.
        # If yes, we wait again.

        last_in_msg = case.logs.filter(direction="IN").order_by('-created_at').first()
        if last_in_msg:
            time_since_last_msg = timezone.now() - last_in_msg.created_at
            if time_since_last_msg < datetime.timedelta(minutes=29):
                # User sent something recently. Let's reschedule for 30 mins after that last message.
                remaining_wait = datetime.timedelta(minutes=30) - time_since_last_msg
                trigger_delayed_relay_task.apply_async(args=[case.id], countdown=remaining_wait.total_seconds())
                return

        # If we reached here, no new message in the last 30 mins.
        # Let's gather all messages sent by the user since the last time we emailed the insurer.
        # Or simply, all IN logs since last_message_to_insurer_at

        last_to_insurer = case.last_message_to_insurer_at

        logs_to_send = case.logs.filter(
            direction="IN",
            created_at__gt=last_to_insurer if last_to_insurer else case.created_at
        ).order_by('created_at')

        if not logs_to_send.exists():
            return

        print(f"📧 [RELAY] Trimit reply la asigurator pentru dosar {case.id} (Grupat)")

        # Colectam textul
        text_messages = []
        for log in logs_to_send:
            if log.content and log.content.strip() and not log.content.startswith("{"): # ignore purely JSON logs or empty
                # Avoid adding button clicks like "Accept Oferta" if they somehow got here
                if log.content.lower() not in ["accept oferta", "service rar", "dauna totala"]:
                    text_messages.append(log.content)

        combined_text = "\n\n".join(text_messages)
        if not combined_text:
            combined_text = "(Clientul a trimis doar atașamente)"

        subject = f"Re: Avizare Dauna Auto - {case.client.full_name} - Dosar {str(case.id)[:8]}"

        body = f"""
        Buna ziua,

        Clientul nostru doreste sa va transmita urmatoarele informatii:

        {combined_text}

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

        # Colectăm și atașamentele (CaseDocument adăugate după ultimul mail)
        docs_to_send = CaseDocument.objects.filter(
            case=case,
            uploaded_at__gt=last_to_insurer if last_to_insurer else case.created_at
        )

        temp_files_to_cleanup = []
        count = 0

        task_tmp_dir = tempfile.mkdtemp()

        try:
            for doc in docs_to_send:
                if doc.file:
                    try:
                        fname = doc.file.name.lower()
                        if fname.endswith(".pdf"):
                            content_type = "application/pdf"
                        elif fname.endswith(".png"):
                            content_type = "image/png"
                        elif fname.endswith(".jpg") or fname.endswith(".jpeg"):
                            content_type = "image/jpeg"
                        elif fname.endswith(".mp4"):
                            content_type = "video/mp4"
                        else:
                            content_type = "application/octet-stream"

                        doc_label = doc.get_doc_type_display().replace("/", "_").replace(" ", "_")
                        clean_name = f"{doc_label}_{count}.{fname.split('.')[-1]}"
                        tmp_path = os.path.join(task_tmp_dir, clean_name)

                        shutil.copy(doc.file.path, tmp_path)
                        email.attach_file(tmp_path, content_type)
                        count += 1
                    except Exception as e:
                        print(f"⚠️ Eroare atașare relay {doc.file.name}: {e}")

            email.send()

            # Update timestamp
            case.last_message_to_insurer_at = timezone.now()
            case.save()

            # Notificam clientul ca s-a trimis
            client = get_client(case)
            client.send_text(case, "✅ Răspunsul tău a fost grupat și transmis către asigurător.")

            # Curățăm flagul de relay delay
            from django.core.cache import cache
            cache.delete(f"relay_notified_{case.id}")

        finally:
            if os.path.exists(task_tmp_dir):
                shutil.rmtree(task_tmp_dir)

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

@shared_task
def send_24h_reminders_task():
    """
    Task care rulează zilnic pentru a trimite remindere.
    Verifică dosarele în stadiul PROCESSING_INSURER.
    Dacă ultimul mesaj este către asigurător și au trecut 24h, trimitem reminder asigurătorului.
    Dacă ultimul mesaj este de la asigurător și au trecut 24h, trimitem reminder clientului.
    """
    from .models import Case
    from django.utils import timezone
    import datetime

    # Doar în zilele lucrătoare? (Opțional - implementăm o verificare simplă de weekend)
    # today_weekday = timezone.now().weekday()
    # if today_weekday >= 5: # 5=Sâmbătă, 6=Duminică
    #     return

    threshold = timezone.now() - datetime.timedelta(hours=24)
    cases = Case.objects.filter(stage=Case.Stage.PROCESSING_INSURER)

    for case in cases:
        # Aflăm cine așteaptă după cine.
        # Comparăm last_message_to_insurer_at cu last_message_from_insurer_at

        last_to = case.last_message_to_insurer_at
        last_from = case.last_message_from_insurer_at

        # Dacă nu avem deloc to_insurer, înseamnă că nu s-a trimis primul mail
        if not last_to:
            continue

        waiting_on_insurer = False
        waiting_on_client = False

        if not last_from:
            # Am trimis primul mail și nu am primit răspuns
            waiting_on_insurer = True
            time_since = timezone.now() - last_to
        else:
            if last_to > last_from:
                waiting_on_insurer = True
                time_since = timezone.now() - last_to
            else:
                waiting_on_client = True
                time_since = timezone.now() - last_from

        # Trimitem reminder doar o dată (verificăm dacă am trimis deja ceva în ultimele 24h)
        from django.core.cache import cache
        cache_key = f"reminder_24h_sent_{case.id}"

        if time_since >= datetime.timedelta(hours=24) and not cache.get(cache_key):
            if waiting_on_insurer and case.insurer_email:
                # Trimitem mail asiguratorului
                try:
                    subject = f"Reminder: Avizare Dauna Auto - {case.client.full_name} - Dosar {str(case.id)[:8]}"
                    body = f"""
                    Buna ziua,

                    Revenim la emailul anterior. Asteptam un raspuns din partea dumneavoastra privind dosarul clientului nostru.

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
                    email.send()
                    cache.set(cache_key, True, timeout=86400) # set cache to not resend for next 24h
                    print(f"✅ Reminder 24h trimis asigurătorului pentru dosar {case.id}")
                except Exception as e:
                    print(f"⚠️ Eroare reminder asigurător: {e}")

            elif waiting_on_client:
                # Trimitem mesaj pe chat clientului
                try:
                    client = get_client(case)
                    msg = (
                        "Salut, asigurătorul așteaptă un răspuns de la tine de mai bine de 24 de ore.\n\n"
                        "Te rugăm să ne scrii mesajul tău pentru a-l transmite mai departe, sau alege una din opțiunile de mai jos dacă dorești să finalizăm dosarul."
                    )
                    client.send_buttons(
                        case,
                        msg,
                        ["Accept Oferta", "Service RAR", "Dauna Totala"]
                    )
                    cache.set(cache_key, True, timeout=86400) # set cache to not resend for next 24h
                    print(f"✅ Reminder 24h trimis clientului pentru dosar {case.id}")
                except Exception as e:
                    print(f"⚠️ Eroare reminder client: {e}")
