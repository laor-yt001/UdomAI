import urllib.parse

import requests


DEFAULT_NGROK_URL = "https://domain-tadpole-golf.ngrok-free.dev/?p=dXNlcnM="


def fetch_ngrok_dashboard_url(endpoint):
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    if parsed_endpoint.scheme != "https" or not parsed_endpoint.hostname:
        raise ValueError("NGROK_URL must be an HTTPS URL.")

    with requests.get(endpoint, allow_redirects=True, stream=True, timeout=10) as response:
        response.raise_for_status()
        resolved = urllib.parse.urlsplit(response.url)

    if (
        resolved.scheme != "https"
        or resolved.hostname != parsed_endpoint.hostname
        or resolved.username
        or resolved.password
    ):
        raise ValueError("Ngrok redirected to an unexpected dashboard URL.")
    return response.url
