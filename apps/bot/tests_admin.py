from django.test import TestCase, Client
from django.contrib.auth.models import User
from apps.claims.models import Case, Client as ClientModel, CommunicationLog
import json
import uuid

class AdminChatTestCase(TestCase):
    def setUp(self):
        # Create Admin User
        self.admin_user = User.objects.create_superuser('admin', 'admin@test.com', 'password')
        self.client.force_login(self.admin_user)

        # Create Client & Case
        self.c_model = ClientModel.objects.create(phone_number="0700000000", full_name="Test Client")
        self.case = Case.objects.create(client=self.c_model, stage=Case.Stage.GREETING)

        # Create some logs
        CommunicationLog.objects.create(case=self.case, direction="IN", content="Hello", channel="WEB")
        CommunicationLog.objects.create(case=self.case, direction="OUT", content="Hi there", channel="WEB")

    def test_dashboard_access(self):
        resp = self.client.get('/bot/admin/dashboard/')
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'admin/bot/chat_dashboard.html')

    def test_api_conversations(self):
        resp = self.client.get('/bot/admin/api/conversations/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn('conversations', data)
        self.assertEqual(len(data['conversations']), 1)
        conv = data['conversations'][0]
        self.assertEqual(conv['client_name'], "Test Client")
        self.assertEqual(conv['last_message'], "Hi there")

    def test_api_messages(self):
        resp = self.client.get(f'/bot/admin/api/messages/{self.case.id}/')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data['messages']), 2)
        self.assertEqual(data['messages'][0]['content'], "Hello")

    def test_api_send_message(self):
        # Send message
        resp = self.client.post(
            f'/bot/admin/api/send/{self.case.id}/',
            data=json.dumps({"message": "Admin Reply"}),
            content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])

        # Verify Case switched to Human
        self.case.refresh_from_db()
        self.assertTrue(self.case.is_human_managed)

        # Verify Log created
        log = CommunicationLog.objects.last()
        self.assertEqual(log.content, "Admin Reply")
        self.assertEqual(log.direction, "OUT")
