

# ── Memory graph stubs ──────────────────────────────────────────────
memory_extra = APIRouter(tags=["memory"])

# Memory surfaces and layers for FE
SURFACES = ["chat", "quiz", "notebook", "co_writer", "research", "book", "partner"]
L3_SLOTS = ["profile", "summary", "overview"]


@memory_extra.get("/memory/snapshot/{surface}")
async def memory_snapshot_surface(
    surface: str,
    current_user: User = Depends(get_current_user),
):
    """FE expects: { entities: [...] }"""
    return {"entities": []}


@memory_extra.get("/memory/doc/{layer}/{surface}")
async def memory_doc(
    layer: str,
    surface: str,
    current_user: User = Depends(get_current_user),
):
    """FE expects: { content: string }"""
    return {"content": ""}
