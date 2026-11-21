"""Built-in list of tech companies with public job boards, searched by default.

Every slug was checked live on 2026-11-20. Add your own with `companies add <slug>`;
turn this list off with `set STARTER false`.
"""

STARTER: dict[str, tuple[str, ...]] = {
    "greenhouse": (
        "affirm", "airbnb", "airtable", "anthropic", "asana", "brex", "chime", "cloudflare", "coinbase",
        "databricks", "datadog", "discord", "dropbox", "duolingo", "elastic", "epicgames", "faire", "figma",
        "flexport", "gitlab", "gusto", "instacart", "janestreet", "lyft", "mongodb", "nextdoor", "okta",
        "pinterest", "reddit", "riotgames", "robinhood", "roblox", "samsara", "scaleai", "spacex",
        "squarespace", "stripe", "toast", "twilio", "twitch", "verkada", "waymo",
    ),
    "lever": ("outreach", "palantir", "ro", "spotify", "zoox"),
    "ashby": (
        "clickhouse", "cohere", "cursor", "elevenlabs", "harvey", "linear", "modal", "notion", "openai",
        "perplexity", "posthog", "ramp", "replit", "runway", "sierra", "supabase", "zapier",
    ),
}


def pairs() -> list[tuple[str, str]]:
    return [(ats, slug) for ats, slugs in STARTER.items() for slug in slugs]


# Proper display names where the board slug doesn't title-case nicely.
NAMES = {
    "openai": "OpenAI", "posthog": "PostHog", "elevenlabs": "ElevenLabs", "clickhouse": "ClickHouse",
    "ro": "Ro", "scaleai": "Scale AI", "janestreet": "Jane Street", "riotgames": "Riot Games",
    "epicgames": "Epic Games", "spacex": "SpaceX", "gitlab": "GitLab", "mongodb": "MongoDB",
}
