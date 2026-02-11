from django.test import TestCase
from unittest.mock import patch, MagicMock
from apps.claims.models import Case, Client, CaseDocument, CommunicationLog
from apps.bot.flow import FlowManager

class HumanModeTestCase(TestCase):
    def setUp(self):
        self.client_model = Client.objects.create(phone_number="0700000000")
        self.case = Case.objects.create(
            client=self.client_model,
            stage=Case.Stage.COLLECTING_DOCS,
            is_human_managed=True
        )

    @patch("apps.bot.flow.analyze_document_task.delay")
    @patch("apps.bot.flow.requests.get")
    def test_whatsapp_human_managed_ignored(self, mock_get, mock_task):
        """
        Verify that WhatsApp uploads are completely ignored when human managed.
        """
        manager = FlowManager(self.case, "0700000000", channel="WHATSAPP")
        media_urls = [("http://example.com/test.jpg", "image/jpeg")]

        manager.process_message("image", "", media_urls=media_urls)

        # Verify NO task called
        self.assertFalse(mock_task.called)

        # Verify NO logs OUT
        logs = CommunicationLog.objects.filter(case=self.case, direction="OUT")
        self.assertFalse(logs.exists())

    @patch("apps.bot.flow.analyze_document_task.delay")
    @patch("apps.bot.flow.requests.get")
    def test_web_human_managed_processed_silently(self, mock_get, mock_task):
        """
        Verify that WEB uploads are processed (OCR task called) but silently (no reply) when human managed.
        """
        # Mock download
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [b"fake_image_content"]
        mock_get.return_value = mock_response

        manager = FlowManager(self.case, "0700000000", channel="WEB")
        media_urls = [("http://example.com/test.jpg", "image/jpeg")]

        manager.process_message("image", "", media_urls=media_urls)

        # Verify Task CALLED
        self.assertTrue(mock_task.called, "OCR Task should be called for WEB uploads even in human mode")

        # Verify NO logs OUT (Silent)
        logs = CommunicationLog.objects.filter(case=self.case, direction="OUT")
        self.assertFalse(logs.exists())

    def test_web_human_managed_text_ignored(self):
        """
        Verify that WEB text messages are ignored when human managed.
        """
        manager = FlowManager(self.case, "0700000000", channel="WEB")

        manager.process_message("text", "Hello")

        # Verify NO logs OUT
        logs = CommunicationLog.objects.filter(case=self.case, direction="OUT")
        self.assertFalse(logs.exists())
