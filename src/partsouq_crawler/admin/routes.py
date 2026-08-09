from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Callable
from typing import Any, cast

from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask.typing import ResponseReturnValue

from partsouq_crawler.admin.config import AdminConfig
from partsouq_crawler.admin.db import RequestDatabase
from partsouq_crawler.admin.query_trace import QueryTrace
from partsouq_crawler.admin.repository import (
    ENTITY_SPECS,
    AdminDataError,
    AdminRepository,
    RecordNotFoundError,
    RevisionConflictError,
    entity_spec,
)

DatabaseFactory = Callable[[AdminConfig, QueryTrace], RequestDatabase]

bp = Blueprint(
    "admin",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/admin-static",
)


def _config() -> AdminConfig:
    return cast(AdminConfig, current_app.extensions["partsouq_admin_config"])


def _repository() -> AdminRepository:
    return AdminRepository(cast(RequestDatabase, g.partsouq_admin_database))


def _csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@bp.before_app_request
def open_database() -> None:
    trace = QueryTrace()
    factory = cast(DatabaseFactory, current_app.extensions["partsouq_admin_database_factory"])
    g.partsouq_admin_query_trace = trace
    g.partsouq_admin_database = factory(_config(), trace)


@bp.before_app_request
def verify_csrf() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if (
        not supplied
        or not isinstance(expected, str)
        or not expected
        or not hmac.compare_digest(supplied, expected)
    ):
        abort(400, description="CSRF 驗證失敗")


@bp.teardown_app_request
def close_database(_error: BaseException | None) -> None:
    database = getattr(g, "partsouq_admin_database", None)
    if database is not None:
        database.close()


@bp.after_app_request
def add_query_headers(response: Response) -> Response:
    trace = cast(QueryTrace | None, getattr(g, "partsouq_admin_query_trace", None))
    if trace is not None:
        response.headers["X-Admin-Query-Count"] = str(trace.count)
        response.headers["X-Admin-Query-Tags"] = ",".join(trace.tags)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; img-src 'self'; "
        "script-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@bp.app_context_processor
def template_context() -> dict[str, Any]:
    return {
        "entity_specs": ENTITY_SPECS,
        "csrf_token": _csrf_token,
        "query_trace": getattr(g, "partsouq_admin_query_trace", QueryTrace()),
    }


@bp.app_errorhandler(RecordNotFoundError)
def record_not_found(error: RecordNotFoundError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 404


@bp.app_errorhandler(RevisionConflictError)
def revision_conflict(error: RevisionConflictError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 409


@bp.app_errorhandler(AdminDataError)
def invalid_data(error: AdminDataError) -> tuple[str, int]:
    return render_template("error.html", message=str(error)), 400


@bp.get("/")
def dashboard() -> str:
    return render_template("dashboard.html", counts=_repository().dashboard_counts())


@bp.get("/health")
def health() -> dict[str, object]:
    counts = _repository().dashboard_counts()
    return {"status": "ok", "entities": len(counts)}


@bp.get("/entities/<entity_type>")
def entity_list(entity_type: str) -> str:
    spec = entity_spec(entity_type)
    include_retired = request.args.get("include_retired") == "1"
    page = _repository().list_records(
        entity_type,
        query=request.args.get("q", ""),
        cursor=request.args.get("cursor"),
        limit=_config().page_size,
        include_retired=include_retired,
    )
    return render_template("list.html", spec=spec, page=page)


@bp.route("/entities/<entity_type>/new", methods=["GET", "POST"])
def entity_create(entity_type: str) -> ResponseReturnValue:
    spec = entity_spec(entity_type)
    if request.method == "GET":
        return render_template(
            "edit.html",
            spec=spec,
            record=None,
            payload_json="{}",
            actor=_config().default_actor,
            mode="create",
        )
    payload = _payload_from_form()
    identity_key = _repository().create_manual(
        entity_type,
        payload,
        actor=request.form.get("actor", ""),
        reason=request.form.get("reason", ""),
    )
    flash("已建立人工資料；來源型錄資料未被修改。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


@bp.get("/entities/<entity_type>/<identity_key>")
def entity_detail(entity_type: str, identity_key: str) -> str:
    spec = entity_spec(entity_type)
    detail = _repository().get_record(entity_type, identity_key)
    return render_template(
        "detail.html",
        spec=spec,
        detail=detail,
        actor=_config().default_actor,
    )


@bp.get("/entities/<entity_type>/<identity_key>/edit")
def entity_edit(entity_type: str, identity_key: str) -> str:
    spec = entity_spec(entity_type)
    detail = _repository().get_record(entity_type, identity_key)
    editable = {
        field: detail.record.payload.get(field)
        for field in spec.editable_fields
        if field in detail.record.payload
    }
    return render_template(
        "edit.html",
        spec=spec,
        record=detail.record,
        payload_json=json.dumps(editable, ensure_ascii=False, indent=2, default=str),
        actor=_config().default_actor,
        mode="update",
    )


@bp.post("/entities/<entity_type>/<identity_key>/update")
def entity_update(entity_type: str, identity_key: str) -> ResponseReturnValue:
    _repository().update_record(
        entity_type,
        identity_key,
        _payload_from_form(),
        expected_revision=_revision_from_form(),
        actor=request.form.get("actor", ""),
        reason=request.form.get("reason", ""),
    )
    flash("已新增一筆覆寫版本；來源型錄資料未被修改。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


@bp.post("/entities/<entity_type>/<identity_key>/retire")
def entity_retire(entity_type: str, identity_key: str) -> ResponseReturnValue:
    _repository().retire_record(
        entity_type,
        identity_key,
        expected_revision=_revision_from_form(),
        actor=request.form.get("actor", ""),
        reason=request.form.get("reason", ""),
    )
    flash("資料已停用；沒有刪除來源資料。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


@bp.post("/entities/<entity_type>/<identity_key>/restore")
def entity_restore(entity_type: str, identity_key: str) -> ResponseReturnValue:
    _repository().restore_record(
        entity_type,
        identity_key,
        expected_revision=_revision_from_form(),
        actor=request.form.get("actor", ""),
        reason=request.form.get("reason", ""),
    )
    flash("資料已恢復啟用。", "success")
    return redirect(
        url_for("admin.entity_detail", entity_type=entity_type, identity_key=identity_key)
    )


def _payload_from_form() -> dict[str, Any]:
    raw = request.form.get("payload_json", "")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise AdminDataError(f"JSON 格式錯誤：{error.msg}") from error
    if not isinstance(payload, dict):
        raise AdminDataError("資料內容必須是 JSON object")
    return cast(dict[str, Any], payload)


def _revision_from_form() -> int:
    try:
        revision = int(request.form.get("revision", ""))
    except ValueError as error:
        raise AdminDataError("版本號格式錯誤") from error
    if revision < 0:
        raise AdminDataError("版本號格式錯誤")
    return revision
