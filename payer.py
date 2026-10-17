"""
Payer Device Module for Offline UPI Payment System
Generates cryptographically signed offline payment tokens (IOUs),
tracks cumulative unsynced spend using exact integer paise accounting,
enforces the ₹500 offline limit cap (50,000 paise),
protects local private keys via PBKDF2-HMAC-SHA256 and AES-256-GCM authenticated encryption,
enforces an explicit token lifecycle (CREATED -> EXPORTED -> SETTLED),
prevents unsafe cancellations once a token has been exported,
and validates authoritative bank-signed receipts for secure offline allowance restoration.
"""

import os
import json
import math
import time
import uuid
import re
from typing import Dict, Any, Optional, List
from crypto_utils import (
    generate_rsa_key_pair,
    public_key_to_pem,
    private_key_to_pem,
    load_public_key_from_pem,
    sign_payload,
    verify_signature,
    encrypt_private_key_pem,
    decrypt_private_key_pem,
    validate_bank_receipt_payload
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class OfflineSpendLimitExceededError(Exception):
    """Raised when an offline token request would exceed the maximum unsynced balance limit."""
    pass


class PayerDevice:
    """
    Simulates a consumer smartphone operating in an offline/low-connectivity environment.
    Maintains a secure RSA key pair protected by passphrase-derived authenticated encryption,
    tracks local offline spend in integer paise, and manages explicit token lifecycle states:
      - CREATED: Minted on device, not yet exported/transferred. Cancellable.
      - EXPORTED: Exported via JSON/QR. Reservation strictly held until bank receipt is verified.
      - SETTLED: Authoritatively settled via bank-signed receipt.
      - CANCELLED: Safely restored before transmission.
    """

    def __init__(
        self,
        payer_id: str,
        offline_cap: float = 500.0,
        state_file: Optional[str] = None,
        passphrase: Optional[str] = None,
        bank_public_key_pem: Optional[str] = None
    ):
        self.payer_id = payer_id.strip()
        self.offline_cap_paise: int = int(round(float(offline_cap) * 100))
        self.unsynced_spent_paise: int = 0
        self.outstanding_tokens: Dict[str, Dict[str, Any]] = {}
        self.passphrase = passphrase or os.getenv("PAYER_PASSPHRASE", "demo-payer-passphrase")
        self.bank_public_key_pem = bank_public_key_pem

        # Configure state persistence file
        if state_file:
            self.state_file = state_file
        else:
            safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', self.payer_id)
            self.state_file = f"payer_state_{safe_id}.json"

        # Load existing state if available, or generate a new key pair
        if not self._load_state():
            self._private_key, self._public_key = generate_rsa_key_pair()
            self.public_key_pem = public_key_to_pem(self._public_key)
            self._save_state()

    @property
    def offline_cap(self) -> float:
        """Offline cap in INR (float for display/API compatibility)."""
        return round(self.offline_cap_paise / 100.0, 2)

    @offline_cap.setter
    def offline_cap(self, val: float) -> None:
        self.offline_cap_paise = int(round(float(val) * 100))

    @property
    def unsynced_spent(self) -> float:
        """Cumulative unsynced spent in INR (float for display/API compatibility)."""
        return round(self.unsynced_spent_paise / 100.0, 2)

    @unsynced_spent.setter
    def unsynced_spent(self, val: float) -> None:
        self.unsynced_spent_paise = int(round(float(val) * 100))

    @property
    def remaining_offline_allowance(self) -> float:
        """Returns remaining offline spending allowance before the ₹500 cap is reached (in INR)."""
        remaining_paise = max(0, self.offline_cap_paise - self.unsynced_spent_paise)
        return round(remaining_paise / 100.0, 2)

    def _save_state(self) -> None:
        """
        Persists payer keys and balance securely to disk.
        Private key PEM is ALWAYS encrypted using AES-256-GCM before writing to disk.
        Plaintext private keys are NEVER stored.
        """
        if not self.state_file:
            return

        priv_pem = private_key_to_pem(self._private_key)
        encrypted_container = encrypt_private_key_pem(priv_pem, self.passphrase)

        state = {
            "payer_id": self.payer_id,
            "offline_cap": self.offline_cap,
            "offline_cap_paise": self.offline_cap_paise,
            "unsynced_spent": self.unsynced_spent,
            "unsynced_spent_paise": self.unsynced_spent_paise,
            "encrypted_private_key": encrypted_container,
            "public_key_pem": self.public_key_pem,
            "bank_public_key_pem": self.bank_public_key_pem,
            "outstanding_tokens": self.outstanding_tokens
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _load_state(self) -> bool:
        """
        Loads persistent state if file exists and belongs to this payer.
        Decrypts private key using passphrase. Migrates unencrypted legacy keys if found.
        """
        if not self.state_file or not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("payer_id") != self.payer_id:
                return False

            if "offline_cap_paise" in state:
                self.offline_cap_paise = int(state["offline_cap_paise"])
            else:
                self.offline_cap_paise = int(round(float(state.get("offline_cap", 500.0)) * 100))

            if "unsynced_spent_paise" in state:
                self.unsynced_spent_paise = int(state["unsynced_spent_paise"])
            else:
                self.unsynced_spent_paise = int(round(float(state.get("unsynced_spent", 0.0)) * 100))

            self.outstanding_tokens = state.get("outstanding_tokens", {})
            self.bank_public_key_pem = state.get("bank_public_key_pem", self.bank_public_key_pem)

            # 1. Check for encrypted private key container (modern standard)
            encrypted_container = state.get("encrypted_private_key")
            if encrypted_container:
                priv_pem = decrypt_private_key_pem(encrypted_container, self.passphrase)
                pub_pem = state.get("public_key_pem")
                self._private_key = serialization.load_pem_private_key(priv_pem.encode("utf-8"), password=None)
                self._public_key = serialization.load_pem_public_key(pub_pem.encode("utf-8"))
                self.public_key_pem = pub_pem
                return True

            # 2. Check for legacy plaintext private key and automatically migrate it
            legacy_priv_pem = state.get("private_key_pem")
            legacy_pub_pem = state.get("public_key_pem")
            if legacy_priv_pem and legacy_pub_pem:
                self._private_key = serialization.load_pem_private_key(legacy_priv_pem.encode("utf-8"), password=None)
                self._public_key = serialization.load_pem_public_key(legacy_pub_pem.encode("utf-8"))
                self.public_key_pem = legacy_pub_pem
                # Migrate to encrypted format on disk immediately
                self._save_state()
                return True

        except Exception as e:
            if "Failed to decrypt" in str(e):
                raise
            return False
        return False

    def generate_token(self, amount: float, payee_id: str) -> Dict[str, Any]:
        """
        Generates and signs an offline payment token with required payee binding.
        Uses exact integer paise calculations to enforce the offline spending cap.
        Initializes token lifecycle status as 'CREATED'.
        """
        # Strict amount validation
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ValueError(f"Transaction amount must be a numeric value, got: {type(amount).__name__}")
        if not math.isfinite(amount):
            raise ValueError(f"Transaction amount must be finite, got: {amount}")
        if amount <= 0:
            raise ValueError(f"Transaction amount must be strictly positive, got: ₹{amount}")

        # Strict payee_id validation
        if not isinstance(payee_id, str) or not payee_id.strip():
            raise ValueError("Intended payee ID ('payee_id') is required and must be a non-empty string.")

        amt_paise = int(round(float(amount) * 100))
        if amt_paise <= 0:
            raise ValueError(f"Transaction amount must be at least ₹0.01 (1 paisa), got: ₹{amount}")

        projected_paise = self.unsynced_spent_paise + amt_paise

        if projected_paise > self.offline_cap_paise:
            attempted_inr = round(amt_paise / 100.0, 2)
            current_inr = self.unsynced_spent
            allowance_inr = self.remaining_offline_allowance
            cap_inr = self.offline_cap
            raise OfflineSpendLimitExceededError(
                f"Offline limit exceeded for {self.payer_id}! "
                f"Attempted: ₹{attempted_inr:.2f}, Unsynced spent: ₹{current_inr:.2f}, "
                f"Allowance remaining: ₹{allowance_inr:.2f}, Cap: ₹{cap_inr:.2f}"
            )

        token_id = str(uuid.uuid4())
        timestamp = time.time()
        amt_inr = round(amt_paise / 100.0, 2)

        payload = {
            "tokenID": token_id,
            "payerID": self.payer_id,
            "payeeID": payee_id.strip(),
            "amount": amt_inr,
            "timestamp": timestamp,
        }

        # Sign canonical payload with payer's private key
        signature = sign_payload(payload, self._private_key)

        # Update local offline spend counter in integer paise and record outstanding token
        self.unsynced_spent_paise = projected_paise

        token_package = {
            "payload": payload,
            "signature": signature,
            "payer_public_key": self.public_key_pem
        }

        self.outstanding_tokens[token_id] = {
            "amount": amt_inr,
            "amount_paise": amt_paise,
            "payee_id": payee_id.strip(),
            "timestamp": timestamp,
            "status": "CREATED"
        }

        self._save_state()
        return token_package

    def mark_token_exported(self, token_id: str) -> bool:
        """
        Marks a token as EXPORTED (via JSON file download, clipboard copy, or QR code display).
        Once exported, cancellation is disallowed to prevent unsafe local refunds of tokens in transit.
        """
        if token_id in self.outstanding_tokens:
            info = self.outstanding_tokens[token_id]
            if info.get("status") == "CREATED":
                info["status"] = "EXPORTED"
                info["exported_at"] = time.time()
                self._save_state()
                return True
        return False

    def cancel_failed_token(self, token_id: str) -> bool:
        """
        Cancels an outstanding token and restores the spending allowance ONLY IF
        the token is in the 'CREATED' state (never exported or transferred).
        Raises ValueError if the token has already been exported or transferred.
        """
        if token_id not in self.outstanding_tokens:
            return False

        token_info = self.outstanding_tokens[token_id]
        status = token_info.get("status", "CREATED")

        if status == "EXPORTED":
            raise ValueError(
                f"Cannot cancel token '{token_id}' because it has already been exported for transfer. "
                "To prevent double spending, offline spend reservations are held until an authoritative bank receipt is reconciled."
            )

        if status in ["SETTLED", "CANCELLED"]:
            return False

        # Safe cancellation: token was created but never exported
        amt_paise = token_info.get("amount_paise")
        if amt_paise is None:
            amt_paise = int(round(float(token_info.get("amount", 0.0)) * 100))

        token_info["status"] = "CANCELLED"
        token_info["cancelled_at"] = time.time()
        self.unsynced_spent_paise = max(0, self.unsynced_spent_paise - amt_paise)
        self._save_state()
        return True

    def reconcile_with_bank_receipt(
        self,
        receipt_package: Dict[str, Any],
        bank_public_key: Optional[rsa.RSAPublicKey] = None
    ) -> Dict[str, Any]:
        """
        Authoritatively reconciles a token using a verifiable Bank-Signed Receipt.
        Enforces:
        1. Receipt structure validation (receiptID, tokenID, payerID, payeeID, amount, status, settled_at).
        2. Cryptographic signature verification using the bank's registered public key.
        3. Payer identity binding (receipt['payerID'] == self.payer_id).
        4. Token existence in outstanding tokens.
        5. Amount match against on-device reservation.
        6. Replay protection (does not credit or deduct twice).
        7. Strict ₹500 cap ceiling enforcement.
        """
        if not isinstance(receipt_package, dict):
            raise ValueError("Receipt package must be a dictionary.")

        receipt = receipt_package.get("receipt")
        signature = receipt_package.get("signature")

        is_valid_struct, err = validate_bank_receipt_payload(receipt)
        if not is_valid_struct:
            raise ValueError(f"Invalid bank receipt payload: {err}")

        if not isinstance(signature, str) or not signature.strip():
            raise ValueError("Bank receipt is missing cryptographic signature.")

        # Determine Bank public key for verification
        pub_key = bank_public_key
        if not pub_key:
            bank_pem = receipt_package.get("bank_public_key") or self.bank_public_key_pem
            if not bank_pem:
                raise ValueError("Bank public key is required to verify settlement receipt.")
            pub_key = load_public_key_from_pem(bank_pem)

        if not verify_signature(receipt, signature, pub_key):
            raise ValueError("Bank receipt cryptographic signature verification FAILED: forged or tampered receipt.")

        token_id = receipt["tokenID"]
        if token_id not in self.outstanding_tokens:
            raise ValueError(f"Token '{token_id}' not found among payer's recorded tokens.")

        token_info = self.outstanding_tokens[token_id]

        if receipt["payerID"] != self.payer_id:
            raise ValueError(f"Receipt payer mismatch: expected '{self.payer_id}', got '{receipt['payerID']}'.")

        amt_paise = token_info.get("amount_paise")
        if amt_paise is None:
            amt_paise = int(round(float(token_info.get("amount", 0.0)) * 100))

        receipt_paise = int(round(float(receipt["amount"]) * 100))
        if amt_paise != receipt_paise:
            raise ValueError(f"Receipt amount mismatch: recorded ₹{amt_paise/100:.2f}, receipt claims ₹{receipt_paise/100:.2f}.")

        # Replay protection: if already settled or cancelled, do not deduct again
        current_status = token_info.get("status")
        if current_status == "SETTLED":
            return {
                "success": True,
                "already_reconciled": True,
                "restored_amount": 0.0,
                "token_id": token_id,
                "unsynced_spent": self.unsynced_spent,
                "remaining_allowance": self.remaining_offline_allowance
            }

        outcome_status = receipt["status"]
        token_info["status"] = outcome_status
        token_info["reconciled_at"] = time.time()
        token_info["receipt_id"] = receipt["receiptID"]

        # Restore allowance
        self.unsynced_spent_paise = max(0, self.unsynced_spent_paise - amt_paise)
        self._save_state()

        return {
            "success": True,
            "already_reconciled": False,
            "restored_amount": round(amt_paise / 100.0, 2),
            "token_id": token_id,
            "status": outcome_status,
            "unsynced_spent": self.unsynced_spent,
            "remaining_allowance": self.remaining_offline_allowance
        }

    def reconcile_settled_tokens(self, settled_token_ids: List[str]) -> float:
        """
        Legacy reconciliation helper by token IDs (retains backward compatibility for tests/scripts).
        Protects against double reconciliation.
        """
        restored_paise = 0
        for tid in settled_token_ids:
            if tid in self.outstanding_tokens:
                info = self.outstanding_tokens[tid]
                if info.get("status") != "SETTLED":
                    amt_paise = info.get("amount_paise")
                    if amt_paise is None:
                        amt_paise = int(round(float(info.get("amount", 0.0)) * 100))
                    restored_paise += amt_paise
                    info["status"] = "SETTLED"

        if restored_paise > 0:
            self.unsynced_spent_paise = max(0, self.unsynced_spent_paise - restored_paise)
            self._save_state()
        return round(restored_paise / 100.0, 2)

    def reconcile_settled_amount(self, settled_amount: float) -> None:
        """Fallback reconciliation method by total settled amount."""
        if settled_amount > 0 and math.isfinite(settled_amount):
            amt_paise = int(round(float(settled_amount) * 100))
            self.unsynced_spent_paise = max(0, self.unsynced_spent_paise - amt_paise)
            self._save_state()

    def reset_offline_balance(self) -> None:
        """Resets unsynced spend counter and clears outstanding tokens."""
        self.unsynced_spent_paise = 0
        self.outstanding_tokens.clear()
        self._save_state()

    def delete_state(self) -> None:
        """Deletes the state file from disk (useful for clean tests)."""
        if self.state_file and os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception:
                pass

    def __repr__(self) -> str:
        return (f"<PayerDevice ID={self.payer_id} UnsyncedSpent=₹{self.unsynced_spent:.2f} "
                f"Allowance=₹{self.remaining_offline_allowance:.2f} Outstanding={len(self.outstanding_tokens)}>")
