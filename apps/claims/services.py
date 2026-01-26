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
        Ești un expert în asigurări auto. Analizează imaginea atașată (OCR).
        
        SARCINA: Identifică tipul documentului și extrage datele pentru dosarul de daună.
        
        TIPURI ACCEPTATE (tip_document):
        ["CI" (Buletin), "PERMIS", "TALON" (Certificat Inmatriculare), "AMIABILA", "PROCURA", "EXTRAS" (Extras Cont), "ACTE_VINOVAT", "ALTELE"]

        EXTRAGERE DATE (date_extrase):
        - Pentru AMIABILA: Extrage 'nr_auto_a', 'vin_a', 'nume_sofer_a' (Vehicul A) și 'nr_auto_b', 'vin_b', 'nume_sofer_b' (Vehicul B).
        - Pentru TALON/PROCURA/ALTELE: Extrage 'nr_auto', 'vin', 'nume', 'cnp'.
        - Pentru BULETIN: Extrage 'nume', 'cnp'.
        - Pentru EXTRAS: Extrage 'iban', 'titular_cont'.
        - Pentru ACTE_VINOVAT: Extrage 'asigurator_vinovat', 'nr_polita'.
        
        ANALIZA ACCIDENT (analiza_accident):
        - Doar pentru Amiabilă: Cine pare vinovat? ("A", "B", sau "Comun").
        
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
