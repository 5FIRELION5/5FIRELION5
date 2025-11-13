from scrape_fide import get_player_profile
import json, sys, traceback

pid = '1503014'
try:
    p = get_player_profile(pid)
    print('raw_profile_url:', p.get('raw_profile_url'))
    print('\n=== formats ===')
    print(json.dumps(p.get('formats'), indent=2))
    print('\n=== per_color ===')
    print(json.dumps(p.get('per_color'), indent=2))
    print('\n=== totals ===')
    print('wins, draws, losses:', p.get('total_wins'), p.get('total_draws'), p.get('total_losses'))
    print('\n=== ratings ===')
    print(json.dumps(p.get('ratings'), indent=2))
except Exception as e:
    traceback.print_exc()
    sys.exit(1)
