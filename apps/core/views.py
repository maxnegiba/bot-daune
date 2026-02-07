from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie

@ensure_csrf_cookie
def home(request):
    """
    Pagina principală. Asigură existența cookie-ului CSRF pentru frontend.
    """
    return render(request, "core/home.html")
