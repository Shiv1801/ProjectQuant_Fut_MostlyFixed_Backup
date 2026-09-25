"""
Run this once per trading day to log into Fyers and save a fresh access
token to .fyers_token (tokens expire daily). Opens the login URL in your
browser automatically.
"""
import webbrowser

from engine.data_layer.fyers_client.auth import generate_auth_url, exchange_auth_code_for_token


def main():
    url = generate_auth_url()
    print(f"\nOpening login page in your browser...\n{url}\n")
    webbrowser.open(url)

    print("After logging in, you'll be redirected to a URL containing 'auth_code=...'")
    print("Copy the FULL redirected URL or just the auth_code value and paste it below.\n")
    raw = input("Paste auth_code (or full redirect URL): ").strip()

    # Allow pasting either the raw code or the full redirect URL
    if "auth_code=" in raw:
        auth_code = raw.split("auth_code=")[1].split("&")[0]
    else:
        auth_code = raw

    token = exchange_auth_code_for_token(auth_code)
    with open(".fyers_token", "w") as f:
        f.write(token)
    print("\nSuccess. Access token saved to .fyers_token")


if __name__ == "__main__":
    main()
