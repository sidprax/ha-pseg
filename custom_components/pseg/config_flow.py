"""Config flow for the PSE&G Energy integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, Dict, Optional

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTH_TYPE_COOKIE,
    AUTH_TYPE_MYMETER,
    AUTH_TYPE_OKTA,
    CONF_AUTH_SERVER_URL,
    CONF_AUTH_TYPE,
    CONF_COOKIE,
    CONF_ELECTRIC_RATE,
    CONF_MFA_CODE,
    CONF_MFA_METHOD,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_AUTH_SERVER_URL,
    DEFAULT_ELECTRIC_RATE,
    DEFAULT_MFA_METHOD,
    DOMAIN,
    MFA_METHOD_EMAIL,
    MFA_METHOD_SMS,
)
from .pseg_client import PsegAuthError, PsegClient, PsegConnectionError
from .util import get_valid_auth_server_url, is_valid_auth_server_url, sanitize_url_for_logging

_LOGGER = logging.getLogger(__name__)


class PsegConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a multi-step guided config flow for PSE&G Energy."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._user_input: Dict[str, Any] = {}
        self._reauth_entry: Optional[config_entries.ConfigEntry] = None

    async def _async_detect_auth_server(self) -> Optional[str]:
        """Probe candidate URLs to detect if the companion auth add-on is running."""
        cached = self.context.get("auth_url")
        if not cached and self._reauth_entry:
            cached = get_valid_auth_server_url(
                self._reauth_entry.options,
                self._reauth_entry.data,
                None,
            )

        candidate_urls = []
        if cached:
            candidate_urls.append(cached)
        for url in [DEFAULT_AUTH_SERVER_URL, "http://localhost:8000"]:
            if url not in candidate_urls:
                candidate_urls.append(url)

        session = async_get_clientsession(self.hass)
        for base_url in candidate_urls:
            if not is_valid_auth_server_url(base_url):
                continue
            try:
                async with session.get(
                    f"{base_url}/health",
                    timeout=aiohttp.ClientTimeout(total=1.5),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == "healthy":
                            self.context["auth_url"] = base_url
                            return base_url
            except Exception:
                continue

        return None

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 1: Choose connection method."""
        working_url = await self._async_detect_auth_server()
        if working_url:
            menu_options = [
                "automated_mymeter",
                "cookie_mymeter",
                "automated_okta",
                "cookie_okta",
            ]
            addon_status = (
                "✨ **Companion Add-on Active**: We recommend **Automated Sign-In: Direct MyMeter** "
                "for hands-off automated background session renewal with zero 2FA prompts.\n\n"
            )
        else:
            menu_options = ["cookie_mymeter", "cookie_okta"]
            addon_status = (
                "ℹ️ **Recommended Method**: For automated background renewal without 2FA, install the "
                "[PSE&G Auth Companion Add-on](https://github.com/sidprax/ha-pseg-auth-addon) to enable **Automated Sign-In**.\n\n"
                "If you prefer not running the companion add-on, you can connect using a manual **Browser Cookie** below (requires periodic re-entry).\n\n"
            )

        return self.async_show_menu(
            step_id="user",
            menu_options=menu_options,
            description_placeholders={"addon_status": addon_status},
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> FlowResult:
        """Handle reauthorization request from Home Assistant."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        working_url = await self._async_detect_auth_server()
        if working_url:
            menu_options = ["reauth_confirm", "cookie_mymeter", "cookie_okta"]
            addon_status = ""
        else:
            menu_options = ["cookie_mymeter", "cookie_okta"]
            addon_status = (
                "⚠️ **PSE&G Auth Companion Add-on Not Detected**\n\n"
                "Automated re-authentication is disabled because the companion add-on is not running. "
                "Update your session using a **Browser Cookie** below, or start the companion add-on.\n\n"
            )

        return self.async_show_menu(
            step_id="reauth",
            menu_options=menu_options,
            description_placeholders={"addon_status": addon_status},
        )

    async def async_step_reconfigure(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle a reconfiguration flow initialized by the user."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        working_url = await self._async_detect_auth_server()
        if working_url:
            menu_options = [
                "automated_mymeter",
                "cookie_mymeter",
                "automated_okta",
                "cookie_okta",
            ]
            addon_status = (
                "✨ **Companion Add-on Active**: We recommend **Automated Sign-In: Direct MyMeter** "
                "for hands-off automated renewal without 2FA or OTP prompts.\n\n"
            )
        else:
            menu_options = ["cookie_mymeter", "cookie_okta"]
            addon_status = (
                "⚠️ **PSE&G Auth Companion Add-on Not Detected**\n\n"
                "For hands-off automated renewal without 2FA, start the companion add-on under Settings > Add-ons. "
                "Alternatively, you can update your connection using a manual **Browser Cookie** below.\n\n"
            )

        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=menu_options,
            description_placeholders={"addon_status": addon_status},
        )

    async def async_step_reauth_confirm(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Confirm reauth credentials and trigger sign-in."""
        errors: Dict[str, str] = {}
        existing_data = self._reauth_entry.data if self._reauth_entry else {}
        existing_options = self._reauth_entry.options if self._reauth_entry else {}
        default_username = existing_data.get(CONF_USERNAME, "")
        default_mfa = existing_data.get(CONF_MFA_METHOD, DEFAULT_MFA_METHOD)

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, default_username).strip()
            password = user_input.get(CONF_PASSWORD, "").strip()
            mfa_method = user_input.get(CONF_MFA_METHOD, default_mfa)
            auth_url = get_valid_auth_server_url(
                existing_options,
                existing_data,
                DEFAULT_AUTH_SERVER_URL,
            )

            if not username or not password:
                errors["base"] = "missing_credentials"
            else:
                candidate_urls = [auth_url]
                if "127.0.0.1" in auth_url and "http://localhost:8000" not in candidate_urls:
                    candidate_urls.append("http://localhost:8000")
                elif "localhost" in auth_url and "http://127.0.0.1:8000" not in candidate_urls:
                    candidate_urls.append("http://127.0.0.1:8000")

                connected = False
                session = async_get_clientsession(self.hass)
                auth_result = None
                for target_url in candidate_urls:
                    if not is_valid_auth_server_url(target_url):
                        continue
                    try:
                        async with session.post(
                            f"{target_url}/login",
                            json={
                                "username": username,
                                "password": password,
                                "mfa_method": mfa_method,
                                "dispatch_mfa": True,
                            },
                            allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=90),
                        ) as resp:
                            connected = True
                            self.context["auth_url"] = target_url
                            self.context["username"] = username
                            self.context["password"] = password
                            if resp.status == 200:
                                res_json = await resp.json()
                                if res_json.get("success") and res_json.get("cookies"):
                                    auth_result = ("success", res_json["cookies"], target_url)
                                    break
                                elif res_json.get("mfa_required"):
                                    auth_result = ("mfa", None, target_url)
                                    break
                                else:
                                    _LOGGER.error("Auth helper failed: %s", res_json.get("error", "authentication failed"))
                                    errors["base"] = "invalid_auth"
                                    break
                            elif 300 <= resp.status < 400:
                                _LOGGER.warning(
                                    "Auth helper at %s returned redirect HTTP %s; companion endpoint must not redirect",
                                    sanitize_url_for_logging(target_url),
                                    resp.status,
                                )
                                errors["base"] = "auth_server_redirect"
                                break
                            else:
                                try:
                                    res_json = await resp.json()
                                except Exception:
                                    res_json = {}
                                if resp.status == 403 or res_json.get("recaptcha_blocked"):
                                    errors["base"] = "recaptcha_blocked"
                                elif resp.status == 500:
                                    _LOGGER.error("Auth helper returned 500: %s", res_json.get("error", "server error"))
                                    errors["base"] = "auth_server_error"
                                else:
                                    errors["base"] = "invalid_auth"
                                break
                    except aiohttp.ClientConnectorError:
                        _LOGGER.debug("Could not reach auth server at %s", sanitize_url_for_logging(target_url))
                        continue
                    except Exception as err:
                        _LOGGER.exception("Reauth attempt to %s failed: %s", sanitize_url_for_logging(target_url), err)
                        errors["base"] = "unknown"
                        break

                if auth_result:
                    action, cookies, target_url = auth_result
                    if action == "success":
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data={
                                **self._reauth_entry.data,
                                CONF_COOKIE: cookies,
                                CONF_USERNAME: username,
                                CONF_PASSWORD: password,
                                CONF_AUTH_SERVER_URL: target_url,
                            },
                        )
                    elif action == "mfa":
                        return await self.async_step_mfa()

                if not connected and not errors:
                    errors["base"] = "auth_server_unavailable"

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=default_username): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_MFA_METHOD, default=default_mfa): vol.In(
                    [MFA_METHOD_SMS, MFA_METHOD_EMAIL]
                ),
            }
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"username": default_username},
        )

    async def async_step_automated_mymeter(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Automated direct sign-in via MyMeter invited account (zero 2FA)."""
        errors: Dict[str, str] = {}
        auth_url = self.context.get("auth_url") or DEFAULT_AUTH_SERVER_URL
        if not is_valid_auth_server_url(auth_url):
            auth_url = DEFAULT_AUTH_SERVER_URL

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "").strip()

            if not username or not password:
                errors["base"] = "missing_credentials"
            else:
                candidate_urls = [auth_url]
                if "127.0.0.1" in auth_url and "http://localhost:8000" not in candidate_urls:
                    candidate_urls.append("http://localhost:8000")
                elif "localhost" in auth_url and "http://127.0.0.1:8000" not in candidate_urls:
                    candidate_urls.append("http://127.0.0.1:8000")

                connected = False
                session = async_get_clientsession(self.hass)
                cookies = None
                for target_url in candidate_urls:
                    if not is_valid_auth_server_url(target_url):
                        continue
                    try:
                        async with session.post(
                            f"{target_url}/login",
                            json={
                                "username": username,
                                "password": password,
                                "auth_type": AUTH_TYPE_MYMETER,
                                "dispatch_mfa": False,
                            },
                            allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=90),
                        ) as resp:
                            connected = True
                            self.context["auth_url"] = target_url
                            self.context["username"] = username
                            self.context["password"] = password
                            if resp.status == 200:
                                res_json = await resp.json()
                                if res_json.get("success") and res_json.get("cookies"):
                                    cookies = res_json["cookies"]
                                    auth_url = target_url
                                    break
                                else:
                                    errors["base"] = "invalid_auth"
                                    break
                            elif 300 <= resp.status < 400:
                                _LOGGER.warning(
                                    "Auth helper at %s returned redirect HTTP %s; companion endpoint must not redirect",
                                    sanitize_url_for_logging(target_url),
                                    resp.status,
                                )
                                errors["base"] = "auth_server_redirect"
                                break
                            else:
                                try:
                                    res_json = await resp.json()
                                except Exception:
                                    res_json = {}
                                if resp.status == 403 or res_json.get("recaptcha_blocked"):
                                    errors["base"] = "recaptcha_blocked"
                                elif resp.status == 500:
                                    errors["base"] = "auth_server_error"
                                else:
                                    errors["base"] = "invalid_auth"
                                break
                    except aiohttp.ClientConnectorError:
                        _LOGGER.debug("Could not reach auth server at %s", sanitize_url_for_logging(target_url))
                        continue
                    except Exception as err:
                        _LOGGER.exception("MyMeter login error via %s: %s", sanitize_url_for_logging(target_url), err)
                        errors["base"] = "unknown"
                        break

                if cookies:
                    if getattr(self, "_reauth_entry", None):
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data={
                                **self._reauth_entry.data,
                                CONF_COOKIE: cookies,
                                CONF_USERNAME: username,
                                CONF_PASSWORD: password,
                                CONF_AUTH_SERVER_URL: auth_url,
                                CONF_AUTH_TYPE: AUTH_TYPE_MYMETER,
                            },
                        )
                    await self.async_set_unique_id(username.lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"PSE&G ({username})",
                        data={
                            CONF_COOKIE: cookies,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_AUTH_SERVER_URL: auth_url,
                            CONF_AUTH_TYPE: AUTH_TYPE_MYMETER,
                        },
                    )

                if not connected and not errors:
                    errors["base"] = "auth_server_unavailable"

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="automated_mymeter",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_automated_okta(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 2a: Automated sign-in via primary PSE&G Okta SSO."""
        errors: Dict[str, str] = {}
        auth_url = self.context.get("auth_url") or DEFAULT_AUTH_SERVER_URL
        if not is_valid_auth_server_url(auth_url):
            auth_url = DEFAULT_AUTH_SERVER_URL

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            password = user_input.get(CONF_PASSWORD, "").strip()
            mfa_method = user_input.get(CONF_MFA_METHOD, DEFAULT_MFA_METHOD)

            if not username or not password:
                errors["base"] = "missing_credentials"
            else:
                candidate_urls = [auth_url]
                if "127.0.0.1" in auth_url and "http://localhost:8000" not in candidate_urls:
                    candidate_urls.append("http://localhost:8000")
                elif "localhost" in auth_url and "http://127.0.0.1:8000" not in candidate_urls:
                    candidate_urls.append("http://127.0.0.1:8000")

                connected = False
                session = async_get_clientsession(self.hass)
                auth_result = None
                for target_url in candidate_urls:
                    if not is_valid_auth_server_url(target_url):
                        continue
                    try:
                        async with session.post(
                            f"{target_url}/login",
                            json={
                                "username": username,
                                "password": password,
                                "auth_type": AUTH_TYPE_OKTA,
                                "mfa_method": mfa_method,
                                "dispatch_mfa": True,
                            },
                            allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=90),
                        ) as resp:
                            connected = True
                            self.context["auth_url"] = target_url
                            self.context["username"] = username
                            self.context["password"] = password
                            self.context["auth_type"] = AUTH_TYPE_OKTA
                            if resp.status == 200:
                                res_json = await resp.json()
                                if res_json.get("success") and res_json.get("cookies"):
                                    auth_result = ("success", res_json["cookies"], target_url)
                                    break
                                elif res_json.get("mfa_required"):
                                    auth_result = ("mfa", None, target_url)
                                    break
                                else:
                                    _LOGGER.error("Auth helper failed: %s", res_json.get("error", "invalid authentication"))
                                    errors["base"] = "invalid_auth"
                                    break
                            elif 300 <= resp.status < 400:
                                _LOGGER.warning(
                                    "Auth helper at %s returned redirect HTTP %s; companion endpoint must not redirect",
                                    sanitize_url_for_logging(target_url),
                                    resp.status,
                                )
                                errors["base"] = "auth_server_redirect"
                                break
                            else:
                                try:
                                    res_json = await resp.json()
                                except Exception:
                                    res_json = {}
                                if resp.status == 403 or res_json.get("recaptcha_blocked"):
                                    errors["base"] = "recaptcha_blocked"
                                elif resp.status == 500:
                                    _LOGGER.error("Auth helper returned 500: %s", res_json.get("error", "server error"))
                                    errors["base"] = "auth_server_error"
                                else:
                                    _LOGGER.error("Auth helper returned HTTP %s", resp.status)
                                    errors["base"] = "invalid_auth"
                                break
                    except aiohttp.ClientConnectorError:
                        _LOGGER.debug("Could not reach auth server at %s", sanitize_url_for_logging(target_url))
                        continue
                    except Exception as err:
                        _LOGGER.exception("Error calling auth server at %s: %s", sanitize_url_for_logging(target_url), err)
                        errors["base"] = "unknown"
                        break

                if auth_result:
                    action, cookies, target_url = auth_result
                    if action == "success":
                        if getattr(self, "_reauth_entry", None):
                            return self.async_update_reload_and_abort(
                                self._reauth_entry,
                                data={
                                    **self._reauth_entry.data,
                                    CONF_COOKIE: cookies,
                                    CONF_USERNAME: username,
                                    CONF_PASSWORD: password,
                                    CONF_AUTH_SERVER_URL: target_url,
                                    CONF_AUTH_TYPE: AUTH_TYPE_OKTA,
                                },
                            )
                        await self.async_set_unique_id(username.lower())
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=f"PSE&G ({username})",
                            data={
                                CONF_COOKIE: cookies,
                                CONF_USERNAME: username,
                                CONF_PASSWORD: password,
                                CONF_AUTH_SERVER_URL: target_url,
                                CONF_AUTH_TYPE: AUTH_TYPE_OKTA,
                            },
                        )
                    elif action == "mfa":
                        return await self.async_step_mfa()

                if not connected and not errors:
                    errors["base"] = "auth_server_unavailable"

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_MFA_METHOD, default=DEFAULT_MFA_METHOD): vol.In(
                    [MFA_METHOD_SMS, MFA_METHOD_EMAIL]
                ),
            }
        )

        return self.async_show_form(
            step_id="automated_okta",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_automated(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Alias for backwards compatibility."""
        return await self.async_step_automated_mymeter(user_input)

    async def async_step_mfa(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Step 3: Verification of 6-digit OTP code."""
        errors: Dict[str, str] = {}
        auth_url = self.context.get("auth_url", DEFAULT_AUTH_SERVER_URL)
        if not is_valid_auth_server_url(auth_url):
            auth_url = DEFAULT_AUTH_SERVER_URL
        username = self.context.get("username", "")
        password = self.context.get("password", "")

        if user_input is not None:
            if user_input.get("restart"):
                if getattr(self, "_reauth_entry", None):
                    return await self.async_step_reauth_confirm()
                return await self.async_step_user()

            code = user_input.get(CONF_MFA_CODE, "").strip()
            if not code:
                errors["base"] = "invalid_code"
            else:
                mfa_success = False
                cookies = None
                session = async_get_clientsession(self.hass)
                try:
                    async with session.post(
                        f"{auth_url}/login/mfa",
                        json={"code": code},
                        allow_redirects=False,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        if resp.status == 200:
                            res_json = await resp.json()
                            if res_json.get("success") and res_json.get("cookies"):
                                cookies = res_json["cookies"]
                                mfa_success = True
                            else:
                                errors["base"] = "invalid_code"
                        elif resp.status == 400:
                            _LOGGER.warning("Auth session expired on helper service, restarting sign-in")
                            if getattr(self, "_reauth_entry", None):
                                return await self.async_step_reauth_confirm()
                            return await self.async_step_user()
                        elif 300 <= resp.status < 400:
                            _LOGGER.warning(
                                "MFA endpoint %s returned redirect HTTP %s; companion endpoint must not redirect",
                                sanitize_url_for_logging(auth_url),
                                resp.status,
                            )
                            errors["base"] = "auth_server_redirect"
                        else:
                            errors["base"] = "invalid_code"
                except Exception as err:
                    _LOGGER.exception("MFA verification error: %s", err)
                    errors["base"] = "unknown"

                if mfa_success and cookies:
                    if getattr(self, "_reauth_entry", None):
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data={
                                **self._reauth_entry.data,
                                CONF_COOKIE: cookies,
                                CONF_USERNAME: username or self._reauth_entry.data.get(CONF_USERNAME),
                                CONF_PASSWORD: password or self._reauth_entry.data.get(CONF_PASSWORD),
                                CONF_AUTH_SERVER_URL: auth_url,
                            },
                        )
                    if username:
                        await self.async_set_unique_id(username.lower())
                        self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"PSE&G ({username})" if username else "PSE&G Energy",
                        data={
                            CONF_COOKIE: cookies,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_AUTH_SERVER_URL: auth_url,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Optional(CONF_MFA_CODE, default=""): str,
                vol.Optional("restart", default=False): bool,
            }
        )

        return self.async_show_form(
            step_id="mfa",
            data_schema=schema,
            errors=errors,
        )

    async def _async_validate_and_create_cookie_entry(
        self, cookie: str, step_id: str, auth_type: str = AUTH_TYPE_COOKIE
    ) -> FlowResult:
        """Helper to validate cookie and create or update the config entry."""
        cookie_valid = False
        try:
            client = PsegClient(cookie)
            await self.hass.async_add_executor_job(client.test_connection)
            cookie_valid = True
        except PsegAuthError as err:
            _LOGGER.error("Cookie validation error: %s", err)
            return self.async_show_form(
                step_id=step_id,
                data_schema=vol.Schema({vol.Required(CONF_COOKIE): str}),
                errors={"base": "invalid_cookie"},
            )
        except PsegConnectionError as err:
            _LOGGER.error("Connection error validating cookie: %s", err)
            return self.async_show_form(
                step_id=step_id,
                data_schema=vol.Schema({vol.Required(CONF_COOKIE): str}),
                errors={"base": "cannot_connect"},
            )
        except Exception as err:
            _LOGGER.exception("Unexpected error validating cookie: %s", err)
            return self.async_show_form(
                step_id=step_id,
                data_schema=vol.Schema({vol.Required(CONF_COOKIE): str}),
                errors={"base": "unknown"},
            )

        if cookie_valid:
            if getattr(self, "_reauth_entry", None):
                return self.async_update_reload_and_abort(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_COOKIE: cookie,
                        CONF_AUTH_TYPE: auth_type,
                    },
                )
            return self.async_create_entry(
                title="PSE&G Energy",
                data={
                    CONF_COOKIE: cookie,
                    CONF_AUTH_TYPE: auth_type,
                },
            )

    async def async_step_cookie_mymeter(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Direct persistent browser cookie from MyMeter invited account."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            cookie = user_input.get(CONF_COOKIE, "").strip()
            if not cookie:
                errors["base"] = "missing_credentials"
            else:
                return await self._async_validate_and_create_cookie_entry(
                    cookie, step_id="cookie_mymeter", auth_type=AUTH_TYPE_COOKIE
                )

        schema = vol.Schema({vol.Required(CONF_COOKIE): str})
        return self.async_show_form(
            step_id="cookie_mymeter",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_cookie_okta(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Session browser cookie from primary PSE&G account."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            cookie = user_input.get(CONF_COOKIE, "").strip()
            if not cookie:
                errors["base"] = "missing_credentials"
            else:
                return await self._async_validate_and_create_cookie_entry(
                    cookie, step_id="cookie_okta", auth_type=AUTH_TYPE_COOKIE
                )

        schema = vol.Schema({vol.Required(CONF_COOKIE): str})
        return self.async_show_form(
            step_id="cookie_okta",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_cookie(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Alias for backwards compatibility."""
        return await self.async_step_cookie_mymeter(user_input)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get the options flow for this handler."""
        return PsegOptionsFlowHandler()


class PsegOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for PSE&G Energy."""

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Manage the PSE&G options."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            # If user left rate empty or entered <= 0, normalize to None to enable 7-day average
            rate = user_input.get(CONF_ELECTRIC_RATE)
            if rate is not None:
                try:
                    if float(rate) <= 0:
                        user_input[CONF_ELECTRIC_RATE] = None
                except (ValueError, TypeError):
                    user_input[CONF_ELECTRIC_RATE] = None

            # Normalize auth_server_url (allow empty for cookie-only users)
            auth_url = user_input.get(CONF_AUTH_SERVER_URL)
            if auth_url:
                cleaned_url = auth_url.strip()
                if not is_valid_auth_server_url(cleaned_url):
                    errors["base"] = "invalid_auth_server_url"
                else:
                    user_input[CONF_AUTH_SERVER_URL] = cleaned_url
            else:
                user_input[CONF_AUTH_SERVER_URL] = ""

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        current_rate = self.config_entry.options.get(
            CONF_ELECTRIC_RATE,
            self.config_entry.data.get(CONF_ELECTRIC_RATE),
        )
        current_auth_url = self.config_entry.options.get(
            CONF_AUTH_SERVER_URL,
            self.config_entry.data.get(CONF_AUTH_SERVER_URL, ""),
        )

        schema_dict: Dict[Any, Any] = {}
        if current_rate is not None:
            try:
                rate_val = float(current_rate)
                if rate_val > 0:
                    schema_dict[vol.Optional(CONF_ELECTRIC_RATE, default=rate_val)] = vol.Coerce(float)
                else:
                    schema_dict[vol.Optional(CONF_ELECTRIC_RATE)] = vol.Coerce(float)
            except (ValueError, TypeError):
                schema_dict[vol.Optional(CONF_ELECTRIC_RATE)] = vol.Coerce(float)
        else:
            schema_dict[vol.Optional(CONF_ELECTRIC_RATE)] = vol.Coerce(float)

        auth_url_val = str(current_auth_url) if current_auth_url else ""
        schema_dict[vol.Optional(CONF_AUTH_SERVER_URL, default=auth_url_val)] = str

        options_schema = vol.Schema(schema_dict)

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            errors=errors,
        )
