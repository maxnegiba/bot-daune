from django.db.models import Count
from apps.claims.models import Case

def dashboard_callback(request, context):
    """
    Callback function to populate the admin dashboard with statistics.
    """
    # KPI Stats
    total_cases = Case.objects.count()
    active_cases = Case.objects.exclude(stage=Case.Stage.CLOSED).count()
    human_attention_needed = Case.objects.filter(is_human_managed=True).count()

    # Recent Cases (limit to 5)
    recent_cases = Case.objects.select_related('client').order_by('-created_at')[:5]

    context.update({
        "kpi": [
            {
                "title": "Total Dosare",
                "metric": total_cases,
                "footer": "Toate dosarele înregistrate",
            },
            {
                "title": "Dosare Active",
                "metric": active_cases,
                "footer": "Dosare care nu sunt închise",
            },
            {
                "title": "Necesită Atenție UMANĂ",
                "metric": human_attention_needed,
                "footer": "Dosare blocate sau comutate pe manual",
            },
        ],
        "recent_cases": recent_cases,
    })

    return context
