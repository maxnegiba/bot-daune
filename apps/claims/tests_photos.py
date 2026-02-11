from django.test import TestCase
from unittest.mock import patch, MagicMock
from apps.claims.models import Case, CaseDocument, Client
from apps.claims.tasks import check_status_and_notify, analyze_document_task
from apps.bot.flow import FlowManager

class PhotoLogicTestCase(TestCase):
    def setUp(self):
        self.client = Client.objects.create(phone_number="0700123456", first_name="Test", last_name="User")
        self.case = Case.objects.create(client=self.client, stage=Case.Stage.COLLECTING_DOCS)
        # Pre-fill required docs so we only test the photo logic
        self.case.has_id_card = True
        self.case.has_car_coupon = True
        self.case.has_accident_report = True
        self.case.save()

    @patch("apps.claims.tasks.DocumentAnalyzer.analyze")
    def test_analyze_task_recognizes_photo(self, mock_analyze):
        """Test that analyze_document_task correctly sets DAMAGE_PHOTO type."""
        # Setup mock return
        mock_analyze.return_value = {
            "tip_document": "FOTO_AUTO",
            "date_extrase": {}
        }

        doc = CaseDocument.objects.create(case=self.case, doc_type=CaseDocument.DocType.UNKNOWN, file="test.jpg")

        # Run task synchronously
        analyze_document_task(doc.id)

        doc.refresh_from_db()
        self.assertEqual(doc.doc_type, CaseDocument.DocType.DAMAGE_PHOTO, "Should be updated to DAMAGE_PHOTO")

    @patch("apps.bot.utils.WhatsAppClient.send_text")
    def test_check_status_missing_photos(self, mock_send):
        """Test that notification asks for photos if < 4 and no video."""
        # 0 Photos, No Video
        check_status_and_notify(self.case)

        args, _ = mock_send.call_args
        msg = args[1]
        self.assertIn("Video 360 Grade SAU minim 4 Poze", msg)
        self.assertIn("ai trimis 0", msg)

        # Add 3 Photos
        for i in range(3):
            CaseDocument.objects.create(case=self.case, doc_type=CaseDocument.DocType.DAMAGE_PHOTO, file=f"p{i}.jpg", ocr_data={})

        check_status_and_notify(self.case)
        args, _ = mock_send.call_args
        msg = args[1]
        self.assertIn("ai trimis 3", msg)

    @patch("apps.bot.utils.WhatsAppClient.send_buttons")
    def test_check_status_success_with_photos(self, mock_buttons):
        """Test that 4 photos satisfy the requirement."""
        # Add 4 Photos
        for i in range(4):
            CaseDocument.objects.create(case=self.case, doc_type=CaseDocument.DocType.DAMAGE_PHOTO, file=f"p{i}.jpg", ocr_data={})

        check_status_and_notify(self.case)

        # Should call send_buttons (Greeting success) or text (Mandate) depending on resolution
        # Here resolution is UNDECIDED so it asks for resolution
        self.assertTrue(mock_buttons.called)
        args, _ = mock_buttons.call_args
        self.assertIn("toate documentele necesare", args[1])

    @patch("apps.bot.utils.WhatsAppClient.send_buttons")
    def test_check_status_success_with_video(self, mock_buttons):
        """Test that video satisfies the requirement even with 0 photos."""
        self.case.has_scene_video = True
        self.case.save()

        check_status_and_notify(self.case)

        self.assertTrue(mock_buttons.called)
        args, _ = mock_buttons.call_args
        self.assertIn("toate documentele necesare", args[1])
