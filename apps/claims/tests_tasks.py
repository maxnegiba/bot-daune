from django.test import TestCase
from unittest.mock import patch, MagicMock, ANY
from apps.claims.tasks import send_claim_email_task
from apps.claims.models import Case, Client, CaseDocument

class SendClaimEmailTaskTestCase(TestCase):
    def setUp(self):
        self.client = Client.objects.create(
            first_name="John",
            last_name="Doe",
            phone_number="+40700000000",
            email="john@example.com"
        )
        self.case = Case.objects.create(client=self.client)

        # Create problematic documents
        self.doc1 = CaseDocument.objects.create(
            case=self.case,
            doc_type=CaseDocument.DocType.ACCIDENT_REPORT, # "Amiabilă / PV Poliție"
            file="uploads/amiabila.jpg"
        )
        self.doc2 = CaseDocument.objects.create(
            case=self.case,
            doc_type=CaseDocument.DocType.DAMAGE_PHOTO, # "Poză Daună / Video"
            file="uploads/photo.jpg"
        )

    @patch("apps.claims.tasks.EmailMessage")
    @patch("apps.claims.tasks.shutil.copy")
    @patch("apps.claims.tasks.os.path.exists")
    @patch("apps.claims.tasks.shutil.rmtree")
    @patch("apps.claims.tasks.tempfile.mkdtemp")
    def test_send_claim_email_sanitizes_filenames(self, mock_mkdtemp, mock_rmtree, mock_exists, mock_copy, mock_email_class):
        # Setup mocks
        mock_mkdtemp.return_value = "/tmp/mock_dir"
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance
        mock_exists.return_value = True # ensure cleanup happens

        # Run task
        send_claim_email_task(self.case.id)

        # Verify EmailMessage was initialized
        mock_email_class.assert_called_once()

        # Verify attachments
        # We expect 2 attachments
        self.assertEqual(mock_email_instance.attach_file.call_count, 2)

        # Check filenames in attach_file calls
        # The path passed to attach_file should contain the sanitized filename

        # Get all calls to attach_file
        calls = mock_email_instance.attach_file.call_args_list

        filenames = []
        for call in calls:
            path = call[0][0] # first arg is path
            filenames.append(path)

        # We expect sanitized names:
        # "Amiabilă___PV_Poliție" or "Amiabilă_/_PV_Poliție" -> "Amiabilă___PV_Poliție" (depending on spaces)
        # "Poză_Daună___Video"

        # Let's check if any filename contains "/" (except for the dir part)
        # The dir part is /tmp/mock_dir

        for f in filenames:
            basename = f.replace("/tmp/mock_dir/", "")
            self.assertNotIn("/", basename, f"Filename {basename} should not contain /")

        # Specific checks based on expected sanitization
        # "Amiabilă / PV Poliție" -> replace / with _, replace space with _
        # "Amiabilă___PV_Poliție" (space / space -> _ _ _)

        # Note: In models.py: ACCIDENT_REPORT = "AMIABILA", _("Amiabilă / PV Poliție")
        # Note: In models.py: DAMAGE_PHOTO = "PHOTO", _("Poză Daună / Video")

        # So "Amiabilă / PV Poliție" -> "Amiabilă___PV_Poliție"

        found_amiabila = any("Amiabilă___PV_Poliție" in f for f in filenames)
        found_photo = any("Poză_Daună___Video" in f for f in filenames)

        self.assertTrue(found_amiabila, f"Did not find sanitized Amiabila filename in {filenames}")
        self.assertTrue(found_photo, f"Did not find sanitized Photo filename in {filenames}")

    @patch("apps.claims.tasks.EmailMessage")
    @patch("apps.claims.tasks.shutil.copy")
    @patch("apps.claims.tasks.os.path.exists")
    @patch("apps.claims.tasks.shutil.rmtree")
    @patch("apps.claims.tasks.tempfile.mkdtemp")
    def test_send_claim_email_handles_missing_file(self, mock_mkdtemp, mock_rmtree, mock_exists, mock_copy, mock_email_class):
         # Test robust error handling if file is missing
        mock_mkdtemp.return_value = "/tmp/mock_dir"
        mock_email_instance = MagicMock()
        mock_email_class.return_value = mock_email_instance

        # Mock copy to raise exception for one file
        mock_copy.side_effect = [FileNotFoundError("File not found"), None]

        # Run task
        send_claim_email_task(self.case.id)

        # Should still try to send email even if one attachment failed
        mock_email_instance.send.assert_called_once()
