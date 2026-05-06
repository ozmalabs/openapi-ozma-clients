"""
Reference FastAPI app demonstrating x-gamma.

Item lifecycle:  draft → published → archived

Endpoints:
    POST   /items              createItem      (no state requirement)
    GET    /items/{id}         getItem
    POST   /items/{id}/publish publishItem     requires_state=draft
    POST   /items/{id}/archive archiveItem     requires_state=published
    DELETE /items/{id}         deleteItem      requires_state=draft|published
    POST   /checkout           checkout        requires_prior=addToCart
    POST   /cart               addToCart
"""
from __future__ import annotations

import sys
import os

# Use the ozmalabs fork if available, fall back to installed fastapi
sys.path.insert(0, os.path.expanduser("~/ozma/fastapi"))

from fastapi import FastAPI, HTTPException
from fastapi import gamma
from pydantic import BaseModel

app = FastAPI(title="Item Lifecycle Demo", version="1.0.0")

# ---------------------------------------------------------------------------
# In-memory store
# ---------------------------------------------------------------------------

_items: dict[int, dict] = {}
_next_id = 1
_cart: list[dict] = []


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ItemCreate(BaseModel):
    title: str
    body: str = ""


class ItemOut(BaseModel):
    id: int
    title: str
    body: str
    status: str


# ---------------------------------------------------------------------------
# State machine registration
# ---------------------------------------------------------------------------

gamma.state_machine(
    "ItemLifecycle",
    states=["draft", "published", "archived"],
    transitions=[
        ("draft", "published"),
        ("published", "archived"),
    ],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/items", response_model=ItemOut, operation_id="createItem")
@gamma.postcondition("Item is created in draft state", effect="resource_exists", produces_state="draft")
async def create_item(body: ItemCreate) -> ItemOut:
    global _next_id
    item = {"id": _next_id, "title": body.title, "body": body.body, "status": "draft"}
    _items[_next_id] = item
    _next_id += 1
    return ItemOut(**item)


@app.get("/items/{id}", response_model=ItemOut, operation_id="getItem")
async def get_item(id: int) -> ItemOut:
    if id not in _items:
        raise HTTPException(status_code=404)
    return ItemOut(**_items[id])


@app.post("/items/{id}/publish", response_model=ItemOut, operation_id="publishItem")
@gamma.requires_state("draft")
@gamma.produces_state("published")
@gamma.postcondition("Item is now publicly visible", effect="state_change")
async def publish_item(id: int) -> ItemOut:
    if id not in _items:
        raise HTTPException(status_code=404)
    if _items[id]["status"] != "draft":
        raise HTTPException(status_code=409, detail="Item is not in draft state")
    _items[id]["status"] = "published"
    return ItemOut(**_items[id])


@app.post("/items/{id}/archive", response_model=ItemOut, operation_id="archiveItem")
@gamma.requires_state("published")
@gamma.produces_state("archived")
@gamma.postcondition("Item is archived and no longer publicly visible", effect="state_change")
@gamma.forbidden_after("archiveItem")
async def archive_item(id: int) -> ItemOut:
    if id not in _items:
        raise HTTPException(status_code=404)
    if _items[id]["status"] != "published":
        raise HTTPException(status_code=409, detail="Item is not published")
    _items[id]["status"] = "archived"
    return ItemOut(**_items[id])


@app.delete("/items/{id}", operation_id="deleteItem")
@gamma.requires_state("draft", "published")
@gamma.postcondition("Item no longer exists", effect="resource_ceases")
@gamma.forbidden_after("deleteItem")
async def delete_item(id: int) -> dict:
    if id not in _items:
        raise HTTPException(status_code=404)
    del _items[id]
    return {"deleted": id}


# ---------------------------------------------------------------------------
# Ordering constraint example
# ---------------------------------------------------------------------------

@app.post("/cart", operation_id="addToCart")
@gamma.postcondition("Item added to cart")
async def add_to_cart(body: dict) -> dict:
    _cart.append(body)
    return {"cart_size": len(_cart)}


@app.post("/checkout", operation_id="checkout")
@gamma.requires_prior("addToCart")
@gamma.postcondition("Cart checked out", effect="resource_ceases")
async def checkout() -> dict:
    if not _cart:
        raise HTTPException(status_code=409, detail="Cart is empty")
    _cart.clear()
    return {"status": "checked_out"}
