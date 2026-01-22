import uuid
from django.db import models
from django.utils.translation import gettext_lazy as _


# --- 1. Clientul ---
class Client(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(
        max_length=20, unique=True, help_text="Format: +407..."
    )
    full_name = models.CharField(max_length=150, blank=True, null=True)
    cnp = models.CharField(max_length=13, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.phone_number} - {self.full_name or 'Nume necunoscut'}"


# --- 2. Dosarul de Daună ---
class Case(models.Model):
    class Status(models.TextChoices):
        INITIATED = "INIT", _("Inițiat (Discuție Start)")
        UPLOADING = "UPLOAD", _("Așteptare Documente")
        PROCESSING_OCR = "OCR", _("Procesare AI & OCR")
        MISSING_INFO = "MISSING", _("Lipsesc Date (Manual)")
        WAITING_SIGNATURE = "SIGN", _("Așteptare Semnătură")
        READY_TO_SEND = "READY", _("Pregătit de trimitere")
        SENT_TO_INSURER = "SENT", _("Trimis la Asigurator")
        CLOSED = "CLOSED", _("Închis")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="cases")
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.INITIATED
    )

    # Rezumat generat de AI (ex: "Vinovatul este X, accident ușor")
    ai_summary = models.TextField(blank=True, null=True)

    is_signed_by_client = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Dosar {str(self.id)[:8]} ({self.status})"


# --- 3. Vehicule Implicate ---
class InvolvedVehicle(models.Model):
    class Role(models.TextChoices):
        VICTIM = "VICTIM", _("Păgubit (Client)")
        PERPETRATOR = "GUILTY", _("Vinovat")
        UNKNOWN = "UNKNOWN", _("Nedeterminat")

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="vehicles")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.UNKNOWN)

    license_plate = models.CharField(max_length=15, blank=True, null=True)
    vin_number = models.CharField(max_length=30, blank=True, null=True)  # Serie sasiu
    insurance_company_name = models.CharField(max_length=100, blank=True, null=True)
    policy_number = models.CharField(max_length=50, blank=True, null=True)

    # Date proprietar/șofer extrase din Constatare/Talon
    driver_name = models.CharField(max_length=100, blank=True, null=True)
    driver_cnp = models.CharField(max_length=13, blank=True, null=True)
    is_offender = models.BooleanField(default=False, verbose_name="Este Vinovat?")

    def __str__(self):
        return f"{self.license_plate} ({self.role})"


# --- 4. Documente (Poze/PDF) ---
class CaseDocument(models.Model):
    class DocType(models.TextChoices):
        ID_CARD = "CI", _("Buletin")
        DRIVERS_LICENSE = "PERMIS", _("Permis")
        CAR_REGISTRATION = "TALON", _("Talon")
        ACCIDENT_STATEMENT = "AMIABILA", _("Amiabilă")
        DAMAGE_PHOTO = "PHOTO", _("Poză Daună")
        POA_GENERATED = "POA_GEN", _("Procură (Nesemnată)")
        POA_SIGNED = "POA_SIGNED", _("Procură (Semnată)")
        UNKNOWN = "UNK", _("Necunoscut")
        POWER_OF_ATTORNEY = "PROCURA", "Procură"

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="documents")

    # Fișierele se vor salva în folderul media/uploads/an/luna/zi
    file = models.FileField(upload_to="uploads/%Y/%m/%d/")

    doc_type = models.CharField(
        max_length=15, choices=DocType.choices, default=DocType.UNKNOWN
    )

    # JSONField este super puternic în Postgres. Aici salvăm TOT ce zice Google Vision/AI
    ocr_data = models.JSONField(blank=True, null=True)

    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.doc_type} - {self.file.name}"


# --- 5. Jurnal Conversație (Log) ---
class CommunicationLog(models.Model):
    case = models.ForeignKey(
        Case, on_delete=models.SET_NULL, null=True, blank=True, related_name="logs"
    )
    direction = models.CharField(
        max_length=10, choices=[("IN", "Primit"), ("OUT", "Trimis")]
    )
    channel = models.CharField(max_length=10, default="WHATSAPP")
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.direction} - {self.created_at}"
