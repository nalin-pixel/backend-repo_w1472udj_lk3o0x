import os
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SearchPayload(BaseModel):
    # Which freshness window to query (Fantastic.jobs only)
    time_window: str = Field(
        "7d", description="One of: 7d, 24h, hourly, backfill, expired, modified"
    )

    # API auth: prefer env vars, but allow passing from client for convenience during setup
    api_key: Optional[str] = Field(
        None, description="RapidAPI key"
    )
    api_host: Optional[str] = Field(
        None, description="RapidAPI host (fantastic.p.rapidapi.com or active-jobs-db.p.rapidapi.com)"
    )

    # Core search filters (Fantastic.jobs)
    title_filter: Optional[str] = None
    advanced_title_filter: Optional[str] = None
    location_filter: Optional[str] = None
    description_filter: Optional[str] = None
    organization_filter: Optional[str] = None
    organization_exclusion_filter: Optional[str] = None
    advanced_organization_filter: Optional[str] = None
    source: Optional[str] = None
    remote: Optional[str] = Field(None, description="true | false | None")
    include_ai: Optional[bool] = Field(False, description="Include AI-enriched fields")

    # AI filters (Fantastic.jobs)
    ai_employment_type_filter: Optional[str] = None
    ai_work_arrangement_filter: Optional[str] = None
    ai_taxonomies_a_filter: Optional[str] = None
    ai_taxonomies_a_primary_filter: Optional[str] = None
    ai_taxonomies_a_exclusion_filter: Optional[str] = None
    ai_has_salary: Optional[str] = None
    ai_experience_level_filter: Optional[str] = None
    ai_visa_sponsorship_filter: Optional[str] = None

    # LinkedIn filters (Fantastic.jobs)
    include_li: Optional[bool] = Field(False)
    li_organization_slug_filter: Optional[str] = None
    li_organization_slug_exclusion_filter: Optional[str] = None
    li_industry_filter: Optional[str] = None
    li_organization_specialties_filter: Optional[str] = None
    li_organization_description_filter: Optional[str] = None

    # Pagination
    limit: Optional[int] = Field(20, ge=1, le=500)
    offset: Optional[int] = Field(0, ge=0)

    # Date filter (Fantastic.jobs only; not for hourly/backfill)
    date_filter: Optional[str] = None

    # Description type (supported by both; Active Jobs expects text|html)
    description_type: Optional[str] = Field(
        None, description="text | html"
    )


def detect_provider(api_host: str) -> str:
    if not api_host:
        return "fantastic"
    host = api_host.lower().strip()
    if "active-jobs-db.p.rapidapi.com" in host:
        return "active"
    return "fantastic"


def get_endpoint_path_fantastic(window: str) -> str:
    mapping = {
        "7d": "/jobs/7d",
        "24h": "/jobs/24h",
        "hourly": "/jobs/hourly",
        "backfill": "/jobs/6m",
        "expired": "/jobs/expired",
        "modified": "/jobs/modified",
    }
    return mapping.get(window, "/jobs/7d")


def build_params(payload: SearchPayload, provider: str) -> Dict[str, Any]:
    fields = payload.model_dump()

    if provider == "active":
        # Active Jobs DB: Modified jobs 24h supports only a narrow set of params
        allowed = ["limit", "offset", "description_type"]
    else:
        # Fantastic.jobs: pass through rich filters
        allowed = [
            "title_filter",
            "advanced_title_filter",
            "location_filter",
            "description_filter",
            "organization_filter",
            "organization_exclusion_filter",
            "advanced_organization_filter",
            "source",
            "remote",
            "include_ai",
            "ai_employment_type_filter",
            "ai_work_arrangement_filter",
            "ai_taxonomies_a_filter",
            "ai_taxonomies_a_primary_filter",
            "ai_taxonomies_a_exclusion_filter",
            "ai_has_salary",
            "ai_experience_level_filter",
            "ai_visa_sponsorship_filter",
            "include_li",
            "li_organization_slug_filter",
            "li_organization_slug_exclusion_filter",
            "li_industry_filter",
            "li_organization_specialties_filter",
            "li_organization_description_filter",
            "limit",
            "offset",
            "date_filter",
            "description_type",
        ]

    params: Dict[str, Any] = {}
    for key in allowed:
        val = fields.get(key)
        if val is not None and val != "":
            params[key] = val

    # Provide provider-specific sane defaults and clamps
    if provider == "active":
        # Defaults per docs: limit 100 if omitted, valid range 10..100
        limit = params.get("limit")
        if limit is None:
            params["limit"] = 100
        else:
            try:
                lim_int = int(limit)
            except (TypeError, ValueError):
                lim_int = 100
            lim_int = max(10, min(100, lim_int))
            params["limit"] = lim_int
        # Offset default 0
        try:
            params["offset"] = int(params.get("offset", 0))
        except (TypeError, ValueError):
            params["offset"] = 0
        # Description type default text
        params.setdefault("description_type", "text")

    return params


@app.post("/api/search")
def search_jobs(payload: SearchPayload):
    # Prefer environment variables for security (Fantastic defaults)
    api_key = payload.api_key or os.getenv("FANTASTIC_RAPIDAPI_KEY")
    api_host = payload.api_host or os.getenv("FANTASTIC_RAPIDAPI_HOST", "fantastic.p.rapidapi.com")

    provider = detect_provider(api_host)

    if provider == "active":
        endpoint_path = "/modified-ats-24h"
    else:
        endpoint_path = get_endpoint_path_fantastic(payload.time_window)

    # Construct base URL from the provided host
    base_url = f"https://{api_host}"
    url = f"{base_url}{endpoint_path}"

    headers = {
        "X-RapidAPI-Key": api_key or "",
        "X-RapidAPI-Host": api_host,
        "Accept": "application/json",
    }

    params = build_params(payload, provider)

    # If there's no key yet, return a helpful message with empty jobs to keep UI working
    if not api_key:
        return {
            "jobs": [],
            "count": 0,
            "note": "Add your API key to fetch live jobs.",
            "provider": provider,
            "endpoint": url,
            "params": params,
        }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        # Collect rate limit headers if present
        rl_headers = {
            k.lower(): v
            for k, v in resp.headers.items()
            if k.lower().startswith("x-ratelimit") or k.lower().startswith("ratelimit")
        }
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail={
                "message": "Upstream API error",
                "status": resp.status_code,
                "text": resp.text,
                "endpoint": url,
                "params": params,
                "rate_limits": rl_headers,
            })
        data = resp.json()
        # Normalize: some APIs return arrays, others objects
        jobs = data if isinstance(data, list) else data.get("results") or data.get("jobs") or data.get("data") or []
        return {
            "jobs": jobs,
            "count": len(jobs),
            "rate_limits": rl_headers,
            "provider": provider,
        }
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail={"message": "Network error", "error": str(e)})


@app.get("/")
def read_root():
    return {"message": "Job Aggregator Backend is running"}


@app.get("/api/hello")
def hello():
    return {"message": "Hello from the backend API!"}


@app.get("/test")
def test_database():
    """Test endpoint to check if database is available and accessible"""
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }

    try:
        # Try to import database module
        from database import db

        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"

            # Try to list collections to verify connectivity
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]  # Show first 10 collections
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"

    except ImportError:
        response["database"] = "❌ Database module not found (run enable-database first)"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"

    # Check environment variables
    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"

    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
