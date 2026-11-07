import sqlite3
from pathlib import Path


class Storage:
    """Keeps what must survive a restart. Queries are tiny, so they don't block the bot noticeably."""

    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(path, autocommit=True)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                value INTEGER NOT NULL,
                PRIMARY KEY (user_id, name)
            );
            CREATE TABLE IF NOT EXISTS sent_files (
                video_key TEXT NOT NULL,
                variant TEXT NOT NULL,
                file_id TEXT NOT NULL,
                PRIMARY KEY (video_key, variant)
            );
            """
        )

    def close(self) -> None:
        self._db.close()

    def load_settings(self, user_id: int) -> dict[str, bool]:
        rows = self._db.execute("SELECT name, value FROM settings WHERE user_id = ?", (user_id,))
        return {name: bool(value) for name, value in rows}

    def save_setting(self, user_id: int, name: str, value: bool) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO settings (user_id, name, value) VALUES (?, ?, ?)",
            (user_id, name, value),
        )

    def get_file_id(self, video_key: str, variant: str) -> str | None:
        row = self._db.execute(
            "SELECT file_id FROM sent_files WHERE video_key = ? AND variant = ?",
            (video_key, variant),
        ).fetchone()
        return row[0] if row else None

    def save_file_id(self, video_key: str, variant: str, file_id: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO sent_files (video_key, variant, file_id) VALUES (?, ?, ?)",
            (video_key, variant, file_id),
        )

    def forget_file_id(self, video_key: str, variant: str) -> None:
        self._db.execute(
            "DELETE FROM sent_files WHERE video_key = ? AND variant = ?", (video_key, variant)
        )
