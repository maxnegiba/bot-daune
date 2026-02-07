import json
from django.test import TestCase, Client as TestClient
from apps.claims.models import Client, Case, CommunicationLog, CaseDocument
from unittest.mock import patch, MagicMock

class WebChatTestCase(TestCase):
    def setUp(self):
        self.c = TestClient()
        self.phone = "0799999999"
        self.name = "Web User"

    def test_full_flow(self):
        # 1. Login
        resp = self.c.post(
            '/bot/chat/login/',
            data=json.dumps({"phone": self.phone, "name": self.name}),
            content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data['success'])
        case_id = data['case_id']

        # Verify Case Created
        case = Case.objects.get(id=case_id)
        self.assertEqual(case.client.phone_number, self.phone)
        self.assertEqual(case.stage, Case.Stage.GREETING)

        # 2. Poll Greeting
        resp = self.c.get(f'/bot/chat/poll/?case_id={case_id}&last_id=0')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        msgs = data['messages']
        self.assertTrue(len(msgs) > 0)
        # Check content of greeting
        # Note: Greeting might be split into multiple messages (text + buttons)
        found_greeting = any("Salut" in m['content'] for m in msgs)
        self.assertTrue(found_greeting)

        last_id = msgs[-1]['id']

        # 3. User Response: "DA, Deschide Dosar"
        resp = self.c.post(
            '/bot/chat/send/',
            data={"case_id": case_id, "message": "DA, Deschide Dosar"}
        )
        self.assertEqual(resp.status_code, 200)

        # 4. Poll Response (Instructions)
        # Wait a bit? No, synchronous test client blocks until response.
        # FlowManager runs synchronously in view.
        resp = self.c.get(f'/bot/chat/poll/?case_id={case_id}&last_id={last_id}')
        data = resp.json()
        msgs = data['messages']
        self.assertTrue(len(msgs) > 0)
        # Should contain "Am deschis dosarul"
        found_response = any("Am deschis dosarul" in m['content'] for m in msgs)
        self.assertTrue(found_response)

        # Verify Stage Change
        case.refresh_from_db()
        self.assertEqual(case.stage, Case.Stage.COLLECTING_DOCS)

    @patch("apps.bot.flow.requests.get")
    @patch("apps.claims.tasks.analyze_document_task.delay") # Mock celery task
    def test_file_upload(self, mock_task, mock_get):
        # Login first
        resp = self.c.post(
            '/bot/chat/login/',
            data=json.dumps({"phone": self.phone, "name": self.name}),
            content_type="application/json"
        )
        case_id = resp.json()['case_id']
        case = Case.objects.get(id=case_id)
        case.stage = Case.Stage.COLLECTING_DOCS
        case.save()

        # Mock requests.get for FlowManager download logic
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.iter_content.return_value = [b"fake_image_content"]
        mock_get.return_value = mock_resp

        # Upload file
        from django.core.files.uploadedfile import SimpleUploadedFile
        f = SimpleUploadedFile("test_doc.jpg", b"file_content", content_type="image/jpeg")

        resp = self.c.post(
            '/bot/chat/send/',
            data={"case_id": case_id, "file_0": f}
        )
        self.assertEqual(resp.status_code, 200)

        # Verify Document Created
        self.assertEqual(CaseDocument.objects.filter(case=case).count(), 1)

        # Verify Bot Ack
        # Poll again
        resp = self.c.get(f'/bot/chat/poll/?case_id={case_id}&last_id=0')
        msgs = resp.json()['messages']
        # Should have "Am primit 1 fișier(e)"
        out_msgs = [m for m in msgs if m['direction'] == 'OUT']
        # The last OUT message should be the ack
        # Note: If previous tests left logs, we might see them? No, setUp creates new client/case?
        # No, setUp creates `self.client` (TestClient), not `Client` model.
        # But `Client.objects.get_or_create` uses phone.
        # Since DB is reset per test case in Django TestCase (transaction rollback), we start fresh.

        found_ack = any("Am primit 1 fișier(e)" in m['content'] for m in out_msgs)
        self.assertTrue(found_ack)
