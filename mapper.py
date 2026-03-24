#!/opt/actual-budget/venv/bin/python3

"""
mapper.py
---------
Converts raw bunq payment objects into actualpy transaction dicts.

Two types of transactions are handled:
  1. Regular transactions  → dict with type="transaction"
  2. Internal transfers    → dict with type="transfer" (between own bunq accounts)

Transfer detection:
  bunq sets counterparty_alias.iban to the IBAN of the destination account.
  If that IBAN matches one of our own accounts (from the iban_map), it's a transfer.

bunq/Actual amount convention:
  - Negative = money leaving the account (expense / transfer out)
  - Positive = money entering the account (income / transfer in)
  actualpy's create_transaction() and create_transfer() both accept decimal.Decimal.
"""

import decimal
import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


def bunq_payment_to_actual(
    payment: dict,
    actual_account_id: str,
    iban_to_account_id: dict = None,
) -> dict | None:
    """Convert a bunq payment dict to an actualpy-compatible transaction dict.

    Args:
        payment:             Raw payment dict from the bunq API
        actual_account_id:   UUID of the Actual account this payment belongs to
        iban_to_account_id:  Optional map of IBAN -> Actual account UUID for own accounts.
                             Used to detect internal transfers.

    Returns:
        Dict with type="transaction" or type="transfer", or None on error.
    """
    try:
        payment_id  = payment["id"]
        amount_raw  = payment["amount"]["value"]   # e.g. "-12.50" or "20.00"
        amount      = decimal.Decimal(amount_raw)

        # Parse the bunq timestamp to a Python date object
        # bunq format: "2024-03-15 14:23:01.123456"
        created_at = payment.get("created", "")
        try:
            tx_date = datetime.strptime(created_at[:10], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            tx_date = date.today()
            logger.warning(
                f"Could not parse date '{created_at}' for payment {payment_id} — using today."
            )

        # Extract counterparty info
        counter      = payment.get("counterparty_alias", {})
        counter_iban = counter.get("iban")
        payee_name   = (counter.get("display_name") or counter.get("name") or "Unknown").strip()
        description  = payment.get("description", "").strip()

        # Build notes string with optional description + bunq ID for traceability
        notes_parts = []
        if description:
            notes_parts.append(description)
        notes_parts.append(f"bunq ID: {payment_id}")
        notes = " | ".join(notes_parts)

        imported_id = f"bunq-{payment_id}"

        # --- Transfer detection ---
        # If the counterparty IBAN belongs to one of our own accounts, this is
        # an internal transfer between two of our bunq accounts.
        if iban_to_account_id and counter_iban and counter_iban in iban_to_account_id:
            dest_account_id = iban_to_account_id[counter_iban]

            # Skip the incoming side — create_transfer() creates both sides automatically.
            # We only process the outgoing side (negative amount) to avoid duplicates.
            if amount > 0:
                logger.debug(
                    f"Skipping incoming transfer side: {imported_id} "
                    f"(outgoing side on account {dest_account_id} will create both)"
                )
                return None

            logger.debug(
                f"Transfer detected: {imported_id} | {actual_account_id} -> {dest_account_id} "
                f"| amount: {amount}"
            )
            return {
                "type":             "transfer",
                "imported_id":      imported_id,
                "date":             tx_date,
                "amount":           abs(amount),        # create_transfer() expects positive amount
                "source_account_id": actual_account_id, # money leaves this account
                "dest_account_id":  dest_account_id,    # money arrives here
                "notes":            notes,
                "cleared":          True,
            }

        # --- Regular transaction ---
        return {
            "type":        "transaction",
            "imported_id": imported_id,
            "date":        tx_date,
            "amount":      amount,                      # Decimal, negative = outgoing
            "account_id":  actual_account_id,
            "payee_name":  payee_name,
            "notes":       notes,
            "cleared":     True,
        }

    except (KeyError, ValueError, decimal.InvalidOperation) as e:
        logger.warning(f"Could not map payment {payment.get('id', '?')}: {e}")
        return None
