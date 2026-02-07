from django.test import TestCase, Client
from unittest.mock import patch, MagicMock
from apps.claims.models import Case, Client as ClientModel, CaseDocument, CommunicationLog
from apps.bot.flow import FlowManager
from apps.claims.tasks import check_status_and_notify

class FlowFixTestCase(TestCase):
    def setUp(self):
        self.client_model = ClientModel.objects.create(phone_number="0700000000")
        self.case = Case.objects.create(
            client=self.client_model,
            stage=Case.Stage.COLLECTING_DOCS
        )

    @patch("apps.bot.flow.analyze_document_task.delay")
    @patch("apps.bot.flow.requests.get")
    def test_async_upload_no_immediate_missing_msg(self, mock_get, mock_task):
        """
        Test that uploading an image triggers analysis but DOES NOT trigger immediate 'Missing Documents' message.
        """
        # Mock download response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [b"fake_image_content"]
        mock_get.return_value = mock_response

        # Initialize FlowManager
        manager = FlowManager(self.case, self.client_model.phone_number, channel="WEB")

        # Simulate Image Upload (1 image)
        media_urls = [("http://example.com/test.jpg", "image/jpeg")]
        manager.process_message("image", "", media_urls=media_urls)

        # 1. Verify Task Called
        self.assertTrue(mock_task.called)

        # 2. Verify Immediate Response
        # Should contain "Analizez..."
        logs = CommunicationLog.objects.filter(case=self.case, direction="OUT")
        self.assertTrue(logs.exists())

        analyzing_msg = logs.filter(content__contains="Analizez").first()
        self.assertIsNotNone(analyzing_msg)

        # Should NOT contain "Mai am nevoie de" (immediate check skipped)
        missing_msg = logs.filter(content__contains="Mai am nevoie de").first()
        self.assertIsNone(missing_msg, "Should NOT immediately ask for missing docs for async uploads")

    @patch("apps.bot.flow.analyze_document_task.delay")
    @patch("apps.bot.flow.requests.get")
    def test_sync_video_upload_immediate_msg(self, mock_get, mock_task):
        """
        Test that uploading a VIDEO triggers immediate 'Missing Documents' message (since it's sync).
        """
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.iter_content.return_value = [b"fake_video_content"]
        mock_get.return_value = mock_response

        manager = FlowManager(self.case, self.client_model.phone_number, channel="WEB")

        # Simulate Video Upload
        media_urls = [("http://example.com/test.mp4", "video/mp4")]
        manager.process_message("image", "", media_urls=media_urls)

        # 1. Verify Task NOT Called (Video is skipped for AI)
        self.assertFalse(mock_task.called)

        # 2. Verify Immediate Response
        logs = CommunicationLog.objects.filter(case=self.case, direction="OUT")

        # Should contain "Analizez..." (from saved_count > 0 block)
        analyzing_msg = logs.filter(content__contains="Analizez").first()
        self.assertIsNotNone(analyzing_msg)

        # Should contain "Mai am nevoie de" (immediate check executed)
        # Because video is handled synchronously
        missing_msg = logs.filter(content__contains="Mai am nevoie de").first()
        self.assertIsNotNone(missing_msg, "Should ask for missing docs for sync video upload")

    def test_check_status_and_notify_success(self):
        """
        Test that check_status_and_notify correctly sends the 'Validated' message.
        """
        # Create a document
        doc = CaseDocument.objects.create(
            case=self.case,
            doc_type=CaseDocument.DocType.ID_CARD,
            file="uploads/test.jpg"
        )
        self.case.has_id_card = True
        self.case.save()

        # Call notification logic manually
        check_status_and_notify(self.case, processed_doc=doc)

        # Verify Log
        logs = CommunicationLog.objects.filter(case=self.case, direction="OUT").order_by("-id")
        latest_msg = logs.first()

        self.assertIsNotNone(latest_msg)
        self.assertIn("Am validat Buletin", latest_msg.content)
        self.assertIn("Mai am nevoie de", latest_msg.content)
        self.assertNotIn("Buletin (obligatoriu)", latest_msg.content) # Since we have it
