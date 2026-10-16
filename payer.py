"""
Payer Device Module for Offline UPI Payment System
Generates cryptographically signed offline payment tokens (IOUs),
tracks cumulative unsynced spend, enforces the ₹500 offline limit cap,
supports payee binding, state persistence across restarts, and cancellation of failed transfers.
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
    sign_payload
)
from cryptography.hazmat.primitives import serialization


class OfflineSpendLimitExceededError(Exception):
    """Raised when an offline token request would exceed the maximum unsynced balance limit."""
    pass


class PayerDevice:
    """
    Simulates a consumer smartphone operating in an offline/low-connectivity environment.
    Maintains a secure RSA key pair, persistent local offline balance tracker,
    and a record of outstanding (unsettled) tokens.
    """

    def __init__(self, payer_id: str, offline_cap: float = 500.0, state_file: Optional[str] = None):
        self.payer_id = payer_id
        self.offline_cap = round(float(offline_cap), 2)
        self.unsynced_spent: float = 0.0
        self.outstanding_tokens: Dict[str, Dict[str, Any]] = {}

        # Configure state persistence file
        if state_file:
            self.state_file = state_file
        else:
            safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', payer_id)
            self.state_file = f"payer_state_{safe_id}.json"

        # Load existing state if available, or generate a new key pair
        if self._load_state():
            pass
        else:
            self._private_key, self._public_key = generate_rsa_key_pair()
            self.public_key_pem = public_key_to_pem(self._public_key)
            self._save_state()

    def _save_state(self) -> None:
        """Persists payer keys, unsynced balance, and outstanding tokens to disk."""
        if not self.state_file:
            return
        state = {
            "payer_id": self.payer_id,
            "offline_cap": self.offline_cap,
            "unsynced_spent": round(self.unsynced_spent, 2),
            "private_key_pem": private_key_to_pem(self._private_key),
            "public_key_pem": self.public_key_pem,
            "outstanding_tokens": self.outstanding_tokens
        }
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    def _load_state(self) -> bool:
        """Loads persistent state if file exists and belongs to this payer."""
        if not self.state_file or not os.path.exists(self.state_file):
            return False
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("payer_id") != self.payer_id:
                return False

            self.unsynced_spent = round(float(state.get("unsynced_spent", 0.0)), 2)
            self.offline_cap = round(float(state.get("offline_cap", self.offline_cap)), 2)
            self.outstanding_tokens = state.get("outstanding_tokens", {})

            priv_pem = state.get("private_key_pem")
            pub_pem = state.get("public_key_pem")
            if priv_pem and pub_pem:
                self._private_key = serialization.load_pem_private_key(priv_pem.encode("utf-8"), password=None)
                self._public_key = serialization.load_pem_public_key(pub_pem.encode("utf-8"))
                self.public_key_pem = pub_pem
                return True
        except Exception:
            return False
        return False

    @property
    def remaining_offline_allowance(self) -> float:
        """Returns remaining offline spending allowance before the ₹500 cap is reached."""
        return round(max(0.0, self.offline_cap - self.unsynced_spent), 2)

    def generate_token(self, amount: float, payee_id: str) -> Dict[str, Any]:
        """
        Generates and signs an offline payment token with required payee binding.
        
        Args:
            amount: Transaction amount in INR (positive finite float/int).
            payee_id: Intended merchant ID (required string).
            
        Returns:
            Dict containing payload, cryptographic signature, and public key PEM.
            
        Raises:
            ValueError: If amount is non-numeric, non-positive, NaN, infinity, or payee_id is invalid.
            OfflineSpendLimitExceededError: If new spend exceeds the offline cap.
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

        amt_rounded = round(float(amount), 2)
        projected_spend = round(self.unsynced_spent + amt_rounded, 2)

        if projected_spend > self.offline_cap:
            raise OfflineSpendLimitExceededError(
                f"Offline limit exceeded for {self.payer_id}! "
                f"Attempted: ₹{amt_rounded:.2f}, Unsynced spent: ₹{self.unsynced_spent:.2f}, "
                f"Allowance remaining: ₹{self.remaining_offline_allowance:.2f}, Cap: ₹{self.offline_cap:.2f}"
            )

        token_id = str(uuid.uuid4())
        timestamp = time.time()

        payload = {
            "tokenID": token_id,
            "payerID": self.payer_id,
            "payeeID": payee_id.strip(),
            "amount": amt_rounded,
            "timestamp": timestamp,
        }

        # Sign canonical payload with payer's private key
        signature = sign_payload(payload, self._private_key)

        # Update local offline spend counter and record outstanding token
        self.unsynced_spent = projected_spend

        token_package = {
            "payload": payload,
            "signature": signature,
            "payer_public_key": self.public_key_pem
        }

        self.outstanding_tokens[token_id] = {
            "amount": amt_rounded,
            "payee_id": payee_id.strip(),
            "timestamp": timestamp,
            "status": "OUTSTANDING"
        }

        self._save_state()
        return token_package

    def cancel_failed_token(self, token_id: str) -> bool:
        """
        Cancels an outstanding token if an offline transfer failed or was rejected by the merchant,
        safely restoring the payer's offline spending allowance.
        """
        if token_id in self.outstanding_tokens:
            token_info = self.outstanding_tokens.pop(token_id)
            amt = token_info.get("amount", 0.0)
            self.unsynced_spent = round(max(0.0, self.unsynced_spent - amt), 2)
            self._save_state()
            return True
        return False

    def reconcile_settled_tokens(self, settled_token_ids: List[str]) -> float:
        """
        Reconciles tokens confirmed as settled by the backend.
        Deducts only valid, previously outstanding tokens and protects against double reconciliation.
        
        Returns:
            Total amount restored to offline allowance.
        """
        total_restored = 0.0
        for tid in settled_token_ids:
            if tid in self.outstanding_tokens:
                info = self.outstanding_tokens.pop(tid)
                total_restored += info.get("amount", 0.0)

        if total_restored > 0:
            self.unsynced_spent = round(max(0.0, self.unsynced_spent - total_restored), 2)
            self._save_state()
        return round(total_restored, 2)

    def reconcile_settled_amount(self, settled_amount: float) -> None:
        """
        Fallback reconciliation method by total settled amount.
        Protects against negative balance.
        """
        if settled_amount > 0 and math.isfinite(settled_amount):
            self.unsynced_spent = round(max(0.0, self.unsynced_spent - float(settled_amount)), 2)
            self._save_state()

    def reset_offline_balance(self) -> None:
        """Resets unsynced spend counter and clears outstanding tokens."""
        self.unsynced_spent = 0.0
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
