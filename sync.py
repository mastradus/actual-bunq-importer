#!/opt/actual-budget/venv/bin/python3
"""
sync.py
-------
Entry point for the bunq -> Actual Budget sync.
Designed to be called by a cron job (e.g. every hour).

Usage:
    python sync.py                              # normal incremental sync
    python sync.py --setup                      # one-time bunq installation + device registration
    python sync.py --init-accounts              # create all bunq accounts in Actual (on-budget)
    python sync.py --init-accounts --off-budget # create all bunq accounts as off-budget
    python sync.py --full                       # re-import all transactions (ignores state)
    python sync.py --list-accounts              # list all Actual accounts
    python sync.py --config /path/to/cfg        # use a custom config file

Cron example (every hour):
    0 * * * * /opt/actual-budget/venv/bin/python3 /opt/actual-budget/sync.py
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import bunq_client
from actual_client import ActualClient
from mapper import bunq_payment_to_actual
from state import SyncState
from salary_detector import detect_salary_transfer_ids
SCRIPT_DIR = Path(__file__).parent.resolve()

def setup_logging(log_file=None, verbose=False):
    """Configure logging to stdout and optionally to a log file."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(level=level, format=fmt, handlers=handlers)


logger = logging.getLogger(__name__)


def load_config(config_path):
    """Load and validate the JSON config file."""
    path = Path(config_path)
    if not path.is_absolute():
        path = SCRIPT_DIR / path

    if not path.exists():
        print(f"ERROR: Config file not found: {config_path}")
        print("Copy config/config.example.json to config/config.json and fill in your values.")
        sys.exit(1)
    with open(path) as f:
        config = json.load(f)
    api_key = config.get("bunq", {}).get("api_key", "")
    if not api_key or api_key == "YOUR_BUNQ_API_KEY_HERE":
        print("ERROR: bunq api_key is not configured in config.json.")
        sys.exit(1)
    actual_pw = config.get("actual", {}).get("password", "")
    if not actual_pw or actual_pw == "YOUR_ACTUAL_PASSWORD":
        print("ERROR: actual password is not configured in config.json.")
        sys.exit(1)
    return config


def save_config(config, config_path):
    """Persist updated config to disk."""
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    logger.debug(f"Config saved to {config_path}")


def _make_actual_client(config):
    """Instantiate ActualClient from config."""
    cfg = config["actual"]
    return ActualClient(
        base_url=cfg["url"],
        password=cfg["password"],
        budget_name=cfg["budget_name"],
        cert=cfg.get("cert"),
        encryption_password=cfg.get("encryption_password"),
        data_dir=cfg.get("data_dir"),
    )


def run_clear_transactions(config):
    """Delete all transactions from all Actual Budget accounts.

    Uses soft-deletion (tombstone=1) — same as the Actual UI.
    Asks for confirmation before proceeding to prevent accidental data loss.
    After clearing, run --full to re-import everything from bunq.
    """
    print("\n⚠️  WARNING: This will delete ALL transactions from ALL accounts in Actual Budget.")
    print("   The sync state will also be reset so --full will re-import everything.\n")
    confirm = input("Type 'yes' to confirm: ").strip().lower()
    if confirm != "yes":
        print("Aborted.")
        return

    client = _make_actual_client(config)
    result = client.clear_all_transactions()
    print(f"\n✓ Deleted {result['deleted']} transaction(s) "
          f"across {result['accounts_affected']} account(s).")
    print("  Run './sync.py --full' to re-import all transactions from bunq.\n")


def run_setup(config, config_path):
    """One-time bunq Installation + Device registration."""
    logger.info("=== One-time bunq setup ===")
    config = bunq_client.setup_installation(config)
    save_config(config, config_path)
    logger.info("Step 1/2: Installation registered.")
    config = bunq_client.setup_device(config)
    save_config(config, config_path)
    logger.info("Step 2/2: Device registered.")
    logger.info("Setup complete! Run: ./sync.py --init-accounts")


def run_list_accounts(config):
    """Print all Actual Budget accounts."""
    client = _make_actual_client(config)
    print("\nActual Budget accounts:")
    print("-" * 50)
    for acc in client.list_accounts():
        flag = " [off-budget]" if acc["offbudget"] else " [on-budget]"
        print(f"  {acc['name']}{flag}")
        print(f"    ID: {acc['id']}")
    print()


def run_init_accounts(config, config_path, off_budget=False):
    """Create all active bunq accounts as Actual Budget accounts.

    Fetches all monetary accounts from bunq and creates matching accounts
    in Actual Budget. Skips accounts that already exist (matched by name).
    Updates sync.account_map in config.json automatically.

    Args:
        config:     Loaded config dict
        config_path: Path to config.json (for saving the updated account_map)
        off_budget: If True, accounts are created as off-budget (tracking only).
                    Default is False (on-budget, participates in envelope budgeting).
    """
    logger.info("=== Initializing accounts from bunq ===")
    budget_type = "off-budget" if off_budget else "on-budget"
    logger.info(f"Account type: {budget_type}")

    if not config["bunq"].get("installation_token"):
        logger.error("bunq is not set up yet. Run: ./sync.py --setup")
        sys.exit(1)

    # Open a bunq session to fetch account list
    session_token, user_id = bunq_client.create_session(config)
    bunq_accounts = bunq_client.get_monetary_accounts(session_token, user_id)

    if not bunq_accounts:
        logger.error("No active bunq accounts found.")
        sys.exit(1)

    logger.info(f"Found {len(bunq_accounts)} active bunq account(s).")

    actual = _make_actual_client(config)

    # Get existing Actual accounts to detect duplicates
    existing = {acc["name"]: acc["id"] for acc in actual.list_accounts()}

    created = []
    skipped = []

    for bunq_acc in bunq_accounts:
        name = bunq_acc["description"]
        iban = bunq_acc.get("iban", "")
        currency = bunq_acc.get("currency", "EUR")
        balance_raw = bunq_acc.get("balance", "0.00")
        balance = balance_raw.get("value", "0.00") if isinstance(balance_raw, dict) else balance_raw

        if name in existing:
            # Account already exists — update note with IBAN if available
            iban = bunq_acc.get("iban", "")
            if iban:
                actual.set_account_note(name, f"IBAN: {iban}")
                logger.info(f"  UPDATED note: '{name}' → IBAN: {iban}")
            else:
                logger.info(f"  SKIP (already exists, no IBAN): '{name}'")
            skipped.append(name)
            continue

        # Create the account in Actual Budget
        account_id = actual.create_account(
            name=name,
            off_budget=off_budget,
            iban=bunq_acc.get("iban"),
        )

        if account_id:
            logger.info(
                f"  CREATED [{budget_type}]: '{name}'"
                + (f" | IBAN: {iban}" if iban else "")
                + f" | Balance: {balance} {currency}"
            )
            created.append((name, account_id, bunq_acc["id"]))
        else:
            logger.error(f"  FAILED to create account: '{name}'")

    # Summary
    print()
    print(f"=== Account init complete ===")
    print(f"  Created : {len(created)}")
    print(f"  Skipped : {len(skipped)} (already existed)")
    print()

    if not created and not skipped:
        logger.warning("No accounts were processed.")
        return

    # Build and save the account_map — includes both newly created and already existing
    existing_after = {acc["name"]: acc["id"] for acc in actual.list_accounts()}

    account_map = {}
    for bunq_acc in bunq_accounts:
        name = bunq_acc["description"]
        actual_id = existing_after.get(name)
        if actual_id:
            account_map[name] = name  # bunq description -> Actual account name

    # Update config: set account_map and clear the cached resolved map
    # (so it gets re-resolved on next sync with the new account IDs)
    if "sync" not in config:
        config["sync"] = {}
    config["sync"]["account_map"] = account_map
    config["sync"]["_resolved_account_map"] = {}  # Force re-resolution on next sync
    save_config(config, config_path)

    print("account_map updated in config.json:")
    for k, v in account_map.items():
        print(f"  '{k}' -> '{v}'")
    print()
    print("Next step: run './sync.py --full' to import all transactions.")


def build_account_map(session_token, user_id, actual, config, config_path):
    """Resolve and cache bunq account ID -> Actual account UUID mapping."""
    resolved = config.get("sync", {}).get("_resolved_account_map", {})
    if resolved:
        return resolved

    configured_map = config.get("sync", {}).get("account_map", {})
    if not configured_map:
        logger.error(
            "sync.account_map is empty in config.json.\n"
            "Run: ./sync.py --init-accounts\n"
            "Or add entries manually:\n"
            '  "account_map": {"bunq account name": "Actual account name"}'
        )
        sys.exit(1)

    logger.info("Resolving account map (first run only)...")
    bunq_accounts = bunq_client.get_monetary_accounts(session_token, user_id)
    bunq_by_name = {acc["description"]: acc for acc in bunq_accounts}

    resolved_map = {}
    for bunq_name, actual_name in configured_map.items():
        bunq_acc = bunq_by_name.get(bunq_name)
        if not bunq_acc:
            logger.warning(
                f"bunq account '{bunq_name}' not found. "
                f"Available: {list(bunq_by_name.keys())}"
            )
            continue
        actual_id = actual.find_account_by_name(actual_name)
        if not actual_id:
            logger.warning(
                f"Actual account '{actual_name}' not found. "
                f"Run --list-accounts to verify."
            )
            continue
        resolved_map[str(bunq_acc["id"])] = actual_id
        logger.info(
            f"  Mapped: '{bunq_name}' (bunq {bunq_acc['id']}) "
            f"-> '{actual_name}' (Actual {actual_id})"
        )

    if not resolved_map:
        logger.error("No accounts matched. Run --init-accounts or check account_map.")
        sys.exit(1)

    config["sync"]["_resolved_account_map"] = resolved_map
    save_config(config, config_path)
    return resolved_map


def run_sync(config, config_path, full_sync=False, since_date=None):

    """Main sync: fetch new bunq payments, import into Actual Budget."""
    logger.info("=== Starting bunq -> Actual Budget sync ===")

    since_from_cli = since_date is not None

    # Fall back to config value if --since was not passed on the CLI
    if since_date is None:
        since_date = config.get("sync", {}).get("since_date")
        if since_date:
            logger.info(f"Using since_date from config: {since_date}")

    if since_date:
        logger.info(f"Date filter active: only importing payments since {since_date}")
        # Only imply --full when --since was explicitly passed on CLI
        if since_from_cli and not full_sync:
            logger.info("Implying --full because --since was specified.")
            full_sync = True

    if not config["bunq"].get("installation_token"):
        logger.error("bunq is not set up yet. Run: ./sync.py --setup")
        sys.exit(1)

    actual = _make_actual_client(config)
    state = SyncState(config["sync"]["state_file"])
    session_token, user_id = bunq_client.create_session(config)

    account_map = build_account_map(
        session_token, user_id, actual, config, config_path
    )

    # Build IBAN -> Actual UUID map for internal transfer detection.
    # IBANs were stored as notes during --init-accounts.
    iban_to_account_id = actual.get_iban_map()
    if iban_to_account_id:
        logger.info(f"Transfer detection active: {len(iban_to_account_id)} own IBANs loaded.")
    else:
        logger.warning(
            "No IBAN map found — transfer detection disabled. "
            "Run --init-accounts to store IBANs in account notes."
        )

    total_imported = total_skipped = total_errors = 0

    # Build bunq_account_id -> current_balance map for opening balance calculation
    bunq_accounts    = bunq_client.get_monetary_accounts(session_token, user_id)
    bunq_balance_map = {str(a["id"]): a["balance"] for a in bunq_accounts}

    for bunq_account_id_str, actual_account_id in account_map.items():
        bunq_account_id = int(bunq_account_id_str)
        last_id = None if full_sync else state.get_last_payment_id(bunq_account_id)


        # Only apply since_date filter when no state exists yet (first sync)
        # For incremental sync, newer_than_id alone is sufficient
        effective_since = since_date if not last_id else None

        if last_id:
            logger.info(f"Account {bunq_account_id}: incremental sync (from ID {last_id})")
        else:
            logger.info(f"Account {bunq_account_id}: full sync" +
                       (f" (since {effective_since})" if effective_since else ""))

        payments = bunq_client.get_payments(
            session_token, user_id, bunq_account_id,
            newer_than_id=last_id,
            since_date=effective_since,
        )
 
        if not payments:
            logger.info(f"  No new payments for account {bunq_account_id}.")
            continue
 
        # Detect which internal transfers were triggered by a salary payment
        salary_transfer_ids = detect_salary_transfer_ids(payments, iban_to_account_id)
 
        max_payment_id = last_id or 0
        transactions = []
        for payment in payments:
            max_payment_id = max(max_payment_id, payment["id"])
            tx = bunq_payment_to_actual(
                payment,
                actual_account_id,
                iban_to_account_id,
                is_salary_transfer=payment["id"] in salary_transfer_ids,
            )
            if tx:
                transactions.append(tx)
        result = actual.import_transactions(transactions)

        if max_payment_id and max_payment_id != (last_id or 0):
            state.set_last_payment_id(bunq_account_id, max_payment_id) 


        logger.info(
            f"  Account {bunq_account_id}: "
            f"{result['imported']} imported, "
            f"{result['skipped']} skipped, "
            f"{result['errors']} errors."
        )
        total_imported += result["imported"]
        total_skipped  += result["skipped"]
        total_errors   += result["errors"]

    logger.info(
        f"=== Sync complete: {total_imported} imported, "
        f"{total_skipped} skipped, {total_errors} errors ==="
    )

    # Step 2+3+4: Opening balance — only when --since is used.
    # Done AFTER the full import loop so all transactions are committed.
    # Per account: IST (bunq) - Actual balance = diff → book as Opening Balance.
    if since_date and full_sync:
        logger.info("=== Calculating opening balances ===")
        for bunq_account_id_str, actual_account_id in account_map.items():
            current_balance = bunq_balance_map.get(bunq_account_id_str)
            if not current_balance:
                logger.warning(f"No bunq balance for account {bunq_account_id_str} — skipped.")
                continue
            actual.set_opening_balance(
                actual_account_id=actual_account_id,
                current_balance=current_balance,
                since_date=since_date,
            )


def main():
    parser = argparse.ArgumentParser(
        description="Sync bunq transactions to Actual Budget",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  ./sync.py --setup                   # one-time bunq registration
  ./sync.py --init-accounts           # create bunq accounts in Actual (on-budget)
  ./sync.py --init-accounts --off-budget  # create as off-budget (tracking only)
  ./sync.py --full                    # import all transactions
  ./sync.py                           # incremental sync (cron mode)
        """
    )
    parser.add_argument("--setup", action="store_true",
                        help="One-time bunq installation + device registration")
    parser.add_argument("--init-accounts", action="store_true",
                        help="Create all bunq accounts in Actual Budget")
    parser.add_argument("--off-budget", action="store_true",
                        help="Used with --init-accounts: create accounts as off-budget")
    parser.add_argument("--clear-transactions", action="store_true",
                        help="Delete ALL transactions from ALL accounts (with confirmation prompt)")
    parser.add_argument("--since",   default=None, metavar="YYYY-MM-DD",
                        help="Only import payments on or after this date (e.g. 2026-01-01)")
    parser.add_argument("--full", action="store_true",
                        help="Re-import all transactions (ignores last sync state)")
    parser.add_argument("--list-accounts", action="store_true",
                        help="List all Actual Budget accounts")
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config/config.json"),
                        help="Path to config file (default: config/config.json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    config = load_config(args.config)
    log_file = config.get("sync", {}).get("log_file")
    setup_logging(log_file=log_file, verbose=args.verbose)

    if args.setup:
        run_setup(config, args.config)
    elif args.init_accounts:
        run_init_accounts(config, args.config, off_budget=args.off_budget)
    elif args.list_accounts:
        run_list_accounts(config)
    elif args.clear_transactions:
        run_clear_transactions(config)
    else:
        run_sync(config, args.config, full_sync=args.full, since_date=args.since)

if __name__ == "__main__":
    main() 
