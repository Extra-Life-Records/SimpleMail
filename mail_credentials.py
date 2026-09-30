"""Mailbox secrets at rest; plaintext exists only in the backend process."""
import copy
import json
import os
import tempfile
from pathlib import Path


FIELDS = ("password", "smtp_password")


def protect(value, decrypt=False):
    from model_control import protect_secret
    return protect_secret(value, decrypt)


def unlock_account(raw, protector=protect):
    account = dict(raw)
    locked = {}
    for field in FIELDS:
        value = account.get(field, "")
        if isinstance(value, dict):
            try:
                if set(value) != {"windows_dpapi"}:
                    raise ValueError("Invalid protected credential")
                account[field] = protector(value["windows_dpapi"], True)
            except Exception:
                # Keep the ciphertext available for later recovery; never overwrite it
                # with a blank value just because Windows cannot unlock it today.
                locked[field] = value
                account[field] = ""
        elif not isinstance(value, str):
            raise ValueError("Invalid mailbox credential")
    if locked:
        account["_locked_credentials"] = locked
    return account


def seal_account(account, protector=protect):
    sealed = dict(account)
    locked = sealed.pop("_locked_credentials", {})
    for field in FIELDS:
        value = sealed.get(field, "")
        if value:
            sealed[field] = {"windows_dpapi": protector(value)}
        elif field in locked:
            sealed[field] = locked[field]
        else:
            sealed[field] = ""
    return sealed


def write_config(path, data, protector=protect):
    """Seal everything before an atomic replace; failures leave the old file intact."""
    path = Path(path)
    sealed = copy.deepcopy(data)
    sealed["accounts"] = [seal_account(a, protector) for a in data.get("accounts", [])]
    for field in FIELDS:
        sealed.pop(field, None)  # obsolete single-account secrets
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".config-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(sealed, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
