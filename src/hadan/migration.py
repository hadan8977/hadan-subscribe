from __future__ import annotations
import hashlib
import json
from pathlib import Path


def import_archive(service, manifest_path, *, apply=False):
    """Explicit manifests prevent associating screenshots/Excel from different source runs."""
    manifest = json.loads(Path(manifest_path).read_text())
    root = Path(manifest["root"]).resolve()
    items = manifest.get("items")
    if not root.is_dir() or not isinstance(items, list) or len(items) > 1000:
        raise ValueError("Invalid archive root/items")
    planned = []
    identities = set()
    for item in items:
        if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
            raise ValueError("Each archive entry requires id/title")
        title = item["title"]
        body = item.get("body", "已导入的历史报告，见附件。")
        entries = item.get("files", [])
        if (
            not isinstance(title, str)
            or not 1 <= len(title) <= 250
            or "\n" in title
            or "\r" in title
            or not isinstance(body, str)
            or len(body) > 500000
            or not isinstance(entries, list)
            or len(entries) > 12
        ):
            raise ValueError("Archive title, body or attachment list exceeds limits")
        product = item.get("product_id", "quantgt")
        if not service.db.one("SELECT id FROM products WHERE id=?", (product,)):
            raise ValueError("Unknown archive product")
        identity = (product, str(item["id"]))
        if identity in identities:
            raise ValueError("Duplicate archive identity in manifest")
        identities.add(identity)
        files = []
        for entry in entries:
            path = (root / entry["path"]).resolve()
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or path.suffix.lower()
                not in {".xlsx", ".png", ".jpg", ".pdf", ".txt", ".csv", ".json"}
                or any(part.startswith(".") for part in path.relative_to(root).parts)
            ):
                raise ValueError("Archive file outside allowed root/types")
            if path.stat().st_size > service.settings.max_attachment_bytes:
                raise ValueError("Archive attachment too large")
            with path.open("rb") as file:
                sha = hashlib.file_digest(file, "sha256").hexdigest()
            if sha != entry.get("sha256"):
                raise ValueError("Archive checksum does not match explicit manifest")
            files.append({"name": path.name, "path": path, "sha256": sha})
        if sum(f["path"].stat().st_size for f in files) > 32 * 1024**2:
            raise ValueError("Archive edition attachments exceed 32 MiB")
        planned.append((item, product, files))
    results = []
    for item, product, files in planned:
        row = {
            "legacy_id": item["id"],
            "files": [f["name"] for f in files],
            "applied": apply,
        }
        if apply:
            loaded = []
            for f in files:
                data = f["path"].read_bytes()
                if hashlib.sha256(data).hexdigest() != f["sha256"]:
                    raise ValueError("Archive file changed after preview")
                loaded.append({"name": f["name"], "data": data})
            row.update(
                service.publish(
                    product,
                    "legacy:" + str(item["id"]),
                    item["title"],
                    str(item.get("body", "已导入的历史报告，见附件。")),
                    files=loaded,
                    actor="archive-import",
                    baseline=True,
                )
            )
        results.append(row)
    return {"apply": apply, "items": results, "subscriber_mail_sent": False}


def legacy_payments(service):
    rows = []
    for source in service.db.all(
        "SELECT value FROM source_state WHERE key LIKE 'legacy-member:%'"
    ):
        member = json.loads(source["value"])
        for i, item in enumerate(member.get("history", [])):
            payment = item.get("payment") if isinstance(item, dict) else None
            if isinstance(payment, dict):
                rows.append(
                    {
                        "email": member["email"],
                        "history_index": i,
                        "paid_at": payment.get("paid_at") or item.get("at"),
                        **payment,
                        "units": "original legacy units; not converted to new ledger minor units",
                    }
                )
    return rows
