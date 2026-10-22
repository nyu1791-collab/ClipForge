#!/usr/bin/env python3
"""Register an already-authorized Google user with private Actions; never print tokens."""

import argparse
import json
import subprocess
import sys

from colab_actions import EXPECTED_ACCOUNT, ControlError, authenticated_client
from colab_bundle import REPOSITORY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--login",
        action="store_true",
        help="Run the one-time human Google consent flow first",
    )
    args = parser.parse_args()
    if args.login:
        if not sys.stdin.isatty():
            raise ControlError(
                "Run --login in an interactive terminal; do not paste OAuth tokens into chat"
            )
        scopes = ",".join(
            [
                "openid",
                "https://www.googleapis.com/auth/cloud-platform",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/colaboratory",
            ]
        )
        subprocess.run(
            [
                "gcloud",
                "auth",
                "application-default",
                "login",
                "--account",
                EXPECTED_ACCOUNT,
                "--scopes",
                scopes,
            ],
            check=True,
        )
    import google.auth
    from google.oauth2.credentials import Credentials

    credentials, _ = google.auth.default()
    if not isinstance(credentials, Credentials):
        raise ControlError(
            "Use the intended Google end-user ADC; service accounts cannot replace the Colab Pro account"
        )
    info = json.loads(credentials.to_json())
    info["type"] = "authorized_user"
    client = authenticated_client(info)
    client.list_assignments()  # Read-only. No CPU/GPU assignment is created.
    owner = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"],
        text=True,
        capture_output=True,
        check=True,
    )
    if owner.stdout.strip() != "nyu1791-collab":
        raise ControlError("Use the authenticated GitHub repository owner connection")
    result = subprocess.run(
        ["gh", "secret", "set", "COLAB_ADC_JSON", "--repo", REPOSITORY],
        input=json.dumps(info),
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise ControlError(
            "Private Actions secret registration failed; credential values were not displayed"
        )
    print(
        "COLAB_ADC_JSON registered for the verified account. No runtime was allocated."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        detail = str(error) if isinstance(error, ControlError) else type(error).__name__
        print("Colab OAuth setup failed: " + detail, file=sys.stderr)
        raise SystemExit(1) from None
