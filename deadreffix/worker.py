"""Point d'entrée du worker : `python -m deadreffix.worker` (tâche continue Toolforge)."""
from .engine import run_worker

if __name__ == "__main__":
    run_worker()
