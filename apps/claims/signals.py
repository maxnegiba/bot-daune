from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import CaseDocument, InvolvedVehicle


@receiver(post_save, sender=CaseDocument)
def process_ocr_data(sender, instance, created, **kwargs):
    """
    Acest signal ascultă când se salvează un CaseDocument.
    Dacă are date OCR, populează automat vehiculele în Dosar (Case).
    """
    if not instance.ocr_data:
        return

    data = instance.ocr_data
    case = instance.case
    doc_type = data.get("tip_document", "").upper()
    extracted = data.get("date_extrase", {})
    analiza = data.get("analiza_accident", {})

    # --- LOGICA PENTRU AMIABILĂ ---
    if "AMIABILA" in doc_type:
        # Procesăm Vehiculul A
        update_or_create_vehicle(
            case=case,
            role_identifier="A",
            license_plate=extracted.get("nr_auto_a"),
            vin=extracted.get("vin_a"),
            driver_name=extracted.get("nume_sofer_a"),
            is_guilty_verdict=analiza.get("vinovat_probabil"),
        )

        # Procesăm Vehiculul B
        update_or_create_vehicle(
            case=case,
            role_identifier="B",
            license_plate=extracted.get("nr_auto_b"),
            vin=extracted.get("vin_b"),
            driver_name=extracted.get("nume_sofer_b"),
            is_guilty_verdict=analiza.get("vinovat_probabil"),
        )

    # --- LOGICA PENTRU PROCURĂ / TALON ---
    elif "PROCURA" in doc_type or "TALON" in doc_type:
        # Aici presupunem că documentul aparține clientului (deci nu vinovatului, de obicei)
        # Dar salvăm datele găsite.
        update_or_create_vehicle(
            case=case,
            role_identifier="Client",  # Sau Generic
            license_plate=extracted.get("nr_auto"),
            vin=extracted.get("vin"),
            driver_name=extracted.get("nume"),
            is_guilty_verdict="Unknown",
        )


def update_or_create_vehicle(
    case, role_identifier, license_plate, vin, driver_name, is_guilty_verdict
):
    """
    Funcție ajutătoare care caută vehiculul și îl actualizează, sau îl creează.
    """
    # 1. Validare minimă: Nu creăm vehicule goale
    if not license_plate and not vin:
        return

    # Curățăm datele (să nu fie 'null' string sau spații)
    license_plate = (
        license_plate.strip().upper()
        if license_plate and license_plate != "null"
        else None
    )
    vin = vin.strip().upper() if vin and vin != "null" else None
    driver_name = driver_name.strip() if driver_name and driver_name != "null" else None

    if not license_plate:
        return

    # 2. Determinăm Vinovăția din verdictul AI-ului (dacă există)
    new_is_offender = None
    if is_guilty_verdict:
         if role_identifier in is_guilty_verdict:
             new_is_offender = True
         else:
             new_is_offender = False

    # 3. Căutăm manual pentru a nu suprascrie datele existente cu valori goale/false
    vehicle = InvolvedVehicle.objects.filter(case=case, license_plate=license_plate).first()
    created = False

    if vehicle:
        # Update parțial
        if vin:
            vehicle.vin_number = vin
        if driver_name:
            vehicle.driver_name = driver_name

        # Actualizăm vinovăția DOAR dacă avem un verdict nou clar
        if new_is_offender is not None:
            vehicle.is_offender = new_is_offender

        vehicle.save()
    else:
        # Create
        vehicle = InvolvedVehicle.objects.create(
            case=case,
            license_plate=license_plate,
            vin_number=vin or "",
            driver_name=driver_name or "",
            is_offender=new_is_offender if new_is_offender is not None else False
        )
        created = True

    action = "Creat" if created else "Actualizat"
    print(
        f"--- [SIGNAL] {action} Vehicul: {license_plate} (Vinovat: {vehicle.is_offender}) ---"
    )
