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
    iban = models.CharField(
        max_length=34, blank=True, null=True, help_text="Pentru Regie Proprie"
    )

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.phone_number} - {self.full_name or 'Client Nou'}"


# --- 2. Dosarul de Daună (Refactorizat) ---
class Case(models.Model):
    # Etapele fluxului (State Machine)
    class Stage(models.TextChoices):
        GREETING = "GREETING", _("1. Greeting / Alegere Flux")
        COLLECTING_DOCS = "DOCS", _("2. Colectare Documente")
        SELECTING_RESOLUTION = "RES_SEL", _("3. Alegere Tip Despăgubire")
        SIGNING_MANDATE = "SIGN", _("4. Semnare Mandat")
        PROCESSING_INSURER = "INSURER", _("5. Discuție Asigurator (Email)")
        OFFER_DECISION = "OFFER", _("6. Decizie Ofertă Client")
        CLOSED = "CLOSED", _("7. Dosar Închis")

    # Opțiunile utilizatorului pentru despăgubire
    class Resolution(models.TextChoices):
        OWN_REGIME = "REGIE", _("Regie Proprie")
        SERVICE_RAR = "SERVICE", _("Service Autorizat RAR")
        TOTAL_LOSS = "DAUNA_TOTALA", _("Daună Totală")
        UNDECIDED = "N/A", _("Nedecis")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="cases")

    # Stadiul curent al dosarului
    stage = models.CharField(
        max_length=20, choices=Stage.choices, default=Stage.GREETING
    )

    # --- Checklist Documente (Validare) ---
    # Acestea vor fi bifate automat de AI sau manual de Bot
    has_id_card = models.BooleanField(default=False, verbose_name="Are Buletin?")
    has_car_coupon = models.BooleanField(default=False, verbose_name="Are Talon?")
    has_accident_report = models.BooleanField(
        default=False, verbose_name="Are Amiabilă/PV?"
    )
    has_repair_auth = models.BooleanField(
        default=False, verbose_name="Are Aut. Reparație?"
    )  # Optional
    has_scene_video = models.BooleanField(default=False, verbose_name="Are Video 360?")
    has_mandate_signed = models.BooleanField(
        default=False, verbose_name="Mandat Semnat?"
    )

    # --- Decizii Client ---
    resolution_choice = models.CharField(
        max_length=20, choices=Resolution.choices, default=Resolution.UNDECIDED
    )

    # --- Control Uman ---
    is_human_managed = models.BooleanField(
        default=False,
        help_text="Dacă este True, botul NU mai răspunde automat. Se activează la Service RAR sau manual.",
    )

    # --- Date Asigurator & Ofertă ---
    insurer_email = models.EmailField(
        blank=True, null=True, help_text="Email identificat al asiguratorului"
    )
    insurer_name = models.CharField(max_length=100, blank=True, null=True)
    insurer_claim_number = models.CharField(
        max_length=50, blank=True, null=True, help_text="Nr dosar la asigurator"
    )

    settlement_offer_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name="Ofertă (RON)",
    )

    # Rezumat AI (păstrat din vechiul cod)
    ai_summary = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        hum = " [UMAN]" if self.is_human_managed else ""
        return f"Dosar {str(self.id)[:8]} - {self.stage}{hum}"


# --- 3. Vehicule Implicate ---
class InvolvedVehicle(models.Model):
    class Role(models.TextChoices):
        VICTIM = "VICTIM", _("Păgubit (Client)")
        PERPETRATOR = "GUILTY", _("Vinovat")
        UNKNOWN = "UNKNOWN", _("Nedeterminat")

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="vehicles")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.UNKNOWN)

    license_plate = models.CharField(max_length=15, blank=True, null=True)
    vin_number = models.CharField(max_length=30, blank=True, null=True)
    insurance_company_name = models.CharField(max_length=100, blank=True, null=True)
    policy_number = models.CharField(max_length=50, blank=True, null=True)

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
        ACCIDENT_REPORT = "AMIABILA", _("Amiabilă / PV Poliție")
        REPAIR_AUTH = "AUT_REP", _("Autorizație Reparație")
        DAMAGE_PHOTO = "PHOTO", _("Poză Daună / Video")
        BANK_STATEMENT = "EXTRAS", _("Extras Cont")
        OTHER_DOCS = "ALTELE", _("Alte Documente")

        # Documente generate/semnate
        MANDATE_UNSIGNED = "MANDAT_RAW", _("Mandat (Generat)")
        MANDATE_SIGNED = "MANDAT_SIGN", _("Mandat (Semnat)")
        COMPENSATION_CLAIM = "CERERE", _("Cerere Despăgubire")

        UNKNOWN = "UNK", _("Necunoscut")

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="documents")
    file = models.FileField(upload_to="uploads/%Y/%m/%d/")
    doc_type = models.CharField(
        max_length=20, choices=DocType.choices, default=DocType.UNKNOWN
    )
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
    message_type = models.CharField(
        max_length=20, default="text"
    )  # text, image, interactive
    content = models.TextField()
    metadata = models.JSONField(
        blank=True, null=True
    )  # Pentru a salva ID-ul butonului apasat etc.
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.direction} - {self.created_at}"


# --- 6. Baza de Date Asiguratori (Pentru Email) ---
class Insurer(models.Model):
    name = models.CharField(max_length=100, verbose_name="Nume Asigurator")
    email_claims = models.EmailField(
        verbose_name="Email Avizări Daune",
        help_text="Adresa unde se trimit documentele",
    )

    identifiers = models.CharField(
        max_length=255,
        verbose_name="Cuvinte Cheie (Matching)",
        help_text="Cuvinte separate prin virgulă pentru identificare. Ex: 'groupama, grupama, group'",
    )

    class Meta:
        verbose_name = "Asigurator"
        verbose_name_plural = "Listă Asiguratori"

    def __str__(self):
        return self.name
