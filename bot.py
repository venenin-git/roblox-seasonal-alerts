import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "items.json"
STATE_PATH = ROOT / "state.json"
WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
API_URL = "https://catalog.roblox.com/v1/catalog/items/details"
HEADERS = {
    "User-Agent": "RobloxSeasonalAlerts/1.0 (availability monitor)",
    "Content-Type": "application/json",
}

def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default

def get_item_details(items):
    payload = {
        "items": [
            {"itemType": item["itemType"], "id": int(item["id"])}
            for item in items
        ]
    }
    response = requests.post(API_URL, json=payload, headers=HEADERS, timeout=25)
    response.raise_for_status()
    body = response.json()
    data = body.get("data", [])
    return {
        (str(entry.get("itemType", "")).lower(), int(entry.get("id", -1))): entry
        for entry in data
        if entry.get("id") is not None
    }

def sale_status(entry):
    # Prefer an explicit boolean when the API response includes one.
    explicit = entry.get("isForSale", entry.get("IsForSale"))
    if isinstance(explicit, bool):
        return explicit, entry.get("price", entry.get("Price"))

    # Roblox's catalog API has used ItemStatus values such as "Sale".
    statuses = entry.get("itemStatus") or entry.get("ItemStatus") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    normalized = {str(status).lower() for status in statuses}
    price = entry.get("price", entry.get("Price"))
    # Explicit status is the preferred signal; price is a fallback for response variants.
    if "sale" in normalized or "forsale" in normalized:
        return True, price
    if price is not None:
        try:
            return float(price) > 0, price
        except (TypeError, ValueError):
            pass
    return False, price

def send_discord_alert(item, price, first_run=False):
    if not WEBHOOK_URL:
        raise RuntimeError("A variável DISCORD_WEBHOOK_URL não foi configurada.")
    price_text = f"{int(price):,} Robux".replace(",", ".") if price is not None else "Preço não informado"
    url = item.get("url") or (
        f"https://www.roblox.com/bundles/{item['id']}/"
        if item["itemType"].lower() == "bundle"
        else f"https://www.roblox.com/catalog/{item['id']}/"
    )
    title = "🎃 ITEM SAZONAL DISPONÍVEL!" if not first_run else "🔔 MONITORAMENTO INICIADO: ITEM DISPONÍVEL"
    payload = {
        "username": "Roblox Seasonal Alerts",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": title,
            "description": f"**[{item['name']}]({url})** está marcado como disponível para compra no catálogo do Roblox.",
            "color": 0x2ECC71,
            "fields": [
                {"name": "Preço listado", "value": price_text, "inline": True},
                {"name": "Tipo", "value": "Pacote" if item["itemType"].lower() == "bundle" else "Item", "inline": True},
            ],
            "footer": {"text": "Roblox Seasonal Alerts • Confirme a disponibilidade na página oficial"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }]
    }
    response = requests.post(WEBHOOK_URL, json=payload, timeout=20)
    response.raise_for_status()

def main():
    items = load_json(CONFIG_PATH, [])
    if not items:
        print("Nenhum item configurado em config/items.json")
        return 1

    previous = load_json(STATE_PATH, {})
    try:
        details = get_item_details(items)
    except Exception as exc:
        print(f"Erro consultando o catálogo Roblox: {exc}", file=sys.stderr)
        return 1

    new_state = dict(previous)
    changed = False
    errors = []

    for item in items:
        key = (item["itemType"].lower(), int(item["id"]))
        state_key = f"{key[0]}:{key[1]}"
        entry = details.get(key)

        if entry is None:
            errors.append(f"API não retornou {item['name']} ({state_key})")
            continue

        is_for_sale, price = sale_status(entry)
        was_for_sale = previous.get(state_key, {}).get("is_for_sale")
        print(f"{item['name']}: disponível={is_for_sale}, preço={price}")

        # First successful check: notify if the item is already on sale.
        if was_for_sale is None and is_for_sale:
            send_discord_alert(item, price, first_run=True)
        elif was_for_sale is False and is_for_sale:
            send_discord_alert(item, price)

        # Store only values that matter for alerting; avoid a commit on every poll.
        new_state[state_key] = {
            "is_for_sale": is_for_sale,
            "price": price,
            "name": item["name"],
        }
        changed = True

    if changed:
        STATE_PATH.write_text(json.dumps(new_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if errors:
        for error in errors:
            print(f"AVISO: {error}", file=sys.stderr)
        # Non-zero exit makes failures visible in the Actions run.
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
