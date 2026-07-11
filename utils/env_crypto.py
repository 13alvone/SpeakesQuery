#!/usr/bin/env python3
"""Encrypt or decrypt .env files using Fernet symmetric encryption."""

import argparse
import logging
import os
import sys
from cryptography.fernet import Fernet

logging.basicConfig(level=logging.INFO, format="%(message)s")


def _get_key() -> bytes:
    """Return the master key from the ``MASTER_KEY`` environment variable."""
    key = os.environ.get("MASTER_KEY")
    if not key:
        logging.error("[x] MASTER_KEY environment variable not set")
        sys.exit(1)
    return key.encode()


def _ensure_secure_permissions(path: str) -> None:
    """Raise ``PermissionError`` if *path* is not mode 0600."""
    mode = os.stat(path).st_mode & 0o777
    if mode != 0o600:
        logging.error("[x] %s permissions must be 0600, found %o", path, mode)
        raise PermissionError(f"insecure permissions {oct(mode)} on {path}")


def encrypt_env(src: str, dst: str) -> None:
    """Encrypt *src* and write to *dst* using ``Fernet``."""
    fernet = Fernet(_get_key())
    with open(src, "rb") as f_in:
        data = f_in.read()
    encrypted = fernet.encrypt(data)
    with open(dst, "wb") as f_out:
        f_out.write(encrypted)
    os.chmod(dst, 0o600)
    logging.info("[i] Wrote encrypted env to %s", dst)


def decrypt_env(src: str) -> str:
    """Return the decrypted contents of *src*."""
    _ensure_secure_permissions(src)
    fernet = Fernet(_get_key())
    with open(src, "rb") as f_in:
        data = f_in.read()
    decrypted = fernet.decrypt(data)
    return decrypted.decode()


def main() -> None:
    parser = argparse.ArgumentParser(description="Encrypt or decrypt .env files")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_enc = sub.add_parser("encrypt", help="Encrypt a plaintext .env")
    p_enc.add_argument("src")
    p_enc.add_argument("dst")
    p_dec = sub.add_parser("decrypt", help="Decrypt an env file")
    p_dec.add_argument("src")
    args = parser.parse_args()

    if args.cmd == "encrypt":
        encrypt_env(args.src, args.dst)
    else:
        print(decrypt_env(args.src))


if __name__ == "__main__":
    main()
