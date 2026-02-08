import base64
import json
import logging
import io
from PIL import Image
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
            width, height = img.size

            # Split vertically
            left_crop = img.crop((0, 0, width // 2, height))
            right_crop = img.crop((width // 2, 0, width, height))

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
           - Format uzual: [JJ NN LLL] (ex: AG 22 PAW, B 101 ABC).
           - Verifică atent caracterele similare: '0' (cifră) vs 'O' (literă), '1' vs 'I', '8' vs 'B'.
           - Corectează județele invalide (ex: 'D1' -> 'DJ').

        3. NUME ȘI PRENUME:
           - Caută la secțiunea 9 "Conducător vehicul" și secțiunea 6 "Asigurat".
           - Transcrie numele complet, corectând majusculele olografe ilizibile.

        TIPURI ACCEPTATE (tip_document):
        ["CI", "PERMIS", "TALON", "AMIABILA", "PROCURA", "EXTRAS", "ACTE_VINOVAT", "ALTELE"]

        EXTRAGERE DATE (date_extrase):

        1. PENTRU AMIABILA:
           - Vehicul A (Imaginea 2 - Stânga):
             - 'nr_auto_a': Nr. Înmatriculare (Rubrica 7).
             - 'vin_a': Serie Șasiu (Opțional/Dacă este lizibil).
             - 'nume_sofer_a': Nume și Prenume (Rubrica 9 sau 6).
             - 'asigurator_a': Societatea de asigurări (Rubrica 8).

           - Vehicul B (Imaginea 3 - Dreapta):
             - 'nr_auto_b': Nr. Înmatriculare (Rubrica 7).
             - 'vin_b': Serie Șasiu (Opțional/Dacă este lizibil).
             - 'nume_sofer_b': Nume și Prenume (Rubrica 9 sau 6).
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
            return json.loads(content)

        except Exception as e:
            logger.error(f"Eroare OpenAI: {e}")
            return {"tip_document": "UNKNOWN", "date_extrase": {}, "error": str(e)}
