# Wordsmith Hangman

A responsive Hangman game served by Python's standard-library HTTP server. The server imports its word source into SQLite on first launch and stores games and leaderboard scores in the same database. No third-party Python packages are required.

## Run locally

Install Python 3.10 or newer, then run:

```powershell
python server.py
```

Open `http://localhost:8000`. On first startup, the server builds `data/hangman.sqlite3` from `words_alpha.txt`. Keep the `data` directory to retain the dictionary and leaderboard between restarts.

To let another device on the same network open the running game, use the host computer's LAN address, for example `http://192.168.1.20:8000`, and allow inbound TCP port 8000 in its firewall. This is not a public internet URL; use a hosted deployment for an off-network demonstration.

## Deploy to Render

Push this directory to a GitHub repository and create a Render Blueprint from `render.yaml`. The included Dockerfile starts the application, and the persistent disk mounted at `/app/data` keeps the SQLite dictionary, games, and leaderboard between deploys. Render provides the public URL after deployment. Do not remove the persistent disk if existing scores must remain.

## Difficulty and scoring report

Difficulty is based on word length: Easy is 4–5 letters, Medium is 6–8, and Hard is 9–12. Medium and Hard words are chosen from the server-side English dictionary. Easy uses a separate set of short, curated words about Nature, Animals, Movies, F1, Football, Cricket, and Academic subjects. Choose a topic in the Easy word topic menu, or select Mix all topics to choose a topic at random and then a word from that topic. The game allows six incorrect guesses.

For a win, score = (10 × distinct correctly guessed letters + 10 × remaining attempts) × difficulty multiplier. Multipliers are Easy ×1, Medium ×1.5, and Hard ×2. A loss scores 0. The displayed in-game score is the current winning score if the game were completed at that point; it is finalized when the game ends. Repeated guesses are ignored and do not consume an attempt.

The game prompts for a name after either outcome and persists the completed score. The leaderboard returns the top 50 by score (ties are ordered by earliest saved time); the first three ranks receive distinct badges and larger names.

## Vocabulary source

`words_alpha.txt` is the lowercase alphabetic English word list from [dwyl/english-words](https://github.com/dwyl/english-words). At startup, the application imports all unique words containing 4–12 ASCII letters into the SQLite `words` table. The dictionary is not embedded in the web page.

The Easy theme pools are maintained in `topic_words.json` and imported into the SQLite `themed_words` table at startup. Each themed word must have 4–5 ASCII letters. Add words to a topic's array in this JSON file to expand its pool; the database import is repeatable and preserves existing scores.
