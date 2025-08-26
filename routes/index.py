# routes/index.py
from fastapi import APIRouter

router = APIRouter()

@router.get("/")
def root():
    return {"message": "OK"}