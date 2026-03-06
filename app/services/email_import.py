"""
邮箱验证码登录并提取 AT，再导入 Team 的服务
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx
from curl_cffi.requests import AsyncSession
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession

from app.services.settings import settings_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


class EmailImportService:
    """邮箱导入服务"""

    OTP_PATTERN = re.compile(r"(?<!\d)(\d{6})(?!\d)")

    CHATGPT_AUTH_PROVIDERS_URL = "https://chatgpt.com/api/auth/providers"
    CHATGPT_AUTH_CSRF_URL = "https://chatgpt.com/api/auth/csrf"
    CHATGPT_AUTH_SIGNIN_URL = "https://chatgpt.com/api/auth/signin/openai"

    OPENAI_AUTHORIZE_CONTINUE_URL = "https://auth.openai.com/api/accounts/authorize/continue"
    OPENAI_EMAIL_VALIDATE_URL = "https://auth.openai.com/api/accounts/email-otp/validate"

    SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"
    SENTINEL_FLOW = "authorize_continue"
    SENTINEL_P_CANDIDATES = ["x"]  # 最小可用 payload，Sentinel 端会返回 token

    YXIANG_API_URL_TEMPLATE = "http://www.yxiang6.com/api/GetLastEmails?email={email}&boxType=1&num=2"

    OTP_TIMEOUT_SECONDS = 180
    OTP_POLL_INTERVAL_SECONDS = 2.5

    def __init__(self):
        from app.services.chatgpt import chatgpt_service
        from app.services.team import team_service
        self.chatgpt_service = chatgpt_service
        self.team_service = team_service

    async def _create_http_session(self, db_session: DBAsyncSession) -> AsyncSession:
        proxy = None
        proxy_config = await settings_service.get_proxy_config(db_session)
        if proxy_config.get("enabled") and proxy_config.get("proxy"):
            proxy = proxy_config["proxy"]

        return AsyncSession(
            impersonate="chrome110",
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=45,
            verify=False
        )

    def _safe_json(self, response) -> Dict[str, Any]:
        try:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        except Exception:
            return {"raw": response.text}

    def _extract_error(self, response, default: str) -> str:
        data = self._safe_json(response)
        if isinstance(data.get("error"), dict):
            msg = data["error"].get("message")
            if msg:
                return msg
            code = data["error"].get("code")
            if code:
                return f"{default}: {code}"
        if isinstance(data.get("error"), str):
            return data["error"]
        if isinstance(data.get("detail"), str):
            return data["detail"]
        if isinstance(data.get("message"), str):
            return data["message"]
        return default

    async def _init_auth_context(
        self,
        http_session: AsyncSession,
        device_id: str,
        auth_session_logging_id: str
    ) -> Dict[str, Any]:
        # 1) 取 providers / csrf
        providers_resp = await http_session.get(
            self.CHATGPT_AUTH_PROVIDERS_URL,
            headers={
                "content-type": "application/json",
                "referer": "https://chatgpt.com/auth/login"
            }
        )
        if providers_resp.status_code != 200:
            return {
                "success": False,
                "error": self._extract_error(providers_resp, "获取登录 Providers 失败")
            }

        csrf_resp = await http_session.get(
            self.CHATGPT_AUTH_CSRF_URL,
            headers={
                "content-type": "application/json",
                "referer": "https://chatgpt.com/auth/login"
            }
        )
        if csrf_resp.status_code != 200:
            return {
                "success": False,
                "error": self._extract_error(csrf_resp, "获取 CSRF Token 失败")
            }

        csrf_token = self._safe_json(csrf_resp).get("csrfToken")
        if not csrf_token:
            return {"success": False, "error": "CSRF Token 为空"}

        # 2) 请求 Signin，拿 authorize URL
        signin_resp = await http_session.post(
            f"{self.CHATGPT_AUTH_SIGNIN_URL}"
            f"?prompt=login&screen_hint=login_or_signup"
            f"&ext-oai-did={device_id}&auth_session_logging_id={auth_session_logging_id}",
            data={
                "callbackUrl": "/",
                "csrfToken": csrf_token,
                "json": "true"
            },
            headers={
                "content-type": "application/x-www-form-urlencoded",
                "referer": "https://chatgpt.com/auth/login"
            }
        )
        if signin_resp.status_code != 200:
            return {
                "success": False,
                "error": self._extract_error(signin_resp, "初始化登录失败")
            }

        authorize_url = self._safe_json(signin_resp).get("url")
        if not authorize_url:
            return {"success": False, "error": "未获取到 authorize URL"}

        # 3) 访问 authorize URL，建立 auth.openai.com 上下文
        authorize_resp = await http_session.get(
            authorize_url,
            allow_redirects=False,
            headers={"referer": "https://chatgpt.com/"}
        )

        if authorize_resp.status_code in (301, 302, 303, 307, 308):
            redirect_url = authorize_resp.headers.get("Location")
            if redirect_url:
                if redirect_url.startswith("/"):
                    redirect_url = f"https://auth.openai.com{redirect_url}"
                await http_session.get(
                    redirect_url,
                    headers={"referer": "https://chatgpt.com/"}
                )

        return {"success": True}

    async def _build_sentinel_header(
        self,
        http_session: AsyncSession,
        device_id: str
    ) -> Dict[str, Any]:
        last_error = "Sentinel Token 生成失败"
        for sentinel_p in self.SENTINEL_P_CANDIDATES:
            try:
                response = await http_session.post(
                    self.SENTINEL_REQ_URL,
                    headers={
                        "content-type": "text/plain;charset=UTF-8",
                        "referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html?sv=20260219f9f6"
                    },
                    data=json.dumps({
                        "p": sentinel_p,
                        "id": device_id,
                        "flow": self.SENTINEL_FLOW
                    })
                )
            except Exception as e:
                last_error = f"Sentinel 请求异常: {e}"
                continue

            if response.status_code != 200:
                last_error = self._extract_error(response, "Sentinel 请求失败")
                continue

            token = self._safe_json(response).get("token")
            if not token:
                last_error = "Sentinel 响应缺少 token"
                continue

            return {
                "success": True,
                "header_value": json.dumps(
                    {
                        "p": sentinel_p,
                        "c": token,
                        "id": device_id,
                        "flow": self.SENTINEL_FLOW
                    },
                    ensure_ascii=False
                )
            }

        return {"success": False, "error": last_error}

    async def _request_email_otp(
        self,
        http_session: AsyncSession,
        email: str,
        sentinel_header_value: str
    ) -> Dict[str, Any]:
        response = await http_session.post(
            self.OPENAI_AUTHORIZE_CONTINUE_URL,
            json={
                "username": {"value": email, "kind": "email"},
                "screen_hint": "login_or_signup"
            },
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "referer": "https://auth.openai.com/log-in-or-create-account",
                "openai-sentinel-token": sentinel_header_value
            }
        )

        if response.status_code != 200:
            return {
                "success": False,
                "error": self._extract_error(response, "请求邮箱验证码失败")
            }

        continue_url = self._safe_json(response).get("continue_url")
        if not continue_url or "email-verification" not in continue_url:
            return {"success": False, "error": "验证码流程未进入邮箱验证页"}

        return {"success": True}

    async def _fetch_latest_mail_date(self, email: str) -> Optional[str]:
        request_url = self.YXIANG_API_URL_TEMPLATE.format(email=quote(email))
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(request_url)
            if response.status_code != 200:
                return None
            data = response.json()
            mail_items = data.get("data", []) if isinstance(data, dict) else []
            if not mail_items:
                return None
            return mail_items[0].get("Date")

    def _extract_code_from_mail_item(self, item: Dict[str, Any]) -> Optional[str]:
        subject = item.get("Subject") or item.get("subject") or ""
        body = item.get("Body") or item.get("body") or ""
        match = self.OTP_PATTERN.search(f"{subject} {body}")
        return match.group(1) if match else None

    async def _wait_for_otp_code(
        self,
        email: str,
        baseline_date: Optional[str]
    ) -> Dict[str, Any]:
        deadline = datetime.now().timestamp() + self.OTP_TIMEOUT_SECONDS
        request_url = self.YXIANG_API_URL_TEMPLATE.format(email=quote(email))

        async with httpx.AsyncClient(timeout=20) as client:
            while datetime.now().timestamp() < deadline:
                try:
                    response = await client.get(request_url)
                    if response.status_code == 200:
                        data = response.json()
                        mail_items = data.get("data", []) if isinstance(data, dict) else []
                        for item in mail_items:
                            mail_date = item.get("Date")
                            # 只接受新邮件，避免拿到旧验证码
                            if baseline_date and mail_date and mail_date <= baseline_date:
                                continue
                            code = self._extract_code_from_mail_item(item)
                            if code:
                                return {"success": True, "code": code, "mail_date": mail_date}
                except Exception as e:
                    logger.warning(f"轮询验证码邮件失败: {e}")

                await asyncio.sleep(self.OTP_POLL_INTERVAL_SECONDS)

        return {"success": False, "error": "等待验证码超时"}

    async def _validate_email_otp(
        self,
        http_session: AsyncSession,
        code: str
    ) -> Dict[str, Any]:
        response = await http_session.post(
            self.OPENAI_EMAIL_VALIDATE_URL,
            json={"code": code},
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "referer": "https://auth.openai.com/email-verification"
            }
        )

        if response.status_code != 200:
            return {
                "success": False,
                "error": self._extract_error(response, "验证码校验失败")
            }

        continue_url = self._safe_json(response).get("continue_url")
        if not continue_url:
            return {"success": False, "error": "验证码校验成功但未返回回调地址"}

        return {"success": True, "continue_url": continue_url}

    async def _finish_login_and_get_session_token(
        self,
        http_session: AsyncSession,
        callback_url: str
    ) -> Dict[str, Any]:
        response = await http_session.get(
            callback_url,
            allow_redirects=True,
            headers={"referer": "https://auth.openai.com/"}
        )
        if response.status_code >= 400:
            return {"success": False, "error": f"完成登录回调失败: HTTP {response.status_code}"}

        for cookie in http_session.cookies.jar:
            if cookie.name == "__Secure-next-auth.session-token" and "chatgpt.com" in cookie.domain:
                return {"success": True, "session_token": cookie.value}

        return {"success": False, "error": "登录成功但未获取到 Session Token"}

    async def extract_at_and_import_team(
        self,
        email: str,
        db_session: DBAsyncSession,
        account_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        通过邮箱验证码流程提取 AT，并导入 Team
        """
        logger.info(f"开始邮箱提取并导入 Team: {email}")
        baseline_mail_date = await self._fetch_latest_mail_date(email)

        device_id = str(uuid.uuid4())
        auth_session_logging_id = str(uuid.uuid4())
        http_session = await self._create_http_session(db_session)

        try:
            init_result = await self._init_auth_context(
                http_session=http_session,
                device_id=device_id,
                auth_session_logging_id=auth_session_logging_id
            )
            if not init_result["success"]:
                return {
                    "success": False,
                    "email": email,
                    "error": init_result["error"]
                }

            sentinel_result = await self._build_sentinel_header(
                http_session=http_session,
                device_id=device_id
            )
            if not sentinel_result["success"]:
                return {
                    "success": False,
                    "email": email,
                    "error": sentinel_result["error"]
                }

            otp_request_result = await self._request_email_otp(
                http_session=http_session,
                email=email,
                sentinel_header_value=sentinel_result["header_value"]
            )
            if not otp_request_result["success"]:
                return {
                    "success": False,
                    "email": email,
                    "error": otp_request_result["error"]
                }

            otp_result = await self._wait_for_otp_code(email, baseline_mail_date)
            if not otp_result["success"]:
                return {
                    "success": False,
                    "email": email,
                    "error": otp_result["error"]
                }

            validate_result = await self._validate_email_otp(
                http_session=http_session,
                code=otp_result["code"]
            )
            if not validate_result["success"]:
                return {
                    "success": False,
                    "email": email,
                    "error": validate_result["error"]
                }

            login_result = await self._finish_login_and_get_session_token(
                http_session=http_session,
                callback_url=validate_result["continue_url"]
            )
            if not login_result["success"]:
                return {
                    "success": False,
                    "email": email,
                    "error": login_result["error"]
                }

            session_token = login_result["session_token"]

            refresh_result = await self.chatgpt_service.refresh_access_token_with_session_token(
                session_token=session_token,
                db_session=db_session,
                account_id=account_id,
                identifier=email
            )
            if not refresh_result.get("success"):
                return {
                    "success": False,
                    "email": email,
                    "error": refresh_result.get("error") or "使用 Session Token 刷新 Access Token 失败"
                }

            extracted_at = get_now().isoformat()
            access_token = refresh_result["access_token"]
            refreshed_session_token = refresh_result.get("session_token") or session_token

            import_result = await self.team_service.import_team_single(
                access_token=access_token,
                db_session=db_session,
                email=email,
                account_id=account_id,
                session_token=refreshed_session_token
            )

            if not import_result.get("success"):
                return {
                    "success": False,
                    "email": email,
                    "extracted_at": extracted_at,
                    "error": import_result.get("error") or "导入 Team 失败"
                }

            return {
                "success": True,
                "email": email,
                "team_id": import_result.get("team_id"),
                "message": import_result.get("message") or "导入成功",
                "extracted_at": extracted_at
            }

        except Exception as e:
            logger.error(f"邮箱提取并导入失败: {e}", exc_info=True)
            return {
                "success": False,
                "email": email,
                "error": f"处理失败: {str(e)}"
            }
        finally:
            await http_session.close()


# 全局实例
email_import_service = EmailImportService()
