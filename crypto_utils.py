"""
Cryptographic Utilities for Offline UPI Payment System
Handles RSA-2048 key generation, serialization, canonical JSON encoding,
and digital signature creation/verification using RSA-PSS and SHA-256.
"""

import json
import base64
import math
import time
from typing import Tuple, Dict, Any, Optional
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
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
    pem_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pem_bytes.decode('utf-8')


def load_public_key_from_pem(pem_str: str) -> rsa.RSAPublicKey:
    """Loads an RSA public key object from a PEM string."""
    return serialization.load_pem_public_key(pem_str.encode('utf-8'))


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
