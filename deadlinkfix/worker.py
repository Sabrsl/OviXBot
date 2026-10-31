"""Point d'entrée du worker : `python -m deadlinkfix.worker` (tâche continue Toolforge)."""
from .engine import run_worker

if __name__ == "__main__":
    run_worker()
