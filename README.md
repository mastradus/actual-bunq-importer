# Actual-Bunq-Importer

## Background

After discovering [Actual Budget](https://actualbudget.org/) as a candidate for my personal budgeting and financial planning, I quickly realised there was no way to connect my european bank. The previous sync provider had discontinued its service for private customers, leaving no out-of-the-box integration.

That's when I started this ambitious little project — building a custom sync connector for [bunq](https://www.bunq.com/), with a lot of help from [Claude](https://claude.ai). Thanks to the well-documented APIs on both sides, the implementation turned out to be more straightforward than expected. I reviewed most of the code myself and can see plenty of room for improvement — but as a pragmatist, I care more about results than perfection.

And voilà: after quite a few rounds of testing, the sync works reliably and I've been running it in production ever since. But enough words — here's everything you need to get started.

---

Imports bank transactions from [bunq](https://www.bunq.com/) into [Actual Budget](https://actualbudget.org/) via the bunq API and [actualpy](https://github.com/bvanelli/actualpy).

## Features

- Incremental sync via cursor-based pagination (only fetches new transactions)
- Automatic internal transfer detection between own bunq accounts
- Duplicate detection via `financial_id` / `imported_id`
- Opening balance calculation for initial import
- Configurable start date (`since_date`) to limit import history
- All transactions imported as **cleared**
- Persistent sync state per account (survives restarts)
- Rotating log file support

## Requirements

- Python 3.11+
- A [bunq](https://www.bunq.com/) account with API key
- A running [Actual Budget](https://actualbudget.org/) server instance

## Installation

```bash
git clone https://github.com/mastradus/actual-bunq-importer.git
cd actual-budget-bunq

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy the example config and fill in your values:

```bash
cp config/config.example.json config/config.json
```

### config.json fields

| Field | Description |
|---|---|
| `bunq.api_key` | Your bunq API key (from bunq app → Profile → Security → API keys) |
| `bunq.environment` | `PRODUCTION` or `SANDBOX` |
| `bunq.device_description` | Arbitrary label shown in bunq app |
| `actual.url` | URL of your Actual Budget server, e.g. `https://fin.example.com` |
| `actual.password` | Actual Budget server password |
| `actual.budget_name` | Name of the budget file (as shown top-left in the UI) |
| `actual.encryption_password` | Optional — only needed if E2E encryption is enabled |
| `actual.data_dir` | Local cache directory for the budget SQLite copy |
| `sync.state_file` | Path to persist last synced payment IDs |
| `sync.log_file` | Optional log file path (stdout only if omitted) |
| `sync.since_date` | Default start date for imports (`YYYY-MM-DD`). Acts as a lower bound on first sync. |
| `sync.account_map` | Mapping of bunq account name → Actual account name (auto-populated by `--init-accounts`) |

## Setup (one-time)

### 1. Register with bunq

```bash
python sync.py --setup
```

This generates an RSA key pair, registers the installation and device with bunq, and saves the tokens to `config.json`.

### 2. Create accounts in Actual Budget

```bash
python sync.py --init-accounts
```

Fetches all active bunq accounts and creates matching accounts in Actual Budget. Stores IBANs in account notes for transfer detection. Updates `account_map` in `config.json` automatically.

Use `--off-budget` to create accounts as tracking-only (not included in envelope budgeting):

```bash
python sync.py --init-accounts --off-budget
```

### 3. Initial import

```bash
python sync.py --full --since 2024-10-01
```

Imports all transactions from the given date. Calculates and books an opening balance if needed to match the current bunq account balance.

## Usage

### Incremental sync (normal / cron mode)

```bash
python sync.py
```

Only fetches transactions newer than the last synced payment ID per account. Safe to run frequently.

### Full re-import

```bash
python sync.py --full
```

Ignores the saved sync state and re-imports all available transactions. Existing transactions are skipped via duplicate detection.

### List Actual accounts

```bash
python sync.py --list-accounts
```

### Delete all transactions

```bash
python sync.py --clear-transactions
```

Prompts for confirmation. Run `--full` afterwards to re-import.

### Custom config path

```bash
python sync.py --config /path/to/config.json
```

### Verbose logging

```bash
python sync.py -v
```

## Cron setup

```bash
crontab -e
```

```cron
*/30 * * * * cd /opt/actual-budget/bunq-importer && /opt/actual-budget/venv/bin/python3 sync.py
```

## Project structure

```
.
├── sync.py           # Entry point and CLI
├── bunq_client.py    # bunq API communication (setup, sessions, payments)
├── actual_client.py  # Actual Budget communication via actualpy
├── mapper.py         # Converts bunq payment dicts to Actual transaction format
├── state.py          # Persists last synced payment ID per account
├── config/
│   ├── config.json          # Your local config
│   └── config.example.json  # Template
├── data/
│   └── sync_state.json      # Auto-generated sync state
└── logs/
    └── sync.log             # Auto-generated log file
```

## Transfer detection

When both sides of a transfer are owned bunq accounts (both IBANs are present in account notes), the sync creates a proper Actual Budget transfer instead of two separate transactions. Only the outgoing side is processed — `create_transfer()` creates both sides automatically.

IBANs are stored during `--init-accounts`. If transfer detection is not working, re-run `--init-accounts` to refresh the IBAN notes.

## Opening balance

When running with `--since` on the CLI, the sync calculates an opening balance per account after the first import:

```
diff = bunq_current_balance - sum(all imported transactions)
```

If `diff != 0`, a single "Starting Balance" transaction is booked one day before the `since_date`. If balances already match, nothing is booked.

This only runs on explicit `--full --since` invocations, not during regular cron sync.

## License

MIT
