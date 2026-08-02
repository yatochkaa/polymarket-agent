"""ЗАДАЧА 3: есть ли дубли токенов в discovery (причина 2x событий)?"""
import httpx
import sys
sys.path.insert(0, r"C:\Users\awf\Desktop\test")

from src.validate.discovery import updown_outcomes

GAMMA_URL = "https://gamma-api.polymarket.com"
with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as gc:
    res = updown_outcomes(gc)

tokens = [o.token_id for o in res.outcomes]
print("всего токенов:", len(tokens))
print("уникальных:", len(set(tokens)))
from collections import Counter
c = Counter(tokens)
dups = {t: n for t, n in c.items() if n > 1}
print("дублей токенов:", len(dups))
for t, n in list(dups.items())[:5]:
    print("  ", t[:16], "x", n)
print("slugs уникальных:", len(set(o.market_slug for o in res.outcomes)))
