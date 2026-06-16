import requests
import discord
import asyncio
import json
import os
import logging
import time
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Set, Optional

# =============================================================================
# LOGGING CONFIGURATION
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# =============================================================================
# RATE LIMITING CONFIGURATION
# =============================================================================
# Start.gg rate limits: 80 requests per 60 seconds, max 1000 objects per request
# We'll track requests to stay well under the limit
class RateLimiter:
    def __init__(self, max_requests=70, time_window=60):
        """
        Rate limiter to stay under start.gg's 80 requests/60s limit.
        Using 70 to have a safety margin.
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = []
    
    def wait_if_needed(self):
        """Wait if we're approaching the rate limit"""
        now = time.time()
        
        # Remove requests older than time_window
        self.requests = [req_time for req_time in self.requests if now - req_time < self.time_window]
        
        if len(self.requests) >= self.max_requests:
            # Calculate how long to wait
            oldest_request = min(self.requests)
            wait_time = self.time_window - (now - oldest_request) + 1
            logger.warning(f"⏸️  Rate limit approaching ({len(self.requests)}/{self.max_requests} requests). Waiting {wait_time:.1f}s...")
            time.sleep(wait_time)
            # Clear old requests after waiting
            now = time.time()
            self.requests = [req_time for req_time in self.requests if now - req_time < self.time_window]
        
        # Record this request
        self.requests.append(now)
        logger.debug(f"📊 Rate limit: {len(self.requests)}/{self.max_requests} requests in last {self.time_window}s")

rate_limiter = RateLimiter()

# =============================================================================
# CONFIGURATION
# =============================================================================
STARTGG_API_TOKEN = os.getenv("STARTGG_API_TOKEN", "")
API_URL = "https://api.start.gg/gql/alpha"

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0"))

# Load configuration from JSON files
def load_json_config(filename: str) -> dict:
    """Load JSON configuration file"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            logger.info(f"✅ Loaded config file: {filename}")
            return data
    except FileNotFoundError:
        logger.error(f"❌ Config file {filename} not found")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"❌ Invalid JSON in {filename}: {e}")
        return {}

logger.info("📂 Loading configuration files...")
NAME_DICT = load_json_config("./data/name_mapping.json")
PLAYER_CONFIG = load_json_config("./data/player_ids.json")
logger.info(f"📋 Loaded {len(NAME_DICT)} name mappings")
logger.info(f"👥 Loaded {len(PLAYER_CONFIG.get('players', []))} player configurations")

# Cache for slug-to-ID conversions to avoid repeated API calls
SLUG_TO_ID_CACHE = {}

# =============================================================================
# GRAPHQL QUERIES
# =============================================================================

PLAYER_RECENT_TOURNAMENTS_QUERY = """
query UserRecentTournaments($userId: ID!, $perPage: Int!) {
  user(id: $userId) {
    id
    player {
      recentStandings(limit: $perPage) {
        id
        placement
        entrant {
          id
          name
          event {
            id
            name
            tournament {
              id
              name
              slug
              startAt
            }
          }
        }
      }
    }
  }
}
"""

TOURNAMENT_QUERY = """
query TournamentQuery($slug: String) {
  tournament(slug: $slug) {
    name
    events {
      name
      id
      numEntrants
    }
  }
}
"""

EVENT_ENTRANTS_QUERY = """
query EventEntrants($eventId: ID!, $page: Int!, $perPage: Int!) {
  event(id: $eventId) {
    id
    name
    entrants(query: {
      page: $page
      perPage: $perPage
    }) {
      pageInfo {
        total
      }
      nodes {
        name
        id
        standing {
          placement
        }
      }
    }
  }
}
"""

# Separate query for fetching sets - only used for CFHB players
PLAYER_SETS_QUERY = """
query PlayerSets($entrantId: ID!) {
  entrant(id: $entrantId) {
    id
    paginatedSets(page: 1, perPage: 30) {
      nodes {
        displayScore
        winnerId
      }
    }
  }
}
"""

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def make_api_request_with_retry(session: requests.Session, query: str, variables: dict, max_retries: int = 3, base_delay: float = 2.0) -> dict:
    """
    Make an API request with exponential backoff retry logic.
    Handles rate limiting and temporary failures.
    """
    for attempt in range(max_retries):
        try:
            # Check rate limit before making request
            rate_limiter.wait_if_needed()
            
            response = session.post(API_URL, json={"query": query, "variables": variables})
            
            # Success
            if response.status_code == 200:
                return response.json()
            
            # Rate limited or server error - retry with backoff
            if response.status_code in [429, 502, 503, 504]:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)  # Exponential backoff
                    logger.warning(f"⚠️  HTTP {response.status_code} - Retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(delay)
                    continue
                else:
                    logger.error(f"❌ HTTP {response.status_code} - Max retries reached")
                    return {"errors": [{"message": f"HTTP {response.status_code} after {max_retries} retries"}]}
            
            # Other error - don't retry
            logger.error(f"❌ HTTP {response.status_code} - {response.text[:200]}")
            return {"errors": [{"message": f"HTTP {response.status_code}"}]}
            
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"⚠️  Network error: {e} - Retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
                continue
            else:
                logger.error(f"❌ Network error after {max_retries} retries: {e}")
                return {"errors": [{"message": str(e)}]}
    
    return {"errors": [{"message": "Max retries exceeded"}]}


def lookup_user_by_slug(session: requests.Session, slug: str) -> dict:
    """
    Look up a user by their slug/discriminator to get their numeric user ID.
    This is helpful for finding the correct user ID format.
    """
    query = """
    query UserBySlug($slug: String!) {
      user(slug: $slug) {
        id
        slug
        player {
          gamerTag
        }
      }
    }
    """
    
    variables = {"slug": slug}
    
    try:
        data = make_api_request_with_retry(session, query, variables)
        
        if "errors" not in data and data.get("data", {}).get("user"):
            user = data["data"]["user"]
            logger.info(f"✅ Found user by slug '{slug}':")
            logger.info(f"   User ID: {user.get('id')}")
            logger.info(f"   Slug: {user.get('slug')}")
            logger.info(f"   GamerTag: {user.get('player', {}).get('gamerTag')}")
            return user
    except Exception as e:
        logger.error(f"Error looking up user by slug: {e}")
    
    return {}


def get_ordinal(n: int) -> str:
    """Get ordinal suffix for a number (1st, 2nd, 3rd, etc.)"""
    if 11 <= (n % 100) <= 13:
        return "th"
    if n % 10 == 1:
        return "st"
    if n % 10 == 2:
        return "nd"
    if n % 10 == 3:
        return "rd"
    return "th"


def get_recent_tournament_slugs(session: requests.Session, days_back: int = 30) -> Set[str]:
    """
    Fetch recent tournaments from all configured players.
    Returns a set of unique tournament slugs from the last 'days_back' days.
    """
    tournament_slugs = set()
    cutoff_date = datetime.now() - timedelta(days=days_back)
    logger.info(f"🔍 Searching for tournaments since {cutoff_date.strftime('%Y-%m-%d')}")
    
    players = PLAYER_CONFIG.get("players", [])
    logger.info(f"👥 Querying {len(players)} players for recent tournaments")
    
    for idx, player in enumerate(players, 1):
        player_id = player.get("id")
        player_name = player.get("name", "Unknown")
        
        if not player_id:
            logger.warning(f"⚠️  Player {player_name} has no ID, skipping")
            continue
        
        # Convert slug to numeric ID if needed
        original_id = player_id
        if not str(player_id).isdigit():
            logger.info(f"🔄 Converting slug '{player_id}' to numeric ID...")
            user_info = lookup_user_by_slug(session, player_id)
            if user_info and user_info.get("id"):
                player_id = user_info["id"]
                logger.info(f"✅ Converted to numeric ID: {player_id}")
            else:
                logger.error(f"❌ Could not resolve slug '{player_id}' to numeric ID, skipping player")
                continue
            
        logger.info(f"🔎 [{idx}/{len(players)}] Fetching tournaments for {player_name} (ID: {player_id})")
        variables = {"userId": player_id, "perPage": 20}
        
        try:
            data = make_api_request_with_retry(session, PLAYER_RECENT_TOURNAMENTS_QUERY, variables)
            
            if "errors" in data:
                logger.error(f"❌ GraphQL errors for {player_name}: {data['errors']}")
                continue
            
            # Log the raw response structure for debugging
            logger.debug(f"API Response structure: {json.dumps(data, indent=2)[:500]}")
            
            user_data = data.get("data", {}).get("user")
            if not user_data:
                logger.warning(f"⚠️  No user data found for {player_name} (ID: {player_id})")
                logger.debug(f"Response data: {data}")
                continue
            
            if not user_data.get("player"):
                logger.warning(f"⚠️  User found but no player profile for {player_name} (ID: {player_id})")
                logger.debug(f"User data: {user_data}")
                continue
                
            standings = user_data["player"].get("recentStandings", [])
            logger.info(f"📊 Found {len(standings)} recent standings for {player_name}")
            
            tournaments_found_for_player = 0
            for standing in standings:
                entrant = standing.get("entrant", {})
                event = entrant.get("event", {})
                tournament = event.get("tournament", {})
                
                slug = tournament.get("slug")
                start_at = tournament.get("startAt")
                tournament_name = tournament.get("name", "Unknown")
                
                if slug and start_at:
                    # Normalize slug - remove "tournament/" prefix if present
                    normalized_slug = slug.replace("tournament/", "") if slug.startswith("tournament/") else slug
                    
                    # Convert Unix timestamp to datetime
                    tournament_date = datetime.fromtimestamp(start_at)
                    
                    # Only include tournaments within the date range
                    if tournament_date >= cutoff_date:
                        if normalized_slug not in tournament_slugs:
                            tournament_slugs.add(normalized_slug)
                            tournaments_found_for_player += 1
                            logger.info(f"  ✓ New tournament: {tournament_name} ({normalized_slug}) on {tournament_date.strftime('%Y-%m-%d')}")
                        else:
                            logger.debug(f"  ↻ Already found: {tournament_name} ({normalized_slug})")
                    else:
                        logger.debug(f"  ⏭️  Skipping old tournament: {tournament_name} ({tournament_date.strftime('%Y-%m-%d')})")
            
            if tournaments_found_for_player > 0:
                logger.info(f"✅ Added {tournaments_found_for_player} new tournament(s) from {player_name}")
            else:
                logger.info(f"ℹ️  No new tournaments from {player_name}")
        
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Network error fetching tournaments for {player_name}: {e}")
            continue
        except Exception as e:
            logger.error(f"❌ Unexpected error for {player_name}: {e}", exc_info=True)
            continue
    
    logger.info(f"🎯 Total unique tournaments found: {len(tournament_slugs)}")
    if tournament_slugs:
        logger.debug(f"Tournament slugs: {sorted(tournament_slugs)}")
    
    return tournament_slugs


def fetch_player_sets(session: requests.Session, entrant_id: str) -> list:
    """
    Fetch match sets for a specific entrant.
    Separate query to reduce complexity of main entrants query.
    """
    variables = {"entrantId": entrant_id}
    data = make_api_request_with_retry(session, PLAYER_SETS_QUERY, variables)
    
    if "errors" in data:
        logger.warning(f"⚠️  Failed to fetch sets for entrant {entrant_id}: {data['errors']}")
        return []
    
    entrant_data = data.get("data", {}).get("entrant")
    if not entrant_data:
        return []
    
    sets = entrant_data.get("paginatedSets", {}).get("nodes", [])
    return sets


def calculate_player_scores(player_nodes, session: requests.Session):
    """
    Calculate scores and records for players.
    Fetches match sets separately for each CFHB player.
    """
    results = []
    logger.debug(f"📊 Calculating scores for {len(player_nodes)} players")

    for p in player_nodes:
        if not p.get("standing"):
            logger.debug(f"⚠️  Player {p.get('name')} has no standing data, skipping")
            continue

        player_id = p["id"]
        player_name = p["name"]
        
        # Fetch sets separately for this player
        logger.debug(f"  Fetching match history for {player_name}")
        sets = fetch_player_sets(session, player_id)

        total_matches = len(sets)
        total_wins = sum(1 for s in sets if s.get("winnerId") == player_id)

        placement = p["standing"]["placement"]
        ending = get_ordinal(placement)
        
        display_name = NAME_DICT.get(player_name, player_name)
        record = f"{total_wins}-{total_matches - total_wins}"

        logger.debug(f"  Player: {display_name} | Placement: {placement}{ending} | Record: {record}")

        results.append({
            "name": display_name,
            "standing": placement,
            "ending": ending,
            "record": record
        })

    results.sort(key=lambda x: x["standing"])
    logger.info(f"✅ Calculated scores for {len(results)} players")
    return results


def build_discord_message(event_slug: str, session: requests.Session) -> tuple[str, bool]:
    """
    Build Discord message for a specific tournament.
    Returns: (message, success) where success indicates if data was fetched successfully
    """
    logger.info(f"🏗️  Building message for tournament: {event_slug}")
    
    # Ensure slug has "tournament/" prefix for API call
    full_slug = f"tournament/{event_slug}" if not event_slug.startswith("tournament/") else event_slug
    
    variables = {"slug": full_slug}
    data = make_api_request_with_retry(session, TOURNAMENT_QUERY, variables)
    
    if "errors" in data:
        logger.error(f"❌ GraphQL errors: {data['errors']}")
        return "", False
    
    tournament_data = data.get("data", {}).get("tournament")
    if not tournament_data:
        logger.error(f"❌ No tournament data found for {event_slug}")
        return "", False

    tourny_name = tournament_data["name"]
    events = tournament_data["events"]
    logger.info(f"📋 Tournament: {tourny_name} with {len(events)} event(s)")

    message_lines = []
    # Use the normalized slug (without tournament/ prefix) for the URL
    url_slug = event_slug.replace("tournament/", "") if event_slug.startswith("tournament/") else event_slug
    message_lines.append(f"Here's how CFHB faired at {tourny_name}(https://www.start.gg/tournament/{url_slug}/)")
    message_lines.append(":saluting_face:")
    message_lines.append("```Results:")

    cfhb_players_found = 0
    api_errors_occurred = False
    
    for event_idx, event in enumerate(events, 1):
        event_name = event["name"]
        num_entrants = event.get("numEntrants", 0)
        
        logger.info(f"🎮 [{event_idx}/{len(events)}] Processing event: {event_name} ({num_entrants} entrants)")
        
        houma_boys = []
        page = 1
        per_page = 100
        entrants_total = 0
        
        # Add small delay between events to avoid rate limiting
        if event_idx > 1:
            time.sleep(0.75)  # Increased from 0.5s to 0.75s for extra safety

        while True:
            vars_page = {"eventId": event["id"], "page": page, "perPage": per_page}
            logger.debug(f"  Fetching entrants page {page} for event {event['id']}")

            json_data = make_api_request_with_retry(session, EVENT_ENTRANTS_QUERY, vars_page)

            if "errors" in json_data:
                logger.error(f"❌ Failed to fetch entrants: {json_data['errors']}")
                api_errors_occurred = True
                break

            event_data = json_data.get("data", {}).get("event")
            if not event_data:
                logger.error(f"❌ No event data returned")
                api_errors_occurred = True
                break
                
            entrants_page = event_data["entrants"]

            if page == 1:
                entrants_total = entrants_page["pageInfo"]["total"]
                event_name = event_data["name"]
                logger.info(f"  📊 Total entrants: {entrants_total}")

            cfhb_on_page = 0
            for node in entrants_page["nodes"]:
                if node["name"] in NAME_DICT:
                    houma_boys.append(node)
                    cfhb_on_page += 1
            
            if cfhb_on_page > 0:
                logger.debug(f"  ✓ Found {cfhb_on_page} CFHB player(s) on page {page}")

            if len(entrants_page["nodes"]) < per_page:
                logger.debug(f"  Reached last page ({page})")
                break

            page += 1

        if not houma_boys:
            logger.debug(f"  ℹ️  No CFHB players found in {event_name}")
            continue

        logger.info(f"  ✅ Found {len(houma_boys)} CFHB player(s) in {event_name}")
        cfhb_players_found += len(houma_boys)

        message_lines.append(f"{event_name} ({entrants_total} Participants):")

        results = calculate_player_scores(houma_boys, session)
        for r in results:
            message_lines.append(f'{r["name"]}: {r["record"]} - {r["standing"]}{r["ending"]} place')

        message_lines.append("")

    message_lines.append("```")

    final_message = "\n".join(message_lines)
    
    # Check if we had API errors or no CFHB players found
    if api_errors_occurred:
        logger.warning(f"⚠️  API errors occurred while fetching data - skipping tournament")
        return "", False
    
    if cfhb_players_found == 0:
        logger.info(f"ℹ️  No CFHB players found in tournament - skipping")
        return "", False
    
    logger.info(f"✅ Message built: {len(final_message)} characters, {cfhb_players_found} CFHB players total")
    return final_message, True


# =============================================================================
# DISCORD POSTING
# =============================================================================

async def send_to_discord(message: str):
    """Send message to Discord channel"""
    logger.info(f"📤 Sending message to Discord (Channel ID: {DISCORD_CHANNEL_ID})")
    logger.debug(f"Message length: {len(message)} characters")
    
    intents = discord.Intents.default()
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        logger.info(f"🤖 Discord bot connected as {client.user}")
        channel = client.get_channel(DISCORD_CHANNEL_ID)
        if channel is None:
            logger.error(f"❌ Could not find channel {DISCORD_CHANNEL_ID}. Check CHANNEL ID + bot permissions.")
        else:
            logger.info(f"📍 Found channel: #{channel.name}")
            try:
                await channel.send(message)
                logger.info("✅ Message sent successfully to Discord")
            except discord.errors.HTTPException as e:
                logger.error(f"❌ Failed to send message: {e}")
            except Exception as e:
                logger.error(f"❌ Unexpected error sending message: {e}", exc_info=True)

        await client.close()

    try:
        await client.start(DISCORD_BOT_TOKEN)
    except discord.errors.LoginFailure:
        logger.error("❌ Failed to login to Discord. Check DISCORD_BOT_TOKEN")
    except Exception as e:
        logger.error(f"❌ Discord client error: {e}", exc_info=True)


def load_posted_results() -> Set[str]:
    """Load previously posted tournament slugs"""
    filename = "./data/posted_results.txt"
    try:
        with open(filename, "r", encoding="utf-8") as f:
            results = set(line.strip() for line in f.readlines() if line.strip())
            logger.info(f"📋 Loaded {len(results)} previously posted results from {filename}")
            return results
    except FileNotFoundError:
        logger.info(f"ℹ️  No {filename} found, starting fresh")
        return set()


def save_posted_result(event_slug: str):
    """Save a tournament slug to the posted results file"""
    filename = "./data/posted_results.txt"
    try:
        with open(filename, "a", encoding="utf-8") as f:
            f.write(event_slug + "\n")
        logger.info(f"💾 Saved {event_slug} to {filename}")
    except Exception as e:
        logger.error(f"❌ Failed to save result to {filename}: {e}")


def main(tournament_slug: Optional[str] = None):
    """
    Main execution function
    
    Args:
        tournament_slug: Optional specific tournament slug to process (without "tournament/" prefix)
    """
    logger.info("=" * 80)
    logger.info("🚀 Starting CFHB Results Bot")
    logger.info("=" * 80)
    
    # Validate configuration
    logger.info("🔐 Validating configuration...")
    if not STARTGG_API_TOKEN:
        logger.error("❌ STARTGG_API_TOKEN not set")
        return
    else:
        logger.info(f"✅ STARTGG_API_TOKEN configured (length: {len(STARTGG_API_TOKEN)})")
    
    if not DISCORD_BOT_TOKEN:
        logger.error("❌ DISCORD_BOT_TOKEN not set")
        return
    else:
        logger.info(f"✅ DISCORD_BOT_TOKEN configured (length: {len(DISCORD_BOT_TOKEN)})")
    
    if DISCORD_CHANNEL_ID == 0:
        logger.error("❌ DISCORD_CHANNEL_ID not set")
        return
    else:
        logger.info(f"✅ DISCORD_CHANNEL_ID configured: {DISCORD_CHANNEL_ID}")
    
    # Initialize session
    logger.info("🔧 Initializing API session...")
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {STARTGG_API_TOKEN}"})
    logger.info(f"✅ Session initialized with API URL: {API_URL}")
    
    # Load already posted results
    posted_results = load_posted_results()
    
    # Determine which tournaments to process
    if tournament_slug:
        # Process specific tournament
        logger.info("\n" + "=" * 80)
        logger.info("🎯 PROCESSING SPECIFIC TOURNAMENT")
        logger.info("=" * 80)
        logger.info(f"Tournament slug: {tournament_slug}")
        
        # Normalize slug (remove "tournament/" prefix if present)
        normalized_slug = tournament_slug.replace("tournament/", "") if tournament_slug.startswith("tournament/") else tournament_slug
        
        # Check if already posted
        if normalized_slug in posted_results:
            logger.warning(f"⚠️  Tournament {normalized_slug} has already been posted")
            logger.info(f"ℹ️  Processing anyway since it was explicitly requested")
        
        new_tournaments = {normalized_slug}
    else:
        # Get recent tournaments from all players
        logger.info("\n" + "=" * 80)
        logger.info("🔍 FETCHING RECENT TOURNAMENTS")
        logger.info("=" * 80)
        recent_tournaments = get_recent_tournament_slugs(session, days_back=30)
        
        # Process new tournaments
        new_tournaments = recent_tournaments - posted_results
        
        logger.info("\n" + "=" * 80)
        logger.info("📊 TOURNAMENT ANALYSIS")
        logger.info("=" * 80)
        logger.info(f"Total tournaments found: {len(recent_tournaments)}")
        logger.info(f"Previously posted: {len(posted_results)}")
        logger.info(f"New tournaments to process: {len(new_tournaments)}")
        
        if not new_tournaments:
            logger.info("✅ No new tournaments to process - all up to date!")
            return
        
        logger.info(f"\n🆕 New tournaments: {sorted(new_tournaments)}")
    
    logger.info("\n" + "=" * 80)
    logger.info("🎮 PROCESSING TOURNAMENTS")
    logger.info("=" * 80)
    
    success_count = 0
    error_count = 0
    
    for idx, event_slug in enumerate(sorted(new_tournaments), 1):
        logger.info(f"\n{'─' * 80}")
        logger.info(f"📝 [{idx}/{len(new_tournaments)}] Processing: {event_slug}")
        logger.info(f"{'─' * 80}")
        
        try:
            message, success = build_discord_message(event_slug, session)
            
            if not success or not message:
                logger.warning(f"⚠️  Skipping {event_slug} - failed to fetch complete data or no CFHB players found")
                if not tournament_slug:
                    logger.info(f"ℹ️  Tournament will NOT be marked as posted and will be retried next run")
                error_count += 1
                continue
            
            # Discord has a 2000 character limit per message
            original_length = len(message)
            if original_length > 2000:
                logger.warning(f"⚠️  Message too long ({original_length} chars), truncating to 2000")
                message = message[:1990] + "\n```(truncated)```"
            
            # Send to Discord
            logger.info("📤 Sending to Discord...")
            asyncio.run(send_to_discord(message))
            
            # Mark as posted ONLY after successful Discord send
            # Skip marking if it was already posted (manual re-run case)
            if event_slug not in posted_results:
                save_posted_result(event_slug)
            logger.info(f"✅ Successfully processed {event_slug}")
            success_count += 1
            
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Network error processing {event_slug}: {e}")
            error_count += 1
            continue
        except Exception as e:
            logger.error(f"❌ Unexpected error processing {event_slug}: {e}", exc_info=True)
            error_count += 1
            continue
    
    logger.info("\n" + "=" * 80)
    logger.info("🎉 PROCESSING COMPLETE")
    logger.info("=" * 80)
    logger.info(f"✅ Successfully processed: {success_count}")
    logger.info(f"❌ Errors: {error_count}")
    logger.info(f"📊 Total: {len(new_tournaments)}")
    logger.info("=" * 80)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CFHB Tournament Results Bot")
    parser.add_argument(
        "--tournament-slug",
        type=str,
        help='Optional: Specific tournament slug to process (without "tournament/" prefix, e.g., "example-2024")',
        default=None
    )
    
    args = parser.parse_args()
    main(tournament_slug=args.tournament_slug)

# Made with Bob
