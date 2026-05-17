from slowapi import Limiter
from slowapi.util import get_remote_address


# Shared limiter instance used by both main.py (app.state) and route modules.
# Keep it in a leaf module so routes and main can import without cycles.
limiter = Limiter(key_func=get_remote_address)
