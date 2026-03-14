from django.test import TestCase, Client as DjangoClient
from django.urls import reverse
from django.contrib.auth.models import User
from apps.claims.models import Client, Case, InvolvedVehicle

class CaseAdminTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser('admin2', 'admin2@test.com', 'password')
        self.client_db = Client.objects.create(phone_number='+40712345678')
        self.case = Case.objects.create(client=self.client_db)
        self.vehicle = InvolvedVehicle.objects.create(case=self.case, role=InvolvedVehicle.Role.VICTIM, license_plate='B123ABC')

    def test_case_changelist(self):
        c = DjangoClient()
        c.login(username='admin2', password='password')
        url = reverse('admin:claims_case_changelist')
        response = c.get(url, follow=True)
        print(f"Status Code: {response.status_code}")

    def test_case_changelist_search(self):
        c = DjangoClient()
        c.login(username='admin2', password='password')
        url = reverse('admin:claims_case_changelist') + "?q=B123ABC"
        response = c.get(url, follow=True)
        print(f"Search Status Code: {response.status_code}")

    def test_client_changelist(self):
        c = DjangoClient()
        c.login(username='admin2', password='password')
        url = reverse('admin:claims_client_changelist')
        response = c.get(url, follow=True)
        print(f"Client Status Code: {response.status_code}")
