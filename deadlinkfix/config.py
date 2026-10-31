"""Configuration par variables d'environnement (Toolforge : `toolforge envvars create`)."""
import os
from dotenv import load_dotenv
load_dotenv()

API = os.environ.get("DRF_API", "https://fr.wikipedia.org/w/api.php")
BOT_USER = os.environ.get("DRF_BOT_USER", "")            # ex. « Sabrsl@OviXBot » (mot de passe de robot)
BOT_PASSWORD = os.environ.get("DRF_BOT_PASSWORD", "")
OAUTH_TOKEN = os.environ.get("DRF_OAUTH_TOKEN", "")       # alternative : jeton OAuth 2 « owner-only »
ACCOUNT = os.environ.get("DRF_ACCOUNT") or (BOT_USER.split("@")[0].strip() or "OviXBot")  # nom du compte (page Utilisateur / discussion)
# Un vrai jeton OAuth 2 est un long JWT (3 segments, > 100 car.). Une chaîne courte de 32 caractères est un mot de passe de robot
# (Spécial:MotsDePasseDeRobot) : elle est alors utilisée avec action=login, jamais comme « Bearer » (cause des erreurs 401).
def _is_oauth2(t):
    return len(t) > 100 and t.count(".") == 2
if OAUTH_TOKEN and not _is_oauth2(OAUTH_TOKEN):
    if not BOT_PASSWORD:
        BOT_PASSWORD = OAUTH_TOKEN
    OAUTH_TOKEN = ""
HAS_CREDENTIALS = bool(OAUTH_TOKEN or (BOT_USER and BOT_PASSWORD))
ADMIN_TOKEN = os.environ.get("DRF_ADMIN_TOKEN", "")       # protège l'interface web
SECRET = os.environ.get("DRF_SECRET", ADMIN_TOKEN or "change-me")
DATA_DIR = os.environ.get("DRF_DATA_DIR") or os.path.join(os.environ.get("TOOL_DATA_DIR") or os.path.expanduser("~"), "ovixbot-data")
USER_AGENT = os.environ.get(
    "DRF_USER_AGENT",
    f"{ACCOUNT}-DeadLinkFix/1.0 (https://fr.wikipedia.org/wiki/Utilisateur:{ACCOUNT}; Toolforge tool ovixbot; Python requests)")
TALK_PAGE = f"Discussion utilisateur:{ACCOUNT}"
KILL_URL = ("https://fr.wikipedia.org/w/index.php?title=Discussion_utilisateur:" + ACCOUNT +
            "&action=edit&section=new&preloadtitle=Arr%C3%AAt%20d%27urgence&preload=Utilisateur:" + ACCOUNT +
            "/Kill-switch-preload&summary=Kill+switch+STOP")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULT_CONFIG = {
    "enabled": False,                 # le robot ne tourne que si activé explicitement
    "mode": "dry",                    # dry (simulation) | supervised (validation humaine) | auto (autonome, drapeau bot requis)
    "autonomy_confirmed": False,      # case à cocher explicite pour le mode auto
    "categories": [],                 # [{"name": "Catégorie:…", "depth": 1}]
    "titles": [],                     # articles ajoutés à la main
    "random_pages": False,            # compléter avec des articles au hasard
    "batch_size": 20,                 # articles tirés par cycle
    "recheck_days": 30,               # ne pas ré-analyser un article avant N jours
    "max_refs": 50,                   # liens distincts vérifiés par article (DeadLinkFix : 50 par défaut)
    "max_seconds": 240,
    "add_archive": True,              # ajouter archive-url / archive-date (capture validée) sur un lien confirmé mort
    "mark_broken": True,              # ajouter « brisé le » (Lien web uniquement) quand une archive est présente
    "min_edit_interval": 30,          # secondes entre deux modifications
    "max_edits_per_hour": 20,
    "max_edits_per_day": 150,
    "max_errors_before_pause": 5,
    "schedule": {"enabled": False, "days": [0, 1, 2, 3, 4, 5, 6], "start_hour": 0, "end_hour": 24, "cycle_pause": 60},
}
