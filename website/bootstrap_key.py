#!/usr/bin/env python3
"""First-touch bootstrap: password SSH -> install deploy pubkey for root."""
import sys
import paramiko

HOST = sys.argv[1]
PASSWORD = sys.argv[2]
PUBKEY = open(sys.argv[3], encoding="ascii").read().strip()

for user in ("root", "ubuntu"):
    try:
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(HOST, username=user, password=PASSWORD, timeout=20, banner_timeout=20, auth_timeout=20)
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"grep -qF '{PUBKEY}' ~/.ssh/authorized_keys 2>/dev/null || echo '{PUBKEY}' >> ~/.ssh/authorized_keys; "
            "chmod 600 ~/.ssh/authorized_keys && echo KEY_INSTALLED && uname -a && free -m | head -2"
        )
        _, out, err = c.exec_command(cmd, timeout=30)
        print(f"USER={user}")
        print(out.read().decode())
        e = err.read().decode().strip()
        if e:
            print("STDERR:", e)
        c.close()
        sys.exit(0)
    except paramiko.AuthenticationException:
        print(f"auth failed for {user}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"{user}: {type(exc).__name__}: {exc}", file=sys.stderr)
sys.exit(1)
