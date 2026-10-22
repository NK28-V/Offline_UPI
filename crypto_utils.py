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

def normalize_canonical_data(data: Any) -> Any:
    """
    Recursively normalizes a data structure for deterministic canonical JSON encoding:
    - Excludes 'signature' key so signature is never part of the signed payload.
    - Preserves booleans without treating them as integers (bool is subclass of int in Python).
    - Normalizes finite float values with zero fractional part to int (e.g. 50.0 -> 50)
      to match JavaScript JSON.stringify behavior (ECMA-262 / RFC 8785).
    - Sorts dictionary keys recursively.
    """
    if isinstance(data, dict):
        return {
            k: normalize_canonical_data(v)
            for k, v in data.items()
            if k != "signature"
        }
    elif isinstance(data, list):
        return [normalize_canonical_data(item) for item in data]
    elif isinstance(data, bool):
        return data
    elif isinstance(data, float):
        if math.isfinite(data) and data.is_integer():
            return int(data)
        return data
    return data


def canonical_json_bytes(data: Dict[str, Any]) -> bytes:
    """
    Serializes a dictionary into deterministic canonical bytes:
    - Sorted keys
    - No extraneous whitespace
    - Normalizes numbers across Python and JavaScript runtimes
    - Excludes signature field consistently
    - UTF-8 encoded
    Crucial for tamper-evidence and consistent cryptographic hashing.
    """
    normalized = normalize_canonical_data(data)
    return json.dumps(normalized, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


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
    Handles standard and URL-safe Base64 encoding and surrounding whitespace.
    Returns True if valid, False if signature or payload has been tampered with.
    """
    try:
        if not isinstance(signature_b64, str) or not signature_b64.strip():
            return False
        sig_str = signature_b64.strip()
        padding_needed = len(sig_str) % 4
        if padding_needed:
            sig_str += '=' * (4 - padding_needed)

        try:
            signature_bytes = base64.b64decode(sig_str)
        except Exception:
            signature_bytes = base64.urlsafe_b64decode(sig_str)

        message_bytes = canonical_json_bytes(payload)
        pss_padding = padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        )
        try:
            public_key.verify(signature_bytes, message_bytes, pss_padding, hashes.SHA256())
            return True
        except InvalidSignature:
            # Fallback for legacy tokens signed prior to float normalization
            try:
                legacy_bytes = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
                public_key.verify(signature_bytes, legacy_bytes, pss_padding, hashes.SHA256())
                return True
            except Exception:
                return False
    except (InvalidSignature, Exception):
        return False


def clean_token_json_str(raw_str: str) -> str:
    """Strips leading/trailing whitespace and optional markdown code fences from JSON string."""
    s = raw_str.strip()
    if s.startswith("```"):
        lines = s.splitlines()
        # Remove opening fence (``` or ```json)
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # Remove closing fence (```)
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()
    return s


def parse_token_package(raw_input: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Safely parses and extracts a token package from a string or dict.
    Handles:
    - Harmless whitespace and markdown code fences
    - Outer wrapper dictionaries like {"token_package": ...}
    - Flattened tokens with top-level payload attributes + signature
    Returns (token_package, None) on success, or (None, error_message) on failure.
    """
    if isinstance(raw_input, str):
        cleaned = clean_token_json_str(raw_input)
        if not cleaned:
            return None, "Empty token input provided."
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            return None, f"Malformed JSON: {e.msg} (at line {e.lineno}, column {e.colno})"
    elif isinstance(raw_input, dict):
        parsed = raw_input
    else:
        return None, f"Expected JSON string or object, got {type(raw_input).__name__}."

    if not isinstance(parsed, dict):
        return None, "Parsed token JSON must be an object/dictionary."

    # Unwrap if nested under 'token_package'
    if "token_package" in parsed and isinstance(parsed["token_package"], dict):
        parsed = parsed["token_package"]

    # Check if payload and signature are present directly
    if "payload" in parsed and isinstance(parsed["payload"], dict) and "signature" in parsed:
        return parsed, None

    # Check if payload attributes are flattened at top level along with signature
    required_payload_fields = ["tokenID", "payerID", "payeeID", "amount", "timestamp"]
    if all(k in parsed for k in required_payload_fields) and "signature" in parsed:
        payload = {k: parsed[k] for k in required_payload_fields}
        token_pkg = {
            "payload": payload,
            "signature": parsed["signature"]
        }
        if "payer_public_key" in parsed:
            token_pkg["payer_public_key"] = parsed["payer_public_key"]
        return token_pkg, None

    return None, "Invalid token structure: missing required 'payload' and 'signature' fields."
