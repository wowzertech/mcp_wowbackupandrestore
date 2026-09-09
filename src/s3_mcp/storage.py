"""S3 access, scoped to one authenticated user.

Real layout, one bucket per region:

    s3://example-backups-beta/          <- CA; -au, -nz, -us, ... per country
      aksh29@yopmail.com/          <- the account's prefix, returned by the DB
        Test1/                     <- Xero organisation name
          2026-05-24/              <- backup date
            accounts.csv
            accounts.jsonl
            balance_sheet.jsonl
            balance_sheet.xlsx
            ...

The security property: the root segment comes from the entitlement record the
database returned for this subject, and is never accepted as an argument.
Organisation, date and filename are caller-supplied, so each is validated against
a character allowlist and re-checked against the confined prefix on every call.
"""

from __future__ import annotations

import json
import posixpath
import re
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError


@contextmanager
def _s3_errors(what: str):
    """Turn botocore failures into something the model can act on.

    A stack trace in a tool result tells the model nothing it can use, and tells
    the user even less. Each case here says whose problem it is: the caller's,
    the data's, or ours."""
    try:
        yield
    except NoCredentialsError:
        raise RuntimeError(
            "This server has no AWS credentials, so it cannot reach the backup "
            "store. Nothing is wrong with your account or your subscription — "
            "it is a configuration problem on our side."
        ) from None
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "NoSuchBucket", "404"}:
            raise ValueError(
                f"{what} does not exist. Check the name against list_files, or "
                f"list_backups for the dates that were actually taken."
            ) from None
        if code in {"AccessDenied", "403", "InvalidAccessKeyId", "SignatureDoesNotMatch"}:
            raise RuntimeError(
                "The server was refused access to the backup store. This is a "
                "permissions problem on our side, not something you can fix."
            ) from None
        raise RuntimeError(f"The backup store returned an error for {what} ({code}).") from None
    except BotoCoreError as exc:
        raise RuntimeError(
            f"Could not reach the backup store for {what}: {type(exc).__name__}."
        ) from None

from .auth import Principal
from .entitlements import OrgLocation
from .settings import get_settings


_DATE_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class ObjectInfo:
    name: str            # bare filename, what the model sees
    size_bytes: int
    last_modified: str


@dataclass(frozen=True)
class ObjectRef:
    """One object, fully addressed. Backups live in the bucket for their
    organisation's country, so bucket and region travel with the key."""

    bucket: str
    region: str
    key: str


@lru_cache
def _base_session() -> boto3.Session:
    profile = get_settings().aws_profile
    return boto3.Session(profile_name=profile) if profile else boto3.Session()


def _config() -> Config:
    return Config(
        retries={"max_attempts": 3, "mode": "adaptive"},
        signature_version="s3v4",
        connect_timeout=3,
        read_timeout=25,
    )


class UserStore:
    """All S3 access for one request, bound to one verified email."""

    def __init__(self, principal: Principal) -> None:
        s = get_settings()
        self._s = s
        self.principal = principal
        # From the database, not the token. The user cannot influence either the
        # caller's own prefix or the per-organisation locations.
        self.root = principal.s3_prefix
        locations = (principal.entitlement.org_locations or {}).values()
        self._roots = sorted({self.root, *(loc.prefix for loc in locations)})
        self._buckets = sorted({loc.bucket for loc in locations} or {s.s3_bucket})
        self._clients: dict[str, object] = {}

    # -- client ------------------------------------------------------------- #

    def _client_for(self, region: str):
        """One client per region, built on demand.

        An account can hold organisations in several regions at once — a
        Canadian and an Australian company under the same login — and a client
        signs for the region it was built for, so they cannot share one."""
        client = self._clients.get(region)
        if client is None:
            client = self._build_client(region)
            self._clients[region] = client
        return client

    def _build_client(self, region: str):
        s = self._s
        if not s.s3_assume_role_arn:
            return _base_session().client("s3", region_name=region, config=_config())

        sts = _base_session().client("sts", region_name=s.aws_region)
        session_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    # Every prefix this caller may read, in every bucket their
                    # organisations live in: their own, plus the owners'
                    # prefixes for organisations shared with them.
                    "Resource": [
                        f"arn:aws:s3:::{bucket}/{root}*"
                        for bucket in self._buckets
                        for root in self._roots
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::{bucket}" for bucket in self._buckets],
                    "Condition": {
                        "StringLike": {"s3:prefix": [f"{root}*" for root in self._roots]}
                    },
                },
            ],
        }
        creds = sts.assume_role(
            RoleArn=s.s3_assume_role_arn,
            # Auth0 `sub`, not email — traceable in CloudTrail even if the
            # address later changes. Sanitised because sub contains '|'.
            RoleSessionName="".join(
                c if c.isalnum() or c in "=,.@-" else "-"
                for c in f"mcp-{self.principal.account_id}"
            )[:64],
            Policy=json.dumps(session_policy),
            DurationSeconds=900,
        )["Credentials"]
        return _base_session().client(
            "s3",
            region_name=region,
            config=_config(),
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    # -- path handling ------------------------------------------------------ #

    def _segment(self, value: str, label: str) -> str:
        """Accept one path segment from the model, or refuse it.

        Allowlist, not denylist. Organisation names, dates and filenames are all
        single segments; a caller who supplies anything else is either confused
        or probing. Note the check runs on the *stripped* value and rejects an
        empty result — "/" must not quietly collapse into nothing and shift every
        later segment up a level."""
        if value is None:
            raise ValueError(f"{label} is required")

        cleaned = value.strip().strip("/").strip()
        if not cleaned:
            raise ValueError(f"{label} is required")
        if len(cleaned) > 255:
            raise ValueError(f"{label} is too long")

        # Leading dots are how traversal tricks start; no legitimate Xero
        # organisation, ISO date or backup filename begins with one.
        if cleaned.startswith("."):
            raise PermissionError(f"invalid {label}: {value!r}")

        allowed = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789"
            " ._-()&@+"
        )
        if not set(cleaned) <= allowed:
            bad = "".join(sorted(set(cleaned) - allowed))
            raise PermissionError(f"invalid character(s) in {label}: {bad!r}")

        return cleaned

    def _resolve(self, root: str, *segments: str) -> str:
        """Join segments under `root` and re-check the result stays inside it.

        `root` is always a prefix the database handed us — never a caller value —
        so the confinement check is against something the caller cannot move."""
        if root not in self._roots:
            raise PermissionError("path resolves outside your backup area")
        absolute = posixpath.normpath(posixpath.join(root, *segments))
        if not absolute.startswith(root):
            raise PermissionError("path resolves outside your backup area")
        return absolute

    def _location_for(self, organisation: str) -> OrgLocation:
        """Where this organisation's backups are — the owner's prefix, and the
        bucket and region for the country it is stored in. None of it is the
        caller's to choose."""
        return self.principal.entitlement.location_for(organisation)

    # -- navigation --------------------------------------------------------- #

    def _list_folders(self, loc: OrgLocation, prefix: str) -> list[str]:
        client = self._client_for(loc.region)
        paginator = client.get_paginator("list_objects_v2")
        names: list[str] = []
        with _s3_errors("this backup area"):
            for page in paginator.paginate(
                Bucket=loc.bucket, Prefix=prefix, Delimiter="/"
            ):
                for cp in page.get("CommonPrefixes", []):
                    names.append(cp["Prefix"][len(prefix):].rstrip("/"))
        return names

    def organisations(self) -> list[str]:
        # With a per-organisation map the database already knows the answer, and
        # knows it better than S3 does: it includes organisations shared from
        # another account, which do not appear under this caller's own prefix,
        # and organisations stored in another region's bucket entirely.
        entitlement = self.principal.entitlement
        if entitlement.org_locations is not None:
            return sorted(entitlement.org_locations)

        own = OrgLocation(prefix=self.root, bucket=self._s.s3_bucket,
                          region=self._s.aws_region)
        found = sorted(self._list_folders(own, self.root))
        # If the account is entitled to a named subset rather than everything
        # under the prefix, filter here — one place, so no tool can skip it.
        return [o for o in found if entitlement.permits_organisation(o)]

    def _check_organisation(self, name: str) -> str:
        """Validate the caller's organisation name and return the canonical one.

        Some organisation names carry stray whitespace — "QBO restore 2 " is a
        real one — and the segment check strips it, so the cleaned name no longer
        matches its entitlement key and the organisation becomes unreachable
        despite being listed. Matching on the stripped form and then returning
        the database's own spelling keeps the S3 path exact while still refusing
        anything the caller invented."""
        cleaned = self._segment(name, "organisation")
        ent = self.principal.entitlement
        locations = ent.org_locations

        if locations is not None and cleaned not in locations:
            # Match on the stripped form, then case-insensitively, returning the
            # database's own spelling so the S3 path stays exact — "butter
            # studios" reaches "Butter Studios" instead of a false refusal.
            low = cleaned.lower()
            for canonical in locations:
                if canonical.strip() == cleaned or canonical.strip().lower() == low:
                    return canonical

        if not ent.permits_organisation(cleaned):
            available = sorted(locations) if locations else sorted(ent.organisations or [])
            hint = (" Your organisations: " + ", ".join(available) + ".") if available else ""
            raise PermissionError(
                f"{cleaned!r} isn't one of your organisations (names are matched "
                f"loosely on case).{hint}"
            )
        return cleaned

    def backup_dates(self, organisation: str) -> list[str]:
        org = self._check_organisation(organisation)
        loc = self._location_for(org)
        prefix = self._resolve(loc.prefix, org) + "/"
        folders = self._list_folders(loc, prefix)

        # Alongside the dated backups an organisation holds working folders —
        # "Attachment", "downloadables". They are not backups, and because they
        # sort above digits a plain reverse sort would make "latest" resolve to
        # one of them, quietly answering every question from the wrong place.
        dates = [name for name in folders if _DATE_FOLDER.fullmatch(name)]
        if dates:
            # Hide backups whose run did not finish (running/paused/failed):
            # the folder is written in progress and may hold partial data.
            if loc.incomplete_dates:
                dates = [d for d in dates if d not in loc.incomplete_dates]
            # And hide runs the database calls 'completed' that nonetheless
            # wrote nothing — empty in storage, nothing to read. The database's
            # file log cannot be trusted for this (it has marked a run complete
            # with 53 files when storage held 9), so ask storage itself.
            with_files = self._dates_with_files(loc, org)
            dates = [d for d in dates if d in with_files]
            # Descending: the most recent usable backup is the one wanted, and
            # trimming after the sort keeps the newest rather than an arbitrary
            # few. The trim is the server's visibility window: resolve_date()
            # validates against this list, so an older backup is unaddressable
            # everywhere, not just missing from the listing.
            return sorted(dates, reverse=True)[: self._s.max_backup_dates]
        # No date-shaped folders: a differently-laid-out area — list as found.
        # Windowed too: these are still offered to the caller as backups, and a
        # layout the code does not recognise is no reason to widen access.
        return sorted(folders, reverse=True)[: self._s.max_backup_dates]

    def _dates_with_files(self, loc: OrgLocation, organisation: str) -> set[str]:
        """Date folders that hold at least one readable file.

        One listing of the whole organisation prefix, bucketed by date. A run
        marked 'completed' in the database can still have written nothing to
        storage, and such an empty backup must not be offered as if it held
        data. A file is any object sitting directly under a date's data path,
        matching what `files()` returns; folder markers (keys ending in '/') do
        not count. One listing is cheaper than probing each date separately."""
        base = self._resolve(loc.prefix, organisation) + "/"
        sub = (loc.backup_subdir + "/") if loc.backup_subdir else ""
        paginator = self._client_for(loc.region).get_paginator("list_objects_v2")
        found: set[str] = set()
        with _s3_errors("this backup area"):
            for page in paginator.paginate(Bucket=loc.bucket, Prefix=base):
                for obj in page.get("Contents", []):
                    key = obj["Key"]
                    if key.endswith("/"):
                        continue
                    rest = key[len(base):]
                    slash = rest.find("/")
                    if slash < 0:
                        continue                    # object directly under org
                    date, tail = rest[:slash], rest[slash + 1:]
                    if sub:
                        if not tail.startswith(sub):
                            continue                # not under the data subdir
                        tail = tail[len(sub):]
                    if tail and "/" not in tail:     # a direct-child file
                        found.add(date)
        return found

    def resolve_date(self, organisation: str, backup_date: str) -> str:
        """Resolve 'latest' or a literal date to a *completed* backup date.

        Only completed backups inside the visibility window are addressable.
        'latest' picks the newest one; a literal date must name one exactly. An
        unfinished run, a backup older than the window, or a date with no backup
        at all, is reported as unavailable rather than served — a partial,
        out-of-window or absent backup must never read as if it were final. The
        message gives nothing away about internal state (a caller cannot tell a
        failed run from a date that never existed) and points at support."""
        completed = self.backup_dates(organisation)
        if backup_date.strip().lower() in {"latest", "newest", "last"}:
            if not completed:
                raise ValueError(
                    f"No completed backups are available for {organisation!r}. "
                    f"Please contact the WOW support team at support@wowzer.tech."
                )
            return completed[0]
        date = self._segment(backup_date, "backup_date")
        if date not in completed:
            if completed:
                # List the servable dates — the same ones `browse` shows, so this
                # leaks no internal state (a failed or in-progress run isn't here).
                avail = ", ".join(completed[:10]) + (" …" if len(completed) > 10 else "")
                raise ValueError(
                    f"{organisation!r} has no available backup dated {date}. "
                    f'Available dates: {avail}. Use one of these, or "latest".'
                )
            raise ValueError(
                f"No backup information is available for {organisation!r} dated "
                f"{date}. Please contact the WOW support team at "
                f"support@wowzer.tech."
            )
        return date

    def files(self, organisation: str, backup_date: str) -> list[ObjectInfo]:
        org = self._check_organisation(organisation)
        date = self._segment(backup_date, "backup_date")
        loc = self._location_for(org)
        segs = (org, date, loc.backup_subdir) if loc.backup_subdir else (org, date)
        prefix = self._resolve(loc.prefix, *segs) + "/"

        paginator = self._client_for(loc.region).get_paginator("list_objects_v2")
        out: list[ObjectInfo] = []
        with _s3_errors(f"backup {date} of {organisation}"):
            for page in paginator.paginate(
                Bucket=loc.bucket, Prefix=prefix, Delimiter="/"
            ):
                for obj in page.get("Contents", []):
                    if obj["Key"].endswith("/"):
                        continue
                    out.append(
                        ObjectInfo(
                            name=obj["Key"][len(prefix):],
                            size_bytes=obj["Size"],
                            last_modified=obj["LastModified"].isoformat(),
                        )
                    )
        return sorted(out, key=lambda o: o.name)

    # -- objects ------------------------------------------------------------ #

    def object_for(
        self, organisation: str, backup_date: str, filename: str
    ) -> ObjectRef:
        """Resolve one file to a bucket and key.

        A key alone stopped identifying an object once organisations were spread
        across regional buckets, so callers carry the pair."""
        org = self._check_organisation(organisation)
        date = self._segment(backup_date, "backup_date")
        name = self._segment(filename, "filename")
        loc = self._location_for(org)
        segs = ((org, date, loc.backup_subdir, name) if loc.backup_subdir
                else (org, date, name))
        return ObjectRef(
            bucket=loc.bucket,
            region=loc.region,
            key=self._resolve(loc.prefix, *segs),
        )

    def head(self, ref: ObjectRef) -> ObjectInfo:
        name = ref.key.rsplit("/", 1)[-1]
        with _s3_errors(name):
            r = self._client_for(ref.region).head_object(
                Bucket=ref.bucket, Key=ref.key
            )
        return ObjectInfo(
            name=name,
            size_bytes=r["ContentLength"],
            last_modified=r["LastModified"].isoformat(),
        )

    def fetch(self, ref: ObjectRef) -> bytes:
        cap = self._s.max_object_bytes
        name = ref.key.rsplit("/", 1)[-1]
        client = self._client_for(ref.region)
        with _s3_errors(name):
            meta = client.head_object(Bucket=ref.bucket, Key=ref.key)
            if meta["ContentLength"] > cap:
                raise ValueError(
                    f"file is {meta['ContentLength']:,} bytes, over the {cap:,} byte "
                    f"limit this server will read inline — describe_file still "
                    f"works, and a narrower file from the same backup may not hit "
                    f"the limit"
                )
            return client.get_object(Bucket=ref.bucket, Key=ref.key)["Body"].read()

