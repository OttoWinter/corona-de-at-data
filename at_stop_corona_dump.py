#!/usr/bin/env python3
from datetime import datetime, timedelta
import format_pb2 as pb
import zipfile
from pprint import pprint
import collections
from pathlib import Path
from pprint import pprint
import hashlib
from dataclasses import dataclass
from typing import Optional, List, Set, Dict

@dataclass(frozen=True)
class TEK:
    """A temporary exposure key."""
    data: bytes
    interval: int
    level: int
    period: int

    @property
    def dt(self) -> datetime:
        return datetime.utcfromtimestamp(self.interval*600)

    @property
    def dt_end(self) -> datetime:
        return self.dt + timedelta(minutes=self.period*10)

    @property
    def sha(self) -> str:
        """A short textual representation of the key data."""
        return hashlib.sha256(self.data).hexdigest()[:8]

    def format(self, added_dt: datetime) -> str:
        """Format the key in a format suitable for a cli."""
        sha = {2: "\033[1;33m", 5: "\033[1;31m"}.get(self.level, '')
        sha += f'{self.sha}'
        age = (added_dt-self.dt).days
        extra = [f'age: {age}d']
        dt_s = self.dt.strftime("%a\033[0m %Y.%m.%d")
        if self.period != 144:
            extra.append(f'period: {self.period*10:.0f} min')
        if self.dt.hour != 0 or self.dt.minute != 0:
            dt_s = self.dt.strftime("%a\033[0m %Y.%m.%d %H:%M")
        extra_s = ', '.join(extra)
        return f"{sha} {dt_s} ({extra_s})"

@dataclass(frozen=True)
class Archive:
    path: Path

    def load_keys(self) -> List[TEK]:
        with self.path.open('rb') as fh:
            with zipfile.ZipFile(fh) as archive:
                with archive.open('export.bin') as export:
                    export_bin = export.read()
        proto_bin = export_bin[16:]
        data = pb.TemporaryExposureKeyExport()
        data.ParseFromString(proto_bin)
        return [TEK(data=key.key_data,
                    interval=key.rolling_start_interval_number,
                    level=key.transmission_risk_level,
                    period=key.rolling_period)
                for key in data.keys]

    @property
    def dt(self) -> datetime:
        # Dirty hack to display in local timezone instead of UTC
        return datetime.strptime(self.path.name, "%Y-%m-%d_%H-%M.zip") + timedelta(hours=2)


@dataclass(frozen=True)
class Event:
    """Base class for an event that could be extracted from the key exports."""
    # The tek associated with the event
    key: TEK
    # When the event occured
    dt: datetime

@dataclass(frozen=True)
class RevokedEvent(Event):
    """A key was revoked."""
    # When the key was first added
    added_dt: datetime

@dataclass(frozen=True)
class AddedEvent(Event):
    """A key was added."""
    # Was key metadata (risk level) changed later?
    changed: bool

@dataclass(frozen=True)
class AutoRemovedEvent(Event):
    """A key was automatically removed."""
    # When the key was first added.
    added_dt: datetime


root = Path(__file__).parent
at_dir = root / 'at-stop-corona-data'
at_7d_dir = at_dir / '7d'
at_14d_dir = at_dir / '14d'
at_7d_files = list(sorted(at_7d_dir.glob('*/*.zip')))
last_archive = Archive(path=list(sorted(at_14d_dir.glob('*/*.zip')))[-1])
archives = [Archive(path=p) for p in at_14d_files]

# All keys that were ever added
all_keys: Set[TEK] = set()
# Set of all keys that were revoked at some point
revoked_keys: Set[TEK] = set()
# Keys from previous archive that was loaded
prev_keys: Optional[List[TEK]] = None
# All events that were detected
events: List[Event] = []
# The datetime of the previous archive
prev_dt: Optional[datetime] = None
# When a TEK was added
added_when: Dict[TEK, datetime] = {}
# A list of the datetimes of archives
dts: List[datetime] = []

# Process archives and extract events
for archive in archives:
    dt = archive.dt
    now_keys = archive.load_keys()
    dts.append(dt)
    if prev_keys is None:
        prev_keys = now_keys
        prev_dt = dt
        continue

    # stop-corona-app adds random padding keys
    # use intersection with previous keys to get "true" keys
    inter = list(set(now_keys) & set(prev_keys))
    inter.sort(key=lambda key: 1000 * key.level + key.interval)

    # Find ADD events
    for key in inter:
        if key not in all_keys:
            added_when[key] = prev_dt
            changed = any(key.data == o.data for o in all_keys)
            events.append(AddedEvent(key=key, dt=prev_dt, changed=changed))
            all_keys.add(key)

    # Find REVOKE/AUTO_REMOVE events
    for key in list(all_keys):
        if key not in inter:
            age = dt-key.dt
            if age <= timedelta(days=8):
                events.append(RevokedEvent(key=key, dt=dt, added_dt=added_when[key]))
                revoked_keys.add(key)
            else:
                events.append(AutoRemovedEvent(key=key, dt=dt, added_dt=added_when[key]))
            all_keys.remove(key)

    prev_keys = now_keys
    prev_dt = dt

# For last archive, we can't compare against a next archive for intersection
# but we can use the 14d archive
now_keys = last_archive.load_keys()
inter = list(set(now_keys) & set(prev_keys))
inter.sort(key=lambda key: 1000 * key.level + key.interval)
for key in inter:
    if key not in all_keys:
        added_when[key] = prev_dt
        changed = any(key.data == o.data for o in all_keys)
        events.append(AddedEvent(key=key, dt=prev_dt, changed=changed))
        all_keys.add(key)


# ======= Output events in human-readable form """"

# Sort events
events.sort(key=lambda e: (e.dt, str(type(e)), e.key.level, e.key.dt))


# How many unrevoked keys were seen at the given interval (date)?
tot_days_count: Dict[int, int] = collections.defaultdict(int)
for dt in dts:
    # filter events by dt
    gevents = [e for e in events if e.dt == dt]
    print(f"{dt:%a %Y.%m.%d %H:%M}:")

    days_count = collections.defaultdict(int)
    for event in gevents:
        key = event.key
        if isinstance(event, RevokedEvent):
            adt = event.added_dt
            delay = (dt-adt).total_seconds() / (60*60)
            print(f"  REVOKE {key.format(dt)} added at {adt:%a %Y.%m.%d %H:%M} (delay: {delay:.0f}h)")
        elif isinstance(event, AddedEvent):
            revoked = key in revoked_keys
            print(f"  ADD {key.format(dt)}" + (" (REVOKED)" if revoked else "") + (" (CHANGED)" if event.changed else ""))
            if key.level == 5 and not revoked:
                days_count[key.interval] += 1
                tot_days_count[key.interval] += 1
        elif isinstance(event, AutoRemovedEvent):
            adt = event.added_dt
            print(f"  AUTO REMOVE {key.format(dt)} added at {adt:%a %Y.%m.%d %H:%M}")
        else:
            assert False

    if days_count:
        print(f"  At least {max(days_count.values())} people diagnosed.")
    print()

print()
print("Total unrevoked red keys seen on:")
for intnum, count in sorted(tot_days_count.items()):
    dt = datetime.utcfromtimestamp(intnum*600)
    print(f"  {dt:%a %Y.%m.%d}: {count:5d}")
print(f"At least {max(tot_days_count.values())} people diagnosed.")
