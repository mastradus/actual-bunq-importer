"""
actual_client.py
----------------
Handles all communication with the Actual Budget server via actualpy.

Key concepts:
  - actualpy downloads a local SQLite copy of the budget database
  - Changes are written locally, then actual.commit() syncs back to the server
  - financial_id (= "bunq-{payment_id}") is the DB column used for deduplication
    (actualpy's create_transaction() parameter name is 'imported_id', maps to financial_id)
"""

import logging
from typing import Optional

from actual import Actual
from actual.queries import (
    create_account,
    create_transaction,
    create_transfer,
    get_account,
    get_accounts,
    get_payee,
    create_payee,
)

logger = logging.getLogger(__name__)


class ActualClient:
    """Thin wrapper around actualpy for the bunq sync workflow."""

    def __init__(self, base_url: str, password: str, budget_name: str,
                 cert=False, encryption_password: Optional[str] = None,
                 data_dir: Optional[str] = None):
        """
        Args:
            base_url:             URL of the Actual server, e.g. 'https://fin.ma-infra.de'
            password:             Actual server password
            budget_name:          Name of the budget file (as shown top-left in the UI)
            cert:                 Path to CA cert, or False to disable TLS verification
            encryption_password:  Optional end-to-end encryption password
            data_dir:             Directory to cache the local budget copy (tmp dir if None)
        """
        self.base_url = base_url
        self.password = password
        self.budget_name = budget_name
        self.cert = cert
        self.encryption_password = encryption_password
        self.data_dir = data_dir

    def _open(self) -> Actual:
        """Open and return an Actual context manager instance."""
        return Actual(
            base_url=self.base_url,
            password=self.password,
            file=self.budget_name,
            cert=False,  # self-signed cert on fin.ma-infra.de
            encryption_password=self.encryption_password,
            data_dir=self.data_dir,
        )

    # -----------------------------------------------------------------------
    # Account management
    # -----------------------------------------------------------------------

    def list_accounts(self) -> list[dict]:
        """Return all open accounts in the budget."""
        with self._open() as actual:
            accounts = get_accounts(actual.session)
            return [
                {
                    "id": str(a.id),
                    "name": a.name,
                    "offbudget": bool(a.offbudget),
                    "closed": bool(a.closed),
                }
                for a in accounts
                if not a.closed
            ]

    def find_account_by_name(self, name: str) -> Optional[str]:
        """Look up an account UUID by its exact name."""
        with self._open() as actual:
            account = get_account(actual.session, name)
            return str(account.id) if account else None

    def create_account(self, name: str, off_budget: bool = False,
                       iban: Optional[str] = None) -> Optional[str]:
        """Create a new account in Actual Budget and return its UUID.

        actualpy's create_account() signature:
            (s, name, initial_balance=0, offbudget=False)

        Args:
            name:       Display name for the account (typically the bunq description)
            off_budget: True = tracking only (off-budget), False = on-budget (default)
            iban:       Optional IBAN to store in the account's note field

        Returns:
            UUID string of the created account, or None on failure
        """
        with self._open() as actual:
            try:
                account = create_account(
                    actual.session,
                    name,
                    0,          # initial_balance: start at 0, transactions will fill it
                    off_budget, # offbudget flag
                )
                # Store IBAN in the Notes table if provided
                if iban:
                    _set_note(actual.session, str(account.id), iban)

                actual.commit()
                account_id = str(account.id)
                logger.debug(
                    f"Created account '{name}' "
                    f"({'off-budget' if off_budget else 'on-budget'}) "
                    f"ID={account_id}"
                    + (f" IBAN={iban}" if iban else "")
                )
                return account_id
            except Exception as e:
                logger.error(f"Failed to create account '{name}': {e}")
                return None

    def get_iban_map(self) -> dict:
        """Build a map of IBAN -> Actual account UUID from stored account notes.

        Handles two note formats that exist in the DB:
          - "IBAN: DE96370190001011282839"  (written by --init-accounts)
          - "DE96370190001010428851"         (bare IBAN, written by older version)

        Returns:
            Dict mapping IBAN string -> Actual account UUID string.
            Example: {"DE96370190001010428851": "ae5c5577-3fe3-...", ...}
        """
        import re
        from actual.database import Notes

        # Regex for a bare IBAN (2 letters + up to 32 alphanumeric chars)
        IBAN_RE = re.compile(r'^[A-Z]{2}[0-9A-Z]{10,30}$')

        iban_map = {}

        with self._open() as actual:
            notes = (
                actual.session.query(Notes)
                .filter(Notes.id.like("account-%"))
                .all()
            )
            for note_row in notes:
                account_uuid = note_row.id.removeprefix("account-")
                for line in (note_row.note or "").splitlines():
                    line = line.strip()
                    if line.startswith("IBAN:"):
                        # Format: "IBAN: DE96..."
                        iban = line.split(":", 1)[1].strip()
                        iban_map[iban] = account_uuid
                        break
                    elif IBAN_RE.match(line):
                        # Format: bare IBAN without prefix
                        iban_map[line] = account_uuid
                        break

        logger.info(f"IBAN map loaded: {len(iban_map)} accounts")
        logger.debug(f"IBAN map: {iban_map}")
        return iban_map

    def set_account_note(self, account_name: str, note: str) -> bool:
        """Set or update the note field for an existing account.

        Uses the Notes table (id = account UUID, note = markdown text).

        Args:
            account_name: Exact account name as shown in Actual Budget
            note:         Note text to store (supports Markdown)

        Returns:
            True on success, False if account not found or on error
        """
        with self._open() as actual:
            account = get_account(actual.session, account_name)
            if not account:
                logger.warning(f"set_account_note: account '{account_name}' not found")
                return False
            try:
                _set_note(actual.session, str(account.id), note)
                actual.commit()
                logger.debug(f"Note set for account '{account_name}': {note}")
                return True
            except Exception as e:
                logger.error(f"Failed to set note for '{account_name}': {e}")
                return False

    # -----------------------------------------------------------------------
    # Transaction import
    # -----------------------------------------------------------------------

    def import_transactions(self, transactions: list[dict]) -> dict:
        """Import a batch of transactions into Actual Budget.

        Handles two transaction types from mapper.py:
          - type="transaction": regular income/expense via create_transaction()
          - type="transfer":    internal transfer via create_transfer() which
                                automatically creates both sides (source + dest)

        Deduplication via financial_id column (= "bunq-{payment_id}").

        Returns:
            Summary dict: {'imported': int, 'skipped': int, 'errors': int}
        """
        if not transactions:
            return {"imported": 0, "skipped": 0, "errors": 0}

        imported = 0
        skipped  = 0
        errors   = 0

        with self._open() as actual:
            for tx in transactions:
                try:
                    imported_id = tx["imported_id"]

                    # Duplicate check via financial_id column
                    if _transaction_exists(actual.session, imported_id):
                        logger.debug(f"Duplicate skipped: {imported_id}")
                        skipped += 1
                        continue

                    if tx.get("type") == "transfer":
                        # --- Internal transfer between two own accounts ---
                        source = _get_account_by_id(actual.session, tx["source_account_id"])
                        dest   = _get_account_by_id(actual.session, tx["dest_account_id"])

                        if not source or not dest:
                            logger.warning(
                                f"Transfer {imported_id}: account not found "
                                f"(source={tx['source_account_id']}, dest={tx['dest_account_id']})"
                            )
                            errors += 1
                            continue

                        # create_transfer() returns (source_tx, dest_tx) — set
                        # financial_id directly on the source side for deduplication
                        source_tx, dest_tx = create_transfer(
                            actual.session,
                            tx["date"],
                            source,
                            dest,
                            tx["amount"],           # positive Decimal
                            tx.get("notes", ""),
                        )
                        source_tx.financial_id = imported_id

                        imported += 1
                        logger.debug(
                            f"Transfer queued: {imported_id} | "
                            f"{tx['source_account_id']} -> {tx['dest_account_id']} | "
                            f"{tx['amount']}"
                        )

                    else:
                        # --- Regular transaction (income or expense) ---
                        account = _get_account_by_id(actual.session, tx["account_id"])
                        if not account:
                            logger.warning(
                                f"Account {tx['account_id']} not found — skipping {imported_id}"
                            )
                            errors += 1
                            continue

                        payee = _get_or_create_payee(actual.session, tx["payee_name"])

                        # Positional call — first param is 's' (not 'session')
                        create_transaction(
                            actual.session,       # s
                            tx["date"],           # date
                            account,              # account
                            payee,                # payee
                            tx.get("notes", ""),  # notes
                            None,                 # category (Actual rules handle assignment)
                            tx["amount"],         # amount: Decimal, negative = outgoing
                            imported_id,          # imported_id -> stored as financial_id in DB
                            True,                 # cleared: bunq transactions are always settled
                        )
                        imported += 1
                        logger.debug(
                            f"Queued: {imported_id} | {tx['payee_name']} | {tx['amount']}"
                        )

                except Exception as e:
                    logger.error(
                        f"Error importing transaction {tx.get('imported_id')}: {e}"
                    )
                    errors += 1

            # Sync all queued changes to the Actual server in one batch
            if imported > 0:
                actual.commit()
                logger.info(f"Committed {imported} transaction(s) to Actual server.")

        return {"imported": imported, "skipped": skipped, "errors": errors}

    def set_opening_balance(
        self,
        actual_account_id: str,
        current_balance: str,
        since_date: str,
    ) -> bool:
        """Create an opening balance transaction only if Actual balance differs from bunq.

        After importing all transactions, compare:
            diff = bunq_balance_today - sum(all transactions in Actual for this account)

        If diff == 0: balances already match, nothing to do.
        If diff != 0: insert a single "Opening Balance" transaction for the difference,
                      dated one day before since_date.

        Args:
            actual_account_id: UUID of the Actual account
            current_balance:   Current balance string from bunq API (e.g. "44.87")
            since_date:        The --since date string (YYYY-MM-DD)

        Returns:
            True on success, False on error
        """
        import decimal
        from datetime import datetime, timedelta
        from sqlalchemy import text as _text

        ZERO = decimal.Decimal("0")

        try:
            since_dt    = datetime.strptime(since_date, "%Y-%m-%d").date()
            balance_dt  = since_dt - timedelta(days=1)
            imported_id = f"opening-balance-{actual_account_id}-{since_date}"

            with self._open() as actual:
                # Remove any previous opening balance so it doesn't skew our sum
                actual.session.execute(_text(
                    "DELETE FROM transactions WHERE financial_id = :fid"
                ), {"fid": imported_id})

                # Sum all transactions in Actual for this account (after import)
                row = actual.session.execute(_text("""
                    SELECT COALESCE(SUM(amount), 0)
                    FROM transactions
                    WHERE acct      = :acct
                      AND tombstone = 0
                """), {"acct": actual_account_id}).fetchone()

                actual_sum_cents = row[0] if row else 0
                actual_balance   = decimal.Decimal(actual_sum_cents) / 100
                bunq_balance     = decimal.Decimal(current_balance)
                diff             = bunq_balance - actual_balance

                logger.debug(
                    f"Balance check: bunq={bunq_balance} actual={actual_balance} "
                    f"diff={diff} account={actual_account_id}"
                )

                # If balances already match — nothing to do
                if abs(diff) < decimal.Decimal("0.01"):
                    logger.info(
                        f"Balances match for {actual_account_id} ({bunq_balance} EUR) — "
                        f"no opening balance needed."
                    )
                    actual.commit()
                    return True

                # Insert the difference as opening balance
                account = _get_account_by_id(actual.session, actual_account_id)
                if not account:
                    logger.warning(f"set_opening_balance: account {actual_account_id} not found")
                    return False

                payee = _get_or_create_payee(actual.session, "Starting Balance")

                create_transaction(
                    actual.session,
                    balance_dt,
                    account,
                    payee,
                    f"Opening balance for import since {since_date}",
                    None,
                    diff,
                    imported_id,
                    True,
                )
                actual.commit()

            logger.info(
                f"Opening balance set: {diff:.2f} EUR on {balance_dt} "
                f"for account {actual_account_id}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to set opening balance for {actual_account_id}: {e}")
            return False

    # -----------------------------------------------------------------------
    # Maintenance
    # -----------------------------------------------------------------------

    def clear_all_transactions(self) -> dict:
        """NOT WORKING AT THE MOMENT!
        Hard-delete all transactions from all accounts directly via SQL.

        Physically removes all rows from the 'transactions' table in the local
        SQLite budget file, then syncs the deletion to the Actual server via
        actual.commit().

        This is a permanent operation — rows are gone from the DB, not just
        soft-deleted (tombstone). Run --full afterwards to re-import from bunq.

        Returns:
            Summary dict: {'deleted': int, 'accounts_affected': int}
        """
        from actual.database import Transactions

        with self._open() as actual:
            # Count affected accounts before deletion for the summary
            from sqlalchemy import text as _text
            affected_accounts = actual.session.execute(
                _text("SELECT COUNT(DISTINCT acct) FROM transactions")
            ).scalar() or 0

            # Count rows to be deleted
            total = actual.session.execute(
                _text("SELECT COUNT(id) FROM transactions")
            ).scalar() or 0

            if total == 0:
                logger.info("No transactions found to delete.")
                return {"deleted": 0, "accounts_affected": 0}

            # Hard-delete: physically remove all rows from the transactions table
            actual.session.execute(_text("DELETE FROM transactions"));

            # Sync the cleared state back to the Actual server
            actual.commit()

        logger.info(f"Hard-deleted {total} transaction(s) across {affected_accounts} account(s).")
        return {"deleted": total, "accounts_affected": affected_accounts}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_account_by_id(session, account_id: str):
    """Fetch an Accounts ORM object by its UUID string."""
    from actual.database import Accounts
    return session.query(Accounts).filter(Accounts.id == account_id).first()


def _set_note(session, account_id: str, note: str):
    """Insert or update a note for an account in the Notes table.

    Actual Budget uses 'account-{uuid}' as the Notes primary key for account notes
    (discovered by inspecting what the UI writes — plain UUID does not show in the UI).
    We upsert: update if the key exists, insert if not.

    Args:
        session:    SQLAlchemy session
        account_id: Account UUID string (with or without 'account-' prefix)
        note:       Note text (Markdown supported)
    """
    from actual.database import Notes

    # Ensure correct key format — Actual expects 'account-{uuid}' for account notes
    note_key = account_id if account_id.startswith("account-") else f"account-{account_id}"

    existing = session.get(Notes, note_key)
    if existing:
        existing.note = note  # Update existing entry
    else:
        session.add(Notes(id=note_key, note=note))  # Insert new entry


def _transaction_exists(session, imported_id: str) -> bool:
    """Check for duplicate via financial_id column (actualpy stores imported_id there)."""
    from actual.database import Transactions
    return (
        session.query(Transactions)
        .filter(Transactions.financial_id == imported_id)
        .first()
    ) is not None


def _get_or_create_payee(session, payee_name: str):
    """Return existing payee ORM object or create a new one."""
    existing = get_payee(session, payee_name)
    if existing:
        return existing
    return create_payee(session, payee_name)


def _set_financial_id_on_transfer(session, imported_id: str, tx_date, amount):
    """Set financial_id on the source side of a just-created transfer for deduplication.

    create_transfer() does not accept an imported_id parameter, so we manually
    set financial_id on the most recently created outgoing transfer transaction.
    Matched by: date + negative amount (source side sends money out).

    Args:
        session:     SQLAlchemy session
        imported_id: The bunq-{id} string to store as financial_id
        tx_date:     Date of the transfer (datetime.date)
        amount:      Positive Decimal — source side is stored as negative, so we negate
    """
    import decimal
    from actual.database import Transactions

    negative_amount = -abs(decimal.Decimal(str(amount)))
    # Amount in DB is stored as integer cents (* 100)
    amount_cents = int(negative_amount * 100)

    tx = (
        session.query(Transactions)
        .filter(
            Transactions.date       == tx_date.strftime("%Y-%m-%d"),
            Transactions.amount     == amount_cents,
            Transactions.financial_id.is_(None),   # Not yet tagged
            Transactions.tombstone  == 0,
        )
        .order_by(Transactions.id.desc())           # Most recently created first
        .first()
    )
    if tx:
        tx.financial_id = imported_id
        logger.debug(f"Set financial_id={imported_id} on transfer tx {tx.id}")
    else:
        logger.warning(
            f"Could not find source side of transfer {imported_id} "
            f"(date={tx_date}, amount_cents={amount_cents}) — duplicate check may miss it"
        )
