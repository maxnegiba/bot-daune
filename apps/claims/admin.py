from django.contrib import admin
from .models import Client, Case, InvolvedVehicle, CaseDocument, CommunicationLog

# --- INLINES (Tabelele din interiorul Dosarului) ---


class DocumentInline(admin.TabularInline):
    model = CaseDocument
    extra = 0
    # Adăugăm 'ocr_data' la readonly, ca să vezi ce a scos AI-ul (JSON)
    # dar să nu te lase să-l strici manual.
    readonly_fields = ("ocr_data",)


class VehicleInline(admin.TabularInline):
    model = InvolvedVehicle
    extra = 0
    # Aici afișăm datele extrase și bifa de vinovăție
    fields = ("license_plate", "vin_number", "driver_name", "is_offender")


# --- ADMIN PRINCIPAL ---


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("phone_number", "full_name", "created_at")
    search_fields = ("phone_number", "full_name")


@admin.register(Case)
class CaseAdmin(admin.ModelAdmin):
    list_display = ("id", "client", "status", "created_at")
    list_filter = ("status",)
    inlines = [
        VehicleInline,  # Aici vor apărea mașinile (Vinovat / Nevinov)
        DocumentInline,  # Aici vor apărea documentele și JSON-ul OCR
    ]


@admin.register(CommunicationLog)
class LogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "direction", "content")


# Opțional: Înregistrăm și Vehiculele separat, în caz că vrei să cauți o mașină anume
@admin.register(InvolvedVehicle)
class InvolvedVehicleAdmin(admin.ModelAdmin):
    list_display = ("license_plate", "case", "driver_name", "is_offender")
    search_fields = ("license_plate", "driver_name")
    list_filter = ("is_offender",)
