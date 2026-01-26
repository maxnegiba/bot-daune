import os
from django.conf import settings
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from apps.claims.models import Case


def generate_proxy_pdf(case_id):
    """
    Generează un PDF (Procura) precompletat cu datele clientului.
    Salvează fișierul în media/documents/ și returnează calea.
    """
    try:
        case = Case.objects.get(id=case_id)
        client = case.client

        # Definim calea unde salvăm
        filename = f"procura_draft_{case.id}.pdf"
        save_dir = os.path.join(settings.MEDIA_ROOT, "documents")
        os.makedirs(save_dir, exist_ok=True)  # Creăm folderul dacă nu există
        file_path = os.path.join(save_dir, filename)

        # Inițializăm Canvas-ul (foaia albă PDF)
        c = canvas.Canvas(file_path, pagesize=A4)
        width, height = A4

        # --- TITLU ---
        c.setFont("Helvetica-Bold", 16)
        c.drawCentredString(width / 2, height - 50, "MANDAT / PROCURĂ")

        # --- TEXT CORP ---
        c.setFont("Helvetica", 12)
        y_position = height - 100
        line_height = 20

        # Text static combinat cu date dinamice
        text_lines = [
            f"Subsemnatul/a {client.last_name} {client.first_name},",
            f"Identificat cu CNP: {client.cnp if client.cnp else '_________________'}",
            f"Telefon: {client.phone_number}",
            " ",
            "Prin prezenta împuternicesc societatea SERVICE AUTO SRL",
            "să mă reprezinte în fața asiguratorului pentru deschiderea",
            "și instrumentarea dosarului de daună.",
            " ",
            f"Pentru autovehiculul cu Nr. Înmatriculare: {case.vehicle.license_plate if hasattr(case, 'vehicle') and case.vehicle else '___________'}",
            " ",
            "Data: __________________",
        ]

        # Scriem liniile pe PDF
        for line in text_lines:
            c.drawString(50, y_position, line)
            y_position -= line_height

        # --- ZONA DE SEMNĂTURĂ (Chenar) ---
        y_signature = y_position - 50
        c.rect(50, y_signature - 60, 200, 80, stroke=1, fill=0)  # Un chenar gol
        c.drawString(60, y_signature, "Semnătura Clientului:")

        # Salvăm pagina
        c.showPage()
        c.save()

        # Returnăm calea relativă pentru a o salva în DB sau a o trimite
        return os.path.join("documents", filename)

    except Case.DoesNotExist:
        print(f"Eroare: Dosarul {case_id} nu există.")
        return None
    except Exception as e:
        print(f"Eroare generare PDF: {e}")
        return None
