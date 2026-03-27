import os
import django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.test.client import RequestFactory
from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from apps.claims.models import Case
from apps.claims.admin import CaseAdmin

try:
    user = User.objects.filter(is_superuser=True).first()
    factory = RequestFactory()
    request = factory.get('/admin/claims/case/')
    request.user = user

    admin_instance = CaseAdmin(Case, site)
    response = admin_instance.changelist_view(request)
    
    if hasattr(response, 'render'):
        response.render()
        print("Rendered successfully.")
    else:
        print(f"Response status: {response.status_code}")

except Exception as e:
    import traceback
    traceback.print_exc()
