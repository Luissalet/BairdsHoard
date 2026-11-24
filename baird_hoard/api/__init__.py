from .agent import router as agent_router
from .copilot import router as copilot_router
from .ui import router as ui_router

ROUTERS = [agent_router, ui_router, copilot_router]
