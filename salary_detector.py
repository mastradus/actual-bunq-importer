#!/opt/actual-budget/venv/bin/python3
"""
salary_detector.py
------------------
Detects internal bunq transfers that were triggered by a salary payment.

Logic:
  1. Scan all payments for external incoming payments with salary keywords
     (GEHALT, LOHN, SALARY, WAGE) in their description.
  2. For each salary payment found, collect all internal outgoing transfers
     that occurred within a configurable time window after the salary arrival.
  3. Return the IDs of those transfers so mapper.py can label them correctly
     as "income-auto-transfer" instead of "internal-transfer".

This runs per-account, per sync batch — so it only sees payments fetched
in the current run. For incremental syncs this is fine since bunq auto-
transfers always happen within minutes of the salary payment.
"""

import decimal
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Keywords that identify an incoming salary payment (case-insensitive match)
SALARY_KEYWORDS = ["GEHALT", "LOHN", "SALARY", "WAGE"]

# How long after a salary payment to consider internal transfers as auto-triggered
SALARY_TRANSFER_WINDOW = timedelta(hours=2)


def detect_salary_transfer_ids(
    payments: list[dict],
    iban_to_account_id: dict,
) -> set[int]:
    """Identify internal transfers that were auto-triggered by a salary payment.

    Args:
        payments:            List of raw bunq payment dicts (newest first from API).
        iban_to_account_id:  Map of IBAN -> Actual account UUID for own accounts.
                             Used to distinguish internal vs. external counterparties.

    Returns:
        Set of payment IDs that are salary-triggered internal transfers.
        Empty set if no salary payment was found in the batch.
    """
    own_ibans = set(iban_to_account_id or {})

    # ── Step 1: Find all salary payments in this batch ──────────────────────
    salary_timestamps = []

    for p in payments:
        try:
            amount = decimal.Decimal(p["amount"]["value"])
            counter_iban = p.get("counterparty_alias", {}).get("iban")
            description = p.get("description", "").upper()

            # Must be: incoming (positive), external (not our own IBAN), salary keyword
            is_incoming = amount > 0
            is_external = counter_iban not in own_ibans
            has_keyword = any(kw in description for kw in SALARY_KEYWORDS)

            if is_incoming and is_external and has_keyword:
                created = datetime.strptime(p["created"][:19], "%Y-%m-%d %H:%M:%S")
                salary_timestamps.append(created)
                logger.debug(
                    f"Salary payment detected: ID={p['id']} "
                    f"amount={amount} at {created} desc='{p.get('description', '')}'"
                )
        except (KeyError, ValueError, decimal.InvalidOperation) as e:
            logger.debug(f"Skipping payment {p.get('id', '?')} in salary scan: {e}")
            continue

    if not salary_timestamps:
        logger.debug("No salary payments found in batch — skipping auto-transfer detection.")
        return set()

    logger.info(f"Found {len(salary_timestamps)} salary payment(s) — scanning for auto-transfers.")

    # ── Step 2: Find internal outgoing transfers within the time window ──────
    auto_transfer_ids = set()

    for p in payments:
        try:
            amount = decimal.Decimal(p["amount"]["value"])
            counter_iban = p.get("counterparty_alias", {}).get("iban")

            # Must be: outgoing (negative), internal (own IBAN as counterparty)
            is_outgoing = amount < 0
            is_internal = counter_iban in own_ibans

            if not (is_outgoing and is_internal):
                continue

            created = datetime.strptime(p["created"][:19], "%Y-%m-%d %H:%M:%S")

            for salary_time in salary_timestamps:
                delta = created - salary_time
                if timedelta(0) <= delta <= SALARY_TRANSFER_WINDOW:
                    auto_transfer_ids.add(p["id"])
                    logger.debug(
                        f"Auto-transfer identified: ID={p['id']} "
                        f"dest_iban={counter_iban} "
                        f"{delta.seconds}s after salary"
                    )
                    break  # No need to check other salary timestamps

        except (KeyError, ValueError, decimal.InvalidOperation) as e:
            logger.debug(f"Skipping payment {p.get('id', '?')} in transfer scan: {e}")
            continue

    logger.info(
        f"Detected {len(auto_transfer_ids)} salary-triggered auto-transfer(s) "
        f"within {SALARY_TRANSFER_WINDOW} of salary payment."
    )
    return auto_transfer_ids
