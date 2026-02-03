from twilio.rest import Client
from django.conf import settings
import logging

logger = logging.getLogger(__name__)


class WhatsAppClient:
    def __init__(self):
        self.client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        self.from_number = (
            settings.TWILIO_WHATSAPP_NUMBER
        )  # ex: 'whatsapp:+14155238886'

    def send_text(self, to_number, text):
        """Trimite un mesaj text simplu"""
        # Asigură prefixul whatsapp:
        if not to_number.startswith("whatsapp:"):
            to_number = f"whatsapp:{to_number}"

        try:
            message = self.client.messages.create(
                from_=self.from_number, to=to_number, body=text
            )
            return message.sid
        except Exception as e:
            logger.error(f"Error sending WhatsApp text: {e}")
            return None

    def send_buttons(self, to_number, body_text, buttons):
        """
        Trimite un mesaj cu butoane.
        Twilio necesită 'Content API' sau 'Templates' pentru butoane native.
        Pentru MVP, folosim formatul standard de liste dacă nu ai template-uri aprobate,
        dar aici simulam structura pentru viitor.
        """
        # NOTA: Dacă nu ai template-uri aprobate, Twilio nu permite butoane "libere"
        # în afara ferestrei de 24h. În sandbox funcționează.
        # Aici folosim un fallback elegant: Text cu opțiuni numerotate dacă butoanele eșuează.

        # Varianta simplă (Text) pentru siguranță maximă acum:
        options = "\n".join([f"🔹 {b}" for b in buttons])
        full_text = f"{body_text}\n\n{options}\n\n*(Răspunde cu textul exact)*"

        return self.send_text(to_number, full_text)

    # TODO: Pentru producție reală cu butoane UI (Clickable),
    # trebuie implementat Twilio Content SID (Templates).
