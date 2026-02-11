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
        # 1. Extragem Data Accidentului (dacă există)
        accident_date_str = extracted.get("data_accident")
        if accident_date_str:
            try:
                from datetime import datetime

                # Curățăm eventualele spații
                accident_date_str = accident_date_str.strip()
                # Încercăm câteva formate comune
                for fmt in ["%d.%m.%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"]:
                    try:
                        dt = datetime.strptime(accident_date_str, fmt).date()
                        case.accident_date = dt
                        case.save(update_fields=["accident_date"])
                        print(f"--- [SIGNAL] Data accident salvată: {dt} ---")
                        break
                    except ValueError:
                        continue
            except Exception as e:
                print(f"Eroare parsing data accident: {e}")

        # Procesăm Vehiculul A
        update_or_create_vehicle(
            case=case,
            role_identifier="A",
            license_plate=extracted.get("nr_auto_a"),
            vin=extracted.get("vin_a"),
            driver_name=extracted.get("nume_sofer_a"),
            is_guilty_verdict=analiza.get("vinovat_probabil"),
            insurance_company=extracted.get("asigurator_a"),
        )

        # Procesăm Vehiculul B
        update_or_create_vehicle(
            case=case,
            role_identifier="B",
            license_plate=extracted.get("nr_auto_b"),
            vin=extracted.get("vin_b"),
            driver_name=extracted.get("nume_sofer_b"),
            is_guilty_verdict=analiza.get("vinovat_probabil"),
            insurance_company=extracted.get("asigurator_b"),
        )

    # --- LOGICA PENTRU CI (BULETIN) ---
    elif "CI" in doc_type or "BULETIN" in doc_type:
        client = case.client
        updated_fields = []

        if extracted.get("adresa_domiciliu"):
            client.address = extracted.get("adresa_domiciliu")
            updated_fields.append("address")

        if extracted.get("seria_ci"):
            client.id_series = extracted.get("seria_ci")
            updated_fields.append("id_series")

        if extracted.get("numar_ci"):
            client.id_number = extracted.get("numar_ci")
            updated_fields.append("id_number")

        if extracted.get("cnp"):
            client.cnp = extracted.get("cnp")
            updated_fields.append("cnp")

        if updated_fields:
            client.save(update_fields=updated_fields)
            print(f"--- [SIGNAL] Client actualizat (CI): {updated_fields} ---")

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
            make=extracted.get("marca"),
            model=extracted.get("model"),
        )


def update_or_create_vehicle(
    case,
    role_identifier,
    license_plate,
    vin,
    driver_name,
    is_guilty_verdict,
    insurance_company=None,
    make=None,
    model=None,
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
    insurance_company = (
        insurance_company.strip()
        if insurance_company and insurance_company != "null"
        else None
    )
    make = make.strip() if make and make != "null" else None
    model = model.strip() if model and model != "null" else None

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
    vehicle = InvolvedVehicle.objects.filter(
        case=case, license_plate=license_plate
    ).first()
    created = False

    if vehicle:
        # Update parțial
        if vin:
            vehicle.vin_number = vin
        if driver_name:
            vehicle.driver_name = driver_name
        if insurance_company:
            vehicle.insurance_company_name = insurance_company
        if make:
            vehicle.make = make
        if model:
            vehicle.model = model

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
            insurance_company_name=insurance_company or "",
            is_offender=new_is_offender if new_is_offender is not None else False,
            make=make or "",
            model=model or "",
        )
        created = True

    action = "Creat" if created else "Actualizat"
    print(
        f"--- [SIGNAL] {action} Vehicul: {license_plate} (Vinovat: {vehicle.is_offender}) ---"
    )
