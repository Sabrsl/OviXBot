"""Point d'entrée WSGI Toolforge (`webservice python3.11`) : expose `app`."""
from deadlinkfix.app import app  # noqa: F401

if __name__ == "__main__":
    app.run(port=8000)
