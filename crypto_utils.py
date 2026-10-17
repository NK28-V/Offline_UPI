"""
Cryptographic Utilities for Offline UPI Payment System
Handles RSA-2048 key generation, serialization, canonical JSON encoding,
digital signature creation/verification using RSA-PSS and SHA-256,
and passphrase-derived authenticated encryption (PBKDF2-HMAC-SHA256 + AES-256-GCM)
for secure local private-key storage.
"""

import os
import json
import base64
import math
import time
from typing import Tuple, Dict, Any, Optional
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.exceptions import InvalidSignature


def validate_token_payload(payload: Any, max_future_skew_seconds: float = 5.0) -> Tuple[bool, Optional[str]]:
    """
    Strictly validates token payload structure, types, amount bounds, and timestamps.
    Returns (True, None) if valid, or (False, error_reason) if invalid.
    """
    if not isinstance(payload, dict):
        return False, "Payload must be a dictionary/JSON object."

    required_fields = ["tokenID", "payerID", "payeeID", "amount", "timestamp"]
    for field in required_fields:
        if field not in payload:
            return False, f"Missing required payload attribute: '{field}'"

    # Validate string identifier types
    for str_field in ["tokenID", "payerID", "payeeID"]:
        val = payload[str_field]
        if not isinstance(val, str) or not val.strip():
            return False, f"Attribute '{str_field}' must be a non-empty string."

    # Validate amount: positive, finite, not bool, not NaN, not infinity
    amount = payload["amount"]
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return False, f"Amount must be a numeric value, got: {type(amount).__name__}"
    if not math.isfinite(amount):
        return False, f"Amount must be a finite number, got: {amount}"
    if amount <= 0:
        return False, f"Amount must be strictly positive, got: {amount}"

    # Validate timestamp: numeric, finite, not future
    timestamp = payload["timestamp"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return False, f"Timestamp must be numeric, got: {type(timestamp).__name__}"
    if not math.isfinite(timestamp) or timestamp <= 0:
        return False, f"Timestamp must be a positive finite epoch, got: {timestamp}"

    current_time = time.time()
    if timestamp > (current_time + max_future_skew_seconds):
        return False, f"Future timestamp detected: token timestamp ({timestamp}) exceeds current time ({current_time}) by more than {max_future_skew_seconds}s."

    return True, None


def validate_bank_receipt_payload(receipt: Any) -> Tuple[bool, Optional[str]]:
    """
    Validates a bank-issued settlement receipt payload structure.
    Returns (True, None) if valid, or (False, error_reason) if invalid.
    """
    if not isinstance(receipt, dict):
        return False, "Receipt must be a dictionary."

    required_fields = ["receiptID", "tokenID", "payerID", "payeeID", "amount", "status", "settled_at"]
    for field in required_fields:
        if field not in receipt:
            return False, f"Missing required receipt field: '{field}'"

    for str_field in ["receiptID", "tokenID", "payerID", "payeeID", "status"]:
        val = receipt[str_field]
        if not isinstance(val, str) or not val.strip():
            return False, f"Receipt field '{str_field}' must be a non-empty string."

    amt = receipt["amount"]
    if isinstance(amt, bool) or not isinstance(amt, (int, float)) or not math.isfinite(amt) or amt <= 0:
        return False, f"Receipt amount must be a positive finite number, got: {amt}"

    settled_at = receipt["settled_at"]
    if isinstance(settled_at, bool) or not isinstance(settled_at, (int, float)) or not math.isfinite(settled_at) or settled_at <= 0:
        return False, f"Receipt settled_at timestamp must be a positive finite epoch, got: {settled_at}"

    return True, None


def validate_rsa_public_key_pem(pem_str: str, min_key_size: int = 2048) -> Tuple[bool, Optional[str]]:
    """
    Validates that a PEM string contains a valid SubjectPublicKeyInfo RSA public key
    with a key size of at least min_key_size bits.
    """
    if not isinstance(pem_str, str) or not pem_str.strip():
        return False, "Public key must be a non-empty PEM string."

    try:
        key = serialization.load_pem_public_key(pem_str.encode('utf-8'))
        if not isinstance(key, rsa.RSAPublicKey):
            return False, f"Key must be an RSA public key, got: {type(key).__name__}"
        if key.key_size < min_key_size:
            return False, f"RSA key size must be at least {min_key_size} bits, got: {key.key_size}"
        return True, None
    except Exception as e:
        return False, f"Malformed public key PEM: {str(e)}"


def generate_rsa_key_pair() -> Tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    """Generates a 2048-bit RSA key pair with public exponent 65537."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )
    public_key = private_key.public_key()
    return private_key, public_key


def private_key_to_pem(private_key: rsa.RSAPrivateKey) -> str:
    """Serializes RSA private key to unencrypted PEM format string."""
    pem_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    return pem_bytes.decode('utf-8')


def public_key_to_pem(public_key: rsa.RSAPublicKey) -> str:
    """Serializes RSA public key to SubjectPublicKeyInfo PEM format string."""
    pem_bytes = public_key.bytes_to_pem if hasattr(public_key, 'bytes_to_pem') else public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pem_bytes.decode('utf-8')


def load_public_key_from_pem(pem_str: str) -> rsa.RSAPublicKey:
    """Loads an RSA public key object from a PEM string."""
    return serialization.load_pem_public_key(pem_str.encode('utf-8'))


# -----------------------------------------------------------------------------
# Passphrase-Derived Authenticated Key Storage (PBKDF2-HMAC-SHA256 + AES-256-GCM)
# -----------------------------------------------------------------------------

def derive_aes_key(passphrase: str, salt: bytes, iterations: int = 100_000) -> bytes:
    """Derives a 256-bit symmetric encryption key from a user passphrase using PBKDF2-HMAC-SHA256."""
    if not passphrase:
        raise ValueError("Passphrase must be a non-empty string.")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations
    )
    return kdf.derive(passphrase.encode('utf-8'))


def encrypt_private_key_pem(private_key_pem: str, passphrase: str) -> Dict[str, Any]:
    """
    Encrypts a private key PEM string using a key derived from passphrase via PBKDF2
    and authenticated encryption via AES-256-GCM.
    Returns a secure dictionary container (salt, nonce, ciphertext).
    """
    if not private_key_pem or not private_key_pem.strip():
        raise ValueError("Private key PEM must not be empty.")
    salt = os.urandom(16)
    nonce = os.urandom(12)
    derived_key = derive_aes_key(passphrase, salt, iterations=100_000)
    aesgcm = AESGCM(derived_key)
    ciphertext = aesgcm.encrypt(nonce, private_key_pem.encode('utf-8'), associated_data=None)

    return {
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": 100_000,
        "cipher": "AES-256-GCM",
        "salt_b64": base64.b64encode(salt).decode('utf-8'),
        "nonce_b64": base64.b64encode(nonce).decode('utf-8'),
        "ciphertext_b64": base64.b64encode(ciphertext).decode('utf-8')
    }


def decrypt_private_key_pem(encrypted_container: Dict[str, Any], passphrase: str) -> str:
    """
    Decrypts a private key PEM from an authenticated container using the supplied passphrase.
    Raises ValueError if the passphrase is wrong or the ciphertext has been altered.
    """
    if not isinstance(encrypted_container, dict):
        raise ValueError("Encrypted container must be a dictionary.")

    for k in ["salt_b64", "nonce_b64", "ciphertext_b64"]:
        if k not in encrypted_container:
            raise ValueError(f"Malformed encrypted container: missing '{k}'")

    salt = base64.b64decode(encrypted_container["salt_b64"])
    nonce = base64.b64decode(encrypted_container["nonce_b64"])
    ciphertext = base64.b64decode(encrypted_container["ciphertext_b64"])
    iterations = int(encrypted_container.get("iterations", 100_000))

    derived_key = derive_aes_key(passphrase, salt, iterations=iterations)
    aesgcm = AESGCM(derived_key)
    try:
        pem_bytes = aesgcm.decrypt(nonce, ciphertext, associated_data=None)
        return pem_bytes.decode('utf-8')
    except Exception:
        raise ValueError("Failed to decrypt private key: incorrect passphrase or corrupted key data.")


# -----------------------------------------------------------------------------
# Canonical JSON Encoding & Digital Signatures
# -----------------------------------------------------------------------------

def canonical_json_bytes(data: Dict[str, Any]) -> bytes:
    """
    Serializes a dictionary into canonical bytes (sorted keys, no extraneous whitespace).
    Crucial for tamper-evidence and consistent cryptographic hashing.
    """
    return json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sign_payload(payload: Dict[str, Any], private_key: rsa.RSAPrivateKey) -> str:
    """
    Signs a dictionary payload using RSA-PSS padding and SHA-256 digest.
    Returns base64 encoded signature string.
    """
    message_bytes = canonical_json_bytes(payload)
    signature = private_key.sign(
        message_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode('utf-8')


def verify_signature(payload: Dict[str, Any], signature_b64: str, public_key: rsa.RSAPublicKey) -> bool:
    """
    Verifies the RSA-PSS signature of a payload.
    Returns True if valid, False if signature or payload has been tampered with.
    """
    try:
        signature_bytes = base64.b64decode(signature_b64)
        message_bytes = canonical_json_bytes(payload)
        public_key.verify(
            signature_bytes,
            message_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        return True
    except (InvalidSignature, Exception):
        return False
