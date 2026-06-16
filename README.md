# CFHB Tournament Results Bot

Automated bot that tracks CFHB players' tournament results from start.gg and posts them to Discord.

## Features

- 🔄 Automatically checks recent tournaments from configured players
- 📊 Fetches tournament results and player standings
- 💬 Posts formatted results to Discord
- 🤖 Runs daily via GitHub Actions at 8:00 AM CST
- 📝 Tracks processed tournaments to avoid duplicates
- 🔀 Automatically creates and merges PRs with updated results

## Setup

### 1. Configure Player IDs

Edit `player_ids.json` to include the start.gg user IDs of players to track:

```json
{
  "players": [
    {
      "id": 123456,
      "name": "PlayerName",
      "display_name": "Display Name"
    }
  ]
}
```

To find a player's ID:
1. Go to their start.gg profile
2. The ID is in the URL: `https://start.gg/user/USER_ID`

### 2. Configure Name Mappings

Edit `name_mapping.json` to map start.gg display names to Discord-friendly names:

```json
{
  "CFHB | PlayerTag": "FirstName",
  "PlayerTag": "FirstName"
}
```

### 3. Set Up GitHub Secrets

In your GitHub repository, go to Settings → Secrets and variables → Actions, and add:

- `STARTGG_API_TOKEN`: Your start.gg API token
  - Get one at: https://start.gg/admin/profile/developer
- `DISCORD_BOT_TOKEN`: Your Discord bot token
  - Create a bot at: https://discord.com/developers/applications
- `DISCORD_CHANNEL_ID`: The Discord channel ID where results should be posted
  - Enable Developer Mode in Discord, right-click the channel, and copy ID

### 4. Enable GitHub Actions

1. Go to your repository's Actions tab
2. Enable workflows if prompted
3. The workflow will run automatically every day at 8:00 AM CST
4. You can also trigger it manually from the Actions tab

### 5. Configure Branch Protection (Optional but Recommended)

To enable auto-merge:
1. Go to Settings → Branches
2. Add a branch protection rule for `main`
3. Enable "Require a pull request before merging"
4. Enable "Allow auto-merge"

## Manual Usage

You can run the script locally:

```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
export STARTGG_API_TOKEN="your_token"
export DISCORD_BOT_TOKEN="your_bot_token"
export DISCORD_CHANNEL_ID="your_channel_id"

# Run the script
python cfhb_results.py
```

## How It Works

1. **Tournament Discovery**: The script queries start.gg for recent tournaments (last 30 days) where configured players competed
2. **Result Processing**: For each new tournament, it fetches:
   - Event details and participant counts
   - Player standings and match records
   - Win/loss records for each player
3. **Discord Notification**: Formats and posts results to the configured Discord channel
4. **Tracking**: Saves processed tournament slugs to `posted_results.txt` to avoid duplicates
5. **Automation**: GitHub Actions runs daily and creates PRs with updated results

## File Structure

```
.
├── cfhb_results.py          # Main script
├── player_ids.json          # Player configuration
├── name_mapping.json        # Name mappings
├── posted_results.txt       # Processed tournaments log
├── requirements.txt         # Python dependencies
├── .github/
│   └── workflows/
│       └── cfhb_results.yml # GitHub Actions workflow
└── README.md               # This file
```

## Troubleshooting

### Script fails with "No tournament data found"
- Verify the player IDs in `player_ids.json` are correct
- Check that players have competed in tournaments in the last 30 days

### Discord messages not sending
- Verify `DISCORD_BOT_TOKEN` is correct
- Ensure the bot has permissions to send messages in the channel
- Check that `DISCORD_CHANNEL_ID` is correct

### GitHub Actions workflow fails
- Check that all secrets are set correctly
- Review the workflow logs in the Actions tab
- Ensure the repository has write permissions enabled for workflows

## License

MIT License - feel free to modify and use as needed.