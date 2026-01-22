import os
import json
import re
from celery import shared_task
from google import genai
from google.genai import types
from .models import CaseDocument

# Configurare
GOOGLE_KEY = os.getenv("GOOGLE_API_KEY")
# Folosim 1.5 Flash pentru stabilitate OCR
MODEL_NAME = os.getenv("GOOGLE_MODEL_NAME", "gemini-1.5-flash")


def clean_json_response(content):
    content = content.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {}


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=10, max_retries=3)
def analyze_document_task(self, document_id):
    try:
        print(f"--- [AI WORKER] Start Doc ID: {document_id} | Model: {MODEL_NAME} ---")

        doc = CaseDocument.objects.get(id=document_id)
        file_path = doc.file.path

        if not os.path.exists(file_path):
            print("EROARE: Fișier lipsă.")
            return

        client = genai.Client(api_key=GOOGLE_KEY)

        with open(file_path, "rb") as f:
            image_bytes = f.read()

        # PROMPT CHIRURGICAL - BAZAT PE LOCAȚIE VIZUALĂ
        prompt = """
        Analizează imaginea atașată strict ca un scanner OCR.
        
        SARCINA 1: IDENTIFICARE TIP
        - Dacă vezi "Constatare Amiabilă", este "AMIABILA".
        - Dacă vezi "Mandat" sau "Imputernicire", este "PROCURA".
        
        SARCINA 2: EXTRAGERE DATE (FĂRĂ HALUCINAȚII)
        - Caută textul scris de mână sau tipărit.
        - NU scrie "Popescu" sau "B 123 ABC" decât dacă scrie clar pe foaie.
        - Dacă un câmp e neclar, scrie null.
        
        DETALII PENTRU AMIABILĂ:
        - Vehicul A (Stânga): Caută la punctul 6 (Asigurat) -> Nume. Caută la punctul 7 (Vehicul) -> Nr. Înmatriculare.
        - Vehicul B (Dreapta): Caută la punctul 6 (Asigurat) -> Nume. Caută la punctul 7 (Vehicul) -> Nr. Înmatriculare.
        
        DETALII PENTRU PROCURĂ:
        - Caută numele persoanei care dă mandatul ("Subsemnatul").
        - Caută numărul auto ("nr. inmatriculare") și VIN-ul ("sasiu").

        Răspunde JSON:
        {
            "tip_document": "...",
            "date_extrase": { 
                "nume": "...", 
                "nr_auto": "...", 
                "vin": "..."
            },
            "analiza_accident": { 
                "vinovat_probabil": "Vehicul A / B", 
                "motiv": "..." 
            }
        }
        """

        # CONFIGURARE DRACONICĂ (Zero Creativitate)
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                types.Content(
                    parts=[
                        types.Part.from_text(text=prompt),
                        types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    ]
                )
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,  # Îngheață creativitatea
                top_p=0.1,  # Ia în considerare doar cele mai sigure litere
                top_k=1,  # Alege doar varianta #1 statistică (fără alternative)
                response_mime_type="application/json",
            ),
        )

        print(f"--- [AI RAW] ---\n{response.text[:200]}...")

        data = clean_json_response(response.text)
        doc.ocr_data = data

        # Mapare Tip
        tip_raw = data.get("tip_document")
        if tip_raw:
            tip = tip_raw.upper()
            if "CI" in tip:
                doc.doc_type = CaseDocument.DocType.ID_CARD
            elif "PERMIS" in tip:
                doc.doc_type = CaseDocument.DocType.DRIVERS_LICENSE
            elif "TALON" in tip:
                doc.doc_type = CaseDocument.DocType.CAR_REGISTRATION
            elif "AMIABILA" in tip:
                doc.doc_type = CaseDocument.DocType.ACCIDENT_STATEMENT
            elif "PROCURA" in tip:
                doc.doc_type = CaseDocument.DocType.POWER_OF_ATTORNEY
        else:
            doc.doc_type = CaseDocument.DocType.UNKNOWN

        doc.save()
        print(f"--- [AI WORKER] Succes! ---")

    except Exception as e:
        print(f"--- [AI ERROR] {e} ---")
        raise self.retry(exc=e)
