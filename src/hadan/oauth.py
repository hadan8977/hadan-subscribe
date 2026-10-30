"""OAuth 2.1 authorization server for the Quant MCP connector.

ChatGPT's remote-MCP client is the OAuth client; Quant is both the resource
server and the authorization server.  Tokens are opaque and only their
SHA-256 digests are stored.  An access token is always bound to one of the
single active agent bindings, so deleting that binding in the web app
immediately invalidates the connector.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .db import audit, uid
from .security import digest, token

ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
CODE_TTL = 300
AUTHORIZATION_TTL = 600
SCOPES = ("quant.read", "quant.ack", "quant.checkout", "offline_access")
_PKCE_CHARS = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        super().__init__(description or error)
        self.error = error
        self.description = description
        self.status = status


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pkce_matches(challenge: str, verifier: str) -> bool:
    if not _PKCE_CHARS.fullmatch(verifier or ""):
        return False
    return hmac.compare_digest(challenge, b64url(hashlib.sha256(verifier.encode()).digest()))


def _append_query(uri: str, values: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend((key, value) for key, value in values.items() if value != "")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


class OAuth:
    def __init__(self, service):
        self.s = service
        self.cfg = service.settings
        self.base = self.cfg.base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    def protected_resource(self, resource: str | None = None):
        return {
            "resource": (resource or (self.base + "/mcp")).rstrip("/"),
            "authorization_servers": [self.base],
            "scopes_supported": list(SCOPES),
            "bearer_methods_supported": ["header"],
        }

    def metadata(self):
        return {
            "issuer": self.base,
            "authorization_endpoint": self.base + "/oauth/authorize",
            "token_endpoint": self.base + "/oauth/token",
            "registration_endpoint": self.base + "/oauth/register",
            "revocation_endpoint": self.base + "/oauth/revoke",
            "scopes_supported": list(SCOPES),
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "authorization_response_iss_parameter_supported": True,
        }

    # ------------------------------------------------------------------
    # Dynamic client registration
    # ------------------------------------------------------------------
    _KNOWN_CALLBACKS = {
        # Grok web custom connector.
        "https://grok.com/connectors-oauth-exchange-code",
        "https://grok.com/connectors-oauth-exchange-code/",
        # Cursor web/cloud Agents and Grok Bot's Cursor-hosted flow.
        "https://www.cursor.com/agents/mcp/oauth/callback",
        "https://www.cursor.com/bot/mcp/oauth/callback",
        "cursor://anysphere.cursor-mcp/oauth/callback",
        # Claude's remote MCP callback.
        "https://claude.ai/api/mcp/auth_callback",
    }

    def _redirect_allowed(self, uri: str) -> bool:
        if not isinstance(uri, str) or len(uri) > 2048 or "\r" in uri or "\n" in uri:
            return False
        parts = urlsplit(uri)
        if parts.fragment or parts.username or parts.password:
            return False
        if uri in self._KNOWN_CALLBACKS:
            return True
        if parts.scheme == "https" and parts.hostname in ("chatgpt.com", "chat.openai.com"):
            try:
                if parts.port not in (None, 443) or parts.query:
                    return False
            except ValueError:
                return False
            path = parts.path
            if path == "/connector_platform_oauth_redirect":
                return True
            if re.fullmatch(r"/connector/oauth/[A-Za-z0-9._-]+/?", path):
                return True
            if re.fullmatch(r"/aip/[A-Za-z0-9._-]+/oauth/callback/?", path):
                return True
            return False
        # Native clients use an exact loopback callback. There is no client
        # secret; the owner still has to approve the authorization request.
        if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
            return parts.path == "/callback"
        return False

    def register(self, data: dict, ip: str) -> dict:
        if not isinstance(data, dict):
            raise OAuthError("invalid_client_metadata", "JSON object required")
        redirects = data.get("redirect_uris")
        if (
            not isinstance(redirects, list)
            or not 1 <= len(redirects) <= 10
            or any(not self._redirect_allowed(uri) for uri in redirects)
        ):
            raise OAuthError(
                "invalid_redirect_uri",
                "A supported HTTPS, loopback, or native callback URI is required",
            )
        method = data.get("token_endpoint_auth_method", "none")
        if method not in ("none", ""):
            raise OAuthError(
                "invalid_client_metadata",
                "Only public PKCE clients are supported",
            )
        grants = data.get("grant_types") or ["authorization_code", "refresh_token"]
        if (
            not isinstance(grants, list)
            or any(not isinstance(item, str) for item in grants)
            or set(grants) - {"authorization_code", "refresh_token"}
        ):
            raise OAuthError("invalid_client_metadata", "Unsupported grant type")
        responses = data.get("response_types") or ["code"]
        if (
            not isinstance(responses, list)
            or any(not isinstance(item, str) for item in responses)
            or set(responses) - {"code"}
        ):
            raise OAuthError("invalid_client_metadata", "Unsupported response type")
        name = " ".join(str(data.get("client_name") or "ChatGPT").split())[:120]
        if not name:
            name = "ChatGPT"
        requested_scope = [item for item in str(data.get("scope") or "").split() if item]
        unknown_scope = [
            item for item in requested_scope if item not in SCOPES and item not in {"openid", "profile", "email"}
        ]
        if unknown_scope:
            raise OAuthError("invalid_scope", "Unsupported scope: " + unknown_scope[0])
        scope_values = [item for item in requested_scope if item in SCOPES]
        scope = " ".join(dict.fromkeys(scope_values or list(SCOPES)))
        cid = "qc_" + token()
        now = time.time()
        with self.s.db.transaction() as db:
            self.s._limit(db, "oauth-register:" + digest(ip or "unknown"), 40, 3600)
            db.execute(
                "INSERT INTO oauth_clients(client_id,client_name,redirect_uris,token_endpoint_auth_method,created_at,last_seen) VALUES(?,?,?,?,?,?)",
                (cid, name, json.dumps(redirects, ensure_ascii=False), "none", now, now),
            )
        return {
            "client_id": cid,
            "client_name": name,
            "redirect_uris": redirects,
            "token_endpoint_auth_method": "none",
            "grant_types": grants,
            "response_types": responses,
            "scope": scope,
            "client_id_issued_at": int(now),
        }

    def client(self, client_id: str):
        if not isinstance(client_id, str) or not client_id:
            raise OAuthError("invalid_client", "client_id is required", 401)
        row = self.s.db.one(
            "SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)
        )
        if not row:
            raise OAuthError("invalid_client", "Unknown client", 401)
        return row

    # ------------------------------------------------------------------
    # Authorization request and consent
    # ------------------------------------------------------------------
    def _resource(self, value: str | None) -> str:
        value = (value or "").strip().rstrip("/")
        if not value:
            return self.base
        if value in (self.base, self.base + "/mcp"):
            return self.base
        raise OAuthError("invalid_target", "Unknown resource")

    def _scope(self, value: str | None) -> str:
        requested = [x for x in (value or "").replace(",", " ").split() if x]
        # ChatGPT may include OIDC discovery scopes even though this server is
        # OAuth-only. Ignore them rather than granting a broader scope.
        unknown = [
            x for x in requested if x not in SCOPES and x not in {"openid", "profile", "email"}
        ]
        if unknown:
            raise OAuthError("invalid_scope", "Unsupported scope: " + unknown[0])
        granted = [x for x in requested if x in SCOPES]
        if not granted:
            granted = list(SCOPES)
        return " ".join(dict.fromkeys(granted))

    def start(self, params, ip: str) -> str:
        if params.get("response_type") != "code":
            raise OAuthError("unsupported_response_type", "response_type=code required")
        client = self.client(params.get("client_id", ""))
        redirect_uri = params.get("redirect_uri", "")
        redirects = json.loads(client["redirect_uris"])
        if not isinstance(redirect_uri, str) or redirect_uri not in redirects:
            raise OAuthError("invalid_request", "redirect_uri is not registered")
        challenge = params.get("code_challenge", "")
        if not _PKCE_CHARS.fullmatch(challenge or ""):
            raise OAuthError("invalid_request", "A valid PKCE code_challenge is required")
        if params.get("code_challenge_method") != "S256":
            raise OAuthError("invalid_request", "PKCE method must be S256")
        state = params.get("state", "") or ""
        if len(state) > 2048:
            raise OAuthError("invalid_request", "state is too long")
        resource = self._resource(params.get("resource"))
        scope = self._scope(params.get("scope"))
        rid = uid()
        now = time.time()
        with self.s.db.transaction() as db:
            self.s._limit(db, "oauth-authorize:" + digest(ip or "unknown"), 120, 3600)
            db.execute(
                "INSERT INTO oauth_authorizations(id,client_id,redirect_uri,code_challenge,scope,resource,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, client["client_id"], redirect_uri, challenge, scope, resource, state, now, now + AUTHORIZATION_TTL),
            )
            db.execute("UPDATE oauth_clients SET last_seen=? WHERE client_id=?", (now, client["client_id"]))
            db.execute(
                "DELETE FROM oauth_authorizations WHERE expires_at<? AND approved_at IS NULL AND denied_at IS NULL",
                (now,),
            )
        return rid

    def request(self, request_id: str) -> dict:
        row = self.s.db.one(
            "SELECT a.*,c.client_name FROM oauth_authorizations a JOIN oauth_clients c ON c.client_id=a.client_id WHERE a.id=?",
            (request_id,),
        )
        if not row or row["expires_at"] < time.time():
            raise OAuthError("invalid_request", "Authorization request expired")
        if row["approved_at"] or row["denied_at"]:
            raise OAuthError("invalid_request", "Authorization request already handled")
        return row

    def approve(self, request_id: str, user_id: str) -> str:
        now = time.time()
        with self.s.db.transaction() as db:
            row = db.execute(
                "SELECT a.*,c.client_name FROM oauth_authorizations a JOIN oauth_clients c ON c.client_id=a.client_id WHERE a.id=?",
                (request_id,),
            ).fetchone()
            if not row or row["expires_at"] < now or row["approved_at"] or row["denied_at"]:
                raise OAuthError("invalid_request", "Authorization request expired")
            if row["user_id"] and row["user_id"] != user_id:
                raise OAuthError("access_denied", "Authorization belongs to another account", 403)
            code = token()
            db.execute(
                "INSERT INTO oauth_codes(digest,authorization_id,client_id,user_id,redirect_uri,code_challenge,scope,resource,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    digest(code),
                    row["id"],
                    row["client_id"],
                    user_id,
                    row["redirect_uri"],
                    row["code_challenge"],
                    row["scope"],
                    row["resource"],
                    now + CODE_TTL,
                ),
            )
            db.execute(
                "UPDATE oauth_authorizations SET user_id=?,approved_at=? WHERE id=?",
                (user_id, now, row["id"]),
            )
            audit(
                db,
                user_id,
                "oauth_authorized",
                row["client_id"],
                {"request_id": row["id"], "scope": row["scope"]},
            )
        return _append_query(
            row["redirect_uri"],
            {"code": code, "state": row["state"], "iss": self.base},
        )

    def deny(self, request_id: str) -> str:
        now = time.time()
        with self.s.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM oauth_authorizations WHERE id=?", (request_id,)
            ).fetchone()
            if not row or row["expires_at"] < now or row["approved_at"] or row["denied_at"]:
                raise OAuthError("invalid_request", "Authorization request expired")
            db.execute(
                "UPDATE oauth_authorizations SET denied_at=? WHERE id=?", (now, row["id"])
            )
        return _append_query(
            row["redirect_uri"],
            {
                "error": "access_denied",
                "error_description": "The owner declined the connection",
                "state": row["state"],
                "iss": self.base,
            },
        )

    # ------------------------------------------------------------------
    # Token exchange
    # ------------------------------------------------------------------
    def _issue(self, db, *, client_id, user_id, binding_id, scope, resource, issue_refresh=True, rotated_from=None):
        now = time.time()
        access = token()
        db.execute(
            "INSERT INTO oauth_tokens(digest,kind,client_id,user_id,binding_id,scope,resource,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (digest(access), "access", client_id, user_id, binding_id, scope, resource, now + ACCESS_TTL, now),
        )
        result = {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": ACCESS_TTL,
            "scope": scope,
        }
        if issue_refresh:
            refresh = token()
            db.execute(
                "INSERT INTO oauth_tokens(digest,kind,client_id,user_id,binding_id,scope,resource,expires_at,created_at,rotated_from) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (digest(refresh), "refresh", client_id, user_id, binding_id, scope, resource, now + REFRESH_TTL, now, rotated_from),
            )
            result["refresh_token"] = refresh
        return result

    def token(self, data: dict) -> dict:
        grant = data.get("grant_type")
        if grant == "authorization_code":
            return self._authorization_code(data)
        if grant == "refresh_token":
            return self._refresh(data)
        raise OAuthError("unsupported_grant_type", "Unsupported grant_type")

    def _authorization_code(self, data: dict):
        code = data.get("code", "")
        client = self.client(data.get("client_id", ""))
        redirect_uri = data.get("redirect_uri", "")
        verifier = data.get("code_verifier", "")
        if not isinstance(code, str) or not code:
            raise OAuthError("invalid_request", "code is required")
        now = time.time()
        with self.s.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM oauth_codes WHERE digest=?", (digest(code),)
            ).fetchone()
            if not row or row["used_at"] or row["expires_at"] < now:
                raise OAuthError("invalid_grant", "Authorization code expired or used")
            if row["client_id"] != client["client_id"] or row["redirect_uri"] != redirect_uri:
                raise OAuthError("invalid_grant", "Code does not match this client")
            if not pkce_matches(row["code_challenge"], verifier):
                raise OAuthError("invalid_grant", "PKCE verification failed")
            if data.get("resource") and self._resource(data["resource"]) != row["resource"]:
                raise OAuthError("invalid_target", "Resource does not match the grant")
            binding_id = self.s._oauth_binding(
                db, row["user_id"], client["client_name"]
            )
            db.execute(
                "UPDATE oauth_codes SET used_at=? WHERE digest=?", (now, row["digest"])
            )
            result = self._issue(
                db,
                client_id=row["client_id"],
                user_id=row["user_id"],
                binding_id=binding_id,
                scope=row["scope"],
                resource=row["resource"],
                issue_refresh="offline_access" in row["scope"].split(),
            )
        return result

    def _refresh(self, data: dict):
        raw = data.get("refresh_token", "")
        client = self.client(data.get("client_id", ""))
        if not isinstance(raw, str) or not raw:
            raise OAuthError("invalid_request", "refresh_token is required")
        now = time.time()
        with self.s.db.transaction() as db:
            row = db.execute(
                "SELECT t.*,b.state binding_state FROM oauth_tokens t JOIN bindings b ON b.id=t.binding_id WHERE t.digest=? AND t.kind='refresh'",
                (digest(raw),),
            ).fetchone()
            if (
                not row
                or row["revoked_at"]
                or row["expires_at"] < now
                or row["client_id"] != client["client_id"]
                or row["binding_state"] != "active"
            ):
                raise OAuthError("invalid_grant", "Refresh token expired or revoked")
            if data.get("resource") and self._resource(data["resource"]) != row["resource"]:
                raise OAuthError("invalid_target", "Resource does not match the grant")
            db.execute(
                "UPDATE oauth_tokens SET revoked_at=? WHERE digest=?",
                (now, row["digest"]),
            )
            result = self._issue(
                db,
                client_id=row["client_id"],
                user_id=row["user_id"],
                binding_id=row["binding_id"],
                scope=row["scope"],
                resource=row["resource"],
                rotated_from=row["digest"],
            )
        return result

    def revoke(self, data: dict):
        raw = data.get("token", "")
        if not isinstance(raw, str) or not raw:
            return {}
        summary = digest(raw)
        now = time.time()
        with self.s.db.transaction() as db:
            row = db.execute(
                "SELECT * FROM oauth_tokens WHERE digest=?", (summary,)
            ).fetchone()
            if not row or row["revoked_at"] or row["expires_at"] <= now:
                return {}
            if data.get("client_id") and row["client_id"] != data["client_id"]:
                return {}
            # Revoking either token type disconnects the whole OAuth binding.
            db.execute(
                "UPDATE oauth_tokens SET revoked_at=? WHERE binding_id=? AND client_id=? AND revoked_at IS NULL",
                (now, row["binding_id"], row["client_id"]),
            )
            db.execute(
                "UPDATE bindings SET state='revoked' WHERE id=? AND state='active'",
                (row["binding_id"],),
            )
            db.execute(
                "UPDATE mcp_event_subscriptions SET active=0,updated_at=? WHERE binding_id=? AND user_id=?",
                (now, row["binding_id"], row["user_id"]),
            )
            audit(
                db,
                row["user_id"],
                "oauth_revoked",
                row["client_id"],
                {"binding_id": row["binding_id"]},
            )
        return {}
