#!/opt/actual-budget/venv/bin/python3
"""
state.py
--------
Persists the last synced bunq payment ID per account.
Enables incremental sync — only fetch payments newer than the last known ID.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SyncState:
    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._data = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            with open(self.state_file) as f:
                return json.load(f)
        return {}

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump(self._data, f, indent=2)

    def get_last_payment_id(self, account_id: int):
        """Return last synced payment ID for this account, or None on first run."""
        return self._data.get(str(account_id))

    def set_last_payment_id(self, account_id: int, payment_id: int):
        """Persist the highest payment ID seen for this account."""
        self._data[str(account_id)] = payment_id
        self._save()
        logger.debug(f"State updated: account {account_id} -> payment {payment_id}")
