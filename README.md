# Limitless TCG Deck Scraper

A high-speed, cross-platform Python scraper that searches **Limitless TCG** tournament decklists from the past 4 weeks for a specific card name, then sorts and displays results by **archetype** and **win percentage**.

Supports **Windows, macOS, and Linux**.

## Features
- Searches all completed Standard-format online tournaments from the last 4 weeks.
- Matches card names **anywhere in the decklist** (Pokémon, Trainers, Energy).
- Groups results by **deck archetype**.
- Sorts within each archetype by:
  1. **Highest win rate** (ties excluded from percentage)
  2. **Total matches played** (more matches ranks higher if win rates are equal)
- Flags **dropped players** with `"Drop"` in results.
- Filters out players below **40% win rate**.
- Outputs results as a **neatly formatted HTML table** for easy viewing.

## Requirements
- **Python 3.8+** (check with `python --version`)
- Internet connection

Python dependencies:
- `aiohttp`
- `tqdm`
- `uvloop` *(optional, for macOS/Linux speed boost)*

## Installation

1. **Clone the repository**
```bash
git clone https://github.com/YOUR_USERNAME/limitless-deck-scraper.git
cd limitless-deck-scraper
```

2.	**Install dependencies**
```bash
pip install -r requirements.txt
```

## Usage

Run the script with the --card argument to search for a card name:
```bash
python scraper.py --card "charizard"
```
- Partial matches work — e.g. "charizard" will match Charizard ex.
- Output will be saved as output.html and opened in your default browser automatically.

## Notes
- The scraper obeys a fixed requests-per-second limit for speed and to avoid rate-limiting.
- Works on Windows, macOS, and Linux without modification.
- If a player drops from a tournament, their record will be shown like:

```bash
0-2-0 Drop
```
---

License

MIT License — you are free to use, modify, and distribute this software.
