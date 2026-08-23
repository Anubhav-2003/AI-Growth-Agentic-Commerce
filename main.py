from fastapi import FastAPI, HTTPException
import uvicorn

from pydantic_models.request_types import NormalizeRequest
from services.normalization_service import normalization_service

app = FastAPI()

@app.get("/")
def read_root():
    return {"message": "Welcome to the FastAPI server!"}

@app.post("/sync")
def sync_data(request: NormalizeRequest):
    normalizer: normalization_service = normalization_service(request.vendor_id)

    if (normalizer.run()):
        return {"message": "Data normalization completed successfully."}
    else:
        raise HTTPException(status_code=500, detail="Data normalization failed.")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
