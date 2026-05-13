from typing import TypeVar, Optional
from fastapi import HTTPException

T = TypeVar("T")

def or_404(entry: Optional[T], name: str) -> T:
    """Raise 404 if entry is None, else return entry."""
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{name} not found")
    return entry
