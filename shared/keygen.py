"""One-shot key generation utility.

Run once per environment, paste the output into `.env` (or your secret
store), and never run it again for that environment. Keys must persist
across restarts (RULES.md); regenerating them would invalidate every
in-flight packet and every already-settled signature.

Usage:
    python -m shared.keygen           # print env-ready lines
    python -m shared.keygen --pem     # print human-readable PEM blocks

The private keys printed here are secrets. Do not commit them, do not
paste them into a shared channel, and do not log them.
"""

from __future__ import annotations

import argparse
import sys

from shared.crypto import generate_rsa_keypair, generate_signing_keypair


def _escape_pem(pem: str) -> str:
    """Flatten PEM text into a single env-var-safe line with \\n escapes."""
    return pem.strip().replace("\n", "\\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shared.keygen",
        description="Generate MeshSettle signing and key-wrapping keypairs.",
    )
    parser.add_argument(
        "--pem",
        action="store_true",
        help="print raw PEM blocks instead of single-line env values",
    )
    args = parser.parse_args(argv)

    signing_private, signing_public = generate_signing_keypair()
    rsa_private, rsa_public = generate_rsa_keypair()

    pairs = [
        ("SENDER_SIGNING_PRIVATE_KEY_PEM", signing_private),
        ("SENDER_SIGNING_PUBLIC_KEY_PEM", signing_public),
        ("SETTLEMENT_RSA_PRIVATE_KEY_PEM", rsa_private),
        ("SETTLEMENT_RSA_PUBLIC_KEY_PEM", rsa_public),
    ]

    print("# MeshSettle generated keys — treat the PRIVATE values as secrets.")
    print("# Copy into .env (gitignored) or your secret manager. Generate once only.")
    print()
    for name, pem in pairs:
        if args.pem:
            print(f"# --- {name} ---")
            print(pem.strip())
            print()
        else:
            print(f'{name}="{_escape_pem(pem)}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
