#!/usr/bin/env python3
"""Add or remove the omen-fx pam_exec lines in a /etc/pam.d file.

Editing a PAM stack is the one genuinely dangerous part of omen-fx, so this
script is deliberately conservative:

  * both lines are ``optional``, so PAM ignores whatever they return;
  * the hook itself always exits 0 (see omen-fx-pam);
  * the original file is copied to <file>.omen-fx.bak before anything changes;
  * the result is checked line by line against the original -- if any existing
    line moved or changed, the edit is rolled back.

Keep a root shell open in another terminal the first time you enable this.
"""

import argparse
import os
import shutil
import sys

BEGIN = "# omen-fx BEGIN -- remove with: sudo /usr/local/lib/omen-fx/pam-hook.py disable /etc/pam.d/sudo"
END = "# omen-fx END"
HOOK = "/usr/local/lib/omen-fx/omen-fx-pam"

AUTH_LINE = f"auth       optional     pam_exec.so quiet {HOOK} start"
FAIL_LINE = f"auth       optional     pam_exec.so quiet {HOOK} fail"
ACCOUNT_LINE = f"account    optional     pam_exec.so quiet {HOOK} stop"

# Modules whose success ends the auth stack, so anything after them runs only
# when they did not succeed. That is where the "howdy gave up" hook belongs.
FACE_MODULES = ("pam_howdy", "pam_face", "howdy")


def strip_hook(lines):
    out, skipping = [], False
    for line in lines:
        if line.strip() == BEGIN:
            skipping = True
            continue
        if skipping:
            if line.strip() == END:
                skipping = False
            continue
        out.append(line)
    return out


def insert_hook(lines):
    lines = strip_hook(lines)
    out, auth_done, account_done, fail_done = [], False, False, False
    for line in lines:
        stripped = line.lstrip()
        if not auth_done and stripped.startswith("auth"):
            # Before everything else in the auth stack, so the bar lights up
            # as the prompt appears rather than after it is answered.
            out += [BEGIN, AUTH_LINE, END]
            auth_done = True
        if not account_done and stripped.startswith("account"):
            out += [BEGIN, ACCOUNT_LINE, END]
            account_done = True
        out.append(line)
        if (not fail_done and stripped.startswith("auth")
                and "sufficient" in stripped
                and any(m in stripped for m in FACE_MODULES)):
            # Straight after the face module: a "sufficient" that succeeds ends
            # the stack, so reaching this line means the face scan failed.
            out += [BEGIN, FAIL_LINE, END]
            fail_done = True
    if not auth_done:
        raise SystemExit("no 'auth' line found -- refusing to edit this file")
    if not account_done:
        out += [BEGIN, ACCOUNT_LINE, END]
    return out


def verify(original, patched):
    """Every pre-existing line must survive, in order and unchanged."""
    kept = [l for l in strip_hook(patched)]
    if kept != strip_hook(original):
        raise SystemExit("sanity check failed: existing PAM lines were altered")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=["enable", "disable", "show"])
    ap.add_argument("files", nargs="*", default=["/etc/pam.d/sudo"],
                    help="PAM service files (default: /etc/pam.d/sudo)")
    args = ap.parse_args()

    for path in (args.files or ["/etc/pam.d/sudo"]):
        if not os.path.isfile(path):
            print(f"skip {path}: not a file", file=sys.stderr)
            continue
        with open(path) as fh:
            original = fh.read().splitlines()

        if args.action == "show":
            state = "enabled" if any(l.strip() == BEGIN for l in original) else "not enabled"
            print(f"{path}: {state}")
            continue

        patched = insert_hook(original) if args.action == "enable" else strip_hook(original)
        if patched == original:
            print(f"{path}: already up to date")
            continue
        verify(original, patched)

        backup = path + ".omen-fx.bak"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        tmp = path + ".omen-fx.tmp"
        with open(tmp, "w") as fh:
            fh.write("\n".join(patched) + "\n")
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
        print(f"{path}: {args.action}d (backup: {backup})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
