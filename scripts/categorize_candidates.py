"""Categorize current snapshot candidates by question text patterns."""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "polymarket.db"

# First match wins — order matters.
RULES: list[tuple[str, list[str]]] = [
    ("politics_government", [
        r"\btrump\b", r"\bbiden\b", r"\bharris\b", r"\bobama\b", r"\bvance\b",
        r"\bputin\b", r"\bzelensk", r"\bnetanyahu\b", r"\borban\b",
        r"\bxi\s+jinping", r"\bkim\s+jong", r"\bmodi\b", r"\bmacron\b",
        r"\belection\b", r"\bprimar(?:y|ies)\b", r"\bvote\b", r"\bvoter",
        r"\bsenate\b", r"\bcongress\b", r"\bhouse\s+(?:of|majority)",
        r"\bsupreme\s+court\b", r"\bscotus\b",
        r"\bpresident\b", r"\bpresidential\b",
        r"\bgovernor\b", r"\bmayor\b",
        r"\bcabinet\b", r"\bsecretary\s+of\b", r"\battorney\s+general\b",
        r"\bdemocrat", r"\brepublican", r"\bgop\b",
        r"\bprime\s+minister", r"\bparliament", r"\bchancellor\b",
        r"\bimpeach", r"\bindict", r"\bsubpoena", r"\bpardon",
        r"\b(?:un|nato|eu|g7|g20)\b", r"\btariff", r"\bsanction",
        r"\bdeport", r"\bborder\b", r"\bimmigrat",
        r"\bministr",
        r"\bbill\s+pass", r"\bsign\s+into\s+law", r"\blegislation",
        r"\bbrexit\b", r"\breferendum",
    ]),
    ("crypto_finance", [
        r"\bbitcoin\b", r"\bbtc\b", r"\bethereum\b", r"\beth\b",
        r"\bsolana\b", r"\bsol\b", r"\bdoge\b", r"\bxrp\b", r"\bbnb\b",
        r"\bhyperliquid\b", r"\bcardano\b", r"\bavax\b", r"\bmemecoin\b",
        r"\bcrypto\b", r"\bstable\s*coin", r"\bdefi\b", r"\bnft\b",
        r"\b(?:fed|federal\s+reserve|fomc)\b", r"\binterest\s+rate",
        r"\brate\s+(?:cut|hike|decrease|increase)\b", r"\bbasis\s+point",
        r"\binflation\b", r"\bcpi\b", r"\bunemployment",
        r"\bipo\b", r"\bmarket\s+cap\b",
        r"\b(?:nasdaq|s&p|dow|russell)\b",
        r"\bstock\b", r"\bequit", r"\bbond\b", r"\byield\b",
        r"\boil\s+price", r"\bgold\s+price",
        r"\brecession", r"\bbear\s+market", r"\bbull\s+market",
        r"\bath\b", r"\ball[- ]time\s+high",
    ]),
    ("science_tech", [
        r"\bai\b", r"\bagi\b", r"\bllm\b", r"\bgpt[- ]?\d?", r"\bchatgpt\b",
        r"\bclaude\b", r"\banthropic\b", r"\bopenai\b", r"\bllama\b",
        r"\bgemini\b", r"\bgrok\b", r"\bdeepseek\b", r"\bcopilot\b",
        r"\bnvidia\b", r"\bblackwell\b", r"\bgpu\b",
        r"\btesla\b", r"\bspacex\b", r"\bstarship\b", r"\bfalcon\s+\d",
        r"\bblue\s+origin\b", r"\bnew\s+glenn\b", r"\bartemis\b",
        r"\bapple\b", r"\biphone\b", r"\bvision\s+pro\b", r"\bairpods\b",
        r"\bgoogle\b", r"\bmicrosoft\b", r"\bmeta\b", r"\bfacebook\b",
        r"\bandroid\b", r"\bios\b",
        r"\brobot", r"\bautonom", r"\bself[- ]driv", r"\bfsd\b",
        r"\bquantum\b", r"\brocket\b", r"\bsatellite\b",
        r"\bmars\b", r"\bmoon\s+land", r"\blunar\b",
    ]),
    ("sports", [
        r"\bnba\b", r"\bnfl\b", r"\bnhl\b", r"\bmlb\b", r"\bmls\b",
        r"\bufc\b", r"\bmma\b", r"\bf1\b", r"\bformula\s+1\b",
        r"\bsuper\s+bowl", r"\bworld\s+cup\b", r"\bworld\s+series\b",
        r"\bchampions\s+league\b", r"\bla\s+liga\b",
        r"\bpremier\s+league\b", r"\bbundesliga\b", r"\bserie\s+a\b",
        r"\beuropa\s+league\b", r"\beuro\s+(?:202\d|cup)\b",
        r"\bcopa\s+america\b", r"\bafcon\b",
        r"\bplayoff", r"\bfinal[s]?\b", r"\bsemifinal", r"\btournament",
        r"\bmatch\b", r"\bbeat\b", r"\bdefeat\b",
        r"\bvs\.?\b",
        r"\bopen\s+(?:single|double|tournament)", r"\bgrand\s+slam",
        r"\bwimbledon\b", r"\baustralian\s+open\b", r"\bus\s+open\b",
        r"\bfrench\s+open\b", r"\broland\s+garros\b",
        r"\bgolf\b", r"\bpga\b", r"\bmasters\b", r"\bryder\s+cup\b",
        r"\bolympic", r"\bboxing\b",
        r"\b(?:lakers|celtics|warriors|knicks|bulls|heat|cavaliers|nuggets|"
        r"mavericks|grizzlies|76ers|nets|clippers|suns|kings|jazz|spurs|"
        r"rockets|pelicans|timberwolves|trail\s+blazers|raptors|hawks|"
        r"hornets|magic|wizards|pacers|bucks|pistons|thunder)\b",
        r"\b(?:patriots|cowboys|chiefs|49ers|eagles|ravens|steelers|packers|"
        r"bills|dolphins|jets|giants|commanders|falcons|saints|vikings|"
        r"bears|lions|seahawks|rams|cardinals|broncos|raiders|chargers|"
        r"titans|colts|texans|jaguars|bengals|browns|panthers|buccaneers)\b",
        r"\b(?:liverpool|chelsea|arsenal|manchester|tottenham|leeds|"
        r"barcelona|real\s+madrid|atletico|psg|bayern|juventus|milan|inter|"
        r"napoli|dortmund|ajax|porto|benfica)\b",
    ]),
    ("entertainment", [
        r"\boscar", r"\bgrammy", r"\bemmy", r"\bgolden\s+globe",
        r"\btony\s+award", r"\bbillboard\b", r"\bbox\s+office\b",
        r"\btaylor\s+swift\b", r"\bkanye\b", r"\bdrake\b", r"\bbeyonc",
        r"\brihanna\b", r"\beminem\b", r"\bweeknd\b", r"\bbad\s+bunny\b",
        r"\bkardashian\b", r"\bjenner\b",
        r"\bmovie\b", r"\bfilm\b", r"\balbum\b", r"\bsong\b", r"\bsingle\b",
        r"\bmusic\s+video\b", r"\bnetflix\b", r"\bdisney\b", r"\bmarvel\b",
        r"\bdc\s+comics\b", r"\bspotify\b",
        r"\brapper\b", r"\bsinger\b", r"\bactor\b", r"\bactress\b",
        r"\bcelebrity\b", r"\bdivorce\b", r"\bengaged\b",
        r"\bsnl\b", r"\btiktok\b", r"\binstagram\b",
        r"\bbachelor\b", r"\bsurvivor\b",
    ]),
    ("world_events", [
        r"\bukraine\b", r"\brussia\b", r"\bgaza\b", r"\bisrael\b",
        r"\bpalestin", r"\biran\b", r"\bsyria\b", r"\bafghan",
        r"\btaiwan\b", r"\bchina\b", r"\bnorth\s+korea\b",
        r"\bsouth\s+korea\b", r"\bvenezuela\b", r"\byemen\b", r"\bsudan\b",
        r"\bwar\b", r"\binvad", r"\bceasefire\b", r"\bhostage\b",
        r"\bnuclear\b", r"\bmissile\b", r"\bdrone\s+strike",
        r"\bhurricane\b", r"\bearthquake\b", r"\btsunami\b", r"\bvolcano\b",
        r"\bwildfire\b", r"\bclimate\b", r"\btemperature\b", r"\bweather\b",
        r"\bcovid", r"\bpandemic", r"\bvirus\b", r"\boutbreak\b",
        r"\brefugee", r"\bmigrant", r"\bprotest", r"\briot",
    ]),
]

CRYPTO_PRICE_PATTERN = re.compile(
    r"\b(bitcoin|ethereum|solana|hyperliquid|xrp|bnb|doge|cardano|avalanche)"
    r"\s+up\s+or\s+down\b",
    re.I,
)


def classify(question: str) -> str:
    q = (question or "").lower()
    if not q:
        return "other"
    if CRYPTO_PRICE_PATTERN.search(q):
        return "crypto_finance"
    for category, patterns in RULES:
        for pat in patterns:
            if re.search(pat, q, re.I):
                return category
    return "other"


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT id, question, COALESCE(volume, 0), COALESCE(liquidity, 0)
          FROM markets
         WHERE status = 'active'
           AND clob_token_ids IS NOT NULL
           AND clob_token_ids != ''
           AND clob_token_ids != '[]'
           AND (liquidity > 250000 OR volume > 500000)
           AND end_date IS NOT NULL
           AND end_date != ''
           AND (orderbook_missing = 0 OR orderbook_missing IS NULL)
           AND end_date > datetime('now', '+1 day')
        """
    ).fetchall()
    conn.close()

    buckets: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "vol_sum": 0.0, "liq_sum": 0.0, "examples": []}
    )
    for mid, q, vol, liq in rows:
        cat = classify(q)
        b = buckets[cat]
        b["count"] += 1
        b["vol_sum"] += vol
        b["liq_sum"] += liq
        if len(b["examples"]) < 3:
            b["examples"].append(q)

    total = len(rows)
    print(f"Total candidates classified: {total:,}")
    print()

    order = [
        "politics_government", "sports", "crypto_finance", "entertainment",
        "science_tech", "world_events", "other",
    ]

    print(
        f"{'category':<22} {'count':>6} {'%':>6}  "
        f"{'avg_volume':>14} {'avg_liquidity':>14}"
    )
    print("-" * 72)
    for cat in order:
        if cat not in buckets:
            continue
        b = buckets[cat]
        n = b["count"]
        pct = 100 * n / total
        avg_v = b["vol_sum"] / n if n else 0
        avg_l = b["liq_sum"] / n if n else 0
        print(
            f"{cat:<22} {n:>6} {pct:>5.1f}%  "
            f"{avg_v:>14,.0f} {avg_l:>14,.0f}"
        )

    print()
    print("Sample questions per category (first 3):")
    for cat in order:
        if cat not in buckets:
            continue
        print(f"  [{cat}]")
        for q in buckets[cat]["examples"]:
            s = q[:80] + "..." if len(q) > 80 else q
            print(f"    - {s}")


if __name__ == "__main__":
    main()
