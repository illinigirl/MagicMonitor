"""SNS → Pushover forwarder for CloudWatch alarms.

Subscribed to the poller + MCP alarm SNS topics. CloudWatch can't POST to
Pushover's API directly (it needs token/user/message form params, not
SNS's JSON envelope), so this tiny Lambda translates an alarm
notification into a Pushover push. Used instead of email because this
account's Gmail silently drops AWS SNS confirmation mail (2026-06-11).

Pure stdlib (urllib) so the asset needs no pip bundling. Pushover creds
come from the same SSM params the poller's notifier uses.
"""

import json
import os
import urllib.parse
import urllib.request

import boto3

_ssm = boto3.client("ssm")
_APP_TOKEN_PARAM = os.environ["PUSHOVER_APP_TOKEN_PARAM"]
_USER_KEY_PARAM = os.environ["PUSHOVER_USER_KEY_PARAM"]
_PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

_cache: dict[str, str] = {}


def _param(name: str) -> str:
    """Lazily fetch + cache an SSM SecureString for the container lifetime."""
    if name not in _cache:
        resp = _ssm.get_parameter(Name=name, WithDecryption=True)
        _cache[name] = resp["Parameter"]["Value"]
    return _cache[name]


def handler(event, context):
    sent = 0
    for record in event.get("Records", []):
        sns = record.get("Sns", {})
        subject = sns.get("Subject") or "CloudWatch Alarm"
        raw = sns.get("Message", "")
        try:
            msg = json.loads(raw)
            name = msg.get("AlarmName", "alarm")
            state = msg.get("NewStateValue", "")
            reason = msg.get("NewStateReason", "")
            title = f"⚠️ {name} → {state}" if state else f"⚠️ {name}"
            body = reason or subject
        except (ValueError, TypeError):
            # Non-alarm / non-JSON message — forward verbatim.
            title = subject
            body = raw or "(no message)"

        payload = urllib.parse.urlencode({
            "token": _param(_APP_TOKEN_PARAM),
            "user": _param(_USER_KEY_PARAM),
            "title": title[:250],
            "message": body[:1024],
            "priority": 1,  # high — ops alerts bypass quiet hours
        }).encode()
        req = urllib.request.Request(_PUSHOVER_URL, data=payload)
        # Let exceptions propagate: a failed send should surface in the
        # forwarder's own error metric (and SNS will retry), not be
        # swallowed — the whole point is to not miss an alarm.
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        sent += 1
    return {"forwarded": sent}
