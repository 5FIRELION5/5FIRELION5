# FIDE Player Lookup

A desktop app to search FIDE players, view ratings (Standard/Rapid/Blitz), analyze results, filter by opponent, color and format, visualize charts, and export rich CSV/XLSX files with metrics.

- Zero-install downloads via GitHub Releases (prebuilt EXE for Windows)
- Opponent-aware stats and pie chart
- Metrics: Performance Index (points), Efficiency Rating (0–100), Expected Score per Color, Color Advantage Δ
- Exports: Ratings time series (CSV/XLSX with extra sheets), Calculations CSV, Opponents CSV

## Download (no install required)

Head to the Releases page of this repository and download the latest `FIDE-Player-Lookup.exe`. Double-click to run on Windows. No Python or dependencies required.

If SmartScreen warns you, click “More info” → “Run anyway”. The binary is built by GitHub Actions from this source.

## Build locally (optional)

If you want to run from source:
- Python 3.10+ recommended
- `pip install -r requirements.txt`
- Run `python main.py`

Or build your own standalone EXE:
- `pip install pyinstaller`
- `pyinstaller --noconfirm --onefile --name FIDE-Player-Lookup --collect-all matplotlib --collect-all pandas --hidden-import matplotlib.backends.backend_tkagg main.py`

Artifacts will be in `dist/`.

## Exports

- Ratings: CSV or Excel (.xlsx). Excel includes extra sheets:
  - Metrics (Performance Index points, Efficiency % for each format/color)
  - Expected (Expected score White/Black and Δ per format)
- Calculations CSV: Period-by-period changes with links
- Opponents CSV: One row per opponent with totals and per-format splits

## License

MIT — see `LICENSE`.

## Disclaimer

- Not affiliated with FIDE. Data fetched from public FIDE endpoints.
- Please respect FIDE’s terms of use and rate limits. This tool caches data locally to reduce repeated requests.

## Security and Privacy

This app makes outbound HTTPS requests to ratings.fide.com to retrieve public player data. No credentials are collected or stored.

## Acknowledgments

Thanks to the FIDE website for providing public access to ratings and statistics.