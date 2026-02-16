from cambot.config import DATA_DIR

MEMORY_PATH = DATA_DIR / "memory.md"


class MemoryStore:
    """Persistent memory as a plain markdown file."""

    def __init__(self):
        self._path = MEMORY_PATH

    def read(self) -> str:
        if not self._path.exists():
            return ""
        return self._path.read_text().strip()

    def append(self, content: str) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a") as f:
            f.write(content + "\n")

    def rewrite(self, content: str) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._path.write_text(content + "\n")

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()
