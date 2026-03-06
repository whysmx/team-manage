"""
兑换流程服务
协调用户兑换流程，包括验证、Team选择、邀请发送、事务处理和并发控制
"""
import logging
import asyncio
from typing import Optional, Dict, Any
from datetime import datetime, timedelta
from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, RedemptionCode, RedemptionRecord
from app.services.redemption import RedemptionService
from app.services.warranty import WarrantyService
from app.services.team import TeamService
from app.services.chatgpt import ChatGPTService
from app.services.encryption import encryption_service
from app.services.notification import notification_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

WARRANTY_TEAM_MAX_MEMBERS = 5
STANDARD_TEAM_MAX_MEMBERS = 10


class RedeemFlowService:
    """兑换流程服务类"""

    def __init__(self):
        """初始化兑换流程服务"""
        from app.services.chatgpt import chatgpt_service
        self.redemption_service = RedemptionService()
        self.warranty_service = WarrantyService()
        self.team_service = TeamService()
        self.chatgpt_service = chatgpt_service

    async def verify_code_and_get_teams(
        self,
        code: str,
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        验证兑换码并获取可用 Team 列表

        Args:
            code: 兑换码
            db_session: 数据库会话

        Returns:
            结果字典,包含 success, valid, reason, teams, error
        """
        try:
            # 1. 验证兑换码
            # 使用事务以确保状态更新(如标记为已过期)被持久化
            async with db_session.begin():
                validate_result = await self.redemption_service.validate_code(code, db_session)
            
            if not validate_result["success"]:
                return {
                    "success": False,
                    "valid": False,
                    "reason": None,
                    "teams": [],
                    "error": validate_result["error"]
                }
            
            if not validate_result["valid"]:
                return {
                    "success": True,
                    "valid": False,
                    "reason": validate_result["reason"],
                    "teams": [],
                    "error": None
                }

            # 2. 根据兑换码类型选择对应车队池
            stmt = select(RedemptionCode).where(RedemptionCode.code == code)
            result = await db_session.execute(stmt)
            redemption_code = result.scalar_one_or_none()
            if not redemption_code:
                return {
                    "success": False,
                    "valid": False,
                    "reason": None,
                    "teams": [],
                    "error": "兑换码记录丢失"
                }

            target_max_members = WARRANTY_TEAM_MAX_MEMBERS if redemption_code.has_warranty else STANDARD_TEAM_MAX_MEMBERS

            # 2. 获取可用 Team 列表
            teams_result = await self.team_service.get_available_teams(
                db_session,
                target_max_members=target_max_members
            )

            if not teams_result["success"]:
                return {
                    "success": False,
                    "valid": True,
                    "reason": None,
                    "teams": [],
                    "error": teams_result["error"]
                }

            target_pool_desc = "<=5 人车队" if redemption_code.has_warranty else ">5 人车队"
            logger.info(
                f"验证兑换码成功: {code}, 目标车队池={target_pool_desc}, 可用 Team 数量: {len(teams_result['teams'])}"
            )

            return {
                "success": True,
                "valid": True,
                "reason": None,
                "teams": teams_result["teams"],
                "error": None
            }

        except Exception as e:
            logger.error(f"验证兑换码并获取 Team 列表失败: {e}")
            return {
                "success": False,
                "valid": False,
                "reason": None,
                "teams": [],
                "error": f"验证失败: {str(e)}"
            }

    async def select_team_auto(
        self,
        db_session: AsyncSession,
        email: Optional[str] = None,
        target_max_members: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        自动选择 Team (选择过期时间最早的)
        如果提供了 email，则自动排除该用户已经加入过的 Team

        Args:
            db_session: 数据库会话
            email: 用户邮箱 (用于排除已加入的 Team)
            target_max_members: 目标车队最大人数 (例如 5=质保池,10=非质保池)

        Returns:
            结果字典,包含 success, team_id, error
        """
        try:
            # 1. 查找用户已经加入过的 Team ID
            exclude_team_ids = []
            if email:
                stmt = select(RedemptionRecord.team_id).where(RedemptionRecord.email == email)
                result = await db_session.execute(stmt)
                exclude_team_ids = result.scalars().all()
                if exclude_team_ids:
                    logger.info(f"自动选择 Team: 排除用户 {email} 已加入的 Team IDs: {exclude_team_ids}")

            # 2. 查询可用 Team，按过期时间升序排序
            stmt = select(Team).where(
                Team.status == "active",
                Team.current_members < Team.max_members
            )
            if target_max_members is not None:
                if target_max_members <= WARRANTY_TEAM_MAX_MEMBERS:
                    stmt = stmt.where(Team.max_members <= WARRANTY_TEAM_MAX_MEMBERS)
                else:
                    stmt = stmt.where(Team.max_members > WARRANTY_TEAM_MAX_MEMBERS)
            
            # 排除已加入的 Team
            if exclude_team_ids:
                stmt = stmt.where(Team.id.not_in(exclude_team_ids))
            
            stmt = stmt.order_by(Team.expires_at.asc()).limit(1)

            result = await db_session.execute(stmt)
            team = result.scalar_one_or_none()

            if not team:
                reason = "没有可用的 Team"
                if exclude_team_ids:
                    reason = "您已加入所有可用 Team"
                if target_max_members is not None:
                    pool_desc = "<=5 人车队" if target_max_members <= WARRANTY_TEAM_MAX_MEMBERS else ">5 人车队"
                    reason = f"没有可用的 {pool_desc}"
                    if exclude_team_ids:
                        reason = f"您已加入所有可用的 {pool_desc}"
                return {
                    "success": False,
                    "team_id": None,
                    "error": reason
                }

            logger.info(
                f"自动选择 Team: {team.id} (过期时间: {team.expires_at}, max_members={team.max_members})"
            )

            return {
                "success": True,
                "team_id": team.id,
                "error": None
            }

        except Exception as e:
            logger.error(f"自动选择 Team 失败: {e}")
            return {
                "success": False,
                "team_id": None,
                "error": f"自动选择 Team 失败: {str(e)}"
            }

    async def redeem_and_join_team(
        self,
        email: str,
        code: str,
        team_id: Optional[int],
        db_session: AsyncSession
    ) -> Dict[str, Any]:
        """
        完整的兑换流程 (带事务和并发控制)
        优化版本: 将网络请求移出写事务,避免 SQLite 锁定
        """
        max_retries = 3
        current_target_team_id = team_id
        last_error = "未知错误"

        for attempt in range(max_retries):
            # 彻底确保会话处于干净状态，防止 "A transaction is already begun" 错误
            # SELECT 操作会隐式开启事务，导致后续 begin() 报错
            if db_session.in_transaction():
                await db_session.rollback()
                
            # 确保每次尝试都从数据库读取最新数据, 避免 identity map 缓存了上一次尝试修改后的状态
            db_session.expire_all()
            
            logger.info(f"正在尝试兑换 (第 {attempt + 1}/{max_retries} 次尝试): email={email}, code={code}")
            
            # 1. 快速检查并执行耗时的质保验证 (事务外执行，避免 SQLite 锁定时间过长)
            try:
                stmt = select(RedemptionCode).where(RedemptionCode.code == code)
                res = await db_session.execute(stmt)
                rc_pre = res.scalar_one_or_none()
                
                if not rc_pre:
                    return {"success": False, "error": "兑换码不存在"}
                
                if rc_pre.has_warranty and rc_pre.status in ["warranty_active", "used"]:
                    # 耗时的网络请求和状态检测放在事务外
                    warranty_check = await self.warranty_service.validate_warranty_reuse(
                        db_session, code, email
                    )
                    if not warranty_check["success"] or not warranty_check["can_reuse"]:
                        # 确保返回字符串，防止 routes 层 NoneType 报错
                        error_msg = warranty_check.get("reason") or warranty_check.get("error") or "质保校验未通过"
                        return {"success": False, "error": error_msg}
            except Exception as e:
                logger.error(f"兑换前置校验异常: {e}")
                if attempt < max_retries - 1: continue
                return {"success": False, "error": f"系统校验异常: {str(e)}"}
            # 确保会话回到干净状态，防止阶段 0 的隐式事务导致后面 begin() 报错
            if db_session.in_transaction():
                await db_session.commit()

            # --- 阶段 0: 预同步 Team 信息 (先更新人数) ---
            # 确定本次尝试的目标 Team
            is_auto_select = current_target_team_id is None
            active_team_id = current_target_team_id
            target_max_members = WARRANTY_TEAM_MAX_MEMBERS if rc_pre.has_warranty else STANDARD_TEAM_MAX_MEMBERS
            
            if is_auto_select:
                select_result = await self.select_team_auto(
                    db_session,
                    email=email,
                    target_max_members=target_max_members
                )
                if not select_result["success"]:
                    return {"success": False, "error": select_result["error"]}
                active_team_id = select_result["team_id"]
            
            # 同步最新人数
            logger.info(f"兑换前同步 Team {active_team_id} 状态...")
            await self.team_service.sync_team_info(active_team_id, db_session)

            # --- 关键修复：确保阶段 0 产生的隐式事务已提交，否则接下来的 begin() 会报错 ---
            if db_session.in_transaction():
                await db_session.commit()
            db_session.expire_all()

            team_id_final = None
            try:
                # --- 阶段 1: 验证并占位 (短事务) ---
                async with db_session.begin():
                    # 再次验证并锁定 (确保原子性)
                    validate_result = await self.redemption_service.validate_code(code, db_session)
                    if not validate_result["success"]:
                        return {"success": False, "error": validate_result["error"]}
                    if not validate_result["valid"]:
                        return {"success": False, "error": validate_result["reason"]}

                    # 再次验证并锁定 (带锁锁定，防止并发)
                    stmt = select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                    result = await db_session.execute(stmt)
                    redemption_code = result.scalar_one_or_none()
                    
                    if not redemption_code:
                        return {"success": False, "error": "兑换码记录丢失"}

                    # 检查状态是否依然有效 (可能在循环间隙被别人捷足先登)
                    allowed_statuses = ["unused", "warranty_active"]
                    if redemption_code.has_warranty:
                        allowed_statuses.append("used")

                    if redemption_code.status not in allowed_statuses:
                        return {"success": False, "error": "兑换码已被使用"}

                    is_warranty_code = redemption_code.has_warranty

                    # 2. 确定 Team (使用阶段 0 选定并同步过的 Team)
                    team_id_final = active_team_id

                    # 3. 锁定并检查 Team (未满再邀请)
                    stmt = select(Team).where(Team.id == team_id_final).with_for_update()
                    result = await db_session.execute(stmt)
                    team = result.scalar_one_or_none()

                    if not team:
                        if is_auto_select and attempt < max_retries - 1:
                            logger.warning(f"选择的 Team {team_id_final} 消失了, 尝试下一次循环")
                            current_target_team_id = None
                            continue
                        return {"success": False, "error": f"Team {team_id_final} 不存在"}

                    if is_warranty_code and team.max_members > WARRANTY_TEAM_MAX_MEMBERS:
                        expected_type_desc = "质保车队(<=5人)"
                        if is_auto_select and attempt < max_retries - 1:
                            logger.warning(f"自动选择到了非质保车队 Team {team_id_final}, 尝试下一次循环")
                            current_target_team_id = None
                            continue
                        return {"success": False, "error": f"兑换码与车队类型不匹配，请选择{expected_type_desc}"}
                    if (not is_warranty_code) and team.max_members <= WARRANTY_TEAM_MAX_MEMBERS:
                        expected_type_desc = "非质保车队(>5人)"
                        if is_auto_select and attempt < max_retries - 1:
                            logger.warning(f"自动选择到了质保车队 Team {team_id_final}, 尝试下一次循环")
                            current_target_team_id = None
                            continue
                        return {"success": False, "error": f"兑换码与车队类型不匹配，请选择{expected_type_desc}"}
                    
                    if team.current_members >= team.max_members:
                        if is_auto_select and attempt < max_retries - 1:
                            logger.warning(f"选择的 Team {team_id_final} 已满, 尝试下一次循环")
                            current_target_team_id = None
                            continue 
                        return {"success": False, "error": "Team 已满，请选择其他 Team"}
                    
                    if team.status != "active":
                        if is_auto_select and attempt < max_retries - 1:
                            logger.warning(f"选择的 Team {team_id_final} 状态异常 ({team.status}), 尝试下一次循环")
                            current_target_team_id = None
                            continue
                        return {"success": False, "error": f"Team 状态异常: {team.status}"}

                    # 特殊处理质保码逻辑
                    is_first_use = redemption_code.status == "unused"
                    
                    if not is_first_use:
                        if not is_warranty_code:
                            return {"success": False, "error": "兑换码已被占用"}
                        # 针对质保码，已经在事务外通过 validate_warranty_reuse 完成了深度验证和孤儿记录清理
                        # 事务内只需通过 allowed_statuses 过滤掉非法状态即可

                    # 4. 更新状态执行占位
                    if is_warranty_code:
                        redemption_code.status = "warranty_active"
                        if is_first_use:
                            warranty_days = redemption_code.warranty_days or 30
                            redemption_code.warranty_expires_at = get_now() + timedelta(days=warranty_days)
                    else:
                        redemption_code.status = "used"
                    
                    redemption_code.used_by_email = email
                    redemption_code.used_team_id = team_id_final
                    redemption_code.used_at = get_now()

                    # 增加 Team 成员数占位 (不再手动 +1，由后续 sync 同步)
                    # team.current_members += 1
                    # if team.current_members >= team.max_members:
                    #     team.status = "full"
                    
                    # 记录信息供 Phase 2 使用
                    final_team_account_id = team.account_id
                    final_team_name = team.team_name
                    final_team_expires_at = team.expires_at
                    final_access_token_encrypted = team.access_token_encrypted
                    final_is_warranty = is_warranty_code
                    
                    # 事务 commit
                
                # --- 阶段 2: 网络请求 ---
                # 获取该 Team 的最新数据以确保 Token 也是最新的 (可能被其他进程同步过)
                stmt = select(Team).where(Team.id == team_id_final)
                res = await db_session.execute(stmt)
                target_team = res.scalar_one_or_none()
                
                if not target_team:
                    await self._rollback_redemption(db_session, code, team_id_final, email=email)
                    if attempt < max_retries - 1:
                        current_target_team_id = None
                        continue
                    return {"success": False, "error": "所选 Team 已失效"}

                # 确保 Access Token 有效 (过期则尝试使用 RT/ST 刷新)
                access_token = await self.team_service.ensure_access_token(target_team, db_session)
                if not access_token:
                    logger.warning(f"无法获取有效的 Access Token (Team {team_id_final})")
                    await self._rollback_redemption(db_session, code, team_id_final, email=email)
                    if attempt < max_retries - 1:
                        current_target_team_id = None
                        continue
                    return {"success": False, "error": "Team 账号 Token 已失效且无法刷新"}

                invite_result = await self.chatgpt_service.send_invite(
                    access_token, final_team_account_id, email, db_session,
                    identifier=target_team.email
                )

                # --- 阶段 3: 最终化 ---
                if invite_result["success"]:
                    # 阶段 2 的网络请求中可能涉及查询设置(代理等)，会隐式开启事务
                    if db_session.in_transaction():
                        await db_session.rollback()
                        
                    async with db_session.begin():
                        redemption_record = RedemptionRecord(
                            email=email,
                            code=code,
                            team_id=team_id_final,
                            account_id=final_team_account_id,
                            is_warranty_redemption=final_is_warranty
                        )
                        db_session.add(redemption_record)

                        # 成功后人数+1 (先本地更新，确保即时性)
                        stmt = select(Team).where(Team.id == team_id_final).with_for_update()
                        res = await db_session.execute(stmt)
                        t = res.scalar_one_or_none()
                        if t:
                            t.current_members += 1
                            if t.current_members >= t.max_members:
                                t.status = "full"
                        
                    # 确保在 sleep 前没有任何未决事务，彻底释放读视图，避免占用数据库资源
                    if db_session.in_transaction():
                        await db_session.rollback()
                    db_session.expire_all()

                    # 延时 5 秒再同步，给 API 留出同步缓冲时间，减少虚假成功误判
                    logger.info(f"等待 5 秒后校验邀请结果: {email}")
                    await asyncio.sleep(5)

                    # 同步最新成员数并校验邀请是否生效
                    sync_res = await self.team_service.sync_team_info(team_id_final, db_session)
                    
                    # 显式提交同步结果，确保 last_sync 等信息已写入数据库
                    if db_session.in_transaction():
                        await db_session.commit()
                    
                    # 强校验：如果同步结果中没有当前邮箱，说明是“虚假成功”
                    member_emails = sync_res.get("member_emails", [])
                    if email.lower() not in [m.lower() for m in member_emails]:
                        logger.error(f"检测到“虚假成功”: Team {team_id_final} 接口返回邀请成功，但同步成员列表未见该邮箱 {email}")
                        
                        # 手动标记错误并累加计数
                        if db_session.in_transaction():
                            await db_session.rollback()
                            
                        async with db_session.begin():
                            stmt = select(Team).where(Team.id == team_id_final).with_for_update()
                            res = await db_session.execute(stmt)
                            target_team = res.scalar_one_or_none()
                            if target_team:
                                target_team.error_count = (target_team.error_count or 0) + 1
                                if target_team.error_count >= 2:
                                    logger.error(f"Team {target_team.id} 连续虚假成功/错误 {target_team.error_count} 次，标记为 error")
                                    target_team.status = "error"
                                await db_session.commit()
                        
                        # 触发回退并进入重试逻辑
                        await self._rollback_redemption(db_session, code, team_id_final, email=email)
                        last_error = f"Team {team_id_final} 校验失败（邀请发送成功但同步未见成员）"
                        
                        if attempt < max_retries - 1:
                            logger.info(f"检测到虚假成功，原 Team ({team_id_final}) 第 {attempt + 1} 次重试...")
                            current_target_team_id = team_id_final # 保持同一个 Team 重试
                            continue
                        return {"success": False, "error": f"连续 {max_retries} 次虚假成功，该 Team 账号 ({team_id_final}) 可能存在同步延迟或异常，请稍后再试"}
                    
                    logger.info(f"兑换成功: {email} 加入 Team {team_id_final}")

                    # 检查库存并发送通知 (异步不阻塞)
                    asyncio.create_task(notification_service.check_and_notify_low_stock())

                    return {
                        "success": True,
                        "message": f"成功加入 Team: {final_team_name}",
                        "team_info": {
                            "team_id": team_id_final,
                            "team_name": final_team_name,
                            "account_id": final_team_account_id,
                            "expires_at": final_team_expires_at.isoformat() if final_team_expires_at else None
                        },
                        "error": None
                    }
                else:
                    logger.warning(f"API 邀请失败 (尝试 {attempt + 1}): {invite_result['error']}")
                    await self._rollback_redemption(db_session, code, team_id_final, email=email)
                    
                    error_msg = invite_result.get("error", "未知错误")
                    
                    # 重新查询 Team 以获取最新状态（尤其是错误计数和状态）
                    stmt = select(Team).where(Team.id == team_id_final)
                    res = await db_session.execute(stmt)
                    target_team = res.scalar_one_or_none()
                    
                    if target_team:
                        # 处理 API 错误（标记封禁、满员、计数错误，如果是 token_expired 则在后台尝试刷新）
                        await self.team_service._handle_api_error(invite_result, target_team, db_session)
                        
                        # 根据最新状态调整给用户的错误信息
                        if target_team.status == "banned":
                            error_msg = "Team 账号被封禁"
                        elif target_team.status == "full":
                            error_msg = "Team 席位已满"
                        elif target_team.status == "error":
                            error_msg = "Team 账号连续出错，已标记异常"
                    
                    last_error = error_msg
                    
                    # 只要还有重试机会，就尝试更换 Team (符合用户要求：报错就尝试下一个)
                    if attempt < max_retries - 1:
                        logger.info(f"加入失败，尝试更换 Team 重试... (错误: {error_msg})")
                        current_target_team_id = None
                        continue
                    else:
                        return {"success": False, "error": f"加入失败: {error_msg}"}

            except Exception as e:
                logger.error(f"兑换尝试异常 (第 {attempt + 1} 次): {e}")
                if team_id_final:
                    try:
                        await self._rollback_redemption(db_session, code, team_id_final, email=email)
                    except:
                        pass
                if attempt < max_retries - 1:
                    continue
                return {"success": False, "error": f"兑换系统异常: {str(e)}"}

    async def _rollback_redemption(
        self,
        db_session: AsyncSession,
        code: str,
        team_id: int,
        email: Optional[str] = None
    ):
        """回退兑换占位"""
        try:
            # 确保会话干净，防止在异常处理路径中再次触发事务冲突
            if db_session.in_transaction():
                await db_session.rollback()
                
            async with db_session.begin():
                # 1. 删除可能已创建的使用记录 (针对 Phase 3 的回退)
                if email:
                    stmt = delete(RedemptionRecord).where(
                        RedemptionRecord.code == code,
                        RedemptionRecord.team_id == team_id,
                        RedemptionRecord.email == email
                    )
                    await db_session.execute(stmt)

                # 2. 回退兑换码状态
                stmt = select(RedemptionCode).where(RedemptionCode.code == code).with_for_update()
                result = await db_session.execute(stmt)
                redemption_code = result.scalar_one_or_none()
                if redemption_code:
                    # 质保码回退到 warranty_active 或 unused
                    if redemption_code.has_warranty:
                        # 检查是否有其他成功的兑换记录
                        stmt = select(RedemptionRecord).where(
                            RedemptionRecord.code == code
                        ).order_by(RedemptionRecord.redeemed_at.desc())
                        result = await db_session.execute(stmt)
                        other_record = result.scalars().first()
                        
                        if other_record:
                            # 有其他记录，恢复为最后一次成功的状态
                            redemption_code.status = "warranty_active"
                            redemption_code.used_by_email = other_record.email
                            redemption_code.used_team_id = other_record.team_id
                            redemption_code.used_at = other_record.redeemed_at
                        else:
                            # 没有其他成功记录，彻底回退到未使用
                            redemption_code.status = "unused"
                            redemption_code.warranty_expires_at = None
                            redemption_code.used_by_email = None
                            redemption_code.used_team_id = None
                            redemption_code.used_at = None
                    else:
                        # 普通码彻底回退到 unused
                        redemption_code.status = "unused"
                        redemption_code.used_by_email = None
                        redemption_code.used_team_id = None
                        redemption_code.used_at = None

                # 回退 Team 计数 (不再手动 -1，稍后由 sync 同步，或保持原样)
                # if team.current_members > 0:
                #     team.current_members -= 1
                # if team.status == "full" and team.current_members < team.max_members:
                #     team.status = "active"
                
                # 在回滚时可能由于各种原因失败，也尝试刷新一次成员数
                # 注意：这里可能需要从 sync_team_info 获取最新数据
                try:
                    await self.team_service.sync_team_info(team_id, db_session)
                except:
                    pass
            logger.info(f"已回退兑换占位: code={code}, team_id={team_id}")
        except Exception as e:
            logger.error(f"回退兑换占位失败: {e}")


# 创建全局实例
redeem_flow_service = RedeemFlowService()
