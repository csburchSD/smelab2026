#!/usr/bin/env python3
"""Provision a Firestore Enterprise (MongoDB-compatible) database for this lab.

Given a GCP project, this creates a Firestore Enterprise database with
MongoDB-compatible data access enabled, a SCRAM user scoped to it via IAM,
waits for that IAM binding to propagate, builds the MONGO_URI connection
string, writes it to .env, and seeds the lab data. This is the fast path
described in FACILITATOR_GUIDE.md's "Setting up a new environment" section
-- it replaces the manual gcloud steps there with one command.

This is also the single entry point for everything else you'd otherwise
need a separate script for -- resetting lab data, checking the current
connection, or (facilitators only) running all 7 anti-pattern scripts in
sequence:

    python provision_lab.py --project-id my-project    # provision a new environment
    python provision_lab.py --project-id my-project --dry-run   # show the plan, touch nothing
    python provision_lab.py --reset      # wipe + reseed lab_ collections in the current .env
    python provision_lab.py --check      # confirm .env can actually connect
    python provision_lab.py --run-all    # facilitator dry run: reset, then run scripts 01-07
    python provision_lab.py              # no args, .env already configured: interactive menu

Requires: gcloud CLI, authenticated (`gcloud auth login`) against the
target project, with permission to create Firestore databases and modify
IAM policy. This creates real, billed GCP resources -- it will show you
exactly what it's about to do and ask for confirmation before touching
anything, unless you pass --yes.

The counterpart to this script is decommission_lab.py, which tears down
everything this script creates.
"""

import argparse
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent
ENV_PATH = LAB_DIR / ".env"

DEFAULT_LOCATION = "us-west1"
DEFAULT_DATABASE_ID = "firestore-lab"
IAM_ROLE = "roles/datastore.owner"
IAM_CONDITION_TITLE = "firestore-lab-user-access"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id", default=None,
                         help="GCP project to provision into. Required to provision a new "
                              "environment; not needed for --reset/--check/--run-all, which "
                              "work off the current .env.")
    parser.add_argument("--database-id", default=DEFAULT_DATABASE_ID,
                         help=f"Firestore database ID (default: {DEFAULT_DATABASE_ID}). "
                              "Reused if it already exists.")
    parser.add_argument("--location", default=None,
                         help="Firestore location. Defaults to the region this machine is "
                              f"detected to be running in, else {DEFAULT_LOCATION}. Running the "
                              "scripts in a different region than the database adds real "
                              "cross-region latency to every query in this lab -- see the "
                              "region-mismatch warning if the two don't match.")
    parser.add_argument("--user-id", default=None,
                         help="SCRAM user ID to create (default: lab-user-<random>, so reruns "
                              "never collide with an existing user).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print the plan and exit -- no gcloud calls that create anything.")
    parser.add_argument("--yes", action="store_true",
                         help="Skip the confirmation prompt before creating real GCP resources.")
    parser.add_argument("--skip-seed", action="store_true",
                         help="Don't seed the lab_ collections automatically after provisioning.")
    parser.add_argument("--reset", action="store_true",
                         help="Wipe and reseed the lab_ collections in the database the current "
                              ".env points at. No GCP calls, no --project-id needed.")
    parser.add_argument("--check", action="store_true",
                         help="Confirm the current .env can actually connect, and report what's "
                              "there.")
    parser.add_argument("--run-all", action="store_true",
                         help="Facilitator dry run: reset, then run all 7 anti-pattern scripts "
                              "in sequence.")
    return parser.parse_args()


def detect_local_region(timeout=2):
    """Best-effort region of the machine currently running these scripts, via
    the GCE metadata server. Returns None off-GCE (e.g. a trainee's own
    laptop, or Cloud Shell) or if the metadata server doesn't respond within
    timeout -- callers must treat None as "unknown", not "no mismatch"."""
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/zone",
            headers={"Metadata-Flavor": "Google"},
        )
        zone = urllib.request.urlopen(req, timeout=timeout).read().decode().strip()
        # e.g. "projects/123456789/zones/us-west1-b" -> "us-west1"
        return zone.rsplit("/", 1)[-1].rsplit("-", 1)[0]
    except Exception:
        return None


def region_from_uri(uri):
    """Pull the Firestore location out of a MONGO_URI's host
    (<uid>.<location>.firestore.goog) -- None if it doesn't match that shape."""
    match = re.search(r"@[^.]+\.([^.]+)\.firestore\.goog", uri)
    return match.group(1) if match else None


class _Heartbeat:
    """gcloud commands like `databases create` block for 30-90s with zero
    output of their own. Print a periodic elapsed-time line from a background
    thread while one's in flight, so a silent terminal doesn't read as hung."""

    def __init__(self, label, interval=15):
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        elapsed = 0
        while not self._stop.wait(self.interval):
            elapsed += self.interval
            print(f"  ...{self.label} ({elapsed}s elapsed, this can take a minute or two)")

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join()


def run(cmd, check=True):
    """--quiet answers gcloud's own interactive prompts with their default
    instead of leaving them dangling -- e.g. its "API not enabled -- enable
    it now? (y/N)" prompt on a brand-new project defaults to N, so it fails
    fast with a visible SERVICE_DISABLED error rather than actually enabling
    anything (confirmed directly: --quiet alone does NOT fix a disabled API,
    which is why ensure_api_enabled() exists as a separate, explicit step).
    stdin=DEVNULL is a second line of defense: capture_output=True hides any
    prompt gcloud prints (it goes into result.stderr, not the terminal), and
    without DEVNULL, stdin is inherited from this process -- so an
    unanticipated prompt would block forever on input the user can't see
    they need to give, instead of failing fast with a visible error
    (observed directly: this is exactly what happened on a fresh project
    before --quiet was added)."""
    if cmd and cmd[0] == "gcloud" and "--quiet" not in cmd:
        cmd = [cmd[0], "--quiet", *cmd[1:]]
    result = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"Command failed: {' '.join(cmd)}")
    return result


def run_json(cmd):
    result = run(cmd)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def database_exists(project_id, database_id):
    result = run([
        "gcloud", "firestore", "databases", "describe",
        f"--database={database_id}", f"--project={project_id}", "--format=json",
    ], check=False)
    return result.returncode == 0


def ensure_api_enabled(project_id):
    """A brand-new project has never called the Firestore API before, so it's
    disabled by default -- `gcloud firestore databases create` fails with
    SERVICE_DISABLED in that case. This isn't the "hang on an invisible
    prompt" bug run()'s --quiet fixes -- confirmed directly: --quiet
    suppresses gcloud's own "enable it now? (y/N)" prompt but defaults it to
    N, so the command still fails, just fast and visibly instead of hanging.
    So enable it explicitly and unconditionally up front -- a fast no-op if
    it's already enabled, which it always will be when reusing an existing
    database."""
    print("Ensuring the Firestore API is enabled...")
    run(["gcloud", "services", "enable", "firestore.googleapis.com", f"--project={project_id}"])


def get_project_number(project_id):
    result = run(["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"])
    number = result.stdout.strip()
    if not number:
        raise SystemExit(f"Could not resolve a project number for '{project_id}'. "
                          "Check the project ID and that you have access to it.")
    return number


def create_database(project_id, database_id, location, attempts=6):
    """A database ID that was just deleted (e.g. a decommission_lab.py run
    moments ago) isn't immediately reusable -- gcloud returns
    FAILED_PRECONDITION with the exact cooldown left in its own error
    message. Parse that and retry instead of just dying, since
    provision -> decommission -> provision is a normal cycle here.

    Also retries on SERVICE_DISABLED: ensure_api_enabled() runs first, but
    Google's own error text on this says enabling "can take a few minutes to
    propagate" -- so a project that just had the API enabled for the first
    time can still 403 here briefly even though enabling itself already
    succeeded."""
    cmd = [
        "gcloud", "firestore", "databases", "create",
        f"--database={database_id}", f"--location={location}", f"--project={project_id}",
        "--edition=enterprise", "--enable-mongodb-compatible-data-access",
    ]
    for attempt in range(1, attempts + 1):
        print(f"Creating Firestore Enterprise database '{database_id}' in {location}...")
        with _Heartbeat("still creating the database"):
            result = run(cmd, check=False)
        if result.returncode == 0:
            return
        match = re.search(r"not available.*retry in (\d+) seconds", result.stderr)
        if match and attempt < attempts:
            wait_s = int(match.group(1)) + 5
            print(f"  '{database_id}' was just deleted and isn't reusable yet -- "
                  f"waiting {wait_s}s for the cooldown (attempt {attempt}/{attempts})...")
            time.sleep(wait_s)
            continue
        if "SERVICE_DISABLED" in result.stderr and attempt < attempts:
            wait_s = 20
            print(f"  Firestore API was just enabled and hasn't finished propagating yet -- "
                  f"waiting {wait_s}s (attempt {attempt}/{attempts})...")
            time.sleep(wait_s)
            continue
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"Command failed: {' '.join(cmd)}")


def create_user(project_id, database_id, user_id, attempts=5, delay_s=15):
    """A database that was just created can briefly reject user-creds create
    with ABORTED before the backend has fully settled (observed directly
    against a fresh project: failed immediately after `databases create`
    returned, succeeded on a manual retry ~30s later). Unlike
    create_database's cooldown error, gcloud doesn't give a wait-time here,
    so just retry a few times with a fixed delay instead of failing the
    whole provisioning run over a startup race."""
    cmd = [
        "gcloud", "firestore", "user-creds", "create", user_id,
        f"--database={database_id}", f"--project={project_id}", "--format=json",
    ]
    print(f"Creating SCRAM user '{user_id}'...")
    for attempt in range(1, attempts + 1):
        with _Heartbeat("still creating the user"):
            result = run(cmd, check=False)
        if result.returncode == 0:
            data = json.loads(result.stdout) if result.stdout.strip() else {}
            break
        if "ABORTED" in result.stderr and attempt < attempts:
            print(f"  database not quite ready yet (ABORTED) -- waiting {delay_s}s "
                  f"(attempt {attempt}/{attempts})...")
            time.sleep(delay_s)
            continue
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(f"Command failed: {' '.join(cmd)}")
    password = data.get("securePassword")
    if not password:
        raise SystemExit(
            "gcloud created the user but didn't return a password in its response. "
            "Raw response:\n" + json.dumps(data, indent=2) +
            "\nA user creds password can only be read once, at creation -- rerun with a "
            "different --user-id to get a fresh one."
        )
    return password


def add_iam_binding(project_id, project_number, database_id, user_id):
    print("Granting IAM data access to the new user (this can take a couple of minutes to propagate)...")
    member = (f"principal://firestore.googleapis.com/projects/{project_number}"
              f"/name/databases/{database_id}/userCreds/{user_id}")
    condition = (f'expression=resource.name == "projects/{project_id}/databases/{database_id}",'
                 f'title={IAM_CONDITION_TITLE}')
    run([
        "gcloud", "projects", "add-iam-policy-binding", project_id,
        f"--member={member}", f"--role={IAM_ROLE}", f"--condition={condition}", "--format=json",
    ])


def get_database_uid_location(project_id, database_id):
    data = run_json([
        "gcloud", "firestore", "databases", "describe",
        f"--database={database_id}", f"--project={project_id}", "--format=json",
    ])
    return data["uid"], data["locationId"]


def build_uri(user_id, password, uid, location, database_id):
    return (f"mongodb://{user_id}:{password}@{uid}.{location}.firestore.goog:443/"
            f"{database_id}?loadBalanced=true&tls=true&authMechanism=SCRAM-SHA-256&retryWrites=false")


def write_env(uri):
    ENV_PATH.write_text(f"MONGO_URI={uri}\n")


def wait_for_access(uri, attempts=6, delay_s=20):
    """IAM bindings can take a couple of minutes to propagate -- retry a trivial
    write instead of assuming success right after add_iam_binding() returns."""
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    print("Confirming write access now that the IAM binding is in place...")
    client = MongoClient(uri)
    db = client.get_default_database()
    for attempt in range(1, attempts + 1):
        try:
            db["lab_provision_probe"].insert_one({"_id": "probe"})
            db["lab_provision_probe"].drop()
            print("Access confirmed.")
            return True
        except PyMongoError as e:
            if attempt == attempts:
                break
            print(f"  not ready yet ({type(e).__name__}), waiting {delay_s}s "
                  f"(attempt {attempt}/{attempts})...")
            time.sleep(delay_s)
    return False


def check_connection():
    """Confirm .env can actually connect, and report what's there -- the
    single place that explains what an empty collection list vs. a
    connection error means, reused by --check, the interactive menu, and
    LAB_GUIDE.md (which just tells trainees to run --check rather than
    spelling this out itself)."""
    if not ENV_PATH.exists():
        print(f"No {ENV_PATH} found -- nothing to check yet. "
              "Run with --project-id to provision an environment.")
        return
    from pymongo.errors import PyMongoError

    from _internal.lab_config import get_lab_db

    try:
        names = get_lab_db().list_collection_names()
    except PyMongoError as e:
        print(f"Couldn't connect ({type(e).__name__}): {e}")
        print("MONGO_URI in .env may be wrong, or the database/user isn't provisioned yet.")
        return
    if not names:
        print("Connected, but this database has no collections yet -- either the wrong "
              "(unseeded) database, or run `python provision_lab.py --reset` to seed it.")
    else:
        print(f"Connected. Collections found: {names}")

    db_region = region_from_uri(ENV_PATH.read_text())
    local_region = detect_local_region()
    if db_region and local_region and db_region != local_region:
        print(
            f"\nWarning: this database is in {db_region}, but this machine is running in "
            f"{local_region}. Every query in this lab will cross regions, which adds real "
            "latency on top of whatever anti-pattern you're trying to isolate -- run these "
            f"scripts from a machine in {db_region}, or `python decommission_lab.py` and "
            f"reprovision with `--location {local_region}`."
        )


def show_menu():
    """Interactive fallback for `python provision_lab.py` with no flags at
    all, once .env is already configured -- surfaces the same actions
    --reset/--check give directly, for anyone who didn't remember the flag
    name. --run-all deliberately isn't offered here -- facilitator-only,
    same visibility run_all.py always had."""
    print(f"An environment is already configured in {ENV_PATH}.")
    print("What would you like to do?")
    print("  1) Reset lab data (wipe + reseed lab_ collections)")
    print("  2) Check connection")
    print("  3) Exit")
    choice = input("Choice [1-3]: ").strip()
    if choice == "1":
        from _internal import reset_lab
        reset_lab.main()
    elif choice == "2":
        check_connection()
    else:
        print("Exiting -- nothing changed.")


def main():
    args = parse_args()

    if args.check:
        check_connection()
        return

    if args.reset:
        from _internal import reset_lab
        reset_lab.main()
        return

    if args.run_all:
        # Several of the numbered scripts run their own argparse.parse_args()
        # against sys.argv -- when run_all.main() calls into them in-process
        # like this (rather than as its own clean `python run_all.py`
        # process), provision_lab.py's own flags are still sitting in argv
        # and get rejected as unrecognized. Strip them so each script sees
        # the same empty argv it would from a standalone invocation.
        saved_argv, sys.argv = sys.argv, [sys.argv[0]]
        try:
            from _internal import run_all
            run_all.main()
        finally:
            sys.argv = saved_argv
        return

    if not args.project_id:
        if ENV_PATH.exists():
            show_menu()
        else:
            print(f"No --project-id given and no {ENV_PATH} found -- pass --project-id to "
                  "provision a new environment. Run with --help to see all options.")
            sys.exit(1)
        return

    user_id = args.user_id or f"lab-user-{secrets.token_hex(4)}"
    db_already_exists = database_exists(args.project_id, args.database_id)
    local_region = detect_local_region()

    region_warning = None
    if db_already_exists:
        existing_uid, existing_location = get_database_uid_location(args.project_id, args.database_id)
        if local_region and existing_location != local_region:
            region_warning = (
                f"'{args.database_id}' already exists in {existing_location}, but this machine "
                f"is running in {local_region}. Every query in this lab will cross regions, "
                "which adds real latency on top of whatever anti-pattern you're trying to "
                "isolate. To fix it: `python decommission_lab.py`, then rerun with "
                f"--location {local_region} (or run these scripts from a machine in "
                f"{existing_location})."
            )
    else:
        args.location = args.location or local_region or DEFAULT_LOCATION
        if local_region and args.location != local_region:
            region_warning = (
                f"this machine is running in {local_region}, but you're about to create the "
                f"database in {args.location}. Every query in this lab will cross regions, "
                "which adds real latency on top of whatever anti-pattern you're trying to "
                f"isolate -- pass --location {local_region} unless you specifically intend to "
                "run cross-region."
            )

    print("Plan:")
    print(f"  Project:  {args.project_id}")
    print(f"  Database: {args.database_id} "
          f"({'reuse existing' if db_already_exists else f'create new, location={args.location}'})")
    print(f"  User:     {user_id} (new SCRAM user + IAM data-access binding)")
    print(f"  Writes:   {ENV_PATH}"
          f"{' (will overwrite existing file)' if ENV_PATH.exists() else ''}")
    if not args.skip_seed:
        print("  Then:     seeds the lab_ collections")
    if region_warning:
        print(f"\nWarning: {region_warning}")

    if args.dry_run:
        print("\n--dry-run: no gcloud calls that create or modify anything were made.")
        return

    if not args.yes:
        prompt = "\nThis will create real, billed GCP resources. Continue? [y/N] "
        if region_warning:
            prompt = "\nContinue despite the region mismatch above? [y/N] "
        confirm = input(prompt).strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    ensure_api_enabled(args.project_id)

    if not db_already_exists:
        create_database(args.project_id, args.database_id, args.location)
        existing_uid, existing_location = get_database_uid_location(args.project_id, args.database_id)
    else:
        print(f"Database '{args.database_id}' already exists -- reusing it.")

    project_number = get_project_number(args.project_id)
    password = create_user(args.project_id, args.database_id, user_id)
    add_iam_binding(args.project_id, project_number, args.database_id, user_id)
    uri = build_uri(user_id, password, existing_uid, existing_location, args.database_id)
    write_env(uri)
    print(f"Wrote {ENV_PATH} (password redacted from this output).")

    if not wait_for_access(uri):
        print(
            "\nIAM access still hasn't propagated after several attempts. This is normal -- "
            "it can take a few minutes. .env is already written; once it's ready, run:\n"
            "  python provision_lab.py --reset"
        )
        return

    if not args.skip_seed:
        print("\nSeeding lab data...")
        from _internal import setup_lab
        setup_lab.main()

    print(f"\nDone. Database '{args.database_id}' is ready -- MONGO_URI is in {ENV_PATH}.")
    print(f"Keep track of: project={args.project_id}, database={args.database_id}, user={user_id} "
          f"-- decommission_lab.py needs the project and database ID to tear this down later.")


if __name__ == "__main__":
    main()
