from twilio.rest import Client
from django.conf import settings
import logging
from apps.claims.models import Case, CommunicationLog, Client as ClientModel

logger = logging.getLogger(__name__)


class BaseChatClient:
    def send_text(self, recipient, text):
        raise NotImplementedError

    def send_buttons(self, recipient, body_text, buttons):
        raise NotImplementedError


class WhatsAppClient(BaseChatClient):
    def __init__(self):
        self.client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        self.from_number = (
            settings.TWILIO_WHATSAPP_NUMBER
        )  # ex: 'whatsapp:+14155238886'

    def _get_phone(self, recipient):
        if isinstance(recipient, Case):
            return recipient.client.phone_number
        return recipient

    def send_text(self, recipient, text):
        """Trimite un mesaj text simplu"""
        to_number = self._get_phone(recipient)

        # Asigură prefixul whatsapp:
        if not to_number.startswith("whatsapp:"):
            to_number = f"whatsapp:{to_number}"

        try:
            # Logăm mesajul OUT (către WhatsApp) pentru consistență
            if isinstance(recipient, Case):
                CommunicationLog.objects.create(
                    case=recipient,
                    direction="OUT",
                    channel="WHATSAPP",
                    content=text
                )

            message = self.client.messages.create(
                from_=self.from_number, to=to_number, body=text
            )
            return message.sid
        except Exception as e:
            logger.error(f"Error sending WhatsApp text: {e}")
            return None

    def send_buttons(self, recipient, body_text, buttons):
        """
        Trimite un mesaj cu butoane.
        Twilio necesită 'Content API' sau 'Templates' pentru butoane native.
        Pentru MVP, folosim formatul standard de liste dacă nu ai template-uri aprobate.
        """
        # Varianta simplă (Text) pentru siguranță maximă acum:
        options = "\n".join([f"🔹 {b}" for b in buttons])
        full_text = f"{body_text}\n\n{options}\n\n*(Răspunde cu textul exact)*"

        return self.send_text(recipient, full_text)


class WebChatClient(BaseChatClient):
    def send_text(self, recipient, text):
        """
        Simulează trimiterea prin salvarea în baza de date.
        Frontend-ul va face polling pentru a prelua aceste mesaje.
        """
        case = None
        if isinstance(recipient, Case):
            case = recipient
        elif isinstance(recipient, str):
            # Încercăm să găsim clientul după telefon
            # Notă: Asta e o ghicire, ideal ar fi să primim obiectul Case.
            phone = recipient.replace("whatsapp:", "")
            try:
                client = ClientModel.objects.get(phone_number=phone)
                case = Case.objects.filter(client=client).exclude(stage=Case.Stage.CLOSED).last()
            except ClientModel.DoesNotExist:
                logger.warning(f"WebChatClient: Could not find client for {phone}")
                return None

        if case:
            CommunicationLog.objects.create(
                case=case,
                direction="OUT",
                channel="WEB",
                content=text
            )
            return "web-msg-saved"
        else:
            logger.error("WebChatClient: No case provided for logging.")
            return None

    def send_buttons(self, recipient, body_text, buttons):
        """
        Pentru Web Chat, putem salva metadate pentru butoane ca să le randăm frumos în UI.
        """
        case = recipient if isinstance(recipient, Case) else None
        # Fallback lookup logic duplicated or refactored?
        # Let's rely on send_text logic or copy it.
        # But wait, send_text saves "content". We want "metadata" for buttons.

        if not case and isinstance(recipient, str):
             phone = recipient.replace("whatsapp:", "")
             try:
                client = ClientModel.objects.get(phone_number=phone)
                case = Case.objects.filter(client=client).exclude(stage=Case.Stage.CLOSED).last()
             except:
                 pass

        if case:
            # Salvăm textul întrebării
            # Și butoanele în metadata
            CommunicationLog.objects.create(
                case=case,
                direction="OUT",
                channel="WEB",
                content=body_text,
                metadata={"buttons": buttons, "type": "interactive"}
            )
            return "web-btn-saved"
        return None
