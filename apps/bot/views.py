import os
import requests
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.core.files.base import ContentFile
from apps.claims.models import Client, Case, CaseDocument, CommunicationLog
from apps.claims.tasks import analyze_document_task


@csrf_exempt
def whatsapp_webhook(request):
    if request.method == "POST":
        data = request.POST
        incoming_msg = data.get("Body", "").strip().lower()
        sender_phone = data.get("From", "").replace("whatsapp:", "")

        # Verificăm câte fișiere media au fost trimise
        try:
            num_media = int(data.get("NumMedia", 0))
        except ValueError:
            num_media = 0

        # 1. Identificăm clientul și dosarul activ
        client, created = Client.objects.get_or_create(phone_number=sender_phone)
        active_case = (
            Case.objects.filter(client=client).exclude(status=Case.Status.CLOSED).last()
        )

        response_msg = ""
        poze_procesate = 0

        # SCENARIUL A: Clientul trimite una sau mai multe POZE
        if num_media > 0 and active_case:
            # Trecem prin TOATE pozele (de la 0 la num_media-1)
            for i in range(num_media):
                media_url = data.get(f"MediaUrl{i}")
                media_type = data.get(f"MediaContentType{i}")

                if media_url:
                    try:
                        print(f"Descarc poza {i+1}/{num_media} de la: {media_url}")

                        # Header pentru a nu fi blocați de servere externe
                        fake_headers = {
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
                        }

                        r = requests.get(media_url, headers=fake_headers, timeout=15)

                        if r.status_code == 200:
                            # Nume unic fișier
                            file_name = f"{sender_phone}_{active_case.id}_{i}_{os.path.basename(media_url)}"
                            if not any(
                                file_name.lower().endswith(ext)
                                for ext in [".jpg", ".jpeg", ".png", ".pdf"]
                            ):
                                ext = media_type.split("/")[-1]
                                file_name += f".{ext}"

                            # Salvăm documentul
                            doc = CaseDocument(
                                case=active_case,
                                doc_type=CaseDocument.DocType.UNKNOWN,
                                ocr_data={},
                            )
                            doc.file.save(file_name, ContentFile(r.content))
                            doc.save()

                            # Trimitem la AI (Worker)
                            analyze_document_task.delay(doc.id)
                            poze_procesate += 1
                        else:
                            print(f"Eroare download poza {i}: Status {r.status_code}")

                    except Exception as e:
                        print(f"Eroare procesare poza {i}: {e}")

            if poze_procesate > 0:
                response_msg = (
                    f"Am primit {poze_procesate} document(e). Le analizez pe toate..."
                )
            else:
                response_msg = "A apărut o eroare la descărcarea imaginilor."

        # SCENARIUL B: Text simplu (fără poze)
        elif not active_case:
            if "dauna" in incoming_msg or "accident" in incoming_msg:
                active_case = Case.objects.create(
                    client=client, status=Case.Status.UPLOADING
                )
                response_msg = "Am deschis un dosar nou. Te rog trimite pozele (Buletin, Permis, Auto)."
            else:
                response_msg = "Salut! Scrie 'dauna' pentru a deschide un dosar."
        else:
            if "gata" in incoming_msg:
                active_case.status = Case.Status.PROCESSING_OCR
                active_case.save()
                response_msg = "Am notat. Verificăm dosarul..."
            else:
                response_msg = (
                    f"Am atașat mesajul la dosarul {str(active_case.id)[:8]}."
                )

        # Logăm doar sumar
        CommunicationLog.objects.create(
            case=active_case,
            direction="IN",
            content=f"[MEDIA x{num_media}]" if num_media > 0 else incoming_msg,
        )

        return HttpResponse(str(response_msg), content_type="text/plain")

    return HttpResponse("Doar POST este permis", status=405)
