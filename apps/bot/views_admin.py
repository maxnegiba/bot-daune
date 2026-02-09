from django.shortcuts import render, get_object_or_404
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse, HttpResponseBadRequest
from django.db.models import Max, Q, OuterRef, Subquery
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import ensure_csrf_cookie
from django.utils import timezone
import json

from apps.claims.models import Case, CommunicationLog
from apps.bot.utils import WebChatClient


@staff_member_required
@ensure_csrf_cookie
def admin_chat_dashboard(request):
    """
    Renders the main admin chat interface.
    """
    return render(request, "admin/bot/chat_dashboard.html")


@staff_member_required
@require_GET
def api_get_conversations(request):
    """
    Returns a list of active cases sorted by the most recent message.
    """
    # 1. Annotate cases with the timestamp of the last message
    # We want active cases (not closed) or recently closed ones? Let's stick to active + recently active.
    # Actually, the user might want to see history of closed cases too.
    # For now, let's fetch all cases that have at least one log.

    # Subquery to get the last message content/time
    last_msg = CommunicationLog.objects.filter(case=OuterRef('pk')).order_by('-created_at')

    cases = Case.objects.annotate(
        last_msg_time=Max('logs__created_at')
    ).filter(
        logs__isnull=False  # Only cases with messages
    ).order_by('-last_msg_time')

    data = []
    for case in cases:
        # Get the actual last message content efficiently
        # (The annotation only gives us the time, not the content)
        last_log = case.logs.order_by('-created_at').first()
        preview = last_log.content if last_log else ""

        # Truncate preview
        if len(preview) > 50:
            preview = preview[:47] + "..."

        data.append({
            "id": str(case.id),
            "client_name": case.client.full_name or case.client.phone_number,
            "client_phone": case.client.phone_number,
            "stage": case.get_stage_display(),
            "is_human": case.is_human_managed,
            "last_message": preview,
            "last_time": last_log.created_at.strftime("%H:%M") if last_log else "",
            "last_timestamp": last_log.created_at.isoformat() if last_log else "",
            # Simple logic: if last message was IN (from client), it might need attention
            "needs_reply": (last_log.direction == "IN") if last_log else False
        })

    return JsonResponse({"conversations": data})


@staff_member_required
@require_GET
def api_get_messages(request, case_id):
    """
    Returns message history for a specific case.
    Supports 'after_id' for polling optimization.
    """
    case = get_object_or_404(Case, id=case_id)

    after_id = request.GET.get('after_id', 0)

    logs = CommunicationLog.objects.filter(
        case=case,
        id__gt=after_id
    ).order_by('created_at')

    messages = []
    for log in logs:
        messages.append({
            "id": log.id,
            "direction": log.direction, # 'IN' or 'OUT'
            "content": log.content,
            "timestamp": log.created_at.strftime("%H:%M"),
            "full_timestamp": log.created_at.isoformat(),
            "channel": log.channel
        })

    return JsonResponse({
        "case_id": str(case.id),
        "client_name": case.client.full_name or "Client",
        "messages": messages
    })


@staff_member_required
@require_POST
def api_send_message(request, case_id):
    """
    Sends a message from the admin to the client.
    Automatically switches the case to HUMAN mode.
    """
    case = get_object_or_404(Case, id=case_id)

    try:
        data = json.loads(request.body)
        message_text = data.get("message", "").strip()

        if not message_text:
            return JsonResponse({"error": "Message cannot be empty"}, status=400)

        # 1. Mark as Human Managed
        if not case.is_human_managed:
            case.is_human_managed = True
            case.save(update_fields=['is_human_managed'])

        # 2. Log the message (OUT)
        # We default to WEB channel for admin chats, or match the last channel used?
        # The prompt says: "client e pe WhatsApp -> Twilio, client e pe Web -> Web DB".
        # We need to detect the active channel.
        # A simple heuristic: check the last IN message's channel.
        last_in_msg = case.logs.filter(direction="IN").order_by('-created_at').first()
        target_channel = last_in_msg.channel if last_in_msg else "WEB"

        # 3. Send Logic
        # For this task, the user said: "3 uita complet de twilio, raman doar pe chatul nostru"
        # So we force WEB channel logic (just DB save).

        # However, to be consistent with the "Chat" interface, we just save it as an OUT message.
        # The Web Client polls for OUT messages.

        log = CommunicationLog.objects.create(
            case=case,
            direction="OUT",
            channel="WEB", # Forcing WEB as per instruction "raman doar pe chatul nostru" (implies internal system)
            content=message_text
        )

        return JsonResponse({
            "success": True,
            "message": {
                "id": log.id,
                "content": log.content,
                "timestamp": log.created_at.strftime("%H:%M"),
                "direction": "OUT"
            }
        })

    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
