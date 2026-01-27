import os
import requests
from django.core.files.base import ContentFile
from django.conf import settings
from apps.claims.models import Case, CaseDocument
from apps.claims.tasks import analyze_document_task
from .utils import WhatsAppClient

wa = WhatsAppClient()


class FlowManager:
    def __init__(self, case, sender_phone):
        self.case = case
        self.phone = sender_phone

    def process_message(self, message_type, content, media_urls=None):
        """
        Router principal în funcție de etapa dosarului.
        Acceptă și media_urls pentru imaginile venite de la Twilio.
        """
        # 0. Verificăm intervenția umană
        if self.case.is_human_managed:
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
            wa.send_text(
                self.phone,
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
                "- Video 360° cu mașina avariată (sau poze din toate unghiurile)\n\n"
                "📌 **OPȚIONAL (Dacă ai):**\n"
                "- Autorizație Reparație (de la Poliție)\n"
                "- Documente șofer vinovat (RCA, Talon, CI)\n"
                "- Alte documente relevante\n\n"
                "Extras Cont Bancar (dacă dorești Regie Proprie)\n\n"
                "👇 Te rog răspunde ACUM la întrebarea de mai jos, apoi poți începe încărcarea pozelor:"
            )
            wa.send_text(self.phone, msg_full)

            # 2. Butoane Rezoluție (Imediat după)
            msg_res = "Cum dorești să soluționezi acest dosar?"
            wa.send_buttons(
                self.phone,
                msg_res,
                ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"]
            )

        elif "alta" in text or "nu" in text:
            self.case.is_human_managed = True
            self.case.save()
            wa.send_text(
                self.phone,
                "Am înțeles. Un operator uman a fost notificat și te va contacta în curând.",
            )
        else:
            # Mesaj Greeting Inițial
            wa.send_buttons(
                self.phone,
                "Salut! Dorești să deschidem un dosar de daună?",
                ["DA, Deschide Dosar", "NU, Am altă problemă"],
            )

    def _handle_image_upload(self, media_urls):
        saved_count = 0
        for url, mime_type in media_urls:
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                r = requests.get(url, headers=headers, timeout=15)
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
                    doc.file.save(file_name, ContentFile(r.content))

                    if is_video:
                        self.case.has_scene_video = True
                        self.case.save()
                        # Nu trimitem la AI video-ul
                    else:
                        # Trimitem la AI doar imaginile/pdf
                        analyze_document_task.delay(doc.id)

                    saved_count += 1
            except Exception as e:
                print(f"Eroare download {url}: {e}")

        if saved_count > 0:
            wa.send_text(self.phone, f"Am primit {saved_count} fișier(e). Analizez...")
            # Verificăm statusul imediat (pt Video)
            self._check_documents_status()

    def _try_handle_resolution_text(self, text):
        text = text.lower()
        choice_made = False

        if "service" in text or "rar" in text:
            self.case.resolution_choice = Case.Resolution.SERVICE_RAR
            self.case.is_human_managed = True
            self.case.save()
            wa.send_text(
                self.phone,
                "✅ Am notat opțiunea Service. Un coleg va prelua dosarul pentru a stabili programarea.",
            )
            return True # Stop processing

        elif "regie" in text:
            self.case.resolution_choice = Case.Resolution.OWN_REGIME
            choice_made = True
            wa.send_text(self.phone, "✅ Am notat: Regie Proprie.")

        elif "totala" in text:
            self.case.resolution_choice = Case.Resolution.TOTAL_LOSS
            choice_made = True
            wa.send_text(self.phone, "✅ Am notat: Daună Totală.")

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
        if not self.case.has_scene_video:
            missing.append("Video 360 Grade (obligatoriu)")

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
                 wa.send_buttons(
                    self.phone,
                    "Ai încărcat toate documentele necesare. Cum dorești să soluționezi?",
                    ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"]
                )
        else:
             # Lipsesc acte. Informăm utilizatorul.
             msg = "👍 Am primit. Mai am nevoie de:\n- " + "\n- ".join(missing)
             wa.send_text(self.phone, msg)

    def _handle_offer_decision(self, text):
        text = text.lower()

        # 1. Accept
        if "accept" in text:
            self.case.stage = Case.Stage.PROCESSING_INSURER # Back to waiting
            self.case.save()

            from apps.claims.tasks import send_offer_acceptance_email_task
            send_offer_acceptance_email_task.delay(self.case.id)

            wa.send_text(
                self.phone,
                "✅ Am trimis acceptul către asigurator. Te anunțăm când se confirmă plata/închiderea."
            )
            return

        # 2. Change Option Request
        if "schimb" in text or "modific" in text:
            wa.send_buttons(
                self.phone,
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

            wa.send_text(
                self.phone,
                "✅ Am notat schimbarea pe Service RAR. Un coleg te va contacta."
            )
            return

        elif "regie" in text:
            self.case.resolution_choice = Case.Resolution.OWN_REGIME
            self.case.stage = Case.Stage.PROCESSING_INSURER # Back to waiting for new offer
            self.case.save()

            send_option_change_email_task.delay(self.case.id, "Regie Proprie")

            wa.send_text(
                self.phone,
                "✅ Am notificat asiguratorul că dorești Regie Proprie. Așteptăm recalcularea."
            )
            return

        elif "totala" in text:
            self.case.resolution_choice = Case.Resolution.TOTAL_LOSS
            self.case.stage = Case.Stage.PROCESSING_INSURER
            self.case.save()

            send_option_change_email_task.delay(self.case.id, "Dauna Totala")

            wa.send_text(
                self.phone,
                "✅ Am notificat asiguratorul că soliciți Daună Totală."
            )
            return

        else:
            wa.send_buttons(
                self.phone,
                "Te rog alege o opțiune validă:",
                ["Accept Oferta", "Schimb Optiunea"]
            )

    def _send_signature_link(self):
        domain = "http://127.0.0.1:8000"
        link = f"{domain}/mandat/semneaza/{self.case.id}/"
        msg = (
            "📝 Dosar complet! Mai avem un singur pas: Semnarea Mandatului.\n"
            f"Te rog intră aici și semnează:\n{link}"
        )
        wa.send_text(self.phone, msg)
