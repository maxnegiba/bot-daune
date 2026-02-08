
from django.test import SimpleTestCase
from unittest.mock import patch, MagicMock
from apps.claims.services import DocumentAnalyzer
import json
import base64

class DocumentAnalyzerTestCase(SimpleTestCase):

    @patch("apps.claims.services.OpenAI")
    @patch("builtins.open")
    @patch("apps.claims.services.Image.open")
    @patch("apps.claims.services.ImageOps")
    def test_analyze_amiabila_deep_scan(self, mock_image_ops, mock_image_open, mock_open, mock_openai_cls):
        # Mock builtins.open
        mock_file = MagicMock()
        mock_file.read.return_value = b"fake_image_content"
        mock_open.return_value.__enter__.return_value = mock_file

        # Mock Image Open & Split
        mock_img = MagicMock()
        mock_img.mode = 'RGB'
        mock_img.size = (1000, 500)
        # Mock crop
        mock_img.crop.return_value = mock_img
        # Mock save
        def save_side_effect(fp, *args, **kwargs):
            fp.write(b"fake_image_bytes")
        mock_img.save.side_effect = save_side_effect

        mock_image_open.return_value.__enter__.return_value = mock_img

        # Mock ImageOps return value
        mock_image_ops.autocontrast.return_value = mock_img

        # Mock OpenAI Client
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        # Mock Responses
        # 1. Full Scan Response
        full_scan_response = {
            "tip_document": "AMIABILA",
            "date_extrase": {
                "nr_auto_a": "AG22PAW",
                "nr_auto_b": "DB56NYH" # Wrong one initially
            },
            "analiza_accident": {"vinovat_probabil": "B"}
        }

        # 2. Deep Scan Left (A)
        deep_scan_a = {
            "nr_auto_a": "AG22PAW",
            "nume_sofer_a": "VELHO ION"
        }

        # 3. Deep Scan Right (B)
        deep_scan_b = {
            "nr_auto_b": "DB96MYH", # Corrected one
            "nume_sofer_b": "DOBLEAC ILIE"
        }

        # Setup side_effect for chat.completions.create
        # It will be called 3 times: Full, Split A, Split B

        mock_choice_full = MagicMock()
        mock_choice_full.message.content = json.dumps(full_scan_response)

        mock_choice_a = MagicMock()
        mock_choice_a.message.content = json.dumps(deep_scan_a)

        mock_choice_b = MagicMock()
        mock_choice_b.message.content = json.dumps(deep_scan_b)

        mock_client.chat.completions.create.side_effect = [
            MagicMock(choices=[mock_choice_full]), # 1. Full
            MagicMock(choices=[mock_choice_a]),    # 2. Split A
            MagicMock(choices=[mock_choice_b])     # 3. Split B
        ]

        # Execute
        result = DocumentAnalyzer.analyze("fake/path/to/image.jpg")

        # Verify
        self.assertEqual(result["tip_document"], "AMIABILA")

        # Check if merged correctly (Deep scan should overwrite)
        self.assertEqual(result["date_extrase"]["nr_auto_b"], "DB96MYH")
        self.assertEqual(result["date_extrase"]["nume_sofer_b"], "DOBLEAC ILIE")

        # Check preservation of existing data
        self.assertEqual(result["analiza_accident"]["vinovat_probabil"], "B")

        # Verify calls
        self.assertEqual(mock_client.chat.completions.create.call_count, 3)

        # Optional: Verify prompt contains new instructions (checking arguments of last call)
        args, kwargs = mock_client.chat.completions.create.call_args
        messages = kwargs['messages']
        user_content = messages[0]['content'][0]['text']

        # Verify our new prompts are being used
        self.assertIn("Valid County Codes", user_content)
        self.assertIn("DB 96 MYH", user_content)
        self.assertIn('"ILIE" vs "ILE"', user_content)
