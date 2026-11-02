from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.dependencies import require_admin, require_user
from app.models import AuditLog, IntegrationAccount, OAuthState, User
from app.services.integrations.vault import (
    VaultUnavailable,
    decrypt_credentials,
    encrypt_credentials,
)

router = APIRouter()
GITHUB_API = "https://api.github.com"
GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
LINKEDIN_AUTHORIZE = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO = "https://api.linkedin.com/v2/userinfo"


class GitHubCredential(BaseModel):
    token: str = Field(min_length=10, max_length=500)


def _audit(request: Request, provider: str, action: str, result: str, metadata: dict[str, object]) -> None:
    with request.app.state.db_factory() as db:
        db.add(AuditLog(
            id=str(uuid4()),
            event_type=f"integration.{provider}",
            source="backend",
            action=action,
            result=result,
            metadata_json=metadata,
        ))
        db.commit()


def _account(request: Request, provider: str) -> IntegrationAccount | None:
    with request.app.state.db_factory() as db:
        row = db.query(IntegrationAccount).filter(IntegrationAccount.provider == provider).one_or_none()
        if row is None:
            return None
        return IntegrationAccount(
            id=row.id,
            provider=row.provider,
            encrypted_credentials=row.encrypted_credentials,
            scopes=row.scopes,
            created_at=row.created_at,
        )


def _save_account(request: Request, provider: str, credentials: dict[str, object], scopes: list[str]) -> None:
    try:
        encrypted = encrypt_credentials(credentials, request.app.state.settings)
    except VaultUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    with request.app.state.db_factory() as db:
        account = db.query(IntegrationAccount).filter(IntegrationAccount.provider == provider).one_or_none()
        if account is None:
            account = IntegrationAccount(provider=provider, encrypted_credentials=encrypted, scopes=scopes)
            db.add(account)
        else:
            account.encrypted_credentials = encrypted
            account.scopes = scopes
        db.commit()


def _get_credentials(request: Request, provider: str) -> dict[str, object] | None:
    account = _account(request, provider)
    if account is None:
        return None
    try:
        return decrypt_credentials(account.encrypted_credentials, request.app.state.settings)
    except VaultUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


async def _github_request(request: Request, path: str) -> Any:
    token = request.app.state.settings.github_token
    if not token:
        credentials = _get_credentials(request, "github")
        token = str(credentials.get("token", "")) if credentials else ""
    if not token:
        raise HTTPException(503, "configure a GitHub PAT in the integration settings")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            response = await client.get(
                f"{GITHUB_API}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "Distributed-Edge-AI-Assistant",
                },
            )
        response.raise_for_status()
        _audit(request, "github", "GitHub API request", "success", {"path": path, "remaining": response.headers.get("X-RateLimit-Remaining")})
        return response.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        detail = "GitHub authorization failed; check PAT scopes" if status in (401, 403) else f"GitHub API returned HTTP {status}"
        _audit(request, "github", "GitHub API request", "failure", {"path": path, "http_status": status})
        raise HTTPException(502 if status >= 500 else status, detail) from exc
    except httpx.TransportError as exc:
        _audit(request, "github", "GitHub API request", "failure", {"path": path, "error": "transport"})
        raise HTTPException(502, "GitHub API connection failed") from exc


@router.put("/integrations/github/credentials")
def save_github_credentials(
    body: GitHubCredential,
    request: Request,
    user: User = Depends(require_admin),
) -> dict[str, str]:
    _save_account(request, "github", {"token": body.token}, ["PAT-provided"])
    _audit(request, "github", "GitHub credential stored", "success", {"user_id": user.id})
    return {"provider": "github", "credential": "encrypted"}


@router.get("/integrations/github/repositories")
async def github_repositories(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    data = await _github_request(request, "/user/repos?per_page=50&sort=updated")
    return {"repositories": [{
        "name": item["full_name"],
        "private": item["private"],
        "stars": item["stargazers_count"],
        "forks": item["forks_count"],
        "updated_at": item["updated_at"],
        "url": item["html_url"],
    } for item in data]}


@router.get("/integrations/github/notifications")
async def github_notifications(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    data = await _github_request(request, "/notifications?per_page=50")
    return {"notifications": [{
        "id": item["id"],
        "reason": item["reason"],
        "subject": item["subject"]["title"],
        "type": item["subject"]["type"],
        "repository": item["repository"]["full_name"],
        "updated_at": item["updated_at"],
    } for item in data]}


@router.get("/integrations/github/repositories/{owner}/{repo}/activity")
async def github_activity(owner: str, repo: str, request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    if not owner.replace("-", "").replace("_", "").isalnum() or not repo.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(422, "invalid repository name")
    prefix = f"/repos/{owner}/{repo}"
    commits, pulls, issues = await asyncio.gather(
        _github_request(request, f"{prefix}/commits?per_page=10"),
        _github_request(request, f"{prefix}/pulls?state=all&per_page=10"),
        _github_request(request, f"{prefix}/issues?state=all&per_page=10"),
    )
    return {
        "commits": [{"sha": item["sha"][:12], "message": item["commit"]["message"].splitlines()[0], "url": item["html_url"]} for item in commits],
        "pull_requests": [{"number": item["number"], "title": item["title"], "state": item["state"], "url": item["html_url"]} for item in pulls],
        "issues": [{"number": item["number"], "title": item["title"], "state": item["state"], "url": item["html_url"]} for item in issues if "pull_request" not in item],
    }


def _oauth_config(request: Request, provider: str) -> tuple[str, str, str]:
    settings = request.app.state.settings
    if provider == "gmail":
        client_id, client_secret, redirect_uri = settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_redirect_uri
    else:
        client_id, client_secret, redirect_uri = (
            settings.linkedin_client_id,
            settings.linkedin_client_secret,
            settings.linkedin_redirect_uri,
        )
    if not client_id or not client_secret or not redirect_uri:
        raise HTTPException(503, f"configure the {provider} OAuth client id, secret, and redirect URI")
    return client_id, client_secret, redirect_uri


def _start_oauth(request: Request, provider: str, user: User, authorization_url: str, scopes: list[str], client_id: str, redirect_uri: str) -> str:
    state = secrets.token_urlsafe(32)
    with request.app.state.db_factory() as db:
        db.add(OAuthState(
            state=state,
            user_id=user.id,
            provider=provider,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        ))
        db.commit()
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if provider == "gmail":
        query.update({"access_type": "offline", "prompt": "consent"})
    return authorization_url + "?" + urlencode(query)


@router.get("/integrations/gmail/authorize")
def gmail_authorize(request: Request, user: User = Depends(require_admin)) -> dict[str, str]:
    client_id, _, redirect_uri = _oauth_config(request, "gmail")
    url = _start_oauth(
        request, "gmail", user, GOOGLE_AUTHORIZE,
        ["https://www.googleapis.com/auth/gmail.readonly", "openid", "email"],
        client_id, redirect_uri,
    )
    return {"authorization_url": url}


@router.get("/integrations/gmail/callback")
async def gmail_callback(
    code: str,
    state: str,
    request: Request,
    error: str | None = None,
) -> dict[str, str]:
    if error:
        raise HTTPException(400, "Google OAuth authorization was denied")
    client_id, client_secret, redirect_uri = _oauth_config(request, "gmail")
    with request.app.state.db_factory() as db:
        oauth_state = db.get(OAuthState, state)
        if oauth_state is None or oauth_state.provider != "gmail" or _as_utc(oauth_state.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(400, "OAuth state is invalid or expired")
        db.delete(oauth_state)
        db.commit()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(GOOGLE_TOKEN, data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            })
        response.raise_for_status()
        tokens = response.json()
    except httpx.HTTPError as exc:
        _audit(request, "gmail", "OAuth token exchange", "failure", {"error": "token_exchange"})
        raise HTTPException(502, "Google OAuth token exchange failed") from exc
    tokens["created_at"] = datetime.now(timezone.utc).timestamp()
    _save_account(request, "gmail", tokens, ["gmail.readonly", "openid", "email"])
    _audit(request, "gmail", "OAuth connected", "success", {"user_id": oauth_state.user_id})
    return {"provider": "gmail", "connected": "true"}


async def _gmail_access_token(request: Request) -> str:
    credentials = _get_credentials(request, "gmail")
    if not credentials:
        raise HTTPException(409, "connect a Gmail account with OAuth first")
    expires_at = float(credentials.get("created_at", 0)) + int(credentials.get("expires_in", 0))
    access_token = str(credentials.get("access_token", ""))
    if expires_at <= datetime.now(timezone.utc).timestamp() + 60:
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            raise HTTPException(401, "Gmail authorization expired; reconnect the account")
        client_id, client_secret, _ = _oauth_config(request, "gmail")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(GOOGLE_TOKEN, data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                })
            response.raise_for_status()
            refreshed = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(502, "Gmail token refresh failed; reconnect if access was revoked") from exc
        credentials.update(refreshed)
        credentials["refresh_token"] = refresh_token
        credentials["created_at"] = datetime.now(timezone.utc).timestamp()
        _save_account(request, "gmail", credentials, ["gmail.readonly", "openid", "email"])
        access_token = str(credentials.get("access_token", ""))
    return access_token


@router.get("/integrations/gmail/unread")
async def gmail_unread(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    token = await _gmail_access_token(request)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            listing = await client.get(f"{GMAIL_API}/users/me/messages", headers=headers, params={"q": "is:unread", "maxResults": 25})
            listing.raise_for_status()
            messages = listing.json().get("messages", [])
            result = []
            for entry in messages[:25]:
                response = await client.get(
                    f"{GMAIL_API}/users/me/messages/{entry['id']}",
                    headers=headers,
                    params={"format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]},
                )
                response.raise_for_status()
                message = response.json()
                message_headers = {item["name"].lower(): item["value"] for item in message.get("payload", {}).get("headers", [])}
                subject = message_headers.get("subject", "(no subject)")
                result.append({
                    "id": message["id"],
                    "sender": message_headers.get("from", ""),
                    "subject": subject,
                    "date": message_headers.get("date", ""),
                    "urgent": any(term in subject.casefold() for term in ("urgent", "critical", "action required", "emergency")),
                    "snippet": message.get("snippet", ""),
                })
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            raise HTTPException(401, "Gmail token was rejected; reconnect the account") from exc
        raise HTTPException(502, f"Gmail API returned HTTP {exc.response.status_code}") from exc
    except httpx.TransportError as exc:
        raise HTTPException(502, "Gmail API connection failed") from exc
    _audit(request, "gmail", "Unread email summary", "success", {"count": len(result)})
    return {"messages": result, "count": len(result)}


@router.get("/integrations/linkedin/authorize")
def linkedin_authorize(request: Request, user: User = Depends(require_admin)) -> dict[str, str]:
    client_id, _, redirect_uri = _oauth_config(request, "linkedin")
    url = _start_oauth(request, "linkedin", user, LINKEDIN_AUTHORIZE, ["openid", "profile"], client_id, redirect_uri)
    return {"authorization_url": url}


@router.get("/integrations/linkedin/callback")
async def linkedin_callback(code: str, state: str, request: Request, error: str | None = None) -> dict[str, str]:
    if error:
        raise HTTPException(400, "LinkedIn OAuth authorization was denied")
    client_id, client_secret, redirect_uri = _oauth_config(request, "linkedin")
    with request.app.state.db_factory() as db:
        oauth_state = db.get(OAuthState, state)
        if oauth_state is None or oauth_state.provider != "linkedin" or _as_utc(oauth_state.expires_at) <= datetime.now(timezone.utc):
            raise HTTPException(400, "OAuth state is invalid or expired")
        db.delete(oauth_state)
        db.commit()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(LINKEDIN_TOKEN, data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
            })
        response.raise_for_status()
        tokens = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "LinkedIn OAuth token exchange failed") from exc
    _save_account(request, "linkedin", tokens, ["openid", "profile"])
    _audit(request, "linkedin", "OAuth connected", "success", {"user_id": oauth_state.user_id})
    return {"provider": "linkedin", "connected": "true"}


@router.get("/integrations/linkedin/capabilities")
def linkedin_capabilities(_: User = Depends(require_user)) -> dict[str, object]:
    return {
        "supported": ["authorized OpenID Connect member profile, when granted"],
        "unsupported": [
            "private messages", "profile views", "private account analytics",
            "connection scraping", "automated account modifications",
        ],
        "scraping": False,
    }


@router.get("/integrations/linkedin/profile")
async def linkedin_profile(request: Request, _: User = Depends(require_user)) -> dict[str, object]:
    credentials = _get_credentials(request, "linkedin")
    if not credentials:
        raise HTTPException(409, "connect LinkedIn OAuth first")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(LINKEDIN_USERINFO, headers={"Authorization": f"Bearer {credentials['access_token']}"})
        response.raise_for_status()
        profile = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"LinkedIn API returned HTTP {exc.response.status_code}; check granted OpenID permissions") from exc
    except httpx.TransportError as exc:
        raise HTTPException(502, "LinkedIn API connection failed") from exc
    _audit(request, "linkedin", "Authorized profile read", "success", {"fields": sorted(profile.keys())})
    return profile


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
