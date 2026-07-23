#!/usr/bin/env python3
"""Tear down everything provision_lab.py created.

Removes the IAM data-access binding and SCRAM user creds for every user
found on the database (queried live via `gcloud firestore user-creds
list` -- not just the one .env happens to remember), then deletes the
Firestore Enterprise database itself -- which also removes every lab_
collection and any other data in it. This is destructive and not
reversible: there is no undo for a deleted database.

Defaults --database-id from this directory's .env (MONGO_URI), since
provision_lab.py's connection string already encodes it. --project-id
always has to be passed explicitly -- it isn't part of the connection
string. --user-id is rarely needed: pass it only to also remove a user
that for some reason doesn't show up in the live user-creds list.

    python decommission_lab.py --project-id my-project
    python decommission_lab.py --project-id my-project --database-id my-lab
    python decommission_lab.py --project-id my-project --dry-run   # show the plan, touch nothing

By default this asks you to type the database ID back to confirm before
deleting anything. Pass --yes to skip that (e.g. for scripted teardown).
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
ENV_PATH = LAB_DIR / ".env"

IAM_ROLE = "roles/datastore.owner"
IAM_CONDITION_TITLE = "firestore-lab-user-access"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id", required=True, help="GCP project the database lives in.")
    parser.add_argument("--database-id", default=None,
                         help="Firestore database ID to delete. Defaults to what's in .env's MONGO_URI.")
    parser.add_argument("--user-id", default=None,
                         help="SCRAM user ID to delete. Defaults to what's in .env's MONGO_URI.")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print the plan and exit -- deletes nothing.")
    parser.add_argument("--yes", action="store_true",
                         help="Skip the typed database-ID confirmation before deleting.")
    return parser.parse_args()


def defaults_from_env():
    """Best-effort database_id/user_id from .env's MONGO_URI, so you don't have
    to remember them separately from what provision_lab.py already wrote down."""
    if not ENV_PATH.exists():
        return None, None
    from pymongo.uri_parser import parse_uri

    for line in ENV_PATH.read_text().splitlines():
        if line.startswith("MONGO_URI="):
            uri = line.split("=", 1)[1].strip()
            try:
                parsed = parse_uri(uri)
            except Exception:
                return None, None
            return parsed.get("database"), parsed.get("username")
    return None, None


def run(cmd):
    """Returns whether cmd actually succeeded -- always, regardless of whether
    that failure is expected (e.g. deleting something already gone) or not.
    (A prior version took a `check` flag that, when False, unconditionally
    returned True and swallowed the real exit code -- so every "already
    removed?" caller below reported success even when the underlying gcloud
    call had actually failed, e.g. from an expired auth token. Silent false
    positives here are worse than a noisy stderr line.)"""
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  {' '.join(cmd)}\n  {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def get_project_number(project_id):
    import subprocess
    result = subprocess.run(
        ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() or None


def list_user_ids(project_id, database_id):
    """Discover every SCRAM user actually on this database via the API --
    ground truth, unlike a --user-id flag or .env's cached MONGO_URI, which
    only ever know about the single user the last provision_lab.py run
    created. Without this, repeated provision runs against the same
    database silently orphan every earlier run's user + IAM binding."""
    import subprocess
    result = subprocess.run(
        ["gcloud", "firestore", "user-creds", "list",
         f"--database={database_id}", f"--project={project_id}", "--format=json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  (couldn't list user creds on '{database_id}': {result.stderr.strip()})", file=sys.stderr)
        return []
    data = json.loads(result.stdout) if result.stdout.strip() else []
    items = data.get("userCreds", []) if isinstance(data, dict) else data
    return sorted(item["name"].rsplit("/", 1)[-1] for item in items)


def remove_iam_binding(project_id, project_number, database_id, user_id):
    print(f"Removing IAM binding for user '{user_id}'...")
    member = (f"principal://firestore.googleapis.com/projects/{project_number}"
              f"/name/databases/{database_id}/userCreds/{user_id}")
    condition = (f'expression=resource.name == "projects/{project_id}/databases/{database_id}",'
                 f'title={IAM_CONDITION_TITLE}')
    ok = run([
        "gcloud", "projects", "remove-iam-policy-binding", project_id,
        f"--member={member}", f"--role={IAM_ROLE}", f"--condition={condition}", "--format=json",
    ])
    print("  done." if ok else "  binding wasn't found (already removed?) -- continuing.")


def delete_user(project_id, database_id, user_id):
    print(f"Deleting SCRAM user '{user_id}'...")
    ok = run([
        "gcloud", "firestore", "user-creds", "delete", user_id,
        f"--database={database_id}", f"--project={project_id}", "--quiet",
    ])
    print("  done." if ok else "  user wasn't found (already deleted?) -- continuing.")


def delete_database(project_id, database_id):
    print(f"Deleting database '{database_id}' (this removes all its data)...")
    ok = run([
        "gcloud", "firestore", "databases", "delete",
        f"--database={database_id}", f"--project={project_id}", "--quiet",
    ])
    if not ok:
        raise SystemExit(f"Failed to delete database '{database_id}' -- see the error above.")
    print("  done.")


def retire_env_file():
    if not ENV_PATH.exists():
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    retired = ENV_PATH.with_name(f".env.decommissioned-{stamp}")
    ENV_PATH.rename(retired)
    print(f"Moved .env to {retired.name} (it pointed at a database that no longer exists).")


def main():
    args = parse_args()
    env_database_id, env_user_id = defaults_from_env()
    database_id = args.database_id or env_database_id

    if not database_id:
        raise SystemExit("No --database-id given and none found in .env -- pass it explicitly.")

    user_ids = set(list_user_ids(args.project_id, database_id))
    if args.user_id:
        user_ids.add(args.user_id)
    if env_user_id:
        user_ids.add(env_user_id)
    user_ids = sorted(user_ids)

    print("Plan:")
    print(f"  Project:  {args.project_id}")
    print(f"  Database: {database_id}  <-- will be DELETED, along with all its data")
    print(f"  Users:    {', '.join(user_ids) if user_ids else '(none found -- nothing to clean up in IAM)'}")
    print(f"  Then:     retires {ENV_PATH.name} (renamed, not removed)")

    if args.dry_run:
        print("\n--dry-run: nothing was deleted.")
        return

    if not args.yes:
        confirm = input(f"\nThis permanently deletes database '{database_id}' and all its data. "
                         f"Type the database ID to confirm: ").strip()
        if confirm != database_id:
            print("Confirmation didn't match -- aborted, nothing was deleted.")
            return

    if user_ids:
        project_number = get_project_number(args.project_id)
        for user_id in user_ids:
            if project_number:
                remove_iam_binding(args.project_id, project_number, database_id, user_id)
            delete_user(args.project_id, database_id, user_id)
    else:
        print("No SCRAM users found on this database -- nothing to clean up in IAM.")

    delete_database(args.project_id, database_id)
    retire_env_file()
    if user_ids:
        print(f"\nDone. '{database_id}' and access grants for {len(user_ids)} user(s) "
              f"({', '.join(user_ids)}) are gone.")
    else:
        print(f"\nDone. '{database_id}' is gone. No SCRAM users needed cleanup.")


if __name__ == "__main__":
    main()
