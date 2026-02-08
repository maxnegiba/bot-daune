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
        Ești un expert în asigurări auto și procesare de documente (OCR), specializat pe documente românești.
        Analizează cu atenție imaginea atașată.

        SARCINA PRINCIPALĂ:
        Identifică tipul documentului și extrage datele cu maximă precizie.
        Dacă este un formular "Constatare Amiabilă de Accident", trebuie să separi STRICT datele din Coloana A (Stânga/Albastru) de cele din Coloana B (Dreapta/Galben).

        INSTRUCȚIUNI CRITICE PENTRU AMIABILĂ:
        1. SEPARAREA COLOANELOR:
           - Coloana A (STÂNGA, fundal albastru) -> Vehicul A.
           - Coloana B (DREAPTA, fundal galben) -> Vehicul B.
           - Nu amesteca datele între cele două vehicule.

        2. NUMERE DE ÎNMATRICULARE (Format Românesc):
           - Caută tipare de forma: [JJ NN LLL] (ex: AG 22 PAW, DJ 05 XYZ) sau [B NNN LLL] (ex: B 101 ABC).
           - Fii atent la confuzia dintre '0' (cifra zero) și 'O' (litera O), sau '1' (cifra unu) și 'I' (litera I). Corectează pe baza contextului (de ex. județul 'DJ' nu 'D1', numărul '05' nu 'O5').
           - Pentru numere străine, copiază exact ce vezi.

        3. NUME ȘI PRENUME:
           - De obicei sunt scrise cu MAJUSCULE de mână.
           - Caută la secțiunea 9 "Conducător vehicul" (Nume, Prenume) și secțiunea 6 "Asigurat".
           - Dacă scrisul este greu lizibil, oferă cea mai probabilă transcriere.

        TIPURI ACCEPTATE (tip_document):
        ["CI" (Buletin), "PERMIS", "TALON" (Certificat Inmatriculare), "AMIABILA", "PROCURA", "EXTRAS" (Extras Cont), "ACTE_VINOVAT", "ALTELE"]

        EXTRAGERE DATE (date_extrase):

        1. PENTRU AMIABILA (Constatare Amiabila):
           - Extrage pentru Vehicul A (Stânga/Albastru):
             - 'nr_auto_a': Nr. Înmatriculare (ex: AG 22 PAW) - Verifică la Rubrica 7.
             - 'vin_a': Serie Șasiu (DOAR dacă apare explicit la Rubrica 7 sau jos).
             - 'nume_sofer_a': Nume și Prenume șofer (Rubrica 9).
             - 'asigurator_a': Societatea de asigurări (Rubrica 8).

           - Extrage pentru Vehicul B (Dreapta/Galben):
             - 'nr_auto_b': Nr. Înmatriculare (ex: AB 96 MYH) - Verifică la Rubrica 7.
             - 'vin_b': Serie Șasiu.
             - 'nume_sofer_b': Nume și Prenume șofer (Rubrica 9).
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
