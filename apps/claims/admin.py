from django.contrib import admin
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin, TabularInline
from unfold.decorators import display

from .models import (
    Client,
    Case,
    InvolvedVehicle,
    CaseDocument,
    CommunicationLog,
    Insurer,
)


@admin.register(Client)
class ClientAdmin(ModelAdmin):
    list_display = ("phone_number", "full_name", "cnp", "created_at")
    search_fields = ("phone_number", "full_name", "cnp")


class InvolvedVehicleInline(TabularInline):
    model = InvolvedVehicle
    extra = 0
    verbose_name = "Vehicul Implicat"
    verbose_name_plural = "Vehicule Implicate"


class CaseDocumentInline(TabularInline):
    model = CaseDocument
    extra = 0
    readonly_fields = ("ocr_data", "uploaded_at")
    verbose_name = "Document"
    verbose_name_plural = "Documente la Dosar"


class CommunicationLogInline(TabularInline):
    model = CommunicationLog
    extra = 0
    readonly_fields = ("direction", "channel", "message_type", "content", "created_at")
    can_delete = False
    verbose_name = "Mesaj"
    verbose_name_plural = "Jurnal Conversație (WhatsApp)"

    def has_add_permission(self, request, obj=None):
        return False  # Nu adăugăm mesaje manual de aici, doar le vizualizăm


@admin.register(Case)
class CaseAdmin(ModelAdmin):
    list_display = (
        "id_short",
        "client_link",
        "get_stage_badge",  # Stadiul cu badge Unfold
        "get_human_status_badge",  # Iconiță Robot/Om cu badge Unfold
        "resolution_choice",
        "created_at",
    )

    list_filter = ("is_human_managed", "stage", "resolution_choice")
    list_filter_submit = True  # Buton de filtrare dedicat
    search_fields = ("client__phone_number", "client__full_name", "id")

    # Adăugăm Inline-ul de Loguri pentru a vedea chat-ul direct în dosar
    inlines = [InvolvedVehicleInline, CaseDocumentInline, CommunicationLogInline]

    fieldsets = (
        (
            "Status & Control",
            {"fields": ("stage", "is_human_managed", "resolution_choice")},
        ),
        ("Client", {"fields": ("client",)}),
        (
            "Checklist Documente (AI)",
            {
                "classes": ["collapse"],
                "fields": (
                    "has_id_card",
                    "has_car_coupon",
                    "has_accident_report",
                    "has_repair_auth",
                    "has_scene_video",
                    "has_mandate_signed",
                )
            },
        ),
        (
            "Date Asigurator & Ofertă",
            {
                "fields": (
                    "insurer_name",
                    "insurer_email",
                    "insurer_claim_number",
                    "settlement_offer_value",
                )
            },
        ),
        ("AI Summary", {"fields": ("ai_summary",)}),
    )

    # --- ACȚIUNI (Butoane Rapide) ---
    actions = ["switch_to_human_mode", "switch_to_bot_mode", "mark_as_closed"]

    @admin.action(description="🛑 STOP BOT (Comută pe Manual)")
    def switch_to_human_mode(self, request, queryset):
        queryset.update(is_human_managed=True)
        self.message_user(request, "Botul a fost oprit pentru dosarele selectate.")

    @admin.action(description="🤖 START BOT (Reactivare Automată)")
    def switch_to_bot_mode(self, request, queryset):
        queryset.update(is_human_managed=False)
        self.message_user(request, "Botul a preluat din nou controlul.")

    @admin.action(description="✅ CAZ SOLUȚIONAT (Închide Dosar)")
    def mark_as_closed(self, request, queryset):
        queryset.update(stage=Case.Stage.CLOSED)
        self.message_user(request, "Dosarele selectate au fost închise.")

    # --- Helper Methods pentru Afișare ---
    def id_short(self, obj):
        return str(obj.id)[:8]

    id_short.short_description = "ID Dosar"

    def client_link(self, obj):
        return obj.client.full_name or obj.client.phone_number

    client_link.short_description = "Client"

    @display(description="Stadiu", label=True)
    def get_stage_badge(self, obj):
        return obj.get_stage_display(), self._get_stage_color(obj.stage)

    def _get_stage_color(self, stage):
        if stage == Case.Stage.CLOSED:
            return "success"
        elif stage == Case.Stage.COLLECTING_DOCS:
            return "warning"
        elif stage == Case.Stage.PROCESSING_INSURER:
            return "info"
        return "default"

    @display(description="Mod Operare", label=True)
    def get_human_status_badge(self, obj):
        if obj.is_human_managed:
            return "UMAN", "danger"
        return "BOT", "success"


@admin.register(CommunicationLog)
class CommunicationLogAdmin(ModelAdmin):
    list_display = ("case", "direction", "channel", "created_at")
    list_filter = ("direction", "channel")


@admin.register(Insurer)
class InsurerAdmin(ModelAdmin):
    list_display = ("name", "email_claims", "identifiers")
    search_fields = ("name", "identifiers", "email_claims")
    fieldsets = (
        ("Date Generale", {"fields": ("name", "email_claims")}),
        ("Configurare AI Matching", {"fields": ("identifiers",)}),
    )
