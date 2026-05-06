"""
Reference FastAPI app demonstrating x-gamma and GammaError.

Item lifecycle:  draft → published → archived

No HTTPException is raised anywhere. Inadmissible operations return
GammaError, which explains WHY they were inadmissible — the graph state
at the point of failure, not just a status code.
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.expanduser("~/ozma/fastapi"))

from fastapi import FastAPI
from fastapi import gamma
from fastapi.gamma import GammaError
from pydantic import BaseModel

app = FastAPI(title="Item Lifecycle Demo", version="1.0.0")

_items: dict[int, dict] = {}
_next_id = 1
_cart: list[dict] = []


class ItemCreate(BaseModel):
    title: str
    body: str = ""


class ItemOut(BaseModel):
    id: int
    title: str
    body: str
    status: str


gamma.state_machine(
    "ItemLifecycle",
    states=["draft", "published", "archived"],
    transitions=[
        ("draft", "published"),
        ("published", "archived"),
    ],
)


@app.post("/items", response_model=ItemOut, operation_id="createItem")
@gamma.postcondition("Item is created in draft state", effect="resource_exists", produces_state="draft")
async def create_item(body: ItemCreate) -> ItemOut:
    global _next_id
    item = {"id": _next_id, "title": body.title, "body": body.body, "status": "draft"}
    _items[_next_id] = item
    _next_id += 1
    return ItemOut(**item)


@app.get("/items/{id}", response_model=ItemOut, operation_id="getItem")
async def get_item(id: int) -> ItemOut | GammaError:
    if id not in _items:
        return GammaError.not_found("item", id)
    return ItemOut(**_items[id])


@app.post("/items/{id}/publish", response_model=ItemOut, operation_id="publishItem")
@gamma.requires_state("draft")
@gamma.produces_state("published")
@gamma.postcondition("Item is now publicly visible", effect="state_change")
async def publish_item(id: int) -> ItemOut | GammaError:
    if id not in _items:
        return GammaError.not_found("item", id)
    item = _items[id]
    if item["status"] != "draft":
        return GammaError.wrong_state(
            operation="publishItem",
            resource="item",
            current=item["status"],
            required=["draft"],
        )
    _items[id]["status"] = "published"
    return ItemOut(**_items[id])


@app.post("/items/{id}/archive", response_model=ItemOut, operation_id="archiveItem")
@gamma.requires_state("published")
@gamma.produces_state("archived")
@gamma.postcondition("Item is archived and no longer publicly visible", effect="state_change")
@gamma.forbidden_after("archiveItem")
async def archive_item(id: int) -> ItemOut | GammaError:
    if id not in _items:
        return GammaError.not_found("item", id)
    item = _items[id]
    if item["status"] != "published":
        return GammaError.wrong_state(
            operation="archiveItem",
            resource="item",
            current=item["status"],
            required=["published"],
        )
    _items[id]["status"] = "archived"
    return ItemOut(**_items[id])


@app.delete("/items/{id}", operation_id="deleteItem")
@gamma.requires_state("draft", "published")
@gamma.postcondition("Item no longer exists", effect="resource_ceases")
@gamma.forbidden_after("deleteItem")
async def delete_item(id: int) -> dict | GammaError:
    if id not in _items:
        return GammaError.not_found("item", id)
    item = _items[id]
    if item["status"] not in ("draft", "published"):
        return GammaError.wrong_state(
            operation="deleteItem",
            resource="item",
            current=item["status"],
            required=["draft", "published"],
        )
    del _items[id]
    return {"deleted": id}


@app.post("/cart", operation_id="addToCart")
@gamma.postcondition("Item added to cart")
async def add_to_cart(body: dict) -> dict:
    _cart.append(body)
    return {"cart_size": len(_cart)}


@app.post("/checkout", operation_id="checkout")
@gamma.requires_prior("addToCart")
@gamma.postcondition("Cart checked out", effect="resource_ceases")
async def checkout() -> dict | GammaError:
    if not _cart:
        return GammaError.requires_prior(operation="checkout", missing=["addToCart"])
    _cart.clear()
    return {"status": "checked_out"}
