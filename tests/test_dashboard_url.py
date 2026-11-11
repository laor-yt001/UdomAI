import unittest
from unittest.mock import MagicMock, patch

from dashboard_url import fetch_ngrok_dashboard_url


class DashboardUrlTests(unittest.TestCase):
    @patch("dashboard_url.requests.get")
    def test_returns_final_ngrok_url_after_redirect(self, get):
        response = MagicMock()
        response.url = "https://domain-tadpole-golf.ngrok-free.dev/?p=dXNlcnM="
        get.return_value.__enter__.return_value = response

        result = fetch_ngrok_dashboard_url("https://domain-tadpole-golf.ngrok-free.dev")

        self.assertEqual(result, response.url)
        get.assert_called_once_with(
            "https://domain-tadpole-golf.ngrok-free.dev",
            allow_redirects=True,
            stream=True,
            timeout=10,
        )
        response.raise_for_status.assert_called_once_with()

    @patch("dashboard_url.requests.get")
    def test_rejects_redirect_to_different_host(self, get):
        response = MagicMock()
        response.url = "https://attacker.example/"
        get.return_value.__enter__.return_value = response

        with self.assertRaisesRegex(ValueError, "unexpected dashboard URL"):
            fetch_ngrok_dashboard_url("https://domain-tadpole-golf.ngrok-free.dev")

    def test_rejects_non_https_endpoint(self):
        with self.assertRaisesRegex(ValueError, "must be an HTTPS URL"):
            fetch_ngrok_dashboard_url("http://domain-tadpole-golf.ngrok-free.dev")


if __name__ == "__main__":
    unittest.main()
