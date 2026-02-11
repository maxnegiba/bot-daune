import os
from django.conf import settings
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER
from apps.claims.models import Case, InvolvedVehicle

def generate_proxy_pdf(case_id):
    """
    Generează un PDF (Procura) precompletat cu datele clientului.
    Salvează fișierul în media/documents/ și returnează calea.
    """
    try:
        case = Case.objects.get(id=case_id)
        client = case.client
        vehicle = case.vehicles.filter(role=InvolvedVehicle.Role.VICTIM).first()

        # Definim calea unde salvăm
        filename = f"procura_draft_{case.id}.pdf"
        save_dir = os.path.join(settings.MEDIA_ROOT, "documents")
        os.makedirs(save_dir, exist_ok=True)  # Creăm folderul dacă nu există
        file_path = os.path.join(save_dir, filename)

        # Configurare ReportLab (DocTemplate pentru text flow automat)
        doc = SimpleDocTemplate(
            file_path,
            pagesize=A4,
            rightMargin=2*cm, leftMargin=2*cm,
            topMargin=2*cm, bottomMargin=2*cm
        )

        styles = getSampleStyleSheet()
        style_normal = styles["Normal"]
        style_normal.alignment = TA_JUSTIFY
        style_normal.fontSize = 10
        style_normal.leading = 14

        style_title = ParagraphStyle(
            'TitleStyle',
            parent=styles['Heading1'],
            alignment=TA_CENTER,
            fontSize=14,
            spaceAfter=10
        )

        story = []

        # --- TITLURI ---
        story.append(Paragraph("- IMPUTERNICIRE -", style_title))
        story.append(Paragraph("- MANDAT DE REPREZENTARE -", style_title))
        story.append(Spacer(1, 12))

        # --- VARIABILE ---
        full_name = client.full_name if client.full_name else "_______________________________________"
        address = client.address if client.address else "Loc. ________________ str. ____________________________ nr. ____, bl. ____, sc. ____, et. ____, ap. ____, jud. ________________________"
        id_series = client.id_series if client.id_series else "____"
        id_number = client.id_number if client.id_number else "_____________"
        cnp = client.cnp if client.cnp else "_________________________________________"
        phone = client.phone_number if client.phone_number else "________________________________"
        email = client.email if client.email else "___________________________________________________________"

        make = vehicle.make if vehicle and vehicle.make else "_______________________"
        model = vehicle.model if vehicle and vehicle.model else "_______________________"
        license_plate = vehicle.license_plate if vehicle and vehicle.license_plate else "_______________________"
        vin = vehicle.vin_number if vehicle and vehicle.vin_number else "_________________________________________"
        acc_date = case.accident_date.strftime("%d.%m.%Y") if case.accident_date else "______________________"

        # --- CORP TEXT ---
        # Paragraful 1: Clientul
        p1 = f"""
        Subsemnatul/Subscrisa <b>{full_name}</b> cu domiciliul / sediul în <b>{address}</b>,
        identificat/ă cu C.I. seria <b>{id_series}</b> nr. <b>{id_number}</b>,
        avand CNP/CUI <b>{cnp}</b>, Tel: <b>{phone}</b> e-mail <b>{email}</b>
        în calitate de membru al Asociației Păgubiților RCA, imputernicesc cu puteri depline pe:
        """
        story.append(Paragraph(p1, style_normal))
        story.append(Spacer(1, 12))

        # Paragraful 2: Mandatarul
        p2 = """
        <b>Asociația Păgubiților RCA</b>, avand sediul procesual ales pentru corespondenta in str. Poet Grigore Alexandrescu Bl. E3 Parter, Loc. Targoviste , Jud. Dambovita, CIF: 41401972 , Nr. Reg. Asociatii si Fundatii la poziția 13/I/A/10.06.2019, Call center 021.9906 , Direct Line 0731.007.658 , Tel Fix: 0245.651.000 , E-mail: office@aprca.ro sa imi reprezinte interesele in fata tuturor institutiilor publice competente, persoane fizice si juridice, I.P.J, IGPR, R.A.R, I.S.U, UPU, Unitati spitalicesti, ASF, SAL-Fin, FGA, ANSPDCP, EIOPA, CoB, Institutul Avocatului Poporului, Parlamentul Romaniei, Senatul Romaniei, Administratia Prezidentiale precum si in fata oricarui asigurator din Romania sau reprezentanti si/sau corespondenti ai asiguratorilor externi, in fata BAAR, precum si la sediul oricarui service auto, societate de rent a car, societate de transport-tractari, in vederea sustinerii intereselor mele privind incasarea despagubirilor , a penalitatilor de intarziere si a oricaror alte sume de bani cuvenite legal in urma evenimentului rutier in care am fost implicat cu autovehiculul/motociclul/remorca:
        """
        story.append(Paragraph(p2, style_normal))
        story.append(Spacer(1, 12))

        # Paragraful 3: Vehiculul
        p3 = f"""
        Marca: <b>{make}</b> Tipul: <b>{model}</b> avand nr. inmatriculare: <b>{license_plate}</b>
        cu seria de sasiu <b>{vin}</b> pt constatarea, instrumentarea si lichidarea prin plata integrala a daunelor de catre asiguratori, BAAR sau FGA in urma evenimentului rutier din data de <b>{acc_date}</b>
        """
        story.append(Paragraph(p3, style_normal))
        story.append(Spacer(1, 12))

        # Paragraful 4: Atribuții
        p4 = """
        Pentru indeplinirea acestui mandat, MANDATARUL se va prezenta la asiguratori, la service-uri, va depune si va ridica toate actele/documentele sau certificatele solicitate, va putea verifica si semna devizele de reparatie, notele de constatare, facturile emise de: unitatile reparatoare, societati specializate in tractari auto precum si societati de inchiriere auto
        """
        story.append(Paragraph(p4, style_normal))
        story.append(Spacer(1, 12))

        # Paragraful 5: Declarații
        p5 = """
        Asociatia Pagubitilor RCA va putea face orice fel de declaratii-petitii-reclamatii necesare in fata oricaror institutii publice sau private sus-mentionate, in vederea apararii drepturilor si intereselor mele legale semnatura lui fiindu-mi opozabila.
        """
        story.append(Paragraph(p5, style_normal))
        story.append(Spacer(1, 12))

        # Paragraful 6: Acțiuni legale
        p6 = """
        In sustinerea intereselor mele legale, mandatarul va putea depune plangere/actiune catre parchetele de pe langa judecatoriile din aria de jurisdictie, catre orice instanta de orice grad, va putea angaja si un avocat prin semnarea contractului de asistenta juridica cu toate drepturile si obligatiile ce decurg din acesta.
        """
        story.append(Paragraph(p6, style_normal))
        story.append(Spacer(1, 12))

        # Paragraful 7: Valabilitate
        p7 = """
        Mandatul este acceptat de catre mandatar fiind valabil pana la indeplinirea scopului pentru care a fost eliberat, dar nu mai mult de 3(trei) ani conform prevederilor Art.2015 N.C.Civ.
        """
        story.append(Paragraph(p7, style_normal))
        story.append(Spacer(1, 24))

        # --- Footer Semnătură ---
        p_date = "Data: __________________"
        story.append(Paragraph(p_date, style_normal))
        story.append(Spacer(1, 24))

        # Chenar Semnătură (simulat prin Paragraph sau Table, dar aici simplu text)
        p_sign = """
        <br/>
        Semnătura Clientului:<br/>
        (Semnat digital la generarea finală)
        """
        s_sign = ParagraphStyle('SignStyle', parent=style_normal, alignment=TA_CENTER)
        story.append(Paragraph(p_sign, s_sign))

        # Generare
        doc.build(story)

        return os.path.join("documents", filename)

    except Case.DoesNotExist:
        print(f"Eroare: Dosarul {case_id} nu există.")
        return None
    except Exception as e:
        print(f"Eroare generare PDF: {e}")
        return None
