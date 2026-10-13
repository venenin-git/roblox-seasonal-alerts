import argparse
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
API_URL = "https://catalog.roblox.com/v1/catalog/items/details"
HEADERS = {
    "User-Agent": "RobloxSeasonalAlerts/1.1 (availability monitor)",
    "Content-Type": "application/json",
}
META_KEY = "_meta"
# O workflow só falha depois de tantas execuções seguidas com erro,
# para não encher o e-mail com falhas passageiras da API.
FAIL_AFTER_CONSECUTIVE_ERRORS = 3

OFF_SALE_VALUES = {"offsale", "offsaledeferred", "notforsale"}
ON_SALE_VALUES = {"sale", "forsale"}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "sim"}


def get_webhook_url():
    return os.getenv("DISCORD_WEBHOOK_URL", "").strip()


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def load_state(path=None):
    """Lê o state.json; se estiver corrompido, avisa e recomeça do zero."""
    path = path or STATE_PATH
    try:
        state = load_json(path, {})
    except (json.JSONDecodeError, OSError) as exc:
        print(f"AVISO: state.json ilegível ({exc}); começando do zero.", file=sys.stderr)
        return {}
    return state if isinstance(state, dict) else {}


def _field(entry, name):
    """Busca um campo aceitando camelCase e PascalCase."""
    if name in entry:
        return entry[name]
    return entry.get(name[:1].upper() + name[1:])


def _norm(value):
    return str(value).replace(" ", "").replace("_", "").lower()


# --------------------------------------------------------------------------
# Catálogo Roblox
# --------------------------------------------------------------------------
def post_with_csrf(session, url, payload):
    """POST com o handshake de X-CSRF-TOKEN que a API do Roblox costuma exigir.

    A primeira chamada pode responder 403 com o token no header
    'x-csrf-token'; nesse caso repetimos a chamada com ele.
    """
    response = session.post(url, json=payload, timeout=25)
    if response.status_code == 403 and response.headers.get("x-csrf-token"):
        session.headers["X-CSRF-TOKEN"] = response.headers["x-csrf-token"]
        response = session.post(url, json=payload, timeout=25)
    response.raise_for_status()
    return response


def get_item_details(items, session=None, debug=False):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    payload = {
        "items": [
            {"itemType": item["itemType"], "id": int(item["id"])}
            for item in items
        ]
    }
    body = post_with_csrf(session, API_URL, payload).json()
    data = body.get("data", [])
    if debug:
        print("DEBUG resposta bruta da API:")
        print(json.dumps(data, indent=2, ensure_ascii=False))
    return {
        (str(entry.get("itemType", "")).lower(), int(entry.get("id", -1))): entry
        for entry in data
        if entry.get("id") is not None
    }


def sale_status(entry):
    """Retorna (à_venda, preço).

    Ordem de confiança: booleano explícito > sinais de "fora de venda" >
    sinais de "à venda" > preço positivo (último recurso, só quando a
    resposta não traz nenhum campo de status).
    """
    price = _field(entry, "price")

    explicit = _field(entry, "isForSale")
    if isinstance(explicit, bool):
        return explicit, price

    price_status = _norm(_field(entry, "priceStatus") or "")
    statuses = _field(entry, "itemStatus") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    normalized = {_norm(s) for s in statuses}

    # Sinais de indisponibilidade têm prioridade sobre qualquer preço listado.
    if price_status in OFF_SALE_VALUES or normalized & OFF_SALE_VALUES:
        return False, price

    if price_status == "free":
        return True, price if price is not None else 0
    if normalized & ON_SALE_VALUES:
        return True, price

    has_status_info = bool(price_status or normalized)
    if (
        not has_status_info
        and isinstance(price, (int, float))
        and not isinstance(price, bool)
    ):
        return price > 0, price
    return False, price


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------
def format_price(price):
    if price is None:
        return "Preço não informado"
    try:
        value = int(price)
    except (TypeError, ValueError):
        return "Preço não informado"
    if value == 0:
        return "Grátis"
    return f"{value:,} Robux".replace(",", ".")


def post_webhook(payload, max_attempts=3):
    """Envia ao Discord respeitando o limite de requisições (HTTP 429)."""
    webhook_url = get_webhook_url()
    if not webhook_url:
        raise RuntimeError("A variável DISCORD_WEBHOOK_URL não foi configurada.")
    for attempt in range(1, max_attempts + 1):
        response = requests.post(webhook_url, json=payload, timeout=20)
        if response.status_code == 429 and attempt < max_attempts:
            try:
                wait = float(response.json().get("retry_after", 2))
            except (ValueError, AttributeError):
                wait = 2.0
            time.sleep(min(wait, 10))
            continue
        response.raise_for_status()
        return


def send_discord_alert(item, price, first_run=False):
    url = item.get("url") or (
        f"https://www.roblox.com/bundles/{item['id']}/"
        if item["itemType"].lower() == "bundle"
        else f"https://www.roblox.com/catalog/{item['id']}/"
    )
    title = (
        "🔔 MONITORAMENTO INICIADO: ITEM DISPONÍVEL"
        if first_run
        else "🎃 ITEM SAZONAL DISPONÍVEL!"
    )
    payload = {
        "username": "Roblox Seasonal Alerts",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": title,
            "description": f"**[{item['name']}]({url})** está marcado como disponível para compra no catálogo do Roblox.",
            "color": 0x2ECC71,
            "fields": [
                {"name": "Preço listado", "value": format_price(price), "inline": True},
                {"name": "Tipo", "value": "Pacote" if item["itemType"].lower() == "bundle" else "Item", "inline": True},
            ],
            "footer": {"text": "Roblox Seasonal Alerts • Confirme a disponibilidade na página oficial"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
    }
    post_webhook(payload)


def send_test_message():
    post_webhook({
        "username": "Roblox Seasonal Alerts",
        "allowed_mentions": {"parse": []},
        "content": "✅ Teste do Roblox Seasonal Alerts: o webhook está funcionando.",
    })


# --------------------------------------------------------------------------
# Execução principal
# --------------------------------------------------------------------------
def run(dry_run=False, debug=False):
    items = load_json(CONFIG_PATH, [])
    if not items:
        print("Nenhum item configurado em config/items.json", file=sys.stderr)
        return 1

    previous = load_state()
    meta = dict(previous.get(META_KEY, {}))
    errors = []

    api_ok = True
    try:
        details = get_item_details(items, debug=debug)
    except Exception as exc:
        api_ok = False
        details = {}
        errors.append(f"Erro consultando o catálogo Roblox: {exc}")

    new_state = dict(previous)

    for item in items:
        key = (item["itemType"].lower(), int(item["id"]))
        state_key = f"{key[0]}:{key[1]}"
        if not api_ok:
            break
        entry = details.get(key)

        if entry is None:
            errors.append(f"API não retornou {item['name']} ({state_key})")
            continue

        is_for_sale, price = sale_status(entry)
        was_for_sale = previous.get(state_key, {}).get("is_for_sale")
        print(f"{item['name']} [{entry.get('name', '?')}]: disponível={is_for_sale}, preço={price}")

        alert = None
        if was_for_sale is None and is_for_sale:
            alert = "primeira execução"
        elif was_for_sale is False and is_for_sale:
            alert = "transição"

        if alert and dry_run:
            print(f"DRY-RUN: enviaria alerta ({alert}) para {item['name']}")
        elif alert:
            try:
                send_discord_alert(item, price, first_run=(alert == "primeira execução"))
            except Exception as exc:
                # Não grava o estado deste item: o alerta será tentado de novo.
                errors.append(f"Falha ao enviar alerta de {item['name']}: {exc}")
                continue

        new_state[state_key] = {
            "is_for_sale": is_for_sale,
            "price": price,
            "name": item["name"],
        }

    # Contador de erros consecutivos (limitado para não gerar commits infinitos).
    if errors:
        meta["consecutive_errors"] = min(
            int(meta.get("consecutive_errors", 0)) + 1, FAIL_AFTER_CONSECUTIVE_ERRORS
        )
    else:
        meta["consecutive_errors"] = 0
    new_state[META_KEY] = meta

    if dry_run:
        print("DRY-RUN: state.json não foi alterado.")
    elif new_state != previous:
        STATE_PATH.write_text(
            json.dumps(new_state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    for error in errors:
        print(f"AVISO: {error}", file=sys.stderr)
    if errors and meta["consecutive_errors"] >= FAIL_AFTER_CONSECUTIVE_ERRORS:
        return 1
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Monitor de itens sazonais do Roblox")
    parser.add_argument("--dry-run", action="store_true",
                        default=env_flag("DRY_RUN"),
                        help="consulta a API, mas não envia alertas nem grava state.json")
    parser.add_argument("--debug", action="store_true",
                        default=env_flag("DEBUG"),
                        help="imprime a resposta bruta da API")
    parser.add_argument("--test-webhook", action="store_true",
                        default=env_flag("TEST_WEBHOOK"),
                        help="envia uma mensagem de teste ao Discord e sai")
    args = parser.parse_args(argv)

    if args.test_webhook:
        try:
            send_test_message()
        except Exception as exc:
            print(f"Falha ao enviar mensagem de teste: {exc}", file=sys.stderr)
            return 1
        print("Mensagem de teste enviada ao Discord.")
        return 0

    return run(dry_run=args.dry_run, debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
