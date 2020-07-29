#!/usr/bin/env python3
"""This script downloads the latest data from:

 - German corona-warn app (exported exposures)
 - Austrian stop-corona app (exported exposures)
 - Austrian department of health: https://info.gesundheitsministerium.at/

"""
import requests
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Union, List
import re
import urllib3
import threading
import queue

# info.gesundheitsministerium.at uses short DH keys, and requests prevents
# them by default

# https://stackoverflow.com/questions/38015537/python-requests-exceptions-sslerror-dh-key-too-small
requests.packages.urllib3.disable_warnings()
requests.packages.urllib3.util.ssl_.DEFAULT_CIPHERS += ':HIGH:!DH:!aNULL'
try:
    requests.packages.urllib3.contrib.pyopenssl.util.ssl_.DEFAULT_CIPHERS += ':HIGH:!DH:!aNULL'
except AttributeError:
    # no pyopenssl support used / needed / available
    pass

now = datetime.utcnow()
print(f"Starting script at {now:%Y-%m-%d %H:%M:%S} UTC")
sess = requests.session()

# Base URL for austrian stop-corona app exposures
AT_BASE_URL = 'https://cdn.prod-rca-coronaapp-fd.net'
at_index = sess.get(f'{AT_BASE_URL}/exposures/at/index.json').json()

DE_COUNTRY_URL = 'https://svc90.main.px.t-online.de/version/v1/diagnosis-keys/country'

root = Path(__file__).parent

DATE_FORMAT = "%Y-%m-%d"
DATETIME_FORMAT = "%Y-%m-%d_%H-%M"

# A queue with jobs that will be executed asynchronously (added in save_to_path)
to_save = queue.Queue()

def save_to_path(url: str, path: Path, check_path: Optional[Path] = None) -> None:
    """Lazily GET `url` and save the result in `path`

    url: The url to GET
    path: The path to save the result at
    check_path: If the content of this path matches the request content, skip saving.
    """
    def func(sess: requests.Session):
        resp = sess.get(url)
        resp.raise_for_status()
        if check_path is not None and check_path.is_file():
            if resp.content== check_path.read_bytes():
                return
        if path.is_file():
            if resp.content != path.read_bytes():
                print(f"WARN: Not same {path}!")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)
        print(f"Saved {path}")

    to_save.put(func)


warn_folder = root / 'de-corona-warn-data'
# for each country indexed by corona warn app
for country in sess.get(f"{DE_COUNTRY_URL}").json():
    country_folder = warn_folder / country

    # for each date for the given country
    for date in sess.get(f"{DE_COUNTRY_URL}/{country}/date").json():
        # Get the date as a datetime object
        dt = datetime.strptime(date, "%Y-%m-%d")

        date_folder = country_folder / '1d' / dt.strftime(DATE_FORMAT)
        date_save_file = date_folder / f'{now.strftime(DATETIME_FORMAT)}.zip'

        # Use the last file in the date folder as check_path (new file won't be
        # written if the content matches with this file)
        last_file = list(sorted(date_folder.glob('*.zip')))
        check_path = last_file[-1] if last_file else None

        url = f"{DE_COUNTRY_URL}/{country}/date/{date}"
        save_to_path(url, date_save_file, check_path=check_path)

        # for each hour interval for the given country and date
        for hour in sess.get(f"{DE_COUNTRY_URL}/{country}/date/{date}/hour").json():
            hour_folder = country_folder / '1h' / f'{dt.strftime(DATE_FORMAT)}_{hour:02d}-00'
            save_file = hour_folder / f'{now.strftime(DATETIME_FORMAT)}.zip'

            last_file = list(sorted(hour_folder.glob(f'*.zip')))
            check_path = last_file[-1] if last_file else None
            save_to_path(f"{url}/hour/{hour}", save_file, check_path=check_path)


def at_save(days: int, batch: Dict[str, Union[int, List[str]]]) -> None:
    """Save austrian stop-corona app exposures"""
    dt = datetime.utcfromtimestamp(batch["interval"]*600)
    for url_path in batch["batch_file_paths"]:
        timestamp = int(re.search(r'/(15\d{8})/', url_path).group(1))
        down_t = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d_%H-%M')
        path = root / 'at-stop-corona-data' / f'{days}d' / f'{dt.strftime(DATE_FORMAT)}' / f'{down_t}.zip'
        save_to_path(f"{AT_BASE_URL}{url_path}", path)


# Save austrian stop-corona exposure exports
at_save(14, at_index["full_14_batch"])
at_save(7, at_index["full_7_batch"])
for day in at_index["daily_batches"]:
    at_save(1, day)

# Save info.gesundheitsministerium.at data
info_folder = root / 'at-info-data'
old_path = list(info_folder.glob('*.zip'))
old_path.sort()
path = info_folder / f'{datetime.utcnow():%Y-%m-%d_%H-%M}.zip'
save_to_path('https://info.gesundheitsministerium.at/data/data.zip', path, old_path[-1])


# Asynchronously process requests from to_save

def worker():
    sess = requests.session()
    while True:
        item = to_save.get()
        if item is None:
            break
        try:
            item(sess)
        except Exception as exc:
            print(exc)
        to_save.task_done()

threads = []
NUM_WORKERS = 4
for i in range(NUM_WORKERS):
    t = threading.Thread(target=worker)
    t.start()
    threads.append(t)

to_save.join()
for i in range(NUM_WORKERS):
    to_save.put(None)
for t in threads:
    t.join()

print("Done")
print()
