import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HANGMAN_DATA_DIR", ROOT / "data"))
DATABASE = DATA_DIR / "hangman.sqlite3"
WORD_SOURCE = ROOT / "words_alpha.txt"
TOPIC_SOURCE = ROOT / "topic_words.json"
PORT = int(os.environ.get("PORT", "8000"))
MAX_ATTEMPTS = 6
TOPIC_LABELS = {
    "nature": "Nature",
    "animals": "Animals",
    "movies": "Movies",
    "f1": "F1",
    "football": "Football",
    "cricket": "Cricket",
    "academic": "Academic",
}
DIFFICULTIES = {
    "easy": (4, 5, 1),
    "medium": (6, 8, 1.5),
    "hard": (9, 12, 2),
}


@contextmanager
def connect():
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS words (
                word TEXT PRIMARY KEY,
                difficulty TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard'))
            );
            CREATE INDEX IF NOT EXISTS words_by_difficulty ON words(difficulty);
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,
                word TEXT NOT NULL,
                difficulty TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT 'any',
                guesses TEXT NOT NULL DEFAULT '[]',
                wrong_guesses INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                final_score INTEGER,
                score_saved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT NOT NULL UNIQUE REFERENCES games(id),
                player_name TEXT NOT NULL,
                score INTEGER NOT NULL,
                difficulty TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS scores_ranking ON scores(score DESC, created_at ASC);
            CREATE TABLE IF NOT EXISTS themed_words (
                word TEXT NOT NULL REFERENCES words(word),
                topic TEXT NOT NULL,
                PRIMARY KEY (word, topic)
            );
            CREATE INDEX IF NOT EXISTS themed_words_by_topic ON themed_words(topic);
            """
        )
        game_columns = {row["name"] for row in connection.execute("PRAGMA table_info(games)")}
        if "topic" not in game_columns:
            connection.execute("ALTER TABLE games ADD COLUMN topic TEXT NOT NULL DEFAULT 'any'")
        word_count = connection.execute("SELECT COUNT(*) FROM words").fetchone()[0]
        if not word_count:
            if not WORD_SOURCE.is_file():
                raise RuntimeError(f"Vocabulary source is missing: {WORD_SOURCE}")
            batch = []
            with WORD_SOURCE.open("r", encoding="utf-8") as source:
                for raw_word in source:
                    word = raw_word.strip().lower()
                    if not word.isascii() or not word.isalpha():
                        continue
                    length = len(word)
                    if length < 4 or length > 12:
                        continue
                    if length <= 5:
                        difficulty = "easy"
                    elif length <= 8:
                        difficulty = "medium"
                    else:
                        difficulty = "hard"
                    batch.append((word, difficulty))
                    if len(batch) == 5000:
                        connection.executemany(
                            "INSERT OR IGNORE INTO words(word, difficulty) VALUES (?, ?)", batch
                        )
                        batch.clear()
            if batch:
                connection.executemany(
                    "INSERT OR IGNORE INTO words(word, difficulty) VALUES (?, ?)", batch
                )

        if not TOPIC_SOURCE.is_file():
            raise RuntimeError(f"Themed word source is missing: {TOPIC_SOURCE}")
        with TOPIC_SOURCE.open("r", encoding="utf-8") as source:
            themed_words = json.load(source)
        if not isinstance(themed_words, dict) or set(themed_words) != set(TOPIC_LABELS):
            raise RuntimeError("Themed word source must define all seven supported topics.")
        for topic, topic_words in themed_words.items():
            if not isinstance(topic_words, list):
                raise RuntimeError(f"The {topic} word list must be an array.")
            entries = []
            for word in topic_words:
                if not isinstance(word, str) or not word.isascii() or not word.isalpha():
                    raise RuntimeError(f"Invalid word in the {topic} word list: {word!r}")
                if not 4 <= len(word) <= 5:
                    raise RuntimeError(f"Easy themed words must contain 4–5 letters: {word!r}")
                entries.append((word, "easy"))
            connection.executemany(
                "INSERT OR IGNORE INTO words(word, difficulty) VALUES (?, ?)", entries
            )
            connection.executemany(
                "INSERT OR IGNORE INTO themed_words(word, topic) VALUES (?, ?)",
                [(word, topic) for word, _ in entries],
            )
        counts = dict(connection.execute("SELECT difficulty, COUNT(*) FROM words GROUP BY difficulty"))
        if sum(counts.values()) < 5000 or any(counts.get(level, 0) < 100 for level in DIFFICULTIES):
            raise RuntimeError(f"Vocabulary import did not provide enough words by level: {counts}")
        topic_counts = dict(
            connection.execute("SELECT topic, COUNT(*) FROM themed_words GROUP BY topic")
        )
        if any(topic_counts.get(topic, 0) < 10 for topic in TOPIC_LABELS):
            raise RuntimeError(f"Themed word lists need at least 10 words per topic: {topic_counts}")


def calculate_score(difficulty, correct_count, remaining_attempts, status):
    if status != "won":
        return 0
    multiplier = DIFFICULTIES[difficulty][2]
    return int((10 * correct_count + 10 * remaining_attempts) * multiplier)


def game_payload(row):
    guesses = json.loads(row["guesses"])
    word = row["word"]
    correct_guesses = sorted(set(guesses).intersection(word))
    remaining = MAX_ATTEMPTS - row["wrong_guesses"]
    active_score = calculate_score(row["difficulty"], len(correct_guesses), remaining, "won")
    finished = row["status"] != "active"
    return {
        "id": row["id"],
        "difficulty": row["difficulty"],
        "topic": row["topic"],
        "topic_label": TOPIC_LABELS.get(row["topic"], "Any topic"),
        "word_length": len(word),
        "masked_word": "".join(character if character in guesses or finished else "_" for character in word),
        "guesses": guesses,
        "correct_guesses": correct_guesses,
        "wrong_guesses": row["wrong_guesses"],
        "remaining_attempts": remaining,
        "status": row["status"],
        "current_score": row["final_score"] if finished else active_score,
        "final_score": row["final_score"] if finished else None,
        "word": word if finished else None,
        "score_saved": bool(row["score_saved"]),
    }


class HangmanHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, format_string, *args):
        print(f"{self.address_string()} - {format_string % args}")

    def send_json(self, status, payload):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def read_json(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request body.") from exc
        if size > 4096:
            raise ValueError("Request body is too large.")
        try:
            return json.loads(self.rfile.read(size) or b"{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            with connect() as connection:
                counts = dict(connection.execute("SELECT difficulty, COUNT(*) FROM words GROUP BY difficulty"))
            self.send_json(200, {"ok": True, "vocabulary_count": sum(counts.values()), "difficulty_counts": counts})
            return
        if path == "/api/leaderboard":
            with connect() as connection:
                scores = connection.execute(
                    "SELECT player_name, score, difficulty FROM scores "
                    "ORDER BY score DESC, created_at ASC, id ASC LIMIT 50"
                ).fetchall()
                word_count = connection.execute("SELECT COUNT(*) FROM words").fetchone()[0]
            self.send_json(
                200,
                {
                    "vocabulary_count": word_count,
                    "scores": [dict(row) for row in scores],
                },
            )
            return
        if path.startswith("/api/"):
            self.send_json(404, {"error": "API endpoint not found."})
            return
        if path in ("/", "/hangman.html"):
            page = (ROOT / "hangman.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        self.send_json(404, {"error": "Page not found."})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        try:
            body = self.read_json()
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object.")
            if path == "/api/games":
                self.create_game(body)
            elif match := re.fullmatch(r"/api/games/([a-f0-9-]+)/guess", path):
                self.submit_guess(match.group(1), body)
            elif match := re.fullmatch(r"/api/games/([a-f0-9-]+)/score", path):
                self.save_score(match.group(1), body)
            else:
                self.send_json(404, {"error": "API endpoint not found."})
        except ValueError as error:
            self.send_json(400, {"error": str(error)})
        except sqlite3.Error as error:
            print(f"Database error: {error}")
            self.send_json(500, {"error": "A storage error occurred. Please try again."})

    def create_game(self, body):
        difficulty = body.get("difficulty")
        if difficulty not in DIFFICULTIES:
            raise ValueError("Choose Easy, Medium, or Hard.")
        topic = body.get("topic", "any")
        if difficulty == "easy":
            if topic != "any" and topic not in TOPIC_LABELS:
                raise ValueError("Choose one of the available Easy word topics.")
        elif topic != "any":
            raise ValueError("Word topics are available only in Easy mode.")
        low, high, _ = DIFFICULTIES[difficulty]
        game_id = str(uuid.uuid4())
        with connect() as connection:
            if difficulty == "easy":
                if topic == "any":
                    topic = connection.execute(
                        "SELECT topic FROM themed_words GROUP BY topic ORDER BY RANDOM() LIMIT 1"
                    ).fetchone()["topic"]
                word = connection.execute(
                    "SELECT word FROM themed_words WHERE topic = ? ORDER BY RANDOM() LIMIT 1",
                    (topic,),
                ).fetchone()
                if word is None:
                    raise RuntimeError(f"No words are available for {TOPIC_LABELS[topic]}.")
                word = word["word"]
            else:
                word_count = connection.execute(
                    "SELECT COUNT(*) FROM words WHERE difficulty = ?", (difficulty,)
                ).fetchone()[0]
                if not word_count:
                    raise RuntimeError(f"No words are available for {difficulty}.")
                offset = int.from_bytes(os.urandom(8), "big") % word_count
                word = connection.execute(
                    "SELECT word FROM words WHERE difficulty = ? LIMIT 1 OFFSET ?",
                    (difficulty, offset),
                ).fetchone()["word"]
            if not low <= len(word) <= high:
                raise RuntimeError("The dictionary contains a word assigned to the wrong difficulty.")
            connection.execute(
                "INSERT INTO games(id, word, difficulty, topic) VALUES (?, ?, ?, ?)",
                (game_id, word, difficulty, topic),
            )
            row = connection.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        self.send_json(201, game_payload(row))

    def submit_guess(self, game_id, body):
        letter = body.get("letter")
        if not isinstance(letter, str) or not re.fullmatch(r"[a-zA-Z]", letter):
            raise ValueError("Enter a single letter from A to Z.")
        letter = letter.lower()
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "Game not found. Start a new game."})
                return
            guesses = json.loads(row["guesses"])
            if row["status"] == "active" and letter not in guesses:
                guesses.append(letter)
                wrong_guesses = row["wrong_guesses"] + (letter not in row["word"])
                if all(character in guesses for character in row["word"]):
                    status = "won"
                elif wrong_guesses >= MAX_ATTEMPTS:
                    status = "lost"
                else:
                    status = "active"
                correct_count = len(set(guesses).intersection(row["word"]))
                remaining = MAX_ATTEMPTS - wrong_guesses
                final_score = (
                    calculate_score(row["difficulty"], correct_count, remaining, status)
                    if status != "active"
                    else None
                )
                connection.execute(
                    "UPDATE games SET guesses = ?, wrong_guesses = ?, status = ?, final_score = ? WHERE id = ?",
                    (json.dumps(guesses), wrong_guesses, status, final_score, game_id),
                )
            updated = connection.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
        self.send_json(200, game_payload(updated))

    def save_score(self, game_id, body):
        player_name = body.get("player_name")
        if not isinstance(player_name, str):
            raise ValueError("Enter a player name.")
        player_name = " ".join(player_name.split())
        if not player_name or len(player_name) > 40 or any(ord(character) < 32 for character in player_name):
            raise ValueError("Name must be between 1 and 40 characters.")
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
            if row is None:
                self.send_json(404, {"error": "Game not found."})
                return
            if row["status"] == "active":
                raise ValueError("Finish the game before saving a score.")
            if row["score_saved"]:
                raise ValueError("This game's score has already been saved.")
            connection.execute(
                "INSERT INTO scores(game_id, player_name, score, difficulty) VALUES (?, ?, ?, ?)",
                (game_id, player_name, row["final_score"], row["difficulty"]),
            )
            connection.execute("UPDATE games SET score_saved = 1 WHERE id = ?", (game_id,))
        self.send_json(201, {"saved": True})


if __name__ == "__main__":
    initialize_database()
    with connect() as database:
        counts = dict(database.execute("SELECT difficulty, COUNT(*) FROM words GROUP BY difficulty"))
    print(f"Vocabulary ready: {sum(counts.values()):,} words ({counts})")
    print(f"Hangman is listening on http://0.0.0.0:{PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), HangmanHandler).serve_forever()
