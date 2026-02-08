import base64
import json
import logging
import io
from PIL import Image, ImageOps
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)


class DocumentAnalyzer:
    @staticmethod
    def analyze(image_path):
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        # 1. Analiză inițială (Imagine completă)
        # Folosim logica existentă pentru a determina tipul și o analiză generală
        initial_result = DocumentAnalyzer._analyze_full_image(client, image_path)

        # Dacă a eșuat sau nu e JSON valid, returnăm așa cum e
        if "error" in initial_result:
            return initial_result

        doc_type = initial_result.get("tip_document", "").upper()

        # 2. Dacă este AMIABILA, declanșăm "Deep Scan" (Split & Merge)
        if "AMIABILA" in doc_type:
            logger.info(f"Amiabila detectată ({image_path}). Inițiere Deep Scan (Split & OCR)...")
            try:
                # Spargem imaginea
                left_bytes, right_bytes = DocumentAnalyzer._split_amiabila(image_path)

                # Analizăm jumătatea stângă (Vehicul A)
                data_a = DocumentAnalyzer._analyze_half(client, left_bytes, "A")

                # Analizăm jumătatea dreaptă (Vehicul B)
                data_b = DocumentAnalyzer._analyze_half(client, right_bytes, "B")

                # Actualizăm datele extrase inițial cu cele mai precise
                extracted = initial_result.get("date_extrase", {})

                # Facem merge (datele din split au prioritate)
                if data_a:
                    extracted.update(data_a)
                if data_b:
                    extracted.update(data_b)

                initial_result["date_extrase"] = extracted
                logger.info("Deep Scan complet. Datele au fost actualizate.")

            except Exception as e:
                logger.error(f"Eroare la Deep Scan Amiabila: {e}")
                # Nu crăpăm tot procesul, returnăm ce am găsit inițial

        return initial_result

    @staticmethod
    def _analyze_full_image(client, image_path):
        """
        Analiza standard pe imaginea întreagă (Identificare tip + Extragere generală).
        """
        try:
            with open(image_path, "rb") as image_file:
                base64_image = base64.b64encode(image_file.read()).decode("utf-8")

            prompt_text = """
            Ești un expert în asigurări auto și procesare de documente (OCR), specializat pe documente românești.
            Analizează cu atenție imaginea atașată.

            SARCINA PRINCIPALĂ:
            Identifică tipul documentului și extrage datele cu maximă precizie.
            Dacă este un formular "Constatare Amiabilă de Accident", trebuie să separi STRICT datele din Coloana A (Stânga/Albastru) de cele din Coloana B (Dreapta/Galben).

            INSTRUCȚIUNI CRITICE PENTRU AMIABILĂ (Structură Plată - Fără Obiecte Imbricate):
            1. SEPARAREA COLOANELOR:
            - Coloana A (STÂNGA, fundal albastru) -> Date cu sufixul "_a".
            - Coloana B (DREAPTA, fundal galben) -> Date cu sufixul "_b".
            - Nu amesteca datele între cele două vehicule.

            2. NUMERE DE ÎNMATRICULARE (Format Românesc):
            - Caută tipare de forma: [JJ NN LLL] (ex: AG 22 PAW, DJ 05 XYZ) sau [B NNN LLL] (ex: B 101 ABC).
            - Fii atent la confuzia dintre '0' (cifra zero) și 'O' (litera O), sau '1' (cifra unu) și 'I' (litera I). Corectează pe baza contextului (de ex. județul 'DJ' nu 'D1', numărul '05' nu 'O5').
            - Pentru numere străine, copiază exact ce vezi.

            3. NUME ȘI PRENUME:
            - De obicei sunt scrise cu MAJUSCULE de mână.
            - Caută la secțiunea 9 "Conducător vehicul" (Nume, Prenume) și secțiunea 6 "Asigurat".
            - Ignoră etichete pre-tipărite. Extrage doar scrisul de mână.

            4. ANTI-HALUCINAȚII:
            - Dacă un câmp nu este lizibil sau este gol, returnează null sau "".
            - NU INVENTA DATE (ex: nu pune "VL 03 YZY" dacă nu scrie clar).
            - NU confunda Numere de Telefon cu VIN (Serie Șasiu). VIN are 17 caractere alfanumerice. Telefonul începe cu 07.

            TIPURI ACCEPTATE (tip_document):
            ["CI" (Buletin), "PERMIS", "TALON" (Certificat Inmatriculare), "AMIABILA", "PROCURA", "EXTRAS" (Extras Cont), "ACTE_VINOVAT", "ALTELE"]

            EXTRAGERE DATE (date_extrase) - STRUCTURĂ PLATĂ:

            1. PENTRU AMIABILA (Constatare Amiabila):
            - Extrage pentru Vehicul A (Stânga/Albastru):
                - 'nr_auto_a': Nr. Înmatriculare (Rubrica 7).
                - 'vin_a': Serie Șasiu (DOAR dacă are 17 caractere. NU TELEFON!).
                - 'nume_sofer_a': Nume și Prenume șofer (Rubrica 9 sau 6).
                - 'asigurator_a': Societatea de asigurări (Rubrica 8).

            - Extrage pentru Vehicul B (Dreapta/Galben):
                - 'nr_auto_b': Nr. Înmatriculare (Rubrica 7).
                - 'vin_b': Serie Șasiu (DOAR dacă are 17 caractere. NU TELEFON!).
                - 'nume_sofer_b': Nume și Prenume șofer (Rubrica 9 sau 6).
                - 'asigurator_b': Societatea de asigurări (Rubrica 8).

            2. PENTRU TALON / PROCURA / ALTELE:
            - Extrage 'nr_auto', 'vin', 'nume', 'cnp'.

            3. PENTRU BULETIN (CI):
            - Extrage 'nume', 'cnp'.

            4. PENTRU EXTRAS CONT:
            - Extrage 'iban', 'titular_cont'.

            5. PENTRU ACTE VINOVAT (RCA, etc):
            - Extrage 'asigurator_vinovat', 'nr_polita'.

            ANALIZA ACCIDENT (analiza_accident):
            - Doar pentru Amiabilă: Analizează cine este vinovat.
            - Verifică căsuțele bifate la secțiunea 12 (Împrejurări).
            - Verifică schița accidentului.
            - Returnează: "A", "B", "Comun" sau "Neculpa" (dacă nu e clar).

            Răspunde STRICT în format JSON:
            {
                "tip_document": "...",
                "date_extrase": {
                    "nr_auto_a": "...", "vin_a": "...", "nume_sofer_a": "...", "asigurator_a": "...",
                    "nr_auto_b": "...", "vin_b": "...", "nume_sofer_b": "...", "asigurator_b": "..."
                },
                "analiza_accident": { "vinovat_probabil": "..." }
            }
            """

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                },
                            },
                        ],
                    }
                ],
                max_tokens=1000,
                temperature=0.0,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            return json.loads(content)

        except Exception as e:
            logger.error(f"Eroare OpenAI (Full Scan): {e}")
            return {"tip_document": "UNKNOWN", "date_extrase": {}, "error": str(e)}

    @staticmethod
    def _split_amiabila(image_path):
        """
        Deschide imaginea, aplică pre-procesare (contrast) și o taie în două pe verticală.
        Returnează (bytes_stanga, bytes_dreapta).
        """
        with Image.open(image_path) as img:
            # Convertim la RGB dacă e necesar
            if img.mode != 'RGB':
                img = img.convert('RGB')

            # Opțional: Auto-contrast pentru a evidenția scrisul
            # img = ImageOps.autocontrast(img, cutoff=2)

            width, height = img.size
            # Tăiem exact la jumătate
            # Presupunem format standard Amiabilă (Landscape sau A4) unde A e stânga, B e dreapta.
            midpoint = width // 2

            left_crop = img.crop((0, 0, midpoint, height))
            right_crop = img.crop((midpoint, 0, width, height))

            # Convertim în bytes
            return (
                DocumentAnalyzer._image_to_bytes(left_crop),
                DocumentAnalyzer._image_to_bytes(right_crop)
            )

    @staticmethod
    def _image_to_bytes(pil_image):
        """
        Helper pentru conversie PIL Image -> Bytes
        """
        buffer = io.BytesIO()
        pil_image.save(buffer, format="JPEG", quality=95)
        return buffer.getvalue()

    @staticmethod
    def _analyze_half(client, image_bytes, vehicle_role):
        """
        Analizează o jumătate de amiabilă (A sau B) cu un prompt focusat pe scris de mână.
        vehicle_role: "A" sau "B"
        """
        base64_image = base64.b64encode(image_bytes).decode("utf-8")

        role_label = "STÂNGA (Vehicul A, Albastru)" if vehicle_role == "A" else "DREAPTA (Vehicul B, Galben)"
        suffix = vehicle_role.lower() # 'a' sau 'b'

        prompt_text = f"""
        Analizezi {role_label} a unei Constatări Amiabile.
        Aceasta este o imagine "crop" de înaltă rezoluție.

        SARCINA TA: Extrage datele de identificare scrise de mână cu maximă precizie.

        INSTRUCȚIUNI SPECIFICE OCR:
        1. Numere de Înmatriculare (Format Românesc):
           - Caută tipare: JJ 00 LLL (ex: AG 22 PAW) sau B 000 LLL.
           - ATENȚIE: Scrisul de mână poate fi ambiguu.
             - '0' (zero) vs 'O' (litera O): În numere sunt cifre (ex: 05), în județe litere (ex: OT).
             - '1' (unu) vs 'I' (litera I): În numere sunt cifre, în județe litere.
           - Returnează numărul curat, fără spații (ex: AG22PAW).

        2. Nume și Prenume:
           - Verifică Rubrica 9 "Conducător vehicul" (cea mai importantă).
           - Verifică Rubrica 6 "Asigurat/Deținător poliță".
           - Numele sunt de obicei scrise cu MAJUSCULE.
           - Transcrie exact ce vezi scris de mână.

        3. Serie Șasiu (VIN):
           - Caută un cod de 17 caractere la Rubrica 7 sau în partea de jos a secțiunii.
           - ATENȚIE: NU confunda cu Numărul de Telefon (care începe cu 07...).
           - Dacă e un număr de telefon, ignoră-l. Returnează doar VIN-ul real sau null.

        4. Asigurator:
           - Verifică Rubrica 8 "Societate de asigurări".
           - Extrage numele companiei (ex: GROUPAMA, ALLIANZ, GENERALI, EUROINS, HELLAS DIRECT, etc).

        Returnează JSON STRICT cu cheile:
        {{
            "nr_auto_{suffix}": "...",
            "vin_{suffix}": "...",
            "nume_sofer_{suffix}": "...",
            "asigurator_{suffix}": "..."
        }}
        Folosește null sau "" pentru valori lipsă.
        """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_image}"
                                },
                            },
                        ],
                    }
                ],
                max_tokens=500,
                temperature=0.0,
                response_format={"type": "json_object"},
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.warning(f"Eroare Deep Scan {vehicle_role}: {e}")
            return {}
