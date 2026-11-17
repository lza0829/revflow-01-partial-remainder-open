from fastapi import APIRouter

from app.services import settle as svc

router = APIRouter(tags=["敞口"])


@router.get("/exposures")
def get_exposures():
    return svc.list_exposures()
