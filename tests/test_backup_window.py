"""The newest-N window on backup_dates(), and that it governs every read.

resolve_date() validates a caller's date against backup_dates(), and every tool
taking a backup_date goes through resolve_date(). So the window is enforced in
one place — these tests pin that, and pin that trimming keeps the *newest*.
"""

import sys
import pytest

from s3_mcp.storage import OrgLocation, UserStore
from s3_mcp.settings import get_settings


def store_with(dates, *, window=None, date_shaped=True):
    """A UserStore whose storage holds exactly `dates`, no AWS involved."""
    s = UserStore.__new__(UserStore)
    s._s = get_settings()
    if window is not None:
        s._s = s._s.model_copy(update={"max_backup_dates": window})
    loc = OrgLocation(prefix="acct@example.com/", bucket="b", region="ca-central-1")
    s._check_organisation = lambda name: name
    s._location_for = lambda org: loc
    s._resolve = lambda root, *segs: "/".join([root.rstrip("/"), *segs])
    s._list_folders = lambda l, p: list(dates)
    s._dates_with_files = lambda l, o: set(dates)
    return s


DATES = [
    "2026-08-12", "2026-08-11", "2026-08-10", "2026-08-09", "2026-08-08",
    "2026-08-07", "2026-08-06", "2026-08-05", "2026-05-08", "2026-05-07",
]


def test_window_is_seven_by_default():
    assert get_settings().max_backup_dates == 7


def test_lists_at_most_the_window():
    assert len(store_with(DATES).backup_dates("Org")) == 7


def test_keeps_the_newest_not_an_arbitrary_seven():
    assert store_with(DATES).backup_dates("Org") == DATES[:7]


def test_shorter_history_is_untouched():
    few = DATES[:3]
    assert store_with(few).backup_dates("Org") == few


def test_unrecognised_layout_is_windowed_too():
    folders = [f"area-{i:02d}" for i in range(12)]
    got = store_with(folders).backup_dates("Org")
    assert got == sorted(folders, reverse=True)[:7]


def test_latest_still_resolves_to_the_newest():
    assert store_with(DATES).resolve_date("Org", "latest") == "2026-08-12"


def test_in_window_date_resolves():
    assert store_with(DATES).resolve_date("Org", "2026-08-06") == "2026-08-06"


def test_date_outside_the_window_is_unavailable():
    """The eighth-newest exists in storage but must not be readable — this is
    what stops describe_file/preview_file/aggregate_file reaching past it."""
    with pytest.raises(ValueError, match="no available backup dated"):
        store_with(DATES).resolve_date("Org", "2026-08-05")


def test_out_of_window_is_indistinguishable_from_absent():
    """Same message either way: a caller cannot probe what lies behind it."""
    store = store_with(DATES)
    behind = out = None
    try:
        store.resolve_date("Org", "2026-05-07")   # real, outside the window
    except ValueError as e:
        behind = str(e).replace("2026-05-07", "D")
    try:
        store.resolve_date("Org", "2020-01-01")   # never existed
    except ValueError as e:
        out = str(e).replace("2020-01-01", "D")
    assert behind == out


def test_window_is_configurable():
    assert len(store_with(DATES, window=3).backup_dates("Org")) == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
