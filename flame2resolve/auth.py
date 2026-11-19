"""Password verifier format for self-hosted instances."""

import hashlib
import secrets

ROUNDS = 600_000


def make_verifier(password):
    salt = secrets.token_bytes(32)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ROUNDS)
    return f"{salt.hex()}:{digest.hex()}\n"


def parse_verifier(value):
    try:
        salt, digest = (bytes.fromhex(piece) for piece in value.strip().split(":"))
        if len(salt) == len(digest) == 32:
            return salt, digest
    except ValueError:
        pass
    raise ValueError("Invalid password verifier. Create one with init-auth.")
