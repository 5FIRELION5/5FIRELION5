import requests, sys, traceback, re
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

pid = '1503014'
url = f'https://ratings.fide.com/profile/{pid}/statistics'
headers = {"User-Agent": "Mozilla/5.0"}

timeout = 30
session = requests.Session()
retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
session.mount('https://', HTTPAdapter(max_retries=retries))

try:
    r = session.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    s = BeautifulSoup(r.text, "html.parser")
    found = False
    for tag in s.find_all(id=re.compile(r"stackedChartID\d+")):
        found = True
        print("---- FOUND TAG ----")
        print("TAG:", tag.name, "id=", tag.get("id"))
        # print a short snippet of the tag's HTML (limited)
        snippet = str(tag)[:2000]
        print("HTML snippet (truncated):")
        print(snippet)
        print("--- nearby previous texts ---")
        for prev in tag.find_all_previous(limit=12):
            t = prev.get_text(" ", strip=True)
            if t:
                print("-", repr(t)[:400])
        print("--- search inside tag for WHITE/BLACK lines ---")
        for node in tag.find_all():
            txt = node.get_text(" ", strip=True)
            if txt and ('WHITE' in txt.upper() or 'BLACK' in txt.upper()):
                print("node:", repr(txt)[:800])
    if not found:
        print("No stackedChartID elements found.")
except Exception:
    print("Exception while fetching stats HTML:")
    traceback.print_exc()
    sys.exit(1)
