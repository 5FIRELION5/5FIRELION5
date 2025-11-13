import requests, json, sys, traceback
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

pid = '1503014'  # change if needed
url = f'https://ratings.fide.com/a_data_stats.php?id1={pid}&id2=0'
headers = {"User-Agent": "Mozilla/5.0", "Referer": f"https://ratings.fide.com/profile/{pid}/statistics", "X-Requested-With": "XMLHttpRequest"}

timeout = 30
# session with retries to avoid transient network/read timeouts
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
session.mount('https://', HTTPAdapter(max_retries=retries))

try:
    r = session.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    j = r.json()
    if not j:
        print("JSON empty")
        sys.exit(0)
    entry = j[0]
    print("JSON keys and sample values (first entry):")
    for k, v in entry.items():
        print(f"{k}: {v}")
except Exception:
    print("Exception while fetching stats JSON:")
    traceback.print_exc()
    sys.exit(1)
