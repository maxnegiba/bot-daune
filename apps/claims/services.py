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

        # 1. Load image and generate splits using Pillow
        try:
            with open(image_path, "rb") as image_file:
                original_bytes = image_file.read()

            # Create PIL Image for splitting
            img = Image.open(io.BytesIO(original_bytes))

            # Apply autocontrast to improve handwriting visibility
            try:
                # Convert to RGB if necessary (e.g. for PNGs with transparency) to avoid errors
                if img.mode == 'RGBA':
                    img = img.convert('RGB')
                img = ImageOps.autocontrast(img)
            except Exception as e:
                logger.warning(f"Autocontrast failed: {e}")

            width, height = img.size

            # Split vertically with OVERLAP (Left: 0-55%, Right: 45-100%)
            # This ensures we don't cut off text in the middle spine
            split_point_left_end = int(width * 0.55)
            split_point_right_start = int(width * 0.45)

            left_crop = img.crop((0, 0, split_point_left_end, height))
            right_crop = img.crop((split_point_right_start, 0, width, height))

            # Helper to convert PIL image to base64
            def pil_to_base64(pil_img):
                buffered = io.BytesIO()
                pil_img.save(buffered, format="JPEG")
                return base64.b64encode(buffered.getvalue()).decode("utf-8")

            # Helper for bytes
            def bytes_to_base64(data):
                return base64.b64encode(data).decode("utf-8")

            base64_full = bytes_to_base64(original_bytes)
            base64_left = pil_to_base64(left_crop)
            base64_right = pil_to_base64(right_crop)

        except Exception as e:
            logger.error(f"Eroare procesare imagine (Pillow): {e}")
            # Fallback in case of image error: rely on original bytes only if split fails
            # But usually if PIL fails, the image is bad.
            return {"tip_document": "UNKNOWN", "date_extrase": {}, "error": f"Image processing error: {str(e)}"}

        # 2. Prompt avansat pentru strategie Split & Scan
        prompt_text = """
        Ești un expert în asigurări auto și procesare de documente (OCR), specializat pe documente românești.
        Ai la dispoziție 3 imagini pentru a asigura o precizie maximă a datelor.

        IMAGINILE PRIMITE:
        1. IMAGINEA COMPLETĂ (Original): Pentru context general, tip document și analiza schiței accidentului.
        2. CROP STÂNGA (Vehicul A): Conține DOAR datele pentru Vehiculul A (Coloana Albastră). Folosește-o pentru a extrage datele vehiculului A.
        3. CROP DREAPTA (Vehicul B): Conține DOAR datele pentru Vehiculul B (Coloana Galbenă). Folosește-o pentru a extrage datele vehiculului B.

        SARCINA PRINCIPALĂ:
        Identifică tipul documentului și extrage datele cu maximă precizie.

        INSTRUCȚIUNI CRITICE PENTRU AMIABILĂ (Constatare Amiabilă de Accident):

        1. UTILIZAREA IMAGINILOR:
           - Pentru 'nr_auto_a', 'nume_sofer_a', 'asigurator_a': Bazează-te PRIORITAR pe Imaginea 2 (Stânga).
           - Pentru 'nr_auto_b', 'nume_sofer_b', 'asigurator_b': Bazează-te PRIORITAR pe Imaginea 3 (Dreapta).
           - Nu amesteca datele între cele două vehicule!

        2. NUMERE DE ÎNMATRICULARE (Format Românesc):
           - Format uzual: [JJ NN LLL] sau [B NNN LLL] (ex: AG 22 PAW, B 101 ABC).
           - JUDEȚE VALIDE: B, AB, AR, AG, BC, BH, BN, BR, BT, BV, BZ, CJ, CL, CS, CT, CV, DB, DJ, GJ, GL, GR, HD, HR, IF, IL, IS, MH, MM, MS, NT, OT, PH, SB, SJ, SM, SV, TL, TM, TR, VL, VN, VS.
           - ATENȚIE JUDEȚE: Fii foarte atent la județul 'DB' (Dâmbovița). Adesea este scris de mână să semene cu 'B' sau '0B'.
             Dacă vezi un număr de forma "B NN LLL" (ex: B 86 MYH), acesta este INVALID pentru București. București are forma "B NNN LLL".
             În acest caz, verifică dacă prima literă poate fi 'D' (DB) sau alt județ (SB, AB, UB).
             Exemplu: "B 36 NYH" -> VERIFICĂ DACA ESTE "DB 86 MYH" sau "AB 96 MYH".

           - CONFUZII FRECVENTE CARACTERE:
             - 'D' vs 'B' vs '0' (Zero)
             - 'M' vs 'N' vs 'H'
             - '3' vs '8' vs 'B'
             - '1' vs 'I' vs 'L'
             - 'Z' vs '2' vs '7'

           - Dacă ești nesigur de un caracter, returnează null pentru tot numărul decât să inventezi.

        3. NUME ȘI PRENUME:
           - PRIORITATE MAXIMĂ: Extrage numele din Rubrica 6 ("Asigurat/Deținător"). Acesta este numele legal al deținătorului.
           - FALLBACK: Folosește Rubrica 9 ("Conducător vehicul") DOAR DACĂ Rubrica 6 este ilizibilă sau goală.
           - CORECȚII LOGICE: Corectează numele trunchiate sau scrise neclar.
             - "ILE" -> "ILIE"
             - "GHE" -> "GHEORGHE"
             - "NIC" -> "NICOLAE"
             - "BOBLEA" -> Verifică dacă nu cumva este "BOBLEAC" (terminația 'C' sau 'AC' poate fi mică/înghesuită).
             - Verifică prima literă: 'D' poate fi confundat cu 'B' și invers (ex: DOBLEA vs BOBLEA).
           - Transcrie numele complet, corectând evidentele erori de scriere olografă.

        TIPURI ACCEPTATE (tip_document):
        ["CI", "PERMIS", "TALON", "AMIABILA", "PROCURA", "EXTRAS", "ACTE_VINOVAT", "ALTELE"]

        EXTRAGERE DATE (date_extrase):

        1. PENTRU AMIABILA:
           - Vehicul A (Imaginea 2 - Stânga):
             - 'nr_auto_a': Nr. Înmatriculare (Rubrica 7).
             - 'vin_a': Serie Șasiu (Opțional/Dacă este lizibil).
             - 'nume_sofer_a': Numele din Rubrica 6 (Prioritar) sau Rubrica 9.
             - 'asigurator_a': Societatea de asigurări (Rubrica 8).

           - Vehicul B (Imaginea 3 - Dreapta):
             - 'nr_auto_b': Nr. Înmatriculare (Rubrica 7).
             - 'vin_b': Serie Șasiu (Opțional/Dacă este lizibil).
             - 'nume_sofer_b': Numele din Rubrica 6 (Prioritar) sau Rubrica 9.
             - 'asigurator_b': Societatea de asigurări (Rubrica 8).

        2. PENTRU ALTE DOCUMENTE (Folosește Imaginea 1):
           - Talon/Procură: 'nr_auto', 'vin', 'nume', 'cnp'.
           - Buletin: 'nume', 'cnp'.

        ANALIZA ACCIDENT (analiza_accident) - Folosește Imaginea 1 (Completă):
        - Analizează schița și bifelor de la rubrica 12.
        - Determină cine este vinovat: "A", "B", "Comun" sau "Neculpa".

        Răspunde STRICT în format JSON:
        {
            "tip_document": "AMIABILA",
            "date_extrase": { ... },
            "analiza_accident": { "vinovat_probabil": "..." }
        }
        """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_text},
                            # Imagine 1: Full
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_full}"
                                },
                            },
                            # Imagine 2: Stânga (Vehicul A)
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_left}"
                                },
                            },
                            # Imagine 3: Dreapta (Vehicul B)
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{base64_right}"
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
            data = json.loads(content)
            return DocumentAnalyzer._normalize_data(data)

        except Exception as e:
            logger.error(f"Eroare OpenAI: {e}")
            return {"tip_document": "UNKNOWN", "date_extrase": {}, "error": str(e)}

    @staticmethod
    def _normalize_data(data):
        if not data or "date_extrase" not in data:
            return data

        extracted = data["date_extrase"]

        # Simple normalization: Uppercase for license plates
        for key in ["nr_auto_a", "nr_auto_b", "nr_auto"]:
            if key in extracted and isinstance(extracted[key], str):
                extracted[key] = extracted[key].upper().strip()

        return data
