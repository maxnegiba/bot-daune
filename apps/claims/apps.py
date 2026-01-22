from django.apps import AppConfig


class ClaimsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.claims"

    def ready(self):
        # Această linie este CRITICĂ. Fără ea, signals nu merg.
        import apps.claims.signals
