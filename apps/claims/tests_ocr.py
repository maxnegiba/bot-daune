from django.test import SimpleTestCase
from unittest.mock import patch, MagicMock
from apps.claims.services import DocumentAnalyzer
import json
import base64

class DocumentAnalyzerTestCase(SimpleTestCase):
    @patch("apps.claims.services.OpenAI")
    @patch("apps.claims.services.Image")
    @patch("builtins.open", new_callable=MagicMock)
    def test_analyze_amiabila_split_strategy(self, mock_open, mock_image, mock_openai):
        # Setup Mocks
        mock_file = MagicMock()
        mock_file.read.return_value = b"fake_image_bytes"
        mock_open.return_value.__enter__.return_value = mock_file

        # Mock PIL Image
        mock_img_instance = MagicMock()
        mock_img_instance.size = (1000, 2000) # Width, Height
        mock_image.open.return_value = mock_img_instance

        # Mock Crops
        mock_left_crop = MagicMock()
        mock_right_crop = MagicMock()
        mock_img_instance.crop.side_effect = [mock_left_crop, mock_right_crop]

        # Mock OpenAI response
        mock_client = MagicMock()
        mock_openai.return_value = mock_client

        expected_response = {
            "tip_document": "AMIABILA",
            "date_extrase": {
                "nr_auto_a": "AG 12 ABC",
                "nr_auto_b": "B 99 XYZ"
            },
            "analiza_accident": {
                "vinovat_probabil": "A"
            }
        }

        mock_completion = MagicMock()
        mock_completion.choices[0].message.content = json.dumps(expected_response)
        mock_client.chat.completions.create.return_value = mock_completion

        # Execute
        result = DocumentAnalyzer.analyze("dummy_path.jpg")

        # Verify Assertions

        # 1. Check if Image was opened
        mock_image.open.assert_called_once()

        # 2. Check if crops were created
        # Expected crop calls:
        # Left: (0, 0, 500, 2000)
        # Right: (500, 0, 1000, 2000)
        self.assertEqual(mock_img_instance.crop.call_count, 2)
        mock_img_instance.crop.assert_any_call((0, 0, 500, 2000))
        mock_img_instance.crop.assert_any_call((500, 0, 1000, 2000))

        # 3. Check OpenAI call
        mock_client.chat.completions.create.assert_called_once()
        call_args = mock_client.chat.completions.create.call_args
        messages = call_args.kwargs['messages']

        # Verify we sent 3 images
        user_content = messages[0]['content']
        image_content_items = [item for item in user_content if item['type'] == 'image_url']
        self.assertEqual(len(image_content_items), 3, "Should send 3 images (Full, Left, Right)")

        # Verify prompt text contains instructions for 3 images
        text_content = [item for item in user_content if item['type'] == 'text'][0]['text']
        self.assertIn("Ai la dispoziție 3 imagini", text_content)
        self.assertIn("1. IMAGINEA COMPLETĂ", text_content)
        self.assertIn("2. CROP STÂNGA", text_content)
        self.assertIn("3. CROP DREAPTA", text_content)

        # 4. Check Result
        self.assertEqual(result, expected_response)
