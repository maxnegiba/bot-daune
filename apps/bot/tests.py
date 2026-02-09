from django.test import TestCase
from unittest.mock import patch, MagicMock
from apps.claims.models import Case, Client, CaseDocument
from apps.bot.flow import FlowManager

class FlowManagerTestCase(TestCase):
    def setUp(self):
        self.client = Client.objects.create(phone_number="123")
        self.case = Case.objects.create(client=self.client, stage=Case.Stage.GREETING)
        self.manager = FlowManager(self.case, "123")

    @patch("apps.bot.flow.WhatsAppClient")
    def test_greeting_yes(self, mock_wa_cls):
        # Create a new manager to use the mocked client
        self.manager = FlowManager(self.case, "123")
        mock_wa = self.manager.client

        # 1. User says "da"
        self.manager.process_message("text", "da, deschide")

        self.case.refresh_from_db()
        # Should transition to COLLECTING_DOCS
        self.assertEqual(self.case.stage, Case.Stage.COLLECTING_DOCS)

        # Should send 2 messages (Instructions + Resolution Buttons)
        self.assertTrue(mock_wa.send_text.called)
        self.assertTrue(mock_wa.send_buttons.called)

    @patch("apps.bot.flow.WhatsAppClient")
    def test_greeting_no(self, mock_wa_cls):
        self.manager = FlowManager(self.case, "123")
        mock_wa = self.manager.client

        # 1. User says "nu"
        self.manager.process_message("text", "nu, alta")

        self.case.refresh_from_db()
        # Should be human managed
        self.assertTrue(self.case.is_human_managed)

        # Should send text confirmation
        mock_wa.send_text.assert_called_with(self.case, "Am înțeles. Un operator uman a fost notificat și te va contacta în curând.")

    @patch("apps.bot.flow.WhatsAppClient")
    def test_collecting_docs_resolution(self, mock_wa_cls):
        self.manager = FlowManager(self.case, "123")
        mock_wa = self.manager.client

        # Setup case in COLLECTING_DOCS
        self.case.stage = Case.Stage.COLLECTING_DOCS
        self.case.save()

        # User chooses "Regie Proprie"
        self.manager.process_message("text", "vreau regie proprie")

        self.case.refresh_from_db()
        self.assertEqual(self.case.resolution_choice, Case.Resolution.OWN_REGIME)
        mock_wa.send_text.assert_called()

    @patch("apps.bot.flow.WhatsAppClient")
    def test_collecting_docs_service_rar(self, mock_wa_cls):
        self.manager = FlowManager(self.case, "123")
        mock_wa = self.manager.client

        # Setup case in COLLECTING_DOCS
        self.case.stage = Case.Stage.COLLECTING_DOCS
        self.case.save()

        # User chooses "Service RAR"
        self.manager.process_message("text", "service autorizat rar")

        self.case.refresh_from_db()
        self.assertEqual(self.case.resolution_choice, Case.Resolution.SERVICE_RAR)
        self.assertTrue(self.case.is_human_managed)
        mock_wa.send_text.assert_called()

    @patch("apps.bot.flow.analyze_document_task")
    @patch("apps.bot.flow.WhatsAppClient")
    def test_image_upload(self, mock_wa_cls, mock_task):
        self.manager = FlowManager(self.case, "123")
        mock_wa = self.manager.client

        self.case.stage = Case.Stage.COLLECTING_DOCS
        self.case.save()

        # Mock requests.get
        with patch("apps.bot.flow.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            # Mock content for iterator
            mock_resp.iter_content.return_value = [b"fake_image_data"]
            mock_get.return_value = mock_resp

            media_urls = [("http://example.com/image.jpg", "image/jpeg")]

            self.manager.process_message("image", "", media_urls=media_urls)

            # Should create a CaseDocument
            doc = self.case.documents.first()
            self.assertIsNotNone(doc)
            self.assertEqual(doc.doc_type, "UNK") # Defaults to UNKNOWN

            # Should call analyze task
            mock_task.delay.assert_called_with(doc.id)

            # Should send ack
            mock_wa.send_text.assert_called()
