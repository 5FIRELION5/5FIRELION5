import sys
import sysconfig
import os
# Protect against local modules shadowing the standard library.
# Remove the project root (script directory) from sys.path before importing
# third-party libraries like requests/urllib3. This prevents a local `http`
# package or `http.py` from being imported instead of the stdlib package.
script_dir = os.path.dirname(os.path.abspath(__file__))
# Keep the project script directory on sys.path so local modules (like
# `scrape_fide`) can be imported when running `main.py` from the project
# directory or via a virtualenv. Previously we removed the script dir to
# avoid shadowing stdlib modules; that prevented importing local modules.
# We resolved the shadowing by removing/renaming problematic files, so
# restore normal import behavior while ensuring the stdlib is early.
stdlib_dir = sysconfig.get_paths().get('stdlib')
if stdlib_dir and stdlib_dir not in sys.path:
	# Put stdlib ahead so standard-library modules are preferred when
	# resolving names like `http` used by urllib/requests.
	sys.path.insert(0, stdlib_dir)

import tkinter as tk
from tkinter import messagebox, filedialog
from tkinter import ttk
# Add ttkthemes for modern themes
from ttkthemes import ThemedTk
# Ensure the stdlib http.client submodule is available before importing
# modules that pull in urllib/requests.
import http.client  # noqa: F401
from scrape_fide import search_player, get_player_profile
from scrape_fide import get_player_games, get_calculations_index
from scrape_fide import get_opponents_aggregate
from scrape_fide import get_opponents, get_stats_vs_opponent
import concurrent.futures

# Matplotlib embedding for rating + pie charts.
try:
	import matplotlib
	# Use TkAgg for interactive embedding (if available). Fallback to Agg.
	try:
		matplotlib.use('TkAgg')
	except Exception:
		matplotlib.use('Agg')
	from matplotlib.figure import Figure
	from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
	HAS_MPL = True
except Exception:
	HAS_MPL = False

last_profile = None
executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
chart_mode = 'rating'  # 'rating' or 'results'
_rating_df = None  # cached rating timeseries for current profile
# Track which player the cached rating data belongs to
_rating_player_id = None
# Timeline customization state
smoothing_enabled = False
timespan = '3y'  # options: '1y','3y','all'
visible_formats = {'standard': True, 'rapid': True, 'blitz': True}

# Opponent selection state/caches
opponent_var = None  # will be initialized after Tk root
opponent_cb = None   # ttk.Combobox for opponent selection
_opponent_map = {}  # label -> opponent_id
_opp_stats_cache = {}  # (player_id, opponent_id) -> entry dict
_opponents_loaded_for = None  # player_id for which the menu is populated
_opponent_labels = []  # preserve order for combobox values


def show_profile(profile=None):
	"""Display the profile in a concise format by default.

	When the color dropdown is changed the aggregate portion is replaced by
	per-color details (White or Black) or the overall summary (Both).
	"""
	global last_profile, _rating_df, _rating_player_id
	if profile is not None:
		last_profile = profile
		# New player selected: invalidate cached rating data so timeline fetches fresh
		_rating_df = None
		_rating_player_id = None
	if 'last_profile' not in globals() or not last_profile:
		return
	p = last_profile

	lines = []
	lines.append(f"Name: {p.get('name')}")
	lines.append(f"FIDE ID: {p.get('player_id')}")
	lines.append(f"Profile URL: {p.get('raw_profile_url')}")
	lines.append("")
	lines.append("Ratings:")
	for k in ["standard", "rapid", "blitz"]:
		v = p.get('ratings', {}).get(k)
		lines.append(f"  {k.title()}: {v if v is not None else 'N/A'}")
	lines.append("")

	sel = color_var.get() if 'color_var' in globals() else 'Both'
	fmt = format_var.get() if 'format_var' in globals() else 'All'
	# If a specific opponent is selected, compute stats from vs-opponent JSON and short-circuit
	try:
		opp_label = opponent_var.get() if opponent_var is not None else 'All Opponents'
		opp_id = _opponent_map.get(opp_label)
		if opp_id:
			entry = _get_vs_entry(p.get('player_id'), opp_id)
			if entry:
				vals = _compute_vs_filtered(entry, sel, fmt)
				tot = vals.get('total'); wins = vals.get('wins'); draws = vals.get('draws'); losses = vals.get('losses')
				lines.append(f"Aggregate stats vs {opp_label} (selected: {sel} / {fmt}):")
				lines.append(f"  Total games: {tot}")
				lines.append(f"  Total wins: {wins}")
				lines.append(f"  Total losses: {losses}")
				lines.append(f"  Total draws: {draws}")
				if tot and tot > 0:
					lines.append(f"  Win ratio: {round((wins or 0)/tot*100,1)}%")
					lines.append(f"  Draw ratio: {round((draws or 0)/tot*100,1)}%")
					lines.append(f"  Loss ratio: {round((losses or 0)/tot*100,1)}%")
				else:
					lines.append("  Win ratio: to be determined")
					lines.append("  Draw ratio: to be determined")
					lines.append("  Loss ratio: to be determined")
				# Add metrics for opponent-selected case
				def _pi_er_from_counts_loc(w, d, l):
					t = (w or 0) + (d or 0) + (l or 0)
					if not t:
						return None, None
					pi_points = (w or 0) + 0.5*(d or 0)
					return pi_points, (pi_points / t) * 100.0
				pi_points, er = _pi_er_from_counts_loc(wins, draws, losses)
				if pi_points is not None:
					lines.append("")
					lines.append("Metrics (based on current selection):")
					lines.append(f"  Performance Index (points): {round(pi_points,2)} of {int((wins or 0)+(draws or 0)+(losses or 0))}")
					lines.append(f"  Efficiency Rating (0–100): {round(er,2)}%")
				# Expected score and color advantage only when a specific format is chosen
				if fmt != 'All':
					def gi(k):
						try:
							v = entry.get(k); return int(str(v).replace(',', '')) if v is not None else 0
						except Exception:
							return 0
					fsuf = 'std' if fmt=='Standard' else ('rpd' if fmt=='Rapid' else ('blz' if fmt=='Blitz' else None))
					if fsuf is None:
						wt = gi('white_total'); ww = gi('white_win_num'); wd = gi('white_draw_num')
						bt = gi('black_total'); bw = gi('black_win_num'); bd = gi('black_draw_num')
					else:
						wt = gi(f'white_total_{fsuf}'); ww = gi(f'white_win_num_{fsuf}'); wd = gi(f'white_draw_num_{fsuf}')
						bt = gi(f'black_total_{fsuf}'); bw = gi(f'black_win_num_{fsuf}'); bd = gi(f'black_draw_num_{fsuf}')
					wl = max(wt-ww-wd,0); bl = max(bt-bw-bd,0)
					def _exp(w,d,l):
						t = (w or 0)+(d or 0)+(l or 0)
						return ((w or 0)/t + 0.5*((d or 0)/t)) if t else None
					Ew = _exp(ww, wd, wl)
					Eb = _exp(bw, bd, bl)
					if Ew is not None or Eb is not None:
						lines.append("")
						lines.append(f"Expected Score per Color ({fmt}):")
						if Ew is not None:
							lines.append(f"  White: {round(Ew,3)}")
						if Eb is not None:
							lines.append(f"  Black: {round(Eb,3)}")
						if Ew is not None and Eb is not None:
							lines.append(f"  Color Advantage Δ (White−Black): {round(Ew-Eb,3)}")
				output.delete("1.0", tk.END)
				output.insert(tk.END, "\n".join(lines))
				if HAS_MPL:
					refresh_chart()
				return
	except Exception:
		pass
	# If Both -> show concise overall summary
	if sel == 'Both':
		# consider selected format
		if fmt == 'All':
			wins = p.get('total_wins')
			draws = p.get('total_draws')
			losses = p.get('total_losses')
			total_games = None
			if wins is not None and draws is not None and losses is not None:
				total_games = wins + draws + losses
			lines.append("Aggregate stats (if available):")
			lines.append(f"  Total games: {total_games if total_games is not None else 'to be determined'}")
			lines.append(f"  Total wins: {wins if wins is not None else 'to be determined'}")
			lines.append(f"  Total losses: {losses if losses is not None else 'to be determined'}")
			lines.append(f"  Total draws: {draws if draws is not None else 'to be determined'}")
			lines.append(f"  Win ratio: {p.get('win_ratio') if p.get('win_ratio') is not None else 'to be determined'}")
			lines.append(f"  Loss ratio: {p.get('loss_ratio') if p.get('loss_ratio') is not None else 'to be determined'}")
			lines.append(f"  Draw ratio: {p.get('draw_ratio') if p.get('draw_ratio') is not None else 'to be determined'}")
		else:
			# format-specific overall (Both colors) stats
			fmtkey = fmt.lower()
			finfo = p.get('formats', {}).get(fmtkey)
			if not finfo or finfo.get('total') is None:
				lines.append(f"Aggregate stats for {fmt} not available")
			else:
				fg_total = finfo.get('total')
				fg_wins = finfo.get('wins')
				fg_draws = finfo.get('draws')
				fg_losses = finfo.get('losses')
				fg_win_ratio = f"{round(fg_wins/fg_total*100,1)}%" if fg_total and fg_wins is not None else 'to be determined'
				fg_draw_ratio = f"{round(fg_draws/fg_total*100,1)}%" if fg_total and fg_draws is not None else 'to be determined'
				fg_loss_ratio = f"{round(fg_losses/fg_total*100,1)}%" if fg_total and fg_losses is not None else 'to be determined'
				lines.append(f"Aggregate stats ({fmt}):")
				lines.append(f"  Total games: {fg_total}")
				lines.append(f"  Total wins: {fg_wins}")
				lines.append(f"  Total draws: {fg_draws}")
				lines.append(f"  Total losses: {fg_losses}")
				lines.append(f"  Win ratio: {fg_win_ratio}")
				lines.append(f"  Draw ratio: {fg_draw_ratio}")
				lines.append(f"  Loss ratio: {fg_loss_ratio}")
	else:
		# show per-color details for the selected color
		key = 'white' if sel == 'White' else 'black'
		per = p.get('per_color') or {}

		def _as_detail(x):
			if isinstance(x, dict):
				return x
			try:
				if x is None:
					return None
				return {'total': int(x)}
			except Exception:
				return None

		info = _as_detail(per.get(key)) if per else None
		lines.append(f"Aggregate stats (selected: {sel}):")
		fmt = format_var.get() if 'format_var' in globals() else 'All'
		if fmt == 'All':
			if not info:
				lines.append("  Per-color details not available for selected color")
			else:
				tot = info.get('total')
				wins_c = info.get('wins')
				draws_c = info.get('draws')
				losses_c = info.get('losses')
				lines.append(f"  Total games ({key.title()}): {tot}")
				lines.append(f"  Wins ({key.title()}): {wins_c}")
				lines.append(f"  Draws ({key.title()}): {draws_c}")
				lines.append(f"  Losses ({key.title()}): {losses_c}")
				# ratios
				if tot and tot > 0:
					lines.append(f"  Win ratio ({key.title()}): {round((wins_c or 0)/tot*100,1)}%")
					lines.append(f"  Draw ratio ({key.title()}): {round((draws_c or 0)/tot*100,1)}%")
					lines.append(f"  Loss ratio ({key.title()}): {round((losses_c or 0)/tot*100,1)}%")
				else:
					lines.append(f"  Win ratio ({key.title()}): to be determined")
					lines.append(f"  Draw ratio ({key.title()}): to be determined")
					lines.append(f"  Loss ratio ({key.title()}): to be determined")
		else:
			# format-specific per-color
			fmtkey = fmt.lower()
			finfo = p.get('formats', {}).get(fmtkey)
			if not finfo:
				lines.append(f"  {fmt} per-color stats not available")
			else:
				pinfo = finfo.get(key) or {}
				tot = pinfo.get('total')
				wins_c = pinfo.get('wins')
				draws_c = pinfo.get('draws')
				losses_c = pinfo.get('losses')
				lines.append(f"  Total games ({key.title()}): {tot if tot is not None else 'N/A'}")
				lines.append(f"  Wins ({key.title()}): {wins_c if wins_c is not None else 'N/A'}")
				lines.append(f"  Draws ({key.title()}): {draws_c if draws_c is not None else 'N/A'}")
				lines.append(f"  Losses ({key.title()}): {losses_c if losses_c is not None else 'N/A'}")
				if tot and tot > 0:
					lines.append(f"  Win ratio ({key.title()}): {round((wins_c or 0)/tot*100,1)}%")
					lines.append(f"  Draw ratio ({key.title()}): {round((draws_c or 0)/tot*100,1)}%")
					lines.append(f"  Loss ratio ({key.title()}): {round((losses_c or 0)/tot*100,1)}%")
				else:
					lines.append(f"  Win ratio ({key.title()}): to be determined")
					lines.append(f"  Draw ratio ({key.title()}): to be determined")
					lines.append(f"  Loss ratio ({key.title()}): to be determined")

	# --- Advanced metrics (Performance Index, Expected Score, Differential, Efficiency) ---
	def _fmt_pct(x):
		return f"{x:.2f}%" if x is not None else 'N/A'
	def _fmt_num(x):
		return f"{x:.3f}" if x is not None else 'N/A'
	def _pi_er_from_counts(w, d, l):
		t = (w or 0) + (d or 0) + (l or 0)
		if not t:
			return None, None
		pi_points = (w or 0) + 0.5*(d or 0)  # raw points (not normalized)
		er_pct = (pi_points / t) * 100.0
		return pi_points, er_pct
	def _exp_from_counts(w, d, l):
		t = (w or 0) + (d or 0) + (l or 0)
		if not t:
			return None
		return (w or 0)/t + 0.5*((d or 0)/t)

	# Determine counts used for PI/ER (based on currently shown section)
	pi_w = pi_d = pi_l = None
	opp_label = opponent_var.get() if opponent_var is not None else 'All Opponents'
	opp_id = _opponent_map.get(opp_label)
	if opp_id:
		entry = _get_vs_entry(p.get('player_id'), opp_id)
		if entry:
			vals_cur = _compute_vs_filtered(entry, sel, fmt)
			pi_w, pi_d, pi_l = vals_cur.get('wins'), vals_cur.get('draws'), vals_cur.get('losses')
	else:
		if sel == 'Both':
			if fmt == 'All':
				pi_w, pi_d, pi_l = p.get('total_wins'), p.get('total_draws'), p.get('total_losses')
			else:
				fi = (p.get('formats') or {}).get(fmt.lower()) or {}
				pi_w, pi_d, pi_l = fi.get('wins'), fi.get('draws'), fi.get('losses')
		else:
			key = 'white' if sel == 'White' else 'black'
			if fmt == 'All':
				ci = (p.get('per_color') or {}).get(key) or {}
				pi_w, pi_d, pi_l = ci.get('wins'), ci.get('draws'), ci.get('losses')
			else:
				fi = (p.get('formats') or {}).get(fmt.lower()) or {}
				ci = fi.get(key) or {}
				pi_w, pi_d, pi_l = ci.get('wins'), ci.get('draws'), ci.get('losses')

	pi_points, er = _pi_er_from_counts(pi_w, pi_d, pi_l)
	if pi_points is not None and er is not None:
		lines.append("")
		lines.append("Metrics (based on current selection):")
		lines.append(f"  Performance Index (points): {_fmt_num(pi_points)} of {int((pi_w or 0)+(pi_d or 0)+(pi_l or 0))}")
		lines.append(f"  Efficiency Rating (0–100): {_fmt_pct(er)}")

	# Expected score per color and differential only when a specific format is selected
	if fmt != 'All':
		# Extract per-color counts for selected format
		w_counts = { 'wins': None, 'draws': None, 'losses': None }
		b_counts = { 'wins': None, 'draws': None, 'losses': None }
		if opp_id and entry:
			# read from vs-opponent JSON keys
			def gi(k):
				try:
					v = entry.get(k); return int(str(v).replace(',', '')) if v is not None else 0
				except Exception:
					return 0
			fsuf = 'std' if fmt=='Standard' else ('rpd' if fmt=='Rapid' else ('blz' if fmt=='Blitz' else None))
			if fsuf is None:
				# Shouldn't happen here, but guard anyway
				wt = gi('white_total'); ww = gi('white_win_num'); wd = gi('white_draw_num')
				bt = gi('black_total'); bw = gi('black_win_num'); bd = gi('black_draw_num')
			else:
				wt = gi(f'white_total_{fsuf}'); ww = gi(f'white_win_num_{fsuf}'); wd = gi(f'white_draw_num_{fsuf}')
				bt = gi(f'black_total_{fsuf}'); bw = gi(f'black_win_num_{fsuf}'); bd = gi(f'black_draw_num_{fsuf}')
			w_counts = {'wins': ww, 'draws': wd, 'losses': max(wt-ww-wd,0)}
			b_counts = {'wins': bw, 'draws': bd, 'losses': max(bt-bw-bd,0)}
		else:
			fmtkey = fmt.lower()
			fi = (p.get('formats') or {}).get(fmtkey) or {}
			winfo = fi.get('white') or {}
			binfo = fi.get('black') or {}
			w_counts = {'wins': winfo.get('wins'), 'draws': winfo.get('draws'), 'losses': winfo.get('losses')}
			b_counts = {'wins': binfo.get('wins'), 'draws': binfo.get('draws'), 'losses': binfo.get('losses')}

		Ew = _exp_from_counts(w_counts['wins'], w_counts['draws'], w_counts['losses'])
		Eb = _exp_from_counts(b_counts['wins'], b_counts['draws'], b_counts['losses'])
		if Ew is not None or Eb is not None:
			lines.append("")
			lines.append(f"Expected Score per Color ({fmt}):")
			if Ew is not None:
				lines.append(f"  White: {_fmt_num(Ew)}")
			if Eb is not None:
				lines.append(f"  Black: {_fmt_num(Eb)}")
			if Ew is not None and Eb is not None:
				lines.append(f"  Color Advantage Δ (White−Black): {_fmt_num(Ew - Eb)}")

	output.delete("1.0", tk.END)
	output.insert(tk.END, "\n".join(lines))

	# refresh chart area based on selected chart mode
	if HAS_MPL:
		refresh_chart()




def on_search():
	name = entry.get().strip()
	if not name:
		messagebox.showerror("Error", "Please enter a player name to search")
		return
	status.set("Searching...")
	root.update_idletasks()
	try:
		results = search_player(name)
	except Exception as e:
		status.set("")
		messagebox.showerror("Search error", str(e))
		return

	# If the initial query returned no results, try common name variants
	if not results:
		variants = []
		# If user typed 'First Last', try 'Last, First' and 'Last First'
		if "," in name:
			# already contains comma, also try swapping
			parts = [p.strip() for p in name.split(",") if p.strip()]
			if len(parts) >= 2:
				swapped = f"{parts[0]}, {parts[1]}" if len(parts) >= 2 else name
				variants.append(swapped)
		elif " " in name:
			parts = [p.strip() for p in name.split() if p.strip()]
			if len(parts) >= 2:
				last = parts[-1]
				first = " ".join(parts[:-1])
				variants.extend([f"{last}, {first}", f"{last} {first}"])

		for v in variants:
			try:
				results = search_player(v)
			except Exception:
				results = []
			if results:
				break

	if not results:
		status.set("")
		messagebox.showinfo("No results", f"No players found matching '{name}'")
		return

	# If multiple results, show a selection dialog
	if len(results) > 1:
		sel = SelectionDialog(root, results)
		root.wait_window(sel.top)
		chosen = sel.chosen
		if not chosen:
			status.set("")
			return
		player_id = chosen["player_id"]
		profile_url = chosen.get("profile_url")
	else:
		player_id = results[0]["player_id"]
		profile_url = results[0].get("profile_url")

	status.set("Fetching profile...")
	root.update_idletasks()
	try:
		# Pass profile_url when available so the fetch uses the correct path
		profile = get_player_profile(player_id, profile_url=profile_url)
	except Exception as e:
		status.set("")
		messagebox.showerror("Profile error", str(e))
		return

	status.set("")
	# Reset opponent selection UI and caches for new player
	try:
		if opponent_var is not None:
			opponent_var.set("All Opponents")
			# reset combobox values to default; populate asynchronously next
			if opponent_cb is not None:
				opponent_cb['values'] = ("All Opponents",)
		global _opponent_map, _opp_stats_cache, _opponents_loaded_for
		_opponent_map = {}
		_opp_stats_cache = {}
		_opponents_loaded_for = None
		global _opponent_labels
		_opponent_labels = ["All Opponents"]
	except Exception:
		pass

	show_profile(profile)
	# Begin async load of opponents for this player to populate menu
	pid = profile.get('player_id')
	def _load_opps(p):
		try:
			return get_opponents(p, timeout=30)
		except Exception as e:
			return e
	future = executor.submit(_load_opps, pid)
	def _done(fut):
		try:
			res = fut.result()
		except Exception as e:
			res = e
		def _ui():
			global _opponent_map, _opponents_loaded_for, _opponent_labels
			if isinstance(res, Exception):
				return
			if opponent_cb is None or opponent_var is None:
				return
			try:
				_opponent_map = {}
				labels = ["All Opponents"]
				for opp in (res or []):
					oid = opp.get('id_number') or opp.get('id') or opp.get('player_id')
					name = opp.get('name') or str(oid)
					if not oid:
						continue
					label = f"{name} ({oid})"
					_opponent_map[label] = str(oid)
					labels.append(label)
				_opponent_labels = labels[:]
				opponent_cb['values'] = tuple(labels)
				_opponents_loaded_for = pid
			except Exception:
				pass
		root.after(0, _ui)
	future.add_done_callback(_done)



class SelectionDialog:
	def __init__(self, parent, results):
		self.top = tk.Toplevel(parent)
		self.top.title("Select player")
		self.chosen = None
		tk.Label(self.top, text="Multiple matches found — select one:").pack(padx=8, pady=4)
		self.listbox = tk.Listbox(self.top, width=80, height=10)
		for r in results:
			display = f"{r.get('name') or 'N/A'} — {r.get('player_id')}"
			self.listbox.insert(tk.END, display)
		self.listbox.pack(padx=8, pady=4)
		btn = tk.Button(self.top, text="OK", command=self.on_ok)
		btn.pack(pady=4)
		self.results = results

	def on_ok(self):
		idx = self.listbox.curselection()
		if not idx:
			return
		i = idx[0]
		self.chosen = self.results[i]
		self.top.destroy()

def schedule_chart_update(player_id):
	"""Fetch rating timeseries in a background thread and plot when ready."""
	if not HAS_MPL or not player_id:
		return

	def _worker(pid):
		try:
			import scrape_fide
			df = scrape_fide.get_rating_timeseries(pid)
			return df
		except Exception as e:
			return e

	future = executor.submit(_worker, player_id)

	def _done(fut):
		try:
			res = fut.result()
		except Exception as e:
			res = e
		def _ui_update():
			if isinstance(res, Exception):
				# ignore plotting errors silently
				return
			global _rating_df, _rating_player_id
			_rating_df = res
			_rating_player_id = player_id
			if chart_mode == 'rating':
				plot_timeseries(_rating_df)
		root.after(0, _ui_update)

	future.add_done_callback(_done)


def refresh_chart():
	"""Redraw chart according to chart_mode and current selections/profile."""
	if not HAS_MPL:
		return
	global _rating_df, _rating_player_id
	if chart_mode == 'rating':
		# ensure timeseries fetched
		if last_profile:
			pid = last_profile.get('player_id')
			if _rating_df is None or _rating_player_id != pid:
				# fetch then return; callback will draw
				schedule_chart_update(pid)
			else:
				# already have the correct player's data -> re-plot (applies timespan/smoothing/visibility)
				plot_timeseries(_rating_df)
		elif _rating_df is not None:
			# no active profile but we have data; plot it
			plot_timeseries(_rating_df)
	elif chart_mode == 'results':
		if last_profile:
			update_pie_chart(last_profile)


def plot_timeseries(df):
	"""Plot the rating DataFrame onto the embedded axes.

	df: pandas.DataFrame with datetime index and columns like 'standard','rapid','blitz'
	"""
	if not HAS_MPL or df is None:
		return
	try:
		import pandas as _pd
		now = _pd.Timestamp.now()
		if timespan == '1y':
			cutoff = now - _pd.DateOffset(years=1)
		elif timespan == '3y':
			cutoff = now - _pd.DateOffset(years=3)
		else:
			cutoff = None
		dplot = df[df.index >= cutoff] if cutoff is not None else df.copy()
		# optional smoothing (rolling mean over 3 periods)
		if smoothing_enabled:
			for col in dplot.columns:
				try:
					dplot[col] = dplot[col].rolling(window=3, min_periods=1).mean()
				except Exception:
					pass
	except Exception:
		dplot = df

	chart_ax.clear()
	# Ensure axes appearance restored after pie mode
	try:
		chart_ax.set_aspect('auto')
		chart_ax.set_frame_on(True)
		for sp in chart_ax.spines.values():
			sp.set_visible(True)
		chart_ax.grid(True, which='both', linestyle='--', alpha=0.25)
	except Exception:
		pass
	colors = {'standard': 'tab:blue', 'rapid': 'tab:green', 'blitz': 'tab:orange'}
	plotted = False
	for col in ('standard', 'rapid', 'blitz'):
		if not visible_formats.get(col):
			continue
		if col in dplot.columns and dplot[col].notna().any():
			chart_ax.plot(dplot.index, dplot[col], label=col.title(), color=colors.get(col))
			plotted = True

	if not plotted:
		chart_ax.text(0.5, 0.5, 'No rating time series available', ha='center', va='center', transform=chart_ax.transAxes)
	else:
		chart_ax.legend()
		chart_ax.set_ylabel('Rating')
		chart_ax.set_xlabel('Date')
	try:
		canvas.draw()
	except Exception:
		pass


# Use ThemedTk for a modern look
root = ThemedTk(theme="arc")  # Try 'arc', 'plastik', 'azure', etc.
root.title("FIDE Player Lookup")

# Ensure the window fits within the Windows work area (not hidden behind taskbar)
def _get_work_area():
	try:
		import ctypes
		from ctypes import wintypes
		SPI_GETWORKAREA = 0x0030
		rect = wintypes.RECT()
		ok = ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
		if ok:
			return rect.left, rect.top, rect.right, rect.bottom
	except Exception:
		pass
	# Fallback: full screen
	try:
		return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
	except Exception:
		return 0, 0, 1200, 800

def _fit_to_work_area():
	try:
		l, t, r, b = _get_work_area()
		work_w = max(200, r - l)
		work_h = max(200, b - t)
		root.update_idletasks()
		req_w = root.winfo_width() or root.winfo_reqwidth()
		req_h = root.winfo_height() or root.winfo_reqheight()
		# Keep a small margin so borders don’t touch taskbar edges
		w = min(req_w, work_w - 10)
		h = min(req_h, work_h - 10)
		x = l + max(0, (work_w - w)//2)
		y = t + max(0, (work_h - h)//2)
		root.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")
		# Prevent resizing beyond work area
		root.maxsize(work_w, work_h)
	except Exception:
		pass

# Apply once after widgets are laid out
root.after(120, _fit_to_work_area)


# Use a modern font for all widgets
default_font = ("Segoe UI", 11)
root.option_add("*Font", default_font)
# Create a scrollable container for the whole app content (helps on small screens)
scroll_container = ttk.Frame(root)
scroll_container.pack(fill='both', expand=True)

scroll_canvas = tk.Canvas(scroll_container, borderwidth=0, highlightthickness=0)
vscroll = ttk.Scrollbar(scroll_container, orient="vertical", command=scroll_canvas.yview)
scroll_canvas.configure(yscrollcommand=vscroll.set)
vscroll.pack(side="right", fill="y")
scroll_canvas.pack(side="left", fill="both", expand=True)

# Inner content frame that holds all actual widgets
content = ttk.Frame(scroll_canvas)
_content_win = scroll_canvas.create_window((0, 0), window=content, anchor="nw")

def _on_content_configure(event):
	try:
		scroll_canvas.configure(scrollregion=scroll_canvas.bbox("all"))
	except Exception:
		pass
content.bind("<Configure>", _on_content_configure)

def _on_canvas_configure(event):
	try:
		# Make inner content width follow the canvas width
		scroll_canvas.itemconfig(_content_win, width=event.width)
	except Exception:
		pass
scroll_canvas.bind("<Configure>", _on_canvas_configure)

def _on_mousewheel_canvas(event):
	try:
		delta = -1 * int(event.delta / 120)
	except Exception:
		delta = -1
	scroll_canvas.yview_scroll(delta, 'units')
	return 'break'
# Bind globally so wheel scrolls the page when hovering anywhere (Windows)
scroll_canvas.bind_all('<MouseWheel>', _on_mousewheel_canvas, add='+')

frame = ttk.Frame(content)
frame.pack(padx=12, pady=12)


ttk.Label(frame, text="Player name:").grid(row=0, column=0, sticky="w")
entry = ttk.Entry(frame, width=40)
entry.grid(row=0, column=1, padx=6)
# Allow pressing Enter in the name field to trigger search
entry.bind("<Return>", lambda e: on_search())
# Add icon to Search button if available
try:
	from tkinter import PhotoImage
	search_icon = PhotoImage(file="search_icon.png")  # Place a PNG icon in your folder
	btn = ttk.Button(frame, text="Search", image=search_icon, compound="left", command=on_search, style='Black.TButton')
except Exception:
	btn = ttk.Button(frame, text="Search", command=on_search, style='Black.TButton')
btn.grid(row=0, column=2, padx=6)


# Color selection for stats filtering: Both / White / Black
color_var = tk.StringVar(value="Both")
ttk.Label(frame, text="Color:").grid(row=1, column=0, sticky="w")
color_options = ["Both", "White", "Black"]
color_menu = ttk.OptionMenu(frame, color_var, color_options[0], *color_options)
color_menu.grid(row=1, column=1, sticky="w", padx=6)
def _on_selection_change(*_):
	show_profile()
	if HAS_MPL:
		if chart_mode == 'results':
			refresh_chart()
color_var.trace_add("write", _on_selection_change)
# Format selection for stats filtering: All / Standard / Rapid / Blitz
format_var = tk.StringVar(value="All")
ttk.Label(frame, text="Format:").grid(row=1, column=2, sticky="w")
format_options = ["All", "Standard", "Rapid", "Blitz"]
format_menu = ttk.OptionMenu(frame, format_var, format_options[0], *format_options)
format_menu.grid(row=1, column=3, sticky="w", padx=6)
format_var.trace_add("write", _on_selection_change)


status = tk.StringVar(value="")
ttk.Label(content, textvariable=status).pack(padx=8, pady=(0, 6))

# Chart area (optional)

chart_frame = ttk.Frame(content)
chart_frame.pack(padx=12, pady=(0, 12), fill='both', expand=True)

# Create a matplotlib figure and canvas with two subplots (rating timeline + pie chart)
if HAS_MPL:
	fig = Figure(figsize=(9, 4), dpi=100)
	chart_ax = fig.add_subplot(111)
	fig.tight_layout(pad=2.0)
	canvas = FigureCanvasTkAgg(fig, master=chart_frame)
	canvas_widget = canvas.get_tk_widget()
	canvas_widget.pack(side=tk.TOP, fill='both', expand=True)
	try:
		toolbar = NavigationToolbar2Tk(canvas, chart_frame)
		toolbar.update()
		canvas._tkcanvas.pack(side=tk.TOP, fill='x')
	except Exception:
		pass
else:
	chart_ax = None
	fig = None


# Add chart mode buttons
if HAS_MPL:
	mode_frame = ttk.Frame(chart_frame)
	mode_frame.pack(fill='x', pady=4)

	def _set_mode(m):
		global chart_mode
		chart_mode = m
		refresh_chart()

	ttk.Button(mode_frame, text='Rating Timeline', command=lambda: _set_mode('rating'), style='Black.TButton').pack(side='left', padx=4)
	ttk.Button(mode_frame, text='Results Pie', command=lambda: _set_mode('results'), style='Black.TButton').pack(side='left', padx=4)

	# Timeline controls (visible only useful in rating mode)
	controls = ttk.Frame(chart_frame)
	controls.pack(fill='x', pady=2)

	# Timespan dropdown
	ttk.Label(controls, text='Timespan:').pack(side='left')
	ts_var = tk.StringVar(value='3y')
	def on_ts_change(*_):
		global timespan
		timespan = ts_var.get()
		if chart_mode == 'rating':
			refresh_chart()
	timespan_options = ['1y', '3y', 'all']
	timespan_menu = ttk.OptionMenu(controls, ts_var, timespan_options[1], *timespan_options, command=lambda *_: on_ts_change())
	timespan_menu.pack(side='left', padx=4)
	ts_var.trace_add('write', on_ts_change)

	# Smoothing checkbox
	sm_var = tk.BooleanVar(value=False)
	def on_sm_change(*_):
		global smoothing_enabled
		smoothing_enabled = bool(sm_var.get())
		if chart_mode == 'rating':
			refresh_chart()
	ttk.Checkbutton(controls, text='Smooth', variable=sm_var, command=on_sm_change).pack(side='left', padx=8)


	# Remove 'Show' checkboxes. Use format dropdown to control chart series.
	def update_visible_formats(*_):
		fmt = format_var.get()
		if fmt == 'All':
			visible_formats['standard'] = True
			visible_formats['rapid'] = True
			visible_formats['blitz'] = True
		else:
			for k in ['standard', 'rapid', 'blitz']:
				visible_formats[k] = (k == fmt.lower())
		refresh_chart()
	format_var.trace_add('write', update_visible_formats)

	# Ensure all buttons use black text across themes
	from tkinter import font as tkfont
	style = ttk.Style(root)
	style.configure('Black.TButton', foreground='#000000')
	# Keep black text in normal/active/pressed; lighter when disabled
	style.map('Black.TButton', foreground=[('disabled', '#7a7a7a'), ('!disabled', '#000000')])

	# Export CSV button
	def export_csv(full=True):
		# Export rating time series plus aggregate stats columns.
		if _rating_df is None or _rating_player_id is None or last_profile is None:
			messagebox.showinfo("Export CSV", "Load a player rating timeline first.")
			return
		import pandas as _pd
		base_df = _rating_df.copy()
		# If exporting visible subset apply current timespan/smoothing/visibility.
		if not full:
			try:
				now = _pd.Timestamp.now()
				if timespan == '1y':
					cutoff = now - _pd.DateOffset(years=1)
				elif timespan == '3y':
					cutoff = now - _pd.DateOffset(years=3)
				else:
					cutoff = None
				if cutoff is not None:
					base_df = base_df[base_df.index >= cutoff]
				if smoothing_enabled:
					for c in base_df.columns:
						base_df[c] = base_df[c].rolling(window=3, min_periods=1).mean()
			except Exception:
				pass
		# Apply visibility filter
		vis_cols = [c for c in base_df.columns if visible_formats.get(c, False)]
		base_df = base_df[vis_cols]
		profile = last_profile
		# Append aggregate columns (overall + per-color and per-format totals if available)
		agg = {
			'aggregate_total_games': (profile.get('total_wins') or 0) + (profile.get('total_draws') or 0) + (profile.get('total_losses') or 0) if profile.get('total_wins') is not None else None,
			'aggregate_wins': profile.get('total_wins'),
			'aggregate_draws': profile.get('total_draws'),
			'aggregate_losses': profile.get('total_losses'),
			'aggregate_win_ratio': profile.get('win_ratio'),
			'aggregate_draw_ratio': profile.get('draw_ratio'),
			'aggregate_loss_ratio': profile.get('loss_ratio'),
		}
		pc = profile.get('per_color') or {}
		for side in ('white','black'):
			info = pc.get(side) or {}
			agg[f'{side}_total'] = info.get('total')
			agg[f'{side}_wins'] = info.get('wins')
			agg[f'{side}_draws'] = info.get('draws')
			agg[f'{side}_losses'] = info.get('losses')
		# Flatten formats summary
		fmts = profile.get('formats') or {}
		for fmt in ('standard','rapid','blitz'):
			fi = fmts.get(fmt) or {}
			agg[f'{fmt}_fmt_total'] = fi.get('total')
			agg[f'{fmt}_fmt_wins'] = fi.get('wins')
			agg[f'{fmt}_fmt_draws'] = fi.get('draws')
			agg[f'{fmt}_fmt_losses'] = fi.get('losses')
		# Convert agg dict to a one-row DataFrame and join (as metadata columns with same value for all rows)
		meta_df = _pd.DataFrame({k: [v]*len(base_df) for k,v in agg.items()})
		out_df = base_df.reset_index().rename(columns={'dt':'date'}) if 'dt' in base_df.columns else base_df.reset_index()
		out_df = _pd.concat([out_df, meta_df], axis=1)
		kind = 'full' if full else 'visible'
		# Prefer Excel as default so we can include Metrics sheet
		default_name = f"ratings_{_rating_player_id}_{kind}.xlsx"
		path = filedialog.asksaveasfilename(
			defaultextension=".xlsx",
			filetypes=[("Excel Workbook", "*.xlsx"), ("CSV files", "*.csv")],
			initialfile=default_name,
			title="Save rating series (Excel supports Metrics sheet)"
		)
		if not path:
			return
		try:
			if path.lower().endswith('.xlsx'):
				# Build metrics sheets
				opp_label = opponent_var.get() if opponent_var is not None else 'All Opponents'
				opp_id = _opponent_map.get(opp_label)
				entry = _get_vs_entry(_rating_player_id, opp_id) if opp_id else None

				def _counts_from_profile(profile, color_sel, format_sel):
					# returns dict wins,draws,losses,total
					if color_sel == 'Both':
						if format_sel == 'All':
							w = profile.get('total_wins'); d = profile.get('total_draws'); l = profile.get('total_losses')
							t = (w or 0) + (d or 0) + (l or 0) if (w is not None and d is not None and l is not None) else None
							return {'wins': w, 'draws': d, 'losses': l, 'total': t}
						else:
							fi = (profile.get('formats') or {}).get(format_sel.lower()) or {}
							w = fi.get('wins'); d = fi.get('draws'); l = fi.get('losses'); t = fi.get('total')
							return {'wins': w, 'draws': d, 'losses': l, 'total': t}
					else:
						key = 'white' if color_sel == 'White' else 'black'
						if format_sel == 'All':
							ci = (profile.get('per_color') or {}).get(key) or {}
							w = ci.get('wins'); d = ci.get('draws'); l = ci.get('losses'); t = ci.get('total')
							return {'wins': w, 'draws': d, 'losses': l, 'total': t}
						else:
							fi = (profile.get('formats') or {}).get(format_sel.lower()) or {}
							ci = fi.get(key) or {}
							w = ci.get('wins'); d = ci.get('draws'); l = ci.get('losses'); t = ci.get('total')
							return {'wins': w, 'draws': d, 'losses': l, 'total': t}

				def _exp_color_counts(profile, entry, fmt_label):
					# returns (w_counts, b_counts) dicts with wins,draws,losses for selected format
					if entry:
						def gi(k):
							try:
								v = entry.get(k); return int(str(v).replace(',', '')) if v is not None else 0
							except Exception:
								return 0
						fsuf = 'std' if fmt_label=='Standard' else ('rpd' if fmt_label=='Rapid' else ('blz' if fmt_label=='Blitz' else None))
						if fsuf is None:
							wt = gi('white_total'); ww = gi('white_win_num'); wd = gi('white_draw_num')
							bt = gi('black_total'); bw = gi('black_win_num'); bd = gi('black_draw_num')
						else:
							wt = gi(f'white_total_{fsuf}'); ww = gi(f'white_win_num_{fsuf}'); wd = gi(f'white_draw_num_{fsuf}')
							bt = gi(f'black_total_{fsuf}'); bw = gi(f'black_win_num_{fsuf}'); bd = gi(f'black_draw_num_{fsuf}')
						w_counts = {'wins': ww, 'draws': wd, 'losses': max(wt-ww-wd,0), 'total': wt}
						b_counts = {'wins': bw, 'draws': bd, 'losses': max(bt-bw-bd,0), 'total': bt}
						return w_counts, b_counts
					else:
						fmtkey = fmt_label.lower() if fmt_label!='All' else None
						if fmtkey is None:
							winfo = (profile.get('per_color') or {}).get('white') or {}
							binfo = (profile.get('per_color') or {}).get('black') or {}
							w_counts = {'wins': winfo.get('wins'), 'draws': winfo.get('draws'), 'losses': winfo.get('losses'), 'total': winfo.get('total')}
							b_counts = {'wins': binfo.get('wins'), 'draws': binfo.get('draws'), 'losses': binfo.get('losses'), 'total': binfo.get('total')}
							return w_counts, b_counts
						fi = (profile.get('formats') or {}).get(fmtkey) or {}
						winfo = fi.get('white') or {}
						binfo = fi.get('black') or {}
						w_counts = {'wins': winfo.get('wins'), 'draws': winfo.get('draws'), 'losses': winfo.get('losses'), 'total': winfo.get('total')}
						b_counts = {'wins': binfo.get('wins'), 'draws': binfo.get('draws'), 'losses': binfo.get('losses'), 'total': binfo.get('total')}
						return w_counts, b_counts

				def _pi_er(w, d, l):
					t = (w or 0) + (d or 0) + (l or 0)
					if not t:
						return None, None
					pi_points = (w or 0) + 0.5*(d or 0)
					return pi_points, (pi_points / t) * 100.0

				metrics_rows = []
				for fmt_label in ['All','Standard','Rapid','Blitz']:
					for color_label in ['Both','White','Black']:
						if entry and opp_id:
							vals = _compute_vs_filtered(entry, color_label, fmt_label)
							w,d,l,t = vals.get('wins'), vals.get('draws'), vals.get('losses'), vals.get('total')
						else:
							vals = _counts_from_profile(last_profile, color_label, fmt_label)
							w,d,l,t = vals.get('wins'), vals.get('draws'), vals.get('losses'), vals.get('total')
						pi_points, er = _pi_er(w,d,l)
						metrics_rows.append({
							'format': fmt_label,
							'color': color_label,
							'opponent': opp_label if opp_id else 'All',
							'opponent_id': opp_id if opp_id else None,
							'total': t,
							'wins': w, 'draws': d, 'losses': l,
							'performance_index_points': round(pi_points, 2) if pi_points is not None else None,
							'efficiency_rating_percent': round(er, 2) if er is not None else None,
						})

				expected_rows = []
				for fmt_label in ['All','Standard','Rapid','Blitz']:
					w_counts, b_counts = _exp_color_counts(last_profile, entry, fmt_label)
					def _exp(cnt):
						t = (cnt.get('wins') or 0) + (cnt.get('draws') or 0) + (cnt.get('losses') or 0)
						return ((cnt.get('wins') or 0)/t + 0.5*((cnt.get('draws') or 0)/t)) if t else None
					Ew = _exp(w_counts) if w_counts else None
					Eb = _exp(b_counts) if b_counts else None
					delta = (Ew - Eb) if (Ew is not None and Eb is not None) else None
					expected_rows.append({
						'format': fmt_label,
						'opponent': opp_label if opp_id else 'All',
						'opponent_id': opp_id if opp_id else None,
						'expected_white': round(Ew, 3) if Ew is not None else None,
						'expected_black': round(Eb, 3) if Eb is not None else None,
						'color_advantage': round(delta, 3) if delta is not None else None,
					})

				metrics_df = _pd.DataFrame(metrics_rows)
				expected_df = _pd.DataFrame(expected_rows)
				with _pd.ExcelWriter(path, engine='openpyxl') as writer:
					out_df.to_excel(writer, index=False, sheet_name='Ratings')
					metrics_df.to_excel(writer, index=False, sheet_name='Metrics')
					expected_df.to_excel(writer, index=False, sheet_name='Expected')
				messagebox.showinfo("Export", f"Saved Ratings + Metrics to:\n{path}")
			else:
				# CSV path: write only ratings CSV
				out_df.to_csv(path, index=False)
				messagebox.showinfo("Export CSV", f"Saved {kind} series with aggregates to:\n{path}\n\nTip: choose Excel (.xlsx) to include Metrics sheets.")
		except Exception as e:
			messagebox.showerror("Export", str(e))

	# Add icon to Export Full CSV button if available
	try:
		export_icon = PhotoImage(file="export_icon.png")
		ttk.Button(controls, text='Export Full CSV', image=export_icon, compound="left", command=lambda: export_csv(full=True), style='Black.TButton').pack(side='right', padx=6)
	except Exception:
		ttk.Button(controls, text='Export Full CSV', command=lambda: export_csv(full=True), style='Black.TButton').pack(side='right', padx=6)

	# Export Calculations CSV (period deltas)
	def export_calculations_csv():
		if not last_profile:
			messagebox.showinfo("Export Calculations CSV", "Search and select a player first.")
			return
		pid = last_profile.get('player_id')
		default_name = f"calculations_{pid}.csv"
		path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")], initialfile=default_name, title="Save calculations as CSV")
		if not path:
			return
		status.set("Fetching calculations index…")
		root.update_idletasks()
		try:
			import pandas as _pd
			df = get_calculations_index(pid)
			if isinstance(df, list):
				df = _pd.DataFrame(df)
			if df is None or df.empty:
				messagebox.showwarning("Export Calculations CSV", "No calculation periods found.")
				status.set("")
				return
			# attach player context columns
			df.insert(0, 'player_id', pid)
			df.insert(1, 'player_name', last_profile.get('name'))
			df.to_csv(path, index=False)
			messagebox.showinfo("Export Calculations CSV", f"Saved {len(df)} period rows to:\n{path}")
		except Exception as e:
			messagebox.showerror("Export Calculations CSV", str(e))
		finally:
			status.set("")

	# Place export buttons
	try:
		calc_icon = PhotoImage(file="calc_icon.png")
		ttk.Button(controls, text='Export Calculations CSV', image=calc_icon, compound="left", command=export_calculations_csv, style='Black.TButton').pack(side='right', padx=6)
	except Exception:
		ttk.Button(controls, text='Export Calculations CSV', command=export_calculations_csv, style='Black.TButton').pack(side='right', padx=6)

	# Export Opponents CSV (aggregated vs each opponent)
	def export_opponents_csv():
		if not last_profile:
			messagebox.showinfo("Export Opponents CSV", "Search and select a player first.")
			return
		pid = last_profile.get('player_id')
		default_name = f"opponents_{pid}.csv"
		path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")], initialfile=default_name, title="Save opponents summary as CSV")
		if not path:
			return
		prog = tk.Toplevel(root)
		prog.title("Exporting opponents…")
		lbl = tk.Label(prog, text=f"Fetching opponent summaries for {last_profile.get('name')} ({pid})…")
		lbl.pack(padx=12, pady=12)
		prog.transient(root)
		prog.grab_set()

		def _worker(p):
			try:
				import scrape_fide
				df = scrape_fide.get_opponents_aggregate(p, timeout=40)
				return (None, df)
			except Exception as e:
				return (e, None)

		fut = executor.submit(_worker, pid)

		def _done(f):
			try:
				err, df = f.result()
			except Exception as e:
				err, df = e, None
			def _ui():
				try:
					prog.destroy()
				except Exception:
					pass
				if err:
					messagebox.showerror("Export Opponents CSV", str(err))
					return
				try:
					import pandas as _pd
					out_df = df if not isinstance(df, list) else _pd.DataFrame(df)
					if out_df is None:
						out_df = _pd.DataFrame()
					out_df.insert(0, 'player_id', pid)
					out_df.insert(1, 'player_name', last_profile.get('name'))
					out_df.to_csv(path, index=False)
					messagebox.showinfo("Export Opponents CSV", f"Saved {len(out_df)} opponents to:\n{path}")
				except Exception as ex:
					messagebox.showerror("Export Opponents CSV", str(ex))
			root.after(0, _ui)

		fut.add_done_callback(_done)

	try:
		opp_icon = PhotoImage(file="opponents_icon.png")
		ttk.Button(controls, text='Export Opponents CSV', image=opp_icon, compound="left", command=export_opponents_csv, style='Black.TButton').pack(side='right', padx=6)
	except Exception:
		ttk.Button(controls, text='Export Opponents CSV', command=export_opponents_csv, style='Black.TButton').pack(side='right', padx=6)

	# Opponent selector UI (placed near top controls)
	opponents_frame = ttk.Frame(frame)
	opponents_frame.grid(row=2, column=0, columnspan=4, sticky='w', pady=(6,0))
	ttk.Label(opponents_frame, text="Opponent:").pack(side='left')
	opponent_var = tk.StringVar(value="All Opponents")
	opponent_cb = ttk.Combobox(opponents_frame, textvariable=opponent_var, values=("All Opponents",), state='normal', width=50)
	opponent_cb.pack(side='left', padx=6)
	def _on_opponent_change(*_):
		show_profile()
		if HAS_MPL and chart_mode == 'results':
			refresh_chart()
	opponent_var.trace_add('write', _on_opponent_change)

	# Type-to-search within combobox: jump to first label starting with typed text

	def _on_opp_typed(event):
		try:
			text = opponent_var.get() or ''
			labels = _opponent_labels or list(_opponent_map.keys()) or ["All Opponents"]
			if not text:
				return
			low = text.lower()
			match = None
			for lab in labels:
				if lab.lower().startswith(low) or low in lab.lower():
					match = lab
					break
			if match and match != text:
				opponent_cb.set(match)
				try:
					opponent_cb.icursor(len(text))
					opponent_cb.selection_range(len(text), tk.END)
				except Exception:
					pass
		except Exception:
			pass

	opponent_cb.bind('<KeyRelease>', _on_opp_typed)
	opponent_cb.bind('<<ComboboxSelected>>', lambda e: _on_opponent_change())

	# --- Tooltips ---
	class ToolTip:
		def __init__(self, widget, text):
			self.widget = widget
			self.text = text
			self.tipwindow = None
			widget.bind("<Enter>", self.show_tip)
			widget.bind("<Leave>", self.hide_tip)
		def show_tip(self, event=None):
			if self.tipwindow or not self.text:
				return
			x, y, cx, cy = self.widget.bbox("insert") if hasattr(self.widget, "bbox") else (0,0,0,0)
			x = x + self.widget.winfo_rootx() + 25
			y = y + self.widget.winfo_rooty() + 20
			self.tipwindow = tw = tk.Toplevel(self.widget)
			tw.wm_overrideredirect(True)
			tw.wm_geometry(f"+{x}+{y}")
			label = tk.Label(tw, text=self.text, justify=tk.LEFT,
							 background="#ffffe0", relief=tk.SOLID, borderwidth=1,
							 font=("Segoe UI", 9))
			label.pack(ipadx=1)
		def hide_tip(self, event=None):
			tw = self.tipwindow
			self.tipwindow = None
			if tw:
				tw.destroy()

	# Add tooltips to key widgets
	ToolTip(entry, "Enter FIDE player name and press Enter or Search")
	ToolTip(btn, "Search for player by name")
	ToolTip(opponent_cb, "Select opponent for detailed stats")
	ToolTip(chart_frame, "Rating timeline and results pie chart")
	# Add more tooltips as needed

	def _get_vs_entry(player_id, opponent_id):
		"""Get cached vs-opponent stats entry for (player,opponent)."""
		key = (str(player_id), str(opponent_id))
		if key in _opp_stats_cache:
			return _opp_stats_cache[key]
		try:
			entry = get_stats_vs_opponent(player_id, opponent_id, timeout=25)
			if entry:
				_opp_stats_cache[key] = entry
			return entry
		except Exception:
			return None

	def _compute_vs_filtered(entry, color_sel, format_sel):
		"""Compute wins/draws/losses dict from a vs-opponent entry based on filters."""
		def gi(k):
			try:
				v = entry.get(k)
				return int(str(v).replace(',', '')) if v is not None else 0
			except Exception:
				return 0
		fmt_suffix = None
		if format_sel == 'Standard':
			fmt_suffix = 'std'
		elif format_sel == 'Rapid':
			fmt_suffix = 'rpd'
		elif format_sel == 'Blitz':
			fmt_suffix = 'blz'
		if color_sel == 'Both':
			if fmt_suffix is None:
				w_total = gi('white_total'); b_total = gi('black_total')
				wins = gi('white_win_num') + gi('black_win_num')
				draws = gi('white_draw_num') + gi('black_draw_num')
				losses = max((w_total + b_total) - wins - draws, 0)
				total = w_total + b_total
			else:
				w_total = gi(f'white_total_{fmt_suffix}')
				b_total = gi(f'black_total_{fmt_suffix}')
				wins = gi(f'white_win_num_{fmt_suffix}') + gi(f'black_win_num_{fmt_suffix}')
				draws = gi(f'white_draw_num_{fmt_suffix}') + gi(f'black_draw_num_{fmt_suffix}')
				losses = max((w_total + b_total) - wins - draws, 0)
				total = w_total + b_total
		else:
			side = 'white' if color_sel == 'White' else 'black'
			if fmt_suffix is None:
				total = gi(f'{side}_total')
				wins = gi(f'{side}_win_num')
				draws = gi(f'{side}_draw_num')
				losses = max(total - wins - draws, 0)
			else:
				total = gi(f'{side}_total_{fmt_suffix}')
				wins = gi(f'{side}_win_num_{fmt_suffix}')
				draws = gi(f'{side}_draw_num_{fmt_suffix}')
				losses = max(total - wins - draws, 0)
		return {'total': total, 'wins': wins, 'draws': draws, 'losses': losses}

def update_pie_chart(profile):
	"""Draw results pie for current color/format selection into single chart axis."""
	if not HAS_MPL or chart_ax is None or not profile:
		return
	chart_ax.clear()
	color_sel = color_var.get()
	format_sel = format_var.get()
	wins = draws = losses = None
	# Apply opponent filter when selected
	opp_label = opponent_var.get() if opponent_var is not None else 'All Opponents'
	opp_id = _opponent_map.get(opp_label)
	if opp_id:
		entry = _get_vs_entry(profile.get('player_id'), opp_id)
		if entry:
			vals = _compute_vs_filtered(entry, color_sel, format_sel)
			wins, draws, losses = vals.get('wins'), vals.get('draws'), vals.get('losses')
	else:
		# No opponent selected -> use overall profile aggregates
		if color_sel == 'Both':
			if format_sel == 'All':
				wins = profile.get('total_wins')
				draws = profile.get('total_draws')
				losses = profile.get('total_losses')
			else:
				fmt = profile.get('formats', {}).get(format_sel.lower()) or {}
				wins = fmt.get('wins')
				draws = fmt.get('draws')
				losses = fmt.get('losses')
		else:
			key = 'white' if color_sel == 'White' else 'black'
			if format_sel == 'All':
				colinfo = (profile.get('per_color') or {}).get(key) or {}
				wins = colinfo.get('wins')
				draws = colinfo.get('draws')
				losses = colinfo.get('losses')
			else:
				fmt = profile.get('formats', {}).get(format_sel.lower()) or {}
				colinfo = fmt.get(key) or {}
				wins = colinfo.get('wins')
				draws = colinfo.get('draws')
				losses = colinfo.get('losses')
	if any(v is None for v in (wins, draws, losses)):
		chart_ax.text(0.5, 0.5, 'Selected results unavailable', ha='center', va='center', transform=chart_ax.transAxes)
		try: canvas.draw()
		except Exception: pass
		return
	total = (wins or 0) + (draws or 0) + (losses or 0)
	if not total:
		chart_ax.text(0.5, 0.5, 'No games', ha='center', va='center', transform=chart_ax.transAxes)
		try: canvas.draw()
		except Exception: pass
		return
	sizes = [wins/total, draws/total, losses/total]
	labels = [f'Wins ({wins})', f'Draws ({draws})', f'Losses ({losses})']
	colors = ['tab:green', 'tab:blue', 'tab:red']
	chart_ax.pie(sizes, labels=labels, colors=colors, autopct=lambda pct: f"{pct:.1f}%", startangle=90, pctdistance=0.75)
	chart_ax.axis('equal')
	if opp_id:
		chart_ax.set_title(f'Results vs {opp_label}: {color_sel} / {format_sel}')
	else:
		chart_ax.set_title(f'Results: {color_sel} / {format_sel}')
	try: canvas.draw()
	except Exception: pass

# Scrollable output area for statistics
output_frame = tk.Frame(content)
output_frame.pack(padx=12, pady=(0, 12), fill='both', expand=True)
output_scroll = tk.Scrollbar(output_frame, orient='vertical')
output_scroll.pack(side=tk.RIGHT, fill='y')
output = tk.Text(output_frame, width=110, height=18, wrap='word', yscrollcommand=output_scroll.set)
output.pack(side=tk.LEFT, fill='both', expand=True)
output_scroll.config(command=output.yview)

# Smooth mouse-wheel scrolling for the stats Text (when hovered)
def _bind_mousewheel(widget, target):
	def _on_mousewheel(event):
		try:
			delta = -1 * int(event.delta/120)
		except Exception:
			delta = -1
		target.yview_scroll(delta, 'units')
		return 'break'
	# Bind only on the specific widget to avoid conflicting with page scroll
	widget.bind('<MouseWheel>', _on_mousewheel, add='+')
_bind_mousewheel(output, output)

root.mainloop()


