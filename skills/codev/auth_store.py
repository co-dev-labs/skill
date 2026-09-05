"""Private native-store bridge, invoked only by auth.py's isolated runtime.

JSON is exchanged over private subprocess pipes, never the agent transcript.
Do not invoke this bridge as a user-facing CLI.
"""

import json
import sys


def main():
    request = json.load(sys.stdin)
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring
    elif sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring as Keyring
    else:
        from keyring.backends.SecretService import Keyring
    # Instantiate only a known OS backend. Never load keyringrc, entry points,
    # PYTHON_KEYRING_BACKEND, or alternative plaintext stores.
    backend = Keyring()
    service, account = request["service"], request["account"]
    action = request["action"]
    try:
        if action == "get":
            value = backend.get_password(service, account)
        elif action == "set":
            backend.set_password(service, account, request["value"])
            value = backend.get_password(service, account) == request["value"]
            if not value:
                raise RuntimeError("Credential write could not be verified")
        elif action == "delete":
            if backend.get_password(service, account) is not None:
                backend.delete_password(service, account)
            value = None
        else:
            raise ValueError("Unknown storage operation")
        print(json.dumps({"ok": True, "value": value}))
    except Exception:
        # Backend exceptions may contain input or native implementation detail.
        print(json.dumps({"ok": False, "code": "credential_store_locked"}))
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print(json.dumps({"ok": False, "code": "credential_store_unavailable"}))
        raise SystemExit(1)
