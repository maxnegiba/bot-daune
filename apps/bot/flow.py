import os
import requests
import tempfile
from django.core.files import File
from django.conf import settings
from apps.claims.models import Case, CaseDocument
from apps.claims.tasks import analyze_document_task
from .utils import WhatsAppClient, WebChatClient


class FlowManager:
    def __init__(self, case, sender_phone, channel="WHATSAPP"):
        self.case = case
        self.phone = sender_phone
        self.channel = channel

        if channel == "WEB":
            self.client = WebChatClient()
        else:
            self.client = WhatsAppClient()

    def process_message(self, message_type, content, media_urls=None):
        """
        Router principal în funcție de etapa dosarului.
        Acceptă și media_urls pentru imaginile venite de la Twilio.
        """
        # 0. Verificăm intervenția umană
        if self.case.is_human_managed:
            # EXCEPȚIE: Dacă e Web Chat și avem fișiere, le procesăm "silentios"
            if self.channel == "WEB" and media_urls:
                self._handle_image_upload(media_urls, silent=True)
            return

        stage = self.case.stage

        # --- ETAPA 1: GREETING (Salut / Alegere Flux) ---
        if stage == Case.Stage.GREETING:
            self._handle_greeting(content)

        # --- ETAPA 2: COLLECTING DOCS (Documente & Rezoluție) ---
        elif stage == Case.Stage.COLLECTING_DOCS:
            if message_type == "image" and media_urls:
                self._handle_image_upload(media_urls)
            else:
                # Dacă primim text, verificăm dacă e o alegere de rezoluție
                if self._try_handle_resolution_text(content):
                    return

                # Altfel, verificăm statusul documentelor (poate userul întreabă ceva)
                self._check_documents_status()

        # --- ETAPA 3: SELECTING RESOLUTION (Legacy / Fallback) ---
        elif stage == Case.Stage.SELECTING_RESOLUTION:
            self._try_handle_resolution_text(content)

        # --- ETAPA 4: SEMNATURA (Mandat) ---
        elif stage == Case.Stage.SIGNING_MANDATE:
            self._send_signature_link()

        # --- ALTE ETAPE ---
        elif stage == Case.Stage.PROCESSING_INSURER:
            # Relay: Orice trimite userul, trimitem la asigurator
            from apps.claims.tasks import relay_message_to_insurer_task

            relay_message_to_insurer_task.delay(self.case.id, content, media_urls)
            self.client.send_text(
                self.case,
                "✅ Am transmis mesajul/documentele către asigurator."
            )
        elif stage == Case.Stage.OFFER_DECISION:
             self._handle_offer_decision(content)

    # =========================================================================
    # LOGICA DETALIATĂ
    # =========================================================================

    def _handle_greeting(self, text):
        text = text.lower()

        # Verificăm dacă userul vrea să deschidă dosarul
        if "da" in text or "deschide" in text:
            # Trecem la pasul următor
            self.case.stage = Case.Stage.COLLECTING_DOCS
            self.case.save()

            # 1. Mesaj UNIC: Documente + Context
            msg_full = (
                "✅ Am deschis dosarul. Te rog să încarci următoarele documente (poze clare):\n\n"
                "📌 **OBLIGATORIU:**\n"
                "- Buletinul (CI) persoanei păgubite\n"
                "- Talonul (Certificat Înmatriculare) auto avariat\n"
                "- Amiabila sau Proces Verbal Poliție\n"
                "- Video 360° cu mașina avariată SAU minim 4 poze (din toate colțurile + daune)\n\n"
                "Instrucțiuni Poze: Te rog fă 4 poze din colțurile mașinii (față-stânga, față-dreapta, spate-stânga, spate-dreapta) și poze detaliate cu dauna.\n\n"
                "📌 **OPȚIONAL (Dacă ai):**\n"
                "- Autorizație Reparație (de la Poliție)\n"
                "- Documente șofer vinovat (RCA, Talon, CI)\n"
                "- Alte documente relevante\n\n"
                "Extras Cont Bancar (dacă dorești Regie Proprie)\n\n"
                "👇 Te rog răspunde ACUM la întrebarea de mai jos, apoi poți începe încărcarea pozelor:"
            )
            self.client.send_text(self.case, msg_full)

            # 2. Butoane Rezoluție (Imediat după)
            msg_res = "Cum dorești să soluționezi acest dosar?"
            self.client.send_buttons(
                self.case,
                msg_res,
                ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"]
            )

        elif "alta" in text or "nu" in text:
            self.case.is_human_managed = True
            self.case.save()
            self.client.send_text(
                self.case,
                "Am înțeles. Un operator uman a fost notificat și te va contacta în curând.",
            )
        else:
            # Mesaj Greeting Inițial
            self.client.send_buttons(
                self.case,
                "Salut! Dorești să deschidem un dosar de daună?",
                ["DA, Deschide Dosar", "NU, Am altă problemă"],
            )

    def _handle_image_upload(self, media_urls, silent=False):
        saved_count = 0
        has_async_processing = False

        for url, mime_type in media_urls:
            try:
                # Dacă e URL local (uploadat via Web), nu avem nevoie de requests.get neapărat
                # Dar pentru uniformitate, dacă vine ca URL, îl tratăm la fel.
                # Totuși, Web Chat va trimite probabil fișierele direct la endpoint,
                # iar endpoint-ul va salva fișierele și va pasa doar calea sau obiectul.
                # DAR, FlowManager e construit pe ideea de `media_urls`.
                # Pentru Web Chat API, voi face upload-ul în view și voi genera un URL local temporar sau persistent.

                headers = {"User-Agent": "Mozilla/5.0"}
                r = requests.get(url, headers=headers, timeout=15, stream=True)
                if r.status_code == 200:
                    ext = mime_type.split("/")[-1]
                    # Detect video simplificat
                    is_video = False
                    if "video" in mime_type or ext in ["mp4", "mov", "avi", "3gp"]:
                         is_video = True
                         ext = "mp4" # Forțăm extensia
                    elif ext not in ["jpeg", "jpg", "png", "pdf"]:
                         ext = "jpg"

                    file_name = f"{self.case.id}_{os.path.basename(url)}.{ext}"

                    doc_type = CaseDocument.DocType.UNKNOWN
                    if is_video:
                         doc_type = CaseDocument.DocType.DAMAGE_PHOTO

                    doc = CaseDocument.objects.create(
                        case=self.case,
                        doc_type=doc_type,
                        ocr_data={},
                    )

                    # Stream download to temp file
                    with tempfile.NamedTemporaryFile() as tmp:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                tmp.write(chunk)
                        tmp.flush()
                        tmp.seek(0)

                        # Save to model
                        doc.file.save(file_name, File(tmp))

                    if is_video:
                        self.case.has_scene_video = True
                        self.case.save()
                        # Nu trimitem la AI video-ul
                    else:
                        # Trimitem la AI doar imaginile/pdf
                        analyze_document_task.delay(doc.id)
                        has_async_processing = True

                    saved_count += 1
            except Exception as e:
                print(f"Eroare download {url}: {e}")

        if saved_count > 0 and not silent:
            self.client.send_text(self.case, f"Am primit {saved_count} fișier(e). Analizez...")
            # Verificăm statusul imediat DOAR daca nu avem procesare asincrona (ex: doar video)
            if not has_async_processing:
                self._check_documents_status()

    def _try_handle_resolution_text(self, text):
        text = text.lower()
        choice_made = False

        if "service" in text or "rar" in text:
            self.case.resolution_choice = Case.Resolution.SERVICE_RAR
            self.case.is_human_managed = True
            self.case.save()
            self.client.send_text(
                self.case,
                "✅ Am notat opțiunea Service. Un coleg va prelua dosarul pentru a stabili programarea.",
            )
            return True # Stop processing

        elif "regie" in text:
            self.case.resolution_choice = Case.Resolution.OWN_REGIME
            choice_made = True
            self.client.send_text(self.case, "✅ Am notat: Regie Proprie.")

        elif "totala" in text:
            self.case.resolution_choice = Case.Resolution.TOTAL_LOSS
            choice_made = True
            self.client.send_text(self.case, "✅ Am notat: Daună Totală.")

        if choice_made:
            self.case.save()
            self._check_documents_status()
            return True

        return False

    def _check_documents_status(self):
        missing = []
        if not self.case.has_id_card:
            missing.append("Buletin (obligatoriu)")
        if not self.case.has_car_coupon:
            missing.append("Talon Auto (obligatoriu)")
        if not self.case.has_accident_report:
            missing.append("Amiabila / PV Politie (obligatoriu)")

        # Conditie: Video 360 SAU Minim 4 Poze
        damage_photos_count = CaseDocument.objects.filter(
            case=self.case,
            doc_type=CaseDocument.DocType.DAMAGE_PHOTO
        ).count()

        if not self.case.has_scene_video and damage_photos_count < 4:
            missing.append(f"Video 360 Grade SAU minim 4 Poze Auto (ai trimis {damage_photos_count})")

        # Condiție Extras Cont
        if self.case.resolution_choice == Case.Resolution.OWN_REGIME:
            if not self.case.has_bank_statement:
                 missing.append("Extras Cont Bancar (pt. Regie Proprie)")

        if not missing:
            # Avem actele. Avem rezoluția?
            if self.case.resolution_choice != Case.Resolution.UNDECIDED:
                # TOTUL GATA -> Mandat
                self.case.stage = Case.Stage.SIGNING_MANDATE
                self.case.save()
                self._send_signature_link()
            else:
                 self.client.send_buttons(
                    self.case,
                    "Ai încărcat toate documentele necesare. Cum dorești să soluționezi?",
                    ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"]
                )
        else:
             # Lipsesc acte. Informăm utilizatorul.
             msg = "👍 Am primit. Mai am nevoie de:\n- " + "\n- ".join(missing)
             self.client.send_text(self.case, msg)

    def _handle_offer_decision(self, text):
        text = text.lower()

        # 1. Accept
        if "accept" in text:
            self.case.stage = Case.Stage.PROCESSING_INSURER # Back to waiting
            self.case.save()

            from apps.claims.tasks import send_offer_acceptance_email_task
            send_offer_acceptance_email_task.delay(self.case.id)

            self.client.send_text(
                self.case,
                "✅ Am trimis acceptul către asigurator. Te anunțăm când se confirmă plata/închiderea."
            )
            return

        # 2. Change Option Request
        if "schimb" in text or "modific" in text:
            self.client.send_buttons(
                self.case,
                "Ce variantă preferi acum?",
                ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"]
            )
            return

        # 3. Handle New Option Selection
        from apps.claims.tasks import send_option_change_email_task

        if "service" in text or "rar" in text:
            self.case.resolution_choice = Case.Resolution.SERVICE_RAR
            self.case.is_human_managed = True
            self.case.save()

            send_option_change_email_task.delay(self.case.id, "Service Autorizat RAR")

            self.client.send_text(
                self.case,
                "✅ Am notat schimbarea pe Service RAR. Un coleg te va contacta."
            )
            return

        elif "regie" in text:
            self.case.resolution_choice = Case.Resolution.OWN_REGIME
            self.case.stage = Case.Stage.PROCESSING_INSURER # Back to waiting for new offer
            self.case.save()

            send_option_change_email_task.delay(self.case.id, "Regie Proprie")

            self.client.send_text(
                self.case,
                "✅ Am notificat asiguratorul că dorești Regie Proprie. Așteptăm recalcularea."
            )
            return

        elif "totala" in text:
            self.case.resolution_choice = Case.Resolution.TOTAL_LOSS
            self.case.stage = Case.Stage.PROCESSING_INSURER
            self.case.save()

            send_option_change_email_task.delay(self.case.id, "Dauna Totala")

            self.client.send_text(
                self.case,
                "✅ Am notificat asiguratorul că soliciți Daună Totală."
            )
            return

        else:
            self.client.send_buttons(
                self.case,
                "Te rog alege o opțiune validă:",
                ["Accept Oferta", "Schimb Optiunea"]
            )

    def _send_signature_link(self):
        domain = settings.APP_DOMAIN
        link = f"{domain}/mandat/semneaza/{self.case.id}/"
        msg = (
            "📝 Dosar complet! Mai avem un singur pas: Semnarea Mandatului.\n"
            f"Te rog intră aici și semnează:\n{link}"
        )
        self.client.send_text(self.case, msg)
