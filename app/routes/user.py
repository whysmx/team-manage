"""
用户路由
处理用户兑换页面
"""
import logging
from pathlib import Path
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from app.database import get_db
from app.models import RedemptionCode

logger = logging.getLogger(__name__)

# 创建路由器
router = APIRouter(
    tags=["user"]
)


@router.get("/", response_class=HTMLResponse)
async def redeem_page(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    用户兑换页面

    Args:
        request: FastAPI Request 对象
        db: 数据库会话

    Returns:
        用户兑换页面 HTML
    """
    try:
        from app.main import templates
        from app.services.team import TeamService
        
        team_service = TeamService()
        remaining_spots = await team_service.get_total_available_spots(db)
        used_codes_stmt = select(func.count(RedemptionCode.id)).where(
            RedemptionCode.status.in_(["used", "warranty_active"])
        )
        used_codes_result = await db.execute(used_codes_stmt)
        used_codes_count = used_codes_result.scalar() or 0
        served_count = used_codes_count
        redeem_css_version = ""
        redeem_js_version = ""
        try:
            redeem_css_path = Path(__file__).resolve().parents[1] / "static" / "css" / "user.css"
            redeem_css_version = str(int(redeem_css_path.stat().st_mtime))
        except Exception:
            redeem_css_version = ""

        try:
            redeem_js_path = Path(__file__).resolve().parents[1] / "static" / "js" / "redeem.js"
            redeem_js_version = str(int(redeem_js_path.stat().st_mtime))
        except Exception:
            # 静态文件版本号仅用于缓存更新，失败时不影响主流程
            redeem_js_version = ""

        logger.info(f"用户访问兑换页面，剩余车位: {remaining_spots}")

        return templates.TemplateResponse(
            "user/redeem.html",
            {
                "request": request,
                "remaining_spots": remaining_spots,
                "served_count": served_count,
                "redeem_css_version": redeem_css_version,
                "redeem_js_version": redeem_js_version
            }
        )

    except Exception as e:
        logger.error(f"渲染兑换页面失败: {e}")
        return HTMLResponse(
            content=f"<h1>页面加载失败</h1><p>{str(e)}</p>",
            status_code=500
        )
