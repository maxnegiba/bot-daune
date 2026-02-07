import base64
import json
import logging
from openai import OpenAI
from django.conf import settings

logger = logging.getLogger(__name__)


class DocumentAnalyzer:
    @staticmethod
    def analyze(image_path):
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        # 1. Codificare imagine în Base64
        with open(image_path, "rb") as image_file:
            base64_image = base64.b64encode(image_file.read()).decode("utf-8")

        # 2. Prompt compatibil cu signals.py
        prompt_text = """
        Ești un expert în asigurări auto și procesare de documente (OCR).
        Analizează cu atenție imaginea atașată. Dacă este un formular "Constatare Amiabilă de Accident", fii foarte atent la separarea coloanelor A (Vehicul A - Stânga/Albastru) și B (Vehicul B - Dreapta/Galben).

        SARCINA: Identifică tipul documentului și extrage datele pentru dosarul de daună.

        ATENȚIE:
        - NU INVENTA DATE. Dacă scrisul este ilizibil sau câmpul este gol, returnează null.
        - Verifică cu atenție numerele de înmatriculare și seriile de șasiu (VIN).

        TIPURI ACCEPTATE (tip_document):
        ["CI" (Buletin), "PERMIS", "TALON" (Certificat Inmatriculare), "AMIABILA", "PROCURA", "EXTRAS" (Extras Cont), "ACTE_VINOVAT", "ALTELE"]

        EXTRAGERE DATE (date_extrase):

        1. PENTRU AMIABILA (Constatare Amiabila):
           - Caută rubricile:
             - 7. Vehicul (Marca, Tip, Nr. Înmatriculare)
             - 8. Societate de asigurări (Denumire)
             - 9. Conducător vehicul (Nume, Prenume)

           - Extrage pentru Vehicul A (Stânga/Albastru):
             - 'nr_auto_a': Nr. Înmatriculare (ex: B 123 ABC)
             - 'vin_a': Serie Șasiu (DOAR dacă apare explicit, altfel null)
             - 'nume_sofer_a': Nume și Prenume șofer
             - 'asigurator_a': Societatea de asigurări

           - Extrage pentru Vehicul B (Dreapta/Galben):
             - 'nr_auto_b': Nr. Înmatriculare
             - 'vin_b': Serie Șasiu (DOAR dacă apare explicit, altfel null)
             - 'nume_sofer_b': Nume și Prenume șofer
             - 'asigurator_b': Societatea de asigurări

        2. PENTRU TALON / PROCURA / ALTELE:
           - Extrage 'nr_auto', 'vin', 'nume', 'cnp'.

        3. PENTRU BULETIN (CI):
           - Extrage 'nume', 'cnp'.

        4. PENTRU EXTRAS CONT:
           - Extrage 'iban', 'titular_cont'.

        5. PENTRU ACTE VINOVAT (RCA, etc):
           - Extrage 'asigurator_vinovat', 'nr_polita'.

        ANALIZA ACCIDENT (analiza_accident):
        - Doar pentru Amiabilă: Cine pare vinovat pe baza schiței și a căsuțelor bifate? ("A", "B", sau "Comun").

        Răspunde STRICT în format JSON:
        {
            "tip_document": "...",
            "date_extrase": { ... },
            "analiza_accident": { "vinovat_probabil": "..." }
        }
        """

        try:
            response = client.chat.completions.create(
                model="gpt-4o",  # Folosim modelul vision
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
                temperature=0.0,  # Zero creativitate, doar OCR
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content
            return json.loads(content)

        except Exception as e:
            logger.error(f"Eroare OpenAI: {e}")
            return {"tip_document": "UNKNOWN", "date_extrase": {}, "error": str(e)}
