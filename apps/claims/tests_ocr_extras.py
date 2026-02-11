from django.test import SimpleTestCase
from unittest.mock import patch, MagicMock
from apps.claims.services import DocumentAnalyzer
import json

class DocumentAnalyzerExtrasTestCase(SimpleTestCase):
    @patch("apps.claims.services.OpenAI")
    @patch("apps.claims.services.Image")
    @patch("apps.claims.services.ImageOps")
    @patch("builtins.open", new_callable=MagicMock)
    def test_analyze_extras_prompt_and_parsing(self, mock_open, mock_image_ops, mock_image, mock_openai):
        # Setup Mocks
        mock_file = MagicMock()
        mock_file.read.return_value = b"fake_image_bytes"
        mock_open.return_value.__enter__.return_value = mock_file

        # Mock PIL Image
        mock_img_instance = MagicMock()
        mock_img_instance.size = (1000, 1000)
        mock_img_instance.mode = 'RGB'
        mock_image.open.return_value = mock_img_instance

        # Mock ImageOps.autocontrast to return the same image
        mock_image_ops.autocontrast.return_value = mock_img_instance

        # Mock crop calls
        mock_img_instance.crop.return_value = MagicMock()

        # Mock OpenAI response
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        # Simulate an AI response for an Extras document
        expected_raw_iban = " ro 12 btrl 0120 1234 5678 90xx "
        expected_normalized_iban = "RO12BTRL01201234567890XX"

        ai_response_data = {
            "tip_document": "EXTRAS",
            "date_extrase": {
                "iban": expected_raw_iban
            },
            "analiza_accident": {}
        }

        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = json.dumps(ai_response_data)
        mock_client.chat.completions.create.return_value = mock_completion

        # Execute
        result = DocumentAnalyzer.analyze("dummy_path_extras.jpg")

        # Verify Assertions

        # 1. Check prompt content
        mock_client.chat.completions.create.assert_called_once()
        call_args = mock_client.chat.completions.create.call_args
        messages = call_args.kwargs['messages']
        user_content = messages[0]['content']
        text_content = [item for item in user_content if item['type'] == 'text'][0]['text']

        # Verify new instructions are present
        self.assertIn("INSTRUCȚIUNI PENTRU EXTRAS DE CONT (BANCAR)", text_content)
        self.assertIn("Extrage codul IBAN complet", text_content)
        self.assertIn("PENTRU EXTRAS DE CONT (Folosește Imaginea 1)", text_content)

        # 2. Check Result Parsing & Normalization
        self.assertEqual(result["tip_document"], "EXTRAS")
        self.assertEqual(result["date_extrase"]["iban"], expected_normalized_iban)
