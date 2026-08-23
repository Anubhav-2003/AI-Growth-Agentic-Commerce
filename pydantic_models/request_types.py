from pydantic import BaseModel

class NormalizeRequest(BaseModel):
    vendor_id: int
