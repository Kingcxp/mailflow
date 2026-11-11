"""Application tools delegate to the same services as manual operations."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from mailflow.domain import PendingOperation, ReplyState
from mailflow.errors import translated_error
from mailflow.permissions import require_admin
from mailflow.plugin_market import MarketPlugin

if TYPE_CHECKING:
    from mailflow.service import MailFlowService


SCHEMAS: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "inspect_app": (
        "Inspect runtime, accounts, models, plugins, gateways or an exact setting key. Plugin query searches configured repositories.",
        {
            "resource": {
                "type": "string",
                "enum": ["runtime", "accounts", "models", "plugins", "gateways", "settings"],
            },
            "query": {"type": "string"},
        },
        ["resource"],
    ),
    "manage_plugin": (
        "Propose installing, uninstalling, enabling or disabling a known plugin. Install includes its dependencies. Never run shell commands.",
        {
            "plugin_id": {"type": "string"},
            "action": {"type": "string", "enum": ["install", "uninstall", "enable", "disable"]},
        },
        ["plugin_id", "action"],
    ),
    "update_mail": (
        "Propose marking mail read/unread, starred or archived. Use IDs from find_mail.",
        {
            "record_ids": {"type": "array", "items": {"type": "string"}},
            **{key: {"type": "boolean"} for key in ("is_read", "starred", "archived")},
        },
        ["record_ids"],
    ),
    "write_reply": (
        "Propose a reply to one mail. send=false saves a draft; send=true sends only after user review. Give the complete reply body.",
        {
            "record_id": {"type": "string"},
            "body": {"type": "string"},
            "send": {"type": "boolean"},
        },
        ["record_id", "body"],
    ),
    "update_setting": (
        "Propose changing one existing setting key discovered with inspect_app. Secrets must be edited in settings, never requested in chat.",
        {
            "key": {"type": "string"},
            "value": {},
        },
        ["key", "value"],
    ),
    "manage_gateway": (
        "Propose starting, stopping or logging in an already configured bot instance.",
        {
            "provider": {"type": "string"},
            "instance_id": {"type": "string"},
            "action": {"type": "string", "enum": ["start", "stop", "login"]},
        },
        ["provider", "instance_id", "action"],
    ),
}


async def stage(
    service: MailFlowService, name: str, args: dict[str, Any]
) -> tuple[str, PendingOperation | None]:
    if name == "inspect_app":
        resource, query = str(args.get("resource", "")), str(args.get("query", "")).strip()
        snap = service.snapshot()
        rows: Any
        if resource == "plugins":
            rows = [
                {
                    "id": p.plugin_id,
                    "name": p.name,
                    "version": p.version,
                    "status": service.plugin_status(p.plugin_id),
                }
                for p in snap.plugins
            ]
            if query:
                report = await service.market_fetch_report()
                rows += [
                    {
                        "id": p.id,
                        "name": p.name,
                        "version": p.version,
                        "source": p.source,
                        "description": p.description_for(service.i18n.language),
                    }
                    for _, p in report.entries
                ]
                rows = [
                    row
                    for row in rows
                    if query.casefold() in json.dumps(row, ensure_ascii=False).casefold()
                ]
        elif resource == "accounts":
            rows = [
                {"id": a.account_id, "email": a.email, "status": a.status} for a in snap.accounts
            ]
        elif resource == "models":
            rows = [{"id": c.llm_id, "model": c.model} for c in service.config.llms]
        elif resource == "gateways":
            rows = [
                {"provider": g.provider, "id": g.instance_id, "status": g.status}
                for g in await service.gateway_instances()
            ]
        elif resource == "settings":
            require_admin(service.t)
            if query:
                option = service.settings_option(query)
                rows = {
                    "key": query,
                    "value": option.display_value() if option else service.t("ux.unsupported"),
                }
            else:
                rows = service.config.general.model_dump(mode="json")
        elif resource == "runtime":
            rows = {
                "running": service.started,
                "accounts": len(snap.accounts),
                "plugins": len(snap.plugins),
            }
        else:
            raise translated_error(ValueError, service.t, "ux.unsupported")
        return json.dumps({"resource": resource, "results": rows}, ensure_ascii=False), None
    require_admin(service.t)
    if name not in SCHEMAS:
        raise translated_error(ValueError, service.t, "ux.unsupported")
    values = dict(args)
    description = service.t(f"assistant.operation_{name}")
    if name == "manage_plugin":
        identity, action = str(args.get("plugin_id", "")), str(args.get("action", ""))
        if action not in {"install", "uninstall", "enable", "disable"}:
            raise translated_error(ValueError, service.t, "ux.unsupported")
        if action == "install":
            found = await asyncio.to_thread(service.market.find, identity)
            if found is None:
                raise translated_error(
                    KeyError, service.t, "plugin.market_not_found", plugin_id=identity
                )
            plugin = found[1]
            values["plugin"] = plugin.model_dump(mode="json")
            description += f"\n{identity} · {plugin.version}\n{plugin.source}\n" + service.t(
                "assistant.plugin_dependencies"
            )
        else:
            await service._require_known_plugin(identity)  # pyright: ignore[reportPrivateUsage]
            description += f"\n{identity} · {action}"
    elif name == "write_reply":
        record = await service.get_mail(str(args.get("record_id", "")))
        if record is None or not str(args.get("body", "")).strip():
            raise translated_error(ValueError, service.t, "ux.unsupported")
        description += f"\n{record.mail.account_id} → {(record.mail.reply_to or record.mail.sender).address}\n{record.mail.subject}\n{args['body']}"
        description += "\n" + service.t(
            "assistant.send_mail" if args.get("send") else "assistant.save_draft"
        )
    elif name == "update_setting":
        option = service.settings_option(str(args.get("key", "")))
        if (
            option is None
            or option.secret
            or "admins" in option.key
            or "repositories" in option.key
        ):
            raise translated_error(ValueError, service.t, "assistant.use_settings")
        description += f"\n{option.key}: {option.display_value()} → {args.get('value')}"
    elif name == "manage_gateway":
        if args.get("action") not in {"start", "stop", "login"}:
            raise translated_error(ValueError, service.t, "ux.unsupported")
        if not any(
            n.notifier_id == args.get("instance_id")
            and n.options.get("gateway") == args.get("provider")
            for n in service.config.notifiers
        ):
            raise translated_error(ValueError, service.t, "assistant.use_settings")
        description += (
            f"\n{args.get('provider')} · {args.get('instance_id')} · {args.get('action')}"
        )
    elif name == "update_mail":
        description += "\n" + json.dumps(args, ensure_ascii=False)
    return description, PendingOperation(
        tool=name,
        arguments=values,
        summary_key="assistant.preview",
        summary_params={"description": description},
    )


async def apply(service: MailFlowService, operation: PendingOperation) -> Any:
    require_admin(service.t)
    args = operation.arguments
    if operation.tool == "manage_plugin":
        identity, action = str(args["plugin_id"]), str(args["action"])
        if action == "install":
            plugin = MarketPlugin.model_validate(args["plugin"])
            return await service._install_plugin(plugin, source=plugin.source)  # pyright: ignore[reportPrivateUsage]
        if action == "uninstall":
            return await service.plugin_uninstall(identity)
        if action == "enable":
            return await service.plugin_enable(identity)
        if action == "disable":
            return await service.plugin_disable(identity)
    elif operation.tool == "update_mail":
        result = await service.set_mail_state(
            args["record_ids"],
            **{k: v for k, v in args.items() if k in {"is_read", "starred", "archived"}},
        )
        if result.failed:
            raise translated_error(RuntimeError, service.t, "errors.operation")
        return result
    elif operation.tool == "write_reply":
        draft = await service.get_draft(str(args["_draft_id"])) if args.get("_draft_id") else None
        if draft is None:
            draft = await service.create_reply(str(args["record_id"]))
            args["_draft_id"] = draft.draft_id
        if draft.state is ReplyState.SENT:
            return draft
        if draft.state is ReplyState.SENDING:
            raise translated_error(RuntimeError, service.t, "reply.send_uncertain")
        draft = await service.edit_draft(draft.draft_id, draft.subject, str(args["body"]))
        if args.get("send"):
            draft = await service.prepare_reply(draft.draft_id)
            return await service.confirm_reply(draft.draft_id, draft.token or "")
        return draft
    elif operation.tool == "update_setting":
        return await service.set_setting(str(args["key"]), args["value"])
    elif operation.tool == "manage_gateway":
        provider, identity = str(args["provider"]), str(args["instance_id"])
        if args["action"] == "stop":
            return await service.gateway_shutdown(provider, identity)
        config = next(n for n in service.config.notifiers if n.notifier_id == identity)
        return await service.gateway_provision(provider, identity, dict(config.options))
    raise translated_error(ValueError, service.t, "ux.unsupported")
