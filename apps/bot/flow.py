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
            # Botul tace, răspunde omul.
            # (Opțional: poți notifica adminul aici că a scris clientul)
            return

        stage = self.case.stage

        # --- ETAPA 1: GREETING (Salut / Alegere Flux) ---
        if stage == Case.Stage.GREETING:
            self._handle_greeting(content)

        # --- ETAPA 2: COLLECTING DOCS (Documente) ---
        elif stage == Case.Stage.COLLECTING_DOCS:
            if message_type == "image" and media_urls:
                self._handle_image_upload(media_urls)
            else:
                # Dacă scrie text în loc să trimită poză, verificăm ce îi lipsește
                self._check_documents_status()

        # --- ETAPA 3: SELECTING RESOLUTION (Regie/Service) ---
        elif stage == Case.Stage.SELECTING_RESOLUTION:
            self._handle_resolution(content)

        # --- ETAPA 4: SEMNATURA (Mandat) ---
        elif stage == Case.Stage.SIGNING_MANDATE:
            # Dacă utilizatorul scrie ceva în timp ce așteptăm semnătura, îi reamintim linkul
            self._send_signature_link()

        # --- ALTE ETAPE (Procesare, Închis etc.) ---
        elif stage == Case.Stage.PROCESSING_INSURER:
            wa.send_text(
                self.phone,
                "Dosarul este în analiză la asigurator. Te vom anunța când primim o ofertă.",
            )

    # =========================================================================
    # LOGICA DETALIATĂ PE ETAPE
    # =========================================================================

    def _handle_greeting(self, text):
        text = text.lower()
        if "dauna" in text or "deschide" in text or "da" in text:
            # Trecem la pasul următor
            self.case.stage = Case.Stage.COLLECTING_DOCS
            self.case.save()
            wa.send_text(
                self.phone,
                "✅ Am deschis dosarul. Te rog trimite o poză clară cu **Buletinul**.",
            )
        elif "alta" in text or "nu" in text:
            self.case.is_human_managed = True
            self.case.save()
            wa.send_text(
                self.phone,
                "Am înțeles. Un operator uman a fost notificat și te va contacta în curând.",
            )
        else:
            # Dacă scrie altceva, reafișăm meniul
            wa.send_buttons(
                self.phone,
                "Nu am înțeles. Dorești să deschidem un dosar de daună?",
                ["DA, Deschide Dosar", "NU, Am altă problemă"],
            )

    def _handle_image_upload(self, media_urls):
        """
        Descarcă imaginile, le salvează și declanșează AI-ul.
        """
        saved_count = 0
        for url, mime_type in media_urls:
            try:
                # 1. Descărcăm poza de la Twilio
                # Folosim un User-Agent fake pentru a evita blocarea
                headers = {"User-Agent": "Mozilla/5.0"}
                r = requests.get(url, headers=headers, timeout=15)

                if r.status_code == 200:
                    # Determinăm extensia
                    ext = mime_type.split("/")[-1]
                    if ext not in ["jpeg", "jpg", "png", "pdf"]:
                        ext = "jpg"

                    file_name = f"{self.case.id}_{os.path.basename(url)}.{ext}"

                    # 2. Creăm obiectul Document
                    doc = CaseDocument.objects.create(
                        case=self.case,
                        doc_type=CaseDocument.DocType.UNKNOWN,  # AI-ul va decide ce e
                        ocr_data={},
                    )
                    doc.file.save(file_name, ContentFile(r.content))

                    # 3. Trimitem la AI (Worker-ul din tasks.py)
                    analyze_document_task.delay(doc.id)
                    saved_count += 1
            except Exception as e:
                print(f"Eroare download {url}: {e}")

        if saved_count > 0:
            # Confirmăm primirea, dar NU schimbăm stadiul încă.
            # Task-ul AI va apela check_status_and_notify() când termină.
            wa.send_text(
                self.phone, f"Am primit {saved_count} document(e). Le analizez acum..."
            )

    def _check_documents_status(self):
        """
        Verifică manual ce lipsește și informează userul.
        """
        missing = []
        if not self.case.has_id_card:
            missing.append("Buletin")
        if not self.case.has_car_coupon:
            missing.append("Talon Auto")
        if not self.case.has_accident_report:
            missing.append("Amiabila / PV Politie")

        if not missing:
            # Avem tot -> Trecem la etapa 3
            self.case.stage = Case.Stage.SELECTING_RESOLUTION
            self.case.save()
            wa.send_buttons(
                self.phone,
                "Dosar complet! Cum dorești să soluționezi?",
                ["Regie Proprie", "Service Autorizat RAR", "Dauna Totala"],
            )
        else:
            # Cerem ce lipsește
            msg = "Pentru a continua, mai am nevoie de:\n- " + "\n- ".join(missing)
            wa.send_text(self.phone, msg)

    def _handle_resolution(self, text):
        text = text.lower()

        # --- OPȚIUNEA 1: SERVICE ---
        if "service" in text or "rar" in text:
            self.case.resolution_choice = Case.Resolution.SERVICE_RAR
            self.case.is_human_managed = True  # STOP BOT, intră omul pentru programare
            self.case.save()
            wa.send_text(
                self.phone,
                "✅ Am notat opțiunea Service. Un coleg va prelua dosarul pentru a stabili programarea în service.",
            )

        # --- OPȚIUNEA 2: REGIE PROPRIE (Fluxul Complet) ---
        elif "regie" in text:
            self.case.resolution_choice = Case.Resolution.OWN_REGIME
            self.case.stage = Case.Stage.SIGNING_MANDATE
            self.case.save()
            self._send_signature_link()

        # --- OPȚIUNEA 3: DAUNĂ TOTALĂ ---
        elif "totala" in text:
            self.case.resolution_choice = Case.Resolution.TOTAL_LOSS
            self.case.stage = Case.Stage.PROCESSING_INSURER
            self.case.save()
            wa.send_text(
                self.phone,
                "Am înregistrat solicitarea de Daună Totală. Vom notifica asiguratorul și revenim cu oferta.",
            )

        else:
            # Userul a scris altceva
            wa.send_buttons(
                self.phone,
                "Te rog alege o opțiune validă:",
                ["Regie Proprie", "Service RAR", "Dauna Totala"],
            )

    def _send_signature_link(self):
        """
        Generează și trimite link-ul de semnare.
        """
        # ÎN PRODUCȚIE: Schimbă domain cu site-ul tău real (ex: https://autodaune.ro)
        # Pentru test local cu ngrok, pune url-ul de ngrok
        domain = "http://127.0.0.1:8000"

        link = f"{domain}/mandat/semneaza/{self.case.id}/"

        msg = (
            "📝 Pentru a putea trimite dosarul la asigurator, avem nevoie de mandatul tău de reprezentare.\n\n"
            f"Te rog intră aici și semnează direct pe ecran:\n{link}"
        )
        wa.send_text(self.phone, msg)
