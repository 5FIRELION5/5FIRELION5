import importlib.machinery
import importlib.util
import sys
import sysconfig
import os

# Ensure stdlib http.client is available before importing requests. Prefer a
# normal import; if that fails (workspace accidentally shadowed stdlib) then
# fall back to loading the stdlib source file and registering it so code that
# checks `hasattr(http, 'client')` sees the submodule.
try:
	import http.client  # noqa: F401
except Exception:
	try:
		if 'http.client' not in sys.modules:
			stdlib = sysconfig.get_paths().get('stdlib')
			if stdlib:
				candidate = os.path.join(stdlib, 'http', 'client.py')
				if os.path.exists(candidate):
					loader = importlib.machinery.SourceFileLoader('http.client', candidate)
					spec = importlib.util.spec_from_loader(loader.name, loader)
					module = importlib.util.module_from_spec(spec)
					loader.exec_module(module)
					# register under sys.modules and also attach to the http package
					sys.modules['http.client'] = module
					try:
						import importlib
						http_pkg = importlib.import_module('http')
						setattr(http_pkg, 'client', module)
					except Exception:
						pass
	except Exception:
		# best-effort; allow normal imports to raise the usual errors later
		pass

import requests
from bs4 import BeautifulSoup
from urllib.parse import quote_plus, urljoin, urlparse, parse_qs
import os
import json
import time
import re
try:
	import pandas as pd
except Exception:
	pd = None

# use the shared http helper (local module http_client.py)
import http_client as http_client

BASE = "https://ratings.fide.com/"


def search_player(name, timeout=10):
	"""Search FIDE for players matching `name`.

	Returns a list of dicts: {name, player_id, profile_url}.
	"""
	# The visible search page uses JavaScript to load results from an include endpoint.
	# Call that endpoint directly to get search results HTML.
	results = []
	seen = set()

	search_url = urljoin(BASE, f"incl_search_l.php?search={quote_plus(name)}&simple=1")
	headers = {
		"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
		"Referer": urljoin(BASE, f"search.phtml?search={quote_plus(name)}"),
		"X-Requested-With": "XMLHttpRequest",
	}
	r = http_client.get(search_url, headers=headers, timeout=timeout)
	soup = BeautifulSoup(r.text, "html.parser")

	for a in soup.find_all("a", href=True):
		href = a["href"]
		pid = None
		# new site uses /profile/<id> links in the AJAX include; support both formats
		if "/profile/" in href:
			# extract trailing number
			parsed = urlparse(href)
			try:
				pid = parsed.path.rstrip("/").split("/")[-1]
			except Exception:
				pid = None
		elif "profile.php" in href and "player=" in href:
			parsed = urlparse(href)
			pid = parse_qs(parsed.query).get("player", [None])[0]

		if not pid:
			continue
		if pid in seen:
			continue
		seen.add(pid)
		full = urljoin(BASE, href)
		display_name = a.text.strip() or None
		results.append({"name": display_name, "player_id": pid, "profile_url": full})

	# If no results from the AJAX include (site changed), fall back to the static search page
	if not results:
		fallback_url = urljoin(BASE, f"search.phtml?search={quote_plus(name)}")
		# perform fallback request
		r2 = http_client.get(fallback_url, headers=headers, timeout=timeout)
		soup2 = BeautifulSoup(r2.text, "html.parser")
		for a in soup2.find_all("a", href=True):
			href = a["href"]
			if "profile.php" in href and "player=" in href:
				parsed = urlparse(href)
				pid = parse_qs(parsed.query).get("player", [None])[0]
				if not pid:
					continue
				if pid in seen:
					continue
				seen.add(pid)
				full = urljoin(BASE, href)
				display_name = a.text.strip() or None
				results.append({"name": display_name, "player_id": pid, "profile_url": full})

	return results


def get_player_profile(player_id, timeout=10, profile_url=None):
	"""Fetch a player's FIDE profile and extract available stats.

	Returns a dict with keys like: name, title, federation, ratings (dict), and raw_profile_url.
	Note: FIDE public pages do not necessarily expose total wins/losses per player; those fields
	will be returned as None when not available.
	"""
	# Accept either a numeric player_id or a full profile_url. Try the newer
	# /profile/<id> path first (site uses that now); fall back to the older
	# profile.php?player= format.
	if profile_url:
		url = profile_url
	else:
		# try /profile/<id> first
		url = urljoin(BASE, f"profile/{player_id}")

	# --- simple in-memory + on-disk cache for profiles ---
	CACHE_TTL = 60 * 5  # 5 minutes
	CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
	try:
		os.makedirs(CACHE_DIR, exist_ok=True)
	except Exception:
		pass

	cache_key = f"profile:{player_id}"

	# in-memory cache dictionary (module-level)
	if not hasattr(get_player_profile, "_memcache"):
		get_player_profile._memcache = {}

	mem = get_player_profile._memcache
	now = time.time()
	# check memory cache
	if cache_key in mem:
		exp, val = mem[cache_key]
		if exp > now:
			return val.copy()
		else:
			del mem[cache_key]

	# check disk cache
	cache_file = os.path.join(CACHE_DIR, f"{player_id}.json")
	if os.path.exists(cache_file):
		try:
			with open(cache_file, "r", encoding="utf-8") as fh:
				obj = json.load(fh)
			if obj.get("_cached_at") and (now - obj.get("_cached_at", 0) < CACHE_TTL):
				# populate memcache and return
				val = obj.get("data")
				mem[cache_key] = (now + CACHE_TTL, val)
				return val.copy()
		except Exception:
			pass

	# fetch the profile page
	r = http_client.get(url, timeout=timeout)
	if r.status_code == 404:
		# fallback to legacy URL format
		url = BASE + f"profile.php?player={player_id}"
		r = http_client.get(url, timeout=timeout)
	soup = BeautifulSoup(r.text, "html.parser")

	data = {"player_id": player_id, "raw_profile_url": url}

	# Try to get the full name from the page title or h1
	name = None
	if soup.title and soup.title.string:
		name = soup.title.string.strip()
	h1 = soup.find("h1")
	if h1 and h1.text.strip():
		name = h1.text.strip()
	data["name"] = name

	# Ratings: look for text nodes mentioning Standard, Rapid, Blitz
	ratings = {}
	# Simple approach: parse tables / rows that contain rating types
	for label in ["Standard", "Rapid", "Blitz"]:
		# find any element that has this label
		found = None
		for tag in soup.find_all():
			if tag.string and label in tag.string:
				found = tag
				break
		if found:
			# try to find nearby numeric rating
			# check siblings, parent siblings, or next elements
			rating = None
			# check next sibling text
			sib = found.find_next(string=True)
			if sib and sib.strip().isdigit():
				rating = sib.strip()
			else:
				# try to find a number in the parent
				parent_text = found.parent.get_text(separator=" ", strip=True)
				import re

				m = re.search(r"(\d{3,4})", parent_text)
				if m:
					rating = m.group(1)
			ratings[label.lower()] = rating
		else:
			ratings[label.lower()] = None

	data["ratings"] = ratings

	# Wins/losses and per-color stats are not consistently published on FIDE public pages.
	# We'll try to look for any summary tables, but default to None.
	data["total_wins"] = None
	data["total_draws"] = None
	data["total_losses"] = None
	data["win_ratio"] = None
	data["loss_ratio"] = None
	# per_color will be either simple totals (old behavior) or a dict with details
	# after we fetch structured JSON: {"white": {total,wins,draws,losses}, "black": {...}}
	data["per_color"] = {"white": None, "black": None}

	# Try to fetch the statistics page which contains richer, structured data
	def extract_number_from_text(s):
		import re

		if not s:
			return None
		m = re.search(r"(\d{1,4})", s.replace(',', ''))
		return m.group(1) if m else None

	def extract_nearby_number(tag):
		"""Given a BeautifulSoup tag, look nearby (siblings, parent, next elements)
		for a 1-4 digit number and return it as a string."""
		import re

		if not tag:
			return None
		# check direct text
		text = tag.get_text(" ", strip=True)
		m = re.search(r"(\d{1,4})", text.replace(',', ''))
		if m:
			return m.group(1)
		# check next siblings
		for sib in tag.next_siblings:
			if isinstance(sib, str):
				m = re.search(r"(\d{1,4})", sib.replace(',', ''))
				if m:
					return m.group(1)
			else:
				m = re.search(r"(\d{1,4})", sib.get_text(" ", strip=True).replace(',', ''))
				if m:
					return m.group(1)
		# check parent text
		if tag.parent:
			m = re.search(r"(\d{1,4})", tag.parent.get_text(" ", strip=True).replace(',', ''))
			if m:
				return m.group(1)
		return None

	def fetch_statistics_page(pid, profile_url=None):
		# try common statistics paths
		urls = []
		if profile_url and profile_url.rstrip('/').endswith(str(pid)):
			urls.append(profile_url.rstrip('/') + '/statistics')
		urls.append(urljoin(BASE, f"profile/{pid}/statistics"))
		urls.append(urljoin(BASE, f"profile.php?player={pid}&action=statistics"))
		urls.append(urljoin(BASE, f"profile.php?player={pid}&statistics=1"))

		headers = {"User-Agent": "Mozilla/5.0"}
		for u in urls:
			try:
				rr = http_client.get(u, headers=headers, timeout=timeout)
				if rr.status_code == 200 and rr.text:
					return u, BeautifulSoup(rr.text, "html.parser")
			except Exception:
				continue
		return None, None

	stats_url, stats_soup = fetch_statistics_page(player_id, profile_url=profile_url)
	if stats_soup:
		# Precise ratings extraction: look for table cells (td) containing the rating labels
		label_map = {
			"standard": ["Standard", "Std."],
			"rapid": ["Rapid", "Rpd."],
			"blitz": ["Blitz", "Blz."],
		# no bullet rating on stats page (omit)
		}
		# initialize as None
		for k in label_map:
			data["ratings"].setdefault(k, None)

		for td in stats_soup.find_all("td"):
			text = td.get_text(" ", strip=True)
			if not text:
				continue
			for key, labels in label_map.items():
				if any(lbl in text for lbl in labels):
					# prefer strong tag if present
					strong = td.find("strong")
					if strong and strong.text.strip():
						val = strong.text.strip().replace(',', '')
						if val.isdigit():
							data["ratings"][key] = int(val)
							continue
					# fallback: extract first 3-4 digit number from the cell
					import re
					m = re.search(r"(\d{3,4})", text.replace(',', ''))
					if m:
						data["ratings"][key] = int(m.group(1))
						continue

		# Structured aggregate stats: the page uses an AJAX JSON endpoint we can call
		json_url = urljoin(BASE, f"a_data_stats.php?id1={player_id}&id2=0")
		headers = {"User-Agent": "Mozilla/5.0", "Referer": stats_url, "X-Requested-With": "XMLHttpRequest"}
		try:
			jr = http_client.get(json_url, headers=headers, timeout=timeout)
			j = jr.json()
		except Exception:
			j = None

		if j and isinstance(j, list) and len(j) > 0:
			entry = j[0]
			# parse totals and per-color
			w_total = int(entry.get("white_total", 0))
			b_total = int(entry.get("black_total", 0))
			w_wins = int(entry.get("white_win_num", 0))
			w_draws = int(entry.get("white_draw_num", 0))
			b_wins = int(entry.get("black_win_num", 0))
			b_draws = int(entry.get("black_draw_num", 0))
			total_games = w_total + b_total
			wins = w_wins + b_wins
			draws = w_draws + b_draws
			losses = total_games - wins - draws if total_games > 0 else None
			data["total_wins"] = wins
			data["total_draws"] = draws
			data["total_losses"] = losses
			# provide detailed per-color breakdown
			w_losses = w_total - w_wins - w_draws if w_total > 0 else None
			b_losses = b_total - b_wins - b_draws if b_total > 0 else None
			data["per_color"] = {
				"white": {"total": w_total, "wins": w_wins, "draws": w_draws, "losses": w_losses},
				"black": {"total": b_total, "wins": b_wins, "draws": b_draws, "losses": b_losses},
			}
			if total_games:
				data["win_ratio"] = f"{round(wins/total_games*100,1)}%"
				data["loss_ratio"] = f"{round((losses/total_games*100),1)}%" if losses is not None else None
				data["draw_ratio"] = f"{round((draws/total_games*100),1)}%"

			# Build per-format stats from JSON keys if present. Common suffixes used
			# on the site are: _std (standard), _rpd (rapid), _blz (blitz). Keys often
			# look like 'white_total_std', 'white_win_num_std', 'black_draw_num_rpd', etc.
			suffix_map = {"_std": "standard", "_rpd": "rapid", "_blz": "blitz"}
			formats = {"standard": {}, "rapid": {}, "blitz": {}}
			for k, v in entry.items():
				if not k or v is None:
					continue
				for suf, fmt in suffix_map.items():
					if k.endswith(suf):
						base = k[:-len(suf)].strip('_')
						# parse value safely
						try:
							ival = int(str(v).replace(',', ''))
						except Exception:
							continue
						parts = base.split('_')
						scope = None
						metric = None
						if parts[0] in ("white", "black"):
							scope = parts[0]
							metric_raw = '_'.join(parts[1:])
						else:
							scope = 'both'
							metric_raw = base
						# normalize metric names
						mr = metric_raw.lower()
						if mr in ("total", "total_games"):
							metric = 'total'
						elif 'win' in mr:
							metric = 'wins'
						elif 'draw' in mr:
							metric = 'draws'
						elif 'loss' in mr:
							metric = 'losses'
						else:
							# unknown metric
							metric = None
						if not metric:
							continue
						# store value
						if scope == 'both':
							formats[fmt].setdefault('both', {})[metric] = ival
						else:
							formats[fmt].setdefault(scope, {})[metric] = ival

			# normalize formats entries: compute totals and losses when possible
			for fmt, d in list(formats.items()):
				both = d.get('both', {})
				w = d.get('white', {})
				b = d.get('black', {})
				# compute totals/wins/draws
				fmt_total = both.get('total') if 'total' in both else None
				fmt_wins = both.get('wins') if 'wins' in both else None
				fmt_draws = both.get('draws') if 'draws' in both else None
				# fallback to summing white+black
				if fmt_total is None:
					if (w.get('total') is not None) or (b.get('total') is not None):
						fmt_total = (w.get('total') or 0) + (b.get('total') or 0)
				if fmt_wins is None:
					fmt_wins = (w.get('wins') or 0) + (b.get('wins') or 0)
				if fmt_draws is None:
					fmt_draws = (w.get('draws') or 0) + (b.get('draws') or 0)
				fmt_losses = None
				if fmt_total is not None:
					fmt_losses = fmt_total - (fmt_wins or 0) - (fmt_draws or 0)
				# compute per-color losses when possible
				for side_dict in (w, b):
					if isinstance(side_dict, dict):
						if ('losses' not in side_dict or side_dict.get('losses') is None) and side_dict.get('total') is not None:
							wins_v = side_dict.get('wins')
							draws_v = side_dict.get('draws')
							if wins_v is not None and draws_v is not None:
								side_dict['losses'] = side_dict['total'] - wins_v - draws_v
				formats[fmt] = {'total': fmt_total, 'wins': fmt_wins, 'draws': fmt_draws, 'losses': fmt_losses, 'white': (w or None), 'black': (b or None)}

			data['formats'] = formats

			# If formats appear missing from the JSON, try to parse them from the
			# statistics HTML (the stacked-bar charts and labels live there). This
			# is more reliable for per-format white/black win-draw-loss breakdowns
			# when the JSON does not include explicit format keys.
			if stats_soup:
				import re
				# First: try the header/container search (existing approach)
				for fmt in ["Standard", "Rapid", "Blitz"]:
					key = fmt.lower()
					# try to find a header mentioning e.g. 'Standard Games' or 'Standard'
					header = None
					for tag in stats_soup.find_all(['h1', 'h2', 'h3', 'div', 'span', 'p']):
						txt = tag.get_text(" ", strip=True)
						if not txt:
							continue
						low = txt.lower()
						if fmt.lower() in low and 'game' in low:
							header = tag
							break

					if not header:
						continue

					# scan inside the header's container for 'WHITE' and 'BLACK' rows that contain numbers
					white = None
					black = None
					container = header.parent or header
					for tag in container.find_all():
						text = tag.get_text(" ", strip=True)
						if not text:
							continue
						u = text.upper()
						if 'WHITE' in u and white is None:
							nums = re.findall(r"\d+", text.replace(',', ''))
							# expected order in chart: wins, draw, loss
							if len(nums) >= 3:
								w_w, w_d, w_l = map(int, nums[:3])
								white = {"total": w_w + w_d + w_l, "wins": w_w, "draws": w_d, "losses": w_l}
							elif len(nums) == 1:
								white = {"total": int(nums[0]), "wins": None, "draws": None, "losses": None}
						if 'BLACK' in u and black is None:
							nums = re.findall(r"\d+", text.replace(',', ''))
							if len(nums) >= 3:
								b_w, b_d, b_l = map(int, nums[:3])
								black = {"total": b_w + b_d + b_l, "wins": b_w, "draws": b_d, "losses": b_l}
							elif len(nums) == 1:
								black = {"total": int(nums[0]), "wins": None, "draws": None, "losses": None}

					# if we found either white or black details from HTML, create the format entry
					if white or black:
						fw = white or {}
						fb = black or {}
						ftotal = (fw.get('total') or 0) + (fb.get('total') or 0)
						fwins = (fw.get('wins') or 0) + (fb.get('wins') or 0) if (fw.get('wins') is not None or fb.get('wins') is not None) else None
						fdraws = (fw.get('draws') or 0) + (fb.get('draws') or 0) if (fw.get('draws') is not None or fb.get('draws') is not None) else None
						flosses = (fw.get('losses') or 0) + (fb.get('losses') or 0) if (fw.get('losses') is not None or fb.get('losses') is not None) else None
						formats[key] = {"total": ftotal if ftotal > 0 else None, "wins": fwins, "draws": fdraws, "losses": flosses, "white": fw or None, "black": fb or None}

				# Second: look for stackedChartID elements (some pages render format charts with these ids)
				for tag in stats_soup.find_all(id=re.compile(r"stackedChartID\d+")):
					# try to derive the format name from nearby text or attributes
					label = None
					for prev in tag.find_all_previous(limit=8):
						txt = prev.get_text(" ", strip=True).lower()
						if 'standard' in txt:
							label = 'standard'
							break
						if 'rapid' in txt:
							label = 'rapid'
							break
						if 'blitz' in txt:
							label = 'blitz'
							break
					# try attributes if not found
					if not label:
						for attr in ('aria-label', 'data-title', 'title'):
							val = tag.get(attr)
							if val:
								vl = val.lower()
								if 'standard' in vl:
									label = 'standard'
									break
								if 'rapid' in vl:
									label = 'rapid'
									break
								if 'blitz' in vl:
									label = 'blitz'
									break
					if not label:
						continue

					# search within tag for WHITE/BLACK numeric rows
					white = None
					black = None
					for tnode in tag.find_all():
						text = tnode.get_text(" ", strip=True)
						if not text:
							continue
						u = text.upper()
						nums = re.findall(r"\d+", text.replace(',', ''))
						if 'WHITE' in u and white is None:
							if len(nums) >= 3:
								w_w, w_d, w_l = map(int, nums[:3])
								white = {"total": w_w + w_d + w_l, "wins": w_w, "draws": w_d, "losses": w_l}
							elif len(nums) == 1:
								white = {"total": int(nums[0]), "wins": None, "draws": None, "losses": None}
						if 'BLACK' in u and black is None:
							if len(nums) >= 3:
								b_w, b_d, b_l = map(int, nums[:3])
								black = {"total": b_w + b_d + b_l, "wins": b_w, "draws": b_d, "losses": b_l}
							elif len(nums) == 1:
								black = {"total": int(nums[0]), "wins": None, "draws": None, "losses": None}

					if white or black:
						fw = white or {}
						fb = black or {}
						ftotal = (fw.get('total') or 0) + (fb.get('total') or 0)
						fwins = (fw.get('wins') or 0) + (fb.get('wins') or 0) if (fw.get('wins') is not None or fb.get('wins') is not None) else None
						fdraws = (fw.get('draws') or 0) + (fb.get('draws') or 0) if (fw.get('draws') is not None or fb.get('draws') is not None) else None
						flosses = (fw.get('losses') or 0) + (fb.get('losses') or 0) if (fw.get('losses') is not None or fb.get('losses') is not None) else None
						formats[label] = {"total": ftotal if ftotal > 0 else None, "wins": fwins, "draws": fdraws, "losses": flosses, "white": fw or None, "black": fb or None}

				data["formats"] = formats

		# If standard rating wasn't found on the statistics page, try the main profile page as a fallback
		if data["ratings"].get("standard") is None:
			# Prefer the chart endpoint which returns a time series including 'rating'
			chart_url = urljoin(BASE, "a_chart_data.phtml")
			headers_chart = {"User-Agent": "Mozilla/5.0", "Referer": stats_url, "X-Requested-With": "XMLHttpRequest"}
			try:
				rc = http_client.get(chart_url, params={"event": player_id, "period": 0}, headers=headers_chart, timeout=timeout)
				cj = rc.json()
				if isinstance(cj, list) and len(cj) > 0:
					# scan from newest to oldest for non-null values
					std_val = None
					rapid_val = None
					blitz_val = None
					for entry in reversed(cj):
						if std_val is None and entry.get("rating"):
							v = str(entry.get("rating")).strip()
							if v.isdigit():
								std_val = int(v)
						if rapid_val is None and entry.get("rapid_rtng"):
							rv = str(entry.get("rapid_rtng")).strip()
							if rv.isdigit():
								rapid_val = int(rv)
						if blitz_val is None and entry.get("blitz_rtng"):
							bv = str(entry.get("blitz_rtng")).strip()
							if bv.isdigit():
								blitz_val = int(bv)
						if std_val is not None and rapid_val is not None and blitz_val is not None:
							break
				# assign if found and not already set by stats_soup parsing
				# prefer chart values for accuracy (override if present)
				if std_val is not None:
					data["ratings"]["standard"] = std_val
				if rapid_val is not None:
					data["ratings"]["rapid"] = rapid_val
				if blitz_val is not None:
					data["ratings"]["blitz"] = blitz_val
			except Exception:
				# fallback: try main profile page td search
				for td in soup.find_all("td"):
					text = td.get_text(" ", strip=True)
					if not text:
						continue
					if any(lbl in text for lbl in ["Standard", "Std."]):
						strong = td.find("strong")
						if strong and strong.text.strip().isdigit():
							data["ratings"]["standard"] = int(strong.text.strip())
							break

	# persist to disk cache
	try:
		with open(cache_file, "w", encoding="utf-8") as fh:
			json.dump({"_cached_at": now, "data": data}, fh, ensure_ascii=False)
	except Exception:
		pass
	# store in memcache
	mem[cache_key] = (now + CACHE_TTL, data)

	return data


if __name__ == "__main__":
	# small demo when running module directly
	name = input("Search player name: ")
	results = search_player(name)
	print("Found", len(results), "results")
	for r in results[:10]:
		print(r)
	if results:
		pid = results[0]["player_id"]
		profile = get_player_profile(pid)
		print(profile)


def get_rating_timeseries(player_id, timeout=10):
	"""Fetch the rating time series for a player from the chart endpoint.

	Returns a pandas.DataFrame indexed by datetime with columns (where
	available) 'standard', 'rapid', 'blitz'. If pandas is not installed
	this function returns the raw JSON list.
	"""
	chart_url = urljoin(BASE, "a_chart_data.phtml")
	headers = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}
	try:
		r = http_client.get(chart_url, params={"event": player_id, "period": 0}, headers=headers, timeout=timeout)
		cj = r.json()
	except Exception:
		return None

	if not cj or not isinstance(cj, list):
		return None if pd is None else pd.DataFrame()

	if pd is None:
		return cj

	df = pd.DataFrame(cj)

	# Find a sensible date column in the JSON payload
	date_col = None
	for c in df.columns:
		cl = c.lower()
		if 'date' in cl or 'time' in cl or 'timestamp' in cl or cl == 'x' or 'created' in cl:
			date_col = c
			break

	# If we didn't find a column, try common short names
	if date_col is None and 'd' in df.columns:
		date_col = 'd'

	if date_col is not None:
		# numeric -> epoch ms likely; else parse as string
		if pd.api.types.is_numeric_dtype(df[date_col]):
			# Try epoch milliseconds then seconds
			try:
				df['dt'] = pd.to_datetime(df[date_col], unit='ms', origin='unix')
			except Exception:
				df['dt'] = pd.to_datetime(df[date_col], unit='s', origin='unix')
		else:
			df['dt'] = pd.to_datetime(df[date_col], errors='coerce')
	else:
		# no date column found; attempt to create from 'year'/'month' keys or fail
		if 'year' in df.columns and 'month' in df.columns:
			df['dt'] = pd.to_datetime(df[['year', 'month']].assign(day=1))
		else:
			# give up — return raw json as empty DataFrame
			return pd.DataFrame()

	df.set_index('dt', inplace=True)
	df.sort_index(inplace=True)

	# Normalize rating column names
	rename_map = {}
	if 'rating' in df.columns:
		rename_map['rating'] = 'standard'
	if 'rapid_rtng' in df.columns:
		rename_map['rapid_rtng'] = 'rapid'
	if 'blitz_rtng' in df.columns:
		rename_map['blitz_rtng'] = 'blitz'
	df.rename(columns=rename_map, inplace=True)

	# Keep only the rating columns we care about (if present) and coerce to numeric
	keep = [c for c in ('standard', 'rapid', 'blitz') if c in df.columns]
	for c in keep:
		try:
			df[c] = pd.to_numeric(df[c], errors='coerce')
		except Exception:
			pass
	clean = df[keep].dropna(how='all')
	# Remove duplicate dates keeping the last occurrence (most recent value for that day)
	try:
		clean = clean[~clean.index.duplicated(keep='last')]
	except Exception:
		pass
	return clean


def resample_rating_series(df, freq='M', agg='last'):
	"""Resample the rating DataFrame to a regular frequency.

	freq: 'D' (daily), 'M' (monthly), 'Y' (yearly) or any pandas offset alias.
	agg: 'last' or 'mean'. Returns a new DataFrame.
	"""
	if df is None or df.empty:
		return df
	# Map simple codes
	freq_map = {'D': 'D', 'M': 'M', 'Y': 'A'}
	f = freq_map.get(freq, freq)
	if agg == 'last':
		return df.resample(f).last().dropna(how='all')
	else:
		return df.resample(f).mean().dropna(how='all')


def _abs_url(href):
	try:
		return urljoin(BASE, href)
	except Exception:
		return href


def get_player_games(player_id, timeout=15, max_pages=None):
	"""Scrape per-game results for a player.

	Returns a pandas.DataFrame with one row per game when pandas is available,
	else a list of dicts. Columns include: date, color, result, opponent_name,
	opponent_id, event, event_url, rating_type, player_rating, opponent_rating,
	K, rating_change. Not all columns are guaranteed present for all rows.
	"""
	start_urls = [
		urljoin(BASE, f"profile/{player_id}/results"),
		urljoin(BASE, f"profile.php?player={player_id}&action=results"),
		urljoin(BASE, f"profile.php?player={player_id}&result=1"),
	]

	def parse_table(soup):
		rows = []
		# Find candidate tables by header keywords
		tables = soup.find_all('table')
		for tbl in tables:
			thead = tbl.find('thead')
			ths = [th.get_text(" ", strip=True).lower() for th in (thead.find_all('th') if thead else [])]
			if not ths:
				# try first row as header
				first = tbl.find('tr')
				ths = [td.get_text(" ", strip=True).lower() for td in (first.find_all(['td','th']) if first else [])]
			# look for core columns
			if not any('white' in h for h in ths) or not any('black' in h for h in ths):
				continue
			# build column index map
			colmap = {}
			for i, h in enumerate(ths):
				hl = h.lower()
				if 'date' in hl:
					colmap['date'] = i
				elif 'white' in hl:
					colmap['white'] = i
				elif 'black' in hl:
					colmap['black'] = i
				elif 'result' in hl or 'res.' in hl:
					colmap['result'] = i
				elif 'k' == hl.strip() or hl.startswith('k '):
					colmap['k'] = i
				elif 'Δ' in hl or '+/-' in hl or 'change' in hl or 'chg' in hl or 'rating change' in hl:
					colmap['delta'] = i
				elif 'tournament' in hl or 'event' in hl:
					colmap['event'] = i
				elif 'elo' in hl or 'rating' in hl:
					# ambiguous; we'll try to read from both player/opponent cells when possible
					pass
			# iterate body rows
			for tr in tbl.find_all('tr'):
				cells = tr.find_all('td')
				if not cells:
					continue
				def getc(key):
					idx = colmap.get(key)
					if idx is None or idx >= len(cells):
						return None
					return cells[idx]
				date = getc('date')
				wc = getc('white')
				bc = getc('black')
				res = getc('result')
				if not wc or not bc:
					continue
				# Extract player/opponent ids from links
				def parse_player(td):
					name = td.get_text(" ", strip=True)
					pid = None
					for a in td.find_all('a', href=True):
						href = a['href']
						if '/profile/' in href or 'profile.php' in href:
							parsed = urlparse(href)
							try:
								pid = parsed.path.rstrip('/').split('/')[-1]
								if not pid or not pid.isdigit():
									pid = parse_qs(parsed.query).get('player', [None])[0]
							except Exception:
								pid = parse_qs(parsed.query).get('player', [None])[0]
						break
					return name, pid
				w_name, w_id = parse_player(wc)
				b_name, b_id = parse_player(bc)
				color = None
				opp_id = None
				opp_name = None
				if str(player_id) == str(w_id):
					color = 'White'
					opp_id, opp_name = b_id, b_name
				elif str(player_id) == str(b_id):
					color = 'Black'
					opp_id, opp_name = w_id, w_name
				# Ratings within player/opponent cells (if present like "Name (Rating)")
				r_re = re.compile(r"(\d{3,4})")
				def parse_rating(td):
					text = td.get_text(" ", strip=True)
					m = r_re.search(text)
					return int(m.group(1)) if m else None
				w_elo = parse_rating(wc)
				b_elo = parse_rating(bc)
				player_elo = w_elo if color == 'White' else (b_elo if color == 'Black' else None)
				opp_elo = b_elo if color == 'White' else (w_elo if color == 'Black' else None)
				date_txt = date.get_text(" ", strip=True) if date else None
				res_txt = res.get_text(" ", strip=True) if res else None
				# event cell and url
				ev_cell = getc('event')
				ev_name = ev_cell.get_text(" ", strip=True) if ev_cell else None
				ev_url = None
				if ev_cell:
					alink = ev_cell.find('a', href=True)
					if alink:
						ev_url = _abs_url(alink['href'])
				# K and delta
				k_val = (getc('k').get_text(" ", strip=True) if getc('k') else None)
				delta_val = (getc('delta').get_text(" ", strip=True) if getc('delta') else None)
				rows.append({
					'date': date_txt,
					'color': color,
					'result': res_txt,
					'opponent_name': opp_name,
					'opponent_id': opp_id,
					'player_rating': player_elo,
					'opponent_rating': opp_elo,
					'event': ev_name,
					'event_url': ev_url,
					'k': k_val,
					'rating_change': delta_val,
				})
		return rows

	def find_next_url(soup, current):
		# Look for a Next pagination link
		for a in soup.find_all('a', href=True):
			label = a.get_text(" ", strip=True).lower()
			aria = (a.get('aria-label') or '').lower()
			if label in ('next', 'older', '›') or 'next' in aria:
				h = _abs_url(a['href'])
				if h and h != current:
					return h
		return None

	seen = set()
	games = []
	pages = 0
	url = None
	# pick the first reachable url
	for u in start_urls:
		try:
			r = http_client.get(u, timeout=timeout)
			if r.status_code == 200 and r.text:
				url = u
				break
		except Exception:
			continue
	if not url:
		return [] if pd is None else (pd.DataFrame())

	while url and url not in seen:
		seen.add(url)
		try:
			r = http_client.get(url, timeout=timeout)
			soup = BeautifulSoup(r.text, 'html.parser')
			games.extend(parse_table(soup))
			pages += 1
			if max_pages and pages >= max_pages:
				break
			url = find_next_url(soup, url)
		except Exception:
			break

	if pd is None:
		return games
	# Build DataFrame and normalize types
	df = pd.DataFrame(games)
	if not df.empty:
		# parse dates
		try:
			df['date'] = pd.to_datetime(df['date'], errors='coerce')
			df.sort_values('date', inplace=True)
		except Exception:
			pass
		# normalize rating change like "+5" to numeric
		def _to_num(s):
			try:
				return float(str(s).replace('+','').replace('±','').replace(',','.'))
			except Exception:
				return None
		if 'rating_change' in df.columns:
			df['rating_change'] = df['rating_change'].map(_to_num)
	return df


def get_calculations_index(player_id, timeout=20):
	"""Fetch the per-period calculations index for a player.

	Returns a pandas.DataFrame with columns:
	- period (datetime)
	- rating_type (int: 0=Standard,1=Rapid,2=Blitz)
	- rating_name (str)
	- delta_text (str, e.g., '2.2' or '-9.5')
	- detail_url (str absolute URL to calculations.phtml for that period+rating)
	"""
	from urllib.parse import urljoin
	url = urljoin(BASE, f"a_calculations.phtml?event={player_id}")
	r = http_client.get(url, timeout=timeout)
	soup = BeautifulSoup(r.text, 'html.parser')
	rows = []
	# Locate the main table with headers: Period | Standard | Rapid | Blitz
	for tbl in soup.find_all('table'):
		thead = tbl.find('thead')
		if not thead:
			continue
		hdrs = [th.get_text(" ", strip=True).lower() for th in thead.find_all('th')]
		if len(hdrs) >= 4 and 'period' in hdrs[0] and 'standard' in hdrs[1] and 'rapid' in hdrs[2] and 'blitz' in hdrs[3]:
			# parse body
			for tr in tbl.find_all('tr'):
				cells = tr.find_all('td')
				if len(cells) < 4:
					continue
				period_txt = cells[0].get_text(" ", strip=True)
				
				def parse_cell(td, rating_type):
					name = {0:'Standard',1:'Rapid',2:'Blitz'}.get(rating_type, str(rating_type))
					link = td.find('a', href=True)
					if link:
						delta = link.get_text(" ", strip=True).replace('View','').strip()
						detail = urljoin(BASE, link['href'])
						rows.append({'period': period_txt, 'rating_type': rating_type, 'rating_name': name, 'delta_text': delta, 'detail_url': detail})
					else:
						val = td.get_text(" ", strip=True)
						if val and val.lower() != 'no games':
							rows.append({'period': period_txt, 'rating_type': rating_type, 'rating_name': name, 'delta_text': val, 'detail_url': None})
				
				for i, td in enumerate(cells[1:4]):
					parse_cell(td, i)
			break
	# Build DataFrame
	if pd is None:
		return rows
	df = pd.DataFrame(rows)
	if not df.empty:
		try:
			df['period'] = pd.to_datetime(df['period'], errors='coerce')
		except Exception:
			pass
	return df


def get_opponents(player_id, timeout=20, stats_referer=None):
	"""Return a list of opponents for a player using the FIDE opponents endpoint.

	Each item contains id_number (opponent FIDE id), name, and country (if provided).
	"""
	from urllib.parse import urljoin
	url = urljoin(BASE, f"a_data_opponents.php?pl={player_id}")
	headers = {"User-Agent": "Mozilla/5.0"}
	if stats_referer:
		headers["Referer"] = stats_referer
	try:
		r = http_client.get(url, timeout=timeout, headers=headers)
		try:
			j = r.json()
		except Exception:
			import json as _json
			j = _json.loads(r.text)
		if isinstance(j, list):
			return j
		return []
	except Exception:
		return []


def get_stats_vs_opponent(player_id, opponent_id, timeout=20, stats_referer=None):
	"""Fetch aggregated stats for player vs a specific opponent via a_data_stats.php.

	Returns the raw JSON entry dict (or None) for that pair.
	"""
	from urllib.parse import urljoin
	url = urljoin(BASE, f"a_data_stats.php?id1={player_id}&id2={opponent_id}")
	headers = {"User-Agent": "Mozilla/5.0", "X-Requested-With": "XMLHttpRequest"}
	if stats_referer:
		headers["Referer"] = stats_referer
	try:
		r = http_client.get(url, timeout=timeout, headers=headers)
		j = r.json()
		if isinstance(j, list) and j:
			return j[0]
		return None
	except Exception:
		return None


def get_opponents_aggregate(player_id, timeout=20):
	"""Return a DataFrame with one row per opponent including totals and per-format splits.

	Columns include:
	- opponent_id, opponent_name, opponent_country
	- total, wins, draws, losses
	- white_total, white_wins, white_draws, white_losses
	- black_total, black_wins, black_draws, black_losses
	- std_total, std_wins, std_draws, std_losses
	- rpd_total, rpd_wins, rpd_draws, rpd_losses
	- blz_total, blz_wins, blz_draws, blz_losses
	"""
	# Get stats page url once to use as referer
	def _fetch_stats_referer(pid):
		urls = [urljoin(BASE, f"profile/{pid}/statistics"), urljoin(BASE, f"profile.php?player={pid}&action=statistics")]
		for u in urls:
			try:
				r = http_client.get(u, timeout=timeout)
				if r.status_code == 200:
					return u
			except Exception:
				continue
		return None

	stats_ref = _fetch_stats_referer(player_id)
	opps = get_opponents(player_id, timeout=timeout, stats_referer=stats_ref) or []
	rows = []
	for opp in opps:
		oid = opp.get('id_number') or opp.get('id') or opp.get('player_id')
		name = opp.get('name')
		country = opp.get('country')
		if not oid:
			continue
		entry = get_stats_vs_opponent(player_id, oid, timeout=timeout, stats_referer=stats_ref) or {}
		def gi(k):
			try:
				v = entry.get(k)
				return int(str(v).replace(',', '')) if v is not None else 0
			except Exception:
				return 0
		w_total = gi('white_total'); w_w = gi('white_win_num'); w_d = gi('white_draw_num'); w_l = max(w_total - w_w - w_d, 0)
		b_total = gi('black_total'); b_w = gi('black_win_num'); b_d = gi('black_draw_num'); b_l = max(b_total - b_w - b_d, 0)
		total = w_total + b_total
		wins = w_w + b_w
		draws = w_d + b_d
		losses = max(total - wins - draws, 0)
		# format specifics
		def fmt(prefix):
			t = gi(f'white_total_{prefix}') + gi(f'black_total_{prefix}')
			w = gi(f'white_win_num_{prefix}') + gi(f'black_win_num_{prefix}')
			d = gi(f'white_draw_num_{prefix}') + gi(f'black_draw_num_{prefix}')
			l = max(t - w - d, 0)
			return t, w, d, l
		std_t, std_w, std_d, std_l = fmt('std')
		rpd_t, rpd_w, rpd_d, rpd_l = fmt('rpd')
		blz_t, blz_w, blz_d, blz_l = fmt('blz')
		rows.append({
			'opponent_id': oid,
			'opponent_name': name,
			'opponent_country': country,
			'total': total, 'wins': wins, 'draws': draws, 'losses': losses,
			'white_total': w_total, 'white_wins': w_w, 'white_draws': w_d, 'white_losses': w_l,
			'black_total': b_total, 'black_wins': b_w, 'black_draws': b_d, 'black_losses': b_l,
			'std_total': std_t, 'std_wins': std_w, 'std_draws': std_d, 'std_losses': std_l,
			'rpd_total': rpd_t, 'rpd_wins': rpd_w, 'rpd_draws': rpd_d, 'rpd_losses': rpd_l,
			'blz_total': blz_t, 'blz_wins': blz_w, 'blz_draws': blz_d, 'blz_losses': blz_l,
		})

	if pd is None:
		return rows
	return pd.DataFrame(rows)
