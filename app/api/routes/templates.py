import json

from fastapi import APIRouter, Depends, Form
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin_user, require_user
from app.models.agent_template import AgentTemplate

router = APIRouter()


@router.get("")
def list_templates(
    db: Session = Depends(get_db),
    current_user=Depends(require_user),
):
    templates = db.execute(select(AgentTemplate)).scalars().all()
    return {
        "items": [
            {
                "id": template.id,
                "name": template.name,
                "description": template.description,
                "category": template.category,
                "model": template.model,
                "tools": json.loads(template.tools or "[]"),
                "fields": json.loads(template.fields or "[]"),
            }
            for template in templates
        ]
    }


@router.post("")
def create_template(
    name: str = Form(...),
    description: str | None = Form(default=None),
    category: str | None = Form(default=None),
    model: str | None = Form(default=None),
    tools: str | None = Form(default=None),
    fields: str | None = Form(default=None),
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_user),
):
    template = AgentTemplate(
        name=name,
        description=description,
        category=category or "general",
        model=model or "auto",
        tools=tools or "[]",
        fields=fields or "[]",
    )
    db.add(template)
    db.commit()
    db.refresh(template)
    return {"id": template.id}
